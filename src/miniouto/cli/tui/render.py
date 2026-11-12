"""Rendering helpers: word wrap, Rich → pyratatui bridging, color math.

WHY a word-wrap engine of our own: pyratatui's ``Paragraph.wrap(True)`` can
wrap, but it exposes no wrapped height, and the TUI must know a row's height
BEFORE it lays out (scroll window, jump button, mouse hit-testing). So rows are
pre-wrapped into explicit ``Line`` objects and their length IS the height.

WHY every width is computed with ``rich.cells.cell_len``: ``Span.width()`` /
``Line.width()`` / ``Text.width()`` count CHARACTERS, not terminal cells — a
``Span("한글")`` answers ``2`` while occupying four columns. Any layout math
built on those numbers silently mis-wraps every CJK line.

WHY tabs are expanded here: a tab (and a newline) INSIDE a ``Span``/``Line``
collapses to nothing when ratatui draws it, so a style run must never carry
one. Tabs become four spaces at wrap time and logical lines are split before
any ``Span`` is built.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable
from typing import NamedTuple

from rich.cells import cell_len
from rich.color import Color as RichColor
from rich.color import ColorType
from rich.console import Console
from rich.markdown import Markdown
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text

from . import pt

TAB_WIDTH = 4
"""Columns one tab expands to (matches the old TUI's ``expandtabs`` usage)."""

_ATOM_RE = re.compile(r"\s+|\S+")
_SEGMENTS_WIDTH = 10**6
"""Width for the style-only console: wrapping never happens there.

The segment stream is split into lines by :func:`segments_to_lines` instead, so
the console only has to preserve style runs, not decide line breaks.
"""


# ─── wrap engine ─────────────────────────────────────────────────────────────


class _Atom(NamedTuple):
    """One run of same-kind characters: a word, or a run of spaces."""

    is_space: bool
    parts: list[tuple[str, pt.Style]]
    cells: int


def word_wrap(text: str, width: int) -> list[str]:
    """Greedy word-wrap ``text`` to ``width`` cells, ``trim=False`` style.

    Rules, mirroring ratatui's ``Wrap { trim: false }`` plus the cases that
    matter for a chat transcript:

    * tabs expand to :data:`TAB_WIDTH` spaces;
    * every ``\\n`` starts a new visual line, and blank lines survive as ``""``;
    * a line's LEADING indentation is kept on its first visual line only —
      continuation lines start at column 0 (so an indented code block keeps its
      shape instead of drifting right);
    * a word wider than ``width`` breaks at the cell boundary; a wide (CJK /
      emoji) character is never split, it moves to the next line whole;
    * the spaces that sit exactly on a wrap point are consumed by the break.

    Trailing spaces are stripped from every produced line.
    """

    wrapped = _wrap_styled([(text, _NO_STYLE)], width)
    return ["".join(piece for piece, _ in line) for line in wrapped]


def wrap_spans(spans: list[tuple[str, pt.Style]], width: int) -> list[list[tuple[str, pt.Style]]]:
    """Wrap styled runs to ``width`` cells, carrying each style across breaks.

    Same engine as :func:`word_wrap` — one implementation, so a styled line and
    a plain line can never wrap differently. A span may be split across two
    visual lines; styles survive the split untouched.
    """

    return _wrap_styled(spans, width)


def _wrap_styled(
    pieces: list[tuple[str, pt.Style]], width: int
) -> list[list[tuple[str, pt.Style]]]:
    width = max(1, width)  # a zero/negative width must still make progress
    out: list[list[tuple[str, pt.Style]]] = []
    for logical in _split_newlines(_expand_tabs(pieces)):
        out.extend(_merge_runs(line) for line in _wrap_line(logical, width))
    return out


def _merge_runs(parts: list[tuple[str, pt.Style]]) -> list[tuple[str, pt.Style]]:
    """Join neighbouring runs that share a style into one piece.

    Wrapping cuts words and spaces apart, so without this a plain line would
    come back as dozens of one-word pieces; the app writes these straight into
    ``Span`` objects and fewer spans means less work per frame.
    """

    merged: list[tuple[str, pt.Style]] = []
    for text, style in parts:
        if merged and merged[-1][1] == style:
            merged[-1] = (merged[-1][0] + text, style)
        else:
            merged.append((text, style))
    return merged


def _expand_tabs(pieces: list[tuple[str, pt.Style]]) -> list[tuple[str, pt.Style]]:
    pad = " " * TAB_WIDTH
    return [(text.replace("\t", pad), style) for text, style in pieces]


def _split_newlines(
    pieces: list[tuple[str, pt.Style]],
) -> list[list[tuple[str, pt.Style]]]:
    """Split styled runs at ``\\n`` — a newline inside a Span renders as nothing."""

    lines: list[list[tuple[str, pt.Style]]] = [[]]
    for text, style in pieces:
        if "\n" not in text:
            if text:
                lines[-1].append((text, style))
            continue
        for index, part in enumerate(text.split("\n")):
            if index:
                lines.append([])
            if part:
                lines[-1].append((part, style))
    return lines


def _tokenise(pieces: list[tuple[str, pt.Style]]) -> list[_Atom]:
    """Split one logical line into alternating word / space atoms."""

    atoms: list[_Atom] = []
    for text, style in pieces:
        for match in _ATOM_RE.finditer(text):
            chunk = match.group(0)
            is_space = chunk.isspace()
            if atoms and atoms[-1].is_space == is_space:
                prev = atoms[-1]
                atoms[-1] = _Atom(is_space, [*prev.parts, (chunk, style)], prev.cells + cell_len(chunk))
            else:
                atoms.append(_Atom(is_space, [(chunk, style)], cell_len(chunk)))
    return atoms


def _wrap_line(pieces: list[tuple[str, pt.Style]], width: int) -> list[list[tuple[str, pt.Style]]]:
    """Wrap a single logical line (no ``\\n`` inside) to ``width`` cells."""

    atoms = _tokenise(pieces)
    lines: list[list[tuple[str, pt.Style]]] = []

    # Leading whitespace is the line's indentation: it seeds the first visual
    # line only, and never pushes content past the width.
    indent: list[tuple[str, pt.Style]] = []
    indent_cells = 0
    while atoms and atoms[0].is_space and indent_cells + atoms[0].cells <= width:
        atom = atoms.pop(0)
        indent.extend(atom.parts)
        indent_cells += atom.cells

    cur: list[tuple[str, pt.Style]] = list(indent)
    cur_cells = indent_cells
    started = False  # does the current line hold any non-space text?

    def flush() -> None:
        nonlocal cur, cur_cells, started
        lines.append(_trim_trailing_spaces(cur))
        cur, cur_cells, started = [], 0, False

    for atom in atoms:
        if atom.is_space:
            if cur_cells + atom.cells <= width:
                cur.extend(atom.parts)
                cur_cells += atom.cells
            elif started:
                flush()  # the spaces sit ON the break and are consumed by it
            continue
        if cur_cells + atom.cells <= width:
            cur.extend(atom.parts)
            cur_cells += atom.cells
            started = True
            continue

        # The word does not fit on the current line.
        if started:
            flush()
        else:
            # Only indentation so far: the indent yields to the content rather
            # than emitting a line holding nothing but blanks.
            cur, cur_cells = [], 0

        remaining = atom.parts
        while remaining:
            taken, remaining = _take_cells(remaining, width - cur_cells)
            cur.extend(taken)
            cur_cells += _cells(taken)
            started = True
            if remaining:
                flush()

    if cur or not lines:
        flush()
    return lines


def _take_cells(
    parts: list[tuple[str, pt.Style]], budget: int
) -> tuple[list[tuple[str, pt.Style]], list[tuple[str, pt.Style]]]:
    """Split off as many whole characters as fit in ``budget`` cells.

    A wide character is atomic: if it does not fit it stays in the remainder,
    except when that would make no progress at all (budget smaller than one
    wide character), where it is emitted alone and overflows — better a line
    one cell too wide than an infinite loop.
    """

    budget = max(1, budget)
    taken: list[tuple[str, pt.Style]] = []
    used = 0
    for index, (text, style) in enumerate(parts):
        if used >= budget:
            return taken, parts[index:]
        if not text:  # an empty run can never consume the budget
            continue
        stop = len(text)
        for pos, char in enumerate(text):
            char_cells = cell_len(char)
            if used + char_cells > budget:
                stop = pos
                break
            used += char_cells
        if stop == 0 and not taken:
            stop = 1  # never split a wide char, and never stall
            used += cell_len(text[0])
        if stop:
            taken.append((text[:stop], style))
        if stop < len(text):
            return taken, [(text[stop:], style), *parts[index + 1 :]]
    return taken, []


def _cells(parts: list[tuple[str, pt.Style]]) -> int:
    return sum(cell_len(text) for text, _ in parts)


def _trim_trailing_spaces(
    parts: list[tuple[str, pt.Style]],
) -> list[tuple[str, pt.Style]]:
    """Drop trailing spaces; a wrapped line never ends in a run of blanks."""

    trimmed = list(parts)
    while trimmed and not trimmed[-1][0].rstrip():
        trimmed.pop()
    if trimmed:
        text, style = trimmed[-1]
        stripped = text.rstrip()
        if stripped != text:
            trimmed[-1] = (stripped, style)
    return trimmed


# ─── Rich → pyratatui ────────────────────────────────────────────────────────

_NO_STYLE = pt.Style()

_SEGMENT_CONSOLE = Console(
    file=io.StringIO(),
    width=_SEGMENTS_WIDTH,
    force_terminal=True,
    color_system="truecolor",
    highlight=False,
    no_color=False,
)
"""Style-preserving console: renders Rich objects to a Segment stream.

``force_terminal`` + ``color_system`` matter: on a plain (non-terminal)
console Rich drops every style, and the whole point here is the style runs.
"""


def style_from_rich(rich_style: RichStyle | str | None) -> pt.Style:
    """Map a ``rich.style.Style`` onto a ``pyratatui.Style``.

    Colors are resolved to truecolor RGB — the TUI is a truecolor surface, so a
    named ANSI color is expanded to its palette triplet instead of being left
    for the terminal to reinterpret. ``reverse`` becomes
    ``Modifier.reversed()`` (ratatui swaps fg/bg at draw time), which is what
    a selection block needs.
    """

    style = pt.Style()
    if rich_style is None:
        return style
    if isinstance(rich_style, str):
        # Text.style keeps the value it was constructed with until something
        # parses it, so a str can still be sitting here.
        rich_style = RichStyle.parse(rich_style)

    foreground = _rich_color_to_pt(rich_style.color)
    if foreground is not None:
        style = style.fg(foreground)
    background = _rich_color_to_pt(rich_style.bgcolor)
    if background is not None:
        style = style.bg(background)

    modifiers = pt.Modifier()
    for flag, modifier in _MODIFIER_MAP:
        if getattr(rich_style, flag, False):
            modifiers = modifiers | modifier
    return style.add_modifier(modifiers)


_MODIFIER_MAP: tuple[tuple[str, object], ...] = (
    ("bold", pt.Modifier.bold()),
    ("dim", pt.Modifier.dim()),
    ("italic", pt.Modifier.italic()),
    ("underline", pt.Modifier.underlined()),
    ("blink", pt.Modifier.slow_blink()),
    ("reverse", pt.Modifier.reversed()),
    ("strike", pt.Modifier.crossed_out()),
    ("conceal", pt.Modifier.hidden()),
)
"""``rich.style.Style`` attribute → pyratatui modifier, in a fixed order."""


def _rich_color_to_pt(color: RichColor | None) -> pt.Color | None:
    """Resolve a Rich color to RGB, or ``None`` for "inherit the default"."""

    if color is None or color.type == ColorType.DEFAULT:
        return None
    red, green, blue = color.get_truecolor()
    return pt.Color.rgb(red, green, blue)


def segments_to_lines(segments: Iterable[Segment]) -> list[pt.Line]:
    """Split a Segment stream into ``Line`` objects at every newline.

    Control segments (cursor moves and friends, which Rich emits for some
    renderables) are dropped: ratatui has no use for them and they would
    otherwise end up as literal text.
    """

    return _segments_to_lines(segments, None)


def rich_text_to_lines(text: Text, base: pt.Style | None = None) -> list[pt.Line]:
    """Convert a Rich ``Text`` into pyratatui ``Line`` objects.

    ``base`` is patched UNDER every segment's own style, so a transcript row
    can paint its theme color while the Text's bold/italic/code runs still win.
    The Text's OWN ``style`` sits between the two: it is more specific than the
    row's color, less specific than a span's.

    That extra layer is not cosmetic — Rich's ``Text.render`` short-circuits on
    a span-less Text and yields the segments with NO style at all, so a
    ``Text("x", style="on #00ff00")`` would otherwise lose its background.
    """

    base_style = style_from_rich(text.style)
    merged = base.patch(base_style) if base is not None else base_style
    return _segments_to_lines(text.render(_SEGMENT_CONSOLE), merged)


def render_markdown(content: str, width: int, base_style: pt.Style) -> list[pt.Line]:
    """Render markdown to pre-wrapped pyratatui lines.

    Rich does the layout at exactly ``width`` columns (so its own table/list
    indentation is correct), and every resulting segment gets ``base_style``
    patched underneath: markdown rarely sets a foreground, and an unstyled
    segment must not fall back to the terminal default — it has to read as the
    theme's answer color.
    """

    console = Console(
        file=io.StringIO(),
        width=max(1, width),
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
        no_color=False,
    )
    rendered = console.render_lines(Markdown(content), pad=False)
    # render_lines drops the newline between lines; put it back so the one
    # shared splitter stays the single place where line ends are decided.
    rebuilt: list[Segment] = []
    for index, line in enumerate(rendered):
        rebuilt.extend(line)
        if index + 1 < len(rendered):
            rebuilt.append(Segment("\n"))
    return _segments_to_lines(rebuilt, base_style)


def _segments_to_lines(
    segments: Iterable[Segment], base: pt.Style | None
) -> list[pt.Line]:
    """Shared Segment → Line splitter for both the Text and Markdown paths."""

    lines: list[pt.Line] = []
    spans: list[pt.Span] = []
    for segment in segments:
        if segment.control is not None:
            continue
        text = segment.text
        if not text:
            continue
        pieces = text.split("\n")
        for index, piece in enumerate(pieces):
            if index:
                lines.append(pt.Line(spans))
                spans = []
            if not piece:
                continue
            style = style_from_rich(segment.style)
            spans.append(pt.Span(piece, base.patch(style) if base is not None else style))
    if spans or not lines:
        lines.append(pt.Line(spans))
    return lines


# ─── color math ──────────────────────────────────────────────────────────────


def blend(fg_hex: str, bg_hex: str, pct: float) -> str:
    """Composite ``fg_hex`` over ``bg_hex`` at ``pct`` opacity → ``#rrggbb``.

    Textual's percentage colors (``#2fc2ff 12%``) are an ALPHA composite, not a
    Lab lighten: the result is ``bg + (fg - bg) * pct`` per channel, truncated.
    Reproducing that exact arithmetic here keeps the ported theme values
    byte-identical to the Textual TUI's, which the port's color tests assert.
    """

    fg_r, fg_g, fg_b = hex_to_rgb(fg_hex)
    bg_r, bg_g, bg_b = hex_to_rgb(bg_hex)
    clamp = min(1.0, max(0.0, pct))
    return rgb_to_hex(
        int(bg_r + (fg_r - bg_r) * clamp),
        int(bg_g + (fg_g - bg_g) * clamp),
        int(bg_b + (fg_b - bg_b) * clamp),
    )


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Parse ``#rrggbb`` (or ``#rgb``) into an ``(r, g, b)`` tuple."""

    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        raise ValueError(f"not a hex color: {value!r}")
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def rgb_to_hex(red: int, green: int, blue: int) -> str:
    """Format an RGB triple as lowercase ``#rrggbb``, clamped to 0..255."""

    def channel(value: int) -> int:
        return min(255, max(0, int(value)))

    return f"#{channel(red):02x}{channel(green):02x}{channel(blue):02x}"
