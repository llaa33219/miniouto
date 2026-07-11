"""Minimal TUI built on Textual.

Layout:
  ┌─────────────────────────────────────────────┐
  │ Header                                      │
  ├─────────────────────────────────────────────┤
  │ ChatLog (scrollable)                        │
  │ ...                                         │
  ├─────────────────────────────────────────────┤
  │ Input                                       │
  ├─────────────────────────────────────────────┤
  │ Working spinner (1 row)                     │
  │ model  provider             style           │   <- clickable chips
  │ session                                      │   <- muted, left-aligned
  │ Tab/click chips · Enter open · Esc cancel   │   <- help hint
  └─────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

from rich.markdown import Markdown
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.strip import Strip
from textual.timer import Timer
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    TextArea,
)

from ..core import lma as catalog_api
from ..core.chat import ChatOptions, run_chat
from ..core.events import LoopEvent
from ..core.providers import SUPPORTED_FORMATS, add_provider_from_lma, sdk_to_format
from ..storage import paths
from ..storage import providers as provider_store
from ..storage import sessions as session_store
from ..storage import settings as settings_store
from ..storage import styles as style_store
from ..storage.providers import SOURCE_CUSTOM, SOURCE_LMA
from ..storage.sessions import MessageRecord

SENTINEL_CATALOG_ADD = "__catalog_add__"
SENTINEL_CUSTOM_ADD = "__custom_add__"


def _parse_optional_int(result: str | None) -> int | None:
    """Parse a TextInputModal int result.

    Returns int for valid positive input, None for empty/cancelled (the
    caller treats None as "clear the override"). Raises ValueError for
    non-numeric, zero, or negative input so the caller can keep the
    existing value and post a system message.
    """
    if result is None:
        return None
    stripped = result.strip()
    if not stripped:
        return None
    v = int(stripped)
    if v <= 0:
        raise ValueError("must be > 0")
    return v

# Braille spinner frames. The status line reads e.g. "⠧ Write…" and the
# glyph rotates through this set at ~12.5fps so the bottom of the screen
# shows the agent is alive even between tool calls.
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_INTERVAL = 0.08
_SPINNER_DEFAULT_TEXT = "Working…"

# Logo: 180x36 packed bitmap from logo.png. _render_logo() scales to fit.
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
        [
            (_LOGO_BITS[y * row_bytes + x // 8] >> (7 - x % 8)) & 1
            for x in range(_LOGO_PIX_W)
        ]
        for y in range(_LOGO_PIX_H)
    ]


def _pixels_to_braille(grid: list[list[int]], w: int, h: int) -> str:
    rows = []
    for gy in range(0, h, 4):
        line = []
        for gx in range(0, w, 2):
            v = 0
            if grid[gy][gx]:
                v |= 0x01
            if gy + 1 < h and grid[gy + 1][gx]:
                v |= 0x02
            if gy + 2 < h and grid[gy + 2][gx]:
                v |= 0x04
            if grid[gy][gx + 1]:
                v |= 0x08
            if gy + 1 < h and grid[gy + 1][gx + 1]:
                v |= 0x10
            if gy + 2 < h and grid[gy + 2][gx + 1]:
                v |= 0x20
            if gy + 3 < h and grid[gy + 3][gx]:
                v |= 0x40
            if gy + 3 < h and grid[gy + 3][gx + 1]:
                v |= 0x80
            line.append(chr(0x2800 + v))
        rows.append("".join(line).rstrip("⠀"))
    while rows and not rows[0]:
        rows.pop(0)
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows)


def _render_logo(max_chars: int) -> str:
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
            src[min(int(py / scale), _LOGO_PIX_H - 1)]
            [min(int(px / scale), _LOGO_PIX_W - 1)]
            for px in range(target_w)
        ]
        for py in range(target_h)
    ]
    return _pixels_to_braille(scaled, target_w, target_h)

# ─── Clickable chip ──────────────────────────────────────────────────────────


class StatusChip(Static):
    """A focusable, clickable status chip that emits `ChipClicked`."""

    can_focus = True

    class ChipClicked(Message):
        """Posted when the chip is activated (click or Enter)."""

        def __init__(self, chip: StatusChip) -> None:
            super().__init__()
            self.chip = chip

    DEFAULT_CSS = """
    StatusChip {
        height: 1;
        width: auto;
        padding: 0 1;
        margin: 0 1 0 0;
        background: $boost;
        color: $text;
        text-style: bold;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    StatusChip.-muted {
        color: $text-muted;
    }
    StatusChip.-accent {
        color: $accent;
    }
    StatusChip:hover {
        background: $primary 30%;
        text-style: bold underline;
    }
    StatusChip:focus {
        background: $primary 50%;
        color: $text;
        text-style: bold reverse;
    }
    """

    def __init__(
        self,
        label: str,
        value: str,
        *,
        id: str | None = None,
        variant: str = "default",
    ) -> None:
        super().__init__(id=id)
        if variant != "default":
            self.add_class(f"-{variant}")
        self._label = label
        self._value = value

    @property
    def label(self) -> str:
        return self._label

    @property
    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value
        self.refresh(layout=True)

    def render(self) -> Text:
        text = self._value or "-"
        display = f"{self._label}: {text}" if self._label else text
        return Text(display)

    def on_click(self) -> None:
        self.post_message(self.ChipClicked(self))

    def _on_focus(self, _event) -> None:  # type: ignore[no-untyped-def]
        # Surface a hover-style on focus so keyboard navigation is visible.
        self.refresh()

    def key_enter(self) -> None:
        self.post_message(self.ChipClicked(self))


# ─── Modal screens ───────────────────────────────────────────────────────────


class SelectionModal(ModalScreen[str | None]):
    """A modal ListView picker. Returns the selected string, or None on cancel.

    Pass `allow_none=True` to add a "(none)" entry that returns None.
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss_cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    SelectionModal {
        align: center middle;
    }
    #selection-dialog {
        width: 60;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    #selection-title {
        width: 100%;
        content-align: center middle;
        text-style: bold;
        margin-bottom: 1;
    }
    #selection-list {
        height: auto;
        max-height: 20;
        background: $boost;
    }
    #selection-hint {
        width: 100%;
        content-align: center middle;
        color: $text-muted;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        title: str,
        options: list[str],
        *,
        current: str = "",
        allow_none: bool = False,
        none_label: str = "(none — clear)",
        extra_options: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        self._current = current
        self._allow_none = allow_none
        self._none_label = none_label
        self._extra_options = list(extra_options or [])
        self._row_values: dict[ListItem, str | None] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="selection-dialog"):
            yield Label(self._title, id="selection-title")
            items: list[ListItem] = []
            if self._allow_none:
                none_item = ListItem(Label(self._none_label), id="row-none")
                self._row_values[none_item] = None
                items.append(none_item)
            for idx, opt in enumerate(self._options):
                marker = "● " if opt == self._current else "  "
                row = ListItem(Label(marker + opt), id=f"row-opt-{idx}")
                self._row_values[row] = opt
                items.append(row)
            for idx, (sentinel, label) in enumerate(self._extra_options):
                row = ListItem(Label(label), id=f"row-extra-{idx}")
                self._row_values[row] = sentinel
                items.append(row)
            yield ListView(*items, id="selection-list")
            yield Label("Enter to select · Esc to cancel", id="selection-hint")

    def on_mount(self) -> None:
        # Pre-select the current value so Enter picks it without further input.
        if self._current and self._current in self._options:
            lv = self.query_one("#selection-list", ListView)
            for idx, opt in enumerate(self._options):
                if opt == self._current:
                    lv.index = idx + (1 if self._allow_none else 0)
                    break
        lv = self.query_one("#selection-list", ListView)
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        value = self._row_values.get(event.item)
        if value is None and event.item not in self._row_values:
            return
        self.dismiss(value)
        event.stop()

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)


