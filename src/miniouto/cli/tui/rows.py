"""Chat-log rows for the PyRatatui TUI: plain state objects, not widgets.

WHY plain objects: pyratatui is immediate-mode — every frame the app composes a
fresh buffer — so there is no widget tree to mount into and nothing to
invalidate. A row therefore owns nothing but STATE (its text, its collapsed
flag, its attached result, its image) and is essentially a function of
``(state, width, theme)`` → styled lines. :meth:`RowStatic.build_lines` is that
function and the only thing the renderer calls.

WHY ``render()`` still exists and still returns Rich ``Text``: the rows author
their content in Rich (``Text.assemble`` with ``"bold #2fc2ff"`` style strings —
exactly what the Textual TUI wrote), the tests read ``.plain`` off it, and Rich
is the only thing that knows how to parse ANSI art into coloured spans. The port
is a REPAINT, not a rewrite: ``build_lines`` runs the Rich content through
:func:`render.rich_text_to_lines` and then through the project's own
cell-correct wrap engine, so the painted result IS the Rich result.

WHY every width is measured with ``rich.cells.cell_len``: ``Span.width()`` counts
CHARACTERS. A ``Span("한글")`` answers 2 while painting four columns, so any
hit-testing or wrapping built on it mis-aligns on the first CJK line. The glyph
hit math (:data:`GLYPH_CELLS`), the gutters and the selection splits all go
through ``cell_len``.

WHY the two mixins survive the port: ``ToggleGlyphHover`` (hover feedback scoped
to the glyph) and ``ClickToToggle`` (a plain click toggles, a text-dragging
gesture never does) are separate concerns that the old rows combined by MRO.
Textual dispatched a naming-convention handler from EVERY class in the MRO, so
both ``on_mouse_move`` definitions ran; here the chain is explicit — each mixin
does its half and calls ``super()`` — the same behaviour without the magic.

``Node`` is the interim node base. ``app.py``'s ``nodes.py`` is the real one (it
owns ``query``/``query_one``, the compositor and the Pilot); this class carries
only what a row needs to be paintable and addressable, so ``nodes.Node`` can
subsume it without touching row logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.cells import cell_len
from rich.markdown import Markdown
from rich.style import Style as RichStyle
from rich.text import Text

from ...tools import ascii_art
from . import pt
from .render import TAB_WIDTH, blend, render_markdown, rich_text_to_lines, wrap_spans
from .theme import MINIOUTO_THEME, TuiTheme

__all__ = [
    "COLLAPSED_GLYPH",
    "DEFAULT_WIDTH",
    "EXPANDED_GLYPH",
    "GLYPH_CELLS",
    "LEFT_MOUSE_BUTTON",
    "TOOL_BADGES",
    "AnswerRow",
    "ClickToToggle",
    "EventRow",
    "Node",
    "Piece",
    "Rect",
    "RowMouseEvent",
    "RowStatic",
    "StreamRow",
    "StyledLine",
    "ThinkingRow",
    "ToggleGlyphHover",
    "ToolRow",
    "art_path_in_result",
    "cells_of",
    "pad_to",
    "place_at",
    "plain_of",
    "png_path_in_result",
    "truncate_to",
]

# ─── core types ──────────────────────────────────────────────────────────────

Piece = tuple[str, pt.Style]
"""One styled run of text. Never carries a newline or a tab (a Span collapses both)."""

StyledLine = list[Piece]
"""One visual line, already wrapped to the width it was built for."""

DEFAULT_WIDTH = 80
"""Width used before a row was ever laid out (``render``/``selectable_text``)."""

BORDER_GLYPH = "┃"
"""Heavy vertical rule: Textual's ``border-left: heavy`` left edge."""

BORDER_CELLS = 1
"""Cells the left border rule spends (``┃`` is single-column)."""

PADDING_CELLS = 1
"""``padding-left: 1`` inside the border, on EventRow and its subclasses."""

_NO_STYLE = pt.Style()
_REVERSED = pt.Style().add_modifier(pt.Modifier.reversed())


# `Rect` and `Node` are the ONE pair every layer shares (see `nodes.py`): a
# modal's row and a chip in the panel have to be addressable by the same query
# and selected by the same harness, so the bases cannot be per-module copies.
from .nodes import Node, Rect  # noqa: E402  (after the type aliases above)


def _matches(node: Node, selector: str) -> bool:
    if selector.startswith("#"):
        return node.id == selector[1:]
    return node.has_class(selector)


# ─── styled-line helpers ─────────────────────────────────────────────────────


def cells_of(pieces: StyledLine) -> int:
    """Cells a styled line spends — ``cell_len``, never ``len``."""

    return sum(cell_len(text) for text, _ in pieces)


def plain_of(pieces: StyledLine) -> str:
    return "".join(text for text, _ in pieces)


def pad_to(pieces: StyledLine, target: int, style: pt.Style) -> StyledLine:
    """Append spaces until the line is ``target`` cells wide (never truncates)."""

    deficit = target - cells_of(pieces)
    if deficit <= 0:
        return list(pieces)
    return [*pieces, (" " * deficit, style)]


def truncate_to(pieces: StyledLine, target: int) -> StyledLine:
    """Cut a styled line at ``target`` cells, keeping whole characters.

    A wide character that would straddle the cut moves to the (dropped) next line
    whole, so a clipped line never carries half a glyph.
    """

    if cells_of(pieces) <= target:
        return list(pieces)
    out: StyledLine = []
    used = 0
    for text, style in pieces:
        if used >= target:
            break
        stop = len(text)
        for index, char in enumerate(text):
            char_cells = cell_len(char)
            if used + char_cells > target:
                stop = index
                break
            used += char_cells
        if stop:
            out.append((text[:stop], style))
        if stop < len(text):
            break
    return out


def _tail_cells(text: str, cells: int) -> str:
    """The last ``cells`` terminal cells of ``text``, keeping whole characters.

    The tail counterpart of :func:`truncate_to`: a wide glyph that would
    straddle the cut moves out of the tail whole, so a clipped tail never
    carries half a glyph.
    """

    kept: list[str] = []
    used = 0
    for char in reversed(text):
        char_cells = cell_len(char)
        if used + char_cells > cells:
            break
        kept.append(char)
        used += char_cells
    return "".join(reversed(kept))


def place_at(line: StyledLine, x: int, pieces: StyledLine, style: pt.Style) -> StyledLine:
    """Paint ``pieces`` starting at cell ``x`` of ``line``.

    Cells already present past ``x`` are REPLACED, not shifted: the chip rows
    right-anchor their second chip, and at a narrow width that lands on top of the
    left chip. Overwriting keeps the right chip reachable instead of pushing it
    off screen — exactly the bug the two-row chip layout fixed.
    """

    out = truncate_to(line, x)
    out = pad_to(out, x, style)
    return [*out, *pieces]


def _text_pieces(text: Text, base: pt.Style) -> list[StyledLine]:
    """Rich ``Text`` → logical lines of styled pieces (newlines already split)."""

    return [
        [(span.content, span.style) for span in line.spans]
        for line in rich_text_to_lines(text, base)
    ]


def _wrap(pieces: list[StyledLine], width: int) -> list[StyledLine]:
    """Wrap every logical line to ``width`` cells with the shared engine.

    One engine for every row and for the input: a styled line and a plain line
    can never wrap differently, and a span split across the break keeps its
    style.
    """

    out: list[StyledLine] = []
    for line in pieces:
        out.extend(wrap_spans(line, width))
    return out or [[]]


# ─── selection painting ──────────────────────────────────────────────────────
#
# The app owns the selection; a row only paints what it is handed. Two shapes are
# accepted so a row never has to know which harness drives it: an object with
# ``get_span(line)`` (``nodes.Selection``) or the plain
# ``(start_line, start_col, end_line, end_col)`` tuple the contract also allows,
# where ``end_col == -1`` means "to the end of the text".


def _span_for_line(selection: Any, index: int) -> tuple[int, int] | None:
    getter = getattr(selection, "get_span", None)
    if getter is not None:
        return getter(index)
    try:
        start_line, start_col, end_line, end_col = selection
    except (TypeError, ValueError):
        return None
    if index < start_line or index > end_line:
        return None
    start = start_col if index == start_line else 0
    end = end_col if index == end_line else -1
    return (int(start), int(end))


