"""Context window monitoring and auto-summarization."""

from __future__ import annotations

from typing import Any

from . import lma as lma_api

SUMMARIZE_THRESHOLD = 0.8

# Hard floor for max_output_tokens. Some providers (Anthropic in particular)
# default to 1024 if you don't set it explicitly, which is far too small
# for our Write tool to handle anything larger than a tiny code snippet:
# a 4KB JS file blows past 1024 output tokens and silently truncates
# mid-line, leaving a half-written file on disk. We always inject at
# least this many tokens unless the API tells us the real cap is lower.
DEFAULT_MAX_OUTPUT_TOKENS = 16384

# Hard ceiling. lma sometimes reports `maxOutputTokens` values far above
# what the upstream API will actually accept in a single request —
# Anthropic's Messages API rejects very large `max_tokens` with
# "Streaming is required for operations that may take longer than 10
# minutes." We cap at 16K, which is plenty for the Write tool (≈64KB
# of code) and stays within every provider's per-request limit.
MAX_OUTPUT_TOKENS_CEILING = 16384

# Cache: (model, provider) → {contextWindow, maxOutputTokens}. A cached
# `{}` is meaningful — it means "lma had no info for this key" and we
# should not re-hit it every turn.
_MODEL_CACHE: dict[tuple[str, str], dict[str, int]] = {}


def _fetch_model_caps(model: str, provider_name: str | None = None) -> dict[str, int]:
    key = (model or "", (provider_name or "").lower())
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached
    result: dict[str, int] = {}
    try:
        info = lma_api.get_model(model, provider_name)
        if info:
            cw = info.get("context_window")
            mo = info.get("max_output_tokens")
            if isinstance(cw, int) and cw > 0:
                result["contextWindow"] = cw
            if isinstance(mo, int) and mo > 0:
                result["maxOutputTokens"] = mo
    except Exception:
        pass
    _MODEL_CACHE[key] = result
    return result


def get_context_window(model: str, provider_name: str | None = None) -> int | None:
    return _fetch_model_caps(model, provider_name).get("contextWindow")


def get_max_output_tokens(model: str, provider_name: str | None = None) -> int:
    """Returns the model's max output token cap, bounded for safety.

    Order of preference:
    1. lma's `max_output_tokens` for the (model, provider) pair.
    2. lma's `context_window` (most APIs cap output at the context
       window; if a separate cap isn't published, this is a proxy).
    3. `DEFAULT_MAX_OUTPUT_TOKENS` (16K) — a hard floor because some
       providers (Anthropic) default to 1024 otherwise, which silently
       truncates Write tool calls and corrupts files.

    The result is also clamped to `MAX_OUTPUT_TOKENS_CEILING` because
    lma's `max_output_tokens` can be the *theoretical* streaming cap
    (e.g. 512K), not the per-request non-streaming cap that we send.
    """

    caps = _fetch_model_caps(model, provider_name)
    raw = caps.get("maxOutputTokens") or caps.get("contextWindow") or DEFAULT_MAX_OUTPUT_TOKENS
    return min(raw, MAX_OUTPUT_TOKENS_CEILING)


def make_summarize_hook(model: str, session_name: str, provider_name: str | None = None) -> Any:
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

    window = get_context_window(model, provider_name)
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
