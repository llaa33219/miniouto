"""Chat runner: prepare runtime, run a single prompt, persist history."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import coreouto as co
from rich.console import Console

from ..storage import sessions as session_store
from ..storage.sessions import MessageRecord
from .runtime import ChatOverrides, build_runtime, resolve_runtime_from_settings

_hook_console = Console(stderr=True, soft_wrap=False, highlight=False)


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

    response = agent.call_sync(opts.prompt, history=core_msgs)
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
                content=m.content,
                tool_calls=tool_calls,
                tool_call_id=m.tool_call_id,
                name=m.name,
            )
        )
    return out


def _log_tool_call(name: str, arguments: dict[str, Any]) -> None:
    if name == "call_subagent":
        msg = arguments.get("message") or arguments.get("task") or ""
        preview = msg if len(msg) < 160 else msg[:157] + "..."
        _hook_console.print(f"subagent: {preview}", style="dim", markup=False)
    elif name in ("Bash", "Write", "Edit", "Delete"):
        preview = _short_arg_summary(name, arguments)
        _hook_console.print(f"  subagent:{name} {preview}", style="dim", markup=False)


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
