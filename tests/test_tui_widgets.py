"""Widget tests: header, chips, the bottom panel, the editor and the scroller.

Ported from the Textual-era ``test_tui_navigation`` (chip reachability, whole
values, click routing), plus the pieces of the old app CSS that are pure geometry
(header centring, the input's band, the panel's four rows) and the ``_RowWindow``
budget from ``test_streaming``.

The assertions the old suite made about REGIONS are kept as region assertions:
a chip that renders but sits past the right edge is a chip nobody can click, which
is exactly how the style and session pickers once went missing. Geometry is
verified against the pty baseline in ``/tmp/tui-baseline``, which is the layout the
owner approved.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from rich.cells import cell_len

from miniouto.cli.tui.input import Key, Mouse
from miniouto.cli.tui.render import blend, hex_to_rgb
from miniouto.cli.tui.rows import EventRow, plain_of
from miniouto.cli.tui.theme import MINIOUTO_THEME, get_theme
from miniouto.cli.tui.widgets import (
    CHIP_MARGIN_RIGHT,
    EVICT_MARGIN_LINES,
    HELP_TEXT,
    INPUT_MAX_HEIGHT,
    INPUT_MIN_HEIGHT,
    JUMP_BUTTON_LABEL,
    TOP_LOAD_LINES,
    WINDOW_CHUNK_ROWS,
    WINDOW_INITIAL_ROWS,
    WINDOW_LOAD_CHUNKS_PER_FRAME,
    WINDOW_MAX_ROWS,
    BottomPanel,
    ChatInput,
    ChatScroll,
    Header,
    JumpButton,
    StatusChip,
    _RowWindow,
    at_bottom,
    wrap_offsets,
)

THEME = MINIOUTO_THEME

# Terminal widths the chip rows must survive: a cramped phone-width window, the
# common 80/100, the 150 cap, and terminals far wider than the cap.
CHIP_WIDTHS = (40, 80, 100, 120, 150, 200, 250)
# From 80 cells up both chips of a row fit whole, so nothing may be cut there.
WIDE_WIDTHS = (80, 100, 120, 150, 200, 250)

# Chip values as a live session produces them: a model plus its reasoning effort on
# the two left chips, a style name and a pinned session name on the two right ones.
CHIP_VALUES = {
    "outo": "gpt-5.5 high",
    "subagent": "claude-opus-4-5 low",
    "style": "coding-work",
    "session": "tui-20260101-120000-abc123",
}

BASELINE = Path("/tmp/tui-baseline/01-boot.txt")
"""The pty capture of the old Textual TUI, when the port's fixtures are around."""


def rgb(hex_value: str) -> str:
    """The ``Rgb(r, g, b)`` fragment a palette entry produces, for style asserts."""

    red, green, blue = hex_to_rgb(hex_value)
    return f"Rgb({red}, {green}, {blue})"


def lines_of(node: object, width: int, theme: object = THEME) -> list[str]:
    return [plain_of(line).rstrip() for line in node.build_lines(width, theme)]


def filled_panel() -> BottomPanel:
    panel = BottomPanel()
    for kind, value in CHIP_VALUES.items():
        panel.set_value(kind, value)
    return panel


# ─── the header ─────────────────────────────────────────────────────────────


def test_the_header_paints_the_panel_background_and_the_title_only() -> None:
    header = Header("~/miniouto", "miniouto v0.11.10")
    line = header.build_lines(120, THEME)[0]
    painted = plain_of(line)
    # `$panel` spans the whole row, so every run carries it
    panel = rgb(THEME.panel)
    assert all(
        panel in repr(style) for text, style in line if text.strip()
    ), "a header run lost the panel background"
    # No icon: the owner had the ⭘ removed, so the row carries no glyph at all.
    assert "\u2b58" not in painted
    assert painted.index("~/miniouto") == 40


def test_the_header_title_is_centred_in_the_cells_the_clock_space_leaves() -> None:
    # Only the 10-cell clock space is still docked (RIGHT), so the title centres
    # in what is left: (120 - 10 - 30)//2 == 40. The icon box is gone.
    header = Header("~/miniouto", "miniouto v0.11.10")
    painted = plain_of(header.build_lines(120, THEME)[0])
    assert painted.index(header.format_title()) == 40
    # ... and the same rule holds on a narrow terminal, without going negative
    narrow = plain_of(header.build_lines(20, THEME)[0])
    assert narrow.index("~/") == 0
    assert cell_len(narrow) == 20


def test_the_header_separator_and_subtitle_are_dim() -> None:
    header = Header("t", "s")
    runs = {text: style for text, style in header.build_lines(40, THEME)[0] if text.strip()}
    # Textual styles both with the `dim` ATTRIBUTE over the header's own
    # foreground, so the ink stays the body colour and only its intensity drops.
    assert "DIM" in repr(runs[" — "])
    assert "DIM" in repr(runs["s"])
    assert rgb(THEME.foreground) in repr(runs["s"])


def test_a_header_without_a_subtitle_renders_the_title_alone() -> None:
    header = Header("only")
    assert header.format_title() == "only"
    assert header.render().plain == "only"


