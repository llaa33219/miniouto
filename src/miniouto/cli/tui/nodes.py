"""The ONE node type of the TUI, plus the Textual-shaped test harness.

WHY this module exists twice over:

* **One node type.** ``rows.py``, ``widgets.py`` and ``modals.py`` are plain
  state objects painted by an immediate-mode renderer — there is no widget tree
  to mount into — but each grew a LOCAL base with an id, a region, a parent and
  children, because each layer had to be readable on its own. Three bases would
  mean ``isinstance`` never works across layers and a query could not walk a
  mixed tree (a modal's ``SettingRow`` lives inside a modal, a chip inside the
  panel). So the node lives HERE and every layer imports it: ``rows.Node`` and
  ``modals.Node`` ARE this class.
* **The ported tests are the specification.** ~300 behavioural tests were
  written against Textual's surface — ``app.query_one("#id")``,
  ``pilot.click(row, offset=(2, 0))``, ``app.screen.selections``,
  ``app.screen._compositor.render_strips()[y].text``, ``Selection.get_span``,
  ``row._render_cache.lines``. Keeping that surface is what lets those
  assertions port with minimal edits, so the harness is part of the framework:
  :class:`Compositor` (the cell grid the app paints), :class:`Pilot` (keys and
  mouse) and :func:`run_test` (a headless boot) are used by the real app.

Deliberate divergences from Textual, all of them narrowing:

* Nodes are not widgets: no CSS, no per-node message pump, no layout engine.
  :attr:`Node.region` is ASSIGNED by the renderer that paints the node, which
  is why a test can read it and why ``place()``/``translate()`` exist.
* A mouse report carries CELL coordinates and a click ``chain``; the row
  handlers were written against exactly those fields.
* ``set_class`` accepts both argument orders. Textual's is ``(add, name)``, the
  row layer's first draft was ``(name, present)``; both spellings are in the
  landed code, so the boolean is detected rather than assumed.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from rich.cells import cell_len
from rich.style import Style as RichStyle
from rich.text import Text

from . import pt
from .theme import MINIOUTO_THEME, TuiTheme

__all__ = [
    "SELECT_ALL",
    "Cell",
    "Compositor",
    "Node",
    "Offset",
    "Pilot",
    "Rect",
    "Screen",
    "Segment",
    "Selection",
    "Size",
    "Strip",
    "TestContext",
    "compile_key",
    "package_seam",
    "pt_style_text",
    "run_test",
    "strip_lines",
]

DEFAULT_WIDTH = 80
"""Width assumed before a node was ever laid out."""


# ─── geometry ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rect:
    """A paintable rectangle in cell coordinates.

    Frozen because a region is DERIVED, never mutated in place: a re-layout
    replaces it, which makes "the region the last frame used" impossible to
    confuse with "the region this frame used".
    """

    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom


class Offset:
    """A cell offset — Textual's ``Offset``, minus the arithmetic.

    Iterable and comparable with a plain ``(x, y)`` pair on purpose: the row
    handlers were written against Textual's object and the ported tests pass
    tuples, so both spellings work in ``.offset``.
    """

    __slots__ = ("x", "y")

    def __init__(self, x: int = 0, y: int = 0) -> None:
        self.x = int(x)
        self.y = int(y)

    def __iter__(self) -> Iterator[int]:
        yield self.x
        yield self.y

    def __getitem__(self, index: int) -> int:
        return (self.x, self.y)[index]

    def __len__(self) -> int:
        return 2

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Offset):
            return (self.x, self.y) == (other.x, other.y)
        if isinstance(other, (tuple, list)) and len(other) == 2:
            return (self.x, self.y) == (int(other[0]), int(other[1]))
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.x, self.y))

    def __repr__(self) -> str:
        return f"Offset({self.x}, {self.y})"

    @property
    def transpose(self) -> tuple[int, int]:
        """``(y, x)`` — Textual's reading order when sorting offsets."""

        return (self.y, self.x)


