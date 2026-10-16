"""Event sink abstraction for chat output.

The agent loop produces two distinct kinds of output that the user
experiences very differently:

1. **Internal loop events** — tool calls and intermediate model text
   (e.g. `outo:Bash …`, `subagent:Image …`). The user wants these
   streamed live so they know what the agent is doing.

2. **The final answer** — the model's terminal response (no follow-up
   tool call). This is the only thing the user actually wants as the
   answer to their prompt.

Both flow through an `EventSink`. `ConsoleEventSink` is the CLI
implementation; the TUI defines its own in `cli/tui.py`; `NullSink`
is the default for programmatic callers.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from threading import Lock
from typing import Any, Literal, Protocol

from rich.console import Console
from rich.status import Status
from rich.text import Text


@dataclass
class LoopEvent:
    actor: str
    kind: str
    text: str
    tool_name: str | None = None
    subagent_id: str | None = None
    detail: str | None = None
    subagent_name: str | None = None
    msg_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"actor": self.actor, "kind": self.kind, "text": self.text}
        if self.tool_name:
            d["tool_name"] = self.tool_name
        if self.subagent_id:
            d["subagent_id"] = self.subagent_id
        if self.detail:
            d["detail"] = self.detail
        if self.subagent_name:
            d["subagent_name"] = self.subagent_name
        if self.msg_ref:
            d["msg_ref"] = self.msg_ref
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LoopEvent:
        return cls(
            actor=str(d.get("actor") or "outo"),
            kind=str(d.get("kind") or "tool"),
            text=str(d.get("text") or ""),
            tool_name=d.get("tool_name") or None,
            subagent_id=d.get("subagent_id") or None,
            detail=d.get("detail") or None,
            subagent_name=d.get("subagent_name") or None,
            msg_ref=d.get("msg_ref") or None,
        )


class EventSink(Protocol):
    def begin_working(self) -> None: ...
    def update_activity(self, text: str) -> None: ...
    def end_working(self) -> None: ...
    def emit_loop_event(self, event: LoopEvent) -> None: ...
    def emit_final_answer(self, content: str, session_name: str) -> None: ...
    def emit_stream_delta(self, kind: str, text: str) -> None: ...
    def remove_events(self, msg_refs: list[str]) -> None: ...


# The two `emit_stream_delta` kinds. Deltas are transient display-only
# fragments of a message the model is still generating (coreouto fires
# ON_STREAM_TEXT / ON_STREAM_THINKING once per incremental fragment).
StreamKind = Literal["text", "thinking"]
STREAM_TEXT: StreamKind = "text"
STREAM_THINKING: StreamKind = "thinking"


class NullSink:
    def begin_working(self) -> None:
        pass

    def update_activity(self, text: str) -> None:
        pass

    def end_working(self) -> None:
        pass

    def emit_loop_event(self, event: LoopEvent) -> None:
        pass

    def emit_final_answer(self, content: str, session_name: str) -> None:
        pass

    def emit_stream_delta(self, kind: str, text: str) -> None:
        pass

    def remove_events(self, msg_refs: list[str]) -> None:
        pass


class ConsoleEventSink:
    SPINNER_TEXT_DEFAULT = "Working…"

    def __init__(self, *, quiet: bool = False, emit_final: bool = True) -> None:
        # Spinner and loop events share stdout so Rich's `Live` keeps them
        # vertically separated. Earlier stderr/stdout splits interleaved
        # on a shared tty, putting the spinner glyph on the same row as
        # the next `outo:` line.
        #
        # The session marker stays the CLI's job (see `cli/chat.py`), not
        # the sink's — `quiet` only governs progress noise + the finish
        # marker, never the session marker.
        #
        # `emit_final=False` hands the answer block back to the caller: only
        # `cli/chat.py` knows whether this turn was the RUN's last one, and
        # `------finish------` marks the run, never a single turn.
        # Default True keeps a direct caller byte-compatible.
        self._console = Console(soft_wrap=False, highlight=False)
        self._activity: str = self.SPINNER_TEXT_DEFAULT
        self._status: Status | None = None
        self._stdout_lock = Lock()
        self._quiet = quiet
        self._emit_final = emit_final

    def begin_working(self) -> None:
        if self._quiet or self._status is not None:
            return
        self._status = self._console.status(
            f" {self._activity}",
            spinner="dots",
            spinner_style="bold cyan",
        )
        self._status.__enter__()

    def update_activity(self, text: str) -> None:
        if self._quiet:
            return
        self._activity = text or self.SPINNER_TEXT_DEFAULT
        if self._status is not None:
            self._status.update(f" {self._activity}")

    def end_working(self) -> None:
        if self._quiet or self._status is None:
            return
        self._status.__exit__(None, None, None)
        self._status = None

    def emit_loop_event(self, event: LoopEvent) -> None:
        if self._quiet:
            return
        # tool_result events exist so the TUI can show tool return values;
        # the CLI deliberately ignores them to keep output byte-identical
        # to the pre-tool_result behavior.
        if event.kind == "tool_result":
            return
        # Text.assemble avoids markup parsing entirely — the model/tool
        # text is stored as raw characters and styled by span, so stray
        # `[brackets]` in the payload can't crash Rich.
        if event.kind == "thinking":
            line = Text.assemble(
                (f"{event.actor}:thinking:", "orange3"),
                (f" {event.text}", "dim white"),
            )
        elif event.kind in ("subagent_start", "subagent_end"):
            # Start/end carry the full task/result for the session record
            # and the TUI detail screen; the CLI shows a one-line preview.
            preview = " ".join(event.text.split())
            if len(preview) > 120:
                preview = preview[:120] + "…"
            style = "dim white" if event.kind == "subagent_end" else "white"
            line = Text.assemble(
                (f"{event.actor}:", "orange3"),
                (f" {preview}", style),
            )
        else:
            line = Text.assemble(
                (f"{event.actor}:", "orange3"),
                (f" {event.text}", "white"),
            )
        self._console.print(line, highlight=False)

    def emit_final_answer(self, content: str, session_name: str) -> None:
        if not self._emit_final:
            return
        with self._stdout_lock:
            if not self._quiet:
                sys.stdout.write("------finish------\n")
            sys.stdout.write(content if content else "")
            if not content or not content.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()

    def emit_stream_delta(self, kind: str, text: str) -> None:
        """No-op: live streaming is a TUI feature only.

        The CLI prints intermediate model text from the discrete
        `response` loop event and the final answer from
        `emit_final_answer`; rendering the same tokens twice (once
        incrementally, once whole) would corrupt that output.
        """

    def remove_events(self, msg_refs: list[str]) -> None:
        """Note the removal — the console cannot unprint what it showed.

        A repair can excise a tool call the user already watched scroll
        past, and stdout is append-only, so the honest v1 behavior is a
        dim note saying how many messages left the record. `quiet`
        suppresses it: `--answer-only` / `--with-session` promise
        byte-identical output.
        """

        if self._quiet or not msg_refs:
            return
        self._console.print(
            Text(
                f"[removed {len(msg_refs)} corrupted messages from the record]",
                style="dim",
            ),
            highlight=False,
        )