def test_a_click_on_the_header_never_opens_the_palette() -> None:
    """The icon click handler went with the icon: the header is now pure title."""

    opened: list[bool] = []

    class App:
        def action_command_palette(self) -> None:
            opened.append(True)

    header = Header("t")
    app = App()
    for x in (0, 1, 7, 40):
        assert not header.handle_mouse(Mouse("down", x, 0), x, 0, app)
    assert opened == []


@pytest.mark.skipif(not BASELINE.exists(), reason="no pty baseline fixture")
def test_the_header_matches_the_captured_baseline_row_apart_from_the_icon() -> None:
    """The baseline row is Textual's; the only intended difference is the icon.

    The owner had the ``⭘`` removed and the title re-centred across the whole
    row, so the captured row must still match once the icon is blanked and the
    title is moved.
    """

    baseline = BASELINE.read_text(encoding="utf-8").split("\n")[0]
    header = Header("~/miniouto", "miniouto v0.11.10")
    painted = plain_of(header.build_lines(120, THEME)[0])
    # The icon is the only cell that disappeared, and the title moved left with
    # it (Textual centred between the icon box AND the clock space).
    assert "\u2b58" not in painted
    assert painted.strip() == baseline.replace("\u2b58", " ").strip().replace("  ", "  ")
    assert painted.index("~/miniouto") < baseline.index("~/miniouto")
    assert header.format_title() in painted


# ─── status chips ───────────────────────────────────────────────────────────


def test_a_chip_renders_its_label_and_value() -> None:
    chip = StatusChip("outo", "gpt-5.5 high")
    assert chip.render().plain == "outo: gpt-5.5 high"
    assert (chip.label, chip.value) == ("outo", "gpt-5.5 high")


def test_a_labelless_chip_renders_its_value_alone() -> None:
    # The style chip has no label: its value IS the chip
    chip = StatusChip("", "coding-work")
    assert chip.render().plain == "coding-work"


def test_an_unset_chip_renders_a_dash_rather_than_nothing() -> None:
    chip = StatusChip("outo", "")
    assert chip.render().plain == "outo: -"
    chip.set_value("m")
    assert chip.render().plain == "outo: m"


def test_the_chip_is_padded_and_as_wide_as_its_value() -> None:
    chip = StatusChip("outo", "gpt-5.5 high")
    painted = chip.build_lines(chip.width_cells, THEME)[0]
    assert plain_of(painted) == " outo: gpt-5.5 high "
    assert cell_len(plain_of(painted)) == chip.width_cells


def test_the_chip_resting_style_is_background_bold() -> None:
    chip = StatusChip("outo", "m", variant="accent")
    style = repr(chip.style(THEME))
    assert rgb(THEME.background) in style
    assert rgb(THEME.accent) in style
    assert "BOLD" in style


def test_each_variant_takes_its_own_text_colour() -> None:
    assert rgb(THEME.accent) in repr(StatusChip("o", "v", variant="accent").style(THEME))
    assert rgb(THEME.text_muted) in repr(
        StatusChip("o", "v", variant="muted").style(THEME)
    )
    # `$text` on `$boost` is the auto 87% ramp, NOT the body foreground
    default = repr(StatusChip("o", "v").style(THEME))
    assert rgb(THEME.foreground) not in default


def test_the_focused_chip_is_the_primary_22_percent_bar_reversed() -> None:
    chip = StatusChip("outo", "m")
    chip.focus()
    style = repr(chip.style(THEME))
    assert rgb("#123547") in style
    assert "REVERSED" in style and "BOLD" in style


def test_the_hovered_chip_is_the_primary_12_percent_bar_bold_underline() -> None:
    chip = StatusChip("outo", "m")
    chip.set_hover(True)
    style = repr(chip.style(THEME))
    assert rgb("#0e2330") in style
    assert "BOLD" in style and "UNDERLINED" in style


def test_a_click_on_a_chip_emits_chip_clicked() -> None:
    chip = StatusChip("outo", "m", id="chip-outo")
    chip.handle_mouse(Mouse("down", 0, 0), 0, 0)
    assert len(chip.messages) == 1
    assert isinstance(chip.messages[0], StatusChip.ChipClicked)
    assert chip.messages[0].chip is chip


def test_enter_on_a_chip_emits_chip_clicked() -> None:
    chip = StatusChip("style", "m", id="chip-style")
    assert chip.handle_key(Key("Enter")) is True
    assert isinstance(chip.messages[-1], StatusChip.ChipClicked)
    # Only Enter activates it
    assert chip.handle_key(Key("x")) is False


def test_a_chip_delivers_through_the_app_when_one_is_attached() -> None:
    delivered: list[object] = []

    class App:
        def post_message(self, message: object) -> None:
            delivered.append(message)

    chip = StatusChip("outo", "m")
    chip.handle_mouse(Mouse("down", 0, 0), 0, 0, App())
    assert [type(m).__name__ for m in delivered] == ["ChipClicked"]
    assert chip.messages == []


# ─── the bottom panel ───────────────────────────────────────────────────────


def test_the_panel_has_exactly_three_rows() -> None:
    panel = BottomPanel()
    assert len(panel.build_lines(120, THEME)) == 3
    assert panel.ROWS == ("spinner-row", "chip-row-a", "chip-row-b")
    for name in panel.ROWS:
        assert panel.row(name) is not None


