"""Context window monitoring: model caps, and the explicit summary trigger.

Two duties, deliberately kept apart:

* **Caps** — the lma-backed lookups (`get_max_output_tokens`,
  `get_context_window`) that pin the output floor and the window size. This
  is invariant 4's machinery and is unchanged.
* **The summary trigger** — this harness never summarizes *implicitly*: no
  compaction, no silent history rewrite, no second LLM rewriting the
  transcript behind the model's back. Instead :func:`make_context_monitor`
  watches how full the window is and, at
  :data:`CONTEXT_SUMMARY_TRIGGER_PCT`, drops **one** request into the session
  inbox. The existing BEFORE_LLM_CALL inbox hook delivers it to the model as
  a user message; the text itself (what to write, where, and the
  `continue_session` follow-up) belongs to `core/summary.py`.

Delivery through the inbox rather than a direct `messages.append` is what
makes the request visible: it is recorded, replayed and rendered exactly
like any other alarm.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from . import inbox
from . import lma as lma_api
from .summary import build_summary_request, summary_dir_for

# The measured context usage (percent of the window) at which the model is
# asked to summarize. Deliberately late: at this point the remaining window
# is still enough to write the summary documents and hand off, which is the
# only thing left that needs context.
CONTEXT_SUMMARY_TRIGGER_PCT = 80

# Source stamped on the injected summary request, so a receiver can tell a
# context trigger apart from a delegated run's completion alarm.
SUMMARY_SOURCE = "context"

# Rough chars-per-token ratio used ONLY by the estimate fallback in
# `_estimate_tokens` (for providers that report no usage at all).
_CHARS_PER_TOKEN = 4

# Hard floor for max_output_tokens. Some providers (Anthropic in particular)
# default to 1024 if you don't set it explicitly, which is far too small
# for a file-writing tool call (a 4KB JS file blows past 1024 output
# tokens and silently truncates mid-line, leaving a half-written file on
# disk). We always inject at
# least this many tokens unless the API tells us the real cap is lower.
DEFAULT_MAX_OUTPUT_TOKENS = 16384

# Cache: (model, provider) → {contextWindow, maxOutputTokens}. A cached
# `{}` is meaningful — it means "lma answered, and had no info for this
# key" — and we should not re-hit it every turn. A *failed* fetch is NOT
# cached: caching the `{}` a transport error produced would pin the key
# to "no caps" for the life of the process (compaction off, 16K floor
# applied) off a single network blip. Same rule as lma's own `_CACHE`
# (see AGENTS.md invariant 13).
_MODEL_CACHE: dict[tuple[str, str], dict[str, int]] = {}


def clear_model_cache() -> None:
    """Drop every cached model-cap lookup (mirror of `lma.clear_cache()`)."""
    _MODEL_CACHE.clear()


def _fetch_model_caps(model: str, provider_name: str | None = None) -> dict[str, int]:
    key = (model or "", (provider_name or "").lower())
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached
    result: dict[str, int] = {}
    try:
        info = lma_api.get_model(model, provider_name)
    except Exception:
        # Uncached fallback: the caller gets the 16K floor and no context
        # window for this call, and the next call retries lma.
        return {}
    if info:
        cw = info.get("context_window")
        mo = info.get("max_output_tokens")
        if isinstance(cw, int) and cw > 0:
            result["contextWindow"] = cw
        if isinstance(mo, int) and mo > 0:
            result["maxOutputTokens"] = mo
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

def _estimate_tokens(messages: Any) -> int | None:
    """Fallback occupancy estimate when a provider reports no usage at all.

    Counts the characters actually in the live transcript and divides by
    `_CHARS_PER_TOKEN`. `None` when the messages carry no text, so the
    caller stays silent rather than guessing.
    """

    if not isinstance(messages, list):
        return None
    chars = 0
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            chars += len(content)
        elif content is not None:
            chars += len(str(content))
        for call in getattr(message, "tool_calls", None) or []:
            chars += len(str(getattr(call, "arguments", None) or ""))
    return chars // _CHARS_PER_TOKEN if chars else None


def _tokens_used(response: Any, messages: Any) -> int | None:
    """How full the context window is, in tokens — provider accounting first.

    `response.usage.prompt_tokens` is the size of the request that was
    just sent, i.e. the true occupancy; `total_tokens` (prompt +
    completion) is the next best and is what providers that omit the split
    still report. Only when neither exists does the chars/4 estimate over
    the live messages stand in.
    """

    usage = getattr(response, "usage", None)
    if usage is not None:
        for attr in ("prompt_tokens", "total_tokens"):
            value = getattr(usage, attr, None)
            if isinstance(value, int) and value > 0:
                return value
    return _estimate_tokens(messages)


def make_context_monitor(
    *, model: str, session: str, provider_name: str | None = None
) -> Callable[..., None] | None:
    """Build the ON_ITERATION hook that ASKS for a summary at the threshold.

    Returns `None` when the window size is unknown — without it there is no
    threshold to measure against, and inventing a window would fire the
    request at an arbitrary moment.

    Fires at most once per hook instance, and `build_runtime` builds a fresh
    one per turn (the same lifecycle the watchdog trackers have), so the
    guard is per turn without any module-level state to reset.
    """

    window = get_context_window(model, provider_name)
    if not window or not session or not session.strip():
        return None

    fired = False

    def hook(*, messages: Any = None, response: Any = None, **_kwargs: Any) -> None:
        nonlocal fired
        if fired:
            return
        used = _tokens_used(response, messages)
        if used is None:
            return
        pct = used * 100 // window
        if pct < CONTEXT_SUMMARY_TRIGGER_PCT:
            return
        fired = True
        # A broken inbox must not take the turn down with it (the same
        # rule the delivery hook follows) — the request is a courtesy, not
        # part of the loop's contract.
        with contextlib.suppress(Exception):
            inbox.insert(
                session,
                build_summary_request(
                    session=session,
                    pct=pct,
                    window_tokens=window,
                    used_tokens=used,
                    summary_dir=summary_dir_for(session),
                ),
                kind=inbox.ALARM_KIND,
                source=SUMMARY_SOURCE,
            )

    return hook
