"""Chat runner: prepare runtime, run a single prompt, persist history."""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from typing import Any

import coreouto as co
from rich.console import Console

from ..storage import sessions as session_store
from ..storage.sessions import TurnRecord
from .context import get_max_output_tokens
from .events import EventSink, LoopEvent, NullSink
from .runtime import (
    ChatOverrides,
    build_runtime,
    current_subagent_depth,
    current_subagent_id,
    resolve_runtime_from_settings,
    set_subagent_observer,
)

# Failure diagnostics still go straight to stderr so a sink-aware caller
# (e.g. the TUI) doesn't have to opt in to error rendering.
_fail_console = Console(stderr=True, soft_wrap=False, highlight=False)

# Per-turn diagnostics: the last tool call observed (if any). When
# `agent.call_sync` raises, we print these to stderr so the user can see
# which tool was the proximate cause — most "'NoneType' object is not
# iterable" / "list index out of range" / "tool not found" errors fire
# on the *next* operation after a malformed tool call, and without this
# trail the traceback alone often points into coreouto internals with
# no clue about the offending input.
_tool_trace: list[dict[str, Any]] = []
_tool_trace_lock = threading.Lock()


@dataclass
class ChatOptions:
    prompt: str
    session: str | None = None
    provider: str | None = None
    model: str | None = None
    style: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    continue_session: bool = False


def _actor_label() -> tuple[str, str | None]:
    """Return (actor_label, subagent_id) for the current hook context.

    Inside a subagent invocation the id ContextVar is always set, so the
    label is `subagent-<6hex>`; the depth-only fallback exists solely for
    defensive robustness (the two vars are set together in the wrapper).
    """

    sid = current_subagent_id()
    if sid:
        return f"subagent-{sid}", sid
    if current_subagent_depth() > 0:
        return "subagent", None
    return "outo", None


class _RecordingSink:
    """Wrap an EventSink, capturing every LoopEvent for the session turn."""

    def __init__(self, inner: EventSink) -> None:
        self._inner = inner
        self.events: list[dict[str, Any]] = []

    def begin_working(self) -> None:
        self._inner.begin_working()

    def update_activity(self, text: str) -> None:
        self._inner.update_activity(text)

    def end_working(self) -> None:
        self._inner.end_working()

    def emit_loop_event(self, event: LoopEvent) -> None:
        self.events.append(event.to_dict())
        self._inner.emit_loop_event(event)

    def emit_final_answer(self, content: str, session_name: str) -> None:
        self._inner.emit_final_answer(content, session_name)


def run_chat(opts: ChatOptions, sink: EventSink | None = None) -> str:
    """Build the runtime, run a single turn, return the final reply.

    `sink` receives every internal-loop event (tool calls, intermediate
    model text) and the final answer. Pass `None` (or omit) for a
    `NullSink` that swallows everything — useful for tests.
    """

    raw_sink = sink if sink is not None else NullSink()
    sink = _RecordingSink(raw_sink)

    runtime = resolve_runtime_from_settings(
        ChatOverrides(provider=opts.provider, model=opts.model, style=opts.style)
    )
    provider_config: dict[str, Any] = {}
    if opts.max_tokens is not None:
        provider_config["max_tokens"] = opts.max_tokens
    else:
        # Default to the model's real cap so the LLM can emit multi-KB
        # tool calls (e.g. heredoc file writes) without hitting
        # Anthropic's 1024 hard default
        # and silently truncating the file content mid-line.
        provider_config["max_tokens"] = get_max_output_tokens(
            runtime.model, runtime.provider_name
        )
    if opts.temperature is not None:
        provider_config["temperature"] = opts.temperature

    on_tool_call = _make_tool_call_dispatcher(sink)

    agent = build_runtime(
        runtime,
        provider_config=provider_config,
        on_tool_call=on_tool_call,
        on_response=_make_response_dispatcher(sink),
        on_thinking=_make_thinking_dispatcher(sink),
        on_iteration=_make_iteration_dispatcher(sink),
        on_provider_error=_make_provider_error_dispatcher(sink),
    )

    session_name = opts.session or runtime.session
    core_msgs = _load_coreouto_history(session_name, opts.continue_session)

    with _tool_trace_lock:
        _tool_trace.clear()

    set_subagent_observer(_make_subagent_dispatcher(sink))
    sink.begin_working()
    try:
        try:
            response = agent.call_sync(opts.prompt, history=core_msgs)
        except Exception as exc:
            sink.end_working()
            _persist_turn(session_name, None, opts, sink, assistant="")
            _dump_failure_diagnostics(exc, session_name)
            raise
    finally:
        sink.end_working()
        set_subagent_observer(None)

    final = response.content
    _persist_turn(session_name, response, opts, sink, assistant=final)
    sink.emit_final_answer(final, session_name)
    return final


