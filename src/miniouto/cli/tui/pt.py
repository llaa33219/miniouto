"""The slice of ``pyratatui`` miniouto's TUI uses, re-implemented on ``xnano``.

WHY this module exists: every TUI layer was written against ``pyratatui``
(``import pyratatui as pt``). The rendering substrate is moving to ``xnano`` /
``xnano-core``, but the behavioural suite describes the LOGIC of the port, not
the Rust binding — so the cheap, low-risk move is a compat shim: this module
answers to the exact ``pt`` surface our code exercises, and each call site
changes only its import (``import pyratatui as pt`` → ``from . import pt``).

Every semantic below was READ OFF pyratatui 0.3.0 (introspection plus a pty
probe), never assumed:

* **Immutable builders.** ``Style.fg`` / ``.bold()`` / ``.patch`` each return a
  NEW style; the receiver never changes. Rows hold shared style objects across
  frames, so an in-place builder would rewrite history.
* **Copy-returning getters.** ``Line.spans`` and ``Text.lines`` hand out a COPY
  list: a caller pushes onto its own object, never onto the node it read.
* **Widths are CELLS, not characters.** ``Span("한글").width()`` is 4 (via
  ``rich.cells.cell_len``), not 2. pyratatui answers 2 there; the number the
  port needs is the one the terminal paints, and EVERY row layout in this code
  base already measures with ``cell_len`` for exactly that reason.
* **``patch`` = "the other's set fields win" and modifiers UNION.** A base with
  ``dim`` patched by ``bold`` is ``BOLD | DIM`` — exactly how the paint order
  below composes span → line → text styles.
* **``Text.push_str`` does not split.** It appends ONE line holding the string
  verbatim; ``Text.from_string`` is the splitter, and it drops a single trailing
  newline (``"a\\nb\\n"`` is two lines, ``""`` is none).
* **``TextState`` is a self-contained readline editor.** ``xnano_core
  .TextEditor`` cannot serve here: its ``handle_key`` accepts only an
  ``xnano_core.KeyEvent``, and that type cannot be instantiated from Python
  ("cannot create KeyEvent instances"), so the app's duck-typed
  :class:`~.input.Key` could never reach it.
* **A painted style is the span's own over the line's over the text block's**,
  modifiers unioning across the three. ``Paragraph.style`` is the widget style:
  it fills the rest of the rect and is the WEAKEST layer for the text itself.

``Terminal`` has two modes over one painting path. Headless (the default) owns
an offscreen xnano canvas and paints every ``render_widget(Paragraph/Text,
Rect)`` as one styled write per row, which ``output_text()`` /
``output_lines()`` / ``output_ansi()`` / ``.canvas`` then expose for the tests.
``Terminal(real_screen=True)`` — what ``run_tui`` builds — opens that same
canvas on the actual terminal instead, and doubles as the app's input source
(:meth:`Terminal.hook`): xnano owns stdin, so its hooks are what feed the app's
own ``Key``/``Mouse``/``Paste``/``Resize`` handlers. Both modes compose the
identical frame; only the flush and the alt screen differ.
"""

from __future__ import annotations

import contextlib
import enum
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import xnano
import xnano.styles as xstyles
from rich.cells import cell_len

__all__ = [
    "Color",
    "Line",
    "Modifier",
    "Paragraph",
    "PromptStatus",
    "Rect",
    "Span",
    "Style",
    "Terminal",
    "Text",
    "TextState",
]


# ─── colors ─────────────────────────────────────────────────────────────────


_NAMED_COLORS: tuple[str, ...] = (
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "gray",
    "dark_gray",
    "light_red",
    "light_green",
    "light_yellow",
    "light_blue",
    "light_magenta",
    "light_cyan",
    "white",
)
"""The names pyratatui's ``Color`` exposes as ``Color.<name>()`` statics."""


@dataclass(frozen=True, repr=False)
class Color:
    """A pyratatui color: RGB, indexed (256-palette), named, or reset.

    Equality is by VALUE, which is what the tests compare
    (``color("#2fc2ff") == pt.Color.rgb(0x2F, 0xC2, 0xFF)``), and ``str()``
    reproduces pyratatui's dump (``Color(Rgb(47, 194, 255))``) because the Rich
    bridges in ``modals`` / ``nodes`` parse that text.
    """

    _kind: str
    """``"rgb"``, ``"indexed"``, ``"named"`` or ``"reset"``."""

    _value: str
    """``#rrggbb`` for RGB, the slot number for indexed, else the name."""

    # ── constructors ────────────────────────────────────────────────────────
    @staticmethod
    def rgb(red: int, green: int, blue: int) -> Color:
        """A truecolor value; an out-of-range channel raises, never wraps."""

        channels = (int(red), int(green), int(blue))
        if any(channel < 0 or channel > 255 for channel in channels):
            raise ValueError(f"rgb channel out of range: {channels}")
        return Color("rgb", "#{:02x}{:02x}{:02x}".format(*channels))

    @staticmethod
    def indexed(index: int) -> Color:
        """A slot in the 256-color palette."""

        slot = int(index)
        if slot < 0 or slot > 255:
            raise ValueError(f"palette index out of range: {slot}")
        return Color("indexed", str(slot))

    @staticmethod
    def reset() -> Color:
        """The terminal's own default foreground/background."""

        return Color("reset", "reset")

    # ── substrate ───────────────────────────────────────────────────────────
    def to_xnano(self) -> str | Any:
        """The value xnano accepts for this color.

        RGB crosses as its ``#rrggbb`` string. An INDEXED color crosses as an
        ``xnano.Color`` object rather than the string ``"indexed(5)"``: xnano
        rejects that string form, while the object is what its writer emits as
        ``38;5;<n>``.
        """

        if self._kind == "indexed":
            return xstyles.Color.indexed(int(self._value))
        return self._value

    # ── pyratatui's dump ────────────────────────────────────────────────────
    def dumped(self) -> str:
        """This color WITHOUT the ``Color(...)`` wrapper (``Rgb(47, 194, 255)``).

        ``Style``'s dump embeds that form (``fg=Some(Rgb(47, 194, 255))``) and
        the Rich bridges in ``modals`` / ``nodes`` read colors back out of it.
        """

        if self._kind == "rgb":
            red, green, blue = (
                int(self._value[1:3], 16),
                int(self._value[3:5], 16),
                int(self._value[5:7], 16),
            )
            return f"Rgb({red}, {green}, {blue})"
        if self._kind == "indexed":
            return f"Indexed({self._value})"
        if self._kind == "reset":
            return "Reset"
        return "".join(part.capitalize() for part in self._value.split("_"))

    def __str__(self) -> str:
        return f"Color({self.dumped()})"

    def __repr__(self) -> str:
        return str(self)