class Size:
    """``(width, height)`` in cells — Textual's ``Size``, minimal."""

    __slots__ = ("height", "width")

    def __init__(self, width: int = 0, height: int = 0) -> None:
        self.width = int(width)
        self.height = int(height)

    def __bool__(self) -> bool:
        return bool(self.width and self.height)

    def __iter__(self) -> Iterator[int]:
        yield self.width
        yield self.height

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Size):
            return (self.width, self.height) == (other.width, other.height)
        if isinstance(other, (tuple, list)) and len(other) == 2:
            return (self.width, self.height) == (int(other[0]), int(other[1]))
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.width, self.height))

    def __repr__(self) -> str:
        return f"Size({self.width}, {self.height})"


# ─── selection ───────────────────────────────────────────────────────────────


class Selection:
    """A selected range of text, in CHARACTER offsets per line.

    Ported from ``textual.selection.Selection`` because the row layer paints
    with exactly these semantics: ``get_span(y)`` answers ``(start, end)`` with
    ``end == -1`` meaning "to the end of the text", and the end is EXCLUSIVE —
    a drag from character 1 to 11 selects ten characters, which is what the
    user sees highlighted.

    Offsets are CONTENT-relative: they count from the first painted character
    of the node's own text, after the row's border/padding gutter. That is the
    coordinate space a Textual widget reported and the one every ported drag
    assertion is written in; the row shifts by its gutter when it paints.
    """

    __slots__ = ("end", "start")

    def __init__(self, start: Offset | None = None, end: Offset | None = None) -> None:
        self.start = start
        self.end = end

    @classmethod
    def from_offsets(cls, offset1: Offset, offset2: Offset) -> Selection:
        """The selection between two offsets, in reading order."""

        offsets = sorted([offset1, offset2], key=lambda offset: offset.transpose)
        return cls(*offsets)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Selection):
            return (self.start, self.end) == (other.start, other.end)
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.start, self.end))

    def __repr__(self) -> str:
        return f"Selection({self.start!r}, {self.end!r})"

    def extract(self, text: str) -> str:
        """The selected slice of ``text`` (``\\n``-joined across lines)."""

        lines = text.splitlines()
        if not lines:
            return ""
        if self.start is None:
            start_line_index, start_offset = 0, 0
        else:
            start_line_index, start_offset = self.start.transpose
        if self.end is None:
            end_line = len(lines)
            end_offset = len(lines[-1])
        else:
            end_line, end_offset = self.end.transpose
        end_line = min(len(lines), end_line)
        if start_line_index == end_line:
            return lines[start_line_index][start_offset:end_offset]
        selection: list[str] = []
        selected_lines = lines[start_line_index : end_line + 1]
        if len(selected_lines) >= 2:
            first_line, *mid_lines, last_line = selected_lines
            selection.append(first_line[start_offset:])
            selection.extend(mid_lines)
            selection.append(last_line[:end_offset])
        else:  # pragma: no cover - defensive
            with contextlib.suppress(IndexError):
                selection.append(lines[start_line_index][start_offset:end_offset])
        return "\n".join(selection)

    def get_span(self, y: int) -> tuple[int, int] | None:
        """The ``(start, end)`` span this selection covers on line ``y``."""

        start, end = self.start, self.end
        if start is None and end is None:
            return 0, -1
        if start is not None and end is not None:
            if y < start.y or y > end.y:
                return None
            if y == start.y and start.y == end.y:
                return start.x, end.x
            if y == end.y:
                return 0, end.x
            if y == start.y:
                return start.x, -1
            return 0, -1
        if start is None and end is not None:
            if y == end.y:
                return 0, end.x
            if y > end.y:
                return None
            return 0, -1
        if end is None and start is not None:
            if y == start.y:
                return start.x, -1
            if y > start.y:
                return 0, -1
            return None
        return 0, -1

    def shifted(self, cells: int) -> Selection:
        """This selection moved right by ``cells`` (a row's gutter)."""

        if cells <= 0:
            return self

        def move(offset: Offset | None) -> Offset | None:
            return None if offset is None else Offset(offset.x + cells, offset.y)

        return Selection(move(self.start), move(self.end))