def _persist_turn(
    session_name: str,
    response: Any,
    opts: ChatOptions,
    sink: _RecordingSink,
    *,
    assistant: str,
) -> None:
    """Rewrite restorable history and append the display turn.

    History = `Response.messages` minus system messages (coreouto always
    prepends a fresh system prompt on the next call, so persisting it
    would duplicate it every turn — see coreouto examples/21). On a failed
    turn (response=None) the previous on-disk history is kept as-is.
    """

    try:
        if response is not None:
            history = [
                _dump_message(m) for m in response.messages if m.role != "system"
            ]
        else:
            history = session_store.load(session_name).history
        session_store.record_turn(
            session_name,
            history=history,
            turn=TurnRecord(user=opts.prompt, assistant=assistant, events=sink.events),
        )
    except Exception:
        pass  # persistence must never mask the turn's real outcome


def _dump_message(m: Any) -> dict[str, Any]:
    try:
        return m.model_dump(mode="json")
    except Exception:
        # Media blocks with raw bytes may not survive JSON mode; degrade to
        # the text content rather than losing the whole transcript.
        return {
            "role": m.role,
            "content": m.content if isinstance(m.content, str) else "",
        }


def _make_tool_call_dispatcher(sink: EventSink):
    """Build the per-tool-call callback wired into the BEFORE_TOOL_CALL hook."""

    def on_tool_call(name: str, arguments: dict[str, Any]) -> None:
        _validate_tool_call_args(name, arguments)
        actor, sid = _actor_label()

        with _tool_trace_lock:
            _tool_trace.append({"name": name, "arguments": dict(arguments or {})})

        if name == "call_subagent":
            # No event here: the subagent observer emits `subagent_start`
            # with the minted id right after, which is the canonical line
            # (`subagent-<6hex>: <task preview>`) in both CLI and TUI.
            return
        if name in ("Bash", "Image", "Video", "Audio"):
            preview = _short_arg_summary(name, arguments)
            sink.emit_loop_event(
                LoopEvent(
                    actor=actor,
                    kind="tool",
                    text=f"{name} {preview}",
                    tool_name=name,
                    subagent_id=sid,
                )
            )
            sink.update_activity(actor if sid else name)

    return on_tool_call


def _make_subagent_dispatcher(sink: EventSink):
    """Build the subagent lifecycle callback for `set_subagent_observer`.

    Receives (phase, sid, text) from the wrapped `call_subagent` handler —
    "start" carries the task brief, "end" the final result or error. This
    is the only place the minted subagent id exists at event level; the
    BEFORE_TOOL_CALL hook for `call_subagent` itself still runs in the
    parent context and never sees the id.
    """

    def on_subagent(phase: str, sid: str, text: str) -> None:
        actor = f"subagent-{sid}"
        # The full text goes into the event (and thus the session turn and
        # the TUI detail screen); sinks truncate for their own display.
        if phase == "start":
            sink.emit_loop_event(
                LoopEvent(
                    actor=actor,
                    kind="subagent_start",
                    text=text,
                    tool_name="call_subagent",
                    subagent_id=sid,
                )
            )
            sink.update_activity(actor)
        else:
            sink.emit_loop_event(
                LoopEvent(
                    actor=actor,
                    kind="subagent_end",
                    text=text or "done",
                    subagent_id=sid,
                )
            )

    return on_subagent


def _make_thinking_dispatcher(sink: EventSink):
    """Build the per-thinking callback wired into the ON_THINKING hook."""

    def on_thinking(thinking: str) -> None:
        actor, sid = _actor_label()
        sink.emit_loop_event(
            LoopEvent(actor=actor, kind="thinking", text=thinking, subagent_id=sid)
        )

    return on_thinking


def _make_response_dispatcher(sink: EventSink):
    """Build the per-LLM-response callback wired into the AFTER_LLM_CALL hook.

    Only intermediate responses (those followed by a tool call) are emitted.
    The terminal response is rendered separately via `sink.emit_final_answer`
    so we don't print the answer twice.
    """

    def on_response(content: str, has_tool_calls: bool) -> None:
        if not content or not has_tool_calls:
            return
        actor, sid = _actor_label()
        sink.emit_loop_event(
            LoopEvent(actor=actor, kind="response", text=content, subagent_id=sid)
        )

    return on_response


def _make_provider_error_dispatcher(sink: EventSink):
    """Build the per-provider-error callback wired into ON_PROVIDER_ERROR.

    Rule-matched provider errors (coreouto >= 0.10 `error_handling`) no
    longer raise out of `call_sync` — they retry, terminate with the
    rule's message, or feed back as a tool result. Without this hook a
    retry storm or a 401 would be invisible until the final answer.
    Forward every match as a `provider:` loop event so the user sees the
    status code and the reaction taken.
    """

    def on_provider_error(
        *,
        status_code: int | None,
        error_message: str,
        reaction: str,
        reaction_message: str,
        **_kwargs: Any,
    ) -> None:
        code = f"HTTP {status_code}" if status_code is not None else "error"
        detail = reaction_message or error_message
        sink.emit_loop_event(
            LoopEvent(
                actor="provider",
                kind="error",
                text=f"{code} → {reaction}: {detail}",
            )
        )
        if reaction == "retry":
            sink.update_activity("provider retry")

    return on_provider_error


