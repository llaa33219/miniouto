"""Chat-log row tests: the collapsible gesture contract, art, and streaming.

Every assertion here is ported from the Textual-era suite (``test_tui_navigation``
and ``test_streaming``) and keeps its INTENT: the mechanics changed — a row is a
plain state object now, so ``build_lines``/``render``/``handle_*`` replace
``render_line``/posted messages — but the behaviour the user sees is the same one
those tests locked, and must stay locked:

1. **The whole row is the button.** A plain click toggles it wherever it lands —
   glyph, header text, expanded body, attached result — while a drag that took text
   never does, and a double/triple click toggles at most once.
2. **Hover is scoped to the glyph.** Only the ``▶``/``▼`` cell reverses; the header
   text beside it and the body below keep their resting style.
3. **Screen art is lazy, capped and truecolour.** A Computer row renders the
   ``.art`` it saved only while expanded, at most ``_ART_MAX_ROWS`` of it, with a
   note naming the command that shows the rest.
4. **Streamed rows update in place.** One row per message, never one per token, and
   the canonical text replaces the streamed prefix rather than doubling it.

Rows are exercised directly (no app, no terminal): they need a theme and a width,
both of which the tests pass in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.text import Text

import miniouto.cli.tui.pt as pt
from miniouto.cli.tui.render import blend, hex_to_rgb
from miniouto.cli.tui.rows import (
    _ART_MAX_ROWS,
    COLLAPSED_GLYPH,
    EXPANDED_GLYPH,
    GLYPH_CELLS,
    LEFT_MOUSE_BUTTON,
    TOOL_BADGES,
    AnswerRow,
    EventRow,
    RowMouseEvent,
    RowStatic,
    StreamRow,
    ThinkingRow,
    ToolRow,
    art_path_in_result,
    cells_of,
    pad_to,
    plain_of,
    png_path_in_result,
    truncate_to,
)
from miniouto.cli.tui.theme import MINIOUTO_THEME
from miniouto.tools import ascii_art

THEME = MINIOUTO_THEME
WIDTH = 60

ART_PATH = "/home/u/.miniouto/computer-screens/screen1-20260101-120000.art"


# ─── helpers ────────────────────────────────────────────────────────────────


def rgb(hex_value: str) -> str:
    """The ``Rgb(r, g, b)`` fragment a palette entry produces, for style asserts.

    ``pt.Style``'s repr carries the colour as ``Rgb(...)`` while ``pt.Color``'s
    carries it as ``Color(Rgb(...))``; comparing the shared fragment is what lets
    a test say "this run is painted in that palette entry" without depending on
    either repr's outer shape.
    """

    red, green, blue = hex_to_rgb(hex_value)
    return f"Rgb({red}, {green}, {blue})"


def rendered(row: RowStatic, width: int = WIDTH) -> str:
    """The row's painted plain text — one string, lines joined by ``\\n``."""

    return "\n".join(plain_of(line).rstrip() for line in row.build_lines(width, THEME))


def click(row: object, x: int = 0, y: int = 0, chain: int = 1) -> None:
    """A plain click at a row-relative cell, the way the app brokered one."""

    row.on_click(RowMouseEvent((x, y), LEFT_MOUSE_BUTTON, chain))


def move(row: object, x: int, y: int, button: int = 0) -> None:
    row.on_mouse_move(RowMouseEvent((x, y), button, 1))


class Selection:
    """The minimal selection shape a row paints from (``get_span`` + ``extract``)."""

    def __init__(self, spans: dict[int, tuple[int, int]]) -> None:
        self._spans = spans

    def get_span(self, y: int) -> tuple[int, int] | None:
        return self._spans.get(y)

    def extract(self, text: str) -> str:
        picked: list[str] = []
        for index, line in enumerate(text.split("\n")):
            span = self.get_span(index)
            if span is None:
                continue
            start, end = span
            picked.append(line[max(0, start) : len(line) if end == -1 else end])
        return "\n".join(picked)


def art_body(rows: int = 60) -> str:
    return "\n".join(
        f"\x1b[38;2;{i % 255};{(i * 3) % 255};{(i * 7) % 255}m"
        f"\x1b[48;2;10;20;30m{'▀' * 20}"
        for i in range(rows)
    )


def write_art(directory: Path, rows: int = 60) -> Path:
    path = directory / "screen1.art"
    meta = {
        "screen": "screen1",
        "captured": "2026-01-01T12:00:00",
        "source": "screen1",
        "grid": f"20x{rows}",
        "cell": "8x8",
        "mode": ascii_art.MODE_COLOR,
    }
    path.write_text(ascii_art.build_art(meta, art_body(rows)), encoding="utf-8")
    return path


def computer_result(path: Path | str) -> str:
    """The exact shape ``tools/computer.py`` returns for a ``screen`` action."""

    return (
        "Screen art captured (screen1 -> 20x60 chars, halfblock truecolor).\n"
        f"Saved to: {path}\n"
        "Click coordinates still come from the screenshot action.\n"
        "Full art: read that file with `cat`.\n"
        + "\n".join(art_body().split("\n")[:24])
    )


def screenshot_result(path: Path | str) -> str:
    """A ``screenshot`` summary: same 'Saved to:' line, no inline preview."""

    return (
        "Screenshot of the virtual display (1280x720 pixels). "
        "Click coordinates for the next action refer to this image.\n"
        f"Saved to: {path}"
    )


# ─── collapsed headers ──────────────────────────────────────────────────────


def test_a_collapsed_header_carries_only_the_toggle_glyph() -> None:
    # Given: a collapsed row of each kind, one of them failed
    tool = ToolRow("Bash ls -la")
    thinking = ThinkingRow("reasoning")
    failed = ToolRow("Bash rm -rf /")
    failed.set_result("error: nope", is_error=True)
    # When: they render their header
    # Then: the glyph IS the whole affordance — no key hint is printed
    assert tool.render().plain == f"{COLLAPSED_GLYPH} Bash ls -la"
    assert thinking.render().plain == f"{COLLAPSED_GLYPH} thinking"
    assert failed.render().plain == f"{COLLAPSED_GLYPH} Bash rm -rf / ✗"
    for row in (tool, thinking, failed):
        assert "[enter]" not in row.render().plain


def test_the_toggle_glyph_is_the_big_triangle_pair() -> None:
    # Given / When: a row in each state
    collapsed = ToolRow("Bash ls -la")
    expanded = ToolRow("Bash ls -la")
    expanded._toggle()
    # Then: U+25B6 collapsed / U+25BC expanded, not the old small triangles
    assert collapsed.render().plain.startswith(COLLAPSED_GLYPH)
    assert expanded.render().plain.startswith(EXPANDED_GLYPH)
    assert collapsed.render().plain.startswith("\u25b6")
    assert expanded.render().plain.startswith("\u25bc")


def test_the_hit_math_uses_the_measured_glyph_width() -> None:
    # Given: the chosen glyphs, both East-Asian-Ambiguous
    # Then: the width the hit math uses is the width they really spend
    assert GLYPH_CELLS == cell_len(COLLAPSED_GLYPH) == cell_len(EXPANDED_GLYPH)


# ─── the whole row is the button ────────────────────────────────────────────


def test_a_plain_click_on_the_glyph_toggles_a_tool_row() -> None:
    row = ToolRow("Bash ls -la")
    click(row, row._glyph_column, 0)
    assert row._expanded is True


def test_a_plain_click_on_the_header_text_toggles_a_tool_row() -> None:
    row = ToolRow("Bash ls -la")
    click(row, row._glyph_column + GLYPH_CELLS + 3, 0)
    assert row._expanded is True


def test_a_second_plain_click_re_collapses_a_tool_row() -> None:
    row = ToolRow("Bash ls -la")
    row._toggle()
    click(row, row._glyph_column + GLYPH_CELLS + 5, 0)
    assert row._expanded is False