SELECT_ALL = Selection(None, None)
"""The whole node — what a triple click would grab if a row allowed it."""


# ─── the node ────────────────────────────────────────────────────────────────


def _matches(node: Any, selector: Any) -> bool:
    """Does ``node`` match ``selector``?

    Accepts the four spellings the layers and the ported tests use: a TYPE
    (``query(SettingRow)``), ``#id``, ``.class``, and a bare class name
    (``query("SettingRow", SettingRow)``).
    """

    if isinstance(selector, type):
        return isinstance(node, selector)
    if not isinstance(selector, str):
        return False
    if selector.startswith("#"):
        return node.id == selector[1:]
    classes = node.classes
    if selector.startswith("."):
        return selector[1:] in classes
    return type(node).__name__ == selector or selector in classes


class Node:
    """A paintable, addressable node: id, region, classes, children, handlers.

    Everything a row, a widget or a modal part needs to be painted, hit-tested,
    queried and focused — and nothing else. The paint entry point is
    :meth:`build_lines` (pure, no I/O); the test entry point is :meth:`render`
    (Rich ``Text``, so ``.plain`` reads what the ported tests read).
    """

    id: str | None = None
    msg_ref: str | None = None
    row_id: int = 0
    display: bool = True
    focused: bool = False
    hovered: bool = False
    image: Any | None = None
    can_focus: ClassVar[bool] = False
    allow_select: ClassVar[bool] = True

    def __init__(self, id: str | None = None, classes: Iterable[str] = ()) -> None:
        self.id = id
        self.region = Rect()
        self.display = True
        self.focused = False
        self.hovered = False
        self.row_id = 0
        self.msg_ref = None
        self.image = None
        self.size = Size()
        self.parent: Node | None = None
        self.children: list[Node] = []
        self.selections: dict[Any, Selection] = {}
        """This screen's selections, keyed by node (``screen.selections``)."""
        self._classes: set[str] = set(classes)
        self._compositor_cache: Compositor | None = None

    # ── classes ───────────────────────────────────────────────────────────
    @property
    def classes(self) -> set[str]:
        """The LIVE class set — the modal layer mutates it directly."""

        return self._classes

    def has_class(self, name: str) -> bool:
        return name in self._classes

    def add_class(self, name: str) -> None:
        self._classes.add(name)

    def remove_class(self, name: str) -> None:
        self._classes.discard(name)

    def set_class(self, *args: Any) -> None:
        """Add or remove a class, accepting BOTH argument orders.

        Textual spells it ``set_class(add, name)``; the row layer's first draft
        spelled it ``set_class(name, present)``. Every call site is in landed
        code, so the boolean is detected rather than assumed.
        """

        present: bool | None = None
        name: str | None = None
        for argument in args:
            if isinstance(argument, bool):
                present = argument
            elif isinstance(argument, str):
                name = argument
        if name is None or present is None:  # pragma: no cover - defensive
            return
        if present:
            self._classes.add(name)
        else:
            self._classes.discard(name)

    # ── placement ─────────────────────────────────────────────────────────
    def place(self, x: int, y: int, width: int = 0, height: int = 0) -> Rect:
        """Record where this node is painted; regions are ABSOLUTE screen cells."""

        self.region = Rect(
            int(x),
            int(y),
            int(width) or self.region.width,
            int(height) or self.region.height,
        )
        return self.region

    def set_region(self, region: Rect) -> Rect:
        self.region = region
        return region

    def translate(self, dx: int, dy: int) -> None:
        """Shift this node AND its subtree by ``(dx, dy)`` cells."""

        for node in self.walk():
            region = node.region
            node.region = Rect(region.x + dx, region.y + dy, region.width, region.height)

    # ── tree ──────────────────────────────────────────────────────────────
    def focus(self, value: bool = True) -> Node:
        """Mark this node focused; the APP owns which node actually holds it."""

        self.focused = value
        return self

    def blur(self) -> None:
        self.focused = False

    def add(self, node: Node) -> Node:
        """Append ``node`` as a child and return it (mounting, minus the pump)."""

        node.parent = self
        self.children.append(node)
        return node

    def mount(self, *nodes: Node) -> None:
        for node in nodes:
            self.add(node)

    def remove(self) -> None:
        """Detach from the parent (a no-op for a node that was never mounted).

        Only the plain ``children`` list is spliced here — a log row lives in
        ``ChatScroll._mounted``, so detaching one goes through
        ``ChatScroll.remove(row)`` instead (see ``_RowWindow.remove``).
        """

        parent = self.parent
        children = getattr(parent, "children", None)
        if children is not None and self in children:
            children.remove(self)
        self.parent = None

    def walk(self) -> Iterator[Node]:
        """This node and every descendant, in tree order."""

        yield self
        for child in list(self.children):
            yield from child.walk()

    def _walk(self) -> list[Node]:
        out: list[Node] = []
        for child in self.children:
            out.append(child)
            out.extend(child._walk())
        return out

    # ── queries ───────────────────────────────────────────────────────────
    def query(self, selector: Any) -> list[Node]:
        """Every node (self included) matching ``selector``, in tree order."""

        return [node for node in self.walk() if _matches(node, selector)]

    def query_one(self, selector: Any, expect: type | None = None) -> Any:
        """The first match; ``expect`` enforces the type the old tests named."""

        found = self.query(selector)
        if not found:
            raise LookupError(f"no node matches {selector!r}")
        node = found[0]
        if expect is not None and not isinstance(node, expect):
            raise LookupError(
                f"{selector!r} matched {type(node).__name__}, not {expect.__name__}"
            )
        return node

    # ── painting ──────────────────────────────────────────────────────────
    def build_lines(self, width: int, theme: TuiTheme) -> list[list[tuple[str, pt.Style]]]:
        """The node's painted content: one entry per visual line."""

        return [[]]

    def height(self, width: int, theme: TuiTheme) -> int:
        return len(self.build_lines(width, theme))

    def render(self) -> Text:
        """The node's content as Rich ``Text`` (tests read ``.plain`` off it)."""

        return Text()

    # ── the ported-API surface ────────────────────────────────────────────
    @property
    def is_attached(self) -> bool:
        """Is this node mounted anywhere (a Textual ``is_attached`` stand-in)?"""

        return self.parent is not None

    def refresh(self, *_: Any, **__: Any) -> None:
        """Drop derived caches — a no-op for a node that has none."""

        refresh = getattr(self, "_refresh", None)
        if callable(refresh) and refresh is not self.refresh:
            refresh()

    def call_after_refresh(self, callback: Any, *args: Any) -> None:
        """Run ``callback`` "after the next reflow" — here, immediately.

        There is no reflow to wait for: the frame measures every node itself,
        so a deferred call would only make the state a test reads depend on
        whether a frame happened to be painted in between.
        """

        callback(*args)

    def _cache_width(self) -> int:
        return int(getattr(self, "_build_width", 0) or DEFAULT_WIDTH)

    def _cache_theme(self) -> TuiTheme:
        return getattr(self, "_theme", MINIOUTO_THEME) or MINIOUTO_THEME

    def _cache_source_lines(self) -> list[list[tuple[str, pt.Style]]]:
        """The lines ``_render_cache`` exposes — a row overrides this to drop its gutter."""

        return self.build_lines(self._cache_width(), self._cache_theme())

    @property
    def _render_cache(self) -> _RenderCache:
        """The node's painted lines as Strips (``row._render_cache.lines``)."""

        return _RenderCache(self._cache_source_lines())

    @property
    def content_size(self) -> Size:
        return Size(self.region.width, self.region.height)

    @property
    def outer_size(self) -> Size:
        return Size(self.region.width, self.region.height)

    @property
    def content_region(self) -> Rect:
        return self.region

    # ── events ────────────────────────────────────────────────────────────
    def handle_key(self, key: Any, app: Any = None) -> bool:
        """Return True when the key was consumed."""

        return False

    def handle_mouse(
        self, ev: Any, x: int = 0, y: int = 0, app: Any = None, *, chain: int | None = None
    ) -> bool:
        """Return True when the mouse event was consumed (``x``/``y`` optional).

        ``chain`` is the click chain (2 = the second click of a double-click), which
        the ROW needs and a plain node ignores — it is keyword-only so every
        existing three-argument call site still matches.
        """
        """Called when a screen pushed above this one pops."""

    def tick(self, _dt: float) -> None:
        """Advance any animation (a blinking cursor)."""

    # ── the screen surface ────────────────────────────────────────────────
    @property
    def _compositor(self) -> Compositor:
        """The cell grid this screen was last painted into."""

        if self._compositor_cache is None:
            self._compositor_cache = getattr(self, "compositor", None) or Compositor()
        return self._compositor_cache

    def get_selected_text(self) -> str:
        """Everything currently selected, in paint order."""

        parts: list[str] = []
        for node, selection in list(self.selections.items()):
            getter = getattr(node, "get_selection", None)
            if getter is None:
                continue
            picked = getter(selection)
            if picked and picked[0]:
                parts.append(picked[0])
        return "\n".join(parts)

    def clear_selection(self) -> None:
        """Drop every selection and un-mark the nodes that carried one."""

        for node in list(self.selections):
            if getattr(node, "selection", None) is not None:
                node.selection = None
        self.selections.clear()