class TextInputModal(ModalScreen[str | None]):
    """A modal Input dialog. Returns entered string, or None if cancelled.

    Empty submission returns "" (which the caller treats as "clear").
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss_cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    TextInputModal {
        align: center middle;
    }
    #text-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    #text-title {
        width: 100%;
        content-align: center middle;
        text-style: bold;
        margin-bottom: 1;
    }
    #text-input {
        margin-bottom: 1;
    }
    #text-hint {
        width: 100%;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        title: str,
        *,
        initial: str = "",
        placeholder: str = "",
        hint: str = "Enter to confirm · Esc to cancel",
    ) -> None:
        super().__init__()
        self._title = title
        self._initial = initial
        self._placeholder = placeholder
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="text-dialog"):
            yield Label(self._title, id="text-title")
            yield Input(value=self._initial, placeholder=self._placeholder, id="text-input")
            yield Label(self._hint, id="text-hint")

    def on_mount(self) -> None:
        self.query_one("#text-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)
        # Input.Submitted bubbles by default; the parent ChatTUI has its own
        # on_input_submitted that would otherwise dispatch the entered value
        # (e.g. an API key) as a chat prompt. Stop propagation here so modal
        # submits stay scoped to the modal.
        event.stop()

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)


# ─── Bottom panel ────────────────────────────────────────────────────────────


HELP_TEXT = (
    "[dim]\u2039Tab\u203a cycle chips  \u00b7  "
    "\u2039Enter\u203a open  \u00b7  "
    "\u2039Esc\u203a cancel  \u00b7  "
    "\u2039Ctrl+L\u203a clear  \u00b7  "
    "\u2039Ctrl+C\u203a quit[/dim]"
)


class BottomPanel(Static):
    """The 4-row panel under the input: spinner, chips, session, help hint."""

    DEFAULT_CSS = """
    BottomPanel {
        height: 4;
        background: $background;
    }
    #spinner-row {
        height: 1;
        padding: 0 1;
    }
    #chip-row {
        height: 1;
    }
    #chip-spacer {
        width: 1fr;
    }
    #session-row {
        height: 1;
        width: 100%;
        padding: 0 1;
        color: $text-muted;
    }
    #help-row {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._chips: dict[str, StatusChip] = {}
        self._session_label: Static | None = None
        self._spinner_row: Static | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="spinner-row")
        with Horizontal(id="chip-row"):
            self._chips["model"] = StatusChip("", "-", id="chip-model", variant="accent")
            self._chips["provider"] = StatusChip(
                "", "-", id="chip-provider", variant="muted"
            )
            self._chips["style"] = StatusChip("", "-", id="chip-style")
            yield self._chips["model"]
            yield self._chips["provider"]
            yield Static("", id="chip-spacer")
            yield self._chips["style"]
        self._session_label = Static("-", id="session-row")
        yield self._session_label
        yield Static(HELP_TEXT, id="help-row", markup=True)

    def set_value(self, kind: str, value: str) -> None:
        if kind == "session":
            if self._session_label is not None:
                self._session_label.update(value or "-")
            return
        chip = self._chips.get(kind)
        if chip is not None:
            chip.set_value(value)

    def get_chip(self, kind: str) -> StatusChip | None:
        return self._chips.get(kind)

    def render_spinner(self, frame: str, text: str) -> None:
        if self._spinner_row is None:
            try:
                self._spinner_row = self.query_one("#spinner-row", Static)
            except Exception:
                return
        if not frame:
            self._spinner_row.update("")
            return
        self._spinner_row.update(
            Text.assemble(
                (frame, f"bold {self.app.current_theme.accent}"),
                (f" {text}", self.app.current_theme.foreground),
            )
        )