def test_a_plain_click_on_the_expanded_body_toggles_a_tool_row() -> None:
    # Given: an EXPANDED row holding a command and its output
    row = ToolRow("Bash ls -la", detail="ls -la /tmp")
    row.set_result("hello world")
    row._toggle()
    # When / Then: a click on the command line of the body collapses it
    click(row, row._glyph_column + 2, 1)
    assert row._expanded is False
    row._toggle()
    # And: the attached result line does the same
    click(row, row._glyph_column + 2, 3)
    assert row._expanded is False


def test_a_plain_click_toggles_a_thinking_row_on_the_header_and_the_body() -> None:
    row = ThinkingRow("some deep reasoning")
    click(row, row._glyph_column + GLYPH_CELLS + 2, 0)
    assert row._expanded is True
    click(row, row._glyph_column + 2, 1)
    assert row._expanded is False


def test_a_click_on_the_header_toggles_a_thinking_row() -> None:
    row = ThinkingRow("some deep reasoning")
    click(row, 0, 0)
    assert row._expanded is True


def test_enter_still_toggles_a_tool_row() -> None:
    row = ToolRow("Bash ls -la")
    row.key_enter()
    assert row._expanded is True


def test_enter_is_consumed_only_by_a_collapsible_row() -> None:
    # Given: a plain body row and a collapsible one
    plain = EventRow("loop output")
    collapsible = ToolRow("Bash ls -la")
    from miniouto.cli.tui.input import Key

    # Then: Enter belongs to the collapsible row alone
    assert plain.handle_key(Key("Enter")) is False
    assert collapsible.handle_key(Key("Enter")) is True
    assert collapsible._expanded is True
    # And: Enter with a modifier is not the toggle gesture
    assert collapsible.handle_key(Key("Enter", ctrl=True)) is False


def test_a_click_without_a_tracked_press_still_toggles() -> None:
    # Given: a row whose gesture tracker never saw a mouse-down
    row = ToolRow("Bash ls -la")
    # When / Then: both clicks count as plain clicks
    click(row, 0, 0)
    assert row._expanded is True
    click(row, 0, 3)
    assert row._expanded is False


def test_a_drag_that_selects_text_never_toggles_the_row() -> None:
    row = ToolRow("Bash ls -la")
    # When: the pointer presses on the header, drags across it, releases
    row.on_mouse_down(RowMouseEvent((3, 0), LEFT_MOUSE_BUTTON, 1))
    row.on_mouse_move(RowMouseEvent((13, 0), LEFT_MOUSE_BUTTON, 1))
    row.selection = Selection({0: (1, 12)})
    row.on_click(RowMouseEvent((13, 0), LEFT_MOUSE_BUTTON, 1))
    # Then: the row stayed collapsed, and the drag really took text
    assert row._expanded is False
    assert row.get_selection(row.selection)[0] == " Bash ls -l"


def test_a_drag_over_the_expanded_body_keeps_the_body_text_selected() -> None:
    row = ToolRow("Bash ls -la", detail="ls -la /tmp")
    row._toggle()
    row.on_mouse_down(RowMouseEvent((3, 1), LEFT_MOUSE_BUTTON, 1))
    row.on_mouse_move(RowMouseEvent((6, 1), LEFT_MOUSE_BUTTON, 1))
    # Content offsets, the space the app builds them in: the press at outer cell
    # 3 is content offset 1 (the border + pad are the gutter).
    row.selection = Selection({1: (1, 5)})
    row.on_click(RowMouseEvent((6, 1), LEFT_MOUSE_BUTTON, 1))
    assert row._expanded is True
    assert row.get_selection(row.selection)[0] == "s -l"


def test_a_drag_that_selects_nothing_still_toggles_the_row() -> None:
    # Given: an expanded row whose middle line is blank, so a drag across it takes
    # no character at all
    row = ThinkingRow("alpha\n\nbeta")
    row._toggle()
    row.on_mouse_down(RowMouseEvent((3, 2), LEFT_MOUSE_BUTTON, 1))
    row.on_mouse_move(RowMouseEvent((7, 2), LEFT_MOUSE_BUTTON, 1))
    row.selection = Selection({2: (3, 7)})
    row.on_click(RowMouseEvent((7, 2), LEFT_MOUSE_BUTTON, 1))
    # Then: nothing was selected, so the gesture counts as a plain click
    assert row._expanded is False
    assert not row.get_selection(row.selection)[0]


def test_a_double_click_toggles_once() -> None:
    row = ToolRow("Bash ls -la")
    click(row, 3, 0, chain=1)
    # The second click of the chain is consumed, so the row opened once and did
    # not flicker shut again
    click(row, 3, 0, chain=2)
    assert row._expanded is True


def test_a_triple_click_toggles_once() -> None:
    row = ToolRow("Bash ls -la")
    toggles: list[bool] = []
    real_toggle = ToolRow._toggle

    def counting(self: ToolRow) -> None:
        toggles.append(True)
        real_toggle(self)

    ToolRow._toggle = counting
    try:
        click(row, 3, 0, chain=1)
        click(row, 3, 0, chain=2)
        click(row, 3, 0, chain=3)
    finally:
        ToolRow._toggle = real_toggle
    assert len(toggles) == 1


def test_a_row_refuses_text_select_all() -> None:
    # Given: a row the framework asks to select everything (chain == 2)
    row = ToolRow("Bash ls -la")
    # When / Then: nothing becomes selected — a drag is the only selection a row has
    row.text_select_all()
    assert row.selection is None


def test_the_click_scope_is_the_whole_row_not_just_the_header_line() -> None:
    row = ToolRow("Bash ls -la")
    move(row, row._glyph_column, 0)
    click(row, 0, 0)
    assert row._expanded is True
    click(row, 0, 1)
    assert row._expanded is False
    row.key_enter()
    assert row._expanded is True


def test_a_terminal_mouse_gesture_toggles_on_release() -> None:
    """The adapter: down + up on one cell is a click; a drag in between is not."""

    from miniouto.cli.tui.input import Mouse

    row = ToolRow("Bash ls -la")
    assert row.handle_mouse(Mouse("down", 5, 0), 5, 0) is True
    assert row.handle_mouse(Mouse("up", 5, 0), 5, 0) is True
    assert row._expanded is True

    dragged = ToolRow("Bash ls -la")
    dragged.handle_mouse(Mouse("down", 5, 0), 5, 0)
    dragged.handle_mouse(Mouse("drag", 15, 0, button=0), 15, 0)
    dragged.selection = Selection({0: (1, 12)})
    dragged.handle_mouse(Mouse("up", 15, 0), 15, 0)
    assert dragged._expanded is False


# ─── hover is scoped to the glyph ───────────────────────────────────────────


def glyph_style(row: RowStatic) -> pt.Style:
    """The painted style of the toggle glyph, i.e. the header's first text run."""

    line = row.build_lines(WIDTH, THEME)[0]
    # `[border, pad, glyph, header…]` — the glyph is the first run with the arrow.
    for text, style in line:
        if text.strip() in (COLLAPSED_GLYPH, EXPANDED_GLYPH):
            return style
    raise AssertionError(f"no glyph in {plain_of(line)!r}")


def header_style(row: RowStatic) -> pt.Style:
    line = row.build_lines(WIDTH, THEME)[0]
    for text, style in line:
        if text.strip().startswith("Bash") or text.strip().startswith("thinking"):
            return style
    raise AssertionError(f"no header text in {plain_of(line)!r}")


def test_hovering_the_glyph_highlights_only_the_glyph() -> None:
    row = ToolRow("Bash ls -la")
    resting = glyph_style(row)
    # When: the pointer sits on the glyph cell
    move(row, row._glyph_column, 0)
    # Then: the glyph itself goes reverse-video
    assert row.hover_glyph is True
    highlighted = glyph_style(row)
    assert "REVERSED" in repr(highlighted)
    assert repr(highlighted) != repr(resting)
    # And: the header text beside it keeps its resting style
    assert "REVERSED" not in repr(header_style(row))


def test_hovering_the_header_text_leaves_the_row_unchanged() -> None:
    row = ToolRow("Bash ls -la")
    resting = repr(glyph_style(row))
    # When: the pointer moves onto the header TEXT, one cell past the glyph
    move(row, row._glyph_column + GLYPH_CELLS, 0)
    # Then: no highlight — the hit math stops at the measured glyph width
    assert row.hover_glyph is False
    assert repr(glyph_style(row)) == resting
    move(row, row._glyph_column + GLYPH_CELLS + 2, 0)
    assert row.hover_glyph is False
    assert repr(glyph_style(row)) == resting


