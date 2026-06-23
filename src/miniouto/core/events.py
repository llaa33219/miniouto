"""Event sink abstraction for chat output.

The agent loop produces two distinct kinds of output that the user
experiences very differently:

1. **Internal loop events** — tool calls and intermediate model text
   (e.g. `outo:Write …`, `subagent:Bash …`). The user wants these
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
from typing import Protocol

from rich.console import Console
from rich.status import Status
from rich.text import Text


@dataclass
class LoopEvent:
    actor: str
    kind: str
    text: str
    tool_name: str | None = None


class EventSink(Protocol):
    def begin_working(self) -> None: ...
    def update_activity(self, text: str) -> None: ...
    def end_working(self) -> None: ...
    def emit_loop_event(self, event: LoopEvent) -> None: ...
    def emit_final_answer(self, content: str, session_name: str) -> None: ...


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


class ConsoleEventSink:
    SPINNER_TEXT_DEFAULT = "Working…"

    def __init__(self) -> None:
        # Spinner and loop events share stdout so Rich's `Live` keeps them
        # vertically separated. Earlier stderr/stdout splits interleaved
        # on a shared tty, putting the spinner glyph on the same row as
        # the next `outo:` line.
        self._console = Console(soft_wrap=False, highlight=False)
        self._activity: str = self.SPINNER_TEXT_DEFAULT
        self._status: Status | None = None
        self._stdout_lock = Lock()

    def begin_working(self) -> None:
        if self._status is not None:
            return
        self._status = self._console.status(
            f" {self._activity}",
            spinner="dots",
            spinner_style="bold cyan",
        )
        self._status.__enter__()

    def update_activity(self, text: str) -> None:
        self._activity = text or self.SPINNER_TEXT_DEFAULT
        if self._status is not None:
            self._status.update(f" {self._activity}")

    def end_working(self) -> None:
        if self._status is None:
            return
        self._status.__exit__(None, None, None)
        self._status = None

    def emit_loop_event(self, event: LoopEvent) -> None:
        # Text.assemble avoids markup parsing entirely — the model/tool
        # text is stored as raw characters and styled by span, so stray
        # `[brackets]` in the payload can't crash Rich.
        line = Text.assemble(
            (f"{event.actor}:", "orange3"),
            (f" {event.text}", "white"),
        )
        self._console.print(line, highlight=False)

    def emit_final_answer(self, content: str, session_name: str) -> None:
        # Stdout (not stderr) so callers can pipe the answer. The marker
        # format is fixed-width so downstream scripts can grep reliably
        # even if the answer itself contains Markdown.
        with self._stdout_lock:
            sys.stdout.write("------finish------\n")
            sys.stdout.write(content if content else "")
            if not content or not content.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.write(f"------{session_name}------\n")
            sys.stdout.flush()
