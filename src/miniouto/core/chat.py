"""Chat runner: prepare runtime, run a single prompt, persist history."""

from __future__ import annotations

import asyncio
import threading
import time
import traceback
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import coreouto as co
from rich.console import Console

from ..storage import sessions as session_store
from ..storage.sessions import STATUS_DONE, STATUS_INTERRUPTED
from .context import get_max_output_tokens
from .events import EventSink, LoopEvent, NullSink
from .runtime import (
    WATCHDOG_MAX_WAKEUPS,
    ChatOverrides,
    LoopCancelledError,
    build_runtime,
    current_subagent_depth,
    current_subagent_id,
    resolve_runtime_from_settings,
    sanitize_history,
    set_subagent_observer,
    supervised_run,
)

# Failure diagnostics still go straight to stderr so a sink-aware caller
# (e.g. the TUI) doesn't have to opt in to error rendering.
_fail_console = Console(stderr=True, soft_wrap=False, highlight=False)

# Per-turn diagnostics: the last tool call observed (if any). When the
# turn raises out of the supervised loop, we print these to stderr so the user can see
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
    subagent_provider: str | None = None
    subagent_model: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    reasoning: str | None = None
    continue_session: bool = False
    # Cooperative cancellation: once set, the loop raises LoopCancelledError
    # at the next hook boundary (before the next LLM call / tool execution).
    cancel_event: threading.Event | None = None


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
        # Set by run_chat once the session name is known: called with each
        # recorded event so the persister can flush it to disk.
        self.on_event: Any = None

    def begin_working(self) -> None:
        self._inner.begin_working()

    def update_activity(self, text: str) -> None:
        self._inner.update_activity(text)

    def end_working(self) -> None:
        self._inner.end_working()

    def emit_loop_event(self, event: LoopEvent) -> None:
        self.events.append(event.to_dict())
        if self.on_event is not None:
            self.on_event(event)
        self._inner.emit_loop_event(event)

    def emit_final_answer(self, content: str, session_name: str) -> None:
        self._inner.emit_final_answer(content, session_name)