def _extract(selection: Any, text: str) -> str:
    """The selected slice of ``text`` — an object with ``extract`` wins (theirs is exact)."""

    extractor = getattr(selection, "extract", None)
    if extractor is not None:
        return str(extractor(text))
    picked: list[str] = []
    for index, line in enumerate(text.split("\n")):
        span = _span_for_line(selection, index)
        if span is None:
            continue
        start, end = span
        start = max(0, start)
        end = len(line) if end == -1 else min(end, len(line))
        if start < end:
            picked.append(line[start:end])
    return "\n".join(picked)


def _stylize(line: StyledLine, span: tuple[int, int]) -> StyledLine:
    """Reverse-video the characters ``[start, end)`` of one painted line.

    Split in CHARACTERS, not cells: ``Strip.crop`` is cell-based and misaligns on
    CJK glyphs, which is why the old code rebuilt the strip as Rich Text and
    stylized a character range. ``end == -1`` stops at the text end, so the row's
    own trailing blanks are never painted as selected.
    """

    plain = plain_of(line)
    length = len(plain.rstrip())
    start, end = span
    start = max(0, start)
    end = length if end == -1 else min(end, length)
    if start >= end:
        return line
    out: StyledLine = []
    cursor = 0
    for text, style in line:
        piece_end = cursor + len(text)
        if piece_end <= start or cursor >= end:
            out.append((text, style))
        else:
            cut_start = max(start - cursor, 0)
            cut_end = min(end - cursor, len(text))
            if cut_start:
                out.append((text[:cut_start], style))
            out.append((text[cut_start:cut_end], style.patch(_REVERSED)))
            if cut_end < len(text):
                out.append((text[cut_end:], style))
        cursor = piece_end
    return out


def _apply_selection(
    lines: list[StyledLine], selection: Any, shift: int = 0
) -> list[StyledLine]:
    """Reverse-video the selected span of every line.

    ``shift`` is the row's gutter width: the selection is built in CONTENT
    coordinates (the app subtracts the border/padding before it records the
    drag, and ``selectable_text`` reads the same space), so a row that paints
    its gutter in front of the text must move the span past it. Without the
    shift the highlight starts ``gutter`` cells too far left — on every
    ``EventRow``/``ThinkingRow``/``StreamRow``/``ToolRow`` it lit the border.
    """

    if selection is None:
        return lines
    out: list[StyledLine] = []
    for index, line in enumerate(lines):
        span = _span_for_line(selection, index)
        if span is None:
            out.append(line)
            continue
        start, end = span
        out.append(_stylize(line, (start + shift, -1 if end == -1 else end + shift)))
    return out


# ─── the row base ────────────────────────────────────────────────────────────