class _RenderCache:
    """``.lines`` of painted Strips — what the ported row assertions read."""

    __slots__ = ("lines",)

    def __init__(self, lines: Sequence[Sequence[tuple[str, pt.Style]]]) -> None:
        self.lines = [
            Strip(
                [Segment(text, rich_from_pt(style)) for text, style in line if text],
                sum(cell_len(text) for text, _ in line),
            )
            for line in lines
        ]


class Screen(Node):
    """The base screen: the app's own tree, one compositor, one selection set."""

    def __init__(self, id: str | None = None, app: Any = None) -> None:
        super().__init__(id=id)
        self.app = app
        self.index = 0

    def _on_log_scroll(self, scroll_y: float) -> None:
        """The log's scroll hook: the app routes it (top-load, jump button)."""

        hook = getattr(self.app, "_on_log_scroll", None)
        if hook is not None:
            hook(scroll_y)


# ─── the painted-grid compat surface ─────────────────────────────────────────


@dataclass(frozen=True)
class Cell:
    """One painted cell: a character (``""`` for a wide char's second half)."""

    char: str
    style: pt.Style


def rich_from_pt(style: pt.Style | None) -> RichStyle:
    """A ``pyratatui.Style`` as a Rich ``Style`` (the ``.render()`` surface).

    Rich is the only vocabulary that names a color as ``#rrggbb``, which is
    what ``.plain``/``segment.style.color.name`` assertions read. A pyratatui
    style exposes no getters for its modifiers, so they are read off its
    ``str()`` form — the technique ``modals`` already uses.
    """

    rich = RichStyle()
    if style is None:
        return rich
    text = str(style)
    for prefix, keyword in (("fg=Some(Rgb(", "color"), ("bg=Some(Rgb(", "bgcolor")):
        if prefix not in text:
            continue
        body = text.split(prefix, 1)[1].split(")", 1)[0]
        try:
            red, green, blue = (int(part.strip()) for part in body.split(","))
        except ValueError:  # pragma: no cover - indexed / reset forms
            continue
        rich += RichStyle(**{keyword: f"#{red:02x}{green:02x}{blue:02x}"})
    for token, attribute in (
        ("BOLD", "bold"),
        ("DIM", "dim"),
        ("ITALIC", "italic"),
        ("UNDERLINED", "underline"),
        ("REVERSED", "reverse"),
        ("CROSSED_OUT", "strike"),
    ):
        if token in text:
            rich += RichStyle(**{attribute: True})
    return rich