def test_the_chips_sit_in_two_rows_anchored_left_and_right() -> None:
    panel = filled_panel()
    panel.build_lines(120, THEME)
    # Then: the rows are separate lines, so no row asks for four chips
    row_a = panel.row("chip-row-a").region
    row_b = panel.row("chip-row-b").region
    assert row_a.y != row_b.y
    for row, (left_kind, right_kind) in (
        (row_a, ("outo", "style")),
        (row_b, ("subagent", "session")),
    ):
        left = panel.chips[left_kind].region
        right = panel.chips[right_kind].region
        # And: each row hugs its left edge with one chip and its right edge with
        # the other (the 1-cell chip margin-right)
        assert left.x == row.x
        assert right.x + right.width == row.x + row.width - CHIP_MARGIN_RIGHT


@pytest.mark.parametrize("width", CHIP_WIDTHS)
def test_no_chip_is_pushed_off_screen_at_any_width(width: int) -> None:
    panel = filled_panel()
    panel.build_lines(width, THEME)
    for kind, chip in panel.chips.items():
        region = chip.region
        # A chip whose whole region sat past the right edge was how the style and
        # session pickers went missing
        assert region.width > 0, f"{kind} collapsed to nothing at {width} cols"
        on_screen = min(region.right, width) - max(region.x, 0)
        assert on_screen > 0, f"{kind} pushed off screen at {width} cols: {region}"


@pytest.mark.parametrize("width", CHIP_WIDTHS)
def test_no_chip_line_carries_an_ellipsis_at_any_width(width: int) -> None:
    panel = filled_panel()
    for index, row_id in enumerate(panel.ROWS):
        if row_id not in ("chip-row-a", "chip-row-b"):
            continue
        line = plain_of(panel.build_lines(width, THEME)[index])
        # The owner's explicit rejection of the one-row ellipsis fit
        assert "…" not in line, f"{row_id} elides at {width} cols: {line!r}"


@pytest.mark.parametrize("width", WIDE_WIDTHS)
def test_chip_values_render_whole_from_80_cells_up(width: int) -> None:
    panel = filled_panel()
    rows = panel.build_lines(width, THEME)
    for kind, chip in panel.chips.items():
        plain = chip.render().plain
        line = plain_of(rows[chip.region.y])
        assert chip.region.x >= 0, f"{kind} starts off screen"
        assert chip.region.right <= width, f"{kind} runs off screen: {chip.region}"
        assert chip.region.width >= cell_len(plain)
        assert plain in line, f"{kind} is not fully painted: {line!r}"


@pytest.mark.parametrize("kind", sorted(CHIP_VALUES))
def test_a_click_on_a_chip_reaches_its_own_handler(kind: str) -> None:
    panel = filled_panel()
    panel.build_lines(120, THEME)
    chip = panel.chips[kind]
    centre = chip.region.x + chip.region.width // 2
    assert panel.chip_at(centre, chip.region.y) is chip
    assert panel.handle_mouse(Mouse("down", centre, chip.region.y), centre, chip.region.y)
    assert isinstance(chip.messages[-1], StatusChip.ChipClicked)


def test_a_hover_lights_up_only_the_chip_under_the_pointer() -> None:
    panel = filled_panel()
    panel.build_lines(120, THEME)
    chip = panel.chips["style"]
    panel.handle_mouse(Mouse("move", chip.region.x + 1, chip.region.y), chip.region.x + 1, chip.region.y)
    assert chip.hover is True
    assert all(
        not other.hover for name, other in panel.chips.items() if name != "style"
    )


def test_set_session_publishes_the_name_and_falls_back_to_a_dash() -> None:
    panel = BottomPanel()
    panel.set_session("tui-1")
    assert panel.get_chip("session").render().plain == "session: tui-1"
    panel.set_session("")
    assert panel.get_chip("session").render().plain == "session: -"


def test_an_unknown_chip_kind_is_ignored_rather_than_raising() -> None:
    panel = BottomPanel()
    panel.set_value("nope", "x")
    assert panel.get_chip("nope") is None


def test_the_spinner_row_carries_the_glyph_and_the_activity() -> None:
    panel = BottomPanel()
    panel.render_spinner("⠋", "Working…")
    line = plain_of(panel.build_lines(120, THEME)[0]).rstrip()
    assert line == " ⠋ Working…"
    style = panel.build_lines(120, THEME)[0][1][1]
    assert rgb(THEME.accent) in repr(style)
    assert "BOLD" in repr(style)


def test_a_notice_has_no_glyph_and_the_row_blanks_when_idle() -> None:
    # A frozen spinner next to a finished action reads as "still working"
    panel = BottomPanel()
    panel.render_spinner("", "queued for the running turn")
    assert plain_of(panel.build_lines(120, THEME)[0]).rstrip() == " queued for the running turn"
    panel.render_spinner("", "")
    assert plain_of(panel.build_lines(120, THEME)[0]).strip() == ""


def test_the_help_hint_is_retired_from_the_panel() -> None:
    # Round 4 removed the row: the Keys popup lists the bindings now. The
    # constant keeps the owner's exact glyphs (the package re-exports it),
    # but NO painted panel line carries them — there is no fourth row.
    panel = BottomPanel()
    lines = panel.build_lines(120, THEME)
    assert len(lines) == 3
    assert HELP_TEXT.startswith("\u2039Tab\u203a cycle chips")
    assert HELP_TEXT.endswith("\u2039Ctrl+C\u203a quit")
    for line in lines:
        assert "\u2039Tab\u203a cycle chips" not in plain_of(line)


