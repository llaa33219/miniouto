"""Render-helper tests: word wrap, Rich → pyratatui bridging, color math.

Two facts drive these assertions and are worth stating once:

* Widths are CELLS, not characters. ``Span("한글").width()`` answers ``2``
  while it paints four columns, so a wrap test that passes on ASCII proves
  nothing about CJK — hence the explicit wide-character cases.
* A tab or a newline inside a ``Span`` collapses to nothing when ratatui draws
  it, so the wrapper has to remove both before any style run reaches a Span.
"""

from __future__ import annotations

# The TUI paints with the shim, so its types are what these tests compare.
import pytest
from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text

import miniouto.cli.tui.pt as pt
from miniouto.cli.tui.render import (
    TAB_WIDTH,
    blend,
    hex_to_rgb,
    render_markdown,
    rgb_to_hex,
    rich_text_to_lines,
    segments_to_lines,
    style_from_rich,
    word_wrap,
    wrap_spans,
)


def plain(line: pt.Line) -> str:
    """Concatenate a rendered Line's spans back into plain text."""

    return "".join(span.content for span in line.spans)


def line_cells(line: pt.Line) -> int:
    return sum(cell_len(span.content) for span in line.spans)


# ─── word_wrap: shape ────────────────────────────────────────────────────────


def test_ascii_text_wraps_greedily_on_word_boundaries() -> None:
    assert word_wrap("the quick brown fox jumps over the lazy dog", 12) == [
        "the quick",
        "brown fox",
        "jumps over",
        "the lazy dog",
    ]


def test_words_are_never_split_when_they_fit_whole() -> None:
    wrapped = word_wrap("aa bb cc", 5)
    assert wrapped == ["aa bb", "cc"]


def test_text_shorter_than_the_width_is_a_single_line() -> None:
    assert word_wrap("short", 40) == ["short"]


def test_empty_string_is_one_empty_line() -> None:
    assert word_wrap("", 10) == [""]


def test_blank_lines_are_preserved() -> None:
    assert word_wrap("a\n\nb", 10) == ["a", "", "b"]


def test_explicit_newlines_start_new_visual_lines() -> None:
    assert word_wrap("one\ntwo\nthree", 40) == ["one", "two", "three"]


def test_trailing_spaces_are_stripped() -> None:
    # A wrapped line must never end in blanks: the transcript would look ragged
    # once a background color is painted behind the text.
    assert word_wrap("hello     ", 20) == ["hello"]
    assert word_wrap("hello   world", 5) == ["hello", "world"]


# ─── word_wrap: wide characters ──────────────────────────────────────────────


def test_cjk_wraps_by_cell_width_not_character_count() -> None:
    # "日本語のテキスト" is 8 characters but 16 cells: width 8 fits 4 characters.
    assert word_wrap("日本語のテキスト", 8) == ["日本語の", "テキスト"]


def test_a_wide_character_is_never_split_across_lines() -> None:
    # Width 3 fits one 2-cell character per line — the odd column stays empty
    # rather than cutting a glyph in half.
    assert word_wrap("漢漢漢", 3) == ["漢", "漢", "漢"]
    for line in word_wrap("漢漢漢漢", 5):
        assert cell_len(line) <= 5


def test_mixed_width_text_respects_the_cell_budget() -> None:
    wrapped = word_wrap("ab 日本語 cd", 8)
    assert all(cell_len(line) <= 8 for line in wrapped)
    assert "".join(wrapped).replace(" ", "") == "ab日本語cd"


def test_emoji_wraps_without_being_corrupted() -> None:
    assert word_wrap("👍👍", 2) == ["👍", "👍"]


# ─── word_wrap: long words, indentation, tabs ────────────────────────────────


def test_a_word_longer_than_the_width_breaks_at_the_cell_boundary() -> None:
    assert word_wrap("supercalifragilistic", 6) == ["superc", "alifra", "gilist", "ic"]


def test_long_word_after_short_words_starts_on_its_own_line() -> None:
    assert word_wrap("ab abcdefgh", 4) == ["ab", "abcd", "efgh"]