class Segment:
    """One styled run of a painted line — Rich's ``Segment``, minimal."""

    __slots__ = ("style", "text")

    def __init__(self, text: str, style: RichStyle | None = None) -> None:
        self.text = text
        self.style = style

    def __repr__(self) -> str:
        return f"Segment({self.text!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Segment):
            return (self.text, str(self.style)) == (other.text, str(other.style))
        return NotImplemented


class Strip:
    """One painted line — Textual's ``Strip``, read as ``.text``/``._segments``.

    Iterable on purpose: a ported assertion reaches a line's own segments with
    ``next(iter(row._render_cache.lines[0]))`` or ``list(...)[1]`` — the way it
    did against Textual's ``Strip(_segments)``, which IS the segment sequence.
    ``len()`` follows Textual too: the segment COUNT, not the cell width (which
    ``cell_length`` carries, as it did there).
    """

    __slots__ = ("_segments", "cell_length")

    def __init__(self, segments: Sequence[Segment], cell_length: int) -> None:
        self._segments = list(segments)
        self.cell_length = cell_length

    @property
    def text(self) -> str:
        return "".join(segment.text for segment in self._segments)

    def __iter__(self) -> Iterator[Segment]:
        return iter(self._segments)

    def __len__(self) -> int:
        return len(self._segments)

    def __repr__(self) -> str:
        return f"Strip({self.text!r})"