def _named_color_factory(name: str) -> Callable[[], Color]:
    """A zero-argument ``Color.<name>()`` static for the fixed name table."""

    def make() -> Color:
        return Color("named", name)

    return make


for _color_name in _NAMED_COLORS:
    setattr(Color, _color_name, staticmethod(_named_color_factory(_color_name)))
del _color_name


# ─── modifiers ──────────────────────────────────────────────────────────────

_FLAG_TO_TOKEN: dict[str, str] = {
    "bold": "BOLD",
    "dim": "DIM",
    "italic": "ITALIC",
    "underlined": "UNDERLINED",
    "slow_blink": "SLOW_BLINK",
    "reversed": "REVERSED",
    "hidden": "HIDDEN",
    "crossed_out": "CROSSED_OUT",
}
"""Flag name → the token pyratatui prints in ``Style(... mods=BOLD | DIM)``.

The insertion order is pyratatui's own, so a dumped style reads the same. The
consumers that read modifiers out of ``str(style)`` (``nodes.rich_from_pt``,
``modals._to_rich``) depend on the token spelling.
"""

_FLAG_ORDER: tuple[str, ...] = tuple(_FLAG_TO_TOKEN)


@dataclass(frozen=True, repr=False)
class Modifier:
    """A set of text attributes, combinable with ``|`` and ``&``.

    A frozenset of flag names, so ``Modifier.bold() | Modifier.dim()`` equals
    any other construction of that pair, and ``Modifier()`` is the empty set
    (``render.style_from_rich`` always calls ``add_modifier``, even with none).
    """

    flags: frozenset[str] = field(default_factory=frozenset)

    @staticmethod
    def bold() -> Modifier:
        return Modifier(frozenset({"bold"}))

    @staticmethod
    def dim() -> Modifier:
        return Modifier(frozenset({"dim"}))

    @staticmethod
    def italic() -> Modifier:
        return Modifier(frozenset({"italic"}))

    @staticmethod
    def underlined() -> Modifier:
        return Modifier(frozenset({"underlined"}))

    @staticmethod
    def slow_blink() -> Modifier:
        return Modifier(frozenset({"slow_blink"}))

    @staticmethod
    def reversed() -> Modifier:
        return Modifier(frozenset({"reversed"}))

    @staticmethod
    def hidden() -> Modifier:
        return Modifier(frozenset({"hidden"}))

    @staticmethod
    def crossed_out() -> Modifier:
        return Modifier(frozenset({"crossed_out"}))

    def __or__(self, other: Modifier) -> Modifier:
        return Modifier(self.flags | other.flags)

    def __and__(self, other: Modifier) -> Modifier:
        return Modifier(self.flags & other.flags)

    def __str__(self) -> str:
        tokens = [_FLAG_TO_TOKEN[flag] for flag in _FLAG_ORDER if flag in self.flags]
        return "Modifier({})".format(" | ".join(tokens) if tokens else "NONE")

    def __repr__(self) -> str:
        return str(self)


_NO_MODIFIER = Modifier()
"""The empty modifier — shared, because the type is immutable."""


# ─── styles ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, repr=False)
class Style:
    """An immutable style: colors plus modifiers.

    Every builder returns a NEW style (pyratatui semantics) and equality is
    structural: rows compare styles with ``==`` to decide whether a repaint
    changed anything. ``patch`` lays another style on top — its set fields win,
    modifiers union — which is how the painter composes the three style levels.
    """

    foreground: Color | None = None
    background: Color | None = None
    modifiers: Modifier = _NO_MODIFIER

    # ── builders (each returns a NEW style) ─────────────────────────────────
    def fg(self, color: Color) -> Style:
        """A copy painted in ``color``."""

        return Style(color, self.background, self.modifiers)

    def bg(self, color: Color) -> Style:
        """A copy on a ``color`` background."""

        return Style(self.foreground, color, self.modifiers)

    def add_modifier(self, modifier: Modifier) -> Style:
        """A copy carrying ``modifier``'s flags IN ADDITION to its own."""

        return Style(self.foreground, self.background, self.modifiers | modifier)

    def patch(self, other: Style) -> Style:
        """A copy with ``other``'s SET fields on top of this one's."""

        return Style(
            other.foreground if other.foreground is not None else self.foreground,
            other.background if other.background is not None else self.background,
            self.modifiers | other.modifiers,
        )

    def bold(self) -> Style:
        return self.add_modifier(Modifier.bold())

    def dim(self) -> Style:
        return self.add_modifier(Modifier.dim())

    def italic(self) -> Style:
        return self.add_modifier(Modifier.italic())

    def underlined(self) -> Style:
        return self.add_modifier(Modifier.underlined())

    def reversed(self) -> Style:
        return self.add_modifier(Modifier.reversed())

    def crossed_out(self) -> Style:
        return self.add_modifier(Modifier.crossed_out())

    def hidden(self) -> Style:
        return self.add_modifier(Modifier.hidden())

    def slow_blink(self) -> Style:
        return self.add_modifier(Modifier.slow_blink())

    # ── substrate ───────────────────────────────────────────────────────────
    def to_xnano(self) -> xstyles.Style:
        """This style as an ``xnano.styles.Style``.

        ``slow_blink`` maps onto xnano's ``blink``: it has one blink, and it is
        the slow one terminals implement. ``blink`` and ``hidden`` reach the cell
        grid faithfully (as style bits ``0x10`` / ``0x80``), but xnano 2.3.0's
        ANSI encoder emits neither SGR 5 nor SGR 8 — verified by running a
        hand-built style through ``xnano_core.cell_grid_to_ansi``. No miniouto
        row paints either attribute, so the gap is documented, not worked around.
        """

        flags = self.modifiers.flags
        return xstyles.Style(
            foreground=self.foreground.to_xnano() if self.foreground is not None else None,
            background=self.background.to_xnano() if self.background is not None else None,
            bold="bold" in flags,
            dim="dim" in flags,
            italic="italic" in flags,
            underline="underlined" in flags,
            blink="slow_blink" in flags,
            reverse="reversed" in flags,
            hidden="hidden" in flags,
            crossed_out="crossed_out" in flags,
        )

    def __str__(self) -> str:
        """pyratatui's dump, which the Rich bridges parse for colors/mods."""

        tokens = [_FLAG_TO_TOKEN[flag] for flag in _FLAG_ORDER if flag in self.modifiers.flags]
        return "Style(fg={}, bg={}, mods={})".format(
            f"Some({self.foreground.dumped()})" if self.foreground is not None else "None",
            f"Some({self.background.dumped()})" if self.background is not None else "None",
            " | ".join(tokens) if tokens else "NONE",
        )

    def __repr__(self) -> str:
        return str(self)


# ─── text primitives ────────────────────────────────────────────────────────