def test_leading_indentation_survives_the_wrap() -> None:
    # Continuation lines return to column 0 so an indented block keeps its
    # shape instead of drifting right on every line.
    assert word_wrap("    indented line that wraps", 12) == [
        "    indented",
        "line that",
        "wraps",
    ]


def test_indentation_wider_than_the_width_does_not_eat_content() -> None:
    assert word_wrap("        deep", 4) == ["deep"]


def test_tabs_expand_to_spaces() -> None:
    assert word_wrap("a\tb", 20) == ["a" + " " * TAB_WIDTH + "b"]
    # A tab must never survive into a Span: ratatui would drop it entirely.
    assert "\t" not in "".join(word_wrap("a\tb\tc", 40))


def test_a_degenerate_width_still_makes_progress() -> None:
    assert word_wrap("ab cd", 1) == ["a", "b", "c", "d"]
    assert word_wrap("ab", 0) == ["a", "b"]


# ─── wrap_spans ──────────────────────────────────────────────────────────────


def _styles(line: list[tuple[str, pt.Style]]) -> list[str]:
    return [repr(style) for _, style in line]


def test_wrap_spans_matches_word_wrap_exactly() -> None:
    text = "the quick brown fox jumps over the lazy dog"
    styled = wrap_spans([(text, pt.Style())], 12)
    assert ["".join(piece for piece, _ in line) for line in styled] == word_wrap(text, 12)


def test_wrap_spans_carries_styles_across_a_line_break() -> None:
    bold = pt.Style().bold()
    italic = pt.Style().italic()
    lines = wrap_spans([("hello world ", bold), ("tail end", italic)], 8)
    assert ["".join(piece for piece, _ in line) for line in lines] == [
        "hello",
        "world",
        "tail end",
    ]
    # The style followed the WORD across the break, it did not reset.
    assert _styles(lines[0]) == [repr(bold)]
    assert _styles(lines[1]) == [repr(bold)]
    assert _styles(lines[2]) == [repr(italic)]


def test_wrap_spans_splits_one_span_across_lines() -> None:
    red = pt.Style().fg(pt.Color.rgb(255, 0, 0))
    lines = wrap_spans([("aaa bbb", red)], 3)
    assert ["".join(piece for piece, _ in line) for line in lines] == ["aaa", "bbb"]
    for line in lines:
        assert all(style == red for _, style in line)


def test_wrap_spans_preserves_a_style_change_inside_a_line() -> None:
    bold = pt.Style().bold()
    plain_style = pt.Style()
    lines = wrap_spans([("ab", bold), ("cd", plain_style)], 20)
    assert lines == [[("ab", bold), ("cd", plain_style)]]


def test_wrap_spans_merges_neighbouring_runs_of_the_same_style() -> None:
    style = pt.Style().bold()
    lines = wrap_spans([("aa ", style), ("bb", style)], 20)
    # One span, not three: fewer spans to build per frame.
    assert lines == [[("aa bb", style)]]


def test_wrap_spans_expands_tabs_and_newlines_like_word_wrap() -> None:
    style = pt.Style()
    lines = wrap_spans([("a\tb\nc", style)], 40)
    assert ["".join(piece for piece, _ in line) for line in lines] == [
        "a" + " " * TAB_WIDTH + "b",
        "c",
    ]
    for line in lines:
        for piece, _ in line:
            assert "\t" not in piece and "\n" not in piece


# ─── style_from_rich ─────────────────────────────────────────────────────────


def test_style_from_rich_maps_colors_to_full_rgb() -> None:
    style = style_from_rich(RichStyle.parse("#010203 on #040506"))
    assert style.foreground == pt.Color.rgb(1, 2, 3)
    assert style.background == pt.Color.rgb(4, 5, 6)