def _make_iteration_dispatcher(sink: EventSink):
    """Build the per-iteration callback wired into the ON_ITERATION hook.

    Emits a `context` loop event with the iteration number and cumulative
    token usage so the user sees the agent is making progress even between
    tool calls. Without this, the loop is silent from the moment the prompt
    is sent until the first tool call or terminal answer — there's no signal
    that work is happening at all.
    """

    cumulative: list[int] = [0]

    def on_iteration(*, iteration: int, messages: Any, response: Any, **_kwargs: Any) -> None:
        usage = getattr(response, "usage", None) if response else None
        tokens = getattr(usage, "total_tokens", None) if usage else None
        if isinstance(tokens, int) and tokens > 0:
            cumulative[0] = tokens
        actor, sid = _actor_label()
        text = f"iter {iteration}"
        if cumulative[0]:
            text += f" · {cumulative[0]} tokens"
        sink.emit_loop_event(
            LoopEvent(actor=actor, kind="context", text=text, subagent_id=sid)
        )

    return on_iteration


def _dump_failure_diagnostics(exc: BaseException, session_name: str) -> None:
    """Print the last tool calls and a traceback to stderr.

    Called when `agent.call_sync` raises. The goal is to give the user enough
    context to tell whether the failure is in miniouto (bad argument shape,
    missing tool, hook bug) or in coreouto (provider quirk, model output
    parsing) without having to re-run with a debugger attached.
    """

    with _tool_trace_lock:
        recent = list(_tool_trace)

    _fail_console.print(
        f"\n[red]✗ {type(exc).__name__}:[/red] {exc}",
        highlight=False,
    )
    if recent:
        _fail_console.print(
            f"[red]Last tool call before failure ({len(recent)} total this turn):[/red]"
        )
        for entry in recent[-5:]:
            name = entry.get("name")
            args = entry.get("arguments") or {}
            summary = _short_arg_summary(name, args) if name in _LOGGABLE_TOOL_NAMES else repr(args)[:160]
            _fail_console.print(f"  - {name}: {summary}")
    else:
        _fail_console.print(
            "[red]No tool call was observed before the failure — the error "
            "fired during model setup, provider call, or response parsing.[/red]"
        )
    _fail_console.print("[red]Traceback:[/red]")
    _fail_console.print(traceback.format_exc(), highlight=False)


def _load_coreouto_history(session: str, continue_session: bool) -> list[co.Message] | None:
    """Rebuild coreouto Messages from the session's persisted history.

    Records are raw `Message.model_dump` dicts; invalid entries degrade to
    a plain text message instead of aborting the resume. Returns None when
    there is nothing to prepend (fresh session or --continue not given).
    """

    if not continue_session:
        return None
    records = session_store.load(session).history
    if not records:
        return None
    out: list[co.Message] = []
    for d in records:
        try:
            out.append(co.Message.model_validate(d))
        except Exception:
            role = d.get("role") if d.get("role") in ("user", "assistant", "tool") else "user"
            content = d.get("content")
            out.append(co.Message(role=role, content=content if isinstance(content, str) else ""))
    return out or None


_LOGGABLE_TOOL_NAMES = (
    "Bash", "Image", "Video", "Audio", "call_subagent"
)


def _validate_tool_call_args(name: str, arguments: Any) -> None:
    """Reject malformed tool calls early with a clear, attributable error.

    coreouto 0.3.2's agent loop calls `tool.handler(**tool_call.arguments)`
    without first checking that `arguments` is a dict. When the LLM produces
    `{"name": "Bash", "arguments": null}` (or any non-dict) — for example
    because the JSON got truncated, the model lost track of which schema
    field it was filling, or the provider's tool_use parser saw a partial
    block — Python raises the cryptic `TypeError: 'NoneType' object is not
    iterable` from `f(**None)`. The LLM then sees that error in the tool
    result and may keep retrying the same broken call until max_iterations.

    Raising here, before the handler is invoked, gives the LLM a single,
    precise message about which argument is missing — and it propagates
    out through coreouto's `try/except` in `agent.py:250-255` so the
    normal tool-error feedback path still applies when arguments is a
    dict but missing required keys. For non-dict cases the diagnostic
    runner in `_dump_failure_diagnostics` will surface the offending
    tool name and (when present) the last successful tool call.
    """

    if isinstance(arguments, dict):
        return
    if arguments is None:
        hint = "the model emitted `arguments: null` for this tool call"
    else:
        hint = f"expected a JSON object for `arguments`, got {type(arguments).__name__}"
    raise ToolCallArgsError(
        f"Tool {name!r} was called with malformed arguments: {hint}. "
        "Re-emit the call with all required fields populated."
    )


class ToolCallArgsError(Exception):
    pass


def _short_arg_summary(name: str, args: dict[str, Any]) -> str:
    if name == "Bash":
        cmd = (args.get("command") or "").replace("\n", " ")
        return cmd
    if name in ("Image", "Video", "Audio"):
        return args.get("file_path", "?")
    return str(args)[:120]