@dataclass(frozen=True, repr=False)
class Span:
    """One styled run of text. Its width is measured in terminal CELLS."""

    content: str = ""
    style: Style | None = None

    def width(self) -> int:
        """The cells this run occupies (a CJK glyph is two, a tab is zero)."""

        return cell_len(self.content)

    def styled(self, style: Style) -> Span:
        """A copy carrying ``style`` INSTEAD of this span's own (pyratatui)."""

        return Span(self.content, style)

    def __str__(self) -> str:
        return f"Span({self.content!r})"

    def __repr__(self) -> str:
        return str(self)


class Line:
    """One row of styled runs.

    ``.spans`` returns a COPY: the row and node layers read a line's spans and
    push onto their own objects, never onto the node they read.
    """

    __slots__ = ("_spans", "_style")

    def __init__(self, spans: Iterable[Span] | None = None, style: Style | None = None) -> None:
        self._spans: list[Span] = list(spans or [])
        self._style = style

    # ── access ──────────────────────────────────────────────────────────────
    @property
    def spans(self) -> list[Span]:
        """This line's runs, as a copy."""

        return list(self._spans)

    @property
    def style(self) -> Style | None:
        """The line-wide style, patched UNDER each span's own when painted."""

        return self._style

    def width(self) -> int:
        """The cells the whole line occupies."""

        return sum(span.width() for span in self._spans)

    # ── builders ────────────────────────────────────────────────────────────
    def push_span(self, span: Span) -> None:
        """Append one run in place (pyratatui's ``push_span`` returns None)."""

        self._spans.append(span)

    def styled(self, style: Style) -> Line:
        """A copy whose line-wide style is ``style``."""

        return Line(self._spans, style)

    @staticmethod
    def from_string(text: str) -> Line:
        """One line holding ``text`` verbatim — newlines are NOT split."""

        return Line([Span(text)])

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Line):
            return NotImplemented
        return self._spans == other._spans and self._style == other._style

    def __str__(self) -> str:
        return f"Line(spans={len(self._spans)})"

    def __repr__(self) -> str:
        return str(self)


class Text:
    """A block of lines. ``.lines`` returns a COPY, like ``Line.spans``."""

    __slots__ = ("_alignment", "_lines", "_style")

    def __init__(
        self,
        lines: Sequence[Line] | None = None,
        style: Style | None = None,
        *,
        alignment: str = "left",
    ) -> None:
        self._lines: list[Line] = list(lines or [])
        self._style = style
        self._alignment = alignment

    # ── access ──────────────────────────────────────────────────────────────
    @property
    def lines(self) -> list[Line]:
        """This block's lines, as a copy."""

        return list(self._lines)

    @property
    def style(self) -> Style | None:
        """The block-wide style, patched UNDER every line when painted."""

        return self._style

    @property
    def alignment(self) -> str:
        """``"left"`` (the default), ``"center"`` or ``"right"``."""

        return self._alignment

    @property
    def height(self) -> int:
        """How many rows the block occupies."""

        return len(self._lines)

    def width(self) -> int:
        """The widest line, in cells."""

        return max((line.width() for line in self._lines), default=0)

    # ── builders ────────────────────────────────────────────────────────────
    def push_line(self, line: Line) -> None:
        """Append one line in place."""

        self._lines.append(line)

    def push_str(self, text: str) -> None:
        """Append ONE line holding ``text`` verbatim (pyratatui does not split).

        Use :meth:`from_string` when the newlines should become lines.
        """

        self._lines.append(Line([Span(text)]))

    def styled(self, style: Style) -> Text:
        """A copy whose block-wide style is ``style``."""

        return Text(self._lines, style, alignment=self._alignment)

    def centered(self) -> Text:
        """A copy whose lines are centered inside the painter's rect."""

        return Text(self._lines, self._style, alignment="center")

    def right_aligned(self) -> Text:
        """A copy whose lines are right-aligned inside the painter's rect."""

        return Text(self._lines, self._style, alignment="right")

    @staticmethod
    def from_string(text: str) -> Text:
        """Split ``text`` on ``\\n``; one trailing newline adds no extra line."""

        parts = text.split("\n")
        if parts and parts[-1] == "":
            parts.pop()
        return Text([Line.from_string(part) for part in parts])

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Text):
            return NotImplemented
        return (
            self._lines == other._lines
            and self._style == other._style
            and self._alignment == other._alignment
        )

    def __str__(self) -> str:
        return f"Text(lines={len(self._lines)})"

    def __repr__(self) -> str:
        return str(self)


# ─── geometry ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rect:
    """A rectangle in cell coordinates.

    Frozen, because a region is DERIVED per frame and replaced, never mutated.
    ``contains`` takes a CELL (``rect.contains(x, y)``) — that is how our rows,
    widgets and modals ask the question; pyratatui's own ``Rect.contains`` takes
    a ``Rect``, and no call site of ours uses that form.
    """

    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    def area(self) -> int:
        """The number of cells."""

        return self.width * self.height

    def is_empty(self) -> bool:
        """True when either side is zero, i.e. nothing can be painted."""

        return self.width == 0 or self.height == 0

    def inner(self, horizontal: int = 1, vertical: int = 1) -> Rect:
        """The rect shrunk by ``horizontal`` columns and ``vertical`` rows.

        A shrink that would invert the size answers the zero rect, which is what
        pyratatui returns and what lets layout code paint nothing.
        """

        width = self.width - 2 * int(horizontal)
        height = self.height - 2 * int(vertical)
        if width < 0 or height < 0:
            return Rect(0, 0, 0, 0)
        return Rect(self.x + int(horizontal), self.y + int(vertical), width, height)

    def intersection(self, other: Rect) -> Rect | None:
        """The overlap with ``other``, or ``None`` when they do not overlap."""

        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.x + self.width, other.x + other.width)
        bottom = min(self.y + self.height, other.y + other.height)
        if right <= left or bottom <= top:
            return None
        return Rect(left, top, right - left, bottom - top)

    def contains(self, x: int, y: int) -> bool:
        """Whether the cell ``(x, y)`` is inside (half-open on both axes)."""

        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height


# ─── paragraphs (a carrier the painter understands) ─────────────────────────


