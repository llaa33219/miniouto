"""Static widgets for the PyRatatui TUI: header, chips, panel, input, scroller.

WHY these are plain objects too: pyratatui has no widget system — the app composes
a whole frame each tick — so a "widget" here is state plus two functions:
``build_lines(width, theme)`` for what the frame paints and ``handle_*`` for what a
gesture does. Nothing mounts, nothing invalidates, and every piece is directly
unit-testable without a terminal.

What each one owes the frame:

* :class:`Header` — the ``$panel`` background, with the ``title — sub`` string
  centred in the cells the docked 10-cell clock space leaves; every cell on the
  row is ``$panel`` to the last column.
* :class:`StatusChip` — ``bg $boost``, bold, ``padding 0 1``, one cell of right
  margin, and the value rendered WHOLE: an earlier one-row layout elided the
  values with ``…`` and the owner rejected it, so the panel has two chip rows.
* :class:`ChatInput` — a multiline editor of our own (pyratatui's ``TextState`` is a
  single-line readline prompt), with the CSS look of Textual's TextArea in the old
  TUI: ``$surface`` band, a thick ``█`` primary rule on the left, ``padding 1 1``,
  ``margin 0 2``, soft wrap and a reversed block cursor.
* :class:`ChatScroll` + :class:`_RowWindow` — the windowed log: only a contiguous
  suffix of rows is kept "mounted", older chunks load as the view approaches the
  top, and far-above rows are evicted again. Textual reflowed every mounted child
  each frame; without windowing a long session gets slower the longer it runs.

All colours come from :class:`theme.TuiTheme`; the percentage tints are alpha
composites computed with :func:`render.blend`, which reproduces Textual's own
arithmetic (``primary 22%`` on ``#0a0e14`` = ``#123547``), so the port is a repaint
rather than a restyle.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.cells import cell_len
from rich.text import Text

from . import pt
from .render import blend, hex_to_rgb
from .rows import (
    DEFAULT_WIDTH,
    Node,
    StyledLine,
    pad_to,
    place_at,
    truncate_to,
)
from .theme import MINIOUTO_THEME, TuiTheme

__all__ = [
    "ACTION_TOLERANCE_LINES",
    "CHIP_GAP",
    "CHIP_MARGIN_RIGHT",
    "CHIP_PADDING",
    "EVICT_MARGIN_LINES",
    "HELP_TEXT",
    "INPUT_MARGIN_X",
    "INPUT_MAX_HEIGHT",
    "INPUT_MIN_HEIGHT",
    "JUMP_BUTTON_LABEL",
    "TAB_WIDTH",
    "TOP_LOAD_LINES",
    "WINDOW_CHUNK_ROWS",
    "WINDOW_INITIAL_ROWS",
    "WINDOW_MAX_ROWS",
    "_EVICT_MARGIN_LINES",
    "_TOP_LOAD_LINES",
    "_WINDOW_CHUNK_ROWS",
    "_WINDOW_INITIAL_ROWS",
    "_WINDOW_MAX_ROWS",
    "BottomPanel",
    "ChatInput",
    "ChatScroll",
    "Header",
    "JumpButton",
    "Size",
    "StatusChip",
    "_RowWindow",
    "at_bottom",
    "auto_text",
    "wrap_offsets",
]

# ─── shared constants ────────────────────────────────────────────────────────

# The quoting glyphs are written as escapes on purpose: they are the owner's exact
# baseline characters (see tui-visual-facts.md) and a literal copy in the source
# trips ruff's ambiguous-character rule for no benefit.
_LQUO = "\u2039"
_RQUO = "\u203a"
_MIDDOT = "\u00b7"

HELP_TEXT = (
    f"{_LQUO}Tab{_RQUO} cycle chips  {_MIDDOT}  "
    f"{_LQUO}Enter{_RQUO} open  {_MIDDOT}  "
    f"{_LQUO}Esc{_RQUO} cancel  {_MIDDOT}  "
    f"{_LQUO}Ctrl+L{_RQUO} clear  {_MIDDOT}  "
    f"{_LQUO}Ctrl+C{_RQUO} quit"
)
"""The bottom hint's plain text (88 cells, matching the Textual baseline).

Textual stored this as Rich markup with a ``[dim]`` wrapper; the port paints it
with an explicit muted style instead, so the string itself must carry no markup — a
stray ``[dim]`` would otherwise be drawn literally.
"""

ACTION_TOLERANCE_LINES = 2
"""Lines of rounding slack that still count as "at the bottom"."""

JUMP_BUTTON_LABEL = "- latest -"
"""Label of the floating "back to the tail" button."""

CHIP_PADDING = 1
"""``padding: 0 1`` — one cell either side of a chip's text."""

CHIP_MARGIN_RIGHT = 1
"""``margin: 0 1 0 0`` — how far a right-anchored chip stops short of the edge."""

CHIP_GAP = 1
"""One blank cell between the two chips when they do not fit side by side."""

INPUT_MIN_HEIGHT = 5
"""``height: 5`` floor: an empty input still shows a 5-row surface band."""

INPUT_MAX_HEIGHT = 12
"""Ceiling: past this the log is the thing that should grow, not the input."""

INPUT_VERTICAL_PADDING = 1
"""``padding: 1 1`` — one blank row above and below the text."""

INPUT_MARGIN_X = 2
"""``margin: 0 2`` — the band is inset two cells on both sides."""

INPUT_BORDER_GLYPH = "\u2588"
"""``█`` — Textual's ``border-left: thick``, drawn with the full block."""

INPUT_PADDING_X = 1
"""``padding: 1 1`` — one blank cell either side of the text."""

TAB_WIDTH = 4
"""Columns a tab expands to on the way INTO the editor.

A tab inside a ``Span`` renders as nothing, so it is expanded at insert time
rather than stored and silently swallowed at draw time.
"""

_WHITE = "#ffffff"
_BLACK = "#000000"
_AUTO_TEXT_ALPHA = 0.87
"""``$text`` is Textual's ``auto 87%``: the ink the platform derives."""
_AUTO_TEXT_LUMA_FLOOR = 0.5
"""Brightness above which Textual flips ``auto`` from white ink to black."""


def auto_text(theme: TuiTheme, background: str) -> str:
    """Textual's ``$text`` for a given background: ``auto 87%``, resolved.

    Rederiving it matters on one row in particular — the FOCUSED chip, whose
    background is the ``primary 22%`` bar. White at 87% over that bar is what
    Textual painted (``$text`` re-resolves per widget against its own background),
    so a pinned foreground there would be a visual regression.
    """

    red, green, blue = hex_to_rgb(background)
    luma = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
    ink = _BLACK if luma > _AUTO_TEXT_LUMA_FLOOR else _WHITE
    return blend(ink, background, _AUTO_TEXT_ALPHA)