@pytest.mark.skipif(not BASELINE.exists(), reason="no pty baseline fixture")
def test_the_panel_matches_the_captured_baseline_rows() -> None:
    baseline = BASELINE.read_text(encoding="utf-8").split("\n")
    panel = BottomPanel()
    panel.set_value("outo", "- -")
    panel.set_value("style", "default")
    panel.set_value("subagent", "same as outo")
    panel.set_session("")
    for index, painted in enumerate(lines_of(panel, 120)):
        assert painted == baseline[27 + index], f"panel row {index} drifted"
    # The help row is the last baseline row before Textual's (unported) Footer
    assert HELP_TEXT in baseline[30]


def test_focus_chip_focuses_exactly_one() -> None:
    panel = BottomPanel()
    panel.focus_chip("session")
    assert panel.chips["session"].focused is True
    assert sum(1 for chip in panel.chips.values() if chip.focused) == 1
    panel.focus_chip("outo")
    assert panel.chips["outo"].focused is True
    assert panel.chips["session"].focused is False


def test_enter_on_a_focused_chip_opens_it() -> None:
    panel = BottomPanel()
    panel.focus_chip("outo")
    assert panel.handle_key(Key("Enter")) is True
    assert isinstance(panel.chips["outo"].messages[-1], StatusChip.ChipClicked)
    # With nothing focused, Enter is not the panel's
    panel.focus_chip("nope")
    assert panel.handle_key(Key("Enter")) is False


# ─── the chat input ─────────────────────────────────────────────────────────


def test_an_empty_input_is_five_rows_of_surface_band() -> None:
    editor = ChatInput()
    rows = editor.build_lines(120, THEME)
    assert len(rows) == INPUT_MIN_HEIGHT == 5
    # `margin: 0 2` then the thick primary rule, exactly as captured
    assert all(plain_of(row).startswith("  █") for row in rows)
    surface = rgb(THEME.surface)
    assert surface in repr(rows[0][2][1])
    assert rgb(THEME.background) in repr(rows[0][0][1])
    assert rgb(THEME.primary) in repr(rows[0][1][1])


def test_the_input_height_grows_with_the_text_and_caps_at_twelve() -> None:
    editor = ChatInput()
    assert editor.height(120) == INPUT_MIN_HEIGHT
    editor.insert("a\nb\nc")
    assert editor.height(120) == INPUT_MIN_HEIGHT
    editor.insert("\n" * 20)
    assert editor.height(120) == INPUT_MAX_HEIGHT == 12


def test_enter_submits_a_non_empty_prompt() -> None:
    editor = ChatInput()
    editor.insert("hello")
    assert editor.handle_key(Key("Enter")) is True
    assert [type(m).__name__ for m in editor.messages] == ["Submitted"]
    assert editor.messages[0].value == "hello"


def test_enter_refuses_a_whitespace_only_prompt() -> None:
    # The old `action_submit` did nothing when `text.strip()` was empty
    editor = ChatInput()
    editor.insert("   \n  ")
    assert editor.handle_key(Key("Enter")) is True
    assert editor.messages == []


def test_shift_and_alt_enter_insert_a_newline() -> None:
    for modifier in ("shift", "alt"):
        editor = ChatInput()
        editor.insert("a")
        key = Key("Enter", shift=True) if modifier == "shift" else Key("Enter", alt=True)
        assert editor.handle_key(key) is True
        assert editor.text == "a\n", modifier
        assert editor.messages == []


def _caret_blocks(editor: ChatInput, width: int) -> list[tuple[int, int]]:
    """Every reversed cell in the painted band, as band-relative ``(x, y)``."""

    blocks: list[tuple[int, int]] = []
    for y, line in enumerate(editor.build_lines(width, THEME)):
        x = 0
        for text, style in line:
            if "REVERSED" in repr(style) and text:
                blocks.append((x, y))
            x += cell_len(text)
    return blocks


def test_the_block_cursor_is_a_reversed_span_over_the_caret() -> None:
    editor = ChatInput()
    rows = editor.build_lines(60, THEME)
    # An EMPTY input still shows the cursor, which is the only thing it shows
    assert any(
        "REVERSED" in repr(style) for _, style in rows[1]
    ), "an empty input painted no block cursor"


def test_the_caret_block_sits_on_the_cell_the_hardware_cursor_reports() -> None:
    """The block and the terminal caret are the SAME cell, at every position.

    Regression: a soft wrap left the caret index at the end of one display row
    AND the start of the next, so `_paint_row` painted TWO blocks a whole row
    apart — a second cursor nowhere near where typing went.
    """

    for width in (30, 40, 60):
        for text in ("", "hello", "x" * 200, "aaa bbb ccc ddd eee fff ggg hhh"):
            editor = ChatInput()
            editor.insert(text)
            editor.set_width(width)
            for position in range(len(text) + 1):
                editor.cursor = position
                blocks = _caret_blocks(editor, width)
                assert len(blocks) == 1, (
                    f"{len(blocks)} blocks at width={width} position={position}"
                )
                assert blocks[0] == editor.cursor_cell(width), (
                    f"block {blocks[0]} != caret {editor.cursor_cell(width)} "
                    f"at width={width} position={position}"
                )