class _TurnPersister:
    """Incrementally persist the in-flight turn so a force-kill loses nothing.

    `run_chat` used to write the session only after the loop returned, so
    a SIGKILL / terminal close / hard crash mid-turn erased the whole turn
    — reopening the session showed the *previous* turn's answer as the
    last thing that happened. Instead we:

    - append the turn as `status="running"` before the loop starts;
    - rewrite its events as they stream in (throttled for high-frequency,
      low-value `thinking`/`context` events; structural events flush
      immediately);
    - snapshot the sanitized live history at every outo iteration
      (`persist_history`), so a resumed session continues from real
      mid-turn progress instead of rolling back to the previous turn.

    Combined with the atomic session save, the worst case on SIGKILL is a
    missing sub-second thinking line. Every method swallows its own
    errors — persistence must never break or mask the agent loop.
    """

    _THROTTLED_KINDS = ("thinking", "context")
    _MIN_INTERVAL_SECONDS = 0.5

    def __init__(self, session_name: str) -> None:
        self._session = session_name
        self._events: list[dict[str, Any]] = []
        self._last_write = 0.0

    def begin(self, prompt: str) -> None:
        with suppress(Exception):
            session_store.begin_turn(self._session, prompt)

    def on_event(self, event: LoopEvent) -> None:
        with suppress(Exception):
            now = time.monotonic()
            throttled = event.kind in self._THROTTLED_KINDS
            if throttled and now - self._last_write < self._MIN_INTERVAL_SECONDS:
                return
            self._last_write = now
            session_store.update_turn_events(self._session, self._events)

    def track(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def persist_history(self, messages: Any) -> None:
        with suppress(Exception):
            clean = sanitize_history(messages)
            if not clean:
                return
            session_store.update_history(
                self._session, [_dump_message(m) for m in clean]
            )


def run_chat(opts: ChatOptions, sink: EventSink | None = None) -> str:
    """Build the runtime, run a single turn, return the final reply.

    `sink` receives every internal-loop event (tool calls, intermediate
    model text) and the final answer. Pass `None` (or omit) for a
    `NullSink` that swallows everything — useful for tests.
    """

    raw_sink = sink if sink is not None else NullSink()
    sink = _RecordingSink(raw_sink)

    runtime = resolve_runtime_from_settings(
        ChatOverrides(
            provider=opts.provider,
            model=opts.model,
            style=opts.style,
            subagent_provider=opts.subagent_provider,
            subagent_model=opts.subagent_model,
        )
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

    session_name = opts.session or runtime.session
    core_msgs = _load_coreouto_history(session_name, opts.continue_session)

    persister = _TurnPersister(session_name)
    persister.track(sink.events)
    persister.begin(opts.prompt)
    sink.on_event = persister.on_event

    on_tool_call = _make_tool_call_dispatcher(sink)

    base_on_iteration = _make_iteration_dispatcher(sink)

    def on_iteration(*, iteration: int, messages: Any, response: Any) -> None:
        base_on_iteration(iteration=iteration, messages=messages, response=response)
        # Hooks are global — a subagent's iterations fire this too, but
        # their transcript is not the session's restorable history.
        if current_subagent_depth() == 0:
            persister.persist_history(messages)

    def on_history_repair(messages: Any) -> None:
        # The 400/422 repair hook mutates the live list in place; without
        # this bridge the fix would be in-memory only and the NEXT turn
        # would reload the same poisoned history from disk (and 400
        # again). Depth-gated like on_iteration: a subagent's repaired
        # transcript is not the session's history.
        if current_subagent_depth() == 0:
            persister.persist_history(messages)

    agent = build_runtime(
        runtime,
        provider_config=provider_config,
        on_tool_call=on_tool_call,
        on_response=_make_response_dispatcher(sink),
        on_thinking=_make_thinking_dispatcher(sink),
        on_iteration=on_iteration,
        on_provider_error=_make_provider_error_dispatcher(sink),
        on_history_repair=on_history_repair,
        on_tool_result=_make_tool_result_dispatcher(sink),
        reasoning=opts.reasoning,
        cancel_event=opts.cancel_event,
    )

    with _tool_trace_lock:
        _tool_trace.clear()

    set_subagent_observer(_make_subagent_dispatcher(sink))
    sink.begin_working()
    try:
        try:
            response = asyncio.run(
                _run_with_watchdog(
                    agent, opts.prompt, core_msgs,
                    sink=sink, cancel_event=opts.cancel_event,
                )
            )
        except LoopCancelledError:
            # User force-stopped the loop — a clean cancel, not a failure:
            # persist the turn but skip the failure diagnostics dump.
            sink.end_working()
            _finish_turn(session_name, None, opts, sink, status=STATUS_INTERRUPTED)
            raise
        except Exception as exc:
            sink.end_working()
            # Record-only (not emitted live — the caller renders its own
            # error line) so a session reload shows why the turn ended.
            sink.events.append(
                LoopEvent(
                    actor="miniouto", kind="error",
                    text=f"{type(exc).__name__}: {exc}",
                ).to_dict()
            )
            _finish_turn(session_name, None, opts, sink, status=STATUS_INTERRUPTED)
            _dump_failure_diagnostics(exc, session_name)
            raise
    finally:
        sink.end_working()
        set_subagent_observer(None)

    final = response.content
    _finish_turn(session_name, response, opts, sink, status=STATUS_DONE, assistant=final)
    sink.emit_final_answer(final, session_name)
    return final


def _finish_turn(
    session_name: str,
    response: Any,
    opts: ChatOptions,
    sink: _RecordingSink,
    *,
    status: str,
    assistant: str = "",
) -> None:
    """Stamp the running turn finished and rewrite restorable history.

    History = `Response.messages` minus system messages (coreouto always
    prepends a fresh system prompt on the next call, so persisting it
    would duplicate it every turn — see coreouto examples/21). On a failed
    or cancelled turn (response=None) the incrementally persisted on-disk
    history is kept — it already reflects real mid-turn progress.
    """

    try:
        history = None
        if response is not None:
            history = [
                _dump_message(m) for m in response.messages if m.role != "system"
            ]
        session_store.finish_turn(
            session_name,
            prompt=opts.prompt,
            assistant=assistant,
            status=status,
            events=sink.events,
            history=history,
        )
    except Exception:
        pass  # persistence must never mask the turn's real outcome


async def _run_with_watchdog(
    agent: co.Agent,
    prompt: str,
    history: list[co.Message] | None,
    *,
    sink: EventSink,
    cancel_event: threading.Event | None,
    states: tuple[Any, Any, dict[str, Any]] | None = None,
) -> co.Response:
    """Run outo's turn under the stall watchdog — see runtime.supervised_run.

    Level 0 supervisor: it defers to any active subagent supervisor, so a
    wedged subagent is recovered in place (its own sanitized transcript)
    instead of taking the whole turn down. Emits `watchdog:` loop events
    on each wakeup and wires the user's cancel_event into the fast path.
    """

    def on_wakeup(wakeups: int, silent: float, phase: Any) -> None:
        sink.emit_loop_event(
            LoopEvent(
                actor="watchdog",
                kind="wakeup",
                text=(
                    f"no activity for {silent:.0f}s (phase={phase!r}) "
                    f"— restarting the turn ({wakeups}/{WATCHDOG_MAX_WAKEUPS})"
                ),
            )
        )
        sink.update_activity("watchdog wakeup")

    return await supervised_run(
        lambda p, h: agent.call(p, history=h),
        prompt,
        history,
        live_key="outo",
        level=0,
        on_wakeup=on_wakeup,
        cancel_event=cancel_event,
        states=states,
    )


_MEDIA_BLOCK_TYPES = ("image", "video", "audio", "document")


def _flatten_media_blocks(blocks: list[Any]) -> str:
    """Flatten content blocks to plain text, replacing media with placeholders.

    Media tool results (Image/Video/Audio) reach the transcript as raw
    provider wire dicts (coreouto's anthropic provider builds them via
    `Message.model_construct`, bypassing validation), so they fail
    `Message.model_validate` on reload — and even when they survive,
    re-sending base64 media on every resumed turn wastes tokens. Text is
    kept verbatim; each media block becomes a one-line placeholder.
    """

    parts: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            text = b.get("text")
            if text:
                parts.append(text)
        elif t in _MEDIA_BLOCK_TYPES:
            source = b.get("source") or {}
            mime = b.get("mime_type") or source.get("media_type") or "unknown"
            parts.append(f"[{t} omitted from restored history: {mime}]")
    return "\n".join(parts)


def _dump_message(m: Any) -> dict[str, Any]:
    try:
        dumped = m.model_dump(mode="json")
    except Exception:
        # Media blocks with raw bytes may not survive JSON mode; degrade to
        # a text placeholder but PRESERVE tool pairing fields — dropping
        # tool_call_id makes the provider reject the whole restored
        # request (HTTP 400) on the next turn.
        d: dict[str, Any] = {
            "role": m.role,
            "content": m.content
            if isinstance(m.content, str)
            else "[media omitted from restored history]",
        }
        for attr in ("tool_call_id", "name"):
            v = getattr(m, attr, None)
            if v is not None:
                d[attr] = v
        tcs = getattr(m, "tool_calls", None)
        if tcs:
            with suppress(Exception):
                d["tool_calls"] = [tc.model_dump(mode="json") for tc in tcs]
        _coerce_dumped_tool_args(d)
        return d
    _coerce_dumped_tool_args(dumped)
    content = dumped.get("content")
    if isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") in _MEDIA_BLOCK_TYPES for b in content
    ):
        dumped["content"] = _flatten_media_blocks(content)
    return dumped


def _coerce_dumped_tool_args(dumped: dict[str, Any]) -> None:
    """Rewrite non-dict tool_call `arguments` to {} in a dumped message.

    A malformed call (arguments: null) can reach the live message list via
    coreouto's validation-bypassing constructors. Persisting it as-is
    poisons the session: the record fails `Message.model_validate` on the
    next load, and the old fallback silently dropped the tool calls. With
    the coercion, the record round-trips; if the provider still rejects
    the empty object, the runtime repair hook cuts the turn.
    """

    tcs = dumped.get("tool_calls")
    if isinstance(tcs, list):
        for tc in tcs:
            if isinstance(tc, dict) and not isinstance(tc.get("arguments"), dict):
                tc["arguments"] = {}


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
            # detail carries the FULL untruncated command for Bash (the
            # preview flattens newlines; the TUI detail view wants the
            # verbatim input). Media tools have no useful extra payload.
            detail = arguments.get("command") if name == "Bash" else None
            sink.emit_loop_event(
                LoopEvent(
                    actor=actor,
                    kind="tool",
                    text=f"{name} {preview}",
                    tool_name=name,
                    subagent_id=sid,
                    detail=detail,
                )
            )
            sink.update_activity(actor if sid else name)

    return on_tool_call


_TOOL_RESULT_NAMES = ("Bash", "Image", "Video", "Audio")
_TOOL_RESULT_MAX = 4000


def _make_tool_result_dispatcher(sink: EventSink):
    """Build the per-tool-result callback wired into the AFTER_TOOL_CALL hook.

    coreouto fires AFTER_TOOL_CALL with the handler's ToolResult, so this
    is the only place a sink can see tool return values. Emits a
    `LoopEvent(kind="tool_result")` whose text is the flattened result
    (prefixed with "error: " on failure, mirroring the subagent_end
    convention the TUI keys on), truncated to 4000 chars. `call_subagent`
    is skipped — the subagent observer's `subagent_end` event already
    carries that result. The dispatcher must never raise: a sink failure
    here must not break the agent loop.
    """

    def on_tool_result(name: str, result: Any) -> None:
        try:
            if name == "call_subagent" or name not in _TOOL_RESULT_NAMES:
                return
            text = result.flatten_text()
            if result.is_error:
                text = f"error: {text}"
            if len(text) > _TOOL_RESULT_MAX:
                text = text[:_TOOL_RESULT_MAX] + "\n… [truncated]"
            actor, sid = _actor_label()
            sink.emit_loop_event(
                LoopEvent(
                    actor=actor,
                    kind="tool_result",
                    text=text,
                    tool_name=name,
                    subagent_id=sid,
                )
            )
        except Exception:
            pass  # a sink/hook failure must never break the agent loop

    return on_tool_result


def _make_subagent_dispatcher(sink: EventSink):
    """Build the subagent lifecycle callback for `set_subagent_observer`.

    Receives (phase, sid, text) from the wrapped `call_subagent` handler —
    "start" carries the task brief, "wakeup" a supervisor restart notice,
    "end" the final result or error. This is the only place the minted
    subagent id exists at event level; the BEFORE_TOOL_CALL hook for
    `call_subagent` itself still runs in the parent context and never
    sees the id.
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
        elif phase == "wakeup":
            # The subagent's own supervisor restarted it after a stall —
            # surfaced as a watchdog event attributed to the invocation.
            sink.emit_loop_event(
                LoopEvent(actor=actor, kind="wakeup", text=text, subagent_id=sid)
            )
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

    Called when the turn raises out of the supervised loop. The goal is to
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
            if isinstance(content, list):
                content = _flatten_media_blocks(content)
            elif not isinstance(content, str):
                content = ""
            kwargs: dict[str, Any] = {"role": role, "content": content}
            # Preserve tool pairing: a tool message without tool_call_id
            # makes the provider reject the whole request (HTTP 400).
            if role == "tool":
                kwargs["tool_call_id"] = d.get("tool_call_id")
                kwargs["name"] = d.get("name")
            # Same for assistant tool calls: validation fails when any
            # `arguments` is not a dict (e.g. the model emitted null and
            # the turn died before the repair hook could cut it). Coerce
            # to {} instead of dropping the calls — dropping them erases
            # what the assistant was doing (the "session lost" symptom)
            # and orphans the matching tool results, while a {} call is
            # handled by the repair hook's malformed-turn cut if the
            # provider still rejects it.
            if role == "assistant":
                tcs = d.get("tool_calls")
                if isinstance(tcs, list):
                    clean_tcs = []
                    for tc in tcs:
                        if not isinstance(tc, dict):
                            continue
                        args = tc.get("arguments")
                        if not isinstance(args, dict):
                            args = {}
                        clean_tcs.append(
                            {
                                "id": tc.get("id") or "",
                                "name": tc.get("name") or "",
                                "arguments": args,
                            }
                        )
                    if clean_tcs:
                        kwargs["tool_calls"] = clean_tcs
            out.append(co.Message(**kwargs))
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