class Compositor:
    """The cell grid a frame was painted into — the app's own "terminal".

    The app composes ONE list of styled lines per frame and paints it here
    before it ever touches a ``Terminal`` (a headless boot never has one). That
    single source of truth is why a ``render_strips()[y].text`` assertion is an
    assertion about the DISPLAY: it is the exact text ratatui is handed, after
    the crop to the screen edge, the centring of the 150-cell column and the
    overlay of a modal on top.
    """

    def __init__(self) -> None:
        self.width = 0
        self.height = 0
        self._cells: list[list[Cell]] = []

    def resize(self, width: int, height: int) -> None:
        width, height = max(0, int(width)), max(0, int(height))
        if width == self.width and height == self.height and self._cells:
            return
        self.width, self.height = width, height
        blank = Cell(" ", pt.Style())
        self._cells = [[blank for _ in range(width)] for _ in range(height)]

    def clear(self) -> None:
        blank = Cell(" ", pt.Style())
        self._cells = [[blank for _ in range(self.width)] for _ in range(self.height)]

    def paint(
        self,
        lines: Sequence[Sequence[tuple[str, pt.Style]]],
        x: int = 0,
        y: int = 0,
    ) -> None:
        """Blit styled lines at ``(x, y)``, clipping to the grid."""

        for index, line in enumerate(lines):
            row = y + index
            if not 0 <= row < self.height:
                continue
            cells = self._cells[row]
            column = x
            for text, style in line:
                for char in text:
                    if column >= self.width:
                        break
                    if column >= 0:
                        cells[column] = Cell(char, style)
                    if cell_len(char) > 1:
                        if 0 <= column + 1 < self.width:
                            cells[column + 1] = Cell("", style)
                        column += 1
                    column += 1

    @property
    def cells(self) -> list[list[Cell]]:
        return self._cells

    def render_strips(self) -> list[Strip]:
        """One :class:`Strip` per painted row (merged into styled runs)."""

        strips: list[Strip] = []
        for row in self._cells:
            segments: list[Segment] = []
            for cell in row:
                if cell.char == "":
                    continue
                style = rich_from_pt(cell.style)
                if segments and str(segments[-1].style) == str(style):
                    segments[-1].text += cell.char
                else:
                    segments.append(Segment(cell.char, style))
            strips.append(Strip(segments, len(row)))
        return strips