def test_hovering_the_body_leaves_the_row_unchanged() -> None:
    row = ToolRow("Bash ls -la", detail="ls -la /tmp")
    row._toggle()
    resting = repr(glyph_style(row))
    move(row, 0, 1)
    assert row.hover_glyph is False
    assert repr(glyph_style(row)) == resting


def test_the_gutter_offsets_the_glyph_hit_box() -> None:
    # Given: an EventRow's border + padding gutter
    row = ToolRow("Bash ls -la")
    # Then: the glyph column starts AFTER it, and the border cell is not the glyph
    assert row._glyph_column == 2
    move(row, 1, 0)
    assert row.hover_glyph is False
    move(row, 2, 0)
    assert row.hover_glyph is True


def test_leaving_the_row_reverts_the_glyph_highlight() -> None:
    row = ToolRow("Bash ls -la")
    move(row, row._glyph_column, 0)
    row.on_leave()
    assert row.hover_glyph is False
    assert "REVERSED" not in repr(glyph_style(row))


def test_a_thinking_row_highlights_its_glyph_on_hover() -> None:
    row = ThinkingRow("reasoning")
    move(row, row._glyph_column, 0)
    assert row.hover_glyph is True
    assert "REVERSED" in repr(glyph_style(row))
    # And: the `thinking` label beside it is untouched
    assert "REVERSED" not in repr(header_style(row))


def test_hovering_the_same_cell_twice_does_not_rebuild_the_row() -> None:
    # A mouse-move storm over one cell must not invalidate the painted cache; the
    # version counter is what every cache key hangs on.
    row = ToolRow("Bash ls -la")
    move(row, row._glyph_column, 0)
    version = row._version
    move(row, row._glyph_column, 0)
    assert row._version == version


# ─── streaming ──────────────────────────────────────────────────────────────


def test_stream_deltas_update_one_row_in_place() -> None:
    row = StreamRow("Hel")
    for char in "lo world":
        row.append(char)
    assert row._text == "Hello world"
    assert row.render().plain == "Hello world"
    assert rendered(row) == "┃ Hello world"


def test_an_empty_delta_never_invalidates_the_row() -> None:
    row = StreamRow("text")
    version = row._version
    row.append("")
    assert row._version == version
    assert row._text == "text"


def test_thinking_deltas_extend_the_live_reasoning_row() -> None:
    row = ThinkingRow()
    row.begin_stream()
    row.append("first ")
    row.append("second")
    # One collapsed row with the live marker plus its reasoning tail, never
    # one row per fragment
    assert row._thinking == "first second"
    assert row.render().plain.startswith(f"{COLLAPSED_GLYPH} thinking ●")
    assert row.render().plain.endswith("first second")
    # And: the completed message replaces the streamed text in place — the
    # settled row is exactly `▶ thinking`, marker and tail both gone
    row.finalize("first second (full)")
    assert row._thinking == "first second (full)"
    assert row.render().plain == f"{COLLAPSED_GLYPH} thinking"


def test_a_live_thinking_row_still_toggles_and_hovers() -> None:
    row = ThinkingRow()
    row.begin_stream()
    row.append("reasoning")
    click(row, 0, 0)
    assert row._expanded is True
    # The expanded body is the reasoning text, below the header
    assert rendered(row).split("\n")[1].endswith("reasoning")



# ─── the incremental stream wrap ─────────────────────────────────────────────
#
# StreamRow freezes every visual line a previous wrap had broken away and
# re-wraps only the tail from the last visual line's start, so an append costs
# O(tail) instead of a full re-wrap of the message. These tests hold that
# shortcut to the contract it must keep: byte-identical output to the full
# re-wrap the base class performs, across random deltas (newlines, CJK, tabs,
# wide spaces), width changes, theme changes and a selection painted on top.


def full_rewrap(row: StreamRow, width: int, theme: object) -> list[list[tuple[str, object]]]:
    """The ground truth: a fresh StreamRow's one-shot wrap of the same text."""

    fresh = StreamRow(row.text)
    # A fresh row's first build is a full wrap (no frozen prefix, no cache).
    return fresh.build_lines(width, theme)


def test_incremental_matches_the_full_rewrap_on_random_deltas() -> None:
    import random

    alphabet = [
        "word ",
        "x",
        "\n",
        "\n\n",
        "漢字 ",
        "🙂",
        "　",  # U+3000: a WIDE space, also `isspace`
        "\t",
        "  ",
        "supercalifragilisticexpialidocious ",
        "a\n b",
        "end.",
    ]
    rng = random.Random(4242)
    for _ in range(200):
        row = StreamRow(rng.choice(["", "seed ", "line\n", "  ind\n\n"]))
        width = rng.choice([1, 2, 3, 7, 11, 30, 118])
        for _ in range(rng.randrange(1, 30)):
            row.append(rng.choice(alphabet))
            got = row.build_lines(width, THEME)
            want = full_rewrap(row, width, THEME)
            assert repr(got) == repr(want), row.text


def test_a_width_change_rewraps_the_whole_text_correctly() -> None:
    row = StreamRow("one two three\nfour five six seven eight")
    for width in (60, 10, 3, 118, 10):
        row.append(" tail")  # grow between switches: cache must not leak across
        got = row.build_lines(width, THEME)
        want = full_rewrap(row, width, THEME)
        assert repr(got) == repr(want)
        for line in got:
            assert cells_of(line[2:]) <= max(1, width - 2) or width <= 3


def test_a_theme_change_rewraps_the_whole_text_correctly() -> None:
    from miniouto.cli.tui.theme import get_theme

    other = get_theme("textual-dark")
    assert other is not None and other is not THEME
    row = StreamRow("alpha beta\ngamma delta epsilon")
    row.build_lines(WIDTH, THEME)
    row.append(" more")  # frozen under THEME…
    same_theme = row.build_lines(WIDTH, THEME)
    assert repr(same_theme) == repr(full_rewrap(row, WIDTH, THEME))
    # …then a different theme re-wraps everything in the new ink
    rethemed = row.build_lines(WIDTH, other)
    assert repr(rethemed) == repr(full_rewrap(row, WIDTH, other))
    assert repr(rethemed[0][2][1]) != repr(same_theme[0][2][1])
    # and back again — still exact
    row.append(" tail")
    assert repr(row.build_lines(WIDTH, THEME)) == repr(full_rewrap(row, WIDTH, THEME))


def test_a_selection_paints_correctly_on_the_incremental_cache() -> None:
    row = StreamRow("hello streaming world, this line wraps at a narrow width")
    row.append(" and keeps going")
    lines = row.build_lines(20, THEME)
    span = Selection({0: (0, 5)})
    # The frozen prefix is shared with the painted cache; the selection must
    # be applied on top of every call and never served from a stale cache.
    painted = row.build_lines(20, THEME, selection=span)
    assert lit_text(painted) == "hello"
    # An unselected rebuild afterwards is untouched, and re-selecting lights
    # the same characters again (no cross-contamination between the two).
    assert lit_text(row.build_lines(20, THEME)) == ""
    assert lit_text(row.build_lines(20, THEME, selection=span)) == "hello"
    assert painted[0] is not lines[0] or True  # painted copies, never mutates


def test_a_shrunk_text_falls_back_to_a_full_rewrap() -> None:
    # The incremental path is valid only while the text GROWS; anything that
    # makes it shorter must rebuild from scratch rather than stitch a stale
    # frozen prefix onto a tail that no longer follows it.
    row = StreamRow("one two three four five six")
    row.build_lines(10, THEME)
    row._text = "short now"
    row._refresh()  # a state change invalidates the painted cache…
    got = row.build_lines(10, THEME)
    assert repr(got) == repr(full_rewrap(row, 10, THEME))



