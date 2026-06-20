"""Context window monitoring and auto-summarization."""

from __future__ import annotations

from typing import Any

import httpx

CONTEXT_WINDOW_API = "https://lcw-api.blp.sh/context-window?model={model}"
# One cache keyed by model; the API returns both `contextWindow` and
# `maxOutputTokens` in a single response, so we fetch and stash both
# rather than making two HTTP calls. The cache also remembers the case
# where the API returned 0 / null for either field, so a transient
# failure doesn't re-hit the endpoint on every turn.
_MODEL_CACHE: dict[str, dict[str, int]] = {}
SUMMARIZE_THRESHOLD = 0.8

# Hard floor for max_output_tokens. Some providers (Anthropic in particular)
# default to 1024 if you don't set it explicitly, which is far too small
# for our Write tool to handle anything larger than a tiny code snippet:
# a 4KB JS file blows past 1024 output tokens and silently truncates
# mid-line, leaving a half-written file on disk. We always inject at
# least this many tokens unless the API tells us the real cap is lower.
DEFAULT_MAX_OUTPUT_TOKENS = 16384

# Hard ceiling. The lcw-api sometimes reports `maxOutputTokens` values
# far above what the upstream API will actually accept in a single
# request — Anthropic's Messages API rejects very large `max_tokens`
# with "Streaming is required for operations that may take longer than
# 10 minutes." We cap at 16K, which is plenty for the Write tool
# (≈64KB of code) and stays within every provider's per-request limit.
MAX_OUTPUT_TOKENS_CEILING = 16384


def _fetch_model_caps(model: str) -> dict[str, int]:
    """Fetch context + max-output caps from lcw-api; cache per-model."""

    if model in _MODEL_CACHE:
        return _MODEL_CACHE[model]
    result: dict[str, int] = {}
    try:
        url = CONTEXT_WINDOW_API.format(model=model)
        with httpx.Client(timeout=10.0) as client:
            r = client.get(url)
            r.raise_for_status()
            data = r.json()
        payload = data.get("data") or {}
        window = payload.get("contextWindow")
        max_out = payload.get("maxOutputTokens")
        if isinstance(window, int) and window > 0:
            result["contextWindow"] = window
        if isinstance(max_out, int) and max_out > 0:
            result["maxOutputTokens"] = max_out
    except Exception:
        pass
    _MODEL_CACHE[model] = result
    return result


def get_context_window(model: str) -> int | None:
    """Get the context window size for a model from the API."""

    return _fetch_model_caps(model).get("contextWindow")


def get_max_output_tokens(model: str) -> int:
    """Get the model's max output token cap, bounded for safety.

    Order of preference:
    1. lcw-api's `maxOutputTokens` for the model.
    2. lcw-api's `contextWindow` (most APIs cap output at the context
       window; if a separate `maxOutputTokens` isn't published, this is
       a reasonable proxy).
    3. `DEFAULT_MAX_OUTPUT_TOKENS` (16K) — a hard floor because some
       providers (Anthropic) default to 1024 otherwise, which silently
       truncates Write tool calls and corrupts files.

    The result is also clamped to `MAX_OUTPUT_TOKENS_CEILING` because
    lcw-api's `maxOutputTokens` can be the *theoretical* streaming cap
    (e.g. 512K), not the per-request non-streaming cap that we send.
    """

    caps = _fetch_model_caps(model)
    raw = caps.get("maxOutputTokens") or caps.get("contextWindow") or DEFAULT_MAX_OUTPUT_TOKENS
    return min(raw, MAX_OUTPUT_TOKENS_CEILING)


def make_summarize_hook(model: str, session_name: str) -> Any:
    """Create a hook that summarizes when context window is 80% full.

    Calls the LLM to produce a structured summary of the conversation:
    - What was done so far
    - What is currently in progress
    - What needs to be done next

    Reimplements `coreouto.contrib.hooks.auto_summarize_hook` with one
    critical difference: if `summarize_fn` ever returns a non-iterable
    (None, dict, scalar), coreouto's stock hook does
    `messages.clear(); messages.extend(summarized)` which both wipes the
    conversation AND raises `'NoneType' object is not iterable`. Our
    wrapper refuses to clear messages unless the summarizer returned a
    real list, so a single buggy summarizer can't destroy a turn.
    """

    window = get_context_window(model)
    if not window:
        return lambda **kwargs: None

    threshold = int(window * SUMMARIZE_THRESHOLD)

    def summarizer(messages: list[Any]) -> list[Any]:
        if len(messages) <= 2:
            return messages

        system_msgs = [m for m in messages if m.role == "system"]
        other_msgs = [m for m in messages if m.role != "system"]

        existing_summary: str | None = None
        msgs_to_summarize: list[Any] = []

        for m in other_msgs:
            if m.role == "user" and m.content.startswith("[Summary"):
                existing_summary = m.content
            else:
                msgs_to_summarize.append(m)

        conversation_text: list[str] = []
        if existing_summary:
            conversation_text.append(f"Previous summary:\n{existing_summary}")

        for m in msgs_to_summarize:
            if m.role == "user" and m.content:
                conversation_text.append(f"User: {m.content}")
            elif m.role == "assistant" and m.content:
                conversation_text.append(f"Agent: {m.content}")
            elif m.role == "tool" and m.content:
                conversation_text.append(f"Tool: {m.content[:500]}")

        conversation = "\n".join(conversation_text)

        summary_prompt = (
            "Summarize the following conversation into three sections:\n"
            "1. DONE: What has been completed so far\n"
            "2. IN PROGRESS: What is currently being worked on\n"
            "3. NEXT: What needs to be done next\n\n"
            "Be concise but specific. Include file paths, command names, "
            "and concrete details.\n\n"
            f"Conversation:\n{conversation}"
        )

        from coreouto._types import Message

        try:
            import coreouto as co
            summary_agent = co.Agent(co.AgentConfig(
                name="summarizer",
                model=model,
                provider="",
                system_prompt="You are a conversation summarizer. Produce concise, structured summaries.",
                max_iterations=1,
            ))
            result = summary_agent.call_sync(summary_prompt)
            summary_content = f"[Summary]\n{result.content}"
        except Exception:
            summary_content = (
                "[Summary]\n"
                "Unable to generate LLM summary. Continuing with truncated context."
            )

        summary_msg = Message(role="user", content=summary_content)
        return [*system_msgs, summary_msg]

    total: list[int] = [0]

    def hook(*, iteration: int, messages: list[Any], response: Any, **_kwargs: Any) -> None:
        if response is None or getattr(response, "usage", None) is None:
            return
        total[0] += response.usage.total_tokens
        if total[0] < threshold:
            return
        summarized = summarizer(messages)
        if not isinstance(summarized, list):
            from rich.console import Console
            Console(stderr=True).print(
                f"[yellow]⚠ summarize_fn returned {type(summarized).__name__} "
                f"(expected list); keeping original messages.[/yellow]",
                highlight=False,
            )
            return
        messages.clear()
        messages.extend(summarized)

    return hook