def test_a_caret_on_a_soft_wrap_boundary_paints_on_the_next_row_only() -> None:
    """The wrapped row's END is the next row's START: exactly one gets the block."""

    editor = ChatInput()
    editor.insert("x" * 60)
    editor.set_width(30)
    rows = editor.display_rows()
    assert len(rows) > 1
    boundary = rows[0][1]  # the character index at the end of display row 0
    editor.cursor = boundary
    blocks = _caret_blocks(editor, 30)
    assert len(blocks) == 1, blocks
    # It lands on the row AFTER the break, which is where the next character is.
    assert blocks[0] == editor.cursor_cell(30)
    assert blocks[0][1] == 1 + 1  # padding + the second display row


def test_the_caret_moves_and_the_text_edits() -> None:
    editor = ChatInput()
    editor.insert("hello world")
    editor.cursor = len("hello")
    editor.insert(",")
    assert editor.text == "hello, world"
    editor.delete_before()
    assert editor.text == "hello world"
    editor.move_home()
    assert editor.cursor == 0
    editor.delete_at()
    assert editor.text == "ello world"
    editor.move_end()
    assert editor.cursor == len(editor.text)


def test_word_operations_respect_word_boundaries() -> None:
    editor = ChatInput()
    editor.insert("one two three")
    editor.move_word(-1)
    assert editor.text[editor.cursor :] == "three"
    editor.delete_word_before()
    assert editor.text == "one three"
    editor.move_home()
    editor.delete_word_at()
    assert editor.text == " three"


def test_vertical_movement_follows_display_rows_not_paragraphs() -> None:
    # A soft-wrapped long paragraph is what the user sees, so an up-arrow has to
    # move a display row, not a whole logical line.
    editor = ChatInput()
    editor.insert("aaaa bbbb cccc dddd eeee ffff")
    editor.set_width(30)
    rows = editor.display_rows()
    assert len(rows) > 1
    editor.cursor = len(editor.text)
    editor.move_vertical(-1)
    assert editor.display_rows()[editor._caret_row(editor.display_rows())][0] <= editor.cursor
    assert editor.cursor != len(editor.text)


def test_home_and_end_stay_on_the_logical_line() -> None:
    editor = ChatInput()
    editor.insert("first\nsecond")
    editor.move_home()
    assert editor.cursor == len("first\n")
    editor.move_end()
    assert editor.cursor == len("first\nsecond")
    editor.move_home()
    editor.move(-3)
    editor.move_home()
    assert editor.cursor == 0


def test_tab_is_left_to_the_app_for_focus_cycling() -> None:
    editor = ChatInput()
    editor.insert("a")
    assert editor.handle_key(Key("Tab")) is False
    assert editor.text == "a"


def test_a_paste_inserts_whole_and_expands_tabs() -> None:
    editor = ChatInput()
    editor.handle_paste("a\tb\nc")
    assert editor.text == "a    b\nc"
    assert editor.cursor == len(editor.text)


def test_typing_is_consumed_and_named_keys_are_not() -> None:
    editor = ChatInput()
    assert editor.handle_key(Key("x")) is True
    assert editor.text == "x"
    assert editor.handle_key(Key("F5")) is False
    assert editor.handle_key(Key("l", ctrl=True)) is False


def test_soft_wrap_breaks_on_words_and_never_splits_a_wide_character() -> None:
    assert wrap_offsets("the quick brown fox", 8) == [(0, 3), (4, 9), (10, 15), (16, 19)]
    # "日本語のテキスト" is 8 characters but 16 cells: width 8 fits four characters
    assert wrap_offsets("日本語のテキスト", 8) == [(0, 4), (4, 8)]
    for start, end in wrap_offsets("ab 日本語 cd", 6):
        assert cell_len("ab 日本語 cd"[start:end]) <= 6


def test_a_glyph_wider_than_the_row_advances_instead_of_spinning() -> None:
    """A two-cell glyph in a one-cell band must advance, not re-test its index.

    Reachable from ``ChatInput`` when the terminal is narrower than the input
    chrome (~<9 columns): ``_wrap_range`` used to append the empty row
    ``(cursor, index)`` and set ``index = cursor``, so the same index was tested
    forever. Run in a SUBPROCESS with a hard timeout so a regression fails the
    test instead of hanging the suite.
    """

    code = (
        "import json; from miniouto.cli.tui.widgets import wrap_offsets; "
        "print(json.dumps([wrap_offsets(text, width) "
        "for text, width in [('a日', 1), ('日', 1)]]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30, check=True
    )
    narrow, single = [[tuple(row) for row in rows] for rows in json.loads(result.stdout)]
    # Both characters land on a row: the union of the ranges covers [0, 2).
    assert {index for start, end in narrow for index in range(start, end)} == {0, 1}
    assert narrow[:2] == [(0, 1), (1, 2)]
    assert narrow[-1] == (2, 2)  # the caret's empty row at the end of the text
    assert single == [(0, 1), (1, 1)]


def test_a_band_wide_enough_for_ordinary_text_wraps_as_before() -> None:
    assert wrap_offsets("hello world", 5) == [(0, 5), (5, 10), (10, 11)]
    assert wrap_offsets("abcdef", 3) == [(0, 3), (3, 6)]