class Paragraph:
    """The text ``app.render`` paints, plus the layout knobs it sets.

    pyratatui's ``Paragraph`` is a full widget; in this port its only consumer
    is ``ChatTUI.render``, which hands over pre-wrapped lines and a 1-row rect
    and lets :class:`Terminal` do the writing. So this object carries the text
    and the knobs, and every builder returns a NEW paragraph (pyratatui behaves
    the same way — verified by identity).
    """

    __slots__ = ("_alignment", "_scroll_x", "_scroll_y", "_style", "_trim", "_wrap", "text")

    def __init__(self, text: Text) -> None:
        self.text = text
        self._style: Style | None = None
        self._wrap = False
        self._trim = True
        self._scroll_y = 0
        self._scroll_x = 0
        self._alignment: str | None = None

    @staticmethod
    def from_string(text: str) -> Paragraph:
        """A paragraph of ``text``, split on newlines like ``Text.from_string``."""

        return Paragraph(Text.from_string(text))

    # ── builders ────────────────────────────────────────────────────────────
    def block(self, block: Any) -> Paragraph:
        """A copy framed by ``block``.

        Our code paints its own borders (``build_lines`` composes them), so the
        block is accepted and dropped rather than silently mis-drawn.
        """

        del block
        return self._copy()

    def style(self, style: Style) -> Paragraph:
        """A copy wearing the widget ``style``: it fills the rect, weakest layer."""

        paragraph = self._copy()
        paragraph._style = style
        return paragraph

    def wrap(self, wrap: bool = True, trim: bool = True) -> Paragraph:
        """A copy that re-wraps. A no-op for painting: the TUI pre-wraps lines."""

        paragraph = self._copy()
        paragraph._wrap = bool(wrap)
        paragraph._trim = bool(trim)
        return paragraph

    def scroll(self, y: int = 0, x: int = 0) -> Paragraph:
        """A copy scrolled by ``y`` rows and ``x`` cells."""

        paragraph = self._copy()
        paragraph._scroll_y = max(0, int(y))
        paragraph._scroll_x = max(0, int(x))
        return paragraph

    def alignment(self, alignment: str) -> Paragraph:
        """A copy aligned ``"left"``, ``"center"`` or ``"right"``."""

        paragraph = self._copy()
        paragraph._alignment = _normalize_alignment(alignment)
        return paragraph

    def centered(self) -> Paragraph:
        """``alignment("center")``, spelled the way pyratatui spells it."""

        return self.alignment("center")

    def right_aligned(self) -> Paragraph:
        """``alignment("right")``, spelled the way pyratatui spells it."""

        return self.alignment("right")

    def left_aligned(self) -> Paragraph:
        """``alignment("left")``, spelled the way pyratatui spells it."""

        return self.alignment("left")

    # ── the painter's view ──────────────────────────────────────────────────
    @property
    def style_or_none(self) -> Style | None:
        """The widget style, or ``None``."""

        return self._style

    @property
    def scroll_y(self) -> int:
        """Rows scrolled off the top."""

        return self._scroll_y

    @property
    def scroll_x(self) -> int:
        """Cells scrolled off the left."""

        return self._scroll_x

    @property
    def alignment_or_text(self) -> str:
        """The paragraph's own alignment, else the text block's."""

        return self._alignment or self.text.alignment

    def _copy(self) -> Paragraph:
        paragraph = Paragraph(self.text)
        paragraph._style = self._style
        paragraph._wrap = self._wrap
        paragraph._trim = self._trim
        paragraph._scroll_y = self._scroll_y
        paragraph._scroll_x = self._scroll_x
        paragraph._alignment = self._alignment
        return paragraph

    def __str__(self) -> str:
        return f"Paragraph(lines={self.text.height}, wrap={str(self._wrap).lower()})"

    def __repr__(self) -> str:
        return str(self)


_ALIGNMENTS = {"left": "left", "center": "center", "centre": "center", "right": "right"}


def _normalize_alignment(alignment: str) -> str:
    """Map an alignment spelling onto ``left``/``center``/``right``."""

    return _ALIGNMENTS.get(str(alignment).lower(), "left")


# ─── the background a CLEAR paints (the resize flash) ───────────────────────


_STDOUT_FD = 1
"""The descriptor the terminal is written on (xnano runs with ``capture=False``)."""

_RESIZE_CLEAR_SGR = "\x1b[48;2;{red};{green};{blue}m"
"""The SGR a terminal's erase fills with (``48;2;r;g;b``)."""


def _bg_sgr(hex_value: str) -> str:
    """The ``ESC[48;2;r;g;b m`` sequence for a ``#rrggbb`` colour.

    Raises on anything else: a silently wrong colour here would be a flash of
    the wrong shade, which is the bug this guards against.
    """

    text = str(hex_value).strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"not a #rrggbb colour: {hex_value!r}")
    return _RESIZE_CLEAR_SGR.format(
        red=int(text[0:2], 16), green=int(text[2:4], 16), blue=int(text[4:6], 16)
    )


def _write_screen(payload: str) -> None:
    """Write escape bytes to the terminal, never raising into the caller."""

    with _suppressed():
        data = payload.encode("ascii", "replace")
        while data:
            written = os.write(_STDOUT_FD, data)
            if written <= 0:
                return
            data = data[written:]


_CARET_SHAPE = "bar"
_CARET_BLINKING = True
"""How the hardware caret looks while the input band owns it.

xnano's ``shape=None`` means "keep the user's own cursor shape", so a canvas
that sets only ``position`` + ``is_visible`` emits no ``DECSCUSR`` at all — a
pty probe found not one ``ESC[N q`` in a whole session of typing. A position
with no shape is drawn in whatever the terminal happens to be set to, and on
many terminals that is invisible, which the owner read as "there is no
cursor". A blinking bar is what a text field shows, so the caret names it.

Re-asserting the same value every frame is free: xnano puts a ``DECSCUSR`` on
the wire only when the shape or the blink bit actually changed (30 assignments
of ``'bar'`` produced exactly one ``ESC[5 q`` on a pty).
"""


class Frame:
    """The frame a draw callback receives: ``.area`` and ``.render_widget``."""

    __slots__ = ("_area", "_paint")

    def __init__(self, area: Rect, paint: Callable[[Any, Rect], None]) -> None:
        self._area = area
        self._paint = paint

    @property
    def area(self) -> Rect:
        """The paintable area — one rect covering the whole canvas."""

        return self._area

    def render_widget(self, widget: Any, area: Rect) -> None:
        """Paint ``widget`` into ``area``, one styled write per row."""

        self._paint(widget, area)