def test_a_delta_splitting_a_vs16_cluster_wraps_like_the_full_rewrap() -> None:
    # cell_len('↔') is 1 but cell_len('↔️') is 2 — the VS16 widens the glyph
    # it follows. A delta that lands the VS16 must not leave the tail start
    # stranded between the base glyph and its selector: the region would
    # measure '↔' (1 cell) where the full wrap measures '↔️' (2), and the
    # incremental wrap would drift off the re-wrap's lines.
    row = StreamRow("🙂\u200d↔")
    row.build_lines(1, THEME)
    row.append("️a")  # the VS16 first, then the plain letter
    got = row.build_lines(1, THEME)
    want = full_rewrap(row, 1, THEME)
    assert repr(got) == repr(want)
    assert rendered(row, 1).split("\n") == ["┃ 🙂", "┃ \u200d↔️", "┃ a"]


def test_a_delta_splitting_a_zwj_sequence_wraps_like_the_full_rewrap() -> None:
    # '🙂\u200d↔️' is ONE cluster: cell_len measures it 2, but the character
    # sum (2+0+1+0) is 3. A tail cut between the joiner and the arrow hands
    # the engine a differently-measured atom, so the freeze must step back to
    # a start that keeps the cluster whole.
    row = StreamRow("🙂\u200d")
    row.build_lines(1, THEME)
    row.append("↔️a")
    got = row.build_lines(1, THEME)
    want = full_rewrap(row, 1, THEME)
    assert repr(got) == repr(want)
    assert rendered(row, 1).split("\n") == ["┃ 🙂", "┃ \u200d↔️", "┃ a"]


def test_a_delta_splitting_a_keycap_wraps_like_the_full_rewrap() -> None:
    # '1️⃣' is a keycap: a 1-cell digit widened to 2 by its selector+joining
    # ligature. The delta that completes it must not freeze mid-cluster.
    row = StreamRow("x1")
    row.build_lines(1, THEME)
    row.append("️⃣2")
    got = row.build_lines(1, THEME)
    want = full_rewrap(row, 1, THEME)
    assert repr(got) == repr(want)
    assert rendered(row, 1).split("\n") == ["┃ x", "┃ 1️⃣", "┃ 2"]


def test_a_growing_word_never_freezes_on_its_own_boundary() -> None:
    # cell_len("\u200b") is 0, but "\u200b漢" measures 2: the zero-width space
    # is re-measured the moment a delta extends the word it opens. The last
    # word of the text is exactly the one a delta can still extend, so a
    # freeze may sit on its boundary only once a space has closed it.
    row = StreamRow("word ")
    row.build_lines(3, THEME)
    row.append("\u200b")  # an open final word: 0 cells now, maybe 2 later
    row.build_lines(3, THEME)
    row.append("漢字テスト ")  # the cluster grows under the frozen prefix
    got = row.build_lines(3, THEME)
    assert repr(got) == repr(full_rewrap(row, 3, THEME))


def test_a_word_that_fits_the_emptied_line_starts_clean() -> None:
    # A word arriving when the line is full flushes it and often fits WHOLE
    # on the emptied line — the engine lays it down exactly as its fits
    # check would, so the line it opens is CLEAN and a legal resume point.
    # Marking it sliced made every pure-ASCII stream re-wrap the whole
    # message on each delta: O(n²), the lag this mirror exists to kill.
    from miniouto.cli.tui.rows import _visual_line_starts

    text = " ".join(f"word{i}" for i in range(120)) + " "
    starts = _visual_line_starts(text, 118)
    assert starts[0] == (0, True)
    assert all(clean for _offset, clean in starts), starts


def test_a_plain_word_stream_actually_rides_the_incremental_path() -> None:
    # The clean-start rule is what lets the freeze advance: a plain word
    # stream must keep moving its tail forward instead of re-wrapping the
    # whole message per delta. Wrap at the width the row really uses —
    # build_lines hands _content_lines the width minus the 2-cell gutter.
    row = StreamRow("")
    frozen = tail = 0
    for index in range(600):
        row.append(f"word{index} ")
        row.build_lines(118, THEME)
        frozen = max(frozen, len(row._wrap_frozen))
        tail = max(tail, row._wrap_tail_start)
    assert repr(row.build_lines(118, THEME)) == repr(full_rewrap(row, 118, THEME))
    assert frozen > 0  # visual lines were broken away and kept
    assert tail > 0  # the re-wrap region moved off the first line


# ─── parsing the saved paths ────────────────────────────────────────────────


def test_the_art_path_is_found_mid_result_like_a_real_computer_result() -> None:
    assert art_path_in_result(computer_result(ART_PATH)) == ART_PATH


def test_the_art_path_is_found_on_the_last_line() -> None:
    assert art_path_in_result(f"done\nSaved to: {ART_PATH}") == ART_PATH


def test_a_path_mentioned_mid_sentence_is_not_an_art_path() -> None:
    assert art_path_in_result(f"the log said Saved to: {ART_PATH} yesterday") is None


def test_no_art_path_is_found_in_a_result_that_saves_nothing() -> None:
    assert art_path_in_result("error: something went wrong") is None


def test_the_saved_line_must_name_an_art_file() -> None:
    assert art_path_in_result("Saved to: /tmp/report.txt") is None


def test_the_png_path_is_found_on_its_own_line_only() -> None:
    assert png_path_in_result("Image saved to: /tmp/a.png") == "/tmp/a.png"
    assert png_path_in_result("the Image saved to: /tmp/a.png line") is None
    assert png_path_in_result("Saved to: /tmp/a.art") is None


# ─── result + art rendering ─────────────────────────────────────────────────


def test_the_result_text_carries_no_escape_bytes() -> None:
    row = ToolRow("Computer screen", tool_name="Computer")
    row.set_result(computer_result(ART_PATH))
    # Raw SGR bytes are unreadable as text; the art file below is the real view
    assert "\x1b" not in row._result_text()


def test_the_result_text_still_carries_the_preview_and_the_path() -> None:
    row = ToolRow("Computer screen", tool_name="Computer")
    row.set_result(computer_result(ART_PATH))
    text = row._result_text()
    assert "Click coordinates" in text
    assert ART_PATH in text


def test_a_long_result_is_truncated_at_the_display_cap() -> None:
    row = ToolRow("Bash", tool_name="Bash")
    row.set_result("x" * (ToolRow._MAX_RESULT_CHARS + 500))
    out = row._result_text()
    assert len(out) < ToolRow._MAX_RESULT_CHARS + 500
    assert "truncated" in out


def test_a_long_detail_is_truncated_at_the_display_cap() -> None:
    row = ToolRow("Bash", detail="d" * (ToolRow._MAX_DETAIL_CHARS + 500))
    row._toggle()
    painted = rendered(row)
    assert "truncated" in painted
    assert painted.count("d") <= ToolRow._MAX_DETAIL_CHARS + 1


def test_an_unbounded_result_is_never_truncated() -> None:
    row = ToolRow("answer", unbounded_result=True)
    row.set_result("y" * (ToolRow._MAX_RESULT_CHARS + 500))
    assert row._result_text() == "y" * (ToolRow._MAX_RESULT_CHARS + 500)


def test_set_result_is_idempotent() -> None:
    row = ToolRow("Bash")
    row.set_result("first")
    row.set_result("second", is_error=True)
    assert row._result == "first"
    assert row._is_error is False


def test_an_error_result_marks_the_row_and_the_header() -> None:
    row = ToolRow("Bash rm -rf /")
    row.set_result("error: nope", is_error=True)
    assert row.has_class("-error") is True
    assert row.render().plain.endswith("✗")
    # The `-error` class is what swaps the border tint and the text layer
    border = row.build_lines(WIDTH, THEME)[0][0][1]
    assert rgb(blend(THEME.error, THEME.background, 0.60)) in repr(border)


def test_the_screen_art_is_capped_with_an_overflow_note(tmp_path: Path) -> None:
    path = write_art(tmp_path, rows=60)
    row = ToolRow("Computer screen", tool_name="Computer")
    row.set_result(computer_result(path))
    block = row._screen_art()
    assert block is not None
    # Only the cap is rendered, and the rest is named in a note that tells the user
    # how to see all of it
    assert len(block.plain.split("\n")) >= _ART_MAX_ROWS
    assert "… 20 more rows" in block.plain
    assert f"miniouto computer show {path}" in block.plain