class _Complete:
    """An already-complete awaitable: ``await x`` works, and so does dropping it.

    Textual's ``mount()`` is a coroutine and the ported tests ``await`` it; this
    port is synchronous, so returning a real coroutine would raise "never awaited"
    warnings at sync call sites. This satisfies both call styles.
    """

    def __await__(self) -> Any:
        return iter(())


def _awaitable_none() -> _Complete:
    return _Complete()


class Size:
    """``(width, height)`` in cells — Textual's ``Size``, minimal."""

    __slots__ = ("height", "width")

    def __init__(self, width: int = 0, height: int = 0) -> None:
        self.width = width
        self.height = height

    def __bool__(self) -> bool:
        return bool(self.width and self.height)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Size)
            and (self.width, self.height) == (other.width, other.height)
        )

    def __hash__(self) -> int:
        return hash((self.width, self.height))

    def __repr__(self) -> str:
        return f"Size({self.width}, {self.height})"


# ─── header ──────────────────────────────────────────────────────────────────


class Header(Node):
    """One-row application header: the centred ``title — sub`` string.

    No icon box: the owner asked for the ``⭘`` glyph at the top-left to go, so
    the row is just the chat title centred across the terminal. Textual's
    10-cell clock space stays docked right (its ``HeaderClockSpace`` was a
    Widget, not a CSS rule, so it occupies cells even with ``show_clock``
    off) — that is what keeps the title at ``(width - 10 - title) // 2``
    instead of the naive full-width centre. The separator and the subtitle
    are ``dim``; the title itself is body text.
    """

    CLOCK_WIDTH = 10
    """``HeaderClockSpace``'s docked width: 10 cells, docked right, always present."""

    def __init__(
        self, title: str = "", sub_title: str = "", *, id: str | None = None
    ) -> None:
        super().__init__(id=id)
        self.title = title
        self.sub_title = sub_title
        self._theme: TuiTheme = MINIOUTO_THEME

    def format_title(self) -> str:
        """``title — sub``, or the title alone when there is no subtitle."""

        if self.sub_title:
            return f"{self.title} \u2014 {self.sub_title}"
        return self.title

    def render(self) -> Text:
        text = Text(self.title)
        if self.sub_title:
            text.append(" \u2014 ", style="dim")
            text.append(self.sub_title, style="dim")
        return text

    def build_lines(self, width: int, theme: TuiTheme) -> list[StyledLine]:
        width = max(1, int(width))
        self._theme = theme
        background = pt.Style().bg(theme.color(theme.panel))
        body = background.fg(theme.color(theme.foreground))
        # Textual's `format_title` styles the separator and the subtitle with the
        # `dim` ATTRIBUTE over the header's own foreground (`(" — ", "dim")` and
        # `.stylize("dim")`), not with the muted palette entry — so the ink stays
        # the body colour and only its intensity drops.
        dim = body.add_modifier(pt.Modifier.dim())

        line: StyledLine = [(" " * width, background)]

        # Centred in what the docked clock space leaves, which is the box
        # Textual's HeaderTitle actually occupied.
        box = max(0, width - self.CLOCK_WIDTH)
        text_width = cell_len(self.format_title())
        start = max(0, (box - text_width) // 2)
        pieces: StyledLine = []
        if self.title:
            pieces.append((self.title, body))
        if self.sub_title:
            if self.title:
                pieces.append((" \u2014 ", dim))
            pieces.append((self.sub_title, dim))
        if pieces and start < width:
            line = place_at(line, start, truncate_to(pieces, width - start), background)
        self.place(0, 0, width, 1)
        return [line]


# ─── status chip ─────────────────────────────────────────────────────────────


class StatusChip(Node):
    """A focusable, clickable status chip that emits :class:`ChipClicked`.

    The chip grows to fit its value (``width: auto`` + ``text-wrap: nowrap``) and
    the value is rendered WHOLE — never elided. That single decision is why the
    panel keeps two chip rows: four chips on one line could only fit by truncating,
    and a truncated value hides exactly the part the user opened the chip to read.
    """

    can_focus = True

    VARIANTS = ("default", "muted", "accent")

    class ChipClicked:
        """Posted when the chip is activated (click or Enter)."""

        def __init__(self, chip: StatusChip) -> None:
            self.chip = chip

    def __init__(
        self,
        label: str,
        value: str,
        *,
        variant: str = "default",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._label = label
        self._value = value
        self.variant = variant if variant in self.VARIANTS else "default"
        self.hover = False
        self.app: Any = None
        """Set by the app so ``post_message`` has somewhere to deliver."""
        self.messages: list[Any] = []
        """Deliveries made before an app was attached (tests read this)."""
        self._theme: TuiTheme = MINIOUTO_THEME

    # ── state ─────────────────────────────────────────────────────────────
    @property
    def label(self) -> str:
        return self._label

    @property
    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value

    def display_text(self) -> str:
        """``label: value``, or just the value for a label-less chip (style)."""

        text = self._value or "-"
        return f"{self._label}: {text}" if self._label else text

    def render(self) -> Text:
        # Rendered whole, never elided: the chip is as wide as its value.
        return Text(self.display_text())

    @property
    def width_cells(self) -> int:
        """Cells this chip asks for: its text plus one pad either side."""

        return cell_len(self.display_text()) + 2 * CHIP_PADDING

    # ── painting ──────────────────────────────────────────────────────────
    def style(self, theme: TuiTheme) -> pt.Style:
        """The chip's style for its current focus / hover / resting state."""

        focus_bg = blend(theme.primary, theme.background, 0.22)
        if self.focused:
            return (
                pt.Style()
                .fg(theme.color(auto_text(theme, focus_bg)))
                .bg(theme.color(focus_bg))
                .add_modifier(pt.Modifier.bold() | pt.Modifier.reversed())
            )
        if self.hover:
            hover_bg = blend(theme.primary, theme.background, 0.12)
            return (
                pt.Style()
                .fg(theme.color(self._variant_color(theme)))
                .bg(theme.color(hover_bg))
                .add_modifier(pt.Modifier.bold() | pt.Modifier.underlined())
            )
        return (
            pt.Style()
            .fg(theme.color(self._variant_color(theme)))
            .bg(theme.color(theme.boost))
            .add_modifier(pt.Modifier.bold())
        )

    def _variant_color(self, theme: TuiTheme) -> str:
        if self.variant == "accent":
            return theme.accent
        if self.variant == "muted":
            return theme.text_muted
        # `color: $text` on `$boost` — auto 87% against the chip's own surface.
        return auto_text(theme, theme.boost)

    def build_lines(self, width: int, theme: TuiTheme) -> list[StyledLine]:
        """The chip painted into ``width`` cells: padding, text, padding.

        ``width`` is normally :attr:`width_cells`; a different box only pads or
        clips, so a chip never draws past what it was placed in.
        """

        self._theme = theme
        style = self.style(theme)
        text = truncate_to([(self.display_text(), style)], max(1, width - 2))
        pieces: StyledLine = [(" " * CHIP_PADDING, style), *text]
        return [pad_to(pieces, max(1, width), style)]

    def set_hover(self, value: bool) -> None:
        self.hover = value

    # ── activation ────────────────────────────────────────────────────────
    def post_message(self, message: Any) -> None:
        app = self.app
        post = getattr(app, "post_message", None)
        if post is not None:
            post(message)
        else:
            self.messages.append(message)

    def activate(self) -> None:
        self.post_message(StatusChip.ChipClicked(self))

    def on_click(self, event: Any = None) -> None:
        self.activate()

    def key_enter(self) -> None:
        self.activate()

    def handle_key(self, key: Any, app: Any = None) -> bool:
        if getattr(key, "code", "") != "Enter":
            return False
        self.app = self.app or app
        self.key_enter()
        return True

    def handle_mouse(self, ev: Any, x: int, y: int, app: Any = None) -> bool:
        self.app = self.app or app
        kind = str(getattr(ev, "kind", ""))
        if kind in ("down", "click") and int(getattr(ev, "button", 0)) == 0:
            self.activate()
            return True
        return False


# ─── bottom panel ────────────────────────────────────────────────────────────


class BottomPanel(Node):
    """The 4-row panel under the input: spinner, two chip rows, help hint.

    The panel's surface spans the terminal but its CONTENT stops at the chat
    column, so the chips line up with the log and the input. The chips sit in two
    rows, each anchored around a ``1fr`` spacer: model+reasoning on the left, the
    other pickers on the right.
    """

    ROWS: ClassVar[tuple[str, ...]] = (
        "spinner-row",
        "chip-row-a",
        "chip-row-b",
        "help-row",
    )

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.app: Any = None
        self._chips: dict[str, StatusChip] = {
            "outo": StatusChip("outo", "-", variant="accent", id="chip-outo"),
            "style": StatusChip("", "-", id="chip-style"),
            "subagent": StatusChip("subagent", "-", variant="muted", id="chip-subagent"),
            "session": StatusChip("session", "-", variant="muted", id="chip-session"),
        }
        self._framed: tuple[str, str] = ("", "")
        """``(frame, text)`` currently on the spinner row."""
        self._rows: dict[str, Node] = {}
        for name in self.ROWS:
            row = Node(id=name)
            self.mount(row)
            self._rows[name] = row
        for chip in self._chips.values():
            self.mount(chip)
        self._theme: TuiTheme = MINIOUTO_THEME

    # ── state ─────────────────────────────────────────────────────────────
    def set_value(self, kind: str, value: str) -> None:
        """Write a chip's value."""

        chip = self._chips.get(kind)
        if chip is not None:
            chip.set_value(value)

    def set_session(self, name: str) -> None:
        """Publish the live session name to its chip (row B, right)."""

        self.set_value("session", name or "-")

    def get_chip(self, kind: str) -> StatusChip | None:
        return self._chips.get(kind)

    @property
    def chips(self) -> dict[str, StatusChip]:
        return self._chips

    @property
    def spinner(self) -> tuple[str, str]:
        return self._framed

    def row(self, name: str) -> Node:
        """The structural row node (``spinner-row`` … ``help-row``)."""

        return self._rows[name]

    def render_spinner(self, frame: str, text: str) -> None:
        """Paint the bottom status line.

        ``frame`` empty with ``text`` set is a NOTICE — plain text, no glyph (a
        frozen spinner next to a finished action reads as "still working"). Both
        empty blanks the row: when nothing needs showing the line shows nothing.
        """

        self._framed = (frame or "", text or "")

    # ── painting ──────────────────────────────────────────────────────────
    def build_lines(self, width: int, theme: TuiTheme) -> list[StyledLine]:
        width = max(1, int(width))
        self._theme = theme
        rows = [
            self._spinner_line(width, theme),
            self._chip_line(width, theme, "outo", "style", 1),
            self._chip_line(width, theme, "subagent", "session", 2),
            self._help_line(width, theme),
        ]
        for index, name in enumerate(self.ROWS):
            self._rows[name].place(0, index, width, 1)
        self.place(0, 0, width, len(rows))
        return rows

    def _spinner_line(self, width: int, theme: TuiTheme) -> StyledLine:
        frame, text = self._framed
        # Every cell carries the theme background: an unstyled cell falls back to
        # the TERMINAL's default background, which is not `theme.background` and
        # made the spinner row read as a lighter band than the log above it.
        base = pt.Style().bg(theme.color(theme.background))
        blank: StyledLine = [(" " * width, base)]
        if not frame and not text:
            return blank
        pieces: StyledLine = [(" ", base)]  # padding: 0 1
        if frame:
            accent = base.fg(theme.color(theme.accent)).add_modifier(pt.Modifier.bold())
            pieces.append((frame, accent))
        if text:
            pieces.append(
                (
                    f" {text}" if frame else text,
                    base.fg(theme.color(theme.foreground)),
                )
            )
        return place_at(blank, 0, truncate_to(pieces, width), base)

    def _chip_line(
        self, width: int, theme: TuiTheme, left_kind: str, right_kind: str, y: int
    ) -> StyledLine:
        """One chip row: the left chip flush left, the right one flush right.

        When the two fit, the right chip is anchored to the row's right edge and
        the row reads as ``[left]      [right]``. When they do NOT fit — a 40-cell
        window, where ``subagent: <model> <reasoning>`` plus ``session: <name>``
        is far wider than the row — the right chip starts one space after the
        left chip ends and is CLIPPED at the row's right edge instead of being
        laid over it. Overflowing keeps both chips clickable, which is the rule
        this row exists for: a chip painted on top of another renders but cannot
        be hit, which is exactly how the style and session pickers went missing.
        """

        base = pt.Style().bg(theme.color(theme.background))
        line: StyledLine = [(" " * width, base)]
        left = self._chips[left_kind]
        right = self._chips[right_kind]

        left_width = min(left.width_cells, width)
        line = place_at(line, 0, left.build_lines(left_width, theme)[0], base)
        left.place(0, y, left_width, 1)

        # The right chip keeps its WHOLE width in its region even where the row
        # clips it: `region` is the click target, and a chip whose region stopped
        # at the edge would be a chip the user cannot reach at all.
        right_width = right.width_cells
        right_x = width - CHIP_MARGIN_RIGHT - right_width
        if right_x <= left_width:
            right_x = left_width + CHIP_GAP
        line = place_at(
            line, right_x, right.build_lines(right_width, theme)[0], base
        )
        right.place(right_x, y, right_width, 1)
        return line

    def _help_line(self, width: int, theme: TuiTheme) -> StyledLine:
        style = pt.Style().fg(theme.color(theme.text_muted)).bg(theme.color(theme.background))
        pieces: StyledLine = [(" ", style), (HELP_TEXT, style)]  # padding: 0 1
        return place_at(
            [(" " * width, style)], 0, truncate_to(pieces, width), style
        )

    # ── focus / mouse ─────────────────────────────────────────────────────
    def chip_at(self, x: int, y: int) -> StatusChip | None:
        """The chip occupying screen cell ``(x, y)``, if any."""

        for chip in self._chips.values():
            region = chip.region
            if region.height and region.contains(x, y):
                return chip
        return None

    def handle_mouse(self, ev: Any, x: int, y: int, app: Any = None) -> bool:
        kind = str(getattr(ev, "kind", ""))
        chip = self.chip_at(x, y)
        for candidate in self._chips.values():
            candidate.set_hover(candidate is chip and kind in ("move", "drag"))
        if chip is None:
            return False
        if kind in ("down", "click"):
            chip.handle_mouse(ev, x, y, app)
            return True
        return kind in ("move", "drag")

    def handle_key(self, key: Any, app: Any = None) -> bool:
        if getattr(key, "code", "") != "Enter":
            return False
        for chip in self._chips.values():
            if chip.focused:
                chip.handle_key(key, app)
                return True
        return False

    def focus_chip(self, kind: str) -> StatusChip | None:
        """Focus exactly one chip (``kind``), or none when it is unknown."""

        for name, chip in self._chips.items():
            chip.focused = name == kind
        return self._chips.get(kind)


# ─── chat input ──────────────────────────────────────────────────────────────


def _clamped_caret_column(text: str, column: int, room: int) -> int:
    """``column`` inside ``room`` cells, pulled back to a CHARACTER boundary.

    A 2-cell glyph that straddles ``room`` would otherwise leave the hardware
    caret on the glyph's SECOND half — half a character away from the position
    the reversed block paints. Walking the text's own cell boundaries costs one
    pass over a single display row, and only when the clamp actually fires.
    """

    if column <= room:
        return column
    used = 0
    for char in text:
        char_cells = cell_len(char)
        if used + char_cells > room:
            break
        used += char_cells
    return used


def wrap_offsets(text: str, width: int) -> list[tuple[int, int]]:
    """Display rows of ``text`` as half-open CHARACTER ranges, soft-wrapped.

    The editor needs this instead of :func:`render.word_wrap` for two reasons a
    transcript row never hits: the caret is an index into the text (so a row has to
    be a RANGE, not just a string), and trailing spaces are visible state the user
    just typed — they must not be trimmed away. Cell widths still come from
    ``cell_len``, so a wide character is never split and the caret lands on it.

    A row breaks at a space when there is one to break at, and hard-breaks at the
    cell boundary otherwise, mirroring the transcript wrapper.
    """

    width = max(1, int(width))
    rows: list[tuple[int, int]] = []
    for start, end in _logical_ranges(text):
        rows.extend(_wrap_range(text, start, end, width))
    return rows or [(0, 0)]


def _logical_ranges(text: str) -> list[tuple[int, int]]:
    """Every ``\\n``-delimited line as a character range (blank lines included)."""

    ranges: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(text):
        if char == "\n":
            ranges.append((start, index))
            start = index + 1
    ranges.append((start, len(text)))
    return ranges


def _wrap_range(text: str, start: int, end: int, width: int) -> list[tuple[int, int]]:
    """One logical line's character range → its display-row ranges."""

    rows: list[tuple[int, int]] = []
    cursor = start
    cells = 0
    index = start
    while index < end:
        char_cells = cell_len(text[index])
        if cells + char_cells > width:
            if index == cursor:
                # A single glyph wider than the whole row (a CJK char in a 1-cell
                # band): emit it alone and ADVANCE. Without this the empty row
                # (cursor, index) re-tests the same index forever.
                rows.append((cursor, index + 1))
                cursor = index + 1
                cells = 0
                index = cursor
                continue
            space = text.rfind(" ", cursor, index)
            if space > cursor:
                rows.append((cursor, space))
                cursor = space + 1
            else:
                rows.append((cursor, index))
                cursor = index
            cells = 0
            index = cursor
            continue
        cells += char_cells
        index += 1
    rows.append((cursor, end))
    return rows


class ChatInput(Node):
    """Multiline prompt editor: soft wrap, block cursor, Enter to send.

    pyratatui has no textarea (its ``TextState`` is a single-line readline prompt),
    so the editor is ours: text plus a caret index, navigation over the WRAPPED
    display rows, and the CSS look of the old TextArea — ``$surface`` band, thick
    ``█`` primary rule on the left, ``padding 1 1``, ``margin 0 2``.

    Enter submits (and only a non-blank value submits, which is how the old
    ``action_submit`` refused empty prompts); Shift+Enter and Alt+Enter insert a
    newline, matching a TextArea where Enter was bound to submit with priority.
    """

    can_focus = True

    class Submitted:
        """Posted when the user sends a non-empty prompt."""

        def __init__(self, value: str) -> None:
            self.value = value

    def __init__(self, text: str = "", *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._text = text
        self._cursor = len(text)
        self._scroll = 0
        self.app: Any = None
        self.messages: list[Any] = []
        self.show_cursor = True
        self._theme: TuiTheme = MINIOUTO_THEME
        self._last_width = DEFAULT_WIDTH

    # ── text / caret ──────────────────────────────────────────────────────
    @property
    def text(self) -> str:
        return self._text

    @text.setter
    def text(self, value: str) -> None:
        self._text = value
        self._cursor = min(self._cursor, len(value))
        self._scroll = 0

    @property
    def cursor(self) -> int:
        return self._cursor

    @cursor.setter
    def cursor(self, value: int) -> None:
        self._cursor = max(0, min(int(value), len(self._text)))

    def insert(self, fragment: str) -> None:
        """Insert ``fragment`` at the caret, expanding tabs on the way in."""

        if not fragment:
            return
        fragment = fragment.replace("\t", " " * TAB_WIDTH)
        self._text = self._text[: self._cursor] + fragment + self._text[self._cursor :]
        self._cursor += len(fragment)

    def delete_before(self) -> None:
        if self._cursor <= 0:
            return
        self._text = self._text[: self._cursor - 1] + self._text[self._cursor :]
        self._cursor -= 1

    def delete_at(self) -> None:
        if self._cursor >= len(self._text):
            return
        self._text = self._text[: self._cursor] + self._text[self._cursor + 1 :]

    def delete_word_before(self) -> None:
        start = self._word_boundary(-1)
        self._text = self._text[:start] + self._text[self._cursor :]
        self._cursor = start

    def delete_word_at(self) -> None:
        end = self._word_boundary(1)
        self._text = self._text[: self._cursor] + self._text[end :]

    def _word_boundary(self, direction: int) -> int:
        """The caret index one WORD away in ``direction`` (-1 back, +1 forward)."""

        text = self._text
        index = self._cursor
        if direction < 0:
            while index > 0 and text[index - 1] == " ":
                index -= 1
            while index > 0 and text[index - 1] != " ":
                index -= 1
            return index
        while index < len(text) and text[index] == " ":
            index += 1
        while index < len(text) and text[index] != " ":
            index += 1
        return index

    def move(self, delta: int) -> None:
        self.cursor = self._cursor + delta

    def move_word(self, direction: int) -> None:
        self.cursor = self._word_boundary(direction)

    def line_bounds(self) -> tuple[int, int]:
        """Character range of the LOGICAL line holding the caret."""

        start = self._text.rfind("\n", 0, self._cursor) + 1
        end = self._text.find("\n", self._cursor)
        return (start, len(self._text) if end < 0 else end)

    def move_home(self) -> None:
        self.cursor = self.line_bounds()[0]

    def move_end(self) -> None:
        self.cursor = self.line_bounds()[1]

    def move_vertical(self, delta: int) -> None:
        """Move one DISPLAY row up/down, keeping the caret's cell column.

        Display rows, not logical lines: a soft-wrapped long paragraph is what the
        user sees, and an up-arrow that jumped a whole paragraph would surprise.
        """

        rows = wrap_offsets(self._text, self.inner_width)
        index = self._caret_row(rows)
        target = index + delta
        if target < 0 or target >= len(rows):
            return
        column = cell_len(self._text[rows[index][0] : self._cursor])
        self.cursor = self._index_for_column(rows[target], column)

    def _caret_row(self, rows: list[tuple[int, int]]) -> int:
        for index, (start, end) in enumerate(rows):
            if start <= self._cursor < end:
                return index
            if start == end == self._cursor:
                return index
        return len(rows) - 1

    def _index_for_column(self, row: tuple[int, int], column: int) -> int:
        start, end = row
        used = 0
        index = start
        while index < end:
            char_cells = cell_len(self._text[index])
            if used + char_cells > column:
                return index
            used += char_cells
            index += 1
        return end

    # ── geometry ──────────────────────────────────────────────────────────
    def set_width(self, width: int) -> None:
        self._last_width = max(1, int(width))

    @property
    def inner_width(self) -> int:
        """Cells available for text: the band minus margin, rule and padding."""

        return max(
            1,
            self._last_width
            - 2 * INPUT_MARGIN_X
            - 1  # the left rule
            - 2 * INPUT_PADDING_X,
        )

    def display_rows(self) -> list[tuple[int, int]]:
        return wrap_offsets(self._text, self.inner_width)

    def height(self, width: int | None = None, theme: TuiTheme | None = None) -> int:
        """``max(5, min(content_rows + 2, 12))`` — the old ``watch_virtual_size``."""

        if width:
            self.set_width(width)
        rows = len(self.display_rows())
        band = rows + 2 * INPUT_VERTICAL_PADDING
        return max(INPUT_MIN_HEIGHT, min(band, INPUT_MAX_HEIGHT))

    @property
    def visible_rows(self) -> int:
        """Text rows the band can show at its current height."""

        return max(1, self.height() - 2 * INPUT_VERTICAL_PADDING)

    # ── painting ──────────────────────────────────────────────────────────
    def build_lines(self, width: int, theme: TuiTheme) -> list[StyledLine]:
        width = max(1, int(width))
        self._theme = theme
        self.set_width(width)
        height = self.height()
        inner = self.inner_width
        visible = max(1, height - 2 * INPUT_VERTICAL_PADDING)

        rows = wrap_offsets(self._text, inner)
        caret_row = self._caret_row(rows)
        # Keep the caret inside the visible band: the editor scrolls, the band
        # never grows past INPUT_MAX_HEIGHT.
        if caret_row < self._scroll:
            self._scroll = caret_row
        elif caret_row >= self._scroll + visible:
            self._scroll = caret_row - visible + 1
        self._scroll = max(0, min(self._scroll, max(0, len(rows) - visible)))

        surface = pt.Style().bg(theme.color(theme.surface))
        margin = pt.Style().bg(theme.color(theme.background))
        rule = pt.Style().fg(theme.color(theme.primary)).bg(theme.color(theme.surface))
        body = pt.Style().fg(theme.color(theme.foreground)).bg(theme.color(theme.surface))
        content_width = inner + 2 * INPUT_PADDING_X

        lines: list[StyledLine] = []
        # The band's own top and bottom padding rows never carry text; the row
        # indexes are all that move as the editor scrolls, which is why the content
        # row is `index - padding + scroll` rather than a slice of `rows`.
        for index in range(height):
            content_index = index - INPUT_VERTICAL_PADDING + self._scroll
            in_band = (
                INPUT_VERTICAL_PADDING <= index < height - INPUT_VERTICAL_PADDING
                and 0 <= content_index < len(rows)
            )
            pieces: StyledLine = [
                (" " * INPUT_MARGIN_X, margin),
                (INPUT_BORDER_GLYPH, rule),
                (" " * INPUT_PADDING_X, surface),
            ]
            if in_band:
                painted = self._paint_row(content_index, rows, body)
                pieces.extend(painted)
                used = INPUT_PADDING_X + sum(cell_len(run) for run, _ in painted)
                pieces.append((" " * max(0, content_width - used), surface))
            else:
                pieces.append((" " * (content_width - INPUT_PADDING_X), surface))
            # margin-right: the band stops two cells short of the column edge.
            pieces.append((" " * INPUT_MARGIN_X, margin))
            lines.append(pad_to(pieces, width, margin))
        self.place(0, 0, width, height)
        return lines

    def _paint_row(
        self, index: int, rows: list[tuple[int, int]], body: pt.Style
    ) -> StyledLine:
        """One display row, with the reversed block cursor over the caret cell.

        The caret belongs to exactly ONE display row, and that row is the one
        `_caret_row` names — NOT every row whose index range happens to touch
        the caret. Two mistakes follow from deriving it per-row instead:

        * a soft wrap CONSUMES the space it breaks at, so a caret sitting on
          that space (`"aaa bbb"` wrapped after `aaa`, caret on the space) is
          inside no range at all and painted no block;
        * the same index is one row's exclusive end and the next row's start,
          which painted the block TWICE — two blocks a row apart.
        """

        start, end = rows[index]
        text = self._text[start:end]
        if not (self.show_cursor and index == self._caret_row(rows)):
            return [(text, body)] if text else []
        # A caret on a consumed wrap space sits one cell BEFORE this row's
        # first character; clamping puts the block on that first cell, which is
        # where `cursor_cell` reports it and where the next keystroke lands.
        offset = max(0, min(self._cursor - start, len(text)))
        before = text[:offset]
        after = text[offset:]
        cursor_char = after[:1] or " "
        rest = after[1:] if after else ""
        pieces: StyledLine = []
        if before:
            pieces.append((before, body))
        pieces.append((cursor_char, body.add_modifier(pt.Modifier.reversed())))
        if rest:
            pieces.append((rest, body))
        return pieces

    def cursor_cell(self, width: int) -> tuple[int, int] | None:
        """Absolute ``(x, y)`` of the caret inside the band, or None when hidden.

        The app positions the real terminal cursor here after the frame: a reversed
        block only IMPLIES the position, while a blinking I-beam needs the hardware
        cursor.
        """

        width = max(1, int(width))
        self.set_width(width)
        rows = wrap_offsets(self._text, self.inner_width)
        caret_row = self._caret_row(rows)
        if not self._scroll <= caret_row < self._scroll + self.visible_rows:
            return None
        start = rows[caret_row][0]
        lead = INPUT_MARGIN_X + 1 + INPUT_PADDING_X
        # The band's last usable cell, in CONTENT cells after the rule + padding.
        column = _clamped_caret_column(
            self._text[start : self._cursor],
            cell_len(self._text[start : self._cursor]),
            width - 1 - lead,
        )
        x = lead + column
        y = INPUT_VERTICAL_PADDING + (caret_row - self._scroll)
        return (x, y)

    def render(self) -> Text:
        """The editor's plain text — what a copy action or a test compares."""

        return Text(self._text)

    # ── submission ────────────────────────────────────────────────────────
    def post_message(self, message: Any) -> None:
        app = self.app
        post = getattr(app, "post_message", None)
        if post is not None:
            post(message)
        else:
            self.messages.append(message)

    def action_submit(self) -> None:
        """Submit the prompt — only when it holds more than whitespace."""

        if self._text.strip():
            self.post_message(ChatInput.Submitted(self._text))

    # ── keys ──────────────────────────────────────────────────────────────
    def handle_key(self, key: Any, app: Any = None) -> bool:
        self.app = self.app or app
        code = str(getattr(key, "code", ""))
        ctrl = bool(getattr(key, "ctrl", False))
        alt = bool(getattr(key, "alt", False))
        shift = bool(getattr(key, "shift", False))

        if code == "Enter":
            if shift or alt:
                self.insert("\n")
                return True
            self.action_submit()
            # Consumed either way: Enter never falls through to a widget below.
            return True
        if code == "Backspace":
            if ctrl:
                self.delete_word_before()
            else:
                self.delete_before()
            return True
        if code == "Delete":
            if ctrl:
                self.delete_word_at()
            else:
                self.delete_at()
            return True
        if code == "Left":
            if ctrl:
                self.move_word(-1)
            else:
                self.move(-1)
            return True
        if code == "Right":
            if ctrl:
                self.move_word(1)
            else:
                self.move(1)
            return True
        if code == "Up":
            self.move_vertical(-1)
            return True
        if code == "Down":
            self.move_vertical(1)
            return True
        if code == "Home":
            self.move_home()
            return True
        if code == "End":
            self.move_end()
            return True
        if code == "Tab":
            # Focus cycling is the APP's: the editor must not swallow Tab.
            return False
        if ctrl or alt:
            return False
        if len(code) == 1:
            self.insert(code)
            return True
        return False

    def handle_paste(self, text: str) -> bool:
        self.insert(text)
        return True

    def handle_mouse(self, ev: Any, x: int, y: int, app: Any = None) -> bool:
        """A click inside the band puts the caret on the clicked cell."""

        if str(getattr(ev, "kind", "")) != "down":
            return False
        region = self.region
        if region.height and not region.contains(x, y):
            return False
        row_index = y - region.y - INPUT_VERTICAL_PADDING + self._scroll
        rows = wrap_offsets(self._text, self.inner_width)
        if not 0 <= row_index < len(rows):
            return True
        column = max(0, x - region.x - INPUT_MARGIN_X - 1 - INPUT_PADDING_X)
        self.cursor = self._index_for_column(rows[row_index], column)
        return True


# ─── scrolling ───────────────────────────────────────────────────────────────


class ChatScroll(Node):
    """Scrollable log with anchored follow and top-edge reporting.

    Textual's ``anchor()`` pins the scroll position to the bottom on every reflow
    until the user scrolls away (and re-engages when they scroll back) — that
    replaces an unconditional ``scroll_end`` on every row mount, so reading history
    while a turn streams no longer yanks the view back down.

    Every scroll action takes ``animate=False``: paging through a log that is still
    streaming must land instantly rather than slide. The keyword is ACCEPTED (and
    ignored) so the ported call sites keep reading like Textual's.
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.scroll_y = 0
        self.scroll_x = 0
        self.max_scroll_x = 0
        self.size = Size()
        self._content_height: int | None = None
        self._anchored = True
        self._anchor_released = False
        self.theme: TuiTheme = MINIOUTO_THEME
        self.content_width = DEFAULT_WIDTH
        self.on_scroll: Any = None
        """``_on_log_scroll(y)`` hook, wired by the app."""
        self._mounted: list[Node] = []

    # ── geometry ──────────────────────────────────────────────────────────
    @property
    def content_height(self) -> int:
        """Total height of the mounted rows, or the app's explicit override."""

        if self._content_height is not None:
            return self._content_height
        return sum(self._row_height(row) for row in self._mounted)

    @content_height.setter
    def content_height(self, value: int | None) -> None:
        self._content_height = value

    @property
    def viewport_height(self) -> int:
        return max(0, self.size.height)

    @property
    def max_scroll_y(self) -> int:
        return max(0, self.content_height - self.viewport_height)

    @property
    def is_vertical_scroll_end(self) -> bool:
        return self.scroll_y >= self.max_scroll_y or not self.viewport_height

    def resize(self, width: int, height: int) -> None:
        self.content_width = max(1, int(width))
        self.size = Size(max(1, int(width)), max(0, int(height)))
        self.scroll_y = min(self.scroll_y, self.max_scroll_y)

    def _row_height(self, row: Node) -> int:
        try:
            return max(0, int(row.height(self.content_width, self.theme)))
        except Exception:  # a row that cannot measure still occupies one line
            return 1

    # ── scrolling ─────────────────────────────────────────────────────────
    def anchor(self, anchor: bool = True) -> None:
        """Follow the tail until the user scrolls away (Textual's ``anchor``)."""

        self._anchored = anchor
        if anchor:
            self._anchor_released = False
            self.scroll_end(animate=False)

    def release_anchor(self) -> None:
        """Stop following the tail without moving the view."""

        self._anchor_released = True

    def _set_scroll(self, value: float) -> None:
        old = self.scroll_y
        new = max(0, min(round(value), self.max_scroll_y))
        self.scroll_y = new
        # Scrolling up is what releases the follow pin; landing back on the tail
        # re-engages it, which is how the anchor recovers itself.
        self._anchor_released = new < self.max_scroll_y
        if new != old and self.on_scroll is not None:
            self.on_scroll(new)

    def check_anchor(self) -> None:
        """Re-pin to the tail when content grew while the view was at the bottom."""

        if self._anchored and not self._anchor_released:
            self.scroll_y = self.max_scroll_y

    def scroll_to(self, x: float | None = None, y: float | None = None, **_: Any) -> None:
        if x is not None:
            self.scroll_x = max(0, int(x))
        self._set_scroll(self.scroll_y if y is None else y)

    def scroll_end(self, animate: bool = False, **_: Any) -> None:
        self._set_scroll(self.max_scroll_y)

    def scroll_home(self, animate: bool = False, **_: Any) -> None:
        self._set_scroll(0)

    def scroll_page_up(self, animate: bool = False, **_: Any) -> None:
        self._set_scroll(self.scroll_y - max(1, self.viewport_height))

    def scroll_page_down(self, animate: bool = False, **_: Any) -> None:
        self._set_scroll(self.scroll_y + max(1, self.viewport_height))

    def scroll_by(self, lines: int) -> None:
        """Wheel scrolling: ``lines`` rows (three per detent, per the app)."""

        self._set_scroll(self.scroll_y + lines)

    def at_bottom(self) -> bool:
        return at_bottom(self)

    # ── children ──────────────────────────────────────────────────────────
    def mount(
        self,
        *rows: Node,
        before: Node | None = None,
        after: Node | None = None,
    ) -> Any:
        """Attach rows to the log; returns an awaitable for the ported call sites.

        Textual's ``mount`` was a coroutine that also ran a layout pass; here the
        order is all that matters, because the frame measures the children itself.
        """

        for row in rows:
            row.parent = self
            index = len(self._mounted)
            if before is not None and before in self._mounted:
                index = self._mounted.index(before)
            elif after is not None and after in self._mounted:
                index = self._mounted.index(after) + 1
            self._mounted.insert(index, row)
            if row not in self.children:
                self.children.append(row)
        self.check_anchor()
        return _awaitable_none()

    def remove(self, row: Node) -> None:
        if row in self._mounted:
            self._mounted.remove(row)
        if row in self.children:
            self.children.remove(row)
        row.parent = None
        self.check_anchor()

    def remove_children(self) -> Any:
        for row in list(self._mounted):
            row.parent = None
        self._mounted.clear()
        self.children.clear()
        self._content_height = None
        self.scroll_y = 0
        return _awaitable_none()

    @property
    def rows(self) -> list[Node]:
        return self._mounted

    def visible_rows(self) -> list[Node]:
        """The mounted rows that intersect the viewport, in paint order."""

        out: list[Node] = []
        offset = 0
        top = self.scroll_y
        bottom = top + self.viewport_height
        for row in self._mounted:
            height = self._row_height(row)
            if offset + height > top and offset < bottom:
                out.append(row)
            offset += height
            if offset >= bottom:
                break
        return out


def at_bottom(log: Any | None) -> bool:
    """True when ``log`` is scrolled to (or within tolerance of) its tail.

    An unlaid-out or absent log counts as "at bottom": the jump button only ever
    appears in response to scrolling AWAY, so the safe default is to stay hidden.
    """

    if log is None:
        return True
    max_scroll = getattr(log, "max_scroll_y", 0) or 0
    if max_scroll <= 0:
        return True
    return (getattr(log, "scroll_y", 0) or 0) >= max_scroll - ACTION_TOLERANCE_LINES


class JumpButton(Node):
    """The floating ``- latest -`` button: shown only when the log is scrolled away.

    Textual drew it docked to the bottom of the log layer, ``$panel`` background
    with muted text, one row high, ``padding 0 2``. Clicking re-engages the follow
    anchor AND returns keyboard focus to the input — hiding a focused button would
    otherwise leave the app with no focused widget at all and typing would die
    until the user clicked back in.
    """

    def __init__(self, log: ChatScroll | None = None, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.log = log
        self.display = False
        self.hover = False
        self.app: Any = None
        self._theme: TuiTheme = MINIOUTO_THEME

    def render(self) -> Text:
        return Text(JUMP_BUTTON_LABEL)

    @property
    def width_cells(self) -> int:
        return cell_len(JUMP_BUTTON_LABEL) + 4  # padding: 0 2

    def sync(self) -> bool:
        """Recompute visibility from the log's scroll position."""

        self.display = not at_bottom(self.log)
        return self.display

    def build_lines(self, width: int, theme: TuiTheme) -> list[StyledLine]:
        self._theme = theme
        focus_bg = blend(theme.primary, theme.background, 0.22)
        hover_bg = blend(theme.primary, theme.background, 0.12)
        if self.focused:
            style = (
                pt.Style()
                .fg(theme.color(auto_text(theme, focus_bg)))
                .bg(theme.color(focus_bg))
            )
        elif self.hover:
            style = (
                pt.Style()
                .fg(theme.color(auto_text(theme, hover_bg)))
                .bg(theme.color(hover_bg))
            )
        else:
            style = pt.Style().fg(theme.color(theme.text_muted)).bg(theme.color(theme.panel))
        pieces: StyledLine = [
            (" " * 2, style),
            (JUMP_BUTTON_LABEL, style),
            (" " * 2, style),
        ]
        self.place(0, 0, max(1, width), 1)
        return [pad_to(pieces, max(1, width), style)]

    def activate(self) -> None:
        log = self.log
        if log is not None:
            # `anchor()` is the follow idiom: it re-engages the bottom pin so new
            # rows keep arriving, where a bare scroll_end would snap back once and
            # then detach again.
            log.anchor()
        self.sync()
        target = getattr(self.app, "_input", None)
        if target is not None:
            target.focused = True
        self.focused = False

    def on_click(self, event: Any = None) -> None:
        self.activate()

    def key_enter(self) -> None:
        self.activate()

    def handle_mouse(self, ev: Any, x: int, y: int, app: Any = None) -> bool:
        self.app = self.app or app
        kind = str(getattr(ev, "kind", ""))
        if kind in ("move", "drag"):
            self.hover = True
            return True
        if kind in ("down", "click"):
            self.activate()
            return True
        return False


# ─── windowing ───────────────────────────────────────────────────────────────

WINDOW_INITIAL_ROWS = 100
"""Tail rows mounted on a session load."""

WINDOW_CHUNK_ROWS = 50
"""Rows prepended per top-edge chunk load."""

WINDOW_MAX_ROWS = 300
"""Soft cap; excess is evicted from the top of the mounted window."""

TOP_LOAD_LINES = 10
"""``scroll_y`` at or below which a chunk load triggers."""

EVICT_MARGIN_LINES = 60
"""Rows this close above the viewport are kept (they may be scrolled into)."""

# The old module spelled these with a leading underscore and the ported tests
# import them that way, so the public names above keep a private alias rather than
# forcing every caller to learn the rename.
_WINDOW_INITIAL_ROWS = WINDOW_INITIAL_ROWS
_WINDOW_CHUNK_ROWS = WINDOW_CHUNK_ROWS
_WINDOW_MAX_ROWS = WINDOW_MAX_ROWS
_TOP_LOAD_LINES = TOP_LOAD_LINES
_EVICT_MARGIN_LINES = EVICT_MARGIN_LINES


class _RowWindow:
    """Windowed row mounting for one scrollable log.

    All rows ever added live in ``rows`` (row objects, so collapse / result / image
    state survives eviction), but only the contiguous suffix starting at ``first``
    is mounted. A long session therefore costs the same per frame no matter how many
    turns it holds: older rows mount in chunks as the user scrolls toward the top
    (:meth:`maybe_load_older`) and rows far above the viewport are evicted again
    once the window overflows (:meth:`evict`).

    Scroll compensation: adding rows ABOVE the viewport shifts the viewed content
    down, removing them shifts it up — both are countered by adjusting ``scroll_y``
    by the exact height of the rows involved. Textual did that after a reflow
    (``call_after_refresh``); here the height is computed on demand, so the
    correction lands in the same call.
    """

    def __init__(self) -> None:
        self.rows: list[Node] = []
        self.first = 0

    def reset(self) -> None:
        self.rows.clear()
        self.first = 0

    def remove(self, row: Node, log: ChatScroll | None = None) -> None:
        """Drop a row from the window AND the log, keeping ``first`` aligned.

        Two callers: a live stream row replaced by the canonical answer row
        (re-mounting the answer at the tail would otherwise leave the streamed prefix
        above it as a duplicate), and a row whose message was removed from history by
        a repair.

        Detaching goes through ``log.remove(row)``, never ``row.remove()``: the log
        paints ``rows`` (which IS ``_mounted``), while ``Node.remove`` only splices
        ``parent.children`` — so the row left behind stayed paintable forever, and
        every turn painted the live stream row ALONGSIDE its canonical answer.
        ``ChatScroll.remove`` is the single detach path: it drops the row from
        ``_mounted`` and from ``children`` and clears ``parent``.

        ``first`` is clamped, not just decremented: removing the last row of a
        fully-evicted window would otherwise leave it pointing past the end of
        ``rows``, and the next top-edge chunk load would index off it.
        """

        target = log if log is not None else self._log_of(row)
        for index, candidate in enumerate(self.rows):
            if candidate is row:
                del self.rows[index]
                if index < self.first:
                    self.first -= 1
                break
        self.first = min(self.first, len(self.rows))
        if target is not None:
            target.remove(row)
        elif row.parent is not None:
            # No log to detach from (a window driven without one, as the pruning
            # tests do): fall back to the plain node detach.
            row.remove()

    def _log_of(self, row: Node) -> ChatScroll | None:
        """The log a row was mounted on, or None when it was never mounted."""

        parent = row.parent
        return parent if isinstance(parent, ChatScroll) else None

    def append(self, log: ChatScroll, row: Node, *, defer_mount: bool = False) -> None:
        self.rows.append(row)
        if defer_mount:
            return
        log.mount(row)
        # No scroll_end: the log's anchor() follows the tail while the user is at
        # the bottom and stays put when they have scrolled up.
        self.evict(log)

    def mount_tail(self, log: ChatScroll) -> None:
        """After a bulk fill, mount only the trailing window."""

        self.first = max(0, len(self.rows) - WINDOW_INITIAL_ROWS)
        for row in self.rows[self.first :]:
            if row.parent is not log:
                log.mount(row)
        log.scroll_end(animate=False)

    def maybe_load_older(self, log: ChatScroll, scroll_y: float) -> None:
        if scroll_y <= TOP_LOAD_LINES and self.first > 0:
            self._load_older(log)

    def _load_older(self, log: ChatScroll) -> None:
        start = max(0, self.first - WINDOW_CHUNK_ROWS)
        new_rows = self.rows[start : self.first]
        self.first = start
        # The new chunk goes in front of the first currently mounted row. A pruned
        # window can have nothing mounted (`first == len(rows)`), and there is then
        # no anchor to insert before.
        anchor_index = start + len(new_rows)
        anchor = self.rows[anchor_index] if anchor_index < len(self.rows) else None
        added = 0
        for row in new_rows:
            if row.parent is not None:
                continue
            if anchor is None:
                log.mount(row)
            else:
                log.mount(row, before=anchor)
            added += self._height(row, log)
        if added:
            # Compensate the shift the new chunk put above the viewport — but
            # only for a view that was scrolled INTO the load zone. At scroll_y
            # == 0 the user asked for the very top (Home), and compensating
            # there walks the viewport DOWN by a chunk on every press, so the
            # top of the log is never actually reached.
            target = 0 if log.scroll_y <= 0 else log.scroll_y + added
            log.scroll_to(y=target, animate=False)

    def evict(self, log: ChatScroll) -> None:
        excess = (len(self.rows) - self.first) - WINDOW_MAX_ROWS
        if excess <= 0:
            return
        viewport_top = log.scroll_y
        removed_height = 0
        victims: list[Node] = []
        for row in self.rows[self.first :]:
            if len(victims) >= excess:
                break
            height = self._height(row, log)
            if removed_height + height > viewport_top - EVICT_MARGIN_LINES:
                break
            removed_height += height
            victims.append(row)
        if not victims:
            return
        self.first += len(victims)
        for row in victims:
            log.remove(row)
        # At the bottom the anchor re-pins to the tail, so only an up-scrolled view
        # needs the shift compensated.
        if removed_height and log.scroll_y < log.max_scroll_y - 1:
            log.scroll_to(y=max(0, log.scroll_y - removed_height), animate=False)

    def _height(self, row: Node, log: ChatScroll) -> int:
        return log._row_height(row)