class SelectableRichLog(RichLog):
    """RichLog subclass that supports text selection via mouse drag.

    The stock ``RichLog`` overrides ``render_line`` and bypasses the
    ``Visual.to_strips`` path where Textual applies the selection
    highlight.  It also inherits ``Widget.get_selection`` which calls
    ``self._render()`` — for ``RichLog`` that returns a ``Panel``
    (from ``ScrollView.render``), so text extraction always fails.

    This subclass:
    * Calls ``Strip.apply_offsets`` so the compositor can map mouse
      positions to content offsets (without this the entire widget is
      selected as ``SELECT_ALL``).
    * Applies ``Style(reverse=True)`` to the selected span to invert
      foreground/background colors.
    * Extracts selected text from ``self.lines`` (the list of ``Strip``
      objects accumulated by ``RichLog.write``).
    """

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        scroll_x, scroll_y = self.scroll_offset
        content_y = scroll_y + y

        selection = self.text_selection
        if selection is not None:
            span = selection.get_span(content_y)
            if span is not None:
                start_x, end_x = span
                start_x = max(0, start_x - scroll_x)
                if end_x == -1:
                    end_x = strip.cell_length
                else:
                    end_x = min(end_x - scroll_x, strip.cell_length)

                if start_x < end_x and start_x < strip.cell_length:
                    before = strip.crop(0, start_x)
                    selected_crop = strip.crop(start_x, end_x)
                    styled_segments = list(
                        Segment.apply_style(
                            selected_crop._segments, post_style=Style(reverse=True)
                        )
                    )
                    selected = Strip(styled_segments, selected_crop.cell_length)
                    after = strip.crop(end_x)
                    strip = Strip.join([before, selected, after])

        return strip.apply_offsets(scroll_x, content_y)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        if not self.lines:
            return None
        text = "\n".join(strip.text for strip in self.lines)
        return selection.extract(text), "\n"