def test_the_screen_art_keeps_its_truecolour_spans(tmp_path: Path) -> None:
    path = write_art(tmp_path, rows=3)
    row = ToolRow("Computer screen", tool_name="Computer")
    row.set_result(computer_result(path))
    block = row._screen_art()
    assert block is not None
    # Real colour, not a grey ramp of characters
    assert any(getattr(span.style, "color", None) is not None for span in block.spans)


def test_no_screen_art_is_rendered_for_a_tool_that_is_not_computer(
    tmp_path: Path,
) -> None:
    # Given: a Bash result that happens to end with the same "Saved to:" line
    path = write_art(tmp_path, rows=3)
    row = ToolRow("Bash cat", tool_name="Bash")
    row.set_result(f"cat {path}\nSaved to: {path}\n")
    # Then: no art block — the tool name gates it
    assert row._screen_art() is None


def test_a_screenshot_result_renders_its_art_just_like_a_screen_call(
    tmp_path: Path,
) -> None:
    path = write_art(tmp_path, rows=60)
    row = ToolRow("Computer screenshot", tool_name="Computer")
    row.set_result(screenshot_result(path))
    block = row._screen_art()
    assert block is not None
    assert str(path) in block.plain
    assert "… 20 more rows" in block.plain


def test_the_art_file_is_not_read_while_the_row_is_collapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a capture taller than the inline cap, so the note has something to
    # count as well as the laziness to prove
    path = write_art(tmp_path, rows=60)
    row = ToolRow("Computer screen", tool_name="Computer")
    row.set_result(computer_result(path))
    reads: list[str] = []
    original = Path.read_text

    def tracking(self: Path, *args: object, **kwargs: object) -> str:
        reads.append(str(self))
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", tracking)
    # When: the collapsed row renders
    row._refresh()
    rendered(row)
    # Then: nothing was read from disk — a session of screenshots must not touch
    # the filesystem on load
    assert reads == []
    # And once expanded it reads exactly once and shows the cap plus the note
    row._toggle()
    painted = rendered(row, 200)
    assert str(path) in reads
    # The note lives in the row's own text (asserted UNWRAPPED, since a narrow
    # width soft-wraps the path onto a second line) and it names the command that
    # shows the rows the cap hid.
    assert f"… {60 - _ART_MAX_ROWS} more rows" in row.render().plain
    assert f"miniouto computer show {path}" in row.render().plain
    assert f"… {60 - _ART_MAX_ROWS} more rows" in painted
    row.build_lines(WIDTH, THEME)
    assert reads.count(str(path)) == 1


def test_a_missing_art_file_renders_nothing_rather_than_raising(
    tmp_path: Path,
) -> None:
    row = ToolRow("Computer screen", tool_name="Computer")
    row.set_result(f"Saved to: {tmp_path}/gone.art\n")
    assert row._screen_art() is None