def test_the_caret_cell_is_reported_for_the_hardware_cursor() -> None:
    editor = ChatInput()
    editor.build_lines(120, THEME)
    # margin (2) + rule (1) + padding (1) at the top padding row (1)
    assert editor.cursor_cell(120) == (4, 1)
    editor.insert("abcd")
    assert editor.cursor_cell(120) == (8, 1)


def test_typing_past_the_visible_band_scrolls_the_editor() -> None:
    editor = ChatInput()
    editor.insert("\n".join(f"line {index}" for index in range(20)))
    rows = editor.build_lines(60, THEME)
    assert len(rows) == INPUT_MAX_HEIGHT
    # The caret's line is on screen even though the text is far taller
    assert "line 19" in "\n".join(plain_of(row) for row in rows)


def test_a_click_inside_the_band_places_the_caret() -> None:
    editor = ChatInput()
    editor.insert("hello")
    editor.build_lines(60, THEME)
    editor.place(0, 10, 60, 5)
    editor.handle_mouse(Mouse("down", 6, 11), 6, 11)
    # x 6 - margin 2 - rule 1 - padding 1 == column 2
    assert editor.cursor == 2


# ─── scrolling ──────────────────────────────────────────────────────────────


def _log(rows: int = 40, height: int = 10) -> ChatScroll:
    log = ChatScroll()
    log.theme = THEME
    for index in range(rows):
        log.mount(EventRow(f"row {index}"))
    log.resize(80, height)
    log.anchor()
    return log


def test_at_bottom_is_true_within_the_tolerance_band() -> None:
    class Fake:
        def __init__(self, scroll_y: float, max_scroll_y: float) -> None:
            self.scroll_y = scroll_y
            self.max_scroll_y = max_scroll_y

    assert at_bottom(Fake(98, 100)) is True
    assert at_bottom(Fake(100, 100)) is True
    assert at_bottom(Fake(40, 100)) is False
    assert at_bottom(Fake(0, 0)) is True
    assert at_bottom(None) is True


def test_a_short_log_cannot_scroll() -> None:
    log = ChatScroll()
    log.mount(EventRow("one"))
    log.resize(80, 10)
    assert log.max_scroll_y == 0
    assert at_bottom(log) is True


def test_anchor_follows_the_tail_as_rows_arrive() -> None:
    log = _log(rows=20, height=10)
    assert log.scroll_y == log.max_scroll_y
    log.mount(EventRow("tail"))
    log.check_anchor()
    assert log.scroll_y == log.max_scroll_y


def test_scrolling_up_releases_the_anchor_and_paging_down_restores_it() -> None:
    log = _log()
    log.scroll_page_up()
    assert log.scroll_y < log.max_scroll_y
    assert at_bottom(log) is False
    log.scroll_page_down()
    assert log.scroll_y == log.max_scroll_y
    assert at_bottom(log) is True


def test_home_and_end_land_instantly() -> None:
    log = _log()
    log.scroll_home()
    assert log.scroll_y == 0
    log.scroll_end()
    assert log.scroll_y == log.max_scroll_y


def test_every_scroll_action_accepts_animate_false() -> None:
    # Every ported call site spells `animate=False`; the keyword must be accepted
    log = _log()
    log.scroll_page_up(animate=False)
    log.scroll_page_down(animate=False)
    log.scroll_home(animate=False)
    log.scroll_end(animate=False)
    log.scroll_to(y=5, animate=False)
    assert log.scroll_y == 5


def test_the_scroll_position_is_reported_to_the_app() -> None:
    seen: list[float] = []
    log = _log()
    log.on_scroll = seen.append
    log.scroll_home()
    assert seen == [0]


def test_the_wheel_scrolls_by_whole_lines() -> None:
    log = _log()
    log.scroll_home()
    log.scroll_by(3)
    assert log.scroll_y == 3


def test_remove_children_resets_the_log() -> None:
    log = _log(rows=3, height=10)
    log.remove_children()
    assert log.rows == []
    assert log.scroll_y == 0


# ─── the jump button ────────────────────────────────────────────────────────


def test_the_jump_button_is_hidden_at_the_bottom_and_shown_away_from_it() -> None:
    log = _log()
    button = JumpButton(log)
    assert button.sync() is False
    log.scroll_home()
    assert button.sync() is True
    assert button.render().plain == JUMP_BUTTON_LABEL


def test_clicking_the_jump_button_re_anchors_and_returns_focus_to_the_input() -> None:
    log = _log()
    log.scroll_home()
    editor = ChatInput()

    class App:
        _input = editor

    button = JumpButton(log)
    button.app = App()
    button.handle_mouse(Mouse("down", 0, 0), 0, 0)
    assert log.scroll_y == log.max_scroll_y
    assert button.display is False
    # Hiding a focused button would leave the app with no focused widget at all
    assert editor.focused is True


def test_the_jump_button_is_a_panel_bg_muted_label() -> None:
    button = JumpButton(None)
    style = button.build_lines(button.width_cells, THEME)[0][1][1]
    assert rgb(THEME.panel) in repr(style)
    assert rgb(THEME.text_muted) in repr(style)


# ─── windowing ──────────────────────────────────────────────────────────────


def test_the_window_budget_matches_the_old_numbers() -> None:
    assert (WINDOW_INITIAL_ROWS, WINDOW_CHUNK_ROWS, WINDOW_MAX_ROWS) == (100, 50, 300)
    assert (TOP_LOAD_LINES, EVICT_MARGIN_LINES) == (10, 60)