def test_style_from_rich_expands_a_named_color_to_its_triplet() -> None:
    # 'red' is the ANSI palette red (128,0,0); the TUI paints truecolor, so the
    # terminal must never be asked to reinterpret the name.
    assert style_from_rich(RichStyle.parse("red")).foreground == pt.Color.rgb(128, 0, 0)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("bold", pt.Style().add_modifier(pt.Modifier.bold())),
        ("dim", pt.Style().add_modifier(pt.Modifier.dim())),
        ("italic", pt.Style().add_modifier(pt.Modifier.italic())),
        ("underline", pt.Style().add_modifier(pt.Modifier.underlined())),
        ("blink", pt.Style().add_modifier(pt.Modifier.slow_blink())),
        ("reverse", pt.Style().add_modifier(pt.Modifier.reversed())),
        ("strike", pt.Style().add_modifier(pt.Modifier.crossed_out())),
        ("conceal", pt.Style().add_modifier(pt.Modifier.hidden())),
    ],
)
def test_style_from_rich_maps_every_modifier(spec: str, expected: pt.Style) -> None:
    assert style_from_rich(RichStyle.parse(spec)) == expected


def test_style_from_rich_combines_modifiers() -> None:
    style = style_from_rich(RichStyle.parse("bold italic #ff0000"))
    assert style == pt.Style().fg(pt.Color.rgb(255, 0, 0)).add_modifier(
        pt.Modifier.bold() | pt.Modifier.italic()
    )


def test_style_from_rich_handles_none_and_default_colors() -> None:
    empty = style_from_rich(None)
    assert empty == pt.Style()
    assert empty.foreground is None and empty.background is None
    # A style with no color must inherit, not resolve to black.
    assert style_from_rich(RichStyle.parse("bold")).foreground is None


# ─── rich_text_to_lines ──────────────────────────────────────────────────────


def test_rich_text_runs_become_separate_spans() -> None:
    text = Text()
    text.append("plain ")
    text.append("bold", style="bold")
    lines = rich_text_to_lines(text)
    assert len(lines) == 1
    assert plain(lines[0]) == "plain bold"
    assert lines[0].spans[0].style == pt.Style()
    assert lines[0].spans[1].style == pt.Style().add_modifier(pt.Modifier.bold())


def test_rich_text_newlines_split_into_lines() -> None:
    text = Text()
    text.append("first\n")
    text.append("second")
    lines = rich_text_to_lines(text)
    assert [plain(line) for line in lines] == ["first", "second"]


def test_rich_text_base_style_sits_under_the_segment_style() -> None:
    base = pt.Style().fg(pt.Color.rgb(9, 9, 9))
    text = Text()
    text.append("inherits ")
    text.append("owns", style="#ff0000")
    spans = rich_text_to_lines(text, base)[0].spans
    # No explicit color on the segment: the base pencils through.
    assert spans[0].style.foreground == pt.Color.rgb(9, 9, 9)
    # An explicit color still wins over the base.
    assert spans[1].style.foreground == pt.Color.rgb(255, 0, 0)


def test_rich_text_keeps_a_text_level_base_style() -> None:
    # Rich short-circuits a span-less Text and emits unstyled segments, so the
    # Text's own style has to be re-applied explicitly — otherwise the row's
    # background vanishes.
    text = Text("x", style="on #00ff00 #0000ff")
    spans = rich_text_to_lines(text)[0].spans
    assert spans[0].style.background == pt.Color.rgb(0, 255, 0)
    assert spans[0].style.foreground == pt.Color.rgb(0, 0, 255)


def test_text_level_style_loses_to_a_span_style() -> None:
    text = Text("x", style="#0000ff")
    text.stylize("#ff0000")
    spans = rich_text_to_lines(text)[0].spans
    assert spans[0].style.foreground == pt.Color.rgb(255, 0, 0)


# ─── segments_to_lines ───────────────────────────────────────────────────────


def test_segments_split_on_newlines_inside_one_segment() -> None:
    lines = segments_to_lines([Segment("ab\ncd")])
    assert [plain(line) for line in lines] == ["ab", "cd"]


def test_segments_control_segments_are_dropped() -> None:
    segments = [
        Segment("ab"),
        Segment("zzz", None, control=("cursor_move", 1)),
        Segment("cd"),
    ]
    lines = segments_to_lines(segments)
    assert [plain(line) for line in lines] == ["abcd"]