class Terminal:
    """An ``xnano`` canvas wearing the pyratatui ``Terminal`` surface.

    Two modes, one class, because the app's frame loop is the same either way:

    * **Headless** (the default: no ``real_screen``): the canvas is offscreen
      (``xnano.new``), ``draw(fn)`` runs ``fn(frame)`` and paints what it
      rendered, and the painted rows are readable through :meth:`output_text` /
      :meth:`output_lines` / :meth:`output_ansi`, which is what the unit tests
      assert on. ``restore()`` releases the canvas; leaving the ``with`` block
      deliberately does not, so a caller can still read the last frame
      afterwards.
    * **Real screen** (``Terminal(real_screen=True)``, what ``run_tui`` builds):
      ``__enter__`` opens the canvas on the actual terminal — xnano claims the
      alternative screen, turns on the input reporting its hooks need, and runs
      its own draw thread — and ``__exit__`` closes it, which is what emits
      ``?1049l``. Exactly one ``?1049h``/``?1049l`` pair reaches the wire,
      because xnano owns both ends of it.

    The two modes share the whole painting path on purpose: :meth:`draw` fills
    the same per-row placements and the same :class:`Frame` either way, so the
    frame the tests read headless is the frame the terminal shows. What differs
    is WHO flushes it — an offscreen canvas needs an explicit ``render()``, a
    live one is drawn by xnano's loop.

    **Input.** On the real screen this object is also the app's input source:
    :meth:`hook` registers the callbacks ``run_async`` translates into our
    ``Key``/``Mouse``/``Paste``/``Resize`` events. Hooks MUST be registered
    before ``__enter__`` opens the canvas, because xnano turns the terminal
    settings a hook needs on at OPEN time and never afterwards — a mouse hook
    added to an already-open canvas is attached and never runs. ``__enter__``
    does the registration, so a caller only has to say what it wants.

    **Output capture is OFF** (``capture=False``). xnano's default replaces fd 1
    with a pipe while the canvas is up, so anything the program prints cannot
    corrupt the frame. miniouto needs the opposite: it writes OSC sequences to
    fd 1 directly (the window title, the clipboard), and it polls ``TIOCGWINSZ``
    on fd 1 to notice a resize. Measured on a pty, the captured fd answers
    ``Inappropriate ioctl for device`` and the OSC never reaches the terminal —
    so with ``capture`` on, resize detection and both OSC lanes die. The frame
    itself is protected anyway: everything the app paints goes through xnano.
    """

    def __init__(
        self,
        inline_height: int | None = None,
        *,
        width: int = 80,
        height: int = 24,
        real_screen: bool = False,
        fps: float = 60.0,
    ) -> None:
        self._inline_height = inline_height
        self._width = max(1, int(width))
        self._height = max(1, int(height))
        self._real_screen = bool(real_screen)
        self._fps = max(1.0, float(fps))
        self._canvas: Any = None
        self._placements: list[Any] = []
        self._rows: dict[tuple[int, int, int], Any] = {}
        """Placements by ``(x, y, width)``, kept across frames so they can be reused."""
        self._row_marks: dict[tuple[int, int, int], int] = {}
        """The frame number each placement was last painted in."""
        self._frame_marks = 0
        self._last_area: tuple[int, int] | None = None
        self._cursor_visible = False
        self._cursor_cell: tuple[int, int] | None = None
        """The cell last WRITTEN to xnano's cursor, so an unchanged tick writes nothing."""
        self._hooks: list[tuple[str, Callable[[Any], None]]] = []
        self._opened = False
        self._screen_bg: str | None = None
        """The ``#rrggbb`` the terminal's ERASES paint, or ``None`` for its default.

        Set by :meth:`set_screen_background`, and re-asserted from the resize
        hook before xnano clears: xnano erases the display on a resize with no
        background of its own, so without this the clear lands on the terminal
        default and the whole screen flashes.
        """

    # ── input hooks ─────────────────────────────────────────────────────────
    def hook(self, kind: str, callback: Callable[[Any], None]) -> None:
        """Hear one kind of input on the real screen.

        ``kind`` is xnano's hook trigger: ``'key'``, ``'mouse'``, ``'scroll'``,
        ``'paste'``, ``'resize'``. Registration is deferred until ``__enter__``
        opens the canvas, because xnano decides which terminal settings to turn
        on from the hooks that exist when the canvas goes up. On a headless
        canvas nothing is listening (there is no input to hear), and the call is
        recorded and inert — the ``Pilot`` injects events directly instead.
        """

        self._hooks.append((str(kind), callback))
        canvas = self._canvas
        if canvas is not None:
            canvas.hook(callback, on=str(kind))

    def set_screen_background(self, hex_value: str | None) -> None:
        """Say what colour the terminal's ERASES should paint.

        xnano erases the whole display (``ESC[2J``) whenever the canvas is
        resized, and it emits that erase OUTSIDE its synchronized-output block —
        measured on a pty over six resizes: eight ``ESC[2J``, every one of them
        ahead of the ``ESC[?2026h``. An erase is painted the instant it arrives,
        so it lands on the terminal's DEFAULT background and the screen shows a
        full-window flash of the wrong colour until the themed frame follows.
        That is the owner's "the screen feels unstable".

        A back-colour-erase terminal fills an erase with the CURRENT SGR
        background, so naming the theme background right before the erase turns
        the flash into the theme's own colour: the screen goes from one themed
        frame straight to the next. :meth:`_guard_resize_clear` does that on
        every resize report, and this method is how the app says WHICH colour,
        at boot and again whenever the theme changes.

        ``None`` means "stop guarding": the erase paints the terminal default
        again, which is what a headless canvas and a caller that never themed
        the screen want.
        """

        self._screen_bg = None if hex_value is None else str(hex_value)
        self._paint_screen_background()

    def _paint_screen_background(self) -> None:
        """Assert the screen background now, if there is one and a real screen."""

        if not self._real_screen or self._screen_bg is None:
            return
        # The SGR covers the write AND the colour conversion: a malformed value
        # must cost the guard, never the frame.
        with _suppressed():
            _write_screen(_bg_sgr(self._screen_bg))

    def _guard_resize_clear(self, _context: Any = None) -> None:
        """Re-assert the screen background before xnano's resize clear.

        Registered on the REAL canvas as a ``'resize'`` hook, and that ordering
        is the whole mechanism: xnano runs the hook first and emits the clear
        afterwards, so the SGR written here is what the erase paints. Verified
        on a pty — the hook's bytes land ahead of both ``ESC[2J``, and the clear
        after it leaves the theme background on every cell.
        """

        self._paint_screen_background()

    # ── context ─────────────────────────────────────────────────────────────
    def __enter__(self) -> Terminal:
        """Enter the terminal.

        Headless: nothing happens until the first draw. Real screen: the canvas
        is created, its hooks are registered, and it is opened on the terminal —
        which is what puts up the alt screen and turns on mouse/paste reporting.
        """

        if self._real_screen:
            self._open_real_screen()
        return self

    def __exit__(self, *_exc: object) -> bool:
        """Leave the terminal; the real screen's alt-screen pair closes here."""

        self.restore()
        return False

    def _open_real_screen(self) -> None:
        """Claim the screen: create the canvas, hook it, open it, size up."""

        canvas = xnano.new()
        # The resize-flash guard goes on FIRST, before the caller's hooks: xnano
        # runs hooks in registration order, and this one only has to get its SGR
        # onto the wire ahead of xnano's own erase. Being registered before
        # `open()` also means it joins the canvas without needing input, so it
        # cannot turn a terminal setting on by accident.
        canvas.hook(self._guard_resize_clear, on="resize")
        for kind, callback in self._hooks:
            canvas.hook(callback, on=kind)
        # `mouse='motion'` is what makes row hover work (the log highlights the
        # row under the pointer), and `scroll`/`paste` are the wheel and
        # bracketed paste. `interrupt=False` keeps Ctrl+C as OUR key event: with
        # the default xnano stops the canvas itself, and the app would never see
        # the chord its own router owns.
        canvas.open(
            mouse="motion",
            scroll=True,
            paste=True,
            interrupt=False,
            capture=False,
            fps=self._fps,
            # `disambiguated` is REQUIRED, not a nicety: with the default
            # ("basic") xnano reads a bare CR as Ctrl+J, so Enter never reached
            # our router as Enter and submit was dead on the real terminal
            # (measured on a pty: `hi\r` produced keys h, i, ctrl+j). The
            # disambiguated protocol is what makes CR arrive as `enter`.
            keyboard="disambiguated",
        )
        self._canvas = canvas
        self._opened = True
        self._width = max(1, int(canvas.screen.width))
        self._height = max(1, int(canvas.screen.height))

    # ── the canvas ──────────────────────────────────────────────────────────
    @property
    def canvas(self) -> Any:
        """The underlying ``xnano`` canvas (an offscreen one when headless)."""

        if self._canvas is None:
            self._canvas = xnano.new(width=self._width, height=self._height)
        return self._canvas

    @property
    def real_screen(self) -> bool:
        """Whether this terminal draws on the actual screen (not offscreen)."""

        return self._real_screen

    @property
    def cursor_visible(self) -> bool:
        """Whether the caret is currently shown (see :meth:`show_cursor`)."""

        return self._cursor_visible

    def area(self) -> Rect:
        """The paintable area: the canvas, whose origin is always 0, 0.

        On the real screen the size is read off the live canvas every call, so a
        resize is visible immediately; the app compares it against
        ``self.size`` each tick and goes through its ordinary ``Resize`` path.
        """

        canvas = self._canvas
        if canvas is not None:
            return Rect(0, 0, max(1, int(canvas.screen.width)), max(1, int(canvas.screen.height)))
        return Rect(0, 0, self._width, self._height)

    # ── the frame loop ──────────────────────────────────────────────────────
    def draw(self, draw_fn: Callable[[Frame], None]) -> None:
        """Run ``draw_fn(frame)``, then paint and present the frame.

        Placements are REUSED across frames rather than made fresh each time: a
        row that paints the same cells at the same width keeps its placement and
        only has its text replaced, so the canvas can diff and emit nothing when
        the row did not change. When a frame paints FEWER rows than the last one
        (a resize, a modal that covered a row and is gone), the leftovers are
        removed first, or the old text would stay on screen underneath the new
        frame.
        """

        self._frame_marks += 1
        area = self.area()
        self._width, self._height = area.width, area.height
        if (area.width, area.height) != self._last_area:
            # A resize invalidates every placement's geometry at once.
            self._remove_placements()
            self._last_area = (area.width, area.height)
        draw_fn(Frame(area, self.render_widget))
        self._drop_stale_placements()
        # A live canvas redraws itself on its own loop; asking it to render here
        # would draw twice per frame. Offscreen, this IS the presentation.
        if not self._real_screen:
            self.canvas.render()

    def _drop_stale_placements(self) -> None:
        """Remove the placements this frame did NOT paint."""

        stale = [key for key in self._rows if self._row_marks.get(key) != self._frame_marks]
        for key in stale:
            placement = self._rows.pop(key)
            placement.remove()
            self._placements = [item for item in self._placements if item is not placement]

    def render_widget(self, widget: Any, area: Rect) -> None:
        """Paint a ``Paragraph``/``Text`` into ``area``, one write per row.

        The style chain is pyratatui's: the span's own style wins, then the
        line's, then the text block's, modifiers unioning across the three. The
        widget style is not painted as a rect fill — every line the TUI hands
        over is already padded edge to edge, and that padding is what carries
        the row's background.
        """

        if area.width <= 0 or area.height <= 0:
            return
        text = _widget_text(widget)
        alignment = _widget_alignment(widget, text)
        skip_rows = _widget_scroll_y(widget)
        skip_cells = _widget_scroll_x(widget)
        for offset, line in enumerate(text.lines):
            if offset < skip_rows:
                continue
            row = offset - skip_rows
            if row >= area.height:
                break
            pieces = _effective_pieces(line, text.style)
            if skip_cells:
                pieces = _drop_cells(pieces, skip_cells)
            if not pieces:
                continue
            start = _alignment_offset(alignment, _pieces_width(pieces), area.width)
            self._write(_styled(pieces), area.x + start, area.y + row, area.width - start)

    def _write(self, content: Any, x: int, y: int, width: int) -> None:
        """One styled row at ``(x, y)``, clipped to ``width`` cells.

        The row's placement is REUSED when the same cell and width was painted
        before, and only its text is replaced. That is what lets the canvas
        diff: a fresh placement is a new object, so xnano repaints every one of
        its cells, while a reused one whose text compares equal emits nothing.
        Measured on a pty, recreate-every-frame cost ~4 kB per frame for a
        120x32 screen — a full repaint 60 times a second, and the input band
        visibly flickered; reuse costs ~0.5 kB.
        """

        if y < 0 or y >= self._height or x >= self._width or width <= 0:
            return
        canvas = self.canvas
        key = (x, y, width)
        self._row_marks[key] = self._frame_marks
        placement = self._rows.get(key)
        if placement is None:
            placement = canvas.write(content, x=x, y=y, width=width, height=1, edges="clip")
            self._rows[key] = placement
            self._placements.append(placement)
            return
        try:
            placement.text = content
        except Exception:
            # The placement died under us (a swapped canvas, a closed screen):
            # forget it and paint the row again from scratch.
            self._rows.pop(key, None)
            self._placements = [item for item in self._placements if item is not placement]
            self._write(content, x, y, width)

    def _remove_placements(self) -> None:
        for placement in self._placements:
            placement.remove()
        self._placements = []
        self._rows.clear()
        self._row_marks.clear()

    # ── screen-wide operations ──────────────────────────────────────────────
    def clear(self) -> None:
        """Erase everything painted so far."""

        self._remove_placements()
        if not self._real_screen:
            self.canvas.render()

    def hide_cursor(self) -> None:
        """Hide the caret for the next frame."""

        self._cursor_visible = False
        # Forgetting the cell is what makes the next `move_cursor` write again:
        # hiding is the one transition where xnano's state moved out from under
        # the memo, so a later move to the SAME cell must not be skipped.
        self._cursor_cell = None
        self._set_cursor_visible(False)

    def show_cursor(self) -> None:
        """Show the caret again."""

        self._cursor_visible = True
        # Showing after a hide has to write again, so the memo is cleared here
        # exactly as `hide_cursor` clears it.
        self._cursor_cell = None
        self._set_cursor_visible(True)

    def move_cursor(self, x: int, y: int) -> None:
        """Put the caret on cell ``(x, y)``, in the canvas's own coordinates.

        This is xnano's cursor, not an escape sequence: a canvas on the screen
        re-asserts its cursor on every frame it draws, so a raw
        ``ESC[row;colH`` written between frames is undone by the next one. The
        app hands the cell over and xnano keeps it there.

        The caret also names its SHAPE. xnano's ``shape=None`` means "keep the
        user's own shape", and a cursor with a position but no shape emits no
        ``DECSCUSR`` at all — measured on a pty, not one ``ESC[N q`` in a whole
        session of typing — so the caret was drawn in whatever the terminal was
        set to. That is what the owner read as "no cursor": the position was
        right (``?25h`` and the CUP both landed in the input band,
        probe-verified); only the shape was left to the terminal. A blinking
        bar is what a text field shows, and re-asserting it per frame is free
        because xnano emits ``DECSCUSR`` only when the value changed.

        A shape is used rather than ``cursor.follow``: follow ties the caret to
        a placement and loses it when that placement is removed, while the app
        recomputes the cell from the live layout every frame. Assigning
        ``position`` also clears whatever follow xnano had (``Cursor.position``
        sets ``_cursor_follow = None``), so our position is the one that wins.
        """

        canvas = self._canvas
        if canvas is None or getattr(canvas, "is_closed", False):
            return
        # `_cursor_cell` remembers what was last WRITTEN, not merely where the
        # caret is: assigning `position` on every tick makes xnano advance a
        # frame each time (measured on a pty: 173 frames and 5.3% of a core over
        # 3 idle seconds, against 0 frames for an untouched canvas). Writing
        # only on a change keeps the caret where it is at no cost, and the
        # first call after a blink/shape reset still lands because `None`
        # never compares equal.
        cell = (max(0, int(x)), max(0, int(y)))
        if cell == self._cursor_cell:
            self._cursor_visible = True
            return
        with _suppressed():
            cursor = canvas.screen.cursor
            cursor.position = cell
            cursor.shape = _CARET_SHAPE
            cursor.is_blinking = _CARET_BLINKING
            cursor.is_visible = True
        self._cursor_cell = cell
        self._cursor_visible = True

    def _set_cursor_visible(self, visible: bool) -> None:
        """Set the xnano cursor's visibility, tolerating a closed canvas."""

        canvas = self._canvas
        if canvas is None or getattr(canvas, "is_closed", False):
            return
        with _suppressed():
            canvas.screen.cursor.is_visible = visible

    def restore(self) -> None:
        """Release the canvas; a no-op when it was never created.

        On the real screen this is what leaves the alternative screen: closing
        the canvas makes xnano emit ``?1049l`` (and turn the input reporting
        back off) exactly once, on the same thread that opened it.
        """

        self._placements = []
        canvas, self._canvas = self._canvas, None
        self._opened = False
        if canvas is not None and not canvas.is_closed:
            with _suppressed():
                canvas.close()

    @property
    def is_open(self) -> bool:
        """Whether ``__enter__`` claimed the screen (the app's leave-owns-it flag)."""

        return self._opened

    # ── what the tests read ─────────────────────────────────────────────────
    def output_text(self) -> str:
        """The painted screen as plain text, rows joined by ``\\n``."""

        return self.canvas.screen.output.text

    def output_lines(self) -> list[str]:
        """The painted screen as one string per row."""

        return list(self.canvas.screen.output.lines())

    def output_ansi(self) -> str:
        """The painted screen with escape sequences — what a pty receives."""

        return self.canvas.screen.output.ansi

    def __str__(self) -> str:
        if self._real_screen:
            mode = "real"
        else:
            mode = "inline" if self._inline_height is not None else "headless"
        area = self.area()
        return f"Terminal({mode}, {area.width}x{area.height})"

    def __repr__(self) -> str:
        return str(self)