def test_a_bulk_fill_mounts_only_the_trailing_window() -> None:
    log = ChatScroll()
    log.theme = THEME
    log.resize(80, 10)
    window = _RowWindow()
    for index in range(WINDOW_INITIAL_ROWS + 50):
        window.append(log, EventRow(f"row {index}"), defer_mount=True)
    assert log.rows == []
    window.mount_tail(log)
    assert len(log.rows) == WINDOW_INITIAL_ROWS
    assert window.first == 50


def test_scrolling_to_the_top_loads_a_batch_of_older_rows() -> None:
    log = ChatScroll()
    log.theme = THEME
    log.resize(80, 10)
    window = _RowWindow()
    for index in range(200):
        window.append(log, EventRow(f"row {index}"), defer_mount=True)
    window.mount_tail(log)
    window.maybe_load_older(log, TOP_LOAD_LINES)
    # One trigger loads a bounded BATCH (a single chunk left a Home-pinned
    # view stuck: no scroll CHANGE ever fired the next load), capped so a
    # trigger never mounts the whole history at once.
    assert window.first == 0
    assert len(log.rows) == 200
    # Above the threshold nothing loads
    window.first = 50
    window.maybe_load_older(log, TOP_LOAD_LINES + 1)
    assert window.first == 50


def test_the_window_evicts_rows_far_above_the_viewport() -> None:
    log = ChatScroll()
    log.theme = THEME
    log.resize(80, 10)
    log.anchor()
    window = _RowWindow()
    for index in range(WINDOW_MAX_ROWS + 120):
        window.append(log, EventRow(f"row {index}"))
    assert window.first > 0, "nothing was evicted from the top"
    assert len(window.rows) - window.first <= WINDOW_MAX_ROWS


def test_one_top_edge_trigger_loads_a_bounded_batch() -> None:
    """Regression: a single-chunk load left a Home-pinned view stalled.

    ``_set_scroll`` only reports CHANGES and a top-edge load keeps the view at
    ``scroll_y == 0``, so nothing fired the next load — the true top of the
    session was unreachable no matter how many times the user hit Home. One
    trigger now loads a capped BATCH, so the top is reached in a few triggers
    without ever mounting the whole history in one frame.
    """

    log = ChatScroll()
    log.theme = THEME
    log.resize(80, 10)
    window = _RowWindow()
    for index in range(WINDOW_INITIAL_ROWS + 5 * WINDOW_CHUNK_ROWS):
        window.append(log, EventRow(f"row {index}"), defer_mount=True)
    window.mount_tail(log)
    mounted = len(log.rows)
    window.maybe_load_older(log, 0)
    loaded = mounted + WINDOW_CHUNK_ROWS * WINDOW_LOAD_CHUNKS_PER_FRAME
    assert len(log.rows) == loaded
    # The batch is capped: 5 chunks remained, but only the batch loaded.
    assert window.first == 3 * WINDOW_CHUNK_ROWS
    # Repeating the trigger walks the rest of the way to the true top.
    for _ in range(WINDOW_LOAD_CHUNKS_PER_FRAME + 1):
        window.maybe_load_older(log, 0)
    assert window.first == 0
    assert len(log.rows) == len(window.rows)


def test_evicted_rows_reload_when_the_view_returns_to_the_top() -> None:
    """Regression: after an evict, scrolling back up must remount the rows."""

    log = ChatScroll()
    log.theme = THEME
    log.resize(80, 10)
    log.anchor()
    window = _RowWindow()
    for index in range(WINDOW_MAX_ROWS + 100):
        window.append(log, EventRow(f"row {index}"))
    assert window.first > 0, "nothing was evicted: the test is vacuous"
    log.scroll_home(animate=False)
    window.maybe_load_older(log, log.scroll_y)
    for _ in range(10):
        if window.first == 0:
            break
        window.maybe_load_older(log, log.scroll_y)
    assert window.first == 0
    assert len(log.rows) == len(window.rows)
    # And the first row of the session is the first mounted row again.
    assert log.rows[0] is window.rows[0]


def test_removing_a_row_keeps_first_aligned() -> None:
    log = ChatScroll()
    log.theme = THEME
    log.resize(80, 10)
    window = _RowWindow()
    rows = [EventRow(f"row {index}") for index in range(4)]
    for row in rows:
        window.append(log, row)
    window.first = 2
    window.remove(rows[0])
    assert window.first == 1
    assert len(window.rows) == 3
    # Removing the LAST row of a fully-evicted window clamps rather than passing it
    window.first = len(window.rows)
    window.remove(window.rows[-1])
    assert window.first == len(window.rows)


def test_reset_drops_every_row() -> None:
    log = ChatScroll()
    log.theme = THEME
    window = _RowWindow()
    window.append(log, EventRow("a"))
    window.reset()
    assert (window.rows, window.first) == ([], 0)


# ─── theme plumbing ─────────────────────────────────────────────────────────


def test_widgets_repaint_for_whatever_theme_they_are_given() -> None:
    chip = StatusChip("outo", "m", variant="accent")
    miniouto = chip.build_lines(chip.width_cells, THEME)[0][0][1]
    other = get_theme("textual-dark")
    assert other is not None
    assert repr(miniouto) != repr(chip.build_lines(chip.width_cells, other)[0][0][1])


