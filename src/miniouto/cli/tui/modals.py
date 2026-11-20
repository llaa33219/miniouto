"""Modal screens: the pickers, the settings popup and the command palette.

WHY plain state objects: the port renders with pyratatui, which has no widget
tree, no CSS and no message pump. A modal is therefore a small object that
holds its state, builds the styled lines of a centered dialog for a given area,
and consumes the keys and mouse reports the app routes to it. The one place a
Textual-shaped surface is preserved is the TEST surface (``.id``, ``.region``,
``.render().plain``, ``.query(...)``) because the ported behavioral tests are
the specification and they read modals that way.

Everything visible is ported 1:1 from the old Textual ``cli/tui.py``: the same
rows, the same trio semantics (AGENTS.md invariant 12), the same pickers, the
same hints. The look comes from the verified pty captures in
``.miniouto/plans/tui-visual-facts.md``:

* centered dialogs — thick ``█▀▄`` primary border, ``$surface`` body,
  ``padding 1 2``, bold centered title, ``$boost`` list body, the list capped at
  20 rows, and a focused row painted solid primary with near-black bold text;
* the text input — tall ``▊▔▎`` primary border, ``$surface`` body, a blinking
  reversed block cursor and the ``$text-disabled`` placeholder;
* the command palette — the same centred dialog listing EVERY command (no
  search box): one bold name row and its dim help row per command, then the
  hint line.

Protocol notes for the app layer:

* ``Modal.dismiss(result)`` pops itself (``app.pop_screen``) BEFORE running its
  callback, because a callback may push the NEXT screen — the caps editor asks
  its two questions exactly that way. ``Modal._settle`` is idempotent, so the
  app may also call it from its own ``pop_screen`` without closing twice.
* ``push_screen_wait`` is ``push_screen(screen)`` plus ``await
  screen.wait_result()``: the future belongs to the modal and is resolved by
  ``dismiss``. A waiter created after a dismiss resolves immediately.
* ``build_lines(area, theme, behind=None)`` paints the WHOLE area — the dimmed
  backdrop (``$background 60%``) plus the centered dialog. Passing the app's
  own composed rows as ``behind`` dims them underneath, exactly like Textual's
  modal-screen background.
* The command palette takes its items from ``app.get_system_commands()``
  (duck-typed: anything with ``.title`` / ``.help`` / ``.callback``), which is
  where the old ``00 New session`` … ``06 Clear log`` list lives;
  :func:`default_system_commands` builds that list plus Textual's defaults.

The trio/config rules themselves live in :mod:`.config` (ported verbatim from
the old module); this module only imports them, so a test that monkeypatches
``config._get_reasoning_api`` reaches every caller.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any, NamedTuple

from rich.cells import cell_len
from rich.style import Style as RichStyle
from rich.text import Text

from ...core import context as context_api
from ...core import modelslist as catalog_api
from ...core.providers import CatalogDialect, catalog_compatibility
from ...storage import providers as provider_store
from ...storage.providers import SOURCE_CUSTOM, is_catalog_source
from . import bridge, pt
from .config import (
    CAPS_FIELD,
    COMPATIBILITY_FIELD,
    SENTINEL_CATALOG_ADD,
    SENTINEL_CUSTOM_ADD,
    SENTINEL_SAME_AS_OUTO,
    TrioTarget,
    _caps_value,
    _effective_config,
    _model_defaults,
    _parse_optional_int,
    _reasoning_choices,
    _reasoning_target,
    _record,
    _trio,
    _trio_display,
)
from .input import Key, Mouse
from .nodes import Node, Rect
from .render import blend, hex_to_rgb
from .rows import truncate_to
from .theme import TuiTheme, color

__all__ = [
    "BACKDROP_ALPHA",
    "CURSOR_BLINK_SECONDS",
    "KEYS_HINT",
    "LIST_MAX_ROWS",
    "NATIVE_DIALECT_LABEL",
    "PALETTE_HINT",
    "PALETTE_MAX_ROWS",
    "PALETTE_PLACEHOLDER",
    "PROVIDER_HINT",
    "SELECTION_HINT",
    "TEXT_INPUT_HINT",
    "CommandPalette",
    "KeyModal",
    "Label",
    "ListItem",
    "Modal",
    "Node",
    "ProviderSettingsPopup",
    "Rect",
    "SelectionList",
    "SelectionModal",
    "SettingRow",
    "StyledLine",
    "SystemCommand",
    "TextInput",
    "TextInputModal",
    "default_system_commands",
    "dim_line",
]

BACKDROP_ALPHA = 0.6
"""Textual's ``ModalScreen { background: $background 60% }``.

The modal screen paints the theme background over what it covers, so the app's
rows stay faintly visible (at 40%) around the dialog.
"""

AUTO_TEXT_ALPHA = 0.87
"""Textual's ``$text auto 87%`` — the ink of a dialog and of its selection bar.

White at 87% over the dark background is ``#dfdfe0`` (the verified value) and
black at 87% over the primary selection bar is ``#061921``.
"""

CURSOR_BLINK_SECONDS = 0.5
"""Block-cursor half period, taken from Textual's ``Input`` / ``TextArea``."""

SELECTION_HINT = "Enter to select · Esc to cancel"
TEXT_INPUT_HINT = "Enter to confirm · Esc to cancel"
PROVIDER_HINT = "Click a row to change it · Esc to close"
PALETTE_PLACEHOLDER = "Search for commands…"
PALETTE_HINT = "Enter to run · ↑↓ to move · Esc to cancel"
PALETTE_MAX_ROWS = 8
PALETTE_WIDTH = 76
"""Dialog width (cells): wide enough for a command name and its help."""
"""Commands listed at once; the list scrolls past this."""
NATIVE_DIALECT_LABEL = "native SDK"
"""What a catalog provider with no saved dialect shows on its Compatibility row."""

LIST_MAX_ROWS = 20
"""``#selection-list { max-height: 20 }`` — the list never grows past this."""

DIALOG_WIDTH = 60
"""``width: 60`` on every ``*-dialog`` container."""

Stripe = tuple[str, pt.Style]
StyledLine = list[Stripe]
"""One visual line of styled runs — the currency every ``build_lines`` speaks."""

_NO_VALUE = object()
"""Marker for a row that carries no value at all (a section header)."""


# ─── geometry ────────────────────────────────────────────────────────────────


def _as_rect(area: Any) -> Rect:
    """Normalize "the area" into a :class:`Rect`.

    Accepts a :class:`Rect`, a pyratatui ``Rect``, anything else exposing
    ``x``/``y``/``width``/``height``, or a ``(width, height)`` pair.
    """

    if isinstance(area, Rect):
        return area
    if isinstance(area, (tuple, list)) and len(area) == 2:
        return Rect(0, 0, int(area[0]), int(area[1]))
    return Rect(
        int(getattr(area, "x", 0)),
        int(getattr(area, "y", 0)),
        int(getattr(area, "width", 0)),
        int(getattr(area, "height", 0)),
    )


