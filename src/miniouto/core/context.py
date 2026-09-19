"""Context window monitoring and auto-summarization."""

from __future__ import annotations

from typing import Any

from . import lma as lma_api

SUMMARIZE_THRESHOLD = 0.8

# Hard floor for max_output_tokens. Some providers (Anthropic in particular)
# default to 1024 if you don't set it explicitly, which is far too small
# for a file-writing tool call (a 4KB JS file blows past 1024 output
# tokens and silently truncates mid-line, leaving a half-written file on
# disk). We always inject at
# least this many tokens unless the API tells us the real cap is lower.
DEFAULT_MAX_OUTPUT_TOKENS = 16384

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


def _provider_caps_override(provider_name: str | None) -> dict[str, int]:
    # Read fresh on every call — the TUI is long-lived and edits these
    # mid-session, so caching here would freeze stale overrides.
    if not provider_name:
        return {}
    try:
        from ..storage import providers as provider_store

        p = provider_store.get(provider_name)
    except Exception:
        return {}
    if p is None:
        return {}
    out: dict[str, int] = {}
    if isinstance(p.max_context_window, int) and p.max_context_window > 0:
        out["contextWindow"] = p.max_context_window
    if isinstance(p.max_output_tokens, int) and p.max_output_tokens > 0:
        out["maxOutputTokens"] = p.max_output_tokens
    return out


def get_context_window(model: str, provider_name: str | None = None) -> int | None:
    override = _provider_caps_override(provider_name).get("contextWindow")
    if override:
        return override
    return _fetch_model_caps(model, provider_name).get("contextWindow")


def get_max_output_tokens(model: str, provider_name: str | None = None) -> int:
    """Returns the model's max output token cap.

    Order of preference:
    1. The per-provider `max_output_tokens` override set in the TUI
       custom-model editor.
    2. lma's `max_output_tokens` for the (model, provider) pair.
    3. lma's `context_window` (most APIs cap output at the context
       window; if a separate cap isn't published, this is a proxy).
    4. `DEFAULT_MAX_OUTPUT_TOKENS` (16K) — a hard floor because some
       providers (Anthropic) default to 1024 otherwise, which silently
       truncates long tool calls (e.g. file writes) and corrupts files.
    """

    override = _provider_caps_override(provider_name).get("maxOutputTokens")
    if override:
        return override
    caps = _fetch_model_caps(model, provider_name)
    return caps.get("maxOutputTokens") or caps.get("contextWindow") or DEFAULT_MAX_OUTPUT_TOKENS


