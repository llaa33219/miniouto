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

# The structured stdout lane for live thinking deltas: one prefixed
# physical line per line of the fragment, so a wrapper reading the pipe
# (the TUI) can tell reasoning fragments from raw answer tokens — a bare
# continuation line would be indistinguishable from answer text. `outo`
# is fixed: one actor per process, so every delta this sink sees is outo's.
THINKING_DELTA_PREFIX = "outo:thinking: "

# Loop-event kinds that close a streamed message's boundary — the same set
# the TUI treats as one (`cli/tui/app.py:_STREAM_BOUNDARY_KINDS`).
# `ON_ITERATION` flushes `thinking` → `response` → `context` for a message
# and a `tool` event follows; past any of these the next text delta belongs
# to a NEW message, so the streamed-duplicate guard closes and the spinner
# may legitimately return.
_STREAM_BOUNDARY_KINDS = frozenset({"thinking", "response", "tool", "context"})


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
        self._stream_text: str | None = None
        self._answer_stream: str | None = None
        self._stream_line_open = False
        # True while THIS message's deltas stopped a live spinner — the
        # boundary event that closes the message re-ignites it, because
        # the next LLM call / tool execution is spinner territory again.
        self._stream_stopped_spinner = False
        # The thinking fragments already written to the structured
        # `outo:thinking:` lane for THIS message. The completed `thinking`
        # event whose text this equals is not printed again (the stream was
        # the rendering); a thinking that never streamed still renders from
        # its completed event, exactly as before. Reset at every message
        # boundary, like `_stream_text`.
        self._stream_thinking: str | None = None

    def begin_working(self) -> None:
        # While a message is mid-stream the TOKENS are the liveness signal:
        # a spinner over freshly written text would overwrite the answer
        # the user is reading (and a `Live` region re-entering on the same
        # rows corrupts them). The spinner returns at the next message
        # boundary — `emit_loop_event` closes the stream there. A thinking
        # delta is liveness exactly like a text one, so the guard covers
        # `_stream_thinking` too.
        if (
            self._stream_text is not None
            or self._stream_thinking is not None
            or self._stream_line_open
        ):
            return
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
        # The streamed-duplicate guard, keyed on the ITERATION flush order
        # coreouto guarantees (`thinking` → `response` → `context`): the
        # whole-text `response` event of the message that just streamed is
        # suppressed when its text matches what the tokens already wrote,
        # and the trailing `context` event — one per message, always last —
        # closes the boundary: the message is complete, so the streamed
        # text moves to `_answer_stream` (what the run's answer must not
        # repeat) and the spinner may return.
        # Those exact tokens already went out one by one: the stream was the
        # rendering, so the line is NOT printed again — but the event is
        # still a message boundary, and the bookkeeping below (closing the
        # stream state, re-igniting the spinner) must run. A `thinking`
        # event is suppressed the same way when its fragments already went
        # out on the prefixed `outo:thinking:` lane; a thinking that never
        # streamed still renders from its completed event, exactly as
        # before.
        suppressed = (
            event.kind == "response"
            and self._stream_text is not None
            and event.text == self._stream_text
        ) or (
            event.kind == "thinking"
            and self._stream_thinking is not None
            and event.text == self._stream_thinking
        )
        if event.kind == "context":
            # One `context` per message, always the LAST event of the flush:
            # the message is complete. Its streamed text becomes the text
            # the run's answer must not repeat (None when it did not
            # stream — which also retires the PREVIOUS message's value).
            self._answer_stream = self._stream_text
            self._stream_text = None
            self._stream_thinking = None
        elif event.kind == "tool":
            # Defensive close: `context` normally precedes a tool event, but
            # no later event may inherit a stale stream.
            self._stream_text = None
            self._stream_thinking = None
        elif event.kind == "thinking":
            # The completed thinking closes the thinking stream of THIS
            # message (suppressed or not): the next thinking fragment
            # belongs to the next message.
            self._stream_thinking = None
        # A raw delta write can leave stdout mid-line; a console.print from
        # here on would glue onto the streamed answer text.
        if self._stream_line_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._stream_line_open = False
        # The message is complete — if its deltas stopped the spinner, the
        # next LLM call / tool execution is spinner territory again. The
        # flag survives boundary events that arrive while the guard is
        # still open (a suppressed `response` before its `context`), so it
        # is spent exactly when the spinner can actually return.
        if (
            event.kind in _STREAM_BOUNDARY_KINDS
            and self._stream_stopped_spinner
            and self._stream_text is None
            and self._stream_thinking is None
            and not self._stream_line_open
        ):
            self._stream_stopped_spinner = False
            self.begin_working()
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
        if not suppressed:
            self._console.print(line, highlight=False)

    def emit_final_answer(self, content: str, session_name: str) -> None:
        if not self._emit_final:
            return
        with self._stdout_lock:
            if self._answer_stream is not None and content == self._answer_stream:
                # The answer already went out token by token: only the run's
                # finish marker is owed — after one newline when the last
                # delta left stdout mid-line (contract: the marker never
                # shares a line with streamed answer text).
                if self._stream_line_open:
                    sys.stdout.write("\n")
                if not self._quiet:
                    sys.stdout.write("------finish------\n")
                sys.stdout.flush()
            else:
                if not self._quiet:
                    sys.stdout.write("------finish------\n")
                sys.stdout.write(content if content else "")
                if not content or not content.endswith("\n"):
                    sys.stdout.write("\n")
                sys.stdout.flush()
            self.streamed_answer_consumed()

    def emit_stream_delta(self, kind: str, text: str) -> None:
        """Write one live fragment to stdout — display-only (invariant 8).

        The CLI is pipe-friendly on purpose: a wrapper process reads the
        token stream off stdout as it arrives, tty or not. `text` deltas
        are written raw (no `actor:` prefix — the stream IS the message).
        `thinking` deltas go out on the STRUCTURED lane: every physical
        line carries the `outo:thinking:` prefix, because a bare reasoning
        fragment would be indistinguishable from raw answer tokens on the
        same pipe (the TUI mounts one live row per lane). A fragment with
        embedded newlines becomes one prefixed physical line per line, so
        the prefix can never sit mid-line; the reader rejoins them by
        stripping one prefix per line and re-adding the newline each
        non-final line carried. `quiet` outputs nothing and touches no
        state: `--answer-only` / `--with-session` promise byte-identical
        output.
        """

        if self._quiet or not text:
            return
        with self._stdout_lock:
            if self._status is not None:
                self.end_working()
                self._stream_stopped_spinner = True
            if kind == STREAM_TEXT:
                self._stream_text = (self._stream_text or "") + text
                self._stream_line_open = not text.endswith("\n")
                sys.stdout.write(text)
                sys.stdout.flush()
                return
            if kind != STREAM_THINKING:
                return
            # The raw text lane can still be mid-line from an earlier
            # fragment; a prefixed line must start at column 0 or the
            # wrapper cannot recognize it.
            if self._stream_line_open:
                sys.stdout.write("\n")
                self._stream_line_open = False
            self._stream_thinking = (self._stream_thinking or "") + text
            # One physical line per line of the fragment (a trailing
            # newline in the fragment ends its last line; no phantom empty
            # line follows it).
            body = text[:-1] if text.endswith("\n") else text
            for line in body.split("\n"):
                sys.stdout.write(THINKING_DELTA_PREFIX + line + "\n")
            sys.stdout.flush()

    def streamed_answer(self) -> str | None:
        """The text this sink already streamed for the LAST message, if any.

        The chain layer (`cli/chat.py`) calls this before printing the
        run's answer: content equal to it was already delivered token by
        token, so only the finish marker is owed (the stream left stdout
        mid-line, and the marker closes the line — contract point 4).
        `None` means the answer did not stream (e.g. a synthesized
        hand-off message), so the whole text is still owed.
        """

        return self._answer_stream

    def begin_newline_if_stream_open(self) -> None:
        """Close a raw stream line before a marker is written after it.

        The chain layer calls this right before printing `------finish------`
        (or a superseded intermediate answer): streamed deltas go out
        unadorned, so the stream can leave stdout mid-line, and a marker
        must never share a line with answer text.
        """

        if self._stream_line_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._stream_line_open = False

    def streamed_answer_consumed(self) -> None:
        """Reset the streamed-answer state: the answer is fully delivered.

        The finish marker (or a full re-print) settles the debt; anything
        the next message streams accumulates from scratch.
        """

        self._answer_stream = None
        self._stream_text = None
        self._stream_thinking = None
        self._stream_line_open = False

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