def _centered_origin(outer: Rect, width: int, height: int) -> tuple[int, int]:
    """Top-left cell that centers a ``width``x``height`` box in ``outer``."""

    return (
        outer.x + max(0, (outer.width - width) // 2),
        outer.y + max(0, (outer.height - height) // 2),
    )


# ─── styling ─────────────────────────────────────────────────────────────────


def _style(
    *,
    fg: str | None = None,
    bg: str | None = None,
    bold: bool = False,
    dim: bool = False,
    underline: bool = False,
    reverse: bool = False,
) -> pt.Style:
    """Build a pyratatui style from palette hexes (``None`` = inherit)."""

    style = pt.Style()
    if fg is not None:
        style = style.fg(color(fg))
    if bg is not None:
        style = style.bg(color(bg))
    modifiers = pt.Modifier()
    if bold:
        modifiers = modifiers | pt.Modifier.bold()
    if dim:
        modifiers = modifiers | pt.Modifier.dim()
    if underline:
        modifiers = modifiers | pt.Modifier.underlined()
    if reverse:
        modifiers = modifiers | pt.Modifier.reversed()
    if bold or dim or underline or reverse:
        style = style.add_modifier(modifiers)
    return style


def _auto_text(bg_hex: str) -> str:
    """Textual's ``auto`` ink for a background, composited at 87%.

    ``get_contrast_text`` picks white on a dark background and black on a light
    one by perceived brightness (the Rec. 601 weights Textual uses), then applies
    the alpha — which is how the picker's selection bar ends up near-black.
    """

    red, green, blue = hex_to_rgb(bg_hex)
    brightness = (299 * red + 587 * green + 114 * blue) / (255 * 1000)
    ink = "#000000" if brightness >= 0.5 else "#ffffff"
    return blend(ink, bg_hex, AUTO_TEXT_ALPHA)


def _text_hex(theme: TuiTheme) -> str:
    """``$text`` — Textual's auto-87% foreground, resolved against the theme."""

    return _auto_text(theme.background)


def _color_hex(value: Any) -> str | None:
    """``pt.Color(Rgb(r, g, b))`` → ``#rrggbb``; ``None`` when it is not RGB."""

    text = str(value)
    if not text.startswith("Color(Rgb("):
        return None
    body = text[len("Color(Rgb(") : -2]
    try:
        red, green, blue = (int(part.strip()) for part in body.split(","))
    except ValueError:  # pragma: no cover - the indexed/reset forms
        return None
    return f"#{red:02x}{green:02x}{blue:02x}"


def _dim_stripe(
    stripe: Stripe, theme: TuiTheme, alpha: float = BACKDROP_ALPHA
) -> Stripe:
    """Composite one run under ``$background`` at ``alpha``.

    ``background: <color> 60%`` is alpha compositing, so the result is the run's
    own color at 40% over the theme background: ``#2fc2ff`` becomes ``#185672``,
    the exact value the pty captures show behind a modal.
    """

    text, style = stripe
    keep = 1.0 - alpha
    fg = _color_hex(style.foreground)
    bg = _color_hex(style.background)
    return (
        text,
        _style(
            fg=blend(fg, theme.background, keep) if fg else None,
            bg=blend(bg, theme.background, keep) if bg else None,
        ),
    )


def dim_line(line: StyledLine, theme: TuiTheme, alpha: float = BACKDROP_ALPHA) -> StyledLine:
    """Dim one composed row — what a modal screen does to the app underneath."""

    return [_dim_stripe(stripe, theme, alpha) for stripe in line]


# ─── line assembly ───────────────────────────────────────────────────────────


def _pad(style: pt.Style, width: int) -> StyledLine:
    """``width`` blank cells (never negative)."""

    return [(" " * width, style)] if width > 0 else []


def _cells(line: StyledLine) -> int:
    return sum(cell_len(text) for text, _ in line)



def _fit(pieces: Iterable[Stripe], width: int, fill: pt.Style) -> StyledLine:
    """Clip ``pieces`` to ``width`` CELLS and pad the remainder with ``fill``.

    Clipping is by cell, not by character: a wide (CJK / emoji) character that
    does not fit is dropped whole and the line is padded, rather than spilling
    into the dialog's border.
    """

    if width <= 0:
        return []
    out: StyledLine = []
    used = 0
    for text, style in pieces:
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
    out.extend(_pad(fill, width - used))
    return out


def _centered(text: str, width: int, style: pt.Style) -> StyledLine:
    """One centered line, filled to ``width`` with ``style``."""

    if width <= 0:
        return []
    cells = cell_len(text)
    if cells >= width:
        return _fit([(text, style)], width, style)
    left = (width - cells) // 2
    return _fit([(" " * left, style), (text, style)], width, style)


def _slice_cells(line: StyledLine, start: int, stop: int) -> StyledLine:
    """The ``[start, stop)`` cell window of a composed line."""

    if stop <= start:
        return []
    out: StyledLine = []
    position = 0
    for text, style in line:
        if position >= stop:
            break
        chunk = ""
        for char in text:
            char_cells = cell_len(char)
            if start <= position and position + char_cells <= stop:
                chunk += char
            position += char_cells
        if chunk:
            out.append((chunk, style))
    return out


def _to_rich(style: pt.Style) -> Any:
    """``pt.Style`` → ``rich.style.Style`` for the ``.render()`` test surface."""

    rich = RichStyle()
    fg = _color_hex(style.foreground)
    bg = _color_hex(style.background)
    if fg:
        rich += RichStyle(color=fg)
    if bg:
        rich += RichStyle(bgcolor=bg)
    dumped = str(style)
    if "BOLD" in dumped:
        rich += RichStyle(bold=True)
    if "UNDERLINED" in dumped:
        rich += RichStyle(underline=True)
    if "DIM" in dumped:
        rich += RichStyle(dim=True)
    if "REVERSED" in dumped:
        rich += RichStyle(reverse=True)
    return rich


def _render_lines(lines: Sequence[StyledLine]) -> Text:
    """The Rich renderable of the styled lines (``.render().plain`` reads it)."""

    text = Text()
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        for chunk, style in line:
            styled = _to_rich(style)
            text.append(chunk, styled if str(styled) else None)
    return text


# ─── the tree / test surface ─────────────────────────────────────────────────


class Label(Node):
    """A one-row static text run (the ``Label`` stand-in)."""

    def __init__(
        self,
        content: str = "",
        id: str | None = None,
        *,
        tone: str = "text",
        align: str = "left",
        bold: bool = False,
    ) -> None:
        super().__init__(id=id)
        self._content = content
        self.tone = tone
        self.align = align
        self.bold = bold

    # ── state ───────────────────────────────────────────────────────────────
    @property
    def content(self) -> Text:
        """The Rich content (``str(...)`` is its plain text, as in Textual)."""

        return Text(self._content)

    @property
    def plain(self) -> str:
        return self._content

    def update(self, content: str) -> None:
        """Replace the content (the ``Label.update`` the popup uses)."""

        self._content = content

    def render(self) -> Text:
        return Text(self._content)

    # ── painting ────────────────────────────────────────────────────────────
    def style_for(self, theme: TuiTheme, bg: str | None) -> pt.Style:
        """The tone's style on ``bg`` (``text`` = Textual's auto ink)."""

        fg = {
            "text": _text_hex(theme),
            "muted": theme.text_muted,
            "error": theme.error,
        }.get(self.tone, _text_hex(theme))
        return _style(fg=fg, bg=bg, bold=self.bold)

    def build_lines(self, width: int, theme: TuiTheme, *, bg: str | None = None) -> list[StyledLine]:
        style = self.style_for(theme, bg)
        if self.align == "center":
            return [_centered(self.plain, width, style)]
        return [_fit([(self.plain, style)], width, style)]


class ListItem(Node):
    """One row of a :class:`SelectionList` (the ``ListItem`` stand-in)."""

    def __init__(self, label: Label, id: str | None = None, classes: Iterable[str] = ()) -> None:
        super().__init__(id=id, classes=classes)
        self.label = self.add(label)
        self.highlighted = False
        self.selectable = "-header" not in self.classes

    def render(self) -> Text:
        return self.label.render()

    def build_lines(
        self, width: int, theme: TuiTheme, *, bg: str, highlighted: bool
    ) -> list[StyledLine]:
        if highlighted:
            # ListView's focused selection: solid primary, near-black bold ink,
            # the FULL row (no marker column, no partial bar).
            style = _style(fg=_auto_text(theme.primary), bg=theme.primary, bold=True)
        else:
            style = self.label.style_for(theme, bg)
        return [_fit([(self.label.plain, style)], width, style)]


class SelectionList(Node):
    """The body of a :class:`SelectionModal` (the ``ListView`` stand-in).

    Holds the rows, the highlighted index — which never rests on a header — and
    the scroll offset. ``children`` IS the row list, so the ported tests read
    ``query_one('#selection-list').children`` exactly as before.
    """

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self.rows: list[ListItem] = []
        self.index: int | None = None
        self.scroll = 0
        self._snapped: int | None = None

    def append(self, row: ListItem) -> ListItem:
        self.rows.append(row)
        return self.add(row)

    # ── navigation ──────────────────────────────────────────────────────────
    def selectable_indices(self) -> list[int]:
        return [index for index, row in enumerate(self.rows) if row.selectable]

    def first_selectable(self) -> int | None:
        indices = self.selectable_indices()
        return indices[0] if indices else None

    def last_selectable(self) -> int | None:
        indices = self.selectable_indices()
        return indices[-1] if indices else None

    def step(self, direction: int) -> int | None:
        """The next selectable index from the current one (headers skipped).

        Movement clamps at the ends instead of wrapping, the way Textual's
        ``ListView`` clamps ``index``.
        """

        indices = self.selectable_indices()
        if not indices:
            return self.index
        current = self.index
        if current is None:
            return indices[0] if direction > 0 else indices[-1]
        if direction > 0:
            later = [index for index in indices if index > current]
            return later[0] if later else current
        earlier = [index for index in indices if index < current]
        return earlier[-1] if earlier else current

    def set_index(self, index: int | None) -> None:
        for row in self.rows:
            row.highlighted = False
            row.set_class(False, "-highlight")
        self.index = index
        row = self.highlighted()
        if row is not None:
            row.highlighted = True
            row.set_class(True, "-highlight")

    def highlighted(self) -> ListItem | None:
        if self.index is None or not 0 <= self.index < len(self.rows):
            return None
        return self.rows[self.index]

    # ── scrolling / painting ────────────────────────────────────────────────
    def limit(self, max_rows: int) -> int:
        return max(0, len(self.rows) - max_rows)

    def window(self, max_rows: int) -> list[tuple[int, ListItem]]:
        """The visible ``(absolute index, row)`` pairs for ``max_rows`` rows.

        The window follows the highlight — it scrolls just enough to keep the
        selected row on screen, which is what ``ListView.scroll_to_widget`` does
        on every index change. It re-snaps only when the HIGHLIGHT actually
        moved, so a wheel scroll is not undone by the very next repaint.
        """

        if max_rows <= 0:
            return []
        if self.index is not None and self.index != self._snapped:
            if self.index < self.scroll:
                self.scroll = self.index
            elif self.index >= self.scroll + max_rows:
                self.scroll = self.index - max_rows + 1
            self._snapped = self.index
        self.scroll = max(0, min(self.scroll, self.limit(max_rows)))
        return list(enumerate(self.rows))[self.scroll : self.scroll + max_rows]

    def scroll_by(self, delta: int, max_rows: int) -> None:
        """Move the window without moving the highlight (wheel scrolling)."""

        self.scroll = max(0, min(self.scroll + delta, self.limit(max_rows)))

    def build_lines(
        self, width: int, theme: TuiTheme, *, bg: str, max_rows: int
    ) -> list[StyledLine]:
        lines: list[StyledLine] = []
        for _, row in self.window(max_rows):
            lines.extend(row.build_lines(width, theme, bg=bg, highlighted=row.highlighted))
        return lines


class TextInput(Node):
    """The single-line editor of :class:`TextInputModal` and the palette.

    The buffer, the cursor and the readline key handling are pyratatui's
    ``TextState`` — duck-typed, so the app's own :class:`~.input.Key` works
    verbatim. This class adds only what a modal needs: the tall ``▊▔▎`` frame,
    the placeholder and the blinking reversed block cursor.
    """

    def __init__(
        self, *, initial: str = "", placeholder: str = "", id: str | None = None
    ) -> None:
        super().__init__(id=id)
        self.placeholder = placeholder
        self.state = pt.TextState()
        self.state.focus()
        if initial:
            self.state.set_value(initial)
        self.blink_on = True
        self._elapsed = 0.0

    # ── state ───────────────────────────────────────────────────────────────
    @property
    def value(self) -> str:
        return str(self.state.value())

    @property
    def cursor_pos(self) -> int:
        return int(self.state.cursor_pos)

    def set_value(self, value: str) -> None:
        self.state.set_value(value)

    def is_aborted(self) -> bool:
        return bool(self.state.is_aborted())

    def is_complete(self) -> bool:
        return bool(self.state.is_complete())

    def render(self) -> Text:
        return Text(self.value or self.placeholder)

    def tick(self, dt: float) -> None:
        self._elapsed += dt
        while self._elapsed >= CURSOR_BLINK_SECONDS:
            self._elapsed -= CURSOR_BLINK_SECONDS
            self.blink_on = not self.blink_on

    def handle_key(self, key: Key) -> bool:
        return bool(self.state.handle_key(key))

    # ── painting ────────────────────────────────────────────────────────────
    def build_lines(self, width: int, theme: TuiTheme, *, framed: bool = True) -> list[StyledLine]:
        """The tall border plus the text row — or just the row when bare."""

        if not framed or width < 2:
            return [self._content_line(width, theme)]
        border = _style(fg=theme.primary, bg=theme.surface)
        inner = width - 2
        return [
            [("▊", border), ("▔" * inner, border), ("▎", border)],
            [("▊", border), *self._content_line(inner, theme), ("▎", border)],
            [("▊", border), ("▁" * inner, border), ("▎", border)],
        ]

    def _content_line(self, width: int, theme: TuiTheme) -> StyledLine:
        """The text row: the buffer with a reversed block over the cursor cell."""

        if width <= 0:
            return []
        body = _style(fg=theme.foreground, bg=theme.surface)
        placeholder = _style(fg=theme.text_disabled, bg=theme.surface)
        cursor = _style(fg=_auto_text(theme.primary), bg=theme.primary, bold=True)
        value = self.value
        if not value:
            # The cursor cell is always the first cell and the placeholder always
            # starts at the second, blink or not: a placeholder that jumped one
            # column every half second would read as a rendering glitch.
            head: StyledLine = [(" ", cursor)]
            if not self.placeholder:
                return _fit(head, width, body)
            tail: Stripe = (self.placeholder, placeholder)
            return _fit([*head, tail], width, body)

        chars = list(value)
        offsets = [0]
        for char in chars:
            offsets.append(offsets[-1] + cell_len(char))
        cursor_index = max(0, min(self.cursor_pos, len(chars)))
        if not self.blink_on:
            start = 0
            while start < cursor_index and offsets[cursor_index] - offsets[start] >= width:
                start += 1
            return _fit([("".join(chars[start:]), body)], width, body)

        # Keep the cursor cell visible: scroll the buffer left when the text
        # before the cursor is wider than the row.
        start = 0
        while start < cursor_index and offsets[cursor_index] - offsets[start] >= width:
            start += 1
        head = "".join(chars[start:cursor_index])
        tail = "".join(chars[cursor_index:])
        if tail:
            pieces: StyledLine = [(head, body), (tail[0], cursor), (tail[1:], body)]
        else:
            pieces = [(head, body), (" ", cursor)]
        return _fit(pieces, width, body)


class SettingRow(Node):
    """One ``Field: value`` line of the provider settings popup.

    Activating it (a click or Enter) fires :attr:`on_activate` — the popup owns
    the picker. A row with nothing to pick from — a model that offers no
    reasoning levels — takes no clicks and paints muted, so it reads as
    unavailable instead of silently doing nothing.
    """

    class Activated(NamedTuple):
        """``SettingRow.Activated``, minus the message pump."""

        row: SettingRow

    def __init__(self, field_name: str, value: str, *, enabled: bool = True) -> None:
        super().__init__(classes=() if enabled else ("-disabled",))
        self._field = field_name
        self._value = value
        self._enabled = enabled
        self.on_activate: Callable[[SettingRow], None] | None = None

    # ── state ───────────────────────────────────────────────────────────────
    @property
    def field(self) -> str:
        return self._field

    @property
    def value(self) -> str:
        return self._value

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def plain(self) -> str:
        return f"{self._field.capitalize()}: {self._value or '-'}"

    def set_value(self, value: str, *, enabled: bool | None = None) -> None:
        """Repaint the row; ``enabled`` is only touched when it is passed."""

        self._value = value
        if enabled is not None and enabled != self._enabled:
            self._enabled = enabled
            self.set_class(not enabled, "-disabled")

    def render(self) -> Text:
        return Text(self.plain)

    # ── input ───────────────────────────────────────────────────────────────
    def activated(self) -> None:
        """Fire the activation callback — only while the row is enabled."""

        if self._enabled and self.on_activate is not None:
            self.on_activate(self)

    def on_click(self) -> None:
        self.activated()

    def key_enter(self) -> None:
        self.on_click()

    def handle_key(self, key: Key) -> bool:
        if key.code == "Enter":
            self.key_enter()
            return True
        return False

    def handle_mouse(self, ev: Mouse) -> bool:
        if not self.region.contains(ev.x, ev.y):
            if ev.kind == "move":
                self.hovered = False
            return False
        if ev.kind == "move":
            self.hovered = True
            return False
        if ev.kind == "down" and ev.button == 0:
            self.on_click()
            return True
        return False

    # ── painting ────────────────────────────────────────────────────────────
    def background(self, theme: TuiTheme) -> str:
        """The row's tint: focus 22%, hover 12%, otherwise the surface."""

        if self.focused:
            return blend(theme.primary, theme.surface, 0.22)
        if self.hovered:
            return blend(theme.primary, theme.surface, 0.12)
        return theme.surface

    def build_lines(self, width: int, theme: TuiTheme, *, bg: str | None = None) -> list[StyledLine]:
        """One row, painted edge to edge with its hover / focus tint."""

        background = bg if bg is not None else self.background(theme)
        fg = _text_hex(theme) if self._enabled else theme.text_muted
        style = _style(fg=fg, bg=background)
        return [_fit([(f" {self.plain}", style)], width, style)]


# ─── the modal base ──────────────────────────────────────────────────────────


class Modal(Node):
    """A full-screen modal holding one centered dialog.

    Subclasses build their dialog subtree in ``__init__`` — so ``query`` /
    ``query_one`` work before the first frame, exactly as a mounted Textual
    screen does — and implement :meth:`content_rows` (the dialog's inner rows for
    a given row budget) and :meth:`place` (each child's region, for mouse
    hit-testing). The frame around them — thick ``█▀▄`` primary border,
    ``padding 1 2``, ``$surface`` body — is drawn here once for every dialog.
    """

    border_width = DIALOG_WIDTH
    """The old CSS ``width: 60`` on every ``*-dialog`` container."""

    padding_x = 2
    """The horizontal half of the old ``padding: 1 2``."""

    def __init__(self, *, id: str, dialog_id: str) -> None:
        super().__init__(id=id)
        self._app: Any = None
        self.on_close: Callable[[Any], None] | None = None
        self.result: Any = None
        self.dialog = self.add(Node(id=dialog_id))
        self._dismissed = False
        self._future: asyncio.Future | None = None
        self._area = Rect(0, 0, DIALOG_WIDTH, 24)

    # ── lifecycle ───────────────────────────────────────────────────────────
    @property
    def app(self) -> Any:
        """The app the modal was pushed on (``None`` until then)."""

        return self._app

    @app.setter
    def app(self, app: Any) -> None:
        """Assigning the app mounts the modal.

        ``push_screen`` sets ``screen.app`` and then awaits an "is mounted"
        message; in the port the assignment IS the mount, so the app layer only
        has to hand the modal over.
        """

        mounted = app is not None and self._app is None
        self._app = app
        if mounted:
            self.on_mount()

    def attach(self, app: Any, on_close: Callable[[Any], None] | None = None) -> None:
        """Bind the modal to its app and its close callback, and mount it."""

        self.on_close = on_close
        self.app = app

    def on_mount(self) -> None:
        """Called once the app pushes the modal (subclasses may override)."""

    def wait_result(self) -> Awaitable[Any]:
        """The future ``push_screen_wait`` awaits; resolved by :meth:`dismiss`."""

        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
            if self._dismissed:
                self._future.set_result(self.result)
        return self._future

    @property
    def dismissed(self) -> bool:
        return self._dismissed

    def _settle(self, result: Any) -> None:
        """Run the close callback and resolve the waiter — exactly ONCE."""

        if self._dismissed:
            return
        self._dismissed = True
        self.result = result
        close, self.on_close = self.on_close, None
        if close is not None:
            close(result)
        future, self._future = self._future, None
        if future is not None and not future.done():
            future.set_result(result)

    def dismiss(self, result: Any = None) -> None:
        """Close the modal, handing ``result`` to the callback and the waiter.

        The screen is popped FIRST: a callback may push the next screen (the
        two-prompt caps editor does), and that one has to land on top of the
        screen UNDER this one rather than on top of this one.
        """

        if self._dismissed:
            return
        pop = getattr(self.app, "pop_screen", None)
        if callable(pop):
            pop(result)
        self._settle(result)

    def tick(self, dt: float) -> None:
        for node in self.walk():
            if isinstance(node, TextInput):
                node.tick(dt)

    # ── pushing nested screens ──────────────────────────────────────────────
    def _push_wait(self, screen: Modal) -> Awaitable[Any]:
        """Push ``screen`` and await its result — ``push_screen_wait``'s contract.

        The app owns the screen stack and implements ``push_screen_wait``; this
        fallback keeps the flows working through ``push_screen`` plus the modal's
        own future, which is exactly what that helper does, for an app (or a test
        double) that does not implement it.
        """

        waiter = getattr(self.app, "push_screen_wait", None)
        if callable(waiter):
            return waiter(screen)
        self.app.push_screen(screen)
        return screen.wait_result()

    # ── geometry ────────────────────────────────────────────────────────────
    def dialog_width(self, area: Rect) -> int:
        return max(8, min(self.border_width, area.width))

    def max_dialog_height(self, area: Rect) -> int:
        """``max-height: 80%`` of the screen."""

        return max(3, int(area.height * 0.8))

    def content_width(self, area: Rect) -> int:
        return max(1, self.dialog_width(area) - 2 - 2 * self.padding_x)

    def row_budget(self, area: Rect) -> int:
        """How many inner rows fit: 80% of the screen minus border + padding."""

        return max(1, self.max_dialog_height(area) - 2 - 2)

    # ── painting ────────────────────────────────────────────────────────────
    def content_rows(self, width: int, theme: TuiTheme, budget: int) -> list[StyledLine]:
        """The dialog's inner rows, each exactly ``width`` cells.

        ``budget`` is how many rows fit inside the 80%-of-screen cap; a
        scrolling list clips itself to it.
        """

        return []

    def place(self, inner: Rect) -> None:
        """Assign every dialog child a region (for mouse hit-testing)."""

    def build_dialog(self, area: Rect, theme: TuiTheme) -> list[StyledLine]:
        """The centered dialog: its border, padding and the subclass's rows."""

        width = self.dialog_width(area)
        inner_width = self.content_width(area)
        budget = self.row_budget(area)
        rows = self.content_rows(inner_width, theme, budget)[:budget]
        height = min(self.max_dialog_height(area), len(rows) + 2 + 2)
        x, y = _centered_origin(area, width, height)
        self.region = Rect(x, y, width, height)
        inner = Rect(x + 1 + self.padding_x, y + 2, inner_width, max(0, height - 4))
        self.dialog.region = inner
        self.place(inner)
        return self._frame(rows, width, height, theme)

    def _frame(
        self, rows: list[StyledLine], width: int, height: int, theme: TuiTheme
    ) -> list[StyledLine]:
        """Border + ``padding 1 2`` + rows, exactly ``height`` rows tall."""

        border = _style(fg=theme.primary, bg=theme.surface)
        surface = _style(bg=theme.surface)
        inner_width = max(0, width - 2)
        pad = " " * self.padding_x
        body_width = max(0, width - 2 - 2 * self.padding_x)
        blank = _pad(surface, body_width)
        content = [blank, *rows, blank][: max(0, height - 2)]
        content.extend(blank for _ in range(max(0, height - 2) - len(content)))
        frame: list[StyledLine] = [[("█", border), ("▀" * inner_width, border), ("█", border)]]
        for line in content:
            frame.append(
                [("█", border), (pad, surface), *line, (pad, surface), ("█", border)]
            )
        frame.append([("█", border), ("▄" * inner_width, border), ("█", border)])
        return frame

    def build_lines(
        self,
        area: Any,
        theme: TuiTheme,
        behind: Sequence[StyledLine] | None = None,
    ) -> list[StyledLine]:
        """The WHOLE area: the dimmed backdrop plus the central dialog.

        ``behind`` is the app's composed screen; passing it dims those rows to
        ``$background 60%`` underneath the dialog, which is what a Textual modal
        screen does. Without it the backdrop is a plain background fill.
        """

        rect = _as_rect(area)
        self._area = rect
        backdrop = _style(bg=theme.background)
        base: list[StyledLine] = []
        for index in range(rect.height):
            if behind is not None and index < len(behind):
                base.append(dim_line(list(behind[index]), theme))
            else:
                base.append(_pad(backdrop, rect.width))
        dialog = self.build_dialog(rect, theme)
        top = self.region.y - rect.y
        left = self.region.x - rect.x
        for index, line in enumerate(dialog):
            row = top + index
            if not 0 <= row < len(base):
                continue
            span = _cells(line)
            # Pad head/tail to their EXACT cell widths. A CJK (2-cell) glyph
            # straddling the dialog edge is dropped whole by _slice_cells, so
            # the head can come back one cell short — without the pad the whole
            # dialog row shifts left by that cell and the border jags row to row.
            head = _fit(_slice_cells(base[row], 0, left), left, backdrop)
            tail = _fit(
                _slice_cells(base[row], left + span, rect.width),
                rect.width - left - span,
                backdrop,
            )
            base[row] = _fit([*head, *line, *tail], rect.width, backdrop)
        return base

    def render(self, theme: TuiTheme | None = None) -> Text:
        """The dialog's content rows as Rich text — the ``.render().plain`` face.

        The one Textual-shaped method the ported tests read: ``.render().plain``
        is the dialog's content, line by line, without the border. A theme is
        optional because only the TEXT is asserted; the tones need one to be
        built at all, so :data:`_PLAIN_THEME` stands in when none is given.
        """

        width = max(1, self.border_width - 2 - 2 * self.padding_x)
        rows = self.content_rows(width, theme or _PLAIN_THEME, LIST_MAX_ROWS)
        return _render_lines(rows)

    # ── input ───────────────────────────────────────────────────────────────
    def handle_key(self, key: Key) -> bool:
        if key.code == "Esc":
            self.dismiss(None)
        return True

    def handle_mouse(self, ev: Mouse) -> bool:
        """A modal owns the whole screen: no report ever falls through to the app.

        Textual's ``ModalScreen`` covers the terminal exactly, so a click beside
        the dialog is a click on the SCREEN — swallowed, not dispatched to the
        chat log underneath. A picker that wants click-outside-closes (the
        command palette) overrides this.
        """

        for child in self.dialog.children:
            if child.handle_mouse(ev):
                return True
        return True

    def app_size(self) -> tuple[int, int]:
        """The app's ``(width, height)``, or the last painted area's."""

        size = getattr(self.app, "size", None)
        width = getattr(size, "width", None)
        height = getattr(size, "height", None)
        if isinstance(width, int) and isinstance(height, int):
            return (width, height)
        return (self._area.width or self.border_width, self._area.height or 24)


_PLAIN_THEME = TuiTheme(
    name="plain",
    dark=True,
    background="#000000",
    surface="#000000",
    panel="#000000",
    primary="#ffffff",
    accent="#ffffff",
    secondary="#ffffff",
    success="#ffffff",
    warning="#ffffff",
    error="#ffffff",
    foreground="#ffffff",
    boost="#000000",
    text_muted="#ffffff",
    text_disabled="#ffffff",
    text_answer="#ffffff",
)
"""Fallback palette for :meth:`Modal.render`, which only needs the TEXT.

The app always passes a real theme to ``build_lines``; this one exists so the
test surface (``.render().plain``) works on a modal that was never painted.
"""


# ─── SelectionModal ──────────────────────────────────────────────────────────


class SelectionModal(Modal):
    """A modal picker. Returns the selected string, or ``None`` on cancel.

    ``allow_none=True`` adds a "(none)" entry that returns ``None``.
    ``sections`` renders non-selectable header rows above grouped ``(value,
    label)`` options; the default (no sections) renders a flat list, unchanged.
    ``extra_options`` appends ``(sentinel, label)`` rows the caller recognises
    by sentinel — the two "add a provider" entries are exactly that.
    """

    def __init__(
        self,
        title: str,
        options: Sequence[str],
        *,
        current: str = "",
        allow_none: bool = False,
        none_label: str = "(none — clear)",
        extra_options: Sequence[tuple[str, str]] | None = None,
        sections: Sequence[tuple[str, Sequence[tuple[str, str]]]] | None = None,
        dialog_width: int = DIALOG_WIDTH,
    ) -> None:
        super().__init__(id="selection-modal", dialog_id="selection-dialog")
        self._title = title
        self._options = list(options)
        self._current = current
        self._allow_none = allow_none
        self._none_label = none_label
        self._extra_options = list(extra_options or [])
        self._sections = [(header, list(opts)) for header, opts in (sections or [])]
        self.border_width = max(8, dialog_width)
        self._row_values: dict[ListItem, str | None] = {}
        self._row_index_by_value: dict[str, int] = {}
        self.title_label = self.dialog.add(
            Label(title, "selection-title", align="center", bold=True)
        )
        self.list = self.dialog.add(SelectionList("selection-list"))
        self.hint = self.dialog.add(
            Label(SELECTION_HINT, "selection-hint", tone="muted", align="center")
        )
        self.rows: list[ListItem] = []
        self._compose_rows()
        self._preselect()

    # ── composition ─────────────────────────────────────────────────────────
    def _compose_rows(self) -> None:
        """Build the rows in the old order: none, sections, options, extras."""

        row = 0
        if self._allow_none:
            self._add_row(ListItem(Label(self._none_label), id="row-none"), None, index=row)
            row = 1
        for header, opts in self._sections:
            if not opts:
                continue
            header_item = ListItem(Label(header), id=f"row-{row}", classes=("-header",))
            self._add_row(header_item, _NO_VALUE, index=row)
            row += 1
            for value, label in opts:
                row = self._add_option(label, value, row=row, mark=value == self._current)
        for option in self._options:
            row = self._add_option(option, option, row=row, mark=option == self._current)
        for sentinel, label in self._extra_options:
            row = self._add_option(label, sentinel, row=row, mark=False)

    def _add_option(self, label: str, value: str, *, row: int, mark: bool) -> int:
        marker = "● " if mark else "  "
        self._add_row(ListItem(Label(marker + label), id=f"row-{row}"), value, index=row)
        return row + 1

    def _add_row(self, item: ListItem, value: Any, *, index: int) -> None:
        self.rows.append(item)
        self.list.append(item)
        if value is _NO_VALUE:
            return
        self._row_values[item] = value
        if value is not None:
            self._row_index_by_value[value] = index

    def _preselect(self) -> None:
        """Pre-select the current value, else the first selectable row.

        `ListView` mounts with `initial_index=0`, so a picker whose value is not
        in the list still opens with the FIRST row highlighted — never with
        nothing highlighted, which would make Enter a no-op.
        """

        index = self._row_index_by_value.get(self._current) if self._current else None
        self.list.set_index(index if index is not None else self.list.first_selectable())


        if not self._current:
            return
        index = self._row_index_by_value.get(self._current)
        if index is not None:
            self.list.set_index(index)

    # ── state ───────────────────────────────────────────────────────────────
    @property
    def title_text(self) -> str:
        return self._title

    @property
    def index(self) -> int | None:
        return self.list.index

    @property
    def hint_text(self) -> str:
        return self.hint.plain

    def highlighted_value(self) -> Any:
        """The highlighted row's value, or ``_NO_VALUE`` for a header."""

        row = self.list.highlighted()
        if row is None:
            return _NO_VALUE
        return self._row_values.get(row, _NO_VALUE)

    def select(self, value: str | None) -> None:
        """Move the highlight onto the row holding ``value`` (if any)."""

        if value is None:
            return
        index = self._row_index_by_value.get(value)
        if index is not None:
            self.list.set_index(index)

    def list_rows(self, budget: int) -> int:
        """``max-height 20``, clipped by the dialog's own row budget."""

        return max(1, min(LIST_MAX_ROWS, budget - 4))

    # ── painting ────────────────────────────────────────────────────────────
    def content_rows(self, width: int, theme: TuiTheme, budget: int) -> list[StyledLine]:
        """Title, blank, the list (``$boost``), blank, the hint."""

        surface = _style(bg=theme.surface)
        rows = self.list_rows(budget)
        return [
            *self.title_label.build_lines(width, theme, bg=theme.surface),
            _pad(surface, width),
            *self.list.build_lines(width, theme, bg=theme.boost, max_rows=rows),
            _pad(surface, width),
            *self.hint.build_lines(width, theme, bg=theme.surface),
        ]

    def place(self, inner: Rect) -> None:
        rows = self.list_rows(self.row_budget(self._area))
        self.title_label.region = Rect(inner.x, inner.y, inner.width, 1)
        self.list.region = Rect(inner.x, inner.y + 2, inner.width, rows)
        for offset, (_, row) in enumerate(self.list.window(rows)):
            row.region = Rect(inner.x, self.list.region.y + offset, inner.width, 1)
            row.label.region = row.region
        self.hint.region = Rect(inner.x, self.list.region.bottom + 1, inner.width, 1)

    # ── input ───────────────────────────────────────────────────────────────
    def handle_key(self, key: Key) -> bool:
        if key.code == "Esc":
            self.dismiss(None)
        elif key.code == "Enter":
            value = self.highlighted_value()
            if value is not _NO_VALUE:
                self.dismiss(value)
        elif key.code == "Down":
            self.list.set_index(self.list.step(1))
        elif key.code == "Up":
            self.list.set_index(self.list.step(-1))
        elif key.code == "Home":
            self.list.set_index(self.list.first_selectable())
        elif key.code == "End":
            self.list.set_index(self.list.last_selectable())
        elif key.code == "PageDown":
            self._page(1)
        elif key.code == "PageUp":
            self._page(-1)
        return True

    def _page(self, direction: int) -> None:
        """Page by the visible row count, landing on a selectable row."""

        rows = self.list_rows(self.row_budget(self._area))
        target = (self.list.index if self.list.index is not None else 0) + direction * rows
        target = max(0, min(target, len(self.list.rows) - 1))
        while 0 <= target < len(self.list.rows) and not self.list.rows[target].selectable:
            target += direction
        if 0 <= target < len(self.list.rows):
            self.list.set_index(target)

    def handle_mouse(self, ev: Mouse) -> bool:
        rows = self.list_rows(self.row_budget(self._area))
        if ev.kind in ("wheel_up", "wheel_down") and self.list.region.contains(ev.x, ev.y):
            self.list.scroll_by(1 if ev.kind == "wheel_down" else -1, rows)
            return True
        if ev.kind == "down" and ev.button == 0 and self.region.contains(ev.x, ev.y):
            for index, row in self.list.window(rows):
                if not row.region.contains(ev.x, ev.y):
                    continue
                if not row.selectable:
                    return True
                self.list.set_index(index)
                self.dismiss(self._row_values.get(row))
                return True
        return super().handle_mouse(ev)


# ─── TextInputModal ──────────────────────────────────────────────────────────


class TextInputModal(Modal):
    """A modal single-line editor. Returns the entered string, or ``None``.

    An empty submission returns ``""`` (which the caller treats as "clear"). The
    readline key handling — Home/End/Ctrl+A/Ctrl+E/Ctrl+U/Ctrl+K/Ctrl+B/Ctrl+F,
    Backspace, Delete, Left, Right — is pyratatui's ``TextState``: the exact
    editor a Textual ``Input`` gives, without a terminal.
    """

    def __init__(
        self,
        title: str,
        *,
        initial: str = "",
        placeholder: str = "",
        hint: str = TEXT_INPUT_HINT,
    ) -> None:
        super().__init__(id="text-modal", dialog_id="text-dialog")
        self._title = title
        self._hint_text = hint
        self.title_label = self.dialog.add(Label(title, "text-title", align="center", bold=True))
        self.input = self.dialog.add(
            TextInput(initial=initial, placeholder=placeholder, id="text-input")
        )
        self.hint = self.dialog.add(Label(hint, "text-hint", tone="muted", align="center"))

    # ── state ───────────────────────────────────────────────────────────────
    @property
    def title_text(self) -> str:
        return self._title

    @property
    def value(self) -> str:
        return self.input.value

    @property
    def hint_text(self) -> str:
        return self._hint_text

    @property
    def placeholder(self) -> str:
        return self.input.placeholder

    def set_value(self, value: str) -> None:
        self.input.set_value(value)

    # ── painting ────────────────────────────────────────────────────────────
    def content_rows(self, width: int, theme: TuiTheme, budget: int) -> list[StyledLine]:
        """Title, blank, the framed input (3 rows), blank, the hint."""

        surface = _style(bg=theme.surface)
        return [
            *self.title_label.build_lines(width, theme, bg=theme.surface),
            _pad(surface, width),
            *self.input.build_lines(width, theme),
            _pad(surface, width),
            *self.hint.build_lines(width, theme, bg=theme.surface),
        ][:budget]

    def place(self, inner: Rect) -> None:
        self.title_label.region = Rect(inner.x, inner.y, inner.width, 1)
        self.input.region = Rect(inner.x, inner.y + 2, inner.width, 3)
        self.hint.region = Rect(inner.x, inner.y + 6, inner.width, 1)

    # ── input ───────────────────────────────────────────────────────────────
    def handle_key(self, key: Key) -> bool:
        if key.code == "Esc":
            self.dismiss(None)
            return True
        self.input.handle_key(key)
        if self.input.is_complete():
            self.dismiss(self.input.value)
        elif self.input.is_aborted():
            self.dismiss(None)
        return True

    def handle_mouse(self, ev: Mouse) -> bool:
        if ev.kind == "down" and self.input.region.contains(ev.x, ev.y):
            self.input.focus()
        return super().handle_mouse(ev)


# ─── ProviderSettingsPopup ───────────────────────────────────────────────────


class ProviderSettingsPopup(Modal):
    """Provider / Model / Reasoning for one actor, as clickable rows.

    ONE popup serves both chips: ``target`` is the trio it edits (outo's or the
    ``subagent_*`` trio) and ``session`` is the pinned session every pick is
    written through, so the edit reaches this conversation even while another
    process moves the global default.

    Provider and Model both reset Reasoning: an effort level belongs to a
    model, so picking either lands on that model's default (the provider
    record's own preference, else modelslist's auto default). Reasoning is one
    more click away whenever that default is not what the user wants.

    A fourth row exists below the trio, and WHICH one depends on the provider the
    trio runs on: ``Caps`` for a hand-added provider, ``Compatibility`` for a
    catalog one — mutually exclusive. Both are endpoint properties: unlike the
    trio rows they write the provider RECORD alone, globally, never a session
    pin.
    """

    def __init__(
        self,
        target: TrioTarget,
        *,
        session: str,
        on_change: Callable[[], None],
    ) -> None:
        super().__init__(id="provider-settings", dialog_id="provider-settings-dialog")
        self._target = target
        self._session = session
        self._on_change = on_change
        self.title_label = self.dialog.add(
            Label(target.title, "provider-settings-title", align="center", bold=True)
        )
        self.rows_box = self.dialog.add(Node(id="provider-settings-rows"))
        self.status = self.dialog.add(
            Label("", "provider-settings-status", tone="error")
        )
        self.hint = self.dialog.add(
            Label(PROVIDER_HINT, "provider-settings-hint", tone="muted", align="center")
        )
        self._focus_index = 0
        self._mount_trio_rows()
        self._sync_endpoint_row()
        self._apply_focus()

    # ── state ───────────────────────────────────────────────────────────────
    @property
    def target(self) -> TrioTarget:
        return self._target

    @property
    def session(self) -> str:
        return self._session

    @property
    def title_text(self) -> str:
        return self._target.title

    @property
    def rows(self) -> list[SettingRow]:
        """The popup's rows in display order (the trio first, endpoint last)."""

        return list(self.rows_box.query(SettingRow))

    def row(self, field_name: str) -> SettingRow | None:
        """The row editing ``field_name``, or ``None`` when it is not shown."""

        return next((row for row in self.rows if row.field == field_name), None)

    def row_text(self, field_name: str) -> str:
        row = self.row(field_name)
        if row is None:
            raise LookupError(f"no {field_name} row")
        return row.render().plain

    @property
    def status_text(self) -> str:
        return self.status.plain

    # ── composition ─────────────────────────────────────────────────────────
    def _mount_trio_rows(self) -> None:
        config = _effective_config(self._session)
        provider, model, reasoning = _trio_display(self._target, config)
        target_provider, target_model = _reasoning_target(self._target, config)
        self._mount_row(SettingRow("provider", provider))
        self._mount_row(SettingRow("model", model))
        self._mount_row(
            SettingRow(
                "reasoning",
                reasoning,
                enabled=self._reasoning_enabled(target_provider, target_model),
            )
        )

    def _mount_row(self, row: SettingRow) -> SettingRow:
        row.on_activate = self._activated
        return self.rows_box.add(row)

    def _apply_focus(self) -> None:
        if not self.rows:
            return
        self._focus_index = max(0, min(self._focus_index, len(self.rows) - 1))
        for index, row in enumerate(self.rows):
            row.focused = index == self._focus_index and row.enabled

    def on_mount(self) -> None:
        self.refresh_rows()

    def on_resume(self) -> None:
        # Coming back from a nested picker — including one the provider-ADD
        # flows pushed on this screen — the rows must show what is configured
        # now, not what it was when the popup opened.
        self.refresh_rows()

    def refresh_rows(self) -> None:
        """Re-read the trio and repaint the rows in place, syncing the endpoint row."""

        config = _effective_config(self._session)
        provider, model, reasoning = _trio_display(self._target, config)
        target_provider, target_model = _reasoning_target(self._target, config)
        self._set_row("provider", provider)
        self._set_row("model", model)
        self._set_row(
            "reasoning",
            reasoning,
            enabled=self._reasoning_enabled(target_provider, target_model),
        )
        self._sync_endpoint_row()
        self._apply_focus()

    def _set_row(self, field_name: str, value: str, *, enabled: bool | None = None) -> None:
        row = self.row(field_name)
        if row is None:  # pragma: no cover - the trio rows are always mounted
            return
        row.set_value(value, enabled=enabled)

    def _sync_endpoint_row(self) -> None:
        """Keep exactly ONE endpoint row, matching the selected provider's kind.

        A provider is either a catalog one (``Compatibility``) or a hand-added one
        (``Caps``), never both, so a provider change removes the old row and
        mounts the new one.
        """

        catalog = self._compatibility_record()
        if catalog is not None:
            self._sync_row(COMPATIBILITY_FIELD, catalog.catalog_dialect or NATIVE_DIALECT_LABEL)
            self._drop_row(CAPS_FIELD)
            return
        custom = self._caps_record()
        if custom is not None:
            self._sync_row(CAPS_FIELD, _caps_value(custom))
            self._drop_row(COMPATIBILITY_FIELD)
            return
        self._drop_row(CAPS_FIELD)
        self._drop_row(COMPATIBILITY_FIELD)

    def _sync_row(self, field_name: str, value: str) -> None:
        row = self.row(field_name)
        if row is None:
            self._mount_row(SettingRow(field_name, value))
            return
        row.set_value(value)

    def _drop_row(self, field_name: str) -> None:
        row = self.row(field_name)
        if row is not None:
            row.remove()

    # ── painting ────────────────────────────────────────────────────────────
    def content_rows(self, width: int, theme: TuiTheme, budget: int) -> list[StyledLine]:
        """Title, blank, the rows, the status line, blank, the hint."""

        surface = _style(bg=theme.surface)
        lines: list[StyledLine] = [
            *self.title_label.build_lines(width, theme, bg=theme.surface),
            _pad(surface, width),
        ]
        for row in self.rows:
            lines.extend(row.build_lines(width, theme))
        lines.extend(
            [
                *self.status.build_lines(width, theme, bg=theme.surface),
                _pad(surface, width),
                *self.hint.build_lines(width, theme, bg=theme.surface),
            ]
        )
        return lines[:budget]

    def place(self, inner: Rect) -> None:
        self.title_label.region = Rect(inner.x, inner.y, inner.width, 1)
        top = inner.y + 2
        for index, row in enumerate(self.rows):
            row.region = Rect(inner.x, top + index, inner.width, 1)
        self.status.region = Rect(inner.x, top + len(self.rows), inner.width, 1)
        self.hint.region = Rect(inner.x, top + len(self.rows) + 2, inner.width, 1)

    # ── input ───────────────────────────────────────────────────────────────
    def handle_key(self, key: Key) -> bool:
        if key.code == "Esc":
            self.dismiss(None)
        elif key.code in ("Down", "Tab"):
            self._move_focus(1)
        elif key.code in ("Up", "BackTab"):
            self._move_focus(-1)
        elif key.code == "Enter" and self.rows:
            self.rows[self._focus_index].key_enter()
        return True

    def _move_focus(self, direction: int) -> None:
        """Cycle the row focus, skipping rows that have nothing to open."""

        if not self.rows:
            return
        index = self._focus_index
        for _ in range(len(self.rows)):
            index = (index + direction) % len(self.rows)
            if self.rows[index].enabled:
                break
        self._focus_index = index
        self._apply_focus()

    def handle_mouse(self, ev: Mouse) -> bool:
        if ev.kind in ("move", "down"):
            hit = next((row for row in self.rows if row.region.contains(ev.x, ev.y)), None)
            for row in self.rows:
                row.hovered = row is hit and ev.kind == "move"
            if hit is None:
                return super().handle_mouse(ev)
            if ev.kind == "down" and ev.button == 0:
                self._focus_index = self.rows.index(hit)
                self._apply_focus()
                hit.on_click()
            return True
        return super().handle_mouse(ev)

    # ── picking ─────────────────────────────────────────────────────────────
    def _values(self) -> tuple[str, str, str]:
        return _trio(_effective_config(self._session), self._target.fields)

    def _apply(self, values: tuple[str, str, str]) -> None:
        """Persist one edit (``config set --target``), then repaint the rows.

        The write is the CLI's; it runs on the app's worker because a
        subprocess round-trip must not freeze the key handler that confirmed
        the pick. The rows repaint when it lands, so what they show is what
        storage now says — never a pre-write guess.
        """

        provider, model, reasoning = values
        which = "subagent" if self._target.inherits else "outo"
        self._run(self._apply_flow(provider, model, reasoning, which))

    async def _apply_flow(
        self, provider: str, model: str, reasoning: str, which: str
    ) -> None:
        ok, message = await asyncio.to_thread(
            bridge.config_set_trio, self._session, which, provider, model, reasoning
        )
        if not ok:
            self._report(message or "config set failed")
            return
        self._report("")
        self.refresh_rows()
        self._on_change()

    def _reasoning_enabled(self, provider: str, model: str) -> bool:
        return bool(model) and _reasoning_choices(model, provider) is not None

    def _report(self, message: str) -> None:
        """Surface why a row could not open its picker, inside the popup."""

        self.status.update(message)

    def _activated(self, row: SettingRow) -> None:
        """Route a row activation to its flow (the old ``on_setting_row_activated``)."""

        if row.field == "provider":
            self._pick_provider()
        elif row.field == "model":
            self._pick_model()
        elif row.field == CAPS_FIELD:
            self._pick_caps()
        elif row.field == COMPATIBILITY_FIELD:
            self._pick_compatibility()
        else:
            self._pick_reasoning()

    def _pick_provider(self) -> None:
        # The picker opens EVEN when no provider is configured: the "add"
        # entries below are the only way to create the first one, so an empty
        # list must not short-circuit into a dead-end message.
        options = sorted(provider_store.load_all())

        def on_close(result: str | None) -> None:
            if not result:
                return
            if result == SENTINEL_CATALOG_ADD:
                self._run(self.app._catalog_add_flow())
                return
            if result == SENTINEL_CUSTOM_ADD:
                self.app._open_custom_add_wizard()
                return
            # A new provider invalidates the other two rows: model and reasoning
            # belong to it, not to the one being left behind.
            self._apply((result, *_model_defaults(result)))

        self.app.push_screen(
            SelectionModal(
                "Select provider" if options else "No providers yet — add one",
                options,
                current=self._values()[0],
                allow_none=False,
                extra_options=[
                    (SENTINEL_CATALOG_ADD, "+ add from catalog…"),
                    (SENTINEL_CUSTOM_ADD, "+ add custom…"),
                ],
            ),
            on_close,
        )

    def _pick_model(self) -> None:
        provider, model, _ = self._values()
        if not provider:
            self._report("Pick a provider first.")
            return
        self._run(self._model_flow(provider, model))

    async def _model_flow(self, provider: str, current: str) -> None:
        """The model picker, with a free-text fallback for an unknown provider.

        A hand-added endpoint need not exist in the modelslist catalog at all, so
        "no models" is answered by asking for the id rather than by a dead end.
        """

        options: list[str] = []
        try:
            models = await asyncio.to_thread(bridge.list_catalog_models, provider)
            if models is None:
                raise RuntimeError("provider models exited non-zero")
            # Preserve the catalog's curated / provider-native order.
            options = [f"{model.get('id', '?')} — {model.get('name', '')}" for model in models]
        except Exception as exc:
            self._report(f"model list unavailable ({exc}); type the model id instead")
        if options:
            current_display = next(
                (option for option in options if option.split(" — ", 1)[0] == current), ""
            )
            picked = await self._push_wait(
                SelectionModal(
                    f"Model ({provider}, {len(options)})",
                    options,
                    current=current_display,
                    allow_none=False,
                )
            )
        else:
            picked = await self._push_wait(
                TextInputModal(
                    f"Model id — {provider}",
                    initial=current,
                    placeholder="model id",
                    hint="Enter to save · Esc to cancel",
                )
            )
        model_id = (picked or "").split(" — ", 1)[0].strip()
        if not model_id:
            return
        resolved, reasoning = _model_defaults(provider, model_id)
        self._apply((provider, resolved, reasoning))

    def _pick_reasoning(self) -> None:
        config = _effective_config(self._session)
        provider, model = _reasoning_target(self._target, config)
        choices = _reasoning_choices(model, provider)
        if not choices:
            self._report(f"{model or 'the current model'} offers no reasoning levels.")
            return
        extra = [(SENTINEL_SAME_AS_OUTO, "same as outo")] if self._target.inherits else []

        def on_close(result: str | None) -> None:
            if result is None:
                return
            provider_value, model_value, _ = self._values()
            # "same as outo" is stored as an empty value, which is exactly what
            # both stores read as "follow the default".
            reasoning = "" if result == SENTINEL_SAME_AS_OUTO else result
            self._apply((provider_value, model_value, reasoning))

        self.app.push_screen(
            SelectionModal(
                f"Reasoning ({model})",
                choices,
                current=self._values()[2] or "off",
                allow_none=False,
                extra_options=extra,
            ),
            on_close,
        )

    def _compatibility_record(self) -> provider_store.Provider | None:
        """The effective catalog provider, including for an inheriting trio."""

        provider, _ = _reasoning_target(self._target, _effective_config(self._session))
        record = _record(provider)
        if record is None or not is_catalog_source(record.source):
            return None
        return record

    def _pick_compatibility(self) -> None:
        record = self._compatibility_record()
        if record is None:
            self._report("Compatibility is selectable for catalog providers only.")
            return
        self._run(self._compatibility_flow(record.name))

    async def _compatibility_flow(self, provider_name: str) -> None:
        """Refresh modelslist formats, then persist the chosen format and URL."""

        current = self._compatibility_record()
        if current is None:
            return
        try:
            catalog_provider = await asyncio.to_thread(
                catalog_api.find_provider_by_slug, provider_name, current.base_url
            )
        except Exception as exc:
            self._report(f"compatibility lookup failed: {exc}")
            return
        if catalog_provider is None:
            self._report("No catalog metadata found for this provider.")
            return
        options = catalog_compatibility(catalog_provider)
        current_option = next(
            (
                option
                for option in options
                if option.dialect == current.catalog_dialect
                and option.api_format == current.api_format
                and option.base_url == current.base_url
            ),
            None,
        )
        # Keep an old catalog dialect or hand-edited endpoint selectable without
        # silently rewriting it merely because the popup was opened.
        saved_label = (
            self._compatibility_label(current_option)
            if current_option is not None
            else f"{current.catalog_dialect or current.api_format} (saved) — "
            f"{current.base_url or 'SDK default URL'}"
        )
        choices = [saved_label]
        choices.extend(
            self._compatibility_label(option)
            for option in options
            if self._compatibility_label(option) != saved_label
        )
        if len(choices) == 1 and current_option is not None:
            self._report("No other compatibility mapping is available for this provider.")
            return
        picked = await self._push_wait(
            SelectionModal(
                f"Compatibility ({provider_name})",
                choices,
                current=saved_label,
                allow_none=False,
                dialog_width=min(100, self.app_size()[0]),
            )
        )
        if not picked or picked == saved_label:
            return
        latest = self._compatibility_record()
        if latest is None or latest.name != provider_name:
            return
        option = next(
            (candidate for candidate in options if self._compatibility_label(candidate) == picked),
            None,
        )
        if option is None:
            return

        async def save_compatibility() -> None:
            """``provider edit NAME --compatibility DIALECT``.

            The CLI re-resolves the dialect's (format, base URL) mapping from
            the catalog itself — the same lookup the in-process read above
            used to build the labels, so the write cannot drift from what
            the picker showed.
            """

            ok, message = await asyncio.to_thread(
                bridge.provider_edit_compatibility, provider_name, option.dialect
            )
            if not ok:
                self._report(message or "provider edit failed")
                return
            self._report("")
            self.refresh_rows()

        self._run(save_compatibility())

    @staticmethod
    def _compatibility_label(option: CatalogDialect) -> str:
        return f"{option.dialect} — {option.base_url}"

    def _caps_record(self) -> provider_store.Provider | None:
        """The record the ``Caps`` row edits, or ``None`` when nothing is editable.

        Caps belong to the endpoint, so the record is whichever provider the trio
        runs on — an inheriting subagent trio runs on outo's — and only a
        hand-added one carries them; a catalog provider's caps come from
        modelslist.
        """

        provider, _ = _reasoning_target(self._target, _effective_config(self._session))
        record = _record(provider)
        if record is None or record.source != SOURCE_CUSTOM:
            return None
        return record

    def _pick_caps(self) -> None:
        """Ask for the two caps in turn, then write them to the provider record.

        Two prompts, like the custom-add wizard. Empty clears that cap back to
        "follow modelslist"; a non-numeric answer is refused, the stored value
        untouched.
        """

        record = self._caps_record()
        if record is None:
            self._report("Caps are editable for custom providers only.")
            return

        def ask_context() -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                try:
                    context_window = _parse_optional_int(result)
                except ValueError:
                    self._report("max context window must be a positive number.")
                    return
                ask_tokens(context_window)

            self.app.push_screen(
                TextInputModal(
                    f"Max context window — {record.name}",
                    initial=str(record.max_context_window or ""),
                    placeholder="e.g. 128000 · empty to follow modelslist",
                    hint="Enter to continue · Esc to cancel",
                ),
                on_close,
            )

        def ask_tokens(context_window: int | None) -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                try:
                    output_tokens = _parse_optional_int(result)
                except ValueError:
                    self._report("max output tokens must be a positive number.")
                    return

                async def save_caps() -> None:
                    """``provider edit --max-context-window/--max-output-tokens``.

                    ``0`` (the cleared answer) is the CLI's documented
                    "follow modelslist" spelling; ``None`` never reaches the
                    argv, so an untouched cap keeps its stored value.
                    """

                    ok, message = await asyncio.to_thread(
                        bridge.provider_edit_caps,
                        record.name,
                        max_context_window=context_window or 0,
                        max_output_tokens=output_tokens or 0,
                    )
                    if not ok:
                        self._report(message or "provider edit failed")
                        return
                    # The override itself re-reads the record on every call,
                    # but the modelslist FALLBACK it hands back to is cached
                    # for the life of this TUI process — drop it or this TUI
                    # serves pre-edit caps until exit.
                    context_api.clear_model_cache()
                    self._report("")
                    self.refresh_rows()

                self._run(save_caps())

            self.app.push_screen(
                TextInputModal(
                    f"Max output tokens — {record.name}",
                    initial=str(record.max_output_tokens or ""),
                    placeholder="e.g. 16384 · empty to follow modelslist",
                    hint="Enter to save · Esc to cancel",
                ),
                on_close,
            )

        ask_context()

    # ── worker plumbing ─────────────────────────────────────────────────────
    def _run(self, coro: Awaitable[Any]) -> Any:
        """Hand a flow to the app's worker (``run_worker``) — one testable seam.

        A picker that awaits ``push_screen_wait`` must not block the key handler,
        so an app with no ``run_worker`` falls back to a bare task.
        """

        runner = getattr(self.app, "run_worker", None)
        if callable(runner):
            return runner(coro, exclusive=False)
        return _spawn(coro)


def _spawn(coro: Awaitable[Any]) -> Any:
    """Run ``coro`` as a task and keep a reference so it is not collected."""

    task = asyncio.ensure_future(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


_TASKS: set[Any] = set()
"""Strong references to the fallback tasks (``asyncio`` only keeps weak ones)."""


# ─── CommandPalette ──────────────────────────────────────────────────────────


class SystemCommand(NamedTuple):
    """One palette entry — the shape Textual's ``SystemCommand`` has.

    The app's ``get_system_commands()`` may yield anything with these three
    attributes; this named tuple is what :func:`default_system_commands` builds
    and what tests compare against.
    """

    title: str
    help: str
    callback: Callable[[], Any] | None = None


def default_system_commands(app: Any) -> list[SystemCommand]:
    """The palette's commands, in the app's own order, with NO numeric prefix.

    Textual's palette prefixed every entry with ``00``, ``01``, … because the
    ListView sorted its items alphabetically and the numbers pinned the
    intended order. This list is already ordered, so the numbers only cost
    cells and read like a menu from another program. The owner asked for them
    gone — together with ``Clear log`` (still on Ctrl+L), ``Maximize`` and
    ``Screenshot``, none of which he ever used.

    The custom entries are the app's own actions; the extras are the ones a
    terminal UI keeps (Quit, Keys). A missing app method becomes ``None``
    rather than a crash: the palette is a convenience, never the only path
    to an action.
    """

    def action(name: str) -> Callable[[], Any] | None:
        method = getattr(app, name, None)
        return method if callable(method) else None

    return [
        SystemCommand("New session", "Create a new session", action("_new_session")),
        SystemCommand(
            "Pick session", "Switch the active session", action("_open_session_picker")
        ),
        SystemCommand(
            "Configure outo",
            "Provider → model → reasoning for the main agent",
            action("_configure_outo"),
        ),
        SystemCommand(
            "Configure subagent",
            "Provider → model → reasoning for the subagent",
            action("_open_subagent_editor"),
        ),
        SystemCommand(
            "Pick style", "Switch the active style document", action("_open_style_picker")
        ),
        SystemCommand("Theme", "Change the current theme", action("action_change_theme")),
        SystemCommand(
            "Keys",
            "Show the keys for the focused widget",
            action("action_show_help"),
        ),
        SystemCommand(
            "Quit", "Quit the application as soon as possible", action("action_quit")
        ),
    ]


class CommandPalette(Modal):
    """Ctrl+P: a centred dialog listing the commands, in the modal house style.

    Deliberately NOT a copy of Textual's palette. That one was a full-width
    ``▔``/``▁`` hkey frame hanging from the top of the screen with an ``00``/
    ``01`` number in front of every entry (a workaround for its alphabetical
    sort); the owner wants the app's own dialog look instead, so this inherits
    :class:`Modal`'s frame — thick ``█▀▄`` primary border, ``$surface`` body,
    ``padding 1 2`` and a bold centred title — exactly like
    :class:`ProviderSettingsPopup` and :class:`SelectionModal`.

    The body is: one row per command on ``$boost`` (the same list surface the
    pickers use) with the highlighted row painted solid primary, then the
    hint line. There is no search box — every command is always on show and
    the list scrolls. Up/Down move, Enter runs, Esc or a click outside
    closes.
    """

    def __init__(
        self,
        commands: Sequence[Any] | None = None,
        *,
        placeholder: str = PALETTE_PLACEHOLDER,
    ) -> None:
        super().__init__(id="command-palette", dialog_id="command-palette-dialog")
        # A palette row is a NAME plus its help, so it needs more room than the
        # pickers' 60-cell default (the owner wants the two on one line).
        self.border_width = PALETTE_WIDTH
        self._commands = list(commands) if commands is not None else None
        self._placeholder = placeholder
        self.title_label = self.dialog.add(
            Label("Menu", "command-palette-title", align="center", bold=True)
        )
        self.hint = self.dialog.add(
            Label(PALETTE_HINT, "command-palette-hint", tone="muted", align="center")
        )
        self.index = 0
        self.scroll = 0
        self._panel = Rect()
        self._row_regions: list[Rect] = []
        self._row_offsets: list[int] = []

    # ── commands ────────────────────────────────────────────────────────────
    @property
    def commands(self) -> list[Any]:
        if self._commands is None:
            self._commands = self._commands_from_app()
        return self._commands

    def _commands_from_app(self) -> list[Any]:
        """``app.get_system_commands()`` — duck-typed, no Textual protocol."""

        getter = getattr(self.app, "get_system_commands", None)
        if not callable(getter):
            return []
        try:
            return list(getter(self))
        except TypeError:
            # The port's app may drop Textual's `screen` argument.
            return list(getter())

    @property
    def results(self) -> list[Any]:
        """Every command, in palette order — there is no query to filter."""

        return list(self.commands)

    def highlighted(self) -> Any | None:
        if 0 <= self.index < len(self.commands):
            return self.commands[self.index]
        return None

    # ── geometry ────────────────────────────────────────────────────────────
    def max_result_rows(self, area: Rect) -> int:
        """How many commands fit inside the dialog's own row budget."""

        return max(1, min(PALETTE_MAX_ROWS, self.row_budget(area) - 4))

    # ── painting ────────────────────────────────────────────────────────────
    def content_rows(self, width: int, theme: TuiTheme, budget: int) -> list[StyledLine]:
        """Title, blank, the list (``$boost``), blank, hint."""

        surface = _style(bg=theme.surface)
        rows = self.max_result_rows(self._area)
        lines: list[StyledLine] = [
            *self.title_label.build_lines(width, theme, bg=theme.surface),
            _pad(surface, width),
        ]
        list_top = len(lines)
        visible = self.commands[self.scroll : self.scroll + rows]
        self._row_offsets = list(range(list_top, list_top + len(visible)))
        for offset, command in enumerate(visible):
            selected = self.scroll + offset == self.index
            lines.append(self._row_lines(width, theme, command, selected))
        if not visible:
            empty = _style(fg=theme.text_muted, bg=theme.boost)
            lines.append(_fit([("  no commands", empty)], width, _style(bg=theme.boost)))
        lines.extend(
            [
                _pad(surface, width),
                *self.hint.build_lines(width, theme, bg=theme.surface),
            ]
        )
        return lines[:budget]

    def _row_lines(
        self, width: int, theme: TuiTheme, command: Any, selected: bool
    ) -> StyledLine:
        """One command row: its name (and, when the dialog is wide, its help).

        The selected row is the picker's highlight — solid primary with
        near-black bold ink — so the list reads exactly like every other list in
        the app instead of the old palette's ``$primary 30%`` tint.
        """

        if selected:
            style = _style(fg=_auto_text(theme.primary), bg=theme.primary, bold=True)
        else:
            style = _style(fg=_text_hex(theme), bg=theme.boost, bold=True)
        pieces = [("  ", style), (str(getattr(command, "title", command)), style)]
        help_text = str(getattr(command, "help", "") or "")
        # The help rides on the SAME row, two cells after the name, and only
        # when at least a few cells of it would actually fit — a name that
        # fills the row simply keeps the row to itself.
        gap = 2
        room = width - _cells(pieces) - gap
        if help_text and room >= 12:
            help_style = style if selected else _style(fg=theme.text_muted, bg=theme.boost)
            pieces = [*pieces, (" " * gap, style), *truncate_to([(help_text, help_style)], room)]
        return _fit(pieces, width, _style(bg=theme.primary if selected else theme.boost))

    def place(self, inner: Rect) -> None:
        """Assign the title, the list rows and the hint."""

        rows = self.max_result_rows(self._area)
        self.title_label.region = Rect(inner.x, inner.y, inner.width, 1)
        # The list starts under the title: title plus one blank row, i.e.
        # 2 rows below the dialog body top.
        top = inner.y + 2
        self._panel = Rect(inner.x, top, inner.width, rows)
        self._row_regions = [
            Rect(inner.x, inner.y + offset, inner.width, 1)
            for offset in self._row_offsets
        ]
        self.hint.region = Rect(inner.x, top + rows, inner.width, 1)

    def render(self) -> Text:
        """The title, then one row per command (plain text)."""

        text = Text(self.title_label.plain)
        for command in self.results:
            text.append("\n" + str(getattr(command, "title", command)))
        return text

    # ── input ───────────────────────────────────────────────────────────────
    def handle_key(self, key: Key) -> bool:
        if key.code == "Esc" or (key.code == "P" and key.ctrl):
            self.dismiss(None)
        elif key.code in ("Down", "Tab"):
            self._move(1)
        elif key.code in ("Up", "BackTab"):
            self._move(-1)
        elif key.code == "Enter":
            command = self.highlighted()
            if command is not None:
                self._invoke(command)
        elif key.code == "PageDown":
            self._move(self.max_result_rows(self._area))
        elif key.code == "PageUp":
            self._move(-self.max_result_rows(self._area))
        return True

    def _move(self, direction: int) -> None:
        if not self.commands:
            return
        rows = self.max_result_rows(self._area)
        self.index = max(0, min(self.index + direction, len(self.commands) - 1))
        if self.index < self.scroll:
            self.scroll = self.index
        elif self.index >= self.scroll + rows:
            self.scroll = self.index - rows + 1

    def _invoke(self, command: Any) -> None:
        """Run the command's callback, then close the palette."""

        callback = getattr(command, "callback", None)
        self.dismiss(command)
        if not callable(callback):
            return
        outcome = callback()
        if inspect.isawaitable(outcome):
            runner = getattr(self.app, "run_worker", None)
            if callable(runner):
                runner(outcome, exclusive=False)
            else:
                _spawn(outcome)

    def handle_mouse(self, ev: Mouse) -> bool:
        if ev.kind != "down" or ev.button != 0:
            return True
        if not self.region.contains(ev.x, ev.y):
            # A click outside the dialog closes the palette.
            self.dismiss(None)
            return True
        for offset, region in enumerate(self._row_regions):
            if region.contains(ev.x, ev.y):
                self.index = self.scroll + offset
                command = self.highlighted()
                if command is not None:
                    self._invoke(command)
                return True
        return True


KEYS_HINT = "Esc to close"
"""The Keys popup's closing hint (it has no list to navigate)."""

DEFAULT_KEY_BINDINGS: tuple[tuple[str, str], ...] = (
    ("Ctrl+P", "open the command palette"),
    ("Ctrl+L", "clear the log"),
    ("Esc Esc", "stop the running turn"),
    ("Ctrl+C", "quit"),
    ("Tab / Shift+Tab", "cycle the chips"),
    ("Enter", "open the focused chip · submit the prompt"),
    ("Esc", "cancel a modal"),
    ("PageUp / PageDown", "scroll the log a page"),
    ("Home / End", "jump to the log's top / bottom"),
   ("Up / Down", "move the caret between input rows"),
    ("Left / Right", "move the caret a character"),
    ("Ctrl+Left / Ctrl+Right", "move the caret a word"),
    ("Ctrl+Shift+C", "copy the selected text"),
)
"""The app's real bindings, one row per branch ``ChatTUI.handle_key`` takes.

The editor block (Backspace/Delete, arrows, Home/End, typing) belongs to the
focused :class:`~.widgets.ChatInput` and is summarised by the arrow rows above;
this tuple is the single source the :class:`KeyModal` paints.
"""


class KeyModal(Modal):
    """Ctrl+P → Keys: the app's real key bindings, in the modal house style.

    The list is not a wish: every line mirrors a branch the app's
    ``handle_key`` actually takes (plus the editor keys the focused
    :class:`~.widgets.ChatInput` owns), so the popup can never advertise a
    binding the terminal does not answer. Esc and a click outside close it.
    """

    def __init__(self, bindings: Sequence[tuple[str, str]] | None = None) -> None:
        super().__init__(id="key-modal", dialog_id="key-dialog")
        self._bindings = list(bindings) if bindings is not None else DEFAULT_KEY_BINDINGS
        self.title_label = self.dialog.add(
            Label("Keys", "key-title", align="center", bold=True)
        )
        self.hint = self.dialog.add(
            Label(KEYS_HINT, "key-hint", tone="muted", align="center")
        )

    # ── state ───────────────────────────────────────────────────────────────
    @property
    def title_text(self) -> str:
        return self.title_label.plain

    @property
    def hint_text(self) -> str:
        return self.hint.plain

    @property
    def bindings(self) -> list[tuple[str, str]]:
        """The ``(key, action)`` rows, exactly as the dialog shows them."""

        return list(self._bindings)

    # ── painting ────────────────────────────────────────────────────────────
    def content_rows(self, width: int, theme: TuiTheme, budget: int) -> list[StyledLine]:
        """Title, blank, one key/action row each, blank, the hint."""

        surface = _style(bg=theme.surface)
        body = _style(bg=theme.boost, bold=True)
        action = _style(fg=theme.text_muted, bg=theme.boost)
        lines: list[StyledLine] = [
            *self.title_label.build_lines(width, theme, bg=theme.surface),
            _pad(surface, width),
        ]
        for key, what in self._bindings[: max(0, budget - 4)]:
            lines.append(_fit([("  " + key, body), ("   ", body), (what, action)], width, body))
        lines.extend([_pad(surface, width), *self.hint.build_lines(width, theme, bg=theme.surface)])
        return lines[:budget]

    def place(self, inner: Rect) -> None:
        self.title_label.region = Rect(inner.x, inner.y, inner.width, 1)
        top = inner.y + 2
        self.hint.region = Rect(
            inner.x, top + min(len(self._bindings), self.row_budget(self._area) - 4) + 1,
            inner.width, 1,
        )

    # ── input ───────────────────────────────────────────────────────────────
    def handle_mouse(self, ev: Mouse) -> bool:
        if ev.kind == "down" and ev.button == 0 and not self.region.contains(ev.x, ev.y):
            # A click outside the dialog closes the popup.
            self.dismiss(None)
        return True
