"""The PyRatatui TUI: ChatTUI, the event loop, the sink and the summary.

This module is the integration layer — it owns the state machine (session
pinning, dispatch, streaming rows, windowing, timers, handoff, inbox
delivery), the screen stack (base screen + modals), key and mouse routing, and
the composition of a frame out of the rows/widgets/modals layers' styled lines.
Nothing below it knows a frame exists; nothing here knows how a row paints.

Two runtimes over one state machine:

* :func:`run_tui` wires the REAL terminal — ``Terminal`` from pyratatui, an
  :class:`~.input.InputReader` thread feeding an asyncio queue, mouse/paste
  modes, OSC 52 clipboard, the window title, and a ~60 Hz draw loop.
  ``poll_event`` is never called: crossterm would take stdin over and swallow
  mouse, paste and resize (see ``input.py``).
* ``ChatTUI(terminal=None)`` is HEADLESS: the same handlers, the same frame
  composition, the same timers — driven by :class:`~.nodes.Pilot` through
  ``run_test``. Every behavioural test in the suite runs this path, which is
  the only way the real one can be trusted.

The ported-behaviour contract is deliberate: the state attribute names, the
method names, the sink protocol (AGENTS.md invariant 8), the ``to_thread``
dispatch (10), the inbox de-duplication (16/20) and the handoff order (21) are
all the old module's, because the tests that pin them are the specification.

Seams: everything the old suites monkeypatch on ``miniouto.cli.tui`` —
``run_chat``, ``summary``, ``_get_reasoning_api``, ``graphics_mode`` — is
resolved through :func:`~.nodes.package_seam` at CALL time. A plain re-export
would bind a copy and the patch would silently miss the caller.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import os
import select
import signal
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

try:  # POSIX only: the boot probe's terminal-mode restore needs it.
    import termios
except ImportError:  # pragma: no cover - Windows
    termios = None  # type: ignore[assignment]

from rich.cells import cell_len
from rich.text import Text

from ... import __version__, paths_runtime
from ...core import inbox, runtime, summary, update_check
from ...core import modelslist as catalog_api
from ...core.chat import ChatOptions, run_chat
from ...core.events import STREAM_THINKING, LoopEvent
from ...core.providers import SUPPORTED_FORMATS, add_provider_from_catalog, resolve_catalog_dialect
from ...core.runtime import LoopCancelledError
from ...storage import paths, session_config
from ...storage import providers as provider_store
from ...storage import sessions as session_store
from ...storage import settings as settings_store
from ...storage import styles as style_store
from ...storage.providers import SOURCE_CUSTOM
from ..chat import handoff_marker
from . import config as config_module
from . import graphics as graphics_module
from . import nodes as nodes_module
from . import pt
from .config import (
    OUTO_TARGET,
    SUBAGENT_TARGET,
    TrioTarget,
    _effective_config,
    _get_reasoning_api,
    _model_defaults,
    _parse_optional_int,
    _persist_trio,
    _reasoning_chip_value,
    _record,
    _trio,
    _trio_display,
    _with_reasoning_effort,
)
from .input import (
    Key,
    Mouse,
    Paste,
    Resize,
    key_from_xnano,
    mouse_from_xnano,
    scroll_from_xnano,
    terminal_size,
)
from .modals import (
    CommandPalette,
    Modal,
    ProviderSettingsPopup,
    SelectionModal,
    TextInputModal,
)
from .nodes import (
    Node,
    Offset,
    Rect,
    Screen,
    Selection,
    Size,
    package_seam,
    pt_style_text,
)
from .rows import (
    AnswerRow,
    EventRow,
    RowStatic,
    StreamRow,
    ThinkingRow,
    ToolRow,
    cells_of,
    pad_to,
    truncate_to,
)
from .theme import (
    BUILTIN_THEMES,
    DEFAULT_THEME_NAME,
    MINIOUTO_THEME,
    THEME_ORDER,
    TuiTheme,
)
from .theme import get_theme as _theme_by_name
from .widgets import (
    HELP_TEXT,
    BottomPanel,
    ChatInput,
    ChatScroll,
    Header,
    JumpButton,
    _awaitable_none,
    _RowWindow,
    auto_text,
)

__all__ = [
    "CHAT_MAX_WIDTH",
    "HELP_TEXT",
    "THEME_NAME",
    "ChatTUI",
    "TUIEventSink",
    "Timer",
    "Workers",
    "run_tui",
    "tui_summary",
]

THEME_NAME = DEFAULT_THEME_NAME
"""Name of the theme the TUI boots on (``settings.theme`` may name another one)."""

CHAT_MAX_WIDTH = 150
"""Chat column width cap (cells).

Past this the log, the input and the chips row stop growing and stay centered —
a 300-column tool output otherwise stretches a sentence across half the screen.
"""

# Braille spinner frames. The status line reads e.g. "⠧ Bash…" and the glyph
# rotates through this set at ~12.5 fps so the bottom of the screen shows the
# agent is alive even between tool calls.
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_INTERVAL = 0.08
_SPINNER_DEFAULT_TEXT = "Working…"

_INBOX_POLL_INTERVAL = 1.0
"""How often the idle TUI drains ``core.inbox``.

No registry callback pushes messages any more (a detached ``miniouto chat
--bg`` run and ``miniouto message insert`` write into a session's inbox
directory and may finish at any moment), so this poll is what turns a queued
message into a turn.
"""

_INBOX_DETAIL_PREFIX = "inbox:"
"""``LoopEvent.detail`` prefix the core stamps on a drained inbox message."""

_STREAM_FLUSH_INTERVAL = 0.033
"""Live token streaming gate: one UI update per ~33 ms (see ``_StreamCoalescer``)."""

_STREAM_BOUNDARY_KINDS = frozenset({"thinking", "response", "tool", "context"})
"""Loop event kinds that close the current streamed message.

The live row is retired for them, so any buffered tail must flush first.
"""

_ESC_DOUBLE_PRESS_WINDOW = 2.0
"""Two escape presses within this window (seconds) while busy force-stop the loop."""

_DEFAULT_SESSION = "default"
"""Config-only fallback before a TUI has chosen a session.