def _suppressed() -> Any:
    """``contextlib.suppress(Exception)``, so a dead terminal cannot raise."""

    return contextlib.suppress(Exception)


def _widget_text(widget: Any) -> Text:
    """The ``Text`` block a paintable widget carries."""

    if isinstance(widget, Paragraph):
        return widget.text
    if isinstance(widget, Text):
        return widget
    raise TypeError(f"render_widget cannot paint {type(widget).__name__}")


def _widget_alignment(widget: Any, text: Text) -> str:
    """The paragraph's alignment when it has one, else the text block's."""

    if isinstance(widget, Paragraph):
        return widget.alignment_or_text
    return text.alignment


def _widget_scroll_y(widget: Any) -> int:
    return widget.scroll_y if isinstance(widget, Paragraph) else 0


def _widget_scroll_x(widget: Any) -> int:
    return widget.scroll_x if isinstance(widget, Paragraph) else 0


def _effective_pieces(line: Line, text_style: Style | None) -> list[tuple[str, Style]]:
    """A line's runs with the line and text styles patched UNDER each span's."""

    pieces: list[tuple[str, Style]] = []
    for span in line.spans:
        if not span.content:
            continue
        style = span.style or Style()
        if line.style is not None:
            style = line.style.patch(style)
        if text_style is not None:
            style = text_style.patch(style)
        pieces.append((span.content, style))
    return pieces