def strip_lines(lines: Sequence[Sequence[tuple[str, pt.Style]]]) -> Text:
    """Styled lines as one Rich ``Text`` (what a region's Paragraph paints)."""

    text = Text()
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        for chunk, style in line:
            if not chunk:
                continue
            text.append(chunk, rich_from_pt(style))
    return text


def pt_style_text(lines: Sequence[Sequence[tuple[str, pt.Style]]]) -> pt.Text:
    """Styled lines as a ``pyratatui.Text`` of pre-wrapped ``Line``s.

    ``Span`` collapses a tab and drops a newline, so both are expanded/split on
    the way in: a line here is exactly one painted row, never two.
    """

    out: list[pt.Line] = []
    for line in lines:
        spans = [
            pt.Span(
                chunk.replace("\t", " " * 4).replace("\n", " "),
                style,
            )
            for chunk, style in line
            if chunk
        ]
        out.append(pt.Line(spans))
    return pt.Text(out or [pt.Line([])])


# ─── the pilot ───────────────────────────────────────────────────────────────

_KEY_CODES: dict[str, str] = {
    "enter": "Enter",
    "return": "Enter",
    "escape": "Esc",
    "esc": "Esc",
    "tab": "Tab",
    "backtab": "BackTab",
    "shift+tab": "BackTab",
    "space": " ",
    "backspace": "Backspace",
    "delete": "Delete",
    "insert": "Insert",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "pagedown": "PageDown",
}
"""Textual key NAMES → the codes ``input.Key`` speaks (PascalCase for named keys)."""


def compile_key(spec: str) -> Any:
    """Turn a Textual key name (``"ctrl+l"``, ``"pageup"``, ``"a"``) into a ``Key``."""

    from .input import Key

    parts = [part for part in spec.strip().split("+") if part != ""]
    ctrl = any(part.lower() in ("ctrl", "control") for part in parts)
    alt = any(part.lower() == "alt" for part in parts)
    shift = any(part.lower() == "shift" for part in parts)
    rest = [part for part in parts if part.lower() not in ("ctrl", "control", "alt", "shift")]
    name = rest[-1] if rest else ""
    lowered = name.lower()
    if lowered in _KEY_CODES:
        code = _KEY_CODES[lowered]
        if code == "BackTab":
            shift = True
            code = "Tab"
    elif len(name) == 1:
        # A modified letter is spelled the way the terminal reports it: Ctrl-L
        # is the byte 0x0c, i.e. "l" with ctrl, never "L" with ctrl.
        code = name if not (ctrl or alt) else name.lower()
    elif len(lowered) == 2 and lowered[0] == "f" and lowered[1].isdigit():
        code = lowered.upper()
    else:
        code = name[:1].upper() + name[1:] if name else name
    return Key(code, ctrl=ctrl, alt=alt, shift=shift)