def test_a_corrupt_art_file_renders_nothing_rather_than_raising(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad.art"
    bad.write_text("not a screen-art file at all", encoding="utf-8")
    row = ToolRow("Computer screen", tool_name="Computer")
    row.set_result(f"Saved to: {bad}\n")
    # parse_art's refusal is contained and the row still renders
    assert row._screen_art() is None
    assert rendered(row).startswith("┃")


# ─── the graphics lane ──────────────────────────────────────────────────────
#
# The image lane is a LATE-ADDED sibling of the `.art` text lane: on a Kitty/Sixel
# terminal the row reserves the pixel rect and hands the placement to the app, and
# on every other terminal the `.art` render is what the user sees. These tests pin
# both halves, plus the import-guard: `graphics` must never be able to break a chat
# row.


def _png(directory: Path) -> Path:
    """A real 64x48 PNG, so the placement maths has pixels to measure."""

    image = pytest.importorskip("PIL.Image")
    path = directory / "shot.png"
    image.new("RGB", (64, 48), (200, 30, 40)).save(path)
    return path


def _screenshot(png: Path) -> str:
    return f"Screenshot of the virtual display.\nImage saved to: {png}"


def test_the_graphics_module_is_imported_lazily_and_defensively() -> None:
    from miniouto.cli.tui import rows as rows_module

    # A missing/broken graphics module must degrade to the `.art` lane, never
    # raise into a chat row.
    saved = rows_module._GRAPHICS
    rows_module._GRAPHICS = None
    try:
        assert rows_module._graphics_module() is not None  # it imports fine here
    finally:
        rows_module._GRAPHICS = saved
    row = ToolRow("Computer screenshot", tool_name="Computer")
    saved = rows_module._GRAPHICS
    rows_module._GRAPHICS = None
    try:
        # An unimportable module answers None, and the row keeps the text lane.
        rows_module._GRAPHICS = None
        row.set_result("Saved to: /tmp/missing.art")
        assert row._screen_art() is None
    finally:
        rows_module._GRAPHICS = saved


def test_a_png_needs_a_graphics_terminal_and_falls_back_to_the_art_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from miniouto.cli.tui import graphics

    png = _png(tmp_path)
    art = write_art(tmp_path, rows=3)
    monkeypatch.setattr(graphics, "_GRAPHICS_MODE", graphics.GRAPHICS_MODE_TEXT)
    row = ToolRow("Computer screenshot", tool_name="Computer")
    row.set_result(f"shot\nImage saved to: {png}\nSaved to: {art}")
    row._toggle()
    # TEXT mode: no placement, and the `.art` render is what the row shows
    assert row.image is None
    assert row.image_visible is False
    assert row.image_placements() == []
    block = row._screen_art()
    assert block is not None and str(art) in block.plain


def test_a_png_on_a_graphics_terminal_reserves_its_rect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from miniouto.cli.tui import graphics

    png = _png(tmp_path)
    monkeypatch.setattr(graphics, "_GRAPHICS_MODE", graphics.GRAPHICS_MODE_TGP)
    row = ToolRow("Computer screenshot", tool_name="Computer")
    row.set_result(_screenshot(png))
    row._toggle()
    lines = row.build_lines(WIDTH, THEME)
    # The pixels are emitted by the app AFTER the frame; the row's job is to
    # reserve the cells and say where they start.
    assert row.image is not None
    assert row.image_visible is True
    placements = row.image_placements()
    assert len(placements) == 1
    line, x, placement = placements[0]
    assert x == row._gutter_width
    assert line < len(lines)
    assert len(lines) == row.image_line + int(placement.height_cells)
    assert "screen image" in row.render().plain


def test_a_collapsed_image_row_reserves_nothing_and_releases_on_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from miniouto.cli.tui import graphics

    png = _png(tmp_path)
    monkeypatch.setattr(graphics, "_GRAPHICS_MODE", graphics.GRAPHICS_MODE_TGP)
    row = ToolRow("Computer screenshot", tool_name="Computer")
    row.set_result(_screenshot(png))
    # Collapsed: the row is the header line only
    assert len(row.build_lines(WIDTH, THEME)) == 1
    row._toggle()
    row.build_lines(WIDTH, THEME)
    assert row.image is not None
    row.release_image()
    assert row.image is None
    assert row.image_placements() == []


def test_an_unreadable_png_falls_back_to_the_art_lane_rather_than_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from miniouto.cli.tui import graphics

    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a png at all")
    monkeypatch.setattr(graphics, "_GRAPHICS_MODE", graphics.GRAPHICS_MODE_TGP)
    row = ToolRow("Computer screenshot", tool_name="Computer")
    row.set_result(_screenshot(broken))
    row._toggle()
    painted = rendered(row)
    # A capture the terminal cannot decode must not take the whole log down
    assert row.image is None
    assert row.image_visible is False
    assert row._image_failed is True
    assert painted.startswith("┃")


def test_the_image_never_shares_a_line_with_the_rows_own_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pixels get their OWN lines, below the caption.

    A child widget would be laid out at the top of the parent's content area —
    exactly where the caption is — so the two would be drawn on top of each
    other. This row therefore RESERVES trailing lines for the image and reports
    that rect to the app, which emits it out of band after the frame.
    """

    from miniouto.cli.tui import graphics

    png = _png(tmp_path)
    monkeypatch.setattr(graphics, "_GRAPHICS_MODE", graphics.GRAPHICS_MODE_TGP)
    row = ToolRow("Computer screenshot", tool_name="Computer")
    row.set_result(_screenshot(png))
    row._toggle()
    lines = row.build_lines(WIDTH, THEME)
    (line, _x, placement), = row.image_placements()
    # Given/When: the reserved rect
    # Then: it starts at or below the caption's last line…
    assert line > 0
    assert line >= max(1, len(lines) - int(placement.height_cells))
    # …so no caption line falls inside it, and the row's own text is untouched
    assert line + int(placement.height_cells) == len(lines)
    assert plain_of(lines[0]).startswith("┃")
    assert "screen image" in row.render().plain


def test_a_screen_action_result_never_engages_the_image_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a PNG line engages the lane; a `screen` result names just the `.art`.

    The `screen` action writes no PNG, so on a graphics-capable terminal it must
    still render as text art — the model's own text view of that capture is the
    stable one, and a screenshot is the only action whose pixels exist.
    """

    from miniouto.cli.tui import graphics

    art = write_art(tmp_path, rows=3)
    monkeypatch.setattr(graphics, "_GRAPHICS_MODE", graphics.GRAPHICS_MODE_TGP)
    row = ToolRow("Computer screen", tool_name="Computer")
    row.set_result(
        "Screen art captured (20x3 chars, halfblock truecolor).\n"
        f"Saved to: {art}\n"
        "Click coordinates still come from the screenshot action."
    )
    row._toggle()
    block = row._screen_art()
    assert block is not None and "screen art" in block.plain
    assert row.image is None
    assert row.image_visible is False
    assert row.image_placements() == []


# ─── per-tool badges ─────────────────────────────────────────────────────────
#
# A badged tool row replaces the ▶/▼ glyph with the tool's mark; a collapsed
# Bash row paints exactly ONE header line; an expanded Bash row boxes the
# command and the result, an expanded other-tool row boxes the result only.


def test_every_badged_tool_paints_its_measured_badge() -> None:
    # Given: one row per badged tool (Image/Video share a badge)
    cases = [
        ("Bash", "[>_]"),
        ("Computer", "[⮜]"),
        ("Image", "[⣏⣹]"),
        ("Video", "[⣏⣹]"),
        ("Audio", "[♫\ufe0e]"),
    ]
    for tool, badge in cases:
        assert TOOL_BADGES[tool] == badge
        row = ToolRow(f"{tool} preview", tool_name=tool)
        # Then: the badge is prepended to the verbatim header text
        assert row.render().plain == f"{badge} {tool} preview"
        # And: its width is MEASURED, not assumed (the audio badge is 4 chars
        # but 3 cells — `♫` carries a U+FE0E variation selector)
        assert row._hover_span() == cell_len(badge)
        # And: the glyph pair is gone from a badged header
        assert COLLAPSED_GLYPH not in row.render().plain


def test_a_row_without_a_badge_keeps_the_triangle_pair() -> None:
    # Given: a continue_session row and one built with no tool name at all
    cont = ToolRow("continue_session /s", tool_name="continue_session")
    plain = ToolRow("Bash ls -la")
    # Then: both keep ▶/▼ exactly as before
    assert cont.render().plain == f"{COLLAPSED_GLYPH} continue_session /s"
    assert cont._hover_span() == GLYPH_CELLS
    assert plain.render().plain == f"{COLLAPSED_GLYPH} Bash ls -la"
    assert plain._hover_span() == GLYPH_CELLS


def test_hover_covers_the_whole_badge_and_stops_at_its_edge() -> None:
    # Given: a badged Bash row
    row = ToolRow("Bash ls -la", tool_name="Bash")
    span = cell_len("[>_]")
    # Then: every badge cell highlights, and the first cell past it does not
    for x in range(row._glyph_column, row._glyph_column + span):
        move(row, x, 0)
        assert row.hover_glyph is True, x
    move(row, row._glyph_column + span, 0)
    assert row.hover_glyph is False
    # And: the badge paints reverse-video while hovered, the header does not
    move(row, row._glyph_column, 0)
    assert "REVERSED" in repr(row.build_lines(WIDTH, THEME)[0][2][1])
    assert "REVERSED" not in repr(row.build_lines(WIDTH, THEME)[0][3][1])


def test_a_collapsed_bash_row_paints_exactly_one_header_line() -> None:
    # Given: a Bash header long enough to wrap several times at WIDTH
    row = ToolRow("Bash " + "echo line1 && echo line2 && " * 10, tool_name="Bash")
    # When: it renders collapsed
    lines = row.build_lines(WIDTH, THEME)
    # Then: ONE line, cell-cut, ending in `…` — never a wrapped wall
    assert len(lines) == 1
    text = plain_of(lines[0])
    assert text.startswith("┃ [>_] Bash ")
    assert text.endswith("…")
    assert cells_of(lines[0]) <= WIDTH
    # And: a short header that fits keeps its whole text with no ellipsis
    short = ToolRow("Bash ls -la", tool_name="Bash")
    short_lines = short.build_lines(WIDTH, THEME)
    assert len(short_lines) == 1
    assert plain_of(short_lines[0]) == "┃ [>_] Bash ls -la"


def test_a_collapsed_bash_header_that_exactly_fits_is_kept_whole() -> None:
    # Given: a Bash header whose first wrapped line is EXACTLY the content
    # width (width 18, minus the 2-cell gutter, = 16 cells; the badge is 5)
    row = ToolRow("Bash " + "a" * 6, tool_name="Bash")
    # When: it paints at width 18
    lines = row.build_lines(18, THEME)
    # Then: the header is kept WHOLE — an exact fit is a fit, not a cut, so
    # the last `a` survives and no `…` is glued on
    assert len(lines) == 1
    assert plain_of(lines[0]) == "┃ [>_] Bash aaaaaa"
    assert cells_of(lines[0]) == 18


def test_a_collapsed_bash_header_one_cell_over_is_cut_with_an_ellipsis() -> None:
    # Given: the SAME header one cell wider than the content width — the
    # extra `b` wraps, so the row is one line cut by `truncate_to`
    row = ToolRow("Bash " + "a" * 6 + " b", tool_name="Bash")
    # When: it paints at width 18
    lines = row.build_lines(18, THEME)
    # Then: ONE line, `…`-terminated, never wrapped
    assert len(lines) == 1
    text = plain_of(lines[0])
    assert text.endswith("…")
    # And: the cut first line keeps every cell it can — `width - 1` plus the
    # ellipsis fills the row exactly
    assert cells_of(lines[0]) == 18


def test_an_expanded_bash_row_boxes_the_command_and_the_result() -> None:
    # Given: an expanded Bash row with a verbatim multi-line command and a
    # multi-line result
    row = ToolRow("Bash heredoc", detail="cat <<'EOF'\none\ntwo\nEOF", tool_name="Bash")
    row.set_result("out line 1\nout line 2")
    row._toggle()
    # When: it paints
    text = rendered(row)
    # Then: TWO boxes — the command first, the result below
    assert text.count("╭") == 2
    assert text.count("╯") == 2
    lines = text.split("\n")
    assert lines[0] == "┃ [>_] Bash heredoc"
    assert any(line.startswith("┃ │ cat <<'EOF'") for line in lines), lines
    assert any(line.startswith("┃ │ one") for line in lines), lines
    assert any(line.startswith("┃ │ two") for line in lines), lines
    assert any(line.startswith("┃ │ EOF") for line in lines), lines
    assert any(line.startswith("┃ │ out line 1") for line in lines), lines
    assert any(line.startswith("┃ │ out line 2") for line in lines), lines
    # And: every box line is exactly the content width (56 at WIDTH 60)
    for line in row.build_lines(WIDTH, THEME):
        assert cells_of(line) <= WIDTH


def test_an_expanded_other_tool_row_boxes_the_result_only() -> None:
    # Given: expanded media and Computer rows (no detail, a result attached)
    for tool in ("Image", "Audio", "Computer"):
        row = ToolRow(f"{tool} preview", tool_name=tool)
        row.set_result("result line")
        row._toggle()
        # When: they paint
        text = rendered(row)
        # Then: ONE box — their call preview already is the header
        assert text.count("╭") == 1, tool
        assert "│ result line" in text
        assert f"{TOOL_BADGES[tool]} {tool} preview" in text


def test_the_bash_command_box_falls_back_to_the_header_without_detail() -> None:
    # Given: a row from an OLD session event — no detail, the header still
    # leads with the flattened command
    row = ToolRow("Bash echo one && echo two", tool_name="Bash")
    row.set_result("ok")
    row._toggle()
    # When: it expands
    text = rendered(row)
    # Then: the command box carries the header minus the `Bash ` prefix
    assert text.count("╭") == 2
    assert "│ echo one && echo two" in text


def test_a_drag_across_a_boxed_content_line_extracts_the_right_characters() -> None:
    # Given: an expanded Bash row holding a command box
    row = ToolRow("Bash ls", detail="abcdef", tool_name="Bash")
    row.set_result("output")
    row._toggle()
    row.build_lines(WIDTH, THEME)
    # The command text sits on the row's 3rd line, inside the box's content
    # area (`│ ` is 2 cells of chrome in front of it)
    row.on_mouse_down(RowMouseEvent((4, 2), LEFT_MOUSE_BUTTON, 1))
    row.on_mouse_move(RowMouseEvent((7, 2), LEFT_MOUSE_BUTTON, 1))
    row.selection = Selection({2: (2, 5)})
    row.on_click(RowMouseEvent((7, 2), LEFT_MOUSE_BUTTON, 1))
    # Then: the drag took exactly the command characters — boxes are CONTENT,
    # so the selection keeps addressing them (and never toggled the row)
    assert row._expanded is True
    assert row.get_selection(row.selection)[0] == "abc"


# ─── the live thinking tail ─────────────────────────────────────────────────


def test_a_streaming_thinking_row_carries_a_one_line_tail() -> None:
    # Given: a live row whose reasoning is longer than one header
    row = ThinkingRow()
    row.begin_stream()
    row.append("alpha beta gamma " * 6 + "the final words")
    # When: it paints at width 80
    lines = row.build_lines(80, THEME)
    # Then: ONE line, marker kept, whitespace-collapsed tail showing the LAST
    # words — never a second wrapped line
    assert len(lines) == 1
    text = plain_of(lines[0])
    assert text.startswith("┃ ▶ thinking ●")
    assert text.endswith("the final words")
    assert cells_of(lines[0]) <= 80


def test_a_narrow_width_prefixes_the_cut_tail_with_an_ellipsis() -> None:
    # Given: the same live row at a narrow width
    row = ThinkingRow()
    row.begin_stream()
    row.append("one two three four five six seven eight nine ten")
    # When: it paints at width 30
    text = plain_of(row.build_lines(30, THEME)[0])
    # Then: the head was cut, so the tail is `…`-prefixed and still ends with
    # the newest words
    assert text.startswith("┃ ▶ thinking ● …")
    assert text.endswith("nine ten")
    assert cell_len(text) <= 30


def test_the_tail_disappears_on_finalize_and_while_expanded() -> None:
    # Given: a live row with a tail on screen
    row = ThinkingRow()
    row.begin_stream()
    row.append("still reasoning about the thing")
    assert plain_of(row.build_lines(80, THEME)[0]).endswith("the thing")
    # When: the row expands
    row._toggle()
    # Then: the header is the plain marker line — no tail on it
    assert plain_of(row.build_lines(80, THEME)[0]) == "┃ ▼ thinking ●"
    row._toggle()
    # And when the message completes, the settled row is byte-identical to a
    # row that never streamed: exactly `▶ thinking`
    row.finalize("still reasoning about the thing")
    assert rendered(row) == f"┃ {COLLAPSED_GLYPH} thinking"
    assert row.render().plain == f"{COLLAPSED_GLYPH} thinking"


def test_the_tail_updates_on_every_append() -> None:
    # Given: a live row
    row = ThinkingRow()
    row.begin_stream()
    # When: two deltas land
    row.append("first words")
    one = plain_of(row.build_lines(80, THEME)[0])
    row.append(" and then later words")
    two = plain_of(row.build_lines(80, THEME)[0])
    # Then: the tail moved with the reasoning
    assert one.endswith("first words")
    assert two.endswith("and then later words")
    assert two != one


def test_the_tail_of_a_huge_reasoning_still_shows_the_final_words() -> None:
    # Given: a live row holding a 200k-char reasoning that ends with known
    # words — the tail must cost the same as a short one, never a full scan
    row = ThinkingRow()
    row.begin_stream()
    row.append("word " * 40_000 + "the very final words")
    # When: it paints at width 80
    lines = row.build_lines(80, THEME)
    # Then: ONE line whose tail carries the final words, `…`-prefixed
    assert len(lines) == 1
    text = plain_of(lines[0])
    assert text.startswith("┃ ▶ thinking ● …")
    assert text.endswith("the very final words")
    assert cells_of(lines[0]) <= 80


def test_the_tail_matches_the_full_scan_on_short_and_medium_texts() -> None:
    # Given: the reference implementation — collapse the WHOLE reasoning,
    # exactly the pre-window shape
    def reference(thinking: str, width: int) -> str:
        from miniouto.cli.tui.rows import _STREAMING_HEADER_CELLS, _tail_cells

        budget = width - _STREAMING_HEADER_CELLS - 1
        if budget < 4:
            return ""
        text = " ".join(thinking.split())
        if not text or cell_len(text) <= budget:
            return text
        return "…" + _tail_cells(text, budget - 1)

    cases = [
        "",
        "   ",
        "one",
        "still reasoning about the thing",
        "one two three four five six seven eight nine ten",
        "alpha beta gamma " * 6 + "the final words",
        "word " * 500,
        "中" * 100 + " tail words here",
        "  \n \n  spaced    out \n\n  runs  " + "x " * 200,
        "a" * 1000,
    ]
    # When/Then: the windowed tail equals the full scan at every width where
    # a tail exists at all — the collapsed form fits the budget or the `…`
    # rule cuts it, identically
    for width in (30, 40, 60, 80, 120):
        for text in cases:
            row = ThinkingRow()
            row.begin_stream()
            row.append(text)
            assert row._thinking_tail(width) == reference(text, width), (width, text[:40])


# ─── the markdown answer ────────────────────────────────────────────────────


def test_the_answer_row_paints_the_answer_layer_and_not_the_body_one() -> None:
    row = AnswerRow("**bold** plain answer text")
    lines = row.build_lines(60, THEME)
    painted = plain_of(lines[0]).strip()
    assert "bold" in painted and "answer" in painted
    # `$text-answer` (12.33:1) — the brightest layer in the palette
    assert rgb(THEME.text_answer) in repr(lines[0][0][1])
    # …and a plain body row beside it keeps the inherited `$foreground`, so the
    # two layers are distinguishable in the transcript rather than merged
    body = RowStatic("plain static body")
    assert rgb(THEME.foreground) in repr(body.build_lines(60, THEME)[0][0][1])
    assert rgb(THEME.text_answer) not in repr(body.build_lines(60, THEME)[0][0][1])


def test_the_answer_row_renders_markdown_structure() -> None:
    row = AnswerRow("# Title\n\nbody **bold**")
    lines = rendered(row, 40).split("\n")
    # Rich lays markdown out at exactly the width it is given, so the heading is
    # centred and keeps its own line
    assert "Title" in lines[0]
    assert lines[0] != lines[2]


def test_the_answer_row_accepts_a_markdown_object() -> None:
    from rich.markdown import Markdown

    assert AnswerRow(Markdown("hi")).markup == "hi"
    assert AnswerRow("hi").markup == "hi"


# ─── styled-line helpers ────────────────────────────────────────────────────


def test_cells_are_measured_not_counted() -> None:
    # "日本語" is three characters and six cells
    assert cells_of([("日本語", pt.Style())]) == 6
    assert cell_len("日本語") == 6


def test_truncate_to_keeps_whole_characters() -> None:
    cut = truncate_to([("日本語", pt.Style())], 5)
    assert plain_of(cut) == "日本"


def test_pad_to_never_shortens_a_line() -> None:
    styled = pad_to([("abc", pt.Style())], 5, pt.Style())
    assert plain_of(styled) == "abc  "
    assert cells_of(styled) == 5
    assert plain_of(pad_to([("abcdef", pt.Style())], 3, pt.Style())) == "abcdef"


# ─── theme tints ────────────────────────────────────────────────────────────


def test_the_event_row_border_is_the_primary_40_percent_tint() -> None:
    # `#2fc2ff 40%` on `#0a0e14` is `#185672`, the value the Textual baseline shows
    row = EventRow("loop output")
    border = row.build_lines(WIDTH, THEME)[0][0]
    assert border[0] == "┃"
    assert rgb("#185672") in repr(border[1])


def test_the_error_row_border_is_the_error_60_percent_tint() -> None:
    row = EventRow("boom")
    row.add_class("-error")
    border = row.build_lines(WIDTH, THEME)[0][0]
    # The rule carries the theme background too (I6): an unstyled cell would fall
    # back to the TERMINAL's default and stripe the log a different colour.
    assert repr(border[1]) == repr(
        pt.Style()
        .fg(THEME.color(blend(THEME.error, THEME.background, 0.60)))
        .bg(THEME.color(THEME.background))
    )
    text = row.build_lines(WIDTH, THEME)[0][2]
    assert rgb(THEME.error) in repr(text[1])


def test_the_alarm_row_paints_warning_and_a_warning_border() -> None:
    row = RowStatic(Text("> alarm"))
    row._content = Text("> alarm")
    row.add_class("-alarm")
    lines = row.build_lines(WIDTH, THEME)
    assert lines[0][0][0] == "┃"
    assert rgb(blend(THEME.warning, THEME.background, 0.60)) in repr(lines[0][0][1])
    assert rgb(THEME.warning) in repr(lines[0][2][1])


def test_a_plain_row_has_no_border_at_all() -> None:
    row = RowStatic("user prompt")
    assert row._glyph_column == 0
    assert rendered(row) == "user prompt"


# ─── selection painting ─────────────────────────────────────────────────────


def lit_text(lines: list[list[tuple[str, pt.Style]]]) -> str:
    """The characters painted reverse-video — what the user sees highlighted."""

    return "".join(
        text for line in lines for text, style in line if "REVERSED" in repr(style)
    )


def test_selection_paints_reverse_video_over_the_selected_characters() -> None:
    # Content offsets, the space the app builds them in: content 0-4 is "hello",
    # and that is exactly the run the user sees lit — NOT the border (see
    # `test_a_drag_lights_the_content_characters_and_not_the_border`).
    row = EventRow("hello world")
    lines = row.build_lines(WIDTH, THEME, selection=Selection({0: (0, 5)}))
    assert lit_text(lines) == "hello"


def test_a_selection_running_to_the_end_stops_at_the_text() -> None:
    # end == -1 means "to the end"; the row's own trailing blanks are never selected
    row = EventRow("abc")
    lines = row.build_lines(WIDTH, THEME, selection=Selection({0: (0, -1)}))
    assert lit_text(lines).strip() == "abc"


def test_a_selection_tuple_is_also_understood() -> None:
    # The contract allows the app to hand a row the raw
    # `(start_line, start_col, end_line, end_col)` tuple instead of an object.
    row = EventRow("hello world")
    lines = row.build_lines(WIDTH, THEME, selection=(0, 0, 0, 5))
    assert lit_text(lines) == "hello"


def test_selectable_text_is_the_painted_text_rstripped() -> None:
    # The row's CONTENT, without the border + padding gutter: that is the text a
    # drag addresses, so the painted line and the copied line agree about which
    # character is which (`test_a_drag_over_the_expanded_body…` pins the same
    # space from the gesture side).
    row = ToolRow("Bash ls -la")
    row.build_lines(20, THEME)
    assert row.selectable_text() == "▶ Bash ls -la"
    assert not row.selectable_text().endswith(" ")


def test_a_multiline_row_joins_its_lines_with_newlines() -> None:
    row = ToolRow("Bash", detail="one\ntwo")
    row._toggle()
    row.build_lines(WIDTH, THEME)
    assert row.selectable_text() == "▼ Bash\none\ntwo"


# ─── node surface ───────────────────────────────────────────────────────────


def test_a_row_carries_the_node_surface_the_app_needs() -> None:
    row = ToolRow("Bash", id="row-1")
    assert row.id == "row-1"
    assert row.msg_ref is None
    assert row.display is True
    assert row.focused is False
    row.msg_ref = "msg-1"
    assert row.msg_ref == "msg-1"
    row.place(3, 7, 40, 2)
    assert (row.region.x, row.region.y, row.region.width) == (3, 7, 40)
    assert row.height(40, THEME) == 1
    row.focus()
    assert row.focused is True


def test_a_focused_collapsible_row_paints_the_primary_22_percent_band() -> None:
    # `ToolRow:focus { background: $primary 22% }`
    row = ToolRow("Bash ls -la")
    row.focus()
    lines = row.build_lines(WIDTH, THEME)
    tint = blend(THEME.primary, THEME.background, 0.22)
    assert tint == "#123547"
    assert rgb(tint) in repr(lines[0][2][1])


def test_a_row_wraps_to_the_width_it_is_given() -> None:
    row = EventRow("the quick brown fox jumps over the lazy dog")
    lines = rendered(row, 20).split("\n")
    assert len(lines) > 1
    for line in lines:
        assert cell_len(line) <= 20


def test_wide_characters_wrap_by_cell_width() -> None:
    row = EventRow("日本語のテキスト")
    lines = row.build_lines(10, THEME)
    # gutter is 2 cells, so the text gets 8 — four wide characters per line
    assert [plain_of(line).strip("┃ ") for line in lines] == ["日本語の", "テキスト"]


def test_the_theme_is_honoured_and_cached_per_theme() -> None:
    from miniouto.cli.tui.theme import get_theme

    row = EventRow("hi")
    first = row.build_lines(WIDTH, THEME)
    other = get_theme("textual-dark")
    assert other is not None
    second = row.build_lines(WIDTH, other)
    assert repr(first[0][0][1]) != repr(second[0][0][1])
    # Same inputs, same lines (and the cache is what makes it cheap)
    assert row.build_lines(WIDTH, THEME) is first


def test_a_drag_lights_the_content_characters_and_not_the_border() -> None:
    """Regression (M4): the highlight used to sit `gutter` cells too far left.

    Offsets reach a row in CONTENT space — the app subtracts the row's
    border+padding gutter from the mouse report, and ``get_selection`` reads the
    same space. Painting applied the span to the PAINTED line instead, so on
    every bordered row the reverse-video block started on the ``┃`` and its pad:
    the user copied ``ABCD`` and saw ``┃ AB`` lit.
    """

    row = EventRow("ABCDEFGHIJ")
    span = Selection({0: (0, 4)})
    painted = row.build_lines(WIDTH, THEME, selection=span)
    # What the drag copies and what it lights are the same four characters
    assert row.get_selection(span)[0] == "ABCD"
    assert lit_text(painted) == "ABCD"
    # The gutter stayed chrome: neither the rule nor its pad is ever reversed
    gutter = row._gutter_width
    assert gutter == 2
    assert "REVERSED" not in repr(painted[0][0][1])
    assert "REVERSED" not in repr(painted[0][1][1])


def test_an_unbordered_row_lights_the_same_characters_it_copies() -> None:
    # The shift is the row's OWN gutter, so a gutterless row is unaffected
    row = RowStatic("ABCDEFGHIJ")
    span = Selection({0: (0, 4)})
    assert lit_text(row.build_lines(WIDTH, THEME, selection=span)) == "ABCD"