class TUIEventSink:
    """Sink that posts chat events to a running `ChatTUI` app.

    The agent loop runs on a worker thread (via `asyncio.to_thread`), so
    every callback must hop to the main thread with `call_from_thread`
    before touching widgets. The spinner is driven by a Textual `Timer`
    that ticks on the main thread — `start_spin` / `stop_spin` only need
    to enable/disable it; `tick_spin` runs in the main loop already.
    """

    def __init__(self, app: ChatTUI) -> None:
        self._app = app
        self._frame_idx = 0
        self._activity = _SPINNER_DEFAULT_TEXT
        self._timer: Timer | None = None

    def begin_working(self) -> None:
        self._app.call_from_thread(self._start_spin)

    def _start_spin(self) -> None:
        if self._timer is not None:
            return
        self._frame_idx = 0
        self._timer = self._app.set_interval(_SPINNER_INTERVAL, self._tick_spin)

    def _tick_spin(self) -> None:
        frame = _SPINNER_FRAMES[self._frame_idx % len(_SPINNER_FRAMES)]
        self._frame_idx += 1
        self._app._render_spinner(frame, self._activity)

    def update_activity(self, text: str) -> None:
        self._activity = text or _SPINNER_DEFAULT_TEXT

    def end_working(self) -> None:
        self._app.call_from_thread(self._stop_spin)

    def _stop_spin(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._app._render_spinner("", "")

    def emit_loop_event(self, event: LoopEvent) -> None:
        self._app.call_from_thread(self._post_loop_event, event)

    def _post_loop_event(self, event: LoopEvent) -> None:
        log = self._app._log
        if log is None:
            return
        accent = self._app.current_theme.accent
        fg = self._app.current_theme.foreground
        line = Text.assemble(
            (f"{event.actor}:", accent),
            (f" {event.text}", fg),
        )
        log.write(line)

    def emit_final_answer(self, content: str, session_name: str) -> None:
        self._app.call_from_thread(self._post_final_answer, content, session_name)

    def _post_final_answer(self, content: str, session_name: str) -> None:
        log = self._app._log
        if log is None:
            return
        if content:
            log.write(Markdown(content))
        else:
            log.write(Text("(empty response)", style="dim"))
        log.write(Text(""))


# ─── Chat input ──────────────────────────────────────────────────────────────


class ChatInput(TextArea):
    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "submit", "Send", show=False, priority=True),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(text="", soft_wrap=True, id=id)

    def on_mount(self) -> None:
        super().on_mount()
        self.show_line_numbers = False

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.action_submit()
            return
        await super()._on_key(event)

    def watch_virtual_size(self, virtual_size) -> None:
        content_lines = virtual_size.height
        target = max(5, min(content_lines + 2, 12))
        self.styles.height = target

    def action_submit(self) -> None:
        if self.text.strip():
            self.post_message(self.Submitted(self.text))


# ─── Main app ────────────────────────────────────────────────────────────────