class Pilot:
    """Drives a booted app: keys, clicks, hovers and the clock.

    Every method settles the app first (drain queued messages, fire due timers,
    repaint) so a test reads the state the USER would see, not the state a
    half-processed batch left behind.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    @property
    def app(self) -> Any:
        return self._app

    # ── the clock ─────────────────────────────────────────────────────────
    async def pause(self, delay: float | None = None) -> None:
        """Let queued callbacks, worker tasks and timers run; then repaint.

        ``delay`` is a real sleep, which is what a test waiting for a
        background ``to_thread`` turn passes; without it the app simply yields
        a few times, which is enough for every in-process callback.
        """

        import asyncio

        rounds = 3 if delay is None else 1
        for _ in range(rounds):
            await asyncio.sleep(0 if delay is None else delay)
            await self._app._pump_once(0.05 if delay is None else max(delay, 1 / 60))

    # ── keys ──────────────────────────────────────────────────────────────
    async def press(self, *keys: str) -> None:
        """Press one or more keys (Textual names: ``"pageup"``, ``"ctrl+l"``)."""

        for spec in keys:
            self._app.inject_key(compile_key(spec))
            await self.pause()

    # ── mouse ─────────────────────────────────────────────────────────────
    def _resolve(self, target: Any) -> Any:
        if target is None or not isinstance(target, str):
            return target
        return self._app.query_one(target)

    async def click(
        self,
        target: Any = None,
        offset: tuple[int, int] = (0, 0),
        times: int = 1,
        button: int = 1,
    ) -> bool:
        """Click at ``target.region + offset`` (screen coords when target is None).

        Emits move + press + release per chain step. The RELEASE completes the
        click: a terminal reports no separate click event, the row handlers
        were ported to that protocol, and emitting a click as well would toggle
        a row twice per gesture.
        """

        return await self._post_mouse(self._resolve(target), offset, times=times)

    async def hover(self, target: Any = None, offset: tuple[int, int] = (0, 0)) -> bool:
        """Move the pointer over ``target.region + offset``."""

        await self.pause()
        return await self._post_mouse(self._resolve(target), offset, kinds=("move",))

    async def mouse_down(
        self, target: Any = None, offset: tuple[int, int] = (0, 0)
    ) -> bool:
        return await self._post_mouse(self._resolve(target), offset, kinds=("move", "down"))

    async def mouse_up(self, target: Any = None, offset: tuple[int, int] = (0, 0)) -> bool:
        return await self._post_mouse(self._resolve(target), offset, kinds=("move", "up"))

    async def _post_mouse(
        self,
        node: Any,
        offset: tuple[int, int],
        *,
        kinds: Sequence[str] = ("move", "down", "up"),
        times: int = 1,
    ) -> bool:
        from .input import Mouse

        origin = node.region if node is not None else Rect(0, 0, *self._app.size)
        x = origin.x + int(offset[0])
        y = origin.y + int(offset[1])
        for chain in range(1, times + 1):
            for kind in kinds:
                await self.pause()
                self._app.inject_mouse(Mouse(kind, x, y, button=0), chain=chain)
        await self.pause()
        return True


# ─── the headless boot ───────────────────────────────────────────────────────


class TestContext:
    """``async with app.run_test(size=(w, h)) as pilot`` — a headless app boot.

    Nothing here touches a terminal: ``ChatTUI`` is constructed headless, the
    context boots it (mount, theme, logo, timers) on the running loop and
    yields a :class:`Pilot`. Exiting cancels the workers and stops every timer,
    so a test cannot leak a task into the next one.
    """

    def __init__(self, app: Any, size: tuple[int, int] = (80, 24)) -> None:
        self._app = app
        self._size = (int(size[0]), int(size[1]))
        self.pilot = Pilot(app)

    async def __aenter__(self) -> Pilot:
        await self._app._boot_headless(self._size)
        return self.pilot

    async def __aexit__(self, *exc_info: object) -> bool:
        await self._app._shutdown_headless()
        return False


def run_test(app: Any, size: tuple[int, int] = (80, 24)) -> TestContext:
    """The ``run_test`` the ported suites call."""

    return TestContext(app, size)


# ─── the package seam ────────────────────────────────────────────────────────


def package_seam(name: str, default: Any, *, original: Any = None) -> Any:
    """Resolve a monkeypatchable name through the PACKAGE, else fall back.

    The suites patch seams on the module they import — ``miniouto.cli.tui`` —
    not on the module that owns the implementation (``config._get_reasoning_api``,
    ``core.chat.run_chat``, ``graphics.graphics_mode``). A plain re-export binds
    a COPY of the function object, so such a patch would never reach the caller
    and the test would silently exercise the real thing.

    Resolving through ``sys.modules['miniouto.cli.tui']`` at CALL time makes the
    patch authoritative. ``original`` is the pristine object: when the package
    still holds it, ``default`` is returned instead — which is what keeps a
    patch applied to the OWNING module (``config._get_reasoning_api``) working,
    and what stops a function from resolving to itself forever.
    """

    package = sys.modules.get("miniouto.cli.tui")
    value = getattr(package, name, None) if package is not None else None
    if value is None or value is (default if original is None else original):
        return default
    return value