def make_summarize_hook(model: str, session_name: str, provider_name: str | None = None) -> Any:
    """Create a hook that compacts the live message list at 80% context.

    The compaction is a **context handoff**, not a prose summary: the agent
    continues the task with ONLY the compacted message, so everything it
    still needs — the verbatim task, file paths, commands run, decisions,
    errors, next steps — must survive inside it. Three hard-won rules:

    - The conversation fed to the summarizer LLM must include the tool
      CALLS (which command produced which result), not just truncated
      result tails — otherwise the handoff cannot name what was done.
    - The compacted message embeds the current task verbatim (extracted
      deterministically, never LLM-paraphrased) and an explicit frame
      telling the agent this is the authoritative record: hunting session
      files or a global `.miniouto` for "the lost transcript" is a known
      confusion failure this frame exists to prevent.
    - The summarizer agent gets its own `max_tokens` — without it the
      active provider's low default (Anthropic: 1024) silently truncates
      the handoff mid-section, which is the same confusion by another door.

    Reimplements `coreouto.contrib.hooks.auto_summarize_hook` with one
    critical difference: if the summarizer ever returns a non-iterable
    (None, dict, scalar), coreouto's stock hook does
    `messages.clear(); messages.extend(summarized)` which both wipes the
    conversation AND raises `'NoneType' object is not iterable`. Our
    wrapper refuses to clear messages unless the summarizer returned a
    real list, so a single buggy summarizer can't destroy a turn.

    Returns `(on_iteration, before_llm_call)` — two hooks. The compaction
    itself runs in `before_llm_call`, NOT in `on_iteration`: coreouto
    fires ON_ITERATION *before* executing the response's tool calls, so
    compacting there would wipe the just-appended assistant tool_use and
    leave the upcoming tool results orphaned (provider 400: "tool result's
    tool id not found"). BEFORE_LLM_CALL of the next iteration is the
    safe point — every tool-call pair in the list is complete.
    `before_llm_call` (and the summarizer it awaits) must be **async**:
    the hook fires inside coreouto's running event loop, where a
    synchronous `call_sync` (its own `asyncio.run`) raises "cannot be
    called from a running event loop" — with the old sync hook the LLM
    handoff silently never worked and every compaction fell back.
    """

    window = get_context_window(model, provider_name)
    if not window:
        noop = lambda **kwargs: None  # noqa: E731
        return noop, noop

    threshold = int(window * SUMMARIZE_THRESHOLD)

    def _compact_frame(task_verbatim: str, handoff: str) -> str:
        frame = (
            "[Summary — compacted context]\n"
            "The earlier conversation was compacted to fit the context "
            "window. This message REPLACES that transcript and is the "
            "authoritative record of everything that happened so far — "
            "everything still relevant is already in here.\n"
            "Rules:\n"
            "- Do NOT search the filesystem for the original conversation, "
            "session files, transcripts, or a `.miniouto` directory to "
            "'recover' context — there is nothing more to recover, and "
            "hunting for it only wastes turns.\n"
            "- If a detail you need is genuinely absent, re-derive it from "
            "the actual project files on disk (they are the ground truth "
            "and already reflect all completed work), or ask the user.\n"
            "- Continue the task from the NEXT section of the handoff.\n"
        )
        parts = [frame]
        if task_verbatim:
            parts.append(f"\n## Current task (verbatim, do not re-ask)\n{task_verbatim}")
        parts.append(f"\n## Context handoff\n{handoff}")
        return "".join(parts)

    def _serialize_conversation(
        existing_summary: str | None, msgs_to_summarize: list[Any]
    ) -> str:
        import json

        lines: list[str] = []
        if existing_summary:
            lines.append(f"Previous compaction (already summarized; carry its facts forward):\n{existing_summary}")

        for m in msgs_to_summarize:
            if m.role == "user" and m.content:
                lines.append(f"User: {m.content}")
            elif m.role == "assistant":
                if m.content:
                    lines.append(f"Agent: {m.content}")
                # The tool CALLS are the record of what was actually done —
                # without them the results below are unattributable output.
                for tc in getattr(m, "tool_calls", None) or []:
                    try:
                        args = json.dumps(tc.arguments or {}, ensure_ascii=False)
                    except Exception:
                        args = repr(getattr(tc, "arguments", None))
                    lines.append(f"Agent tool call: {tc.name}({args})")
            elif m.role == "tool" and m.content:
                name = getattr(m, "name", None) or "tool"
                text = m.content if isinstance(m.content, str) else str(m.content)
                lines.append(f"Tool result [{name}]: {text[:4000]}")
        return "\n".join(lines)

    async def summarizer(messages: list[Any]) -> list[Any]:
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

        # Deterministic verbatim task extraction — never trust the LLM to
        # preserve the user's exact request.
        task_verbatim = ""
        for m in reversed(msgs_to_summarize):
            if m.role == "user" and m.content and not m.content.startswith("[Summary"):
                task_verbatim = m.content
                break

        conversation = _serialize_conversation(existing_summary, msgs_to_summarize)

        summary_prompt = (
            "You are writing a CONTEXT HANDOFF that will REPLACE the "
            "conversation for the working agent. The agent continues the "
            "task with ONLY this document — every detail it still needs "
            "must survive here. Write it for the agent, not for a human "
            "reviewer.\n\n"
            "Produce exactly these sections:\n"
            "1. TASK — what the user asked for, in order; keep key "
            "phrasing verbatim (requests, constraints, preferences)\n"
            "2. DONE — completed work: file paths, commands run, outcomes\n"
            "3. IN PROGRESS — the exact step in flight when compaction "
            "happened\n"
            "4. FILES — every file read/created/modified: path + one line "
            "on its role and what changed in it\n"
            "5. DECISIONS — design choices made, alternatives rejected, "
            "user-imposed constraints, forbidden actions\n"
            "6. ERRORS — failures hit, root causes identified, fixes "
            "applied, unresolved ones\n"
            "7. NEXT — remaining steps in order, plus the verification "
            "commands that will prove completion\n\n"
            "Rules: paths, commands, and identifiers verbatim — never "
            "paraphrased. Specifics over prose. No preamble, no "
            "meta-commentary.\n\n"
            f"Conversation to compact:\n{conversation}"
        )

        from coreouto._types import Message

        try:
            import coreouto as co
            # Resolve the output cap the same way the main agent does
            # (provider TUI override > lma max_output_tokens > lma
            # context_window > DEFAULT_MAX_OUTPUT_TOKENS floor), fresh at
            # compaction time so mid-session override edits apply. A
            # hardcoded value either truncates the handoff (too small) or
            # gets the request rejected by the provider (too large — the
            # failure mode observed with 8192 on providers whose real cap
            # is lower).
            summary_agent = co.Agent(co.AgentConfig(
                name="summarizer",
                model=model,
                provider=provider_name or "",
                system_prompt=(
                    "You are a context-compaction engine. You produce "
                    "complete, dense, structured handoff documents for a "
                    "working agent. You never chat."
                ),
                max_iterations=1,
                provider_config={
                    "max_tokens": get_max_output_tokens(model, provider_name)
                },
            ))
            result = await summary_agent.call(summary_prompt)
            handoff = result.content or ""
        except Exception as exc:
            # Visible, not silent: a summarizer that fails every compaction
            # silently degrades every long turn to the fallback tail.
            from rich.console import Console
            Console(stderr=True).print(
                f"[yellow]⚠ LLM summarization failed "
                f"({type(exc).__name__}: {exc}); falling back to verbatim "
                "tail.[/yellow]",
                highlight=False,
            )
            # Deterministic fallback: a truncated recent tail of the real
            # transcript preserves far more than a content-free apology.
            handoff = (
                "(LLM summarization failed — verbatim tail of the recent "
                f"conversation follows)\n{conversation[-6000:]}"
            )

        summary_msg = Message(role="user", content=_compact_frame(task_verbatim, handoff))
        return [*system_msgs, summary_msg]

    total: list[int] = [0]
    pending: list[bool] = [False]

    def on_iteration(*, iteration: int, messages: list[Any], response: Any, **_kwargs: Any) -> None:
        if response is None or getattr(response, "usage", None) is None:
            return
        total[0] += response.usage.total_tokens
        if total[0] >= threshold:
            pending[0] = True

    async def before_llm_call(*, messages: list[Any], **_kwargs: Any) -> None:
        if not pending[0]:
            return
        pending[0] = False
        summarized = await summarizer(messages)
        if not isinstance(summarized, list):
            from rich.console import Console
            Console(stderr=True).print(
                "[yellow]⚠ summarize_fn returned "
                f"{type(summarized).__name__} (expected list); "
                "keeping original messages.[/yellow]",
                highlight=False,
            )
            return
        messages.clear()
        messages.extend(summarized)
        # Reset after compaction (coreouto examples/23 pattern): without
        # this, every later iteration that reports usage re-triggers the
        # LLM summarizer, turning one compaction into a per-iteration tax.
        total[0] = 0

    return on_iteration, before_llm_call