def test_empty_segment_stream_yields_one_empty_line() -> None:
    assert [plain(line) for line in segments_to_lines([])] == [""]


def test_segments_apply_a_style_per_segment() -> None:
    segments = [Segment("a", RichStyle.parse("bold")), Segment("b")]
    spans = segments_to_lines(segments)[0].spans
    assert spans[0].style == pt.Style().add_modifier(pt.Modifier.bold())
    assert spans[1].style == pt.Style()


# ─── render_markdown ─────────────────────────────────────────────────────────


def _answer_style() -> pt.Style:
    return pt.Style().fg(pt.Color.rgb(0xC5, 0xD0, 0xD8))


def test_markdown_render_produces_the_heading_text() -> None:
    lines = render_markdown("# Title here\n\nbody text", 40, _answer_style())
    assert any("Title here" in plain(line) for line in lines)


def test_markdown_render_keeps_code_block_content() -> None:
    lines = render_markdown("```python\nx = 1\n```\n", 40, _answer_style())
    rendered = "\n".join(plain(line) for line in lines)
    assert "x = 1" in rendered


def test_markdown_render_keeps_bold_and_list_content() -> None:
    lines = render_markdown("some **bold** words\n\n- item one\n- item two", 40, _answer_style())
    rendered = "\n".join(plain(line) for line in lines)
    assert "bold" in rendered
    assert "item one" in rendered and "item two" in rendered
    # The bold run survived as a modifier, not just as text.
    assert any(
        span.style == _answer_style().add_modifier(pt.Modifier.bold())
        for line in lines
        for span in line.spans
    )


def test_markdown_render_patches_the_base_color_under_unstyled_text() -> None:
    # Markdown sets no foreground on body prose; without the base patch those
    # segments would paint in the terminal default and leave the palette.
    lines = render_markdown("just prose", 40, _answer_style())
    prose = [span for line in lines for span in line.spans if "prose" in span.content]
    assert prose, "expected the prose run"
    assert prose[0].style.foreground == pt.Color.rgb(0xC5, 0xD0, 0xD8)


def test_markdown_render_respects_the_requested_width() -> None:
    wrapped = render_markdown("word " * 40, 20, _answer_style())
    assert len(wrapped) > 1
    assert all(line_cells(line) <= 20 for line in wrapped)


def test_markdown_render_of_an_empty_document_still_returns_a_line() -> None:
    # pyratatui's Line has no __eq__, so compare the painted content.
    lines = render_markdown("", 40, _answer_style())
    assert [plain(line) for line in lines] == [""]


# ─── color math ──────────────────────────────────────────────────────────────


def test_blend_is_a_truncating_alpha_composite() -> None:
    # Textual's percentage colors mix with int() truncation, and the port has to
    # match it exactly or every blended surface shifts by a step.
    assert blend("#ffffff", "#000000", 0.5) == "#7f7f7f"
    assert blend("#2fc2ff", "#0a0e14", 0.12) == "#0e2330"
    assert blend("#a9bac5", "#0a0e14", 0.6) == "#69757e"


def test_blend_endpoints_return_the_pure_colors() -> None:
    assert blend("#123456", "#abcdef", 0.0) == "#abcdef"
    assert blend("#123456", "#abcdef", 1.0) == "#123456"


def test_blend_clamps_out_of_range_percentages() -> None:
    assert blend("#ffffff", "#000000", -1.0) == "#000000"
    assert blend("#ffffff", "#000000", 2.0) == "#ffffff"


def test_hex_and_rgb_round_trip() -> None:
    assert hex_to_rgb("#2fc2ff") == (47, 194, 255)
    assert rgb_to_hex(47, 194, 255) == "#2fc2ff"
    # A short form is expanded, as CSS allows.
    assert hex_to_rgb("#fff") == (255, 255, 255)


def test_rgb_to_hex_clamps_channels() -> None:
    assert rgb_to_hex(-5, 300, 128) == "#00ff80"


def test_hex_to_rgb_rejects_a_bad_value() -> None:
    with pytest.raises(ValueError):
        hex_to_rgb("#12345")