def test_the_auto_text_ramp_flips_to_black_on_a_light_surface() -> None:
    from miniouto.cli.tui.widgets import auto_text

    light = get_theme("solarized-light")
    assert light is not None
    dark_ink = auto_text(light, light.boost)
    light_ink = auto_text(THEME, THEME.boost)
    # On a light surface the ink steps toward black, on a dark one toward white;
    # the ladder only has to be ORDERED, so compare the channel sums.
    assert sum(hex_to_rgb(dark_ink)) < sum(hex_to_rgb(light_ink))
    # `auto 87%` over the focused bar is the e0e4e7 the Textual capture shows
    assert auto_text(THEME, blend(THEME.primary, THEME.background, 0.22)) == "#e0e4e7"


def test_a_padded_line_never_grows_past_the_width_it_was_given() -> None:
    panel = BottomPanel()
    for width in CHIP_WIDTHS:
        for line in panel.build_lines(width, THEME):
            assert cell_len(plain_of(line)) <= width


# ─── the caret never lands mid-glyph (regression m12) ───────────────────────


def test_the_caret_is_never_placed_mid_glyph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (m12): the caret clamped to ``width - 1`` mid-glyph.

    ``cursor_cell`` used ``min(x, width - 1)``, which ignores that a 2-cell CJK
    glyph spans TWO columns: once the clamp fires, the hardware caret can land on
    the glyph's SECOND half — half a character away from the reversed block that
    marks the insertion point. The band is forced width-wise here (the wrap engine
    normally keeps the caret inside it), because that is the only way to reach the
    clamp without a terminal narrower than the editor's own chrome.
    """

    from miniouto.cli.tui import widgets as widgets_module
    from miniouto.cli.tui.widgets import INPUT_MARGIN_X, INPUT_PADDING_X

    lead = INPUT_MARGIN_X + 1 + INPUT_PADDING_X
    editor = ChatInput()
    editor.insert("日日")
    editor.set_width(8)  # room = width - 1 - lead = 3 cells, caret wants 4
    monkeypatch.setattr(widgets_module, "wrap_offsets", lambda text, width: [(0, 2)])
    cell = editor.cursor_cell(8)
    assert cell is not None
    # Then: 2 content cells (ONE whole glyph). `min(lead + 4, 7)` = 7 would put
    # the caret on the second half of the second glyph.
    assert cell[0] == lead + 2
    assert cell[0] != 7


def test_the_caret_is_pulled_back_to_a_character_boundary() -> None:
    """The clamp helper itself: a wide glyph straddling the edge is not split."""

    from miniouto.cli.tui.widgets import _clamped_caret_column

    # 2 cells of room for a 2-cell glyph: no clamp fires
    assert _clamped_caret_column("日", 2, 2) == 2
    # 1 cell of room for a 2-cell glyph: pulled back to the boundary BEFORE it
    assert _clamped_caret_column("日", 2, 1) == 0
    # ASCII is unaffected — every column is a boundary
    assert _clamped_caret_column("ab", 2, 1) == 1
    # A mixed row keeps the widest whole prefix that fits
    assert _clamped_caret_column("a日日", 5, 4) == 3


# ─── round 4: no help row · resting chips on the background ─────────────────


def test_the_panel_has_exactly_three_rows_and_no_help_row() -> None:
    panel = BottomPanel()
    assert panel.ROWS == ("spinner-row", "chip-row-a", "chip-row-b")
    lines = panel.build_lines(120, THEME)
    assert len(lines) == 3
    for name in panel.ROWS:
        assert panel.row(name) is not None
    # And: the retired hint is painted nowhere on the panel
    for line in lines:
        assert HELP_TEXT not in plain_of(line)


def test_the_chip_resting_style_is_the_app_background_bold() -> None:
    """The owner's round-4 call: at rest a chip is the row's own colour."""
    chip = StatusChip("outo", "m", variant="accent")
    style = repr(chip.style(THEME))
    assert rgb(THEME.background) in style
    assert rgb(THEME.accent) in style
    assert "BOLD" in style
    # And: the old boost box is gone from the RESTING style
    assert rgb(THEME.boost) not in style


def test_each_variant_takes_its_own_text_colour_on_the_background() -> None:
    assert rgb(THEME.accent) in repr(StatusChip("o", "v", variant="accent").style(THEME))
    assert rgb(THEME.text_muted) in repr(
        StatusChip("o", "v", variant="muted").style(THEME)
    )
    # `$text` on `$background` is the auto 87% ramp, NOT the body foreground
    default = repr(StatusChip("o", "v").style(THEME))
    assert rgb(THEME.foreground) not in default


def test_the_header_fills_the_whole_row_with_the_panel_background() -> None:
    """Round 4: the docked clock space is `$panel` too, not the page background."""
    header = Header("~/miniouto", "miniouto v0.11.10")
    line = header.build_lines(120, THEME)[0]
    panel = rgb(THEME.panel)
    background = rgb(THEME.background)
    # EVERY run carries the panel background — the blank tail included
    assert all(panel in repr(style) for text, style in line if text.strip())
    # And: the composed line is exactly the screen width (no background fill-in)
    assert cell_len(plain_of(line)) == 120
    # And: the page background appears nowhere on the row
    assert background not in repr(line)