def _pieces_width(pieces: Iterable[tuple[str, Style]]) -> int:
    """The cells a run list occupies."""

    return sum(cell_len(content) for content, _style in pieces)


def _drop_cells(pieces: list[tuple[str, Style]], cells: int) -> list[tuple[str, Style]]:
    """The run list without its first ``cells`` columns."""

    out: list[tuple[str, Style]] = []
    remaining = int(cells)
    for content, style in pieces:
        if remaining <= 0:
            out.append((content, style))
            continue
        width = cell_len(content)
        if width <= remaining:
            remaining -= width
            continue
        head = ""
        for char in content:
            if remaining > 0:
                remaining -= cell_len(char)
                continue
            head += char
        if head:
            out.append((head, style))
    return out


def _alignment_offset(alignment: str, width: int, area_width: int) -> int:
    """The column that aligns ``width`` cells inside ``area_width``.

    pyratatui centers with ``area_width // 2 - width // 2`` (not
    ``(area_width - width) // 2``): the two differ by one cell when both are odd,
    and a probe of the real terminal shows it uses the halved form.
    """

    if alignment == "center":
        return max(0, area_width // 2 - width // 2)
    if alignment == "right":
        return max(0, area_width - width)
    return 0


def _styled(pieces: Sequence[tuple[str, Style]]) -> xstyles.StyledText:
    """Styled runs as the value xnano writes."""

    return xstyles.StyledText(
        [xstyles.TextSpan(content, style=style.to_xnano()) for content, style in pieces]
    )


# ─── the single-line editor ─────────────────────────────────────────────────


class PromptStatus(enum.Enum):
    """The three states of a :class:`TextState` (pyratatui's ``PromptStatus``)."""

    Pending = "pending"
    Complete = "complete"
    Aborted = "aborted"

    def __str__(self) -> str:
        return f"PromptStatus.{self.name}"

    __repr__ = __str__


class TextState:
    """A single-line readline editor: the buffer, the caret and its edit keys.

    ``TextInputModal`` and the command palette drive one of these with the app's
    duck-typed :class:`~.input.Key`, and the modal tests are its specification:
    type, move, Ctrl+K, Enter completes, Esc/Ctrl+C aborts, and after either the
    editor consumes nothing until ``reset_status``.

    It is our own rather than ``xnano_core.TextEditor``: that editor's
    ``handle_key`` takes an ``xnano_core.KeyEvent``, which cannot be instantiated
    from Python, so a duck-typed key could never reach it. The behaviour below
    was read off pyratatui's ``TextState`` one chord at a time.

    Named keys come first, so ``Ctrl+Left`` still moves one cell and
    ``Ctrl+Backspace`` still deletes; only the ctrl CHORDS are modifier-specific.
    """

    __slots__ = ("_cursor", "_focused", "_status", "_value")

    def __init__(self, initial: str = "") -> None:
        self._value = str(initial)
        self._cursor = len(self._value)
        self._status = PromptStatus.Pending
        self._focused = False

    # ── focus ───────────────────────────────────────────────────────────────
    def focus(self) -> None:
        """Mark the editor focused (the caret shows)."""

        self._focused = True

    def blur(self) -> None:
        """Mark the editor unfocused."""

        self._focused = False

    @property
    def is_focused(self) -> bool:
        """Whether :meth:`focus` was the last of the pair called."""

        return self._focused

    # ── buffer ──────────────────────────────────────────────────────────────
    def value(self) -> str:
        """The current buffer."""

        return self._value

    def set_value(self, value: str) -> None:
        """Replace the buffer, caret at its end; the status is untouched."""

        self._value = str(value)
        self._cursor = len(self._value)

    @property
    def cursor_pos(self) -> int:
        """The caret's offset, in characters."""

        return self._cursor

    def clear_input(self) -> None:
        """Empty the buffer, keeping the status and the focus."""

        self._value = ""
        self._cursor = 0

    # ── status ──────────────────────────────────────────────────────────────
    @property
    def status(self) -> PromptStatus:
        """Where the line stands: pending, complete or aborted."""

        return self._status

    def is_pending(self) -> bool:
        return self._status is PromptStatus.Pending

    def is_complete(self) -> bool:
        return self._status is PromptStatus.Complete

    def is_aborted(self) -> bool:
        return self._status is PromptStatus.Aborted

    def reset_status(self) -> None:
        """Back to ``Pending``; the buffer and the caret stay where they are."""

        self._status = PromptStatus.Pending

    def reset(self) -> None:
        """Empty the buffer and go back to ``Pending``."""

        self._value = ""
        self._cursor = 0
        self._status = PromptStatus.Pending

    # ── input ───────────────────────────────────────────────────────────────
    def handle_key(self, key: Any) -> bool:
        """Edit by one key; returns whether the editor consumed it.

        A completed or aborted editor consumes nothing — the modal has already
        dismissed, so the next keystroke belongs to the screen underneath.
        """

        if self._status is not PromptStatus.Pending:
            return False
        code = str(getattr(key, "code", ""))
        if code == "Enter":
            self._status = PromptStatus.Complete
            return True
        if code == "Esc":
            self._status = PromptStatus.Aborted
            return True
        edit = _NAMED_EDITS.get(code)
        if edit is not None:
            edit(self)
            return True
        if bool(getattr(key, "ctrl", False)):
            return self._handle_ctrl(code)
        if len(code) == 1 and code.isprintable() and not bool(getattr(key, "alt", False)):
            self._insert(code)
            return True
        return False

    def _handle_ctrl(self, code: str) -> bool:
        """The readline chords; anything else is not ours (and not consumed)."""

        if code == "c":
            self._status = PromptStatus.Aborted
            return True
        edit = _CTRL_EDITS.get(code)
        if edit is None:
            return False
        edit(self)
        return True

    # ── the edit primitives ─────────────────────────────────────────────────
    def _insert(self, text: str) -> None:
        self._value = self._value[: self._cursor] + text + self._value[self._cursor :]
        self._cursor += len(text)

    def _left(self) -> None:
        self._cursor = max(0, self._cursor - 1)

    def _right(self) -> None:
        self._cursor = min(len(self._value), self._cursor + 1)

    def _home(self) -> None:
        self._cursor = 0

    def _end(self) -> None:
        self._cursor = len(self._value)

    def _backspace(self) -> None:
        if self._cursor > 0:
            self._value = self._value[: self._cursor - 1] + self._value[self._cursor :]
            self._cursor -= 1

    def _delete(self) -> None:
        if self._cursor < len(self._value):
            self._value = self._value[: self._cursor] + self._value[self._cursor + 1 :]

    def _kill_to_end(self) -> None:
        self._value = self._value[: self._cursor]

    def _kill_all(self) -> None:
        """Ctrl+U: pyratatui empties the WHOLE buffer, not just the left half."""

        self._value = ""
        self._cursor = 0

    def _kill_word(self) -> None:
        """Ctrl+W: drop the spaces, then the word before the caret."""

        start = self._cursor
        while start > 0 and self._value[start - 1].isspace():
            start -= 1
        while start > 0 and not self._value[start - 1].isspace():
            start -= 1
        self._value = self._value[:start] + self._value[self._cursor :]
        self._cursor = start

    def __str__(self) -> str:
        return f"TextState(value={self._value!r}, cursor={self._cursor}, status={self._status})"

    def __repr__(self) -> str:
        return str(self)


_NAMED_EDITS: dict[str, Callable[[TextState], None]] = {
    "Left": lambda state: state._left(),
    "Right": lambda state: state._right(),
    "Home": lambda state: state._home(),
    "End": lambda state: state._end(),
    "Backspace": lambda state: state._backspace(),
    "Delete": lambda state: state._delete(),
}
"""Named keys that edit. Any OTHER named key (Tab, F1, Up …) is not consumed."""

_CTRL_EDITS: dict[str, Callable[[TextState], None]] = {
    "a": lambda state: state._home(),
    "e": lambda state: state._end(),
    "b": lambda state: state._left(),
    "f": lambda state: state._right(),
    "h": lambda state: state._backspace(),
    "u": lambda state: state._kill_all(),
    "k": lambda state: state._kill_to_end(),
    "w": lambda state: state._kill_word(),
}
"""Ctrl chords, spelled exactly as pyratatui spells them (lowercase only)."""