class RowStatic(Node):
    """Chat-log row with uniform reverse-video drag selection.

    Textual's native selection painted with ``$screen-selection-background``, and
    Markdown rows (``RichVisual``) got no native painting at all. This base
    suppresses the native path and paints the selected span itself with a plain
    ``reverse`` modifier — a true fg/bg inversion that reads identically on every
    row type (prompts, loop output, thinking, alarms, Markdown answers).

    ``msg_ref`` is the ``str(id(message))`` of the live coreouto Message this row
    was built from, or None for rows that have no message behind them (user
    prompts, system notices, answers) and for events that are never prunable
    (alarms, wakeups, provider errors).
    """

    _BORDER = False
    _FOCUSABLE = False

    def __init__(self, content: Text | str | None = None, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._content: Text = content if isinstance(content, Text) else Text(content or "")
        self.selection: Any | None = None
        """Set by the app: this row's current selection (object or tuple)."""
        self._theme: TuiTheme = MINIOUTO_THEME
        self._build_width: int | None = None
        self._painted: list[StyledLine] | None = None
        self._cache: dict[tuple[int, str, int], list[StyledLine]] = {}
        self._version = 0

    # ── themable pieces ───────────────────────────────────────────────────
    def _document_color(self, theme: TuiTheme) -> str:
        """The row's text layer: body by default (``$foreground``)."""

        if self.has_class("-alarm"):
            return theme.warning
        return theme.foreground

    def _border_color(self, theme: TuiTheme) -> str | None:
        """The left rule's tint, or None when the row has no border."""

        if self.has_class("-alarm"):
            # `$warning 60%`: the alarm reads as a system notice rather than as
            # the user's own words, without shouting.
            return blend(theme.warning, theme.background, 0.60)
        return None

    def _base_style(self, theme: TuiTheme) -> pt.Style:
        """The row's ink, always on the theme background.

        A ``pt.Style()`` with no ``bg`` falls back to the TERMINAL's own default
        background, which is not ``theme.background`` — the log then paints a
        visibly different (lighter) panel than the logo, the input band and the
        bottom rows around it.
        """

        style = pt.Style().fg(theme.color(self._document_color(theme))).bg(
            theme.color(theme.background)
        )
        if self.focused and self._FOCUSABLE:
            # `:focus { background: $primary 22% }` — one tinted block painted on
            # every piece, so a partially painted line still reads as focused.
            style = style.bg(theme.color(blend(theme.primary, theme.background, 0.22)))
        return style

    @property
    def _has_border(self) -> bool:
        """Does this row paint a left rule?

        ``_BORDER`` is the class-level default (every ``EventRow`` has one), but a
        subclass may add a rule through its theme hook alone — ``RowStatic.-alarm``
        paints the warning rule and has no ``_BORDER`` of its own. Asking both keeps
        the gutter, the glyph hit box and the selection offsets agreeing with what
        is actually on screen.
        """

        return self._BORDER or self._border_color(self._theme) is not None

    @property
    def _gutter_width(self) -> int:
        """Cells the left border + its padding spend (0 when there is no border)."""

        return BORDER_CELLS + PADDING_CELLS if self._has_border else 0

    @property
    def _glyph_column(self) -> int:
        """First cell of the toggle glyph: the row's border + padding gutter.

        Mouse offsets are relative to the ROW's outer region, so the hit math must
        start after the border cell and the padding cell — otherwise the glyph
        highlights while the pointer is still on the border.
        """

        return self._gutter_width

    def _gutter_pieces(self, theme: TuiTheme) -> StyledLine:
        color = self._border_color(theme)
        if color is None:
            return []
        rule = pt.Style().fg(theme.color(color)).bg(theme.color(theme.background))
        return [(BORDER_GLYPH, rule), (" ", rule)]

    # ── content ───────────────────────────────────────────────────────────
    def _content_text(self, theme: TuiTheme) -> Text:
        """The row's Rich content for ``theme`` (state → Text, no width needed)."""

        return self._content

    def _content_lines(
        self, width: int, theme: TuiTheme, base: pt.Style
    ) -> list[StyledLine]:
        return _wrap(_text_pieces(self._content_text(theme), base), width)

    def build_lines(
        self,
        width: int,
        theme: TuiTheme,
        selection: Any | None = None,
    ) -> list[StyledLine]:
        """Painted lines for ``width`` cells: the ONLY entry point the app uses.

        The unselected lines are cached per ``(width, theme, state version)``; the
        selection is applied on top of EVERY call because a drag mutates it in
        place and must never be served from a stale cache.
        """

        width = max(1, int(width))
        self._theme = theme
        self._build_width = width
        key = (width, theme.name, self._version)
        lines = self._cache.get(key)
        if lines is None:
            base = self._base_style(theme)
            lines = self._content_lines(max(1, width - self._gutter_width), theme, base)
            gutter = self._gutter_pieces(theme)
            if gutter:
                lines = [[*gutter, *line] for line in lines]
            self._cache[key] = lines
            if len(self._cache) > 4:  # a resize storm must not grow without bound
                for stale in list(self._cache)[:-4]:
                    del self._cache[stale]
        self._painted = lines
        active = self.selection if selection is None else selection
        return lines if active is None else _apply_selection(lines, active, self._gutter_width)

    def height(self, width: int, theme: TuiTheme) -> int:
        return len(self.build_lines(width, theme))

    def render(self) -> Text:
        return self._content_text(self._theme)

    def _refresh(self) -> None:
        """Drop every derived cache after a state change (the old ``refresh()``)."""

        self._version += 1
        self._cache.clear()

    # ── selection ─────────────────────────────────────────────────────────
    def selectable_text(self) -> str:
        """The row's CONTENT text, rstripped per line — the drag-copy source.

        Content, not painted cells: the border glyph and its pad are chrome, so
        the text a selection addresses starts after them. That is the space the
        app builds offsets in (it subtracts the same gutter from the mouse
        report) and the one a drag sees — a press on the glyph cell is offset 0.
        ``_render_cache`` exposes the same content lines, which is what keeps a
        ``row._render_cache.lines[0]`` assertion and a drag assertion agreeing
        about which character is which.
        """

        return "\n".join(plain_of(line).rstrip() for line in self._content_lines_cached())

    def _content_lines_cached(self) -> list[StyledLine]:
        """The painted lines with the gutter dropped (content space)."""

        painted = self._painted
        if painted is None:
            painted = self.build_lines(self._build_width or DEFAULT_WIDTH, self._theme)
        gutter = self._gutter_width
        return [line[gutter:] if gutter else line for line in painted]

    def _cache_source_lines(self) -> list[list[tuple[str, pt.Style]]]:
        """``_render_cache``'s lines: the CONTENT, without the border/padding gutter.

        Textual's cache held what the widget's own content area showed, so a
        ``row._render_cache.lines[0]`` assertion addressed the row's text and not
        its chrome. Keeping that here is what lets those assertions port unchanged
        alongside the drag ones, which address the same content offsets.
        """

        if self._painted is None:
            self.build_lines(self._cache_width(), self._cache_theme())
        return self._content_lines_cached()

    def get_selection(self, selection: Any) -> tuple[str, str] | None:
        """What ``selection`` currently holds for this row: its text and newline."""

        text = self.selectable_text()
        if not text:
            return None
        return _extract(selection, text), "\n"

    def text_select_all(self) -> None:
        """Select-all is deliberately OFF: a double-click must not take a row.

        Textual's ``Widget._on_click`` calls ``text_select_all()`` when
        ``event.chain == 2``, BEFORE the click is brokered to ``on_click`` — so no
        click handler could undo it; the only lever is not to do it. Whole-row
        select-all is wrong twice over on a chat log: it grabs far more than the
        gesture aimed at, and the selection paints every line reverse.

        Drag selection is untouched by this: the screen builds it from the select
        state as the button moves, never through this call.
        """

    # ── handler surface ───────────────────────────────────────────────────
    #
    # Declared as no-ops on the base so the gesture mixins can chain with
    # `super()` the way Textual's MRO dispatch did: every class in the MRO got
    # the naming-convention handler, so both halves of a mouse move ran.

    def on_mouse_down(self, event: Any = None) -> None:
        """No-op: a plain row has no press gesture."""

    def on_mouse_move(self, event: Any = None) -> None:
        """No-op: a plain row has no hover feedback."""

    def on_leave(self, event: Any = None) -> None:
        """No-op: a plain row has no hover feedback to revert."""

    def on_click(self, event: Any = None) -> None:
        """No-op: a plain row has nothing to toggle."""

    def handle_key(self, key: Any, app: Any = None) -> bool:
        return False


# ─── intermediate loop output ────────────────────────────────────────────────


class EventRow(RowStatic):
    """One intermediate loop-output line (tool call, thinking, progress).

    Rendered with a translucent left border and muted gray text instead of the
    old ``actor:`` prefix — everything that is not the final answer is visually
    grouped and subordinated. The ``$primary 40%`` tint follows the active theme
    automatically; ``-error`` swaps in ``$error`` text and an ``$error 60%`` rule,
    which is how a failed tool call reads at a glance.
    """

    _BORDER = True

    def _document_color(self, theme: TuiTheme) -> str:
        if self.has_class("-error"):
            return theme.error
        return theme.text_muted

    def _border_color(self, theme: TuiTheme) -> str:
        if self.has_class("-error"):
            return blend(theme.error, theme.background, 0.60)
        return blend(theme.primary, theme.background, 0.40)


COLLAPSED_GLYPH = "▶"
"""Toggle glyph of a collapsed row header."""

EXPANDED_GLYPH = "▼"
"""Toggle glyph of an expanded row header."""

GLYPH_CELLS = max(cell_len(COLLAPSED_GLYPH), cell_len(EXPANDED_GLYPH))
"""Terminal cells the toggle glyph occupies, measured rather than assumed.

Both U+25B6 and U+25BC are East-Asian-Ambiguous, so a hard-coded 1 would be a
width assumption the terminal may not honour; the hover hit math and every test
that reasons about the header read this instead.
"""

TOOL_BADGES: dict[str, str] = {
    "Bash": "[>_]",
    "Computer": "[⮜]",
    "Image": "[⣏⣹]",
    "Video": "[⣏⣹]",
    "Audio": "[♫\ufe0e]",
}
"""Per-tool header badge, painted where the ▶/▼ glyph would sit.

A badged row reads its tool at a glance in a wall of collapsed rows. The badge
REPLACES the toggle glyph (the whole row stays the click target, so nothing
needs the triangle); tools without an entry — ``continue_session`` and rows
built with no tool name — keep ▶/▼, as does :class:`ThinkingRow`. Every width
that touches a badge is MEASURED with ``cell_len``, never assumed: the Audio
badge is 4 characters but 3 cells, because ``♫`` (U+266B) carries a U+FE0E
variation selector that paints inside its cell.
"""

_STREAMING_HEADER_CELLS = cell_len(f"{COLLAPSED_GLYPH} thinking ●")
"""Cells the live streaming header (``▶ thinking ●``) spends."""


# ─── gestures ────────────────────────────────────────────────────────────────

LEFT_MOUSE_BUTTON = 1
"""The button that drags a text selection (``Mouse.button`` while held).

Kept at Textual's value: the original code and the tests ported from it both
spell the left button ``1``. :data:`_SELECTING_BUTTONS` accepts ``0`` as well,
because :class:`input.Mouse` numbers buttons from zero — a drag is then
recognised in either vocabulary instead of being silently ignored in one.
"""

_SELECTING_BUTTONS = frozenset({0, 1})
"""Button ids a drag may carry: 1 (Textual-shaped) or 0 (``input.Mouse``)."""


@dataclass(frozen=True)
class RowMouseEvent:
    """A row-relative mouse event, detached from any concrete event class.

    ``offset`` is in CELLS relative to the row's outer region (y=0 is the header
    line), which is what both the glyph hit math and the toggle gesture were
    written against.
    """

    offset: tuple[int, int] = (0, 0)
    button: int = 0
    chain: int = 1


def _offset_xy(event: Any) -> tuple[int, int]:
    """Read ``(x, y)`` off an offset that may be a tuple or an ``Offset`` object."""

    offset = getattr(event, "offset", None)
    if offset is None:
        return (int(getattr(event, "x", 0)), int(getattr(event, "y", 0)))
    try:
        return (int(offset[0]), int(offset[1]))
    except TypeError:
        return (int(offset.x), int(offset.y))


class ToggleGlyphHover:
    """Hover feedback for the toggle glyph of a collapsible row header.

    Hover is scoped to the GLYPH ALONE: a ``:hover`` rule (or a row-wide tint)
    would repaint the header text, the body and the attached result too, which is
    not what the affordance is about. The highlight is rendered into the row's own
    text instead of being handed to a style layer, and it flips only for a pointer
    on the HEADER line inside the measured glyph width — starting after the row's
    border + padding gutter, since mouse offsets are relative to the outer region.
    """

    hover_glyph: bool = False

    def _set_hover(self, value: bool) -> None:
        if self.hover_glyph == value:
            return  # re-rendering on every motion over the same cell is waste
        self.hover_glyph = value
        self._refresh()

    def on_mouse_move(self, event: Any = None) -> None:
        if event is not None:
            self._set_hover(self._pointer_on_glyph(*_offset_xy(event)))
        super().on_mouse_move(event)

    def on_leave(self, event: Any = None) -> None:
        self._set_hover(False)

    def _hover_span(self) -> int:
        """Cells the hover hit box covers — the toggle glyph by default.

        A row whose glyph is a wider mark (:class:`ToolRow`'s per-tool badge)
        overrides this so the WHOLE badge highlights, not just its first cell.
        """

        return GLYPH_CELLS

    def _pointer_on_glyph(self, x: int, y: int) -> bool:
        return y == 0 and self._glyph_column <= x < self._glyph_column + self._hover_span()

    def _glyph_style(self, theme: TuiTheme, *, accent: bool) -> RichStyle:
        """The glyph's Rich style: resting, or dimmed toward the background while hovered.

        ``accent`` is the glyph's resting colour — ToolRow paints its toggle with
        the theme accent, ThinkingRow leaves it to the row's own colour. The hover
        style composites that resting family 60% over the theme background — the
        house dim, the same blend the alarm border uses — instead of reversing
        video, so the feedback reads as a subtle darkening while staying the ONLY
        piece that changes.
        """

        style = RichStyle()
        if accent:
            style += RichStyle(bold=True, color=theme.accent)
        if self.hover_glyph:
            resting = theme.accent if accent else theme.text_muted
            style += RichStyle(bold=True, color=blend(resting, theme.background, 0.60))
        return style

    def _glyph_piece(
        self, glyph: str, theme: TuiTheme, *, accent: bool = False
    ) -> tuple[str, RichStyle]:
        """The ``▶ `` header prefix, ready for ``Text.assemble``."""

        return f"{glyph} ", self._glyph_style(theme, accent=accent)


class ClickToToggle:
    """A plain click anywhere on a collapsible row toggles it; a drag never does.

    The whole row is the button — glyph, header text, expanded body, attached
    result — because the click is told apart from a text selection by the GESTURE,
    not by where it landed: ``on_mouse_down`` records where the press started, a
    move while it is held marks the gesture a drag, and ``on_click`` toggles only
    when that drag took no text. So dragging a command out of a collapsed row
    still copies it, and clicking the same cell expands it.

    The deciding signal is the SELECTION (a drag that got at least one character
    is a selection; a gesture that got none is a plain click). The app keeps the
    row's selection in ``row.selection`` (or on ``app.screen.selections``), so it
    is already there by the time ``on_click`` runs.

    A click with ``chain > 1`` — the second click of a double/triple-click — is
    consumed, so one double-click gesture toggles at most once (no
    open-then-close flicker).
    """

    _press_offset: tuple[int, int] | None = None
    _dragged: bool = False

    def on_mouse_down(self, event: Any = None) -> None:
        self._press_offset = _offset_xy(event) if event is not None else None
        self._dragged = False
        super().on_mouse_down(event)

    def on_mouse_move(self, event: Any = None) -> None:
        if event is None:
            super().on_mouse_move(event)
            return
        held = int(getattr(event, "button", LEFT_MOUSE_BUTTON))
        if (
            held in _SELECTING_BUTTONS
            and self._press_offset is not None
            and _offset_xy(event) != self._press_offset
        ):
            self._dragged = True
        super().on_mouse_move(event)

    def on_click(self, event: Any = None) -> None:
        if int(getattr(event, "chain", 1)) > 1 or self._drag_took_text():
            return
        self._toggle()

    def key_enter(self) -> None:
        self._toggle()

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._refresh()

    def _drag_took_text(self) -> bool:
        """Whether the press behind this click dragged at least one character out."""

        if not self._dragged or self.selection is None:
            return False
        selected = self.get_selection(self.selection)
        return bool(selected and selected[0])

    # ── terminal-event adapters ───────────────────────────────────────────
    #
    # The old handlers took Textual event objects. The app feeds us
    # `input.Mouse` (kind + absolute cell coords) instead, so the adapter
    # translates one into the other: same gesture, same decisions, no Textual.

    def handle_key(self, key: Any, app: Any = None) -> bool:
        if getattr(key, "code", "") != "Enter":
            return False
        if getattr(key, "ctrl", False) or getattr(key, "alt", False):
            return False
        self.key_enter()
        return True

    def handle_mouse(
        self, ev: Any, x: int, y: int, app: Any = None, *, chain: int | None = None
    ) -> bool:
        self._adopt_selection(app)
        kind = str(getattr(ev, "kind", ""))
        button = int(getattr(ev, "button", LEFT_MOUSE_BUTTON))
        if chain is None:
            chain = int(getattr(ev, "chain", 1) or 1)
        event = RowMouseEvent((x, y), button, chain)
        if kind == "down":
            self.on_mouse_down(event)
            return True
        if kind in ("move", "drag"):
            # Consumed either way: the row owns hover feedback AND the drag
            # tracker, and nothing below it should react to plain motion.
            self.on_mouse_move(event)
            return True
        if kind == "up":
            # Textual's Click needed the release on the same WIDGET as the press,
            # not the same CELL: a gesture that moved but took no text is still a
            # plain click, and `on_click` is what decides that (`_drag_took_text`).
            # This row only sees the release at all when the pointer is still on
            # it, so a tracked press is the whole test.
            pressed = self._press_offset is not None
            self._press_offset = None
            if pressed:
                self.on_click(event)
            return True
        if kind in ("click", "press"):
            self.on_click(event)
            return True
        return False

    def _adopt_selection(self, app: Any) -> None:
        """Pull this row's selection off the app when the row has none of its own."""

        if self.selection is not None or app is None:
            return
        try:
            self.selection = app.screen.selections.get(self)
        except (AttributeError, TypeError):
            return


class ThinkingRow(ToggleGlyphHover, ClickToToggle, EventRow):
    """Collapsible reasoning row: ``▶ thinking`` collapsed (default).

    A plain click anywhere on the row or ``Enter`` expands the full reasoning
    text; a plain click again re-collapses it. A drag is never a toggle, so the
    reasoning can be selected and copied straight out of the collapsed row without
    it opening under the pointer.

    While the model is still streaming its reasoning the collapsed header carries
    a ``●`` marker and a ONE-LINE dim tail of the reasoning so far —
    whitespace-collapsed, anchored to the newest words, ``…``-prefixed when the
    head was cut — so a live row reads at a glance. ``finalize`` drops the
    marker AND the tail and swaps the streamed prefix for the message's
    canonical text (the stream IS a prefix of it, so keeping both would
    duplicate): a settled collapsed row renders exactly ``▶ thinking``.
    """

    can_focus = True
    _FOCUSABLE = True

    def __init__(self, thinking: str = "", *, id: str | None = None) -> None:
        super().__init__("", id=id)
        self._thinking = thinking
        self._expanded = False
        self._streaming = False

    def begin_stream(self) -> None:
        self._streaming = True
        self._refresh()

    def append(self, delta: str) -> None:
        if not delta:
            return
        self._thinking += delta
        self._refresh()

    def finalize(self, thinking: str) -> None:
        """Replace the streamed text with the message's complete reasoning."""

        self._streaming = False
        self._thinking = thinking
        self._refresh()

    @property
    def thinking(self) -> str:
        return self._thinking

    @property
    def streaming(self) -> bool:
        return self._streaming

    def _content_text(self, theme: TuiTheme) -> Text:
        marker = " ●" if self._streaming else ""
        glyph = EXPANDED_GLYPH if self._expanded else COLLAPSED_GLYPH
        pieces: list[tuple[str, RichStyle | str]] = [
            self._glyph_piece(glyph, theme),
            (f"thinking{marker}", ""),
        ]
        if self._expanded:
            pieces.append(("\n", ""))
            pieces.append((self._thinking, ""))
        else:
            # ``render()`` has no width, so the tail it carries is the
            # DEFAULT_WIDTH cut; ``_content_lines`` re-cuts it to the real one.
            tail = self._thinking_tail(DEFAULT_WIDTH)
            if tail:
                pieces.append(
                    (
                        f" {tail}",
                        RichStyle(color=theme.text_disabled, bgcolor=theme.background),
                    )
                )
        return Text.assemble(*pieces)

    def _content_lines(
        self, width: int, theme: TuiTheme, base: pt.Style
    ) -> list[StyledLine]:
        if not (self._streaming and not self._expanded):
            return super()._content_lines(width, theme, base)
        # The live header plus its ONE-LINE tail, built directly instead of
        # wrapping ``_content_text`` (whose tail is cut at DEFAULT_WIDTH, not
        # at this one), so the tail can never wrap onto a second line.
        header = Text.assemble(
            self._glyph_piece(COLLAPSED_GLYPH, theme), ("thinking ●", "")
        )
        lines = _wrap(_text_pieces(header, base), width)
        tail = self._thinking_tail(width)
        if tail:
            dim = (
                pt.Style()
                .fg(theme.color(theme.text_disabled))
                .bg(theme.color(theme.background))
            )
            lines = [[*lines[0], (" ", dim), (tail, dim)], *lines[1:]]
        return lines

    def _thinking_tail(self, width: int) -> str:
        """The reasoning's last words, cut to what fits beside the live header.

        Whitespace-collapsed (streamed fragments arrive as arbitrary ``"  \n"``
        runs that would otherwise shove the header around), anchored to the END
        — the newest words are the live ones — and prefixed ``…`` when the head
        had to be cut. Empty unless the row is streaming, collapsed, and the
        budget is worth having (under 4 cells a tail is noise, not a tail).

        Only the reasoning's LAST ``(budget + 2) * 2 + 8`` characters are
        collapsed: the tail never shows more than ``budget`` cells and a
        character adds at most two of them, so the work stays bounded per
        append instead of growing with the reasoning (a word cut at the window
        boundary reads no worse than the ``…`` replacing the cut head).
        """

        budget = width - _STREAMING_HEADER_CELLS - 1
        if not self._streaming or self._expanded or budget < 4:
            return ""
        window = (budget + 2) * 2 + 8
        text = " ".join(self._thinking[-window:].split())
        if not text or cell_len(text) <= budget:
            return text
        return "…" + _tail_cells(text, budget - 1)


# ─── incremental stream wrap ─────────────────────────────────────────────
#
# WHY a second wrap walker lives here: a streamed row used to re-wrap its
# WHOLE text on every delta — O(n²) over a message, measured ~10ms per
# append+build at 2000 tokens. But ``append`` only ever EXTENDS the text, so
# every visual line a previous wrap had already broken away — every line but
# the last — is final: nothing a later delta adds can move an earlier line's
# break. ``StreamRow`` therefore keeps those lines frozen and re-wraps only
# the tail that starts where the last visual line begins — a region bounded
# by the wrap width, not by the message length.
#
# The price is finding that start offset. It cannot be read off the wrapped
# result (the engine expands tabs and drops trailing spaces by then), so
# ``_visual_line_starts`` walks the SAME atom / indent / break rules and
# records where each visual line began. It mirrors render.py's ``_tokenise``
# + ``_wrap_line`` step for step: when the engine's rules change, this mirror
# must change with them — the equivalence tests in ``test_tui_rows.py`` are
# what holds the two together.
#
# ``_safe_start`` then decides which of those starts the incremental path may
# re-wrap from. The engine measures a word with ONE ``cell_len`` over the
# whole chunk, so a freeze must hand the re-wrap atoms that are measured
# exactly as the full wrap measured them: the start has to sit on an atom
# boundary, AND the line it starts has to begin that atom on an empty budget
# (a word that arrived mid-line is broken apart character by character — a
# different path whose result the re-wrap cannot reproduce). Only a CLEAN
# start survives every future delta unchanged.

_STREAM_ATOM_RE = re.compile(r"\s+|\S+")
"""``render._ATOM_RE`` mirrored: the tail scan must split atoms identically."""


def _atom_cells(chunk: str) -> int:
    """Cells an atom spends — a tab counts as :data:`TAB_WIDTH` spaces."""

    if "\t" not in chunk:
        return cell_len(chunk)
    return cell_len(chunk.replace("\t", " " * TAB_WIDTH))


def _visual_line_starts(source: str, width: int) -> list[tuple[int, bool]]:
    """Where every visual line of ``source``'s wrap begins, and if that is CLEAN.

    A start is clean when no word was sliced to open the line — the line began
    because the previous one was full (or a newline), not because the
    character-level break loop cut a word that arrived mid-line. The walk is
    :func:`render._wrap_line` over each logical line the Rich bridge keeps
    (a trailing ``"\\n"`` contributes no line of its own — see
    ``_text_pieces``), with the growing line tracked as ``(start, end)`` source
    spans so a flushed line can report where it began. Leading indentation
    seeds the first line, the spaces sitting on a break are consumed by it,
    and a word that cannot fit breaks at cell boundaries with a wide glyph
    moving down whole — each rule exactly as the engine applies it.
    """

    width = max(1, width)
    starts: list[tuple[int, bool]] = []
    base = 0
    logical = source.split("\n")
    if source.endswith("\n"):
        logical.pop()  # a trailing "\n" contributes no line of its own
    for line_src in logical:
        atoms: list[tuple[int, int, int, bool]] = []  # (start, end, cells, is_space)
        for match in _STREAM_ATOM_RE.finditer(line_src):
            chunk = match.group(0)
            is_space = chunk[0].isspace()
            cells = _atom_cells(chunk)
            if atoms and atoms[-1][3] == is_space:
                start, _end, total, _space = atoms[-1]
                atoms[-1] = (start, match.end(), total + cells, is_space)
            else:
                atoms.append((match.start(), match.end(), cells, is_space))

        indent: list[tuple[int, int]] = []
        indent_cells = 0
        index = 0
        while index < len(atoms) and atoms[index][3] and indent_cells + atoms[index][2] <= width:
            indent.append((atoms[index][0], atoms[index][1]))
            indent_cells += atoms[index][2]
            index += 1

        cur: list[tuple[int, int]] = list(indent)  # the growing line, as spans
        cur_cells = indent_cells
        started = False  # does the current line hold any non-space text?
        opened_clean = True  # did this line open on a whole atom, not a word slice?
        line_starts: list[tuple[int, bool]] = []

        def flush(line_starts: list[tuple[int, bool]] = line_starts) -> None:
            # the default arg binds THIS line's list, not the next loop turn's
            nonlocal cur, cur_cells, started, opened_clean
            line_starts.append((cur[0][0] if cur else 0, opened_clean))
            cur, cur_cells, started, opened_clean = [], 0, False, True

        for start, end, cells, is_space in atoms[index:]:
            if is_space:
                if cur_cells + cells <= width:
                    cur.append((start, end))
                    cur_cells += cells
                elif started:
                    flush()  # the spaces sit ON the break and are consumed by it
                continue
            if cur_cells + cells <= width:
                cur.append((start, end))
                cur_cells += cells
                started = True
                continue

            # The word does not fit on the current line. From here the
            # character-level break loop places SLICES of it, and a line that
            # opens on a slice can never be a resume point: the full wrap split
            # the word because it did not fit where it arrived, while a re-wrap
            # from the slice start would measure the whole word again at an
            # empty budget and lay it down differently.
            if started:
                flush()
            else:
                # Only indentation so far: the indent yields to the content rather
                # than emitting a line holding nothing but blanks.
                cur, cur_cells = [], 0
            # The emptied line opens CLEAN — the whole word may land on it
            # in one slice, exactly as the engine's fits check would place
            # it. Only a slice that ends mid-word (below) opens one unclean.
            pos = start
            while pos < end:
                span_start = pos
                budget = max(1, width - cur_cells)
                taken = 0
                consumed = 0
                while pos < end and taken + cell_len(line_src[pos]) <= budget:
                    taken += cell_len(line_src[pos])
                    pos += 1
                    consumed += 1
                if consumed == 0 and pos < end:
                    taken = cell_len(line_src[pos])  # a wide glyph never stalls
                    pos += 1
                cur.append((span_start, pos))
                # the engine re-measures the SLICE whole (``render._cells``),
                # which is not the per-character sum when the slice carries a
                # VS16/ZWJ cluster — mirror that, not ``taken``.
                cur_cells += cell_len(line_src[span_start:pos])
                started = True
                if pos < end:
                    # this line ends mid-word, so it opened on a slice —
                    opened_clean = False  # not a resume point either
                    flush()
                    opened_clean = False  # the next line opens mid-word too
        if cur or not line_starts:
            flush()
        starts.extend((base + offset, clean) for offset, clean in line_starts)
        base += len(line_src) + 1
    return starts or [(0, True)]


def _safe_start(text: str, start: int, clean: bool) -> bool:
    """May the incremental re-wrap begin at ``start`` without drifting?

    Three conditions, all about handing the engine the SAME measurement the
    full wrap made — now and under every future append. (1) ``start`` must be
    an atom boundary (or sit right after a newline): ``cell_len`` measures a
    chunk holding a ZWJ (``\\u200d``) or VS16 (``\\ufe0f``) as one grapheme
    cluster — non-additively — so a region cut mid-word hands the engine a
    differently-measured atom than the full wrap measured, and the two drift
    apart. (2) the line must be CLEAN — no word was sliced to open it. A word
    that arrived mid-line is placed by the character-level break loop, whose
    slices a whole-atom fits check on a fresh line cannot reproduce;
    re-wrapping that line from its start would displace those slices and
    drift. (3) the word the region begins on must be CLOSED — followed by a
    space inside the text. ``cell_len`` of a growing word changes as it grows
    (``'\\u200b'`` measures 0 alone but joins the CJK run after it), and the
    last word of the text is exactly the one a later delta can still extend.
    A clean start on a closed word hands the engine atoms measured exactly as
    the full wrap measured them — forever — so the wrap cannot drift.
    """

    if start <= 0 or start >= len(text):
        return True
    if not clean:
        return False
    if text[start - 1] != "\n" and text[start - 1].isspace() == text[start].isspace():
        return False  # mid-atom (a newline always ends its atom)
    end = start
    limit = len(text)
    while end < limit and not text[end].isspace():
        end += 1
    # a word that runs to the end of the text is still OPEN — a delta may
    # extend it and change how it is measured
    return text[start].isspace() or end < limit


class StreamRow(EventRow):
    """Live assistant text: one row, updated in place as tokens arrive.

    Mounted on the first text delta of a message and appended to (never replaced)
    for each subsequent one, so a streamed message costs one row instead of one
    per token. The final answer replaces this row with the canonical Markdown
    :class:`AnswerRow`.

    The wrap is INCREMENTAL: the visual lines a previous build had already
    broken away are frozen and only the tail is re-wrapped, so an append costs
    O(tail) — bounded by the wrap width — instead of a full re-wrap of the
    whole message. The tail begins at the LATEST wrap line start that is safe
    to resume from (:func:`_safe_start`): the engine measures an atom — and a
    ZWJ/VS16 grapheme cluster inside it — whole, so a resume point that cuts
    one, or re-lays a line the break loop had sliced, would re-measure the
    text differently and drift the wrap off the full re-wrap's result. The
    frozen prefix stays
    valid only while the text GROWS by :meth:`append` (a delta never touches
    earlier characters); anything else falls back to a full re-wrap through
    the length guard in :meth:`_content_lines`. A width or ink (``base``
    style) change re-wraps everything, exactly as the base class always did.
    """

    def __init__(self, text: str = "", *, id: str | None = None) -> None:
        super().__init__("", id=id)
        self._text = text
        self._wrap_key: tuple[int, pt.Style, int] | None = None
        """``(width, base style, text length)`` the frozen lines were built for."""
        self._wrap_frozen: list[StyledLine] = []
        """Visual lines before the unfrozen tail, shared (never mutated) with the cache."""
        self._wrap_tail_start = 0
        """Source offset where the unfrozen tail — what the next append re-wraps — begins."""
        self._wrap_tail_clean = True
        """Was the tail's line opened clean (no word sliced to open it)? See ``_safe_start``."""

    def append(self, delta: str) -> None:
        if not delta:
            return
        self._text += delta
        self._refresh()

    @property
    def text(self) -> str:
        return self._text

    def _content_text(self, theme: TuiTheme) -> Text:
        return Text(self._text)

    def _content_lines(
        self, width: int, theme: TuiTheme, base: pt.Style
    ) -> list[StyledLine]:
        """The streamed text wrapped to ``width``, re-wrapping only the tail.

        Byte-identical to the full ``_wrap(_text_pieces(...))`` the base class
        runs — the equivalence tests hold the two together — but linear in
        the tail instead of the message: the frozen lines are reused as-is
        and only the unfrozen tail goes back through the engine, from a
        start that never splits a measured cluster (:func:`_safe_start`
        picks which).
        """

        text = self._text
        key = self._wrap_key
        frozen = self._wrap_frozen
        start = self._wrap_tail_start
        if (
            key is None
            or key[0] != width
            or key[1] != base
            or len(text) < key[2]
            or not _safe_start(text, start, self._wrap_tail_clean)
        ):
            # First build, a width/ink change, a non-append mutation, or a
            # tail the grown text has rendered unsafe (see ``_safe_start``):
            # wrap the whole text and restart the freeze from it.
            lines = _wrap(_text_pieces(Text(text), base), width)
            starts = _visual_line_starts(text, width)
        else:
            tail = text[start:]
            # A trailing newline ends the last logical line and contributes no
            # line of its own, so the engine is handed the piece without it.
            piece = tail[:-1] if tail.endswith("\n") else tail
            wrapped = wrap_spans([(piece, base)], width)
            lines = [*frozen, *wrapped]
            starts = [
                (start + offset, clean)
                for offset, clean in _visual_line_starts(tail, width)
            ]
        # Freeze at the LATEST safe start: the lines before it are final (the
        # text only ever grows), the rest is what the next append re-wraps.
        freeze = len(starts) - 1
        while freeze > 0 and not _safe_start(text, *starts[freeze]):
            freeze -= 1
        if not _safe_start(text, *starts[freeze]):
            # Even the first start is unsafe (a sliced line the region cannot
            # reproduce); the frozen prefix is unusable, so start over whole.
            lines = _wrap(_text_pieces(Text(text), base), width)
            starts = _visual_line_starts(text, width)
            freeze = len(starts) - 1
            while freeze > 0 and not _safe_start(text, *starts[freeze]):
                freeze -= 1
        self._wrap_frozen = lines[: len(lines) - len(starts) + freeze]
        self._wrap_tail_start, self._wrap_tail_clean = starts[freeze]
        self._wrap_key = (width, base, len(text))
        return lines


# ANY Computer result that names a `.art` file: the `screen` action ends with
# "Saved to: …", and a `screenshot` appends the same line to its own summary — so
# the gate is the line, never the action, and a screenshot renders too. The
# `.art` extension is what makes this unambiguous; a stray ".art" inside command
# output cannot match it.
_ART_SAVED_RE = re.compile(r"Saved to:\s*(?P<path>\S+\.art)\s*$", re.MULTILINE)

# A `screenshot` result ALSO names the raw PNG written next to the `.art`; on a
# graphics-capable terminal that file is what the row shows and the `.art` is the
# fallback.
_PNG_SAVED_RE = re.compile(r"Image saved to:\s*(?P<path>\S+\.png)\s*$", re.MULTILINE)

_ART_MAX_ROWS = 40
"""Rows of screen art rendered inline in an expanded Computer tool row.

A full screenshot is hundreds of rows; the point is to SEE the screen, not to
scroll it, and the rest is one ``miniouto computer show`` away.
"""


def art_path_in_result(result: str) -> str | None:
    """The ``.art`` path a Computer tool result names, or None.

    Matched on its own line so a path echoed mid-sentence does not count.
    """

    match = _ART_SAVED_RE.search(result)
    return match.group("path") if match else None


def png_path_in_result(result: str) -> str | None:
    """The raw-PNG path a Computer ``screenshot`` result names, or None."""

    match = _PNG_SAVED_RE.search(result)
    return match.group("path") if match else None


_GRAPHICS: Any = None


def _graphics_module() -> Any:
    """The graphics module, imported lazily and defensively.

    ``graphics.py`` lands with its own editor and may be absent or mid-write while
    the rows are being developed; an ImportError must degrade to the ``.art`` text
    lane rather than break every chat row. The import is retried on each call
    until it succeeds, so a module written after this one is still picked up.
    """

    global _GRAPHICS
    if _GRAPHICS is not None:
        return _GRAPHICS
    try:
        from . import graphics as module
    except Exception:  # any import failure = "no graphics", the text lane
        return None
    _GRAPHICS = module
    return module


class ToolRow(ToggleGlyphHover, ClickToToggle, EventRow):
    """Collapsible tool-call row: a badge (else ▶/▼) + header, collapsed.

    The header leads with the row's per-tool badge (:data:`TOOL_BADGES`) when
    the tool has one; rows without a badge (``continue_session``, or a row
    built with no tool name) keep the ▶/▼ glyph, as before. A COLLAPSED Bash
    row paints exactly ONE line — the header's first visual line, cell-cut with
    a trailing ``…`` — never a wrapped wall. Expanded, a Bash row paints the
    verbatim command and then the attached result in two bordered boxes; every
    other badged tool paints ONE result box (its call preview already is the
    header). A plain click anywhere on the row or ``Enter`` expands it. The
    result is attached later via :meth:`set_result`; until then the expanded
    row is header plus command box only. A drag is never a toggle, so the
    command and its output can be dragged out and copied without the row
    opening under the pointer, and a double-click selects nothing.

    When the result is an error (prefixed ``"error: "``) the row gains the
    ``-error`` class (muted → ``$error`` text and rule) and the header shows a
    subtle ``✗`` marker.

    Detail (multi-line command) and result are each capped at
    :attr:`_MAX_DETAIL_CHARS` / :attr:`_MAX_RESULT_CHARS` on display as a safety
    net even though the core layer already truncates. ``unbounded_result=True``
    opts out — a result the user cannot afford to see cut must read in full.
    """

    can_focus = True
    _FOCUSABLE = True

    _MAX_DETAIL_CHARS = 4000
    _MAX_RESULT_CHARS = 4000

    def __init__(
        self,
        header: str,
        *,
        detail: str | None = None,
        tool_name: str | None = None,
        unbounded_result: bool = False,
        id: str | None = None,
    ) -> None:
        super().__init__("", id=id)
        self._header = header
        self._detail = detail
        self._result: str | None = None
        self._is_error = False
        self._expanded = False
        self._tool_name = tool_name
        self._badge = TOOL_BADGES.get(tool_name or "")
        self._unbounded_result = unbounded_result
        self._image_failed = False
        self.image_visible = False
        self._screen_text: Text | None = None
        self._screen_ready = False
        self._image_line = 0

    # ── state ─────────────────────────────────────────────────────────────
    @property
    def tool_name(self) -> str | None:
        return self._tool_name

    @property
    def has_result(self) -> bool:
        return self._result is not None

    @property
    def is_error(self) -> bool:
        return self._is_error

    @property
    def result(self) -> str | None:
        return self._result

    @property
    def image_line(self) -> int:
        """Line index (row-relative) the reserved image rect starts at."""

        return self._image_line

    def set_result(self, text: str, *, is_error: bool = False) -> None:
        """Attach a result to this row. Idempotent — a second call is a no-op.

        Used for the live path (a paired Bash/Image/... result) and for the single
        result attached at mount time.
        """

        if self._result is not None:
            return
        self._result = text
        self._is_error = is_error
        if is_error:
            self.add_class("-error")
        self._screen_ready = False
        self._screen_text = None
        self.image_visible = False
        self._refresh()

    # ── content ───────────────────────────────────────────────────────────
    def _content_text(self, theme: TuiTheme) -> Text:
        """The width-less Rich view ``render()`` returns.

        The header carries its FULL text here (the one-line cut is a layout
        decision that needs a width, and ``render().plain`` is what a copy
        wants) — except that an EXPANDED Bash header is the badge alone,
        mirroring ``build_lines``. An expanded row carries the command and
        result as PLAIN text lines — the box chrome needs a width, so
        ``build_lines`` paints it and this surface carries the payload
        without chrome.
        """

        if not self._expanded:
            self.image_visible = False
            return Text.assemble(*self._header_pieces(COLLAPSED_GLYPH, theme))

        pieces = self._header_pieces(EXPANDED_GLYPH, theme)
        if self._detail:
            detail = self._detail
            if len(detail) > self._MAX_DETAIL_CHARS:
                detail = detail[: self._MAX_DETAIL_CHARS] + "\n…(truncated)"
            pieces.append(("\n", ""))
            pieces.append((detail, ""))
        if self._result is None:
            self.image_visible = False
            return Text.assemble(*pieces)
        # A badge-less row keeps the legacy `── output ──` label; a badged row
        # carries plain lines only — its painted form separates the boxes.
        pieces.append(("\n── output ──\n", "dim") if not self._badge else ("\n", ""))
        pieces.append((self._result_text(), ""))
        body = Text.assemble(*pieces)
        screen = self._screen_art()
        if screen is not None:
            body.append("\n")
            body.append_text(screen)
        return body

    def _header_pieces(
        self, glyph: str, theme: TuiTheme
    ) -> list[tuple[str, RichStyle | str]]:
        lead: tuple[str, RichStyle | str] = (
            (f"{self._badge} ", self._glyph_style(theme, accent=True))
            if self._badge
            else self._glyph_piece(glyph, theme, accent=True)
        )
        # An EXPANDED Bash header is the badge (plus the error suffix) alone:
        # the command below is the same text verbatim, so repeating it in the
        # header would duplicate the box. Collapsed keeps the preview, and
        # every other tool keeps badge+preview (its box holds only the result).
        text = self._header
        if self._expanded and self._badge == TOOL_BADGES["Bash"]:
            text = ""
        pieces: list[tuple[str, RichStyle | str]] = [lead, (text, "")]
        if self._is_error:
            pieces.append((" ✗", RichStyle(bold=True, color=theme.error)))
        return pieces

    def _hover_span(self) -> int:
        """The badge is the hover target when there is one — measured, not assumed."""

        return cell_len(self._badge) if self._badge else GLYPH_CELLS

    def _content_lines(
        self, width: int, theme: TuiTheme, base: pt.Style
    ) -> list[StyledLine]:
        lines = self._body_lines(width, theme, base)
        placement = self.image
        if placement is not None and self.image_visible:
            rows = int(getattr(placement, "height_cells", 0) or 0)
            if rows > 0:
                # The pixels are emitted by the app AFTER the frame (out-of-band
                # escapes); the row only reserves the cells and remembers where
                # they start, so the app can blank and place that exact rect.
                self._image_line = len(lines)
                lines = [*lines, *[[] for _ in range(rows)]]
        return lines

    def _body_lines(
        self, width: int, theme: TuiTheme, base: pt.Style
    ) -> list[StyledLine]:
        """The row's own lines, before the image lane reserves its cells.

        A row WITHOUT a badge (``continue_session``, or one built with no tool
        name) keeps the plain wrapping layout exactly as it was before badges
        existed, so every legacy row renders byte-identically.
        """

        if not self._badge:
            return super()._content_lines(width, theme, base)
        glyph = EXPANDED_GLYPH if self._expanded else COLLAPSED_GLYPH
        header = Text.assemble(*self._header_pieces(glyph, theme))
        lines = _wrap(_text_pieces(header, base), width)
        if not self._expanded:
            self.image_visible = False
            return self._collapsed_header(lines, width, base)
        if self._badge == TOOL_BADGES["Bash"]:
            # The verbatim command first: the header's flattened preview is
            # the same text with its newlines gone.
            lines = [
                *lines,
                *self._box_lines(self._command_text(), width, theme, base),
            ]
        if self._result is None:
            self.image_visible = False
            return lines
        lines = [*lines, *self._box_lines(self._result_text(), width, theme, base)]
        screen = self._screen_art()
        if screen is not None:
            lines = [*lines, *_wrap(_text_pieces(screen, base), width)]
        return lines

    def _collapsed_header(
        self, lines: list[StyledLine], width: int, base: pt.Style
    ) -> list[StyledLine]:
        """A collapsed Bash row paints ONE line: the header's first, cell-cut.

        The wrapped header of a long multi-line command would bury the log; its
        first visual line plus a trailing ``…`` says the same thing in one row.
        A first line that fits the content width — an EXACT fit included — is
        kept whole, so the ``…`` only appears once a cell was really cut.
        Every other tool keeps the wrapping header (its preview is one line by
        construction — ``_short_arg_summary`` flattened it).
        """

        if self._badge != TOOL_BADGES["Bash"]:
            return lines
        first = lines[0]
        if len(lines) > 1 or cells_of(first) > width:
            one = [*truncate_to(first, width - 1), ("…", base)]
            return [one]
        return [first]

    def _command_text(self) -> str:
        """The verbatim command the expanded Bash box carries.

        ``detail`` is the primary source (the core layer now stamps every
        tool's primary argument there); an older session event carries none, so
        its header minus the leading ``Bash `` prefix stands in — the header
        was built as ``f"{name} {preview}"`` and the Bash preview is the
        command with newlines flattened.
        """

        detail = self._detail
        if not detail:
            detail = self._header.removeprefix("Bash ")
        if len(detail) > self._MAX_DETAIL_CHARS:
            detail = detail[: self._MAX_DETAIL_CHARS] + "\n…(truncated)"
        return detail

    def _box_lines(
        self, text: str, width: int, theme: TuiTheme, base: pt.Style
    ) -> list[StyledLine]:
        """A rounded box around ``text``, in CONTENT space (no gutter).

        ``build_lines`` prepends the row's gutter after this returns, so a box
        built here stays inside the selection's content coordinates — the same
        space a drag addresses. The border paints ``text_disabled`` on the
        theme background and the content keeps the row's own ink; a width too
        narrow for the chrome (under 8 cells) degrades to plain wrapped lines
        rather than a broken box.
        """

        if width < 8:
            return _wrap(_text_pieces(Text(text), base), width)
        border = (
            pt.Style()
            .fg(theme.color(theme.text_disabled))
            .bg(theme.color(theme.background))
        )
        inner = width - 4
        out: list[StyledLine] = [
            [("╭", border), ("─" * (width - 2), border), ("╮", border)]
        ]
        for line in _wrap(_text_pieces(Text(text), base), inner):
            out.append([("│ ", border), *pad_to(line, inner, base), (" │", border)])
        out.append([("╰", border), ("─" * (width - 2), border), ("╯", border)])
        return out

    # ── result rendering ──────────────────────────────────────────────────
    def _result_text(self) -> str:
        """The result with any SGR stripped.

        A Computer result carries an inline preview of the art it also saved to a
        ``.art`` file; rendering those raw escape bytes as plain text is unreadable
        noise, and the file render below it is the one worth looking at.
        """

        result = self._result or ""
        if not self._unbounded_result and len(result) > self._MAX_RESULT_CHARS:
            result = result[: self._MAX_RESULT_CHARS] + "\n…(truncated)"
        return ascii_art.strip_sgr(result)

    def _screen_art(self) -> Text | None:
        """Truecolour render of what a Computer result saved, or None.

        The gate is the TOOL, then the saved path — never the action: ``screen``
        ends with "Saved to: …" and a ``screenshot`` appends the same line to its
        summary, so both render. A screenshot the model could not receive as pixels
        is precisely the one the user most wants to see.

        Read LAZILY (only while expanded) and cached for the life of the result, so
        a session holding hundreds of screenshots never touches the filesystem on
        load and never re-reads a file per frame.
        """

        if self._tool_name != "Computer":
            return None
        if self._screen_ready:
            # A cached image caption must re-arm the visibility a collapse
            # cleared, or the PNG lane stays lost once the row re-expands (the
            # caption is the image's exactly when a placement survived).
            if self.image is not None:
                self.image_visible = True
            return self._screen_text
        self._screen_ready = True
        self.image_visible = False
        result = self._result or ""

        # A graphics-capable terminal shows the raw PNG instead of the unicode
        # art; the caption stays the row's own text and the pixels are the app's
        # to place. The PNG is checked BEFORE the `.art`: a capture whose art file
        # could not be written (a full disk, a read-only home) still has its
        # pixels, and those are exactly what the graphics lane shows.
        png = png_path_in_result(result)
        if png is not None and self._ensure_image(png):
            self.image_visible = True
            self._screen_text = Text(f"── screen image ({png}) ──", style="dim")
            return self._screen_text

        path = art_path_in_result(result)
        if path is None:
            self._screen_text = None
            return None
        try:
            art = ascii_art.parse_art(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A missing or truncated capture must not take the whole log down.
            self._screen_text = None
            return None
        lines = art.body.split("\n")
        hidden = len(lines) - _ART_MAX_ROWS
        block = Text()
        block.append(f"── screen art ({path}) ──\n", style="dim")
        block.append_text(Text.from_ansi("\n".join(lines[:_ART_MAX_ROWS])))
        if hidden > 0:
            block.append(
                f"\n… {hidden} more rows (miniouto computer show {path})", style="dim"
            )
        self._screen_text = block
        return block

    def _ensure_image(self, png: str) -> bool:
        """Build the image placement once; False means "use the .art lane"."""

        if self.image is not None:
            return True
        if self._image_failed:
            return False
        module = _graphics_module()
        if module is None:
            return False  # retried next render: the module may still land
        if module.graphics_mode() == module.GRAPHICS_MODE_TEXT:
            # No graphics terminal: the `.art` text lane, unchanged. NOT latched
            # as a failure — the mode is a process-wide fact a later probe sets.
            return False
        width = max(1, (self._build_width or DEFAULT_WIDTH) - self._gutter_width)
        placement = module.ImagePlacement.create(png, width_cells=width)
        if placement is None:
            self._image_failed = True
            return False
        self.image = placement
        return True

    def release_image(self) -> None:
        """Delete the terminal-side image when this row goes away."""

        placement = self.image
        self.image = None
        self.image_visible = False
        if placement is not None:
            cleanup = getattr(placement, "cleanup", None)
            if cleanup is not None:
                cleanup()

    def image_placements(self) -> list[tuple[int, int, Any]]:
        """``(line, x, placement)`` for the image lane, for the app's overlay.

        Empty unless the row is expanded, the result named a usable PNG and the
        terminal speaks a graphics protocol — so a caller can iterate
        unconditionally.
        """

        if not self.image_visible or self.image is None:
            return []
        height = int(getattr(self.image, "height_cells", 0) or 0)
        width = int(getattr(self.image, "width_cells", 0) or 0)
        if height <= 0 or width <= 0:
            return []
        return [(self._image_line, self._gutter_width, self.image)]

    def remove(self) -> None:
        self.release_image()
        super().remove()


class AnswerRow(RowStatic):
    """Final-answer row (Markdown) — the TOP of the text hierarchy.

    ``$text-answer`` paints the whole renderable, which is the one rule that puts
    the agent's final answer on the brightest layer (12.33:1) against a 9.69:1
    body and a 5.11:1 dim layer. Selection comes from RowStatic — needed because a
    Markdown render is one Rich object with no per-widget hooks at all.
    """

    def __init__(self, content: str | Markdown, *, id: str | None = None) -> None:
        # The old app built these as `AnswerRow(Markdown(text))`; both shapes carry
        # the same markup, so accept either and keep the SOURCE: the painted form
        # depends on the width, which is not known yet.
        markup = content.markup if isinstance(content, Markdown) else str(content)
        super().__init__("", id=id)
        self._markup = markup

    @property
    def markup(self) -> str:
        return self._markup

    def _document_color(self, theme: TuiTheme) -> str:
        return theme.text_answer

    def _content_lines(
        self, width: int, theme: TuiTheme, base: pt.Style
    ) -> list[StyledLine]:
        return [
            [(span.content, span.style) for span in line.spans]
            for line in render_markdown(self._markup, width, base)
        ]

    def render(self) -> Text:
        """The markdown SOURCE as Text.

        Rich only lays Markdown out at a concrete width, so the painted form lives
        in :meth:`build_lines`; ``.plain`` here is the source, which is what a copy
        action wants and what the harness compares.
        """

        return Text(self._markup)