class ChatTUI(App):
    CSS = """
    Screen { layout: vertical; }
    #main-area {
        width: 1fr;
        height: 1fr;
        background: $background;
    }
    #chat {
        height: 1fr;
        width: 1fr;
        background: $background;
        overflow-y: auto;
        scrollbar-size: 0 0;
    }
    #chat:focus {
        background: $background;
        background-tint: transparent;
    }
    #input {
        height: 5;
        width: 1fr;
        padding: 1 1;
        margin: 0 2;
        background: $surface;
        border: none;
        border-left: thick $primary;
        scrollbar-size: 1 1;
    }
    #input:focus {
        border: none;
        border-left: thick $primary;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+shift+c", "copy_text", "Copy"),
        Binding("ctrl+l", "clear_log", "Clear"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._log: SelectableRichLog | None = None
        self._input: Input | None = None
        self._panel: BottomPanel | None = None
        self._busy = False
        self._logo_shown = False
        self._chat_started = False
        self._session_assigned = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="main-area"):
            self._log = SelectableRichLog(highlight=False, id="chat", wrap=True, markup=False)
            yield self._log
            self._input = ChatInput(id="input")
            yield self._input
        self._panel = BottomPanel()
        yield self._panel
        yield Footer()

    def on_mount(self) -> None:
        cwd = Path.cwd()
        home = Path.home()
        try:
            cwd_display = "~/" + str(cwd.relative_to(home))
        except ValueError:
            cwd_display = str(cwd)
        self.title = cwd_display
        self._refresh_chips()
        self._log = self.query_one("#chat", SelectableRichLog)
        saved = settings_store.load()
        if saved.theme:
            self.theme = saved.theme
        self._show_logo()
        if self._input is not None:
            self._input.focus()

    def _show_logo(self) -> None:
        if self._log is None:
            return
        self._log.clear()
        width = self.size.width if self.size.width > 0 else 80
        self._log.write(Text(_render_logo(width - 2), style=self.current_theme.accent))
        self._log.write(Text(""))
        self._log.write(Text("Ready. Press Ctrl+P for commands.", style="dim"))
        self._logo_shown = True

    def watch_theme(self) -> None:
        settings_store.update(theme=self.theme)
        if self._logo_shown:
            self._show_logo()

    def get_system_commands(self, screen) -> Iterable[SystemCommand]:
        yield SystemCommand(
            "00 New session",
            "Create a new session",
            self._new_session,
        )
        yield SystemCommand(
            "01 Pick session",
            "Switch the active session",
            self._open_session_picker,
        )
        yield SystemCommand(
            "02 Pick model",
            "Set the active provider's default model",
            self._open_model_editor,
        )
        yield SystemCommand(
            "03 Pick provider",
            "Switch the active provider (or add from catalog / custom)",
            self._open_provider_picker,
        )
        yield SystemCommand(
            "04 Pick style",
            "Switch the active style document",
            self._open_style_picker,
        )
        yield SystemCommand(
            "05 Theme",
            "Change the current theme",
            self.action_change_theme,
        )
        yield SystemCommand(
            "06 Clear log",
            "Clear the chat log (also bound to Ctrl+L)",
            self.action_clear_log,
        )
        remaining = [
            cmd for cmd in super().get_system_commands(screen)
            if cmd.title != "Theme"
        ]
        for i, cmd in enumerate(remaining):
            yield SystemCommand(
                f"{i + 7:02d} {cmd.title}",
                cmd.help,
                cmd.callback,
                cmd.discover,
            )

    # ── chip click routing ──────────────────────────────────────────────────

    def on_status_chip_chip_clicked(self, event: StatusChip.ChipClicked) -> None:
        chip = event.chip
        if chip.id == "chip-provider":
            self._open_provider_picker()
        elif chip.id == "chip-model":
            self._open_model_editor()
        elif chip.id == "chip-style":
            self._open_style_picker()
        elif chip.id == "chip-session":
            self._open_session_picker()

    # ── modal actions ───────────────────────────────────────────────────────

    def _open_provider_picker(self) -> None:
        providers = sorted(provider_store.load_all().keys())
        s = settings_store.load()

        def _on_close(result: str | None) -> None:
            if not result:
                return
            if result == SENTINEL_CATALOG_ADD:
                self.run_worker(self._catalog_add_flow(), exclusive=False)
                return
            if result == SENTINEL_CUSTOM_ADD:
                self._open_custom_add_wizard()
                return
            self._switch_provider(result)

        self.push_screen(
            SelectionModal(
                "Select provider",
                providers,
                current=s.provider,
                allow_none=False,
                extra_options=[
                    (SENTINEL_CATALOG_ADD, "+ add from catalog…"),
                    (SENTINEL_CUSTOM_ADD, "+ add custom…"),
                ],
            ),
            _on_close,
        )

    def _switch_provider(self, name: str) -> None:
        settings_store.update(provider=name)
        self._refresh_chips()
        self._spinner_status(f"provider → {name}")

    async def _catalog_add_flow(self) -> None:
        self._spinner_status("fetching catalog…")
        try:
            all_providers = await asyncio.to_thread(catalog_api.list_providers)
        except Exception as exc:
            self._spinner_status(f"catalog error: {exc}")
            return
        self._clear_spinner_status()

        name_to_provider: dict[str, dict[str, Any]] = {}
        for p in all_providers:
            fmt, _ = sdk_to_format(p.get("sdk"), p.get("api"))
            if fmt:
                name_to_provider[p["name"]] = p
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

        catalog_p = name_to_provider[picked_name]
        our_name = catalog_api.slugify(picked_name)
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

        self._spinner_status("fetching model list…")
        default_model = ""
        try:
            models = await asyncio.to_thread(catalog_api.list_models, picked_name)
            if models:
                default_model = models[0].get("id", "")
        except Exception as exc:
            self._spinner_status(f"catalog models error: {exc}")

        try:
            provider = add_provider_from_lma(
                name=our_name,
                api_key=api_key.strip(),
                sdk=catalog_p.get("sdk"),
                api=catalog_p.get("api"),
                default_model=default_model,
            )
        except ValueError as exc:
            self._spinner_status(f"add failed: {exc}")
            return

        paths.ensure_dirs()
        provider_store.upsert(provider)
        settings_store.update(provider=our_name)
        self._refresh_chips()
        self._spinner_status(
            f"provider → {our_name} (added from catalog, "
            f"default-model={default_model or '-'})"
        )

    def _open_custom_add_wizard(self) -> None:
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
                paths.ensure_dirs()
                provider_store.upsert(
                    provider_store.Provider(
                        name=state["name"],
                        api_format=state["api_format"],
                        base_url=state["base_url"],
                        api_key=state["api_key"],
                        default_model=model,
                        source=SOURCE_CUSTOM,
                        max_context_window=ctx,
                        max_output_tokens=parsed,
                    )
                )
                settings_store.update(provider=state["name"])
                self._refresh_chips()
                self._spinner_status(f"provider → {state['name']} (custom)")

            self.push_screen(
                TextInputModal(
                    "Custom provider: max output tokens (optional)",
                    placeholder="e.g. 16384 · empty to skip",
                    hint="Enter to save · Esc to cancel",
                ),
                on_close,
            )

        ask_name()

    def _open_model_editor(self) -> None:
        s = settings_store.load()
        provider = provider_store.get(s.provider) if s.provider else None
        if not provider:
            self._spinner_status("No active provider. Pick a provider first.")
            return
        if provider.source == SOURCE_LMA:
            self.run_worker(self._catalog_model_picker_flow(provider), exclusive=False)
        else:
            self._open_custom_model_editor(provider)

    def _open_custom_model_editor(self, provider) -> None:
        current_model = provider.default_model

        def ask_model_id() -> None:
            def _on_close(result: str | None) -> None:
                if result is None:
                    return
                ask_max_context(result.strip())

            self.push_screen(
                TextInputModal(
                    f"Edit model (custom provider: {provider.name})",
                    initial=current_model,
                    placeholder=(
                        f"current default: {current_model}" if current_model else "model id"
                    ),
                    hint="Empty to clear · Enter to continue · Esc to cancel",
                ),
                _on_close,
            )

        def ask_max_context(new_model: str) -> None:
            cur = provider.max_context_window

            def _on_close(result: str | None) -> None:
                if result is None:
                    return
                try:
                    parsed = _parse_optional_int(result)
                except ValueError:
                    self._spinner_status(
                        f"invalid max context window; keeping existing "
                        f"({cur if cur else '-'})"
                    )
                    parsed = cur
                ask_max_tokens(new_model, parsed)

            self.push_screen(
                TextInputModal(
                    f"Max context window (tokens) — {provider.name}",
                    initial=str(cur) if cur else "",
                    placeholder="optional · e.g. 128000 · empty to clear",
                    hint="Enter to continue · Esc to cancel",
                ),
                _on_close,
            )

        def ask_max_tokens(new_model: str, new_ctx: int | None) -> None:
            cur = provider.max_output_tokens

            def _on_close(result: str | None) -> None:
                if result is None:
                    return
                try:
                    parsed = _parse_optional_int(result)
                except ValueError:
                    self._spinner_status(
                        f"invalid max output tokens; keeping existing "
                        f"({cur if cur else '-'})"
                    )
                    parsed = cur
                self._save_custom_model(provider.name, new_model, new_ctx, parsed)

            self.push_screen(
                TextInputModal(
                    f"Max output tokens — {provider.name}",
                    initial=str(cur) if cur else "",
                    placeholder="optional · default 16384 · empty to clear",
                    hint="Enter to save · Esc to cancel",
                ),
                _on_close,
            )

        ask_model_id()

    async def _catalog_model_picker_flow(self, provider) -> None:
        self._spinner_status("fetching model list…")
        try:
            models = await asyncio.to_thread(catalog_api.list_models, provider.name)
        except Exception as exc:
            self._spinner_status(f"catalog error: {exc}; falling back to text input")
            self._open_custom_model_editor(provider)
            return
        self._clear_spinner_status()
        if not models:
            self._spinner_status(f"No models found for {provider.name!r} in catalog.")
            return

        options = [f"{m.get('id', '?')} — {m.get('name', '')}" for m in models]
        current = provider.default_model
        current_disp = next(
            (opt for opt in options if opt.split(" — ", 1)[0] == current),
            "",
        )

        picked = await self.push_screen_wait(
            SelectionModal(
                f"Model ({provider.name}, {len(options)})",
                options,
                current=current_disp,
                allow_none=False,
            )
        )
        if not picked:
            return
        new_id = picked.split(" — ", 1)[0].strip()
        self._save_model_change(provider.name, new_id)

    def _save_model_change(self, provider_name: str, new_model: str) -> None:
        p = provider_store.get(provider_name)
        if p is None:
            return
        provider_store.upsert(replace(p, default_model=new_model))
        settings_store.update(model="")
        self._refresh_chips()
        if new_model:
            self._spinner_status(f"model → {new_model} (provider default)")
        else:
            self._spinner_status("model → cleared (provider default empty)")

    def _save_custom_model(
        self,
        provider_name: str,
        new_model: str,
        max_context_window: int | None,
        max_output_tokens: int | None,
    ) -> None:
        p = provider_store.get(provider_name)
        if p is None:
            return
        provider_store.upsert(
            replace(
                p,
                default_model=new_model,
                max_context_window=max_context_window,
                max_output_tokens=max_output_tokens,
            )
        )
        settings_store.update(model="")
        self._refresh_chips()
        parts: list[str] = [
            f"model → {new_model}" if new_model else "model → cleared"
        ]
        if max_context_window is not None:
            parts.append(f"ctx={max_context_window}")
        if max_output_tokens is not None:
            parts.append(f"max-tokens={max_output_tokens}")
        self._spinner_status(" · ".join(parts) + " (provider default)")

    def _open_style_picker(self) -> None:
        styles = style_store.list_styles()
        if not styles:
            self._spinner_status("No styles installed. Run `miniouto style add <repo>`.")
            return
        s = settings_store.load()

        def _on_close(result: str | None) -> None:
            if not result:
                return
            settings_store.update(style=result)
            self._refresh_chips()
            self._spinner_status(f"style → {result}")

        self.push_screen(
            SelectionModal("Select style", styles, current=s.style, allow_none=False),
            _on_close,
        )

    def _open_session_picker(self) -> None:
        sessions = session_store.list_sessions()
        s = settings_store.load()
        current = s.session if self._chat_started else ""

        def _on_close(result: str | None) -> None:
            if result is None:
                return
            if result == "__new__":
                self._new_session()
                return
            settings_store.update(session=result)
            self._session_assigned = True
            self._chat_started = True
            self._load_session_history(result)
            self._refresh_chips()

        options = [*sessions, "__new__"]
        self.push_screen(
            SelectionModal(
                "Select session",
                options,
                current=current,
                allow_none=False,
                extra_options=[("__new__", "+ new session…")],
            ),
            _on_close,
        )

    def _new_session(self) -> None:
        import datetime
        import uuid

        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = uuid.uuid4().hex[:6]
        name = f"tui-{ts}-{suffix}"
        settings_store.update(session=name)
        session_store.append(name, MessageRecord(role="system", content="(session created)"))
        self._session_assigned = True
        self._chat_started = True
        self._refresh_chips()
        self._show_logo()

    def _load_session_history(self, session_name: str) -> None:
        if self._log is None:
            return
        self._log.clear()
        self._logo_shown = False
        messages = session_store.load(session_name)
        if not messages:
            self._log.write(Text("(empty session)", style="dim"))
            return
        accent = self.current_theme.accent
        fg = self.current_theme.foreground
        for m in messages:
            if m.role == "user" and m.content:
                self._log.write(
                    Text("> ", style=f"bold {accent}") + Text(m.content)
                )
            elif m.role == "assistant":
                if m.content:
                    self._log.write(Markdown(m.content))
                if m.tool_calls:
                    for tc in m.tool_calls:
                        fn = tc.get("function", {}).get("name", "?")
                        self._log.write(
                            Text.assemble(("tool:", accent), (f" {fn}", fg))
                        )
            elif m.role == "tool" and m.content:
                name = m.name or "tool"
                snippet = m.content[:200] + ("…" if len(m.content) > 200 else "")
                self._log.write(
                    Text.assemble((f"{name}:", accent), (f" {snippet}", fg))
                )
            elif m.role == "system" and m.content:
                self._log.write(
                    Text(f"[{m.content}]", style=self.current_theme.warning)
                )
        self._log.write(Text(""))

    # ── status / refresh ────────────────────────────────────────────────────

    def _refresh_chips(self) -> None:
        if self._panel is None:
            return
        s = settings_store.load()
        provider = provider_store.get(s.provider) if s.provider else None
        active_model = provider.default_model if provider else ""
        self._panel.set_value("provider", s.provider or "-")
        self._panel.set_value("model", active_model or "-")
        self._panel.set_value("style", s.style or "-")
        if self._chat_started:
            self._panel.set_value("session", s.session or "-")
        else:
            self._panel.set_value("session", "-")

    # ── chat flow ───────────────────────────────────────────────────────────

    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        if self._busy:
            return
        text = event.value.strip()
        if not text:
            return
        self._input.text = ""
        if self._logo_shown and self._log is not None:
            self._log.clear()
            self._logo_shown = False
        self._post_user(text)
        self.run_worker(self._dispatch(text), exclusive=True)

    def action_clear_log(self) -> None:
        if self._log is not None:
            self._log.clear()

    def action_copy_text(self) -> None:
        text = self.screen.get_selected_text()
        if text:
            self.copy_to_clipboard(text)
            self.screen.clear_selection()

    def _post_user(self, text: str) -> None:
        if self._log is None:
            return
        self._log.write(Text("> ", style=f"bold {self.current_theme.accent}") + Text(text))

    def _post_assistant(self, text: str) -> None:
        if self._log is None:
            return
        self._log.write(Text(text, style=self.current_theme.foreground))

    def _post_system(self, text: str) -> None:
        if self._log is None:
            return
        self._log.write(Text(f"[{text}]", style=self.current_theme.warning))

    def _render_spinner(self, frame: str, text: str) -> None:
        if self._panel is not None:
            self._panel.render_spinner(frame, text)

    def _spinner_status(self, text: str) -> None:
        if self._panel is not None:
            self._panel.render_spinner("⠋", text)

    def _clear_spinner_status(self) -> None:
        if self._panel is not None:
            self._panel.render_spinner("", "")

    async def _dispatch(self, prompt: str) -> None:
        assert self._log is not None
        first_message = not self._chat_started
        self._chat_started = True
        self._busy = True
        if first_message and not self._session_assigned:
            import datetime
            import uuid

            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            suffix = uuid.uuid4().hex[:6]
            settings_store.update(session=f"tui-{ts}-{suffix}")
            self._session_assigned = True
            self._refresh_chips()
        s = settings_store.load()
        sink = TUIEventSink(self)
        try:
            opts = ChatOptions(
                prompt=prompt,
                session=s.session or "default",
                model=s.model or None,
                continue_session=True,
            )
            await asyncio.to_thread(run_chat, opts, sink)
        except Exception as exc:
            self._post_system(f"error: {exc}")
            self._busy = False
            return
        self._busy = False
        self._refresh_chips()


def run_tui() -> None:
    ChatTUI().run()


def tui_summary() -> dict[str, Any]:
    s = settings_store.load()
    provider = provider_store.get(s.provider)
    return {
        "provider": s.provider,
        "model": s.model or (provider.default_model if provider else ""),
        "style": s.style,
        "session": s.session,
        "styles_available": style_store.list_styles(),
    }
