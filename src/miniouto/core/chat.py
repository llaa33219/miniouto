"""Chat runner: prepare runtime, run a single prompt, persist history."""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from typing import Any

import coreouto as co
from rich.console import Console

from ..storage import sessions as session_store
from ..storage.sessions import MessageRecord
from .context import get_max_output_tokens
from .runtime import (
    ChatOverrides,
    build_runtime,
    current_subagent_depth,
    resolve_runtime_from_settings,
)

_hook_console = Console(stderr=True, soft_wrap=False, highlight=False)

# Per-turn diagnostics: the last tool call observed (if any) and the index of
# the iteration that produced it. When `agent.call_sync` raises, we print
# these to stderr so the user can see which tool was the proximate cause —
# most "'NoneType' object is not iterable" / "list index out of range" /
# "tool not found" errors fire on the *next* operation after a malformed
# tool call, and without this trail the traceback alone often points into
# coreouto internals with no clue about the offending input.
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


def run_chat(opts: ChatOptions) -> str:
    """Build the runtime, run a single turn, return the final reply."""

    runtime = resolve_runtime_from_settings(
        ChatOverrides(provider=opts.provider, model=opts.model, style=opts.style)
    )
    provider_config: dict[str, Any] = {}
    if opts.max_tokens is not None:
        provider_config["max_tokens"] = opts.max_tokens
    else:
        # Default to the model's real cap so the LLM can emit multi-KB
        # Write tool calls without hitting Anthropic's 1024 hard default
        # and silently truncating the file content mid-line.
        provider_config["max_tokens"] = get_max_output_tokens(runtime.model)
    if opts.temperature is not None:
        provider_config["temperature"] = opts.temperature

    agent = build_runtime(
        runtime,
        provider_config=provider_config,
        on_tool_call=_log_tool_call,
    )

    session_name = opts.session or runtime.session
    history = _load_history(session_name, opts.continue_session)
    core_msgs = _to_coreouto_history(history) if history else None
    session_store.append(session_name, MessageRecord(role="user", content=opts.prompt))

    with _tool_trace_lock:
        _tool_trace.clear()

    try:
        response = agent.call_sync(opts.prompt, history=core_msgs)
    except Exception as exc:
        _dump_failure_diagnostics(exc, session_name)
        raise

    final = response.content

    last_assistant = next(
        (m for m in reversed(response.messages) if m.role == "assistant"),
        None,
    )
    tool_calls = (
        [tc.model_dump() for tc in (last_assistant.tool_calls or [])]
        if last_assistant
        else []
    )
    session_store.append(
        session_name,
        MessageRecord(role="assistant", content=final, tool_calls=tool_calls),
    )
    return final


def _dump_failure_diagnostics(exc: BaseException, session_name: str) -> None:
    """Print the last tool calls and a traceback to stderr.

    Called when `agent.call_sync` raises. The goal is to give the user enough
    context to tell whether the failure is in miniouto (bad argument shape,
    missing tool, hook bug) or in coreouto (provider quirk, model output
    parsing) without having to re-run with a debugger attached.
    """

    with _tool_trace_lock:
        recent = list(_tool_trace)

    Console(stderr=True).print(
        f"\n[red]✗ {type(exc).__name__}:[/red] {exc}",
        highlight=False,
    )
    if recent:
        Console(stderr=True).print(
            f"[red]Last tool call before failure ({len(recent)} total this turn):[/red]"
        )
        for entry in recent[-5:]:
            name = entry.get("name")
            args = entry.get("arguments") or {}
            summary = _short_arg_summary(name, args) if name in _LOGGABLE_TOOL_NAMES else repr(args)[:160]
            Console(stderr=True).print(f"  - {name}: {summary}")
    else:
        Console(stderr=True).print(
            "[red]No tool call was observed before the failure — the error "
            "fired during model setup, provider call, or response parsing.[/red]"
        )
    Console(stderr=True).print("[red]Traceback:[/red]")
    Console(stderr=True).print(traceback.format_exc(), highlight=False)


def _load_history(session: str, continue_session: bool) -> list[MessageRecord]:
    if not continue_session:
        return []
    return session_store.load(session)


def _to_coreouto_history(messages: list[MessageRecord]) -> list[co.Message]:
    out: list[co.Message] = []
    for m in messages:
        tool_calls = None
        if m.tool_calls:
            tool_calls = [co.ToolCall(**tc) for tc in m.tool_calls]
        out.append(
            co.Message(
                role=m.role,
                content=m.content if m.content is not None else "",
                tool_calls=tool_calls,
                tool_call_id=m.tool_call_id,
                name=m.name,
            )
        )
    return out


_LOGGABLE_TOOL_NAMES = ("Bash", "Write", "Edit", "Delete", "call_subagent")


def _log_tool_call(name: str, arguments: dict[str, Any]) -> None:
    _validate_tool_call_args(name, arguments)
    nested = current_subagent_depth() > 0
    if name in _LOGGABLE_TOOL_NAMES:
        with _tool_trace_lock:
            _tool_trace.append({"name": name, "arguments": dict(arguments or {})})
    if name == "call_subagent":
        msg = arguments.get("message") or arguments.get("task") or ""
        preview = msg if len(msg) < 160 else msg[:157] + "..."
        _hook_console.print(f"subagent: {preview}", style="dim", markup=False)
    elif name in ("Bash", "Write", "Edit", "Delete"):
        preview = _short_arg_summary(name, arguments)
        if nested:
            _hook_console.print(f"  subagent:{name} {preview}", style="dim", markup=False)
        else:
            _hook_console.print(f"  outo:{name} {preview}", style="dim", markup=False)


def _validate_tool_call_args(name: str, arguments: Any) -> None:
    """Reject malformed tool calls early with a clear, attributable error.

    coreouto 0.3.2's agent loop calls `tool.handler(**tool_call.arguments)`
    without first checking that `arguments` is a dict. When the LLM produces
    `{"name": "Write", "arguments": null}` (or any non-dict) — for example
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
        return cmd if len(cmd) < 160 else cmd[:157] + "..."
    if name == "Write":
        path = args.get("file_path", "?")
        size = len(args.get("content") or "")
        return f"{path} ({size} bytes)"
    if name == "Edit":
        path = args.get("file_path", "?")
        edits = args.get("edits") or []
        return f"{path} ({len(edits)} edit{'s' if len(edits) != 1 else ''})"
    if name == "Delete":
        return args.get("file_path", "?")
    return str(args)[:120]