Deliberately NOT an inbox destination: every unassigned TUI needs its own
pending queue.
"""

_CLIPBOARD_OSC52 = "\x1b]52;c;{payload}\x07"
_TITLE_OSC = "\x1b]0;{title}\x07"
_LEAVE_ALT_SCREEN = "\x1b[?1049l"
_CURSOR_HOME = "\x1b[{row};{col}H"

WHEEL_LINES = 3
"""Rows one wheel detent scrolls the log."""

_REDRAW_SECONDS = 1 / 60
"""Target frame interval of the real loop."""


# ─── logo ────────────────────────────────────────────────────────────────────
#
# The start-up ornament: a 180x36 packed bitmap rendered as braille. Each
# braille cell carries 2x4 pixels, so the art is 90 cells wide at full size and
# is scaled down to whatever the chat column can hold. Kept here (not in
# ``widgets``) because it is the app's boot ornament, exactly as in the old
# module.

_LOGO_PIX_W = 180
_LOGO_PIX_H = 36
_LOGO_BITS = bytes.fromhex(
    "0000000000000000000000c00000000000000000000000"
    "000000000000000000001ffe0000000000000003ff0000"
    "000000000000000000007fff800000000000000fffe000"
    "000000000f0000007800ffffe00000000000003ffff000"
    "000000000f0000007803fffff0000000000000fffffc00"
    "000000000f0000007807f803f8000000000001ff01fe00"
    "000000000f000000780fe000fc000000000003f8007f00"
    "0000000000000000001f80007e000000000003f0001f80"
    "0000000000000000001f00e03f000000000007c0780fc0"
    "0000000000000000003e01e01f00000000000f80f807c0"
    "0000000000000000007c03f00f80000000000f80fc03e0"
    "000000000f000000787c07f80780000001e01f01fe03e0"
    "000000000f00000078787fff87c0000001e01e07ff81f0"
    "000000000f00000078f8ffffc3c0000001e03e3ffff1f0"
    "000000000f00000078f8ffffc3c0000001e03e7ffff0f0"
    "fffffff80f1ffff878f07fffc3c3800e1ffc3c7ffff8f0"
    "fffffff80f1ffff878f07fff83c3800e1ffc3c7ffff0f0"
    "fffffff80f1ffff878f03fff83e3800e1ffc3c3ffff0f0"
    "f001c0070f1e007878f07fff83e3800e01e03c1fffe0f0"
    "f001c0070f1e007878f07fff83c3800e01e03c1fffc0f0"
    "f001c0070f1e007878f07fffc3c3800e01e03c0fffc0f0"
    "f001c0070f1e007878f8ffffc3c3800e01e03c0fffc0f0"
    "f001c0070f1e007878787fffc7c3800e01e03e0fffc0f0"
    "f001c0070f1e007878787fff87c3800e01e01e0fffc1f0"
    "f001c0070f1e0078787c07f80f83800e01e01f0fffc1e0"
    "f001c0070f1e0078783e03f00f83800e01e01f0fcf83e0"
    "f001c0070f1e0078783e01e01f03800e01e00f820107c0"
    "f001c0070f1e0078781f00c03f03800e01e007c0000fc0"
    "f001c0070f1e0078780fc0007e03800e01e007e0001f80"
    "f001c0070f1e0078780fe001fc03800e01e003f8003f00"
    "f001c0070f1e00787807fc07f803800e01e001fe00fe00"
    "f001c0070f1e00787801fffff003fffe01fc00fffffc00"
    "f001c0070f1e00787800ffffc003fffe01fc003ffff800"
    "f001c0070f1e007878003fff8003fffe01fc001fffe000"
    "f001c0070f1e007878000ffc0003fffe01fc0007ff8000"
    "0000000000000000000000000000000000000000000000"
)


def _unpack_logo_pixels() -> list[list[int]]:
    row_bytes = (_LOGO_PIX_W + 7) // 8
    return [
        [(_LOGO_BITS[y * row_bytes + x // 8] >> (7 - x % 8)) & 1 for x in range(_LOGO_PIX_W)]
        for y in range(_LOGO_PIX_H)
    ]


def _pixels_to_braille(grid: list[list[int]], w: int, h: int) -> str:
    rows = []
    for gy in range(0, h, 4):
        line = []
        for gx in range(0, w, 2):
            value = 0
            if grid[gy][gx]:
                value |= 0x01
            if gy + 1 < h and grid[gy + 1][gx]:
                value |= 0x02
            if gy + 2 < h and grid[gy + 2][gx]:
                value |= 0x04
            if grid[gy][gx + 1]:
                value |= 0x08
            if gy + 1 < h and grid[gy + 1][gx + 1]:
                value |= 0x10
            if gy + 2 < h and grid[gy + 2][gx + 1]:
                value |= 0x20
            if gy + 3 < h and grid[gy + 3][gx]:
                value |= 0x40
            if gy + 3 < h and grid[gy + 3][gx + 1]:
                value |= 0x80
            line.append(chr(0x2800 + value))
        rows.append("".join(line).rstrip("\u2800"))
    while rows and not rows[0]:
        rows.pop(0)
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows)


def _render_logo(max_chars: int) -> str:
    """The start-up logo as braille, scaled into ``max_chars`` cells."""

    if max_chars < 10:
        return "miniouto"
    src = _unpack_logo_pixels()
    target_w = min(max_chars * 2, _LOGO_PIX_W)
    target_w -= target_w % 2
    if target_w >= _LOGO_PIX_W:
        return _pixels_to_braille(src, _LOGO_PIX_W, _LOGO_PIX_H)
    scale = target_w / _LOGO_PIX_W
    target_h = int(_LOGO_PIX_H * scale)
    target_h -= target_h % 4
    if target_h < 4:
        target_h = 4
    scaled = [
        [
            src[min(int(py / scale), _LOGO_PIX_H - 1)][min(int(px / scale), _LOGO_PIX_W - 1)]
            for px in range(target_w)
        ]
        for py in range(target_h)
    ]
    return _pixels_to_braille(scaled, target_w, target_h)


def logo_columns(log_width: int, app_width: int) -> int:
    """Cells the start-up logo may occupy: the chat column, never the terminal.

    ``log_width`` is 0 on the very first mount, before the log has been laid
    out, so the app width stands in until it is known — either way the result is
    capped, because the logo is drawn INSIDE the capped column.
    """

    width = log_width or app_width
    return min(width, CHAT_MAX_WIDTH) if width > 0 else CHAT_MAX_WIDTH


# ─── timers / workers ────────────────────────────────────────────────────────


class Timer:
    """A repeating timer on the app's clock (Textual's ``Timer``, minimal).

    Textual scheduled on the event loop's clock; the port keeps ONE clock the
    app advances (``ChatTUI.tick``), so a headless test moves time forward
    deterministically instead of sleeping. ``pause``/``resume``/``stop`` are the
    three operations the ported tests use.
    """

    def __init__(self, app: ChatTUI, interval: float, callback: Callable[[], Any]) -> None:
        self.app = app
        self.interval = max(1e-6, float(interval))
        self.callback = callback
        self.due = app.now + self.interval
        self.paused = False
        self.stopped = False

    @property
    def active(self) -> bool:
        return not (self.paused or self.stopped)

    def tick(self, now: float) -> None:
        """Fire as many times as the clock has passed (never an endless burst)."""

        if not self.active:
            return
        fired = 0
        while now >= self.due and fired < 64:
            self.due += self.interval
            fired += 1
            if self.stopped:
                return
            # A timer callback dirties the frame only where it MUTATES. The
            # inbox poll fires every second and usually delivers nothing;
            # marking unconditionally there made every idle second compose a
            # frame that was already correct, which defeated the dirty flag.
            result = self.callback()
            if asyncio.iscoroutine(result):
                _spawn(result)
        if fired >= 64:  # a long stall must not replay thousands of frames
            self.due = now + self.interval

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False
        self.due = self.app.now + self.interval

    def stop(self) -> None:
        self.stopped = True
        if self in self.app._timers:
            self.app._timers.remove(self)


_TASKS: set[Any] = set()
"""Strong references to fire-and-forget tasks (asyncio only keeps weak ones)."""


def _spawn(coro: Any) -> Any:
    task = asyncio.ensure_future(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


class Workers:
    """The app's background tasks — ``app.workers.wait_for_complete()``.

    Textual's ``WorkerManager``, cut to the two things the suite does: count what
    is running and wait for it. A finished task removes itself, so
    ``len(app.workers) == 0`` after a wait is the assertion it looks like.
    """

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[Any]] = []

    def add(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        self._tasks.append(task)
        task.add_done_callback(self._discard)
        return task

    def _discard(self, task: asyncio.Task[Any]) -> None:
        if task in self._tasks:
            self._tasks.remove(task)

    def __len__(self) -> int:
        return len(self._tasks)

    def __iter__(self) -> Any:
        return iter(list(self._tasks))

    async def wait_for_complete(self) -> None:
        """Block until every task has finished (exceptions are swallowed)."""

        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def cancel_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()


# ─── the streaming sink ──────────────────────────────────────────────────────


class _StreamCoalescer:
    """Fold per-token deltas into one UI update per flush window.

    coreouto fires one ``ON_STREAM_*`` hook per token, often 100+/s, and every
    sink call hops to the UI thread — hopping and repainting per token would
    starve the loop. The sink appends into this buffer (only the single worker
    thread calls it, so no lock is needed) and drains it on a time gate; the
    turn's last window is drained explicitly when working ends, so no tail is
    ever lost.
    """

    def __init__(
        self,
        flush_interval: float = _STREAM_FLUSH_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._flush_interval = flush_interval
        self._clock = clock
        self._text: list[str] = []
        self._thinking: list[str] = []
        self._last_flush = 0.0

    def push(self, kind: str, text: str) -> bool:
        """Buffer one delta; return True when the caller should flush now."""

        target = self._thinking if kind == STREAM_THINKING else self._text
        target.append(text)
        now = self._clock()
        if now - self._last_flush < self._flush_interval:
            return False
        self._last_flush = now
        return True

    def drain(self) -> tuple[str, str]:
        text, thinking = "".join(self._text), "".join(self._thinking)
        self._text.clear()
        self._thinking.clear()
        return text, thinking


class TUIEventSink:
    """Sink that posts chat events to a running ``ChatTUI`` (invariant 8, closed).

    The agent loop runs on a worker thread (via ``asyncio.to_thread``), so every
    callback hops to the main thread with ``call_from_thread`` before touching
    state. The sink owns NO animation state: the spinner ticker lives on the app,
    because a sink is created per dispatch and dies at ``end_working``.
    """

    def __init__(self, app: ChatTUI) -> None:
        self._app = app
        self._stream = _StreamCoalescer()

    def begin_working(self) -> None:
        self._app.call_from_thread(self._app._set_chat_working, True)

    def update_activity(self, text: str) -> None:
        # Plain attribute on the app: the ticker repaints it on the next frame,
        # so there is nothing to hop to the UI thread for.
        self._app._set_activity(text)

    def end_working(self) -> None:
        self._app.call_from_thread(self._end_working_on_ui)

    def _end_working_on_ui(self) -> None:
        self._app._set_chat_working(False)
        # Flush the last (< interval) window on the UI thread we are already on
        # — call_from_thread would raise from this thread.
        self._drain_stream()

    def emit_stream_delta(self, kind: str, text: str) -> None:
        if not text:
            return
        if self._stream.push(kind, text):
            self._flush_stream()

    def _flush_stream(self) -> None:
        text, thinking = self._stream.drain()
        if not text and not thinking:
            return
        self._app.call_from_thread(self._post_stream, text, thinking)

    def _drain_stream(self) -> None:
        text, thinking = self._stream.drain()
        if text or thinking:
            self._post_stream(text, thinking)

    def _post_stream(self, text: str, thinking: str) -> None:
        if thinking:
            self._app._apply_stream_thinking(thinking)
        if text:
            self._app._apply_stream_text(text)

    def emit_loop_event(self, event: LoopEvent) -> None:
        # A message-boundary event retires the current live stream row. Flush the
        # buffered tail first so it renders on the row it belongs to — otherwise
        # the coalescer's last (< interval) window survives the boundary and lands
        # on the NEXT message's live row.
        if event.kind in _STREAM_BOUNDARY_KINDS:
            self._flush_stream()
        self._app.call_from_thread(self._post_loop_event, event)

    def _post_loop_event(self, event: LoopEvent) -> None:
        self._app._add_event_row(event)

    def emit_final_answer(self, content: str, session_name: str) -> None:
        self._app.call_from_thread(self._post_final_answer, content, session_name)

    def _post_final_answer(self, content: str, session_name: str) -> None:
        self._app._finalize_stream()
        if content:
            self._app._mount_row(AnswerRow(content))
        else:
            self._app._mount_row(RowStatic(Text("(empty response)", style="dim")))
        self._app._mount_row(RowStatic(Text("")))

    def remove_events(self, msg_refs: list[str]) -> None:
        if not msg_refs:
            return
        self._app.call_from_thread(self._app._remove_event_rows, msg_refs)


# ─── the handoff marker (on disk) ────────────────────────────────────────────


def _persist_handoff_marker(session: str, handoff: summary.Handoff) -> None:
    """Close the OLD conversation with the hand-off marker, on disk.

    A trailing turn holding one ``handoff`` event, so reopening the old session
    replays the marker through ``_add_event_row`` instead of showing a transcript
    that just stops mid-work. It is appended as its own turn rather than merged
    into the last one, because replay renders a turn's events BEFORE its answer —
    merged, the marker would sit above the final message.
    """

    event = LoopEvent(actor="outo", kind="handoff", text=handoff_marker(handoff))
    data = session_store.load(session)
    data.turns.append(
        session_store.TurnRecord(
            user="", events=[event.to_dict()], status=session_store.STATUS_DONE
        )
    )
    session_store.save(session, data)


# ─── monkeypatch seams ───────────────────────────────────────────────────────

_RUN_CHAT = run_chat
_CONFIG_REASONING_API = _get_reasoning_api
_GRAPHICS_MODE = graphics_module.graphics_mode


def _reasoning_api() -> Any:
    """``config._get_reasoning_api()``, honouring a patch on EITHER module.

    The old suites patch ``miniouto.cli.tui._get_reasoning_api`` (a re-export of
    config's function) while the trio helpers in ``config`` are the real
    callers. ``package_seam`` resolves the package-level name unless it is still
    the pristine object — which is what makes both patch targets work.
    """

    return package_seam(
        "_get_reasoning_api", _get_reasoning_api, original=_CONFIG_REASONING_API
    )


def _graphics_mode() -> str:
    """``graphics.graphics_mode()``, honouring a patch on the package."""

    return package_seam("graphics_mode", graphics_module.graphics_mode)


def _summary_api() -> Any:
    """``core.summary``, honouring a patch on the package (the hand-off seam)."""

    return package_seam("summary", summary)


def _chat_runner() -> Any:
    """``core.chat.run_chat``, honouring a patch on the package."""

    return package_seam("run_chat", _RUN_CHAT, original=_RUN_CHAT)


# ─── the app ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Command:
    """One palette entry — the shape Textual's ``SystemCommand`` has."""

    title: str
    help: str
    callback: Callable[[], Any] | None = None


class ChatTUI:
    """The chat TUI. Construct headless; :func:`run_tui` wires the terminal."""

    def __init__(self, terminal: Any = None) -> None:
        self._terminal = terminal
        self.size = Size(80, 24)
        self.title = ""
        self.sub_title = f"miniouto v{__version__}"

        # ── the tree ──────────────────────────────────────────────────────
        self._base_screen = Screen(id="base", app=self)
        self._screens: list[Any] = [self._base_screen]
        self._header = Header("", f"miniouto v{__version__}", id="header")
        self._log = ChatScroll(id="chat")
        self._input = ChatInput(id="input")
        self._panel = BottomPanel(id="bottom-panel")
        self._jump = JumpButton(self._log, id="jump-bottom")
        self._jump.display = False
        for widget in (self._input, self._panel, self._jump):
            widget.app = self
        # The chips deliver through their own `post_message`, which needs the
        # app they belong to; the panel is what the app holds, so the wiring is
        # done here rather than in the widget (which must stay constructible
        # without an app, as the widget tests build it bare).
        for chip in self._panel.chips.values():
            chip.app = self
        self._log.on_scroll = self._on_log_scroll
        self._base_screen.mount(self._header, self._log, self._input, self._panel, self._jump)

        # ── chat state (the old names, deliberately) ───────────────────────
        self._busy = False
        self._logo_shown = False
        self._chat_started = False
        self._session_assigned = False
        self._session_name: str | None = None
        self._unassigned_inbox = f"tui-pending-{uuid.uuid4().hex[:12]}"
        self._optimistic_rows: dict[str, Any] = {}
        self._pending_tool_rows: list[ToolRow] = []
        self._window = _RowWindow()
        self._bulk_loading = False
        self._stream_text_row: StreamRow | None = None
        self._stream_thinking_row: ThinkingRow | None = None
        self._cancel_event: threading.Event | None = None
        self._last_escape = 0.0
        self._esc_hint: str | None = None
        self._esc_hint_until = 0.0
        self._spin_timer: Timer | None = None
        self._spin_frame_idx = 0
        self._chat_working = False
        self._activity = _SPINNER_DEFAULT_TEXT
        self._wizard_status: str | None = None
        self._wizard_working = False
        self._update_notice: str | None = None
        self._inbox_timer: Timer | None = None

        # ── runtime plumbing ──────────────────────────────────────────────
        self._timers: list[Timer] = []
        self._messages: list[Any] = []
        self._workers = Workers()
        self._now = 0.0
        self._clipboard = ""
        # The frame the screen is showing is only believed to match the state
        # while nothing has changed since it was painted. Every mutating entry
        # point below (key, mouse, timer, message, hop, tree edit) sets this,
        # and `run_async` then SKIPS `compose()` on a tick that did not.
        self._dirty = True
        self._focused: Node | None = None
        self._row_origins: dict[Any, Rect] = {}
        self._log_rect = Rect()
        self._select_anchor: tuple[Any, Offset] | None = None
        self._select_dragged = False
        self._overlay = graphics_module.GraphicsOverlay()
        self._booted = False
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        """The loop every UI callback must run on (bound at boot, not per call)."""
        self._loop_thread: int = 0
        """``threading.get_ident()`` of the loop thread — the hop's destination."""
        self._alt_screen_entered = False
        """Did ``Terminal.__enter__`` open the alt screen (it then also closes it)?"""

        # ── theme (last: `watch_theme` reads `_logo_shown`) ────────────────
        # The NAME is stored directly rather than through the `theme` setter:
        # the setter persists, and a construction must not write settings.
        self._theme_name = self._boot_theme()
        self._set_focus(self._input)

    # ── theme ─────────────────────────────────────────────────────────────
    @property
    def theme(self) -> str:
        """The active theme's NAME (Textual's ``App.theme``)."""

        return self._theme_name

    @theme.setter
    def theme(self, name: str) -> None:
        """Assigning a theme persists it — Textual's reactive ran ``watch_theme``.

        The old app spelled this ``theme = reactive(...)``, so ``app.theme = X``
        published the choice to ``settings.toml`` and repainted the logo. Keeping
        that in the setter is what makes "pick a theme, restart, still themed"
        true for any assignment — the picker, a test, or a future keybinding —
        rather than only for the one call site that remembered to call the watch.
        """

        self._theme_name = name
        self.watch_theme()

    @property
    def available_themes(self) -> dict[str, TuiTheme]:
        """Every registered theme, by name (Textual's ``available_themes``)."""

        return {THEME_NAME: MINIOUTO_THEME, **BUILTIN_THEMES}

    def get_theme(self, name: str) -> TuiTheme | None:
        """A theme by name, or None when no build registers it."""

        return _theme_by_name(name)

    @property
    def current_theme(self) -> TuiTheme:
        """The resolved palette — the rows' and widgets' only view of the theme."""

        return _theme_by_name(self.theme) or MINIOUTO_THEME

    def _boot_theme(self) -> str:
        """The theme to start on: ``settings.theme``, else ``THEME_NAME``.

        A stored name that is not registered (a typo, or a theme from a build
        that never registered it) falls back instead of raising: booting must
        never fail over a stale key in ``settings.toml``.
        """

        configured = settings_store.load().theme.strip()
        if not configured:
            return THEME_NAME
        return configured if self.get_theme(configured) else THEME_NAME

    def watch_theme(self) -> None:
        """Persist the choice, retheme the screen's erases, repaint the ornament.

        ``_sync_screen_background`` is here rather than in the picker because
        the setter is the ONE door a theme change comes through (the picker, a
        test, a future keybinding), and the terminal's erase colour has to
        follow the theme it paints.
        """

        settings_store.update(theme=self.theme)
        self._sync_screen_background()
        if self._logo_shown:
            self._show_logo()

    def get_css_variables(self) -> dict[str, str]:
        """The resolved palette, keyed the way the old CSS references named it.

        The port has no CSS, but the palette IS a user-visible contract (the
        contrast ladder, the owner-pinned accent, ``$boost``) and the theme tests
        read it through this one door.
        """

        theme = self.current_theme
        return {
            "primary": theme.primary,
            "accent": theme.accent,
            "secondary": theme.secondary,
            "success": theme.success,
            "warning": theme.warning,
            "error": theme.error,
            "background": theme.background,
            "surface": theme.surface,
            "panel": theme.panel,
            "boost": theme.boost,
            "foreground": theme.foreground,
            "text": auto_text(theme, theme.background),
            "text-muted": theme.text_muted,
            "text-disabled": theme.text_disabled,
            "text-answer": theme.text_answer,
            "screen-selection-background": theme.primary,
            "chat-max-width": str(CHAT_MAX_WIDTH),
        }

    def action_change_theme(self) -> None:
        """``05 Theme``: pick from the registered themes."""

        def on_close(result: str | None) -> None:
            if not result or result == self.theme:
                return
            self.theme = result
            self._spinner_status(f"theme → {result}")

        self.push_screen(
            SelectionModal(
                "Select theme", list(THEME_ORDER), current=self.theme, allow_none=False
            ),
            on_close,
        )

    # ── geometry / screens ────────────────────────────────────────────────
    @property
    def screen(self) -> Any:
        """The top of the screen stack (the base screen when no modal is up)."""

        return self._screens[-1]

    @property
    def screens(self) -> list[Any]:
        return list(self._screens)

    # ── the ported test surface: query / run_test / remove_children ───────
    def query(self, selector: Any) -> list[Node]:
        """Every node matching ``selector`` in the BASE screen's tree.

        Textual's ``App.query`` walks the default screen's DOM, and so does
        this: the header, the log, the input, the panel, the chips and the
        jump button are all mounted on ``_base_screen``. A modal's contents are
        reached through ``app.screen.query(...)`` — the same split the ported
        tests already use, because a pushed screen is a separate tree.
        """

        return self._base_screen.query(selector)

    def query_one(self, selector: Any, expect: type | None = None) -> Any:
        """The first match in the base screen's tree (``app.query_one('#id')``)."""

        return self._base_screen.query_one(selector, expect)

    def remove_children(self) -> Any:
        """Drop every child of the BASE screen (Textual's ``App.remove_children``).

        The app's own widgets are mounted once in ``__init__``, so this exists
        for ported teardown paths only: a test that empties the tree is expected
        to rebuild the app rather than keep painting into a half-tree.
        """

        for child in list(self._base_screen.children):
            # `Node.remove()` takes no argument, but `ChatScroll.remove(row)`
            # is the log's own row dropper — so detach through the parent list
            # rather than calling whatever `remove` the child happens to expose.
            if child in self._base_screen.children:
                self._base_screen.children.remove(child)
            child.parent = None
        return _awaitable_none()

    def run_test(self, size: tuple[int, int] = (80, 24)) -> Any:
        """``async with app.run_test(size=(w, h)) as pilot`` — a headless boot."""

        return nodes_module.run_test(self, size)

    @property
    def focused(self) -> Node | None:
        """The node that owns the keyboard."""

        return self._focused

    def set_focus(self, node: Node | None) -> None:
        self._set_focus(node)

    def _set_focus(self, node: Node | None) -> None:
        for chip in self._panel.chips.values():
            chip.focused = False
        self._input.focused = False
        self._jump.focused = False
        if node is not None:
            node.focused = True
        self._focused = node
        self._invalidate()

    def _focus_order(self) -> list[Node]:
        return [self._input, *self._panel.chips.values()]

    def _cycle_focus(self, direction: int) -> None:
        """Tab / Shift+Tab: input → outo → style → subagent → session → input."""

        order = self._focus_order()
        try:
            index = order.index(self._focused)
        except ValueError:
            index = 0
        self._set_focus(order[(index + direction) % len(order)])

    def push_screen(self, screen: Any, on_close: Callable[[Any], None] | None = None) -> Any:
        """Show ``screen`` on top of the current one (Textual's ``push_screen``)."""

        screen.attach(self, on_close)
        self._screens.append(screen)
        return screen

    async def push_screen_wait(self, screen: Any) -> Any:
        """Push ``screen`` and await its result (``dismiss`` resolves it)."""

        self.push_screen(screen)
        return await screen.wait_result()

    def pop_screen(self, result: Any = None) -> None:
        """Pop the top screen, resolve its waiter and resume the one below.

        The pop happens FIRST: a close callback may push the NEXT screen (the
        two-prompt caps editor does), and that one has to land on the screen
        UNDER this one. ``Modal._settle`` is idempotent, so a ``dismiss`` that
        already settled loses nothing here.
        """

        if len(self._screens) <= 1:
            return
        screen = self._screens.pop()
        settle = getattr(screen, "_settle", None)
        if callable(settle):
            settle(result)
        resume = getattr(self.screen, "on_resume", None)
        if callable(resume):
            resume()

    def dismiss_screen(self, result: Any = None) -> None:
        self.pop_screen(result)

    def run_worker(self, coro: Any, exclusive: bool = False) -> Any:
        """Run ``coro`` as a background task (Textual's ``run_worker``)."""

        if exclusive:
            for task in list(self._workers):
                if task.get_name() == "dispatch":
                    task.cancel()
        task = asyncio.ensure_future(coro)
        task.set_name("dispatch" if exclusive else "worker")
        return self._workers.add(task)

    @property
    def workers(self) -> Workers:
        return self._workers

    def call_from_thread(self, callback: Callable[..., Any], *args: Any) -> None:
        """Run ``callback`` on the loop thread (safe from any thread).

        The loop and its thread id are captured ONCE at boot (``run_async`` /
        ``_boot_headless``) rather than looked up per call:
        ``asyncio.get_running_loop()`` raises on the ``asyncio.to_thread`` worker
        the agent loop runs on, so the old "no loop → run inline" branch fired on
        EVERY sink callback and mutated UI state from the worker thread, racing
        the loop's ``compose()``. That is invariant 10's hop, silently skipped.
        """

        loop = self._loop
        if loop is not None and threading.get_ident() != self._loop_thread:
            loop.call_soon_threadsafe(callback, *args)
            return
        # Already on the loop thread (a timer, a key handler) or running with no
        # loop at all (a synchronous caller): run it here.
        callback(*args)
        # A sink hop is a state mutation by definition — the workers call this
        # only to touch the UI.
        self._invalidate()

    def _bind_loop(self) -> None:
        """Remember the running loop and its thread — the hop's destination."""

        with contextlib.suppress(RuntimeError):  # no running loop: stay unbound
            self._loop = asyncio.get_running_loop()
            self._loop_thread = threading.get_ident()

    def set_interval(self, interval: float, callback: Callable[[], Any]) -> Timer:
        """A repeating timer on the app's clock (Textual's ``set_interval``)."""

        timer = Timer(self, interval, callback)
        self._timers.append(timer)
        return timer

    def post_message(self, message: Any) -> None:
        """Queue a message for the next pump (Textual's ``post_message``)."""

        self._messages.append(message)
        self._invalidate()

    def _invalidate(self) -> None:
        """Mark the next frame as needing a repaint.

        Called by everything that can change what the frame shows: a key or
        mouse handler (they run through :meth:`handle_key` and
        :meth:`_route_mouse`), a fired timer, a queued message, a worker hop,
        and any mutation of the log/screen tree. The loop then decides whether
        to run ``compose()`` at all.
        """

        self._dirty = True

    def _mark_clean(self) -> None:
        """Record that the frame just painted matches the current state."""

        self._dirty = False

    @property
    def is_dirty(self) -> bool:
        """Whether the next frame differs from the one on screen."""

        return self._dirty

    def _paint_now(self) -> list[list[tuple[str, pt.Style]]] | None:
        """Compose the frame, and clear the dirty flag that asked for it.

        This is the ONE place the app decides to repaint. It returns the
        composed lines so a caller (the real loop) can hand them straight to
        ``term.draw`` instead of composing a second time — ``compose()`` is
        the expensive half of a frame (measured at 4.4 ms against 6.5 ms for
        the whole draw on a 120x32 screen), so composing twice per frame for
        nothing was the bulk of the idle cost.

        ``None`` means "nothing changed, the screen already shows this state":
        the loop SKIPS the draw entirely, which is what makes an idle tick free
        instead of a full 60 fps rebuild.
        """

        if not self._dirty:
            return None
        self._mark_clean()
        return self.compose(self.size.width, self.size.height)

    def copy_to_clipboard(self, text: str) -> None:
        """Copy ``text`` with OSC 52 — no clipboard library, works over ssh."""

        self._clipboard = text
        if self._terminal is None:
            return
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        _write_terminal(_CLIPBOARD_OSC52.format(payload=payload))

    def exit(self) -> None:
        """Ask the loop to stop (cancelling the running turn first)."""

        if self._cancel_event is not None:
            self._cancel_event.set()
        self._running = False

    def action_quit(self) -> None:
        self.exit()

    def action_show_help(self) -> None:
        self._spinner_status("Ctrl+P palette · Ctrl+L clear · Esc Esc stop · Ctrl+C quit")

    def action_maximize(self) -> None:
        self._spinner_status("maximize is not available in the terminal TUI")

    def action_screenshot(self) -> None:
        self._spinner_status("screenshots are taken with the Computer tool")

    # ── the command palette ───────────────────────────────────────────────
    def get_system_commands(self, screen: Any = None) -> list[Any]:
        """The palette's items: this app's own actions, in the order it shows them.

        No numeric prefix and no ``Clear log`` / ``Maximize`` / ``Screenshot``
        entries: the list is already ordered, and the owner never used those
        three (``Clear log`` stays on Ctrl+L). The wording lives here, once.
        """

        return [
            _Command("New session", "Create a new session", self._new_session),
            _Command("Pick session", "Switch the active session", self._open_session_picker),
            _Command(
                "Configure outo",
                "Provider → model → reasoning for the main agent",
                self._configure_outo,
            ),
            _Command(
                "Configure subagent",
                "Provider → model → reasoning for the subagent",
                self._open_subagent_editor,
            ),
            _Command(
                "Pick style", "Switch the active style document", self._open_style_picker
            ),
            _Command("Theme", "Change the current theme", self.action_change_theme),
            _Command(
                "Keys",
                "Show the keys for the focused widget",
                self.action_show_help,
            ),
            _Command("Quit", "Quit the application as soon as possible", self.action_quit),
        ]

    def action_command_palette(self) -> None:
        """Ctrl+P: the centred palette dialog, closed on Esc or a click outside."""

        self.push_screen(CommandPalette())

    # ── app state helpers ─────────────────────────────────────────────────

    @property
    def now(self) -> float:
        """The app's clock (virtual in a headless boot)."""

        return self._now

    def _mint_session_name(self) -> str:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"tui-{ts}-{uuid.uuid4().hex[:6]}"

    def _pin_session(self, name: str) -> None:
        """Adopt ``name`` as this conversation's session and publish it.

        The pin is what routing reads from here on; the
        ``settings_store.update`` is the "active session" convention other
        surfaces (and ``miniouto status``) read.
        """

        self._session_name = name
        self._session_assigned = True
        self._chat_started = True
        settings_store.update(session=name)

    def _adopt_minted_session(self) -> None:
        """Mint this TUI's own session name, then pin it.

        A fresh name would otherwise start with NO provider config and freeze
        whatever the global default happened to be at its first turn, silently
        dropping the model the conversation the user just continued was
        configured with. Seeding is copy-if-absent (``session_config``), so a
        source without a pin seeds nothing.
        """

        # The seed source is the published active session, read once here — it
        # must NOT go through `_pinned_session`, or this TUI's routing (and its
        # inbox drain) would adopt another process's session.
        source = settings_store.load().session or _DEFAULT_SESSION
        name = self._mint_session_name()
        session_config.seed_from(name, source)
        self._pin_session(name)

    def _pinned_session(self) -> str:
        """The session this conversation routes on (see ``_session_name``).

        Before the first session event there is no pin, so this falls back to a
        unique per-instance queue WITHOUT adopting the published active session.
        The mint in ``_dispatch`` still replaces the fallback with this TUI's own
        ``tui-…`` name, which is why a CLI run's ``--name`` is never continued.
        """

        if self._session_name is None:
            self._session_name = self._unassigned_inbox
        return self._session_name

    def _inbox_session(self) -> str:
        """The session whose inbox this TUI drains — the value ``ChatOptions`` gets."""

        return self._pinned_session()

    def _config_session(self) -> str:
        """Whose pinned config the chips should read, WITHOUT adopting it.

        ``_pinned_session`` is the one-shot adoption that decides what this
        conversation routes on, and it belongs to the first routing call — a
        repaint must not make that decision as a side effect.
        """

        return self._session_name or settings_store.load().session or _DEFAULT_SESSION

    # ── boot / teardown ───────────────────────────────────────────────────
    def _boot(self, size: tuple[int, int]) -> None:
        """Everything ``on_mount`` did: title, chips, log, logo, focus, timers."""

        self.size = Size(*size)
        cwd = Path.cwd()
        home = Path.home()
        try:
            cwd_display = "~/" + str(cwd.relative_to(home))
        except ValueError:
            cwd_display = str(cwd)
        self.title = cwd_display
        self.sub_title = f"miniouto v{__version__}"
        self._header.title = self.title
        self._header.sub_title = self.sub_title
        self._refresh_chips()
        self._log.anchor()
        saved = settings_store.load()
        if saved.theme and self.get_theme(saved.theme):
            self.theme = saved.theme
        self._show_logo()
        self._set_focus(self._input)
        if self._inbox_timer is None:
            self._inbox_timer = self.set_interval(
                _INBOX_POLL_INTERVAL, self._deliver_pending_messages
            )
        self._booted = True
        self.run_worker(self._check_for_updates(), exclusive=False)
        self.compose(*size)
        self._publish_title()
        self._sync_screen_background()

    async def _boot_headless(self, size: tuple[int, int] = (80, 24)) -> ChatTUI:
        """The ``run_test`` boot: no terminal, no reader thread, no signal."""

        self._bind_loop()
        if not self._booted:
            self._boot(size)
        return self

    async def _shutdown_headless(self) -> None:
        """Stop timers and workers so nothing leaks into the next test."""

        for timer in list(self._timers):
            timer.stop()
        self._timers.clear()
        self._inbox_timer = None
        self._spin_timer = None
        self._workers.cancel_all()
        await asyncio.gather(*list(self._workers), return_exceptions=True)
        self._messages.clear()
        self._screens = [self._base_screen]
        self._booted = False
        # The loop is about to close: a stale binding would make the next
        # `call_from_thread` raise into a worker instead of running inline.
        self._loop = None
        self._loop_thread = 0

    def _drain_messages(self, rounds: int = 32) -> None:
        """Deliver every queued message, including ones a handler posts.

        This is the SYNCHRONOUS half of ``_pump_once``, and it is what makes the
        real loop work: ``ChatInput.action_submit`` posts
        ``ChatInput.Submitted`` through ``app.post_message``, which only appends
        to ``_messages``. A loop that drained the input QUEUE alone therefore
        left Enter doing nothing in a real terminal — the headless
        ``pilot.pause`` path hid it, because ``_pump_once`` pumps both. Looping
        for a few rounds is what lets a handler's own post (a chip click, the
        first paint of a dispatch) land in the same tick rather than the next.
        """

        for _ in range(rounds):
            if not self._messages:
                return
            batch, self._messages = self._messages, []
            for message in batch:
                # Dispatching a message IS the mutation (a submit, a chip
                # click, a screen push), so the frame is stale from here on.
                self._invalidate()
                self._dispatch_message(message)

    async def _pump_once(self, dt: float = 0.0) -> None:
        """Advance the clock, fire timers, drain messages, repaint — one tick."""

        self._now += max(0.0, float(dt))
        self._fire_timers()
        self._drain_messages()
        await asyncio.sleep(0)
        # The PILOT path always paints, dirty or not: `pilot.pause()` is the
        # "settle" the tests (and `Pilot.click`, which reads `node.region`) rely
        # on, and a region is only correct after a compose. The real loop is the
        # one that skips, and it skips because it has a frame already on screen.
        self._invalidate()
        self._paint_now()

    def _fire_timers(self) -> None:
        now = self._now
        for timer in list(self._timers):
            timer.tick(now)

    def tick(self, dt: float = _REDRAW_SECONDS) -> None:
        """Advance the clock deterministically (what the real loop calls)."""

        self._now += max(0.0, float(dt))
        self._fire_timers()

    # ── message dispatch ──────────────────────────────────────────────────
    def _dispatch_message(self, message: Any) -> None:
        """Route one queued message to the handler that owns it."""

        if hasattr(message, "screen_x") and hasattr(message, "screen_y"):
            self._dispatch_mouse_message(message)
            return
        if hasattr(message, "chip"):
            self.on_status_chip_chip_clicked(message)
            return
        if hasattr(message, "value"):
            self.on_chat_input_submitted(message)

    def _dispatch_mouse_message(self, message: Any) -> None:
        """A Textual-shaped mouse report: convert, then route by cell."""

        name = type(message).__name__
        kind = {"MouseDown": "down", "MouseUp": "up"}.get(name, "move")
        chain = int(getattr(message, "chain", 1) or 1)
        x = int(getattr(message, "screen_x", 0) or 0)
        y = int(getattr(message, "screen_y", 0) or 0)
        self._route_mouse(Mouse(kind=kind, x=x, y=y, button=0), chain=chain)

    def inject_key(self, key: Key) -> bool:
        """Feed one key event through the same handler the real loop uses."""

        # Every input path — this one, the real loop's `_handle_event`, and the
        # modal layer — mutates state the frame paints, so the frame is stale
        # from here on. It is cheap (one attribute write) and it is what lets
        # the real loop skip `compose()` on an idle tick.
        self._invalidate()
        return self.handle_key(key)

    def inject_mouse(self, mouse: Mouse, chain: int = 1) -> bool:
        self._invalidate()
        return self._route_mouse(mouse, chain=chain)

    def inject_paste(self, text: str) -> bool:
        self._invalidate()
        return self._route_paste(text)

    # ── keys ──────────────────────────────────────────────────────────────
    def handle_key(self, key: Key) -> bool:
        """The one key router: global bindings → modal → the focused node."""

        code = key.code
        ctrl = key.ctrl
        shift = key.shift

        if ctrl and code == "c" and not shift:
            self.exit()
            return True
        if ctrl and shift and code == "c":
            self.action_copy_text()
            return True
        if ctrl and code == "l":
            self.action_clear_log()
            return True
        if ctrl and code == "p":
            if isinstance(self.screen, Modal):
                self.pop_screen(None)
            else:
                self.action_command_palette()
            return True

        if isinstance(self.screen, Modal):
            return bool(self.screen.handle_key(key))

        if code == "Esc":
            self.action_escape_pressed()
            return True
        if code == "PageUp":
            self.action_page_up()
            return True
        if code == "PageDown":
            self.action_page_down()
            return True
        if code == "Home":
            self.action_scroll_home()
            return True
        if code == "End":
            self.action_scroll_end()
            return True
        if code == "BackTab":
            # CSI Z. The parser carries it as its own code rather than
            # Tab+shift, and the headless `compile_key` rewrites it to Tab+shift —
            # so testing only for "Tab" here made the real terminal ignore it.
            self._cycle_focus(-1)
            return True
        if code == "Tab":
            self._cycle_focus(-1 if shift else 1)
            return True

        target = self._focused
        return bool(target is not None and target.handle_key(key, self))

    def action_escape_pressed(self) -> None:
        """Esc: modal → dismiss, busy → double-press stop, else nothing."""

        if isinstance(self.screen, Modal):
            self.screen.dismiss(None)
            return
        if not self._busy or self._cancel_event is None:
            self._last_escape = 0.0
            return
        now = time.monotonic()
        if now - self._last_escape <= _ESC_DOUBLE_PRESS_WINDOW:
            self._last_escape = 0.0
            self._esc_hint = "stopping after the current step…"
            self._esc_hint_until = float("inf")
            self._cancel_event.set()
            self._spinner_status(self._esc_hint)
        else:
            self._last_escape = now
            self._esc_hint = "press esc again to stop"
            self._esc_hint_until = now + _ESC_DOUBLE_PRESS_WINDOW
            self._spinner_status(self._esc_hint)

    # ── mouse ─────────────────────────────────────────────────────────────
    def _row_at(self, x: int, y: int) -> Any:
        """The mounted log row occupying screen cell ``(x, y)``, if any."""

        for row in self._log.rows:
            region = self._row_origins.get(row) or row.region
            if region.height and region.contains(x, y):
                return row
        return None

    def _route_mouse(self, mouse: Mouse, chain: int = 1) -> bool:
        """Route one mouse report: a modal owns the screen, else the base one."""

        if isinstance(self.screen, Modal):
            return bool(self.screen.handle_mouse(mouse))
        if self._jump.display and self._jump.region.contains(mouse.x, mouse.y):
            return bool(self._jump.handle_mouse(mouse, mouse.x, mouse.y, self))
        if self._panel.region.height and self._panel.region.contains(mouse.x, mouse.y):
            return bool(self._panel.handle_mouse(mouse, mouse.x, mouse.y, self))

        row = self._row_at(mouse.x, mouse.y)
        if row is None:
            if mouse.kind == "down":
                self.clear_selection()
            return False
        return self._row_mouse(row, mouse, chain=chain)

    def _row_mouse(self, row: Any, mouse: Mouse, chain: int = 1) -> bool:
        """Selection first (it is the gesture's evidence), then the row's own logic.

        Offsets are CONTENT cells — the row's own text after its border/padding
        gutter — which is the space ``Selection.get_span`` and every ported drag
        assertion speak (a press on the collapsed header's glyph is offset 0, not
        2). The mouse report carries OUTER cells, so the gutter is subtracted
        here, and the row shifts the span back by the same amount when it paints.
        """

        rel_x = mouse.x - row.region.x
        rel_y = mouse.y - row.region.y
        gutter = getattr(row, "_gutter_width", 0)
        here = Offset(max(0, rel_x - gutter), rel_y)
        if mouse.kind == "down":
            self._select_anchor = (row, here)
            self._select_dragged = False
        elif mouse.kind in ("move", "drag") and self._select_anchor is not None:
            anchor_row, start = self._select_anchor
            if anchor_row is row and here != start:
                self._select_dragged = True
                self._set_selection(row, Selection.from_offsets(start, here))
        elif mouse.kind == "up":
            if not self._select_dragged:
                # A click that dragged nothing clears the selection, exactly as
                # Textual did on MouseUp — and that absence is what lets the row
                # treat the gesture as a plain click.
                self.clear_selection()
            self._select_anchor = None
        row.handle_mouse(mouse, rel_x, rel_y, self, chain=chain)
        return True

    def _set_selection(self, row: Any, selection: Selection) -> None:
        """Record a selection, extending the end over the cell dragged onto."""

        start, end = selection.start, selection.end
        if start is not None and end is not None:
            selection = Selection(start, Offset(end.x + 1, end.y))
        self.screen.selections[row] = selection
        row.selection = selection

    def clear_selection(self) -> None:
        self.screen.clear_selection()

    def _route_paste(self, text: str) -> bool:
        handler = getattr(self._focused, "handle_paste", None)
        if handler is not None:
            return bool(handler(text))
        return False

    # ── actions ───────────────────────────────────────────────────────────
    def action_clear_log(self) -> None:
        self._log.remove_children()
        self._reset_rows()
        self._logo_shown = False

    def action_copy_text(self) -> None:
        text = self.screen.get_selected_text()
        if text:
            self.copy_to_clipboard(text)
            self.clear_selection()

    def action_page_up(self) -> None:
        self._log.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        self._log.scroll_page_down(animate=False)

    def action_scroll_home(self) -> None:
        self._log.scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        self._log.scroll_end(animate=False)

    def scroll_wheel(self, lines: int) -> None:
        """Wheel scrolling: negative scrolls up (one detent = WHEEL_LINES)."""
        self._log.scroll_by(lines)

    def _route_wheel(self, event: Mouse) -> bool:
        """A wheel detent: the modal on top owns it, else the log scrolls.

        Every other mouse report already goes through ``_route_mouse``, which
        hands a modal the whole screen. Wheel reports were dispatched straight to
        ``scroll_wheel``, so with a palette (or any picker) open the LOG scrolled
        behind it and the modal's own list did not move.
        """

        if isinstance(self.screen, Modal):
            return bool(self.screen.handle_mouse(event))
        self.scroll_wheel(-WHEEL_LINES if event.kind == "wheel_up" else WHEEL_LINES)
        return True

    def _on_log_scroll(self, scroll_y: float) -> None:
        self._window.maybe_load_older(self._log, scroll_y)
        self._sync_jump_button()

    def _sync_jump_button(self) -> None:
        """Show the jump button exactly when the log is away from the tail."""
        self._jump.sync()

    # ── rows ──────────────────────────────────────────────────────────────
    def _reset_rows(self) -> None:
        """Drop the row list alongside ``remove_children()`` (logo/clear/load)."""
        self._window.reset()
        self._optimistic_rows.clear()
        self._stream_text_row = None
        self._stream_thinking_row = None
        self._pending_tool_rows.clear()
        self._row_origins.clear()

    def _mount_row(self, row: Any, msg_ref: str | None = None) -> None:
        # A row on screen is a frame that changed: this is the one place every
        # log append goes through (a user row, a system row, an event, an
        # answer), so it is where the dirty flag is set for them all.
        self._invalidate()
        if msg_ref is not None:
            row.msg_ref = msg_ref
        self._window.append(self._log, row, defer_mount=self._bulk_loading)
        self._sync_jump_button()

    def _mount_tail_window(self) -> None:
        """After a bulk load, mount only the trailing window of rows."""
        self._window.mount_tail(self._log)
        self._sync_jump_button()

    def _user_row(self, text: str) -> RowStatic:
        return RowStatic(Text("> ", style=f"bold {self.current_theme.accent}") + Text(text))
    def _post_user(self, text: str) -> None:
        self._mount_row(self._user_row(text))

    def _post_alarm_row(self, text: str) -> None:
        """Render an alarm as the user message the model actually receives.

        An alarm is a synthetic ``role="user"`` message (invariant 16), so it
        reads like one — the same ``> `` prompt — plus the ``-alarm`` class
        (``$warning`` text, no rule), which keeps it identifiable as a notice.
        """

        row = self._user_row(text)
        row.add_class("-alarm")
        self._mount_row(row)

    def _post_system(self, text: str) -> None:
        self._mount_row(RowStatic(Text(f"[{text}]", style=self.current_theme.warning)))

    def _apply_stream_text(self, delta: str) -> None:
        """Extend the current message's live text row (UI thread only).

        The reference is cleared on every path that invalidates the row, so a
        row that is gone — evicted, or dropped by a session reload — is never
        updated again and never re-mounted, which would put it out of order.
        """

        if not delta:
            return
        row = self._stream_text_row
        if row is None:
            row = StreamRow(delta)
            self._stream_text_row = row
            self._mount_row(row)
            return
        row.append(delta)

    def _apply_stream_thinking(self, delta: str) -> None:
        """Extend the current message's live reasoning row (UI thread only)."""

        if not delta:
            return
        row = self._stream_thinking_row
        if row is None:
            row = ThinkingRow()
            self._stream_thinking_row = row
            self._mount_row(row)
            row.begin_stream()
        row.append(delta)

    def _finalize_stream(self) -> None:
        """Drop the live rows once the message's canonical form exists."""

        for row in (self._stream_text_row, self._stream_thinking_row):
            if row is not None:
                self._window.remove(row, self._log)
        self._stream_text_row = None
        self._stream_thinking_row = None

    def _add_event_row(self, event: LoopEvent) -> None:
        if event.kind == "thinking":
            live = self._stream_thinking_row
            self._stream_thinking_row = None
            if live is not None and live.is_attached:
                # The streamed reasoning was this message's prefix; the
                # whole-text event is the canonical copy.
                live.finalize(event.text)
                live.msg_ref = event.msg_ref
                return
            self._mount_row(ThinkingRow(event.text), event.msg_ref)
            return
        if event.kind == "tool":
            row = ToolRow(
                event.text,
                detail=getattr(event, "detail", None),
                tool_name=event.tool_name,
            )
            row.msg_ref = event.msg_ref
            self._pending_tool_rows.append(row)
            self._mount_row(row)
            return
        if event.kind == "tool_result":
            text = event.text or ""
            is_error = text.startswith("error: ")
            pending: ToolRow | None = None
            for candidate in self._pending_tool_rows:
                if candidate.tool_name == event.tool_name and not candidate.has_result:
                    pending = candidate
                    break
            if pending is not None:
                self._pending_tool_rows.remove(pending)
                pending.set_result(text, is_error=is_error)
            else:
                # Defensive fallback: a tool_result with no matching tool event
                # (replay oddity, out-of-order delivery, partial session). Mount
                # a plain muted row so the result is not lost.
                fallback: Any = EventRow(Text(text))
                if is_error:
                    fallback.add_class("-error")
                self._mount_row(fallback, event.msg_ref)
            return
        if event.kind == "response" and self._stream_text_row is not None:
            # The deltas already rendered this message in the live row above; a
            # second copy would duplicate the text.
            self._stream_text_row = None
            return
        if event.kind == "alarm":
            if not self._claim_inbox_event(event):
                return
            self._post_alarm_row(event.text)
            return
        if event.kind == inbox.USER_KIND:
            if not self._claim_inbox_event(event):
                return
            self._post_user(event.text)
            return
        if event.kind in ("response", "context"):
            # Message boundary: the next deltas belong to a new message and need
            # their own live row.
            self._stream_text_row = None
            self._stream_thinking_row = None
        plain: Any = EventRow(Text(event.text))
        if event.kind == "error":
            plain.add_class("-error")
        self._mount_row(plain, event.msg_ref)

    def _remove_event_rows(self, msg_refs: list[str]) -> None:
        """Drop the rows of messages the agent loop removed (UI thread)."""

        refs = set(msg_refs)
        if not refs:
            return
        for row in list(self._window.rows):
            if getattr(row, "msg_ref", None) in refs:
                self._window.remove(row, self._log)
        self._pending_tool_rows = [r for r in self._pending_tool_rows if r.msg_ref not in refs]

    # ── the animation ticker ──────────────────────────────────────────────
    #
    # ONE ticker drives every animated indicator: the chat status line, the
    # wizard's "fetching…" line, and the per-row spinners of live background
    # jobs. It runs while any of them has something to show, and stops (with the
    # bottom row blanked) the moment none does — a spinner frozen mid-frame, or a
    # stale line left after the work finished, is worse than no indicator.

    def _render_spinner(self, frame: str, text: str) -> None:
        # The spinner is the only thing that changes on an otherwise idle
        # screen, so the ticker's mutation is what dirties those frames.
        self._invalidate()
        self._panel.render_spinner(frame, text)

    def _set_chat_working(self, working: bool) -> None:
        self._chat_working = working
        self._sync_ticker()

    def _set_activity(self, text: str) -> None:
        self._activity = text or _SPINNER_DEFAULT_TEXT

    def _sync_ticker(self) -> None:
        needed = self._chat_working or self._wizard_working
        if needed:
            if self._spin_timer is None:
                self._spin_frame_idx = 0
                self._spin_timer = self.set_interval(_SPINNER_INTERVAL, self._tick_spin)
                self._paint_spinner(_SPINNER_FRAMES[0])
        elif self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
            self._render_spinner("", "")

    def _tick_spin(self) -> None:
        frame = _SPINNER_FRAMES[self._spin_frame_idx % len(_SPINNER_FRAMES)]
        self._spin_frame_idx += 1
        self._paint_spinner(frame)
        # A turn that just ended can leave nothing to animate, so re-check
        # instead of leaving a timer running on nothing.
        self._sync_ticker()

    def _paint_spinner(self, frame: str) -> None:
        if self._chat_working:
            self._render_spinner(frame, self._spin_text())
        elif self._wizard_working and self._wizard_status:
            self._render_spinner(frame, self._wizard_status)
        else:
            self._render_spinner("", "")

    def _spin_text(self) -> str:
        text = self._activity
        if self._esc_hint is not None:
            if time.monotonic() < self._esc_hint_until:
                return self._esc_hint
            self._esc_hint = None
        return text

    def _spinner_status(self, text: str, *, working: bool = False) -> None:
        """Put a line in the bottom row (``working=True`` animates it)."""

        self._wizard_status = text
        self._wizard_working = working
        self._sync_ticker()
        if not working:
            self._render_spinner("", text)

    def _clear_spinner_status(self) -> None:
        self._wizard_status = None
        self._wizard_working = False
        self._sync_ticker()
        if not self._chat_working:
            # `_sync_ticker` only blanks as part of stopping a timer it owns; a
            # notice never had one, so blank the line explicitly.
            self._render_spinner("", "")

    # ── chips ─────────────────────────────────────────────────────────────
    def _refresh_chips(self) -> None:
        s = settings_store.load()
        # Read-through: the pinned session's own config wins, so a TUI bound to a
        # session shows THAT session's provider/model/reasoning even while the
        # global default moves underneath it in another process.
        config = _effective_config(self._config_session())
        outo_model, outo_reasoning = _trio_display(OUTO_TARGET, config)[1:]
        self._panel.set_value("outo", f"{outo_model} {outo_reasoning}")

        sub_provider, sub_model, sub_reasoning = _trio(config, SUBAGENT_TARGET.fields)
        if sub_provider or sub_model or sub_reasoning:
            record = _record(sub_provider)
            model_part = sub_model or (record.default_model if record else "") or "same"
            reasoning_part = sub_reasoning or _reasoning_chip_value(record)
            self._panel.set_value("subagent", f"{model_part} {reasoning_part}")
        else:
            self._panel.set_value("subagent", "same as outo")

        # The style chip reads the SESSION's pinned style, the same read-through
        # the provider/model chips do.
        pinned_style = config.style or s.style
        self._panel.set_value("style", pinned_style or "-")
        name = self._session_name if self._chat_started and self._session_name else ""
        self._panel.set_session(name)

    # ── session events ────────────────────────────────────────────────────
    def _show_logo(self) -> None:
        """The boot ornament: logo, blank row, version line (inside the column)."""

        self._log.remove_children()
        self._reset_rows()
        # The logo belongs to the chat COLUMN, not to the app: on a wide terminal
        # the column is capped, so sizing by the app width alone would draw it
        # past the right edge of the log.
        columns = logo_columns(self._log.size.width, self.size.width)
        self._log.mount(
            RowStatic(Text(_render_logo(columns - 2), style=self.current_theme.accent))
        )
        self._log.mount(RowStatic(Text("")))
        self._log.mount(
            RowStatic(
                Text(
                    f"miniouto v{__version__} — Ready. Press Ctrl+P for commands.",
                    style="dim",
                )
            )
        )
        if self._update_notice is not None:
            self._post_system(self._update_notice)
        self._logo_shown = True

    def _new_session(self) -> None:
        self._adopt_minted_session()
        session_store.create(self._pinned_session())
        self._refresh_chips()
        self._show_logo()

    def _load_session_history(self, session_name: str) -> None:
        self._log.remove_children()
        self._reset_rows()
        self._logo_shown = False
        self._sync_ticker()
        data = session_store.load(session_name)
        # Bulk: rows are only collected here; `_mount_tail_window` mounts just the
        # trailing window instead of the whole history.
        self._bulk_loading = True
        try:
            if not data.turns:
                self._mount_row(RowStatic(Text("(empty session)", style="dim")))
            for turn in data.turns:
                if turn.user:
                    self._post_user(turn.user)
                for ev_dict in turn.events:
                    self._replay_event(LoopEvent.from_dict(ev_dict))
                if turn.assistant:
                    self._mount_row(AnswerRow(turn.assistant))
                elif turn.status in ("running", "interrupted"):
                    # A "running" turn on disk means its writer died mid-turn
                    # (SIGKILL/crash); either way there is no final answer to
                    # render — mark the gap explicitly.
                    self._post_system(
                        "turn interrupted — progress above was saved; "
                        "send a message to continue"
                    )
                self._mount_row(RowStatic(Text("")))
        finally:
            self._bulk_loading = False
        self._mount_tail_window()

    def _replay_event(self, event: LoopEvent) -> None:
        """Re-render one recorded turn event (session reload path).

        Everything routes through ``_add_event_row``, so a reloaded turn renders
        exactly like the live one. Events recorded by an older miniouto —
        ``subagent_start`` / ``subagent_end`` in particular — still deserialize
        and now replay as plain rows.
        """

        self._add_event_row(event)

    # ── the provider / style / session pickers ─────────────────────────────
    def on_status_chip_chip_clicked(self, event: Any) -> None:
        chip_id = getattr(getattr(event, "chip", None), "id", None)
        if chip_id == "chip-outo":
            self._configure_outo()
        elif chip_id == "chip-subagent":
            self._open_subagent_editor()
        elif chip_id == "chip-style":
            self._open_style_picker()
        elif chip_id == "chip-session":
            self._open_session_picker()

    def _configure_outo(self) -> None:
        self._open_provider_settings(OUTO_TARGET)

    def _open_subagent_editor(self) -> None:
        self._open_provider_settings(SUBAGENT_TARGET)

    def _open_provider_settings(self, target: TrioTarget) -> None:
        """Open the provider settings popup for one trio (invariant 12)."""

        self.push_screen(
            ProviderSettingsPopup(
                target, session=self._pinned_session(), on_change=self._refresh_chips
            )
        )

    def _apply_provider(self, name: str) -> None:
        """Adopt ``name`` as the active provider, through the pin and the default.

        The pin write is what keeps a provider added mid-conversation from being
        shadowed by the config this session already froze.
        """

        _persist_trio(self._pinned_session(), OUTO_TARGET, (name, *_model_defaults(name)))
        self._refresh_chips()

    async def _catalog_add_flow(self) -> None:
        self._spinner_status("fetching catalog…", working=True)
        try:
            all_providers = await asyncio.to_thread(catalog_api.list_providers)
        except Exception as exc:
            self._spinner_status(f"catalog error: {exc}")
            return
        self._clear_spinner_status()

        name_to_provider: dict[str, dict[str, Any]] = {}
        for provider in all_providers:
            if resolve_catalog_dialect(provider) is not None:
                name_to_provider[provider["name"]] = provider
        if not name_to_provider:
            self._spinner_status("No catalog providers have a supported api_format.")
            return

        picked_name = await self.push_screen_wait(
            SelectionModal(
                f"Add from catalog ({len(name_to_provider)} addable)",
                sorted(name_to_provider.keys()),
                allow_none=False,
            )
        )
        if not picked_name:
            return

        catalog_provider = name_to_provider[picked_name]
        our_name = str(catalog_provider["slug"])
        if provider_store.get(our_name) is not None:
            choice = await self.push_screen_wait(
                SelectionModal(
                    f"Provider {our_name!r} already exists",
                    ["overwrite", "cancel"],
                    allow_none=False,
                )
            )
            if choice != "overwrite":
                return

        api_key = await self.push_screen_wait(
            TextInputModal(
                f"API key for {picked_name}",
                placeholder="sk-…",
                hint="Enter to confirm · Esc to cancel",
            )
        )
        if api_key is None:
            return

        self._spinner_status("fetching model list…", working=True)
        default_model = ""
        try:
            models = await asyncio.to_thread(catalog_api.list_models, picked_name)
            if models:
                default_model = models[0].get("id", "")
        except Exception as exc:
            self._spinner_status(f"catalog models error: {exc}")

        reasoning_effort: str | None = None
        reasoning_api = _reasoning_api()
        if default_model and reasoning_api is not None:
            choices: list[str] | None = None
            try:
                choices = await asyncio.to_thread(
                    reasoning_api.reasoning_choices, default_model, our_name
                )
            except Exception:
                choices = None
            if choices:
                default_choice: str | None = None
                try:
                    default_choice = await asyncio.to_thread(
                        reasoning_api.default_reasoning_choice, default_model, our_name
                    )
                except Exception:
                    default_choice = None
                picked = await self.push_screen_wait(
                    SelectionModal(
                        f"Reasoning ({default_model})",
                        choices,
                        current=default_choice or "off",
                        allow_none=False,
                    )
                )
                if picked:
                    reasoning_effort = picked

        try:
            provider = add_provider_from_catalog(
                catalog_provider,
                api_key=api_key.strip(),
                default_model=default_model,
                reasoning_effort=reasoning_effort,
            )
        except ValueError as exc:
            self._spinner_status(f"add failed: {exc}")
            return

        paths.ensure_dirs()
        provider_store.upsert(provider)
        if reasoning_effort is not None:
            stored = provider_store.get(our_name)
            if stored is not None:
                try:
                    provider_store.upsert(replace(stored, reasoning_effort=reasoning_effort))
                except TypeError:
                    provider_store.upsert(_with_reasoning_effort(stored, reasoning_effort))
        self._apply_provider(our_name)
        status_parts = [
            f"provider → {our_name} (added from catalog, default-model={default_model or '-'}"
        ]
        if reasoning_effort:
            status_parts.append(f"reasoning={reasoning_effort}")
        status_parts.append(")")
        self._spinner_status(" · ".join(status_parts))

    def _open_custom_add_wizard(self) -> None:
        """The six-prompt hand-added-provider wizard (name → … → reasoning)."""

        state: dict[str, Any] = {}

        def ask_name() -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                name = catalog_api.slugify(result)
                if not name:
                    self._spinner_status("name required; wizard cancelled.")
                    return
                if provider_store.get(name) is not None:
                    self._spinner_status(f"Provider {name!r} already exists.")
                    return
                state["name"] = name
                ask_format()

            self.push_screen(
                TextInputModal(
                    "Custom provider: name (lowercase id)",
                    placeholder="my-ollama",
                    hint="Enter to continue · Esc to cancel",
                ),
                on_close,
            )

        def ask_format() -> None:
            def on_close(result: str | None) -> None:
                if result is None or result not in SUPPORTED_FORMATS:
                    return
                state["api_format"] = result
                ask_url()

            self.push_screen(
                SelectionModal(
                    "Custom provider: api_format",
                    list(SUPPORTED_FORMATS),
                    current=state.get("api_format", "openai"),
                    allow_none=False,
                ),
                on_close,
            )

        def ask_url() -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                state["base_url"] = result.strip()
                ask_key()

            self.push_screen(
                TextInputModal(
                    "Custom provider: base URL (optional)",
                    placeholder="https://api.example.com/v1",
                    hint="Empty to skip · Enter to continue · Esc to cancel",
                ),
                on_close,
            )

        def ask_key() -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                state["api_key"] = result.strip()
                ask_model()

            self.push_screen(
                TextInputModal(
                    "Custom provider: api key (optional)",
                    placeholder="sk-…",
                    hint="Empty to skip · Enter to continue · Esc to cancel",
                ),
                on_close,
            )

        def ask_model() -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                ask_max_context(result.strip())

            self.push_screen(
                TextInputModal(
                    "Custom provider: default model (optional)",
                    placeholder="model-id",
                    hint="Empty to set later · Enter to continue · Esc to cancel",
                ),
                on_close,
            )

        def ask_max_context(model: str) -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                try:
                    parsed = _parse_optional_int(result)
                except ValueError:
                    self._spinner_status("invalid max context window; skipping")
                    parsed = None
                ask_max_tokens(model, parsed)

            self.push_screen(
                TextInputModal(
                    "Custom provider: max context window (optional)",
                    placeholder="e.g. 128000 · empty to skip",
                    hint="Enter to continue · Esc to cancel",
                ),
                on_close,
            )

        def ask_max_tokens(model: str, ctx: int | None) -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                try:
                    parsed = _parse_optional_int(result)
                except ValueError:
                    self._spinner_status("invalid max output tokens; skipping")
                    parsed = None
                ask_reasoning(model, ctx, parsed)

            self.push_screen(
                TextInputModal(
                    "Custom provider: max output tokens (optional)",
                    placeholder="e.g. 16384 · empty to skip",
                    hint="Enter to save · Esc to cancel",
                ),
                on_close,
            )

        def ask_reasoning(model: str, ctx: int | None, tokens: int | None) -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                stored = result.strip() or None
                state["reasoning"] = stored
                paths.ensure_dirs()
                provider_store.upsert(
                    config_module._construct_provider_with_reasoning(
                        name=state["name"],
                        api_format=state["api_format"],
                        base_url=state["base_url"],
                        api_key=state["api_key"],
                        default_model=model,
                        source=SOURCE_CUSTOM,
                        max_context_window=ctx,
                        max_output_tokens=tokens,
                        reasoning_effort=stored,
                    )
                )
                self._apply_provider(state["name"])
                status = f"provider → {state['name']} (custom)"
                if stored:
                    status = f"{status} · reasoning={stored}"
                self._spinner_status(status)

            self.push_screen(
                TextInputModal(
                    "Custom provider: reasoning effort (optional)",
                    placeholder="optional · e.g. medium / on · empty to skip",
                    hint="Enter to save · Esc to cancel",
                ),
                on_close,
            )

        ask_name()

    def _open_style_picker(self) -> None:
        styles = style_store.list_styles()
        if not styles:
            self._spinner_status("No styles installed. Run `miniouto style add <repo>`.")
            return
        # The style shown is the one THIS session resolves: its own pin, else the
        # global default. `_config_session` (not `_pinned_session`): adopting a
        # session as a side effect of opening a picker would be wrong.
        session = self._config_session()
        current = (session_config.read(session) or session_config.EMPTY).style
        current = current or settings_store.load().style

        def on_close(result: str | None) -> None:
            if not result:
                return
            # The choice becomes the DEFAULT and every session that already
            # exists freezes the style it is running under.
            runtime.set_active_style(result, session=session)
            self._refresh_chips()
            self._spinner_status(f"style → {result}")

        self.push_screen(
            SelectionModal("Select style", styles, current=current, allow_none=False),
            on_close,
        )

    def _open_session_picker(self) -> None:
        here = str(paths_runtime.INVOCATION_CWD)
        this_dir: list[tuple[str, str]] = []
        other_dir: list[tuple[str, str]] = []
        for name, cwd, _mtime in session_store.list_sessions_with_meta():
            if cwd == here:
                this_dir.append((name, name))
            else:
                label = f"[{Path(cwd).name or cwd}] {name}" if cwd else f"[?] {name}"
                other_dir.append((name, label))
        current = self._session_name if self._chat_started else ""

        def on_close(result: str | None) -> None:
            if result is None:
                return
            if result == "__new__":
                self._new_session()
                return
            self._pin_session(result)
            self._load_session_history(result)
            self._refresh_chips()

        sections: list[tuple[str, list[tuple[str, str]]]] = []
        if this_dir:
            sections.append(("This directory", this_dir))
        if other_dir:
            sections.append(("Other directories", other_dir))
        self.push_screen(
            SelectionModal(
                "Select session",
                [],
                current=current,
                allow_none=False,
                extra_options=[("__new__", "+ new session…")],
                sections=sections,
            ),
            on_close,
        )

    # ── chat flow ─────────────────────────────────────────────────────────
    def on_chat_input_submitted(self, event: Any) -> None:
        text = str(getattr(event, "value", "")).strip()
        if not text:
            return
        self._input.text = ""
        if self._busy:
            self._queue_inbox_message(text)
            return
        if self._logo_shown:
            self._log.remove_children()
            self._reset_rows()
            self._logo_shown = False
        self._post_user(text)
        # Re-engage the follow anchor: submitting a new prompt always jumps to
        # the tail, even if the user was reading history.
        self._log.anchor()
        self.run_worker(self._dispatch(text), exclusive=True)

    def _queue_inbox_message(self, text: str) -> None:
        """Hand typed text to the running turn (mid-turn interjection).

        Writing it to the inbox makes it an ordinary user message for the model:
        the core's BEFORE_LLM_CALL hook drains it into the live history at the
        next iteration, exactly like an alarm. The row is rendered here for
        immediacy and remembered by id — when the real event arrives
        ``_add_event_row`` suppresses its row, so the text is shown once.
        """

        try:
            message_id = inbox.insert(
                self._inbox_session(), text, kind=inbox.USER_KIND, source="tui"
            )
        except (OSError, ValueError) as exc:
            self._post_system(f"could not queue message: {exc}")
            return
        self._optimistic_rows[message_id] = self._user_row(text)
        self._mount_row(self._optimistic_rows[message_id])
        self._spinner_status("queued for the running turn")

    def _claim_inbox_event(self, event: LoopEvent) -> bool:
        """False when this event's row is already on screen (optimistic row)."""

        detail = event.detail or ""
        if not detail.startswith(_INBOX_DETAIL_PREFIX):
            return True
        message_id = detail[len(_INBOX_DETAIL_PREFIX) :]
        if message_id not in self._optimistic_rows:
            return True
        self._optimistic_rows.pop(message_id)
        return False

    def _deliver_pending_messages(self) -> None:
        """Turn queued inbox messages into a real turn (UI thread).

        The in-turn path (``runtime._make_alarm_hook``, on BEFORE_LLM_CALL) is
        the only delivery a RUNNING turn needs — it wins while ``_busy``, so it
        owns the queue and this must not double-deliver. Between turns no hook
        runs and the message would sit invisible until the user typed something;
        instead the text becomes the prompt of a turn, so ``turn.user``, the
        chat row and the model's own view are the same string by construction.
        Several messages coalesce into one prompt and one turn.
        """

        if self._busy:
            return
        try:
            messages = inbox.drain(self._inbox_session())
        except (OSError, ValueError):
            return
        if not messages:
            return
        text = "\n\n".join(m.text for m in messages)
        for message in messages:
            # A message inserted mid-turn was already shown where the user typed
            # it; retire that row so the prompt row below replaces it.
            stale = self._optimistic_rows.pop(message.id, None)
            if stale is not None:
                self._window.remove(stale, self._log)
        if self._logo_shown:
            self._log.remove_children()
            self._reset_rows()
            self._logo_shown = False
        self._post_user(text)
        self._log.anchor()
        self.run_worker(self._dispatch(text), exclusive=True)

    def _apply_handoff(self) -> None:
        """Move the conversation into the session a summary hand-off opened.

        The OLD conversation visibly ENDS with the marker (and keeps it on
        disk), then the pin moves — which is what makes the
        ``_deliver_pending_messages`` call after this drain the NEW session's
        inbox, where the continuation message already is.
        """

        handoff = _summary_api().consume_handoff()
        if handoff is None:
            return
        previous = self._pinned_session()
        self._mount_row(EventRow(Text(handoff_marker(handoff))))
        _persist_handoff_marker(previous, handoff)
        self._pin_session(handoff.new_session)
        self._refresh_chips()
        self._spinner_status(f"handoff → {handoff.new_session}")

    async def _dispatch(self, prompt: str) -> None:
        """Run one turn (invariant 10: the sync driver goes to a thread)."""

        first_message = not self._chat_started
        self._chat_started = True
        self._busy = True
        # A previous turn that died mid-stream can leave live rows behind.
        self._finalize_stream()
        if first_message and not self._session_assigned:
            # Gated on `_session_assigned`, NOT on `_session_name is None`: the
            # one-shot adoption in `_pinned_session` sets the name alone, and
            # this TUI must still mint its own `tui-…` session over it.
            self._adopt_minted_session()
            self._refresh_chips()
        s = settings_store.load()
        # A TUI run is always plain, so identity is all it registers.
        _summary_api().set_run_contract(session=self._pinned_session(), style=s.style or "")
        sink = TUIEventSink(self)
        self._cancel_event = threading.Event()
        self._last_escape = 0.0
        try:
            opts = ChatOptions(
                prompt=prompt,
                session=self._pinned_session(),
                # NO model override: the session's PIN already carries the trio
                # the settings popup wrote. An override here would outrank the pin
                # and let a global `settings.model` from another process redirect
                # a live conversation — the exact thing the pin exists to stop.
                continue_session=True,
                cancel_event=self._cancel_event,
                # This TUI owns the terminal alternate screen: a failure
                # traceback written to stderr would paint over the frame being
                # composed. Diagnostics go to the failure log instead; the
                # "[error: …]" row posted below is the user-facing surface.
                quiet_diagnostics=True,
            )
            await asyncio.to_thread(_chat_runner(), opts, sink)
        except asyncio.CancelledError:
            self._cancel_event = None
            self._busy = False
            raise
        except LoopCancelledError:
            self._post_system("stopped by user (esc esc)")
        except Exception as exc:
            self._post_system(f"error: {exc}")
        self._cancel_event = None
        self._busy = False
        self._esc_hint = None
        self._refresh_chips()
        # A summary hand-off may have ended that turn; the session moves first so
        # the delivery below reads the NEW session's inbox.
        self._apply_handoff()
        # A message can land while the final answer is already streaming, after
        # the last BEFORE_LLM_CALL — nothing else will consume it.
        self._deliver_pending_messages()

    async def _check_for_updates(self) -> None:
        """Check PyPI without delaying startup or interrupting a chat worker."""

        latest = await asyncio.to_thread(update_check.latest_pypi_version)
        notice = update_check.update_notice(update_check.current_version(), latest)
        if notice is not None:
            self._update_notice = notice
            self._post_system(notice)

    # ── composition ───────────────────────────────────────────────────────
    def layout(self, width: int, height: int) -> tuple[Rect, Rect, Rect, int]:
        """``(log, input, panel, column_x)`` for a ``width``x``height`` screen.

        Header / main (log + input) / panel — no Footer: the owner dropped the
        Textual key bar. The chat column is ``min(width, CHAT_MAX_WIDTH)`` and
        centred; the header and the panel's BACKGROUND span the terminal, while
        the panel's CONTENT stops at the column so the chips line up with the
        log and the input.
        """

        column = max(1, min(width, CHAT_MAX_WIDTH))
        column_x = max(0, (width - column) // 2)
        input_height = self._input.height(column)
        panel_height = len(BottomPanel.ROWS)
        log_height = max(1, height - 1 - input_height - panel_height)
        log = Rect(column_x, 1, column, log_height)
        input_rect = Rect(column_x, 1 + log_height, column, input_height)
        panel = Rect(0, height - panel_height, width, panel_height)
        return log, input_rect, panel, column_x

    def compose(self, width: int, height: int) -> list[list[tuple[str, pt.Style]]]:
        """Build one whole frame and paint it into the compositor.

        This is the ONLY place a frame is assembled: the header, the windowed
        log, the input band, the bottom panel, the jump button and — when a modal
        is on top — that modal over a dimmed copy of everything below it. The
        console reads ``render_strips()`` off the compositor, so this is also
        what a test asserts against.
        """

        width = max(1, int(width))
        height = max(1, int(height))
        self.size = Size(width, height)
        theme = self.current_theme
        self._base_screen.size = Size(width, height)
        background = pt.Style().bg(theme.color(theme.background))
        lines: list[list[tuple[str, pt.Style]]] = [
            [(" " * width, background)] for _ in range(height)
        ]

        # The image placements belong to ONE frame: the rows' rects are blanked
        # below and the pixels are emitted after the draw.
        self._overlay = graphics_module.GraphicsOverlay()

        # header (full width)
        self._header.title = self.title
        self._header.sub_title = self.sub_title
        lines[0] = _fit_line(self._header.build_lines(width, theme)[0], width, background)

        log_rect, input_rect, panel_rect, column_x = self.layout(width, height)
        self._paint_log(lines, log_rect, theme)
        self._paint_input(lines, input_rect, theme)
        self._paint_panel(lines, panel_rect, column_x, theme)
        self._paint_jump(lines, log_rect, column_x, theme)

        if isinstance(self.screen, Modal):
            modal = self.screen
            modal.size = Size(width, height)
            over = modal.build_lines(Rect(0, 0, width, height), theme, behind=lines)
            lines = [list(line) for line in over]

        compositor = self._base_screen._compositor
        compositor.resize(width, height)
        compositor.clear()
        compositor.paint(lines)
        self.screen._compositor_cache = compositor
        return lines

    def _paint_log(
        self, lines: list[list[tuple[str, pt.Style]]], rect: Rect, theme: TuiTheme
    ) -> None:
        log = self._log
        log.place(rect.x, rect.y, rect.width, rect.height)
        log.resize(rect.width, rect.height)
        log.theme = theme
        self._log_rect = rect
        self._row_origins.clear()

        heights: list[tuple[Any, int, int]] = []
        offset = 0
        for row in log.rows:
            height = max(1, log._row_height(row))
            heights.append((row, offset, height))
            offset += height
        log.content_height = offset
        log.check_anchor()
        top = int(log.scroll_y)

        background = pt.Style().bg(theme.color(theme.background))
        for row, row_offset, row_height in heights:
            absolute = rect.y + row_offset - top
            row.place(rect.x, absolute, rect.width, row_height)
            self._row_origins[row] = row.region
            if absolute + row_height <= rect.y or absolute >= rect.y + rect.height:
                continue
            selection = self.screen.selections.get(row)
            row.selection = selection
            painted = row.build_lines(rect.width, theme, selection)
            first = max(0, rect.y - absolute)
            last = min(len(painted), rect.height - (absolute - rect.y))
            for index in range(first, last):
                # `absolute` is a FRAME row: the log rect starts at rect.y, and
                # the row's first painted line lands there, so the target is
                # absolute + index — not the row-relative offset, which wrote
                # one row too high (a negative index wrapped to the LAST line).
                target = absolute + index
                if 0 <= target < len(lines):
                    lines[target] = _overlay_line(lines[target], rect.x, painted[index], background)
            self._paint_images(row, absolute, rect, lines, background)

    def _paint_images(
        self,
        row: Any,
        absolute: int,
        rect: Rect,
        lines: list[list[tuple[str, pt.Style]]],
        background: pt.Style,
    ) -> None:
        """Reserve and register a row's image lane (blank here, pixels later)."""

        for line_index, x_offset, placement in _placements(row):
            width = int(getattr(placement, "width_cells", 0) or 0)
            height = int(getattr(placement, "height_cells", 0) or 0)
            # `top` is a FRAME row (what `GraphicsOverlay.add` emits at), so the
            # blanking uses the same coordinate: subtracting rect.y shifted the
            # reserved rect one row up, painting log text over the image.
            top = absolute + line_index
            # Only the part inside the log viewport is blanked: `top` is a FRAME
            # row, so a lane scrolled above or below the rect would otherwise
            # erase the header, the input or the panel.
            for blank in range(height):
                row_index = top + blank
                if rect.y <= row_index < rect.y + rect.height:
                    lines[row_index] = _place_blank(
                        lines[row_index], rect.x + x_offset, width, background
                    )
            self._overlay.add(placement, rect.x + x_offset, top)

    def _paint_input(
        self, lines: list[list[tuple[str, pt.Style]]], rect: Rect, theme: TuiTheme
    ) -> None:
        painted = self._input.build_lines(rect.width, theme)
        self._input.place(rect.x, rect.y, rect.width, len(painted))
        background = pt.Style().bg(theme.color(theme.background))
        for index, line in enumerate(painted[: rect.height]):
            lines[rect.y + index] = _overlay_line(lines[rect.y + index], rect.x, line, background)

    def _paint_panel(
        self,
        lines: list[list[tuple[str, pt.Style]]],
        rect: Rect,
        column_x: int,
        theme: TuiTheme,
    ) -> None:
        column = min(rect.width, CHAT_MAX_WIDTH)
        painted = self._panel.build_lines(column, theme)
        # `build_lines` lays the rows and chips out relative to the panel; the
        # panel itself is placed in screen cells. A node's region is an ABSOLUTE
        # cell rect (that is what `pilot.click` and `Node.region.contains` read),
        # so the subtree is shifted onto the panel's origin here — without this
        # a chip's region said (0, 1) while the panel sat at row 28, and every
        # chip click landed on the log instead of the chip.
        self._panel.translate(column_x, rect.y)
        self._panel.place(column_x, rect.y, column, len(painted))
        background = pt.Style().bg(theme.color(theme.background))
        for index, line in enumerate(painted):
            row = rect.y + index
            if not 0 <= row < len(lines):
                continue
            lines[row] = _overlay_line(lines[row], column_x, line, background)

    def _paint_jump(
        self,
        lines: list[list[tuple[str, pt.Style]]],
        log_rect: Rect,
        column_x: int,
        theme: TuiTheme,
    ) -> None:
        self._jump.sync()
        if not self._jump.display:
            self._jump.place(0, 0, 0, 0)
            return
        width = min(self._jump.width_cells, log_rect.width)
        x = column_x + log_rect.width - width
        # `margin: 0 0 1 0` — one row of air between the button and the input.
        y = max(log_rect.y, log_rect.y + log_rect.height - 2)
        painted = self._jump.build_lines(width, theme)
        self._jump.place(x, y, width, len(painted))
        background = pt.Style().bg(theme.color(theme.background))
        for index, line in enumerate(painted):
            row = y + index
            if not 0 <= row < len(lines):
                continue
            lines[row] = _overlay_line(lines[row], x, line, background)

    # ── the render entry points ───────────────────────────────────────────
    def render_frame(self, width: int, height: int) -> list[list[tuple[str, pt.Style]]]:
        """The frame as styled lines (the ``.render_to_buffer`` equivalent)."""

        return self.compose(width, height)

    def render_to_buffer(self) -> list[list[tuple[str, pt.Style]]]:
        return self.compose(self.size.width, self.size.height)

    def render(self, frame: Any) -> None:
        """The ``Terminal.draw`` callback: compose, then paint one row at a time.

        pyratatui gives no buffer access, so a row is a 1-high ``Paragraph`` of
        pre-wrapped ``Line``s — the heights are exact because the wrap engine
        built them. The image rects were blanked in ``compose``; their pixels are
        emitted AFTER the draw (``GraphicsOverlay``), which is what keeps the
        diff from painting text over the picture.
        """

        area = frame.area
        for index, line in enumerate(self.compose(area.width, area.height)):
            frame.render_widget(
                pt.Paragraph(pt_style_text([line])),
                pt.Rect(0, index, area.width, 1),
            )

    def _draw_lines(self, lines: list[list[tuple[str, pt.Style]]]) -> Any:
        """A ``Terminal.draw`` callback that paints ALREADY-composed lines.

        The real loop composes once (``_paint_now``) and hands the result here,
        so a frame costs one compose instead of two. ``render`` stays the
        compose-and-paint entry point the headless path and the tests use.
        """

        def paint(frame: Any) -> None:
            area = frame.area
            for index, line in enumerate(lines):
                if index >= area.height:
                    break
                frame.render_widget(
                    pt.Paragraph(pt_style_text([line])),
                    pt.Rect(0, index, area.width, 1),
                )

        return paint

    def _publish_title(self) -> None:
        if self._terminal is None:
            return
        title = f"{self.title} — {self.sub_title}" if self.title else self.sub_title
        _write_terminal(_TITLE_OSC.format(title=title))

    def _sync_screen_background(self) -> None:
        """Tell the terminal what colour its ERASES paint — the theme background.

        xnano erases the whole display on every resize (``ESC[2J``), and it does
        it OUTSIDE its synchronized-output block: measured on a pty over six
        resizes, eight ``ESC[2J`` and all eight ahead of the ``ESC[?2026h``. An
        erase paints immediately, so it landed on the terminal's DEFAULT
        background and the window flashed the wrong colour until the themed
        frame followed — the owner's "the screen feels unstable".

        ``pt.Terminal`` turns that clear into a themed one by putting the theme
        background on the wire just before it (``set_screen_background``, and
        the resize guard that re-asserts it), which is why this reads the palette
        rather than composing anything. Called at boot and from `watch_theme`,
        so the erase colour always matches the theme on screen.

        A terminal double with no such method (the loop tests drive a fake) is
        left alone: there is no real screen to protect.
        """

        setter = getattr(self._terminal, "set_screen_background", None)
        if callable(setter):
            with contextlib.suppress(Exception):
                setter(self.current_theme.background)

    def _cursor_position(self) -> tuple[int, int] | None:
        """The input caret in screen cells, or None when it is not visible."""

        _log_rect, input_rect, _panel, _x = self.layout(self.size.width, self.size.height)
        if self._focused is not self._input:
            return None
        cell = self._input.cursor_cell(input_rect.width)
        if cell is None:
            return None
        return (input_rect.x + cell[0], input_rect.y + cell[1])

    # ── the real event loop ───────────────────────────────────────────────
    async def run_async(self) -> None:
        """Own the terminal, its input hooks and the draw loop until exit.

        The loop SHAPE is the PyRatatui one and stays that way — drain events,
        drain posted messages, poll the size, tick, draw, overlay, place the
        caret, sleep — because the state machine below (rows, timers, the sink's
        ``call_from_thread`` hops, the handoff order) is what the suite pins.
        What changed is who owns the terminal:

        * **Screen.** ``pt.Terminal`` opens a real xnano canvas on the terminal
          (``?1049h``) and closes it on the way out (``?1049l``); xnano runs the
          frame loop that blits it.
        * **Input.** xnano owns stdin, so the old ``InputReader``/``InputParser``
          pair cannot coexist with it — they would block on a descriptor that
          never delivers. Instead one hook per kind is registered BEFORE the
          canvas opens (that is what makes xnano turn mouse/scroll/paste on),
          and each hook translates its xnano reading into our own
          ``Key``/``Mouse``/``Paste``/``Resize`` and hops it onto THIS loop
          thread, so every handler still runs where it always ran.
        * **Esc.** The lone-ESC ``flush_timeout`` path is gone: xnano
          disambiguates a bare Escape from a sequence start itself, so an Esc
          now arrives as its own key event instead of waiting for the next
          keystroke to prove it was not a prefix.

        ``_drain_messages()`` and the ``TIOCGWINSZ`` poll both STAY.
        """

        self._bind_loop()
        self._register_terminal_hooks()
        self._running = True
        previous_sigint: Any = None
        try:
            # The Terminal context owns the ALT SCREEN pair: `__enter__` opens
            # the xnano canvas on the terminal and `__exit__` closes it
            # (verified with a pty probe: exactly one ?1049h and one ?1049l).
            # Nothing here emits either code, and `run_tui`'s finally only does
            # when the context never opened.
            with self._terminal as term:
                # `Terminal.__enter__` opened the screen, so its `__exit__` owns
                # the matching leave. `run_tui`'s finally then has nothing to do.
                self._alt_screen_entered = True
                # Mouse/paste/scroll reporting is xnano's: it turned them on at
                # open because the hooks above were registered first. The old
                # manual `MOUSE_PASTE_ON` write is gone with the reader.
                self._boot(self._terminal_size())
                with contextlib.suppress(ValueError, OSError):
                    previous_sigint = signal.signal(signal.SIGINT, self._on_sigint)
                try:
                    while self._running:
                        # Drain the posted-message queue: a key handler that
                        # posts a message (Enter -> ChatInput.Submitted, a click
                        # -> a chip's ChipClicked) only APPENDS to `_messages`,
                        # so without this the real terminal never dispatched
                        # anything the headless `pilot` path handled for free.
                        self._drain_messages()
                        # Resize is polled as well as hooked: xnano reports a
                        # size change, but the app's geometry is also the thing
                        # `_cursor_position` reads, so the cheap ioctl keeps the
                        # two honest even if a resize report is coalesced away.
                        self._sync_terminal_size()
                        self.tick(_REDRAW_SECONDS)
                        # DRAW ONLY WHEN SOMETHING CHANGED. Every mutation sets
                        # `_dirty` (a key/mouse handler, a fired timer, a queued
                        # message, a worker hop, a tree edit), so a tick that
                        # changed nothing has a frame on screen that already
                        # matches the state. Composing again there was the whole
                        # cost of an idle tick: 42% of one core at 60 fps for
                        # output the terminal already had, and it raced xnano's
                        # own frame loop, which is what the owner felt as stutter.
                        lines = self._paint_now()
                        if lines is not None:
                            term.draw(self._draw_lines(lines))
                            self._emit_overlay()
                            # The caret is placed WITH the frame it belongs to,
                            # one placement per painted frame: xnano re-asserts
                            # its own cursor on every frame it draws, so a
                            # placement on a skipped tick would be a no-op at
                            # best and a second xnano frame at worst.
                            self._position_cursor(term)
                        await asyncio.sleep(_REDRAW_SECONDS)
                finally:
                    # The ONE owner of the caret restore: this runs INSIDE the
                    # Terminal context, so it lands while the alt screen is
                    # still up and `__exit__` then leaves it. `_position_cursor`
                    # hides the caret on every frame that has no caret to place
                    # (focus off the input, a modal up), and nothing else ever
                    # undid that.
                    self._running = False
                    if previous_sigint is not None:
                        with contextlib.suppress(ValueError, OSError):
                            signal.signal(signal.SIGINT, previous_sigint)
                    with contextlib.suppress(Exception):
                        term.show_cursor()
        finally:
            self._running = False
            self._loop = None
            self._loop_thread = 0
            # `_alt_screen_entered` is NOT cleared here: `run_tui`'s finally reads
            # it after this coroutine returns, and clearing it would make that
            # finally emit a second ?1049l over the one `Terminal.__exit__` sent.

    # ── the input hooks (xnano owns stdin) ────────────────────────────────
    def _register_terminal_hooks(self) -> None:
        """Wire xnano's input hooks to the handlers this app already has.

        Registered on the TERMINAL (which defers them until it opens the
        canvas), because xnano decides which terminal settings to switch on from
        the hooks that exist when the canvas goes up — a mouse hook added after
        open is attached and never runs.

        Each callback does the same three things: translate the xnano reading
        into our own event, hop onto the loop thread, and call the handler the
        headless ``Pilot`` calls. The hop matters — xnano runs hooks on its own
        thread, and every handler here assumes the loop thread (``compose``
        mutates the node tree the loop is painting).
        """

        term = self._terminal
        register = getattr(term, "hook", None)
        if register is None:  # a Terminal double with no input at all
            return
        loop = self._loop
        mine = self._loop_thread

        def hop(callback: Callable[..., None], *args: Any) -> None:
            """Run ``callback`` on the loop thread, or inline when already there."""

            # Every hook is INPUT, so whatever it does the frame on screen is
            # stale by the time it returns. Marking it here (rather than in each
            # handler) is what makes a keystroke paint on the NEXT loop tick
            # instead of waiting for some unrelated timer to dirty the frame:
            # measured on a pty, the wait was 33 ms of extra echo latency.
            self._invalidate()
            if loop is not None and mine and threading.get_ident() != mine:
                with contextlib.suppress(RuntimeError):  # loop already closed
                    loop.call_soon_threadsafe(callback, *args)
                return
            callback(*args)

        def on_key(context: Any) -> None:
            keyboard = context.keyboard
            key = key_from_xnano(
                keyboard.key, keyboard.kind, keyboard.modifiers, keyboard.text
            )
            if key is not None and self._running:
                hop(self.handle_key, key)

        def on_mouse(context: Any) -> None:
            # The wheel arrives on its own hook, so `mouse.kind` here is a
            # button/drag/move report and `position` is its cell.
            pointer = context.mouse.position
            if pointer is None or not self._running:
                return
            mouse = mouse_from_xnano(
                context.mouse.kind, pointer.x, pointer.y, context.mouse.button
            )
            if mouse is not None:
                hop(self._handle_event, mouse)

        def on_scroll(context: Any) -> None:
            if not self._running:
                return
            x, y = context.mouse.pointer or (0, 0)
            mouse = scroll_from_xnano(context.mouse.scroll.y, x, y)
            if mouse is not None:
                hop(self._handle_event, mouse)

        def on_paste(context: Any) -> None:
            text = context.keyboard.pasted
            if text and self._running:
                hop(self._handle_event, Paste(text))

        def on_resize(context: Any) -> None:
            size = context.size
            if self._running:
                hop(self._handle_event, Resize(size.width, size.height))

        register("key", on_key)
        register("mouse", on_mouse)
        register("scroll", on_scroll)
        register("paste", on_paste)
        register("resize", on_resize)

    def _terminal_size(self) -> tuple[int, int]:
        """The size the frame starts at: the terminal's own, else the polled one.

        On the real screen xnano has already measured the window, so its canvas
        is the better answer than a ``TIOCGWINSZ`` that can be a stale fallback
        (a fresh pty reports 0x0 until the first winsize arrives). A headless
        ``Terminal`` was never opened, so its boot size is all there is.
        """

        area = getattr(self._terminal, "area", None)
        if callable(area):
            with contextlib.suppress(Exception):
                rect = area()
                if rect.width > 1 and rect.height > 1:
                    return (int(rect.width), int(rect.height))
        return terminal_size()

    def _emit_overlay(self) -> None:
        """Paint this frame's images — if the graphics lane can still do it.

        The image lane is being ported to ``xnano_image`` in its own slice, so
        this call has to be unable to bite: an overlay that raises (a
        ``graphics`` module mid-rewrite, a terminal that lost its protocol)
        must cost the PICTURE, never the frame or the boot. The graphics
        module already swallows its own write failures; this is the belt to
        that braces, and it is why the loop can keep the call unconditionally.
        """

        emit = getattr(self._overlay, "emit_post_draw", None)
        if emit is None:
            return
        with contextlib.suppress(Exception):
            emit()

    def _sync_terminal_size(self) -> None:
        """Poll ``TIOCGWINSZ`` and repaint when it moved.

        ``terminal_size()`` is one cheap ioctl, so the loop calls this every
        tick; a size that did not change costs nothing. A change goes through the
        ordinary :class:`Resize` path, so the frame and ``self.size`` (which the
        caret is placed from) move together.

        Two guards, both learned on a pty. The ioctl can FAIL outright — a
        redirected stdout, or a stdout xnano has swapped for a capture pipe —
        and ``terminal_size()`` answers its 80x24 fallback there; taking that
        as a real size would repaint the whole app into a corner, so a poll
        that disagrees with the LIVE CANVAS is not believed. And xnano reports
        resizes through its own hook, so this poll is the backstop, not the
        primary signal (a bare ``Resize`` with no geometry change is cheap: the
        handler just recomposes).
        """

        width, height = terminal_size()
        if (width, height) == (self.size.width, self.size.height):
            return
        area = getattr(self._terminal, "area", None)
        if callable(area):
            with contextlib.suppress(Exception):
                rect = area()
                if (rect.width, rect.height) != (width, height):
                    # The canvas is the better witness: a captured fd answers
                    # the 80x24 fallback, not the window's real size.
                    return
        self._handle_event(Resize(width, height))

    def _on_sigint(self, signum: int, frame: Any) -> None:
        """Ctrl-C: stop the loop; the ``finally`` above restores the terminal."""

        self.exit()

    def _handle_event(self, event: Any) -> None:
        """One decoded input event, on the loop thread."""

        # ONE choke point for the real loop: whatever the event was, it was
        # input, so the frame it produces is stale until the next draw.
        self._invalidate()
        if isinstance(event, Key):
            self.handle_key(event)
        elif isinstance(event, Mouse):
            if event.kind in ("wheel_up", "wheel_down"):
                self._route_wheel(event)
            else:
                self._route_mouse(event)
        elif isinstance(event, Paste):
            self.inject_paste(event.text)
        elif isinstance(event, Resize):
            # `compose` used to run here, and then the loop composed again for
            # the draw — two full frames per resize report. The geometry moves
            # here and the loop's single `_paint_now()` paints it.
            self.size = Size(event.width, event.height)

    def _position_cursor(self, term: Any) -> None:
        """Place the hardware caret after the draw (``draw`` force-hides it).

        The caret must go through the TERMINAL when the terminal can take one.
        On the real screen xnano re-asserts its own cursor on every frame it
        draws, so a raw ``ESC[row;colH`` written to fd 1 is overwritten a few
        milliseconds later — measured on a pty: the move landed, the next frame
        hid the caret again. ``move_cursor`` is the shim's xnano-backed
        placement, and it is what makes the caret sit in the input band and
        stay there.

        The escape write is kept for a terminal double that has no cursor API
        (the loop tests drive a fake), so both shapes keep working.
        """

        cell = self._cursor_position()
        if cell is None:
            with contextlib.suppress(Exception):
                term.hide_cursor()
            return
        move = getattr(term, "move_cursor", None)
        if callable(move):
            with contextlib.suppress(Exception):
                move(cell[0], cell[1])
            with contextlib.suppress(Exception):
                term.show_cursor()
            return
        with contextlib.suppress(Exception):
            term.show_cursor()
        _write_terminal(_CURSOR_HOME.format(row=cell[1] + 1, col=cell[0] + 1))


# ─── line helpers ────────────────────────────────────────────────────────────


def _fit_line(
    line: Sequence[tuple[str, pt.Style]], width: int, fill: pt.Style
) -> list[tuple[str, pt.Style]]:
    """Clip a composed line to ``width`` cells and pad it out with ``fill``."""

    if width <= 0:
        return []
    out: list[tuple[str, pt.Style]] = []
    used = 0
    for text, style in line:
        if used >= width or not text:
            continue
        remaining = width - used
        if cell_len(text) <= remaining:
            out.append((text, style))
            used += cell_len(text)
            continue
        taken = ""
        for char in text:
            char_cells = cell_len(char)
            if used + char_cells > width:
                break
            taken += char
            used += char_cells
        if taken:
            out.append((taken, style))
        break
    if used < width:
        out.append((" " * (width - used), fill))
    return out


def _overlay_line(
    base: Sequence[tuple[str, pt.Style]],
    x: int,
    pieces: Sequence[tuple[str, pt.Style]],
    fill: pt.Style,
) -> list[tuple[str, pt.Style]]:
    """Paint ``pieces`` over ``base`` starting at cell ``x`` (cells are replaced).

    The cells to the RIGHT of ``pieces`` are kept: the log, the input band and
    the panel are overlays onto a full-width frame, so dropping the tail would
    erase everything right of the centered column (on a 200-column terminal the
    panel's right-hand chip cells lost their background and painted the
    terminal default instead of ``theme.background``).
    """

    if x < 0:  # a right-anchored box can hang off a narrow terminal
        pieces = _slice_from(pieces, -x)
        x = 0
    head = pad_to(truncate_to(list(base), x), x, fill)
    return [*head, *pieces, *_slice_from(base, x + cells_of(pieces))]


def _place_blank(
    line: Sequence[tuple[str, pt.Style]], x: int, width: int, fill: pt.Style
) -> list[tuple[str, pt.Style]]:
    """Overwrite ``width`` cells starting at ``x`` with blanks (an image rect)."""

    if width <= 0:
        return list(line)
    head = pad_to(truncate_to(list(line), max(0, x)), max(0, x), fill)
    return [*head, (" " * width, fill), *_slice_from(line, x + width)]


def _slice_from(
    line: Sequence[tuple[str, pt.Style]], start: int
) -> list[tuple[str, pt.Style]]:
    """The cells of ``line`` from ``start`` on (cell-accurate)."""
    if start <= 0:
        return list(line)
    out: list[tuple[str, pt.Style]] = []
    position = 0
    for text, style in line:
        if position >= start:
            out.append((text, style))
            position += cell_len(text)
            continue
        chunk = ""
        for char in text:
            if position >= start:
                chunk += char
            position += cell_len(char)
        if chunk:
            out.append((chunk, style))
    return out


def _write_terminal(payload: str) -> None:
    """Write raw bytes to the terminal (fd 1), never raising into the loop."""

    with contextlib.suppress(OSError, ValueError):
        os.write(1, payload.encode("utf-8", "replace"))






_GRAPHICS_RESULT_MARK = b"\x00MINIOUTO-GRAPHICS\x00"
"""The tag the forked probe prefixes its answer with (see below)."""

_GRAPHICS_PROBE_TIMEOUT = 12.0
"""Seconds to wait for the isolated probe (its own terminal query allows 2)."""


def _detect_graphics_mode() -> str:
    """Probe the terminal for a graphics protocol, in an ISOLATED child.

    The probe has to ask the terminal a question and read the answer off stdin,
    which means putting the tty into cbreak for the round trip. Doing that in
    THIS process poisons xnano's input reader: measured on a pty, the reader
    then delivers every key with the kernel back in canonical mode, so ``\r``
    arrives as ``Ctrl+J`` and Enter never reaches the app — while every
    escape-sequence assertion still passes, because the bytes themselves are
    fine. Restoring the modes afterwards does not help; the reader's own saved
    copy is what is wrong.

    So the probe runs in a forked child. It inherits the terminal, asks its
    question, writes the answer down a pipe and exits; the tty modes it touched
    die with it, and this process never leaves canonical mode to begin with.
    That fixes the failure at the root instead of compensating for it, and it
    leaves ``graphics`` — the image lane's module, owned by other work —
    untouched.

    The answer is PUBLISHED to ``graphics`` so the render path reads the same
    cached mode it always has. A platform that cannot fork, a child that says
    nothing and a probe that raises all fall back to calling the module here,
    which is the pre-port behaviour and answers TEXT on any failure.
    """

    probe = package_seam("detect_graphics_mode", graphics_module.detect_graphics_mode)
    answer = _probe_in_child(probe)
    if answer is not None:
        with contextlib.suppress(Exception):
            graphics_module._GRAPHICS_MODE = answer
            return str(graphics_module.graphics_mode())
    # No fork, or a child that could not answer: probe the old way. Its own
    # `except` already turns a missing library / a non-tty into TEXT.
    with contextlib.suppress(Exception):
        return str(probe())
    return graphics_module.GRAPHICS_MODE_TEXT


def _probe_in_child(probe: Any) -> str | None:
    """Run ``probe()`` in a forked child; its answer, or ``None``."""

    try:
        read_fd, write_fd = os.pipe()
    except OSError:  # pragma: no cover - fd exhaustion
        return None
    try:
        pid = os.fork()
    except (AttributeError, OSError):  # pragma: no cover - no fork here
        os.close(read_fd)
        os.close(write_fd)
        return None
    if pid == 0:  # pragma: no cover - the child never returns
        status = 1
        try:
            os.close(read_fd)
            answer = str(probe()).encode("utf-8", "replace")
            os.write(write_fd, _GRAPHICS_RESULT_MARK + answer + b"\n")
            status = 0
        finally:
            with contextlib.suppress(OSError):
                os.close(write_fd)
            # `os._exit`, so no `finally`, buffer flush or atexit hook of the
            # parent's runs twice in this process.
            os._exit(status)
    os.close(write_fd)
    try:
        return _read_probe_answer(read_fd)
    finally:
        os.close(read_fd)
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, 0)


def _read_probe_answer(read_fd: int) -> str | None:
    """Read the child's tagged one-line answer off the pipe, or ``None``."""

    chunks: list[bytes] = []
    deadline = time.monotonic() + _GRAPHICS_PROBE_TIMEOUT
    while time.monotonic() < deadline:
        ready, _, _ = select.select([read_fd], [], [], 0.1)
        if not ready:
            continue
        try:
            data = os.read(read_fd, 4096)
        except OSError:
            return None
        if not data:
            break
        chunks.append(data)
        if b"\n" in data:
            break
    payload = b"".join(chunks)
    if not payload.startswith(_GRAPHICS_RESULT_MARK):
        return None
    return payload[len(_GRAPHICS_RESULT_MARK):].split(b"\n", 1)[0].decode("utf-8", "replace")



def _placements(row: Any) -> list[tuple[int, int, Any]]:
    """``(line, x, placement)`` for a row's image lane (empty for a plain row)."""

    getter = getattr(row, "image_placements", None)
    if getter is None:
        return []
    try:
        return list(getter())
    except Exception:  # a broken image must cost the image, never the frame
        return []


# ─── entry points ────────────────────────────────────────────────────────────


def run_tui() -> None:
    """Run the real TUI until the user quits.

    The graphics probe runs BEFORE the ``Terminal`` opens: it reads the
    terminal's reply on stdin, and the moment xnano's canvas goes up xnano owns
    stdin and that reply is never seen (invariant 15). The boot order is
    therefore probe, terminal, loop.

    The probe runs in a FORKED child (see :func:`_probe_graphics_isolated`).
    It has to: asking the terminal a question means reading the answer off
    stdin, which turns the tty into cbreak, and this build's probe leaves
    xnano's input reader believing the tty is still raw -- every key it then
    delivered arrived with the kernel canonical again, so Enter came through as
    Ctrl+J and submit was dead on a real terminal. A child process takes its tty
    modes with it when it exits, so the parent never leaves canonical mode.

    The answer is published back to the ``graphics`` module, so the render path
    sees exactly the mode it always read; a child that cannot be forked falls
    back to calling the module in-process, and any failure there is taken as
    "no graphics" rather than a failed boot.

    The terminal is ``pt.Terminal(real_screen=True)``: it opens an xnano canvas
    on the actual screen (alternate screen, mouse/scroll/paste reporting) and
    is also where ``run_async`` registers its input hooks.
    """

    _detect_graphics_mode()
    app = ChatTUI(terminal=pt.Terminal(real_screen=True))
    try:
        asyncio.run(app.run_async())
    except KeyboardInterrupt:  # pragma: no cover - the signal handler wins first
        pass
    finally:
        # `Terminal.__enter__` opens the alt screen and `__exit__` closes it
        # (verified on a pty: exactly one ?1049h and one ?1049l), so that pair
        # belongs to the context and this is the ONLY other writer — it covers
        # the paths where the context never opened at all (a failed boot, Ctrl-C
        # before the loop started), and stays silent otherwise so the leave is
        # never sent twice. The caret and the input reporting are restored by
        # `run_async`'s own `finally` INSIDE the context.
        if not app._alt_screen_entered:
            _write_terminal(_LEAVE_ALT_SCREEN)


def tui_summary() -> dict[str, Any]:
    """The TUI's resolved configuration (what ``miniouto status`` reports)."""

    s = settings_store.load()
    provider = provider_store.get(s.provider)
    return {
        "provider": s.provider,
        "model": s.model or (provider.default_model if provider else ""),
        "style": s.style,
        "session": s.session,
        "styles_available": style_store.list_styles(),
    }
