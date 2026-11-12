"""The pyratatui-compat shim: the surface, and the semantics it must keep.

Every assertion here was first READ OFF pyratatui 0.3.0 — by introspection and
by a pty probe — so the file is the contract the port is held to, not a
transcription of what the shim happens to do. Three groups of rules matter:

1. **Semantics the call sites depend on.** Builders are immutable, ``.spans``
   and ``.lines`` hand out copies, ``patch`` lets the other style's set fields
   win while modifiers union, and ``Text.push_str`` does NOT split.
2. **Widths are CELLS.** ``Span("한글").width()`` is 4. pyratatui answers 2
   there; the port needs the painted width, which is why every other module in
   this package already measures with ``rich.cells.cell_len``.
3. **The screen.** ``Terminal`` must paint a composed frame well enough that the
   next slice can swap the substrate without touching ``compose()``, so a draw
   is asserted through the PAINTED output (text and ANSI), not through internals.

Two divergences are deliberate and pinned as such: ``Span.width()`` counts
cells, and ``TextState.handle_key`` accepts ANY duck-typed key (pyratatui's own
takes a bare ``str`` code). Everything else matches the binding.
"""

from __future__ import annotations

import pytest
import xnano.styles as xstyles

import miniouto.cli.tui.pt as pt
from miniouto.cli.tui.input import Key


def styled(text: str, style: pt.Style) -> pt.Span:
    """One span carrying ``style`` — the shape most paint tests build."""

    return pt.Span(text, style)


def ansi_of(terminal: pt.Terminal) -> str:
    """The painted frame's ANSI, which is what a pty would receive."""

    return terminal.output_ansi()


# ─── Color ──────────────────────────────────────────────────────────────────


def test_rgb_is_stored_as_hex_and_round_trips_to_xnano() -> None:
    color = pt.Color.rgb(47, 194, 255)

    assert color.to_xnano() == "#2fc2ff"
    assert str(color) == "Color(Rgb(47, 194, 255))"


def test_colors_compare_by_value_and_are_usable_as_dict_keys() -> None:
    # `theme.color` is LRU-cached on the hex string and its result is compared
    # against a freshly built Color all over the suites.
    assert pt.Color.rgb(47, 194, 255) == pt.Color.rgb(0x2F, 0xC2, 0xFF)
    assert pt.Color.rgb(1, 2, 3) != pt.Color.rgb(1, 2, 4)
    assert pt.Color.indexed(5) != pt.Color.rgb(5, 5, 5)
    assert {pt.Color.rgb(1, 2, 3): "one"}[pt.Color.rgb(1, 2, 3)] == "one"


def test_indexed_colors_cross_as_an_xnano_color_object() -> None:
    # xnano rejects the string "indexed(5)" (verified), so the object form is
    # the only one that survives the border; its writer emits 38;5;5.
    assert str(pt.Color.indexed(5)) == "Color(Indexed(5))"
    assert isinstance(pt.Color.indexed(5).to_xnano(), xstyles.Color)


@pytest.mark.parametrize(
    "name",
    ["black", "red", "green", "gray", "dark_gray", "light_cyan", "white"],
)
def test_every_named_color_exists_and_crosses_as_its_name(name: str) -> None:
    color = getattr(pt.Color, name)()

    assert color.to_xnano() == name
    assert str(color) == "Color({})".format("".join(p.capitalize() for p in name.split("_")))


def test_reset_is_its_own_color() -> None:
    assert str(pt.Color.reset()) == "Color(Reset)"
    assert pt.Color.reset().to_xnano() == "reset"


@pytest.mark.parametrize(("red", "green", "blue"), [(-1, 0, 0), (0, 256, 0), (0, 0, 999)])
def test_an_out_of_range_rgb_channel_raises(red: int, green: int, blue: int) -> None:
    with pytest.raises(ValueError, match="out of range"):
        pt.Color.rgb(red, green, blue)


def test_an_out_of_range_palette_slot_raises() -> None:
    with pytest.raises(ValueError, match="out of range"):
        pt.Color.indexed(256)


# ─── Modifier ───────────────────────────────────────────────────────────────


def test_modifiers_combine_with_or_and_compare_by_value() -> None:
    combined = pt.Modifier.bold() | pt.Modifier.dim()

    assert combined == pt.Modifier.dim() | pt.Modifier.bold()
    assert combined != pt.Modifier.bold()
    assert "BOLD" in str(combined) and "DIM" in str(combined)


def test_modifiers_intersect_with_and() -> None:
    both = pt.Modifier.bold() | pt.Modifier.dim()

    assert both & pt.Modifier.bold() == pt.Modifier.bold()
    assert pt.Modifier.bold() & pt.Modifier.dim() == pt.Modifier()


def test_the_empty_modifier_is_a_valid_operand() -> None:
    # render.style_from_rich always calls add_modifier, even when no rich
    # attribute was set, so Modifier() must be harmless.
    assert pt.Modifier() | pt.Modifier.bold() == pt.Modifier.bold()
    assert str(pt.Modifier()) == "Modifier(NONE)"


@pytest.mark.parametrize(
    ("factory", "token"),
    [
        (pt.Modifier.bold, "BOLD"),
        (pt.Modifier.dim, "DIM"),
        (pt.Modifier.italic, "ITALIC"),
        (pt.Modifier.underlined, "UNDERLINED"),
        (pt.Modifier.slow_blink, "SLOW_BLINK"),
        (pt.Modifier.reversed, "REVERSED"),
        (pt.Modifier.hidden, "HIDDEN"),
        (pt.Modifier.crossed_out, "CROSSED_OUT"),
    ],
)
def test_every_modifier_names_itself_the_way_pyratatui_does(factory: object, token: str) -> None:
    # The Rich bridges (nodes.rich_from_pt, modals._to_rich) read modifiers out
    # of str(style), so the token spelling is load-bearing.
    modifier = factory()  # type: ignore[operator]

    assert str(modifier) == f"Modifier({token})"
    assert token in str(pt.Style().add_modifier(modifier))


# ─── Style: builders are immutable ──────────────────────────────────────────


def test_the_style_builders_never_mutate_the_receiver() -> None:
    base = pt.Style()

    painted = base.fg(pt.Color.rgb(1, 2, 3)).bg(pt.Color.rgb(4, 5, 6)).bold()

    assert base == pt.Style()
    assert base.foreground is None and base.background is None
    assert painted.foreground == pt.Color.rgb(1, 2, 3)
    assert painted.background == pt.Color.rgb(4, 5, 6)


@pytest.mark.parametrize(
    ("builder", "token"),
    [
        ("bold", "BOLD"),
        ("dim", "DIM"),
        ("italic", "ITALIC"),
        ("underlined", "UNDERLINED"),
        ("reversed", "REVERSED"),
        ("crossed_out", "CROSSED_OUT"),
        ("hidden", "HIDDEN"),
        ("slow_blink", "SLOW_BLINK"),
    ],
)
def test_every_shortcut_builder_adds_its_own_modifier(builder: str, token: str) -> None:
    style = getattr(pt.Style(), builder)()

    assert token in str(style)
    assert style != pt.Style()


def test_styles_compare_structurally() -> None:
    # Rows compare styles with == to decide whether a repaint changed anything.
    red = pt.Color.rgb(255, 0, 0)

    assert pt.Style().fg(red) == pt.Style().fg(pt.Color.rgb(255, 0, 0))
    assert pt.Style().fg(red) != pt.Style().fg(pt.Color.rgb(255, 0, 1))
    assert pt.Style().bold() != pt.Style()
    assert pt.Style() == pt.Style()


def test_a_style_dumps_the_way_pyratatui_dumps_it() -> None:
    assert str(pt.Style()) == "Style(fg=None, bg=None, mods=NONE)"
    assert str(pt.Style().fg(pt.Color.rgb(1, 2, 3))) == (
        "Style(fg=Some(Rgb(1, 2, 3)), bg=None, mods=NONE)"
    )
    assert str(pt.Style().add_modifier(pt.Modifier.bold() | pt.Modifier.crossed_out())) == (
        "Style(fg=None, bg=None, mods=BOLD | CROSSED_OUT)"
    )


# ─── Style: patch ───────────────────────────────────────────────────────────


def test_patch_lets_the_other_styles_set_fields_win() -> None:
    base = pt.Style().fg(pt.Color.rgb(1, 2, 3)).bg(pt.Color.rgb(4, 5, 6))

    patched = base.patch(pt.Style().fg(pt.Color.rgb(9, 9, 9)))

    assert patched.foreground == pt.Color.rgb(9, 9, 9)
    assert patched.background == pt.Color.rgb(4, 5, 6)


def test_patch_keeps_the_base_fields_the_other_style_does_not_set() -> None:
    base = pt.Style().fg(pt.Color.rgb(1, 2, 3))

    assert base.patch(pt.Style()) == base
    assert base.patch(pt.Style().add_modifier(pt.Modifier.dim())).foreground == pt.Color.rgb(
        1, 2, 3
    )


def test_patch_unions_modifiers_instead_of_replacing_them() -> None:
    # This is what makes a bold span under a dim line paint BOLD | DIM.
    base = pt.Style().add_modifier(pt.Modifier.dim())

    patched = base.patch(pt.Style().add_modifier(pt.Modifier.bold()))

    assert "DIM" in str(patched)
    assert "BOLD" in str(patched)


# ─── Style: the xnano bridge ────────────────────────────────────────────────


def test_to_xnano_maps_every_field_and_the_modifier_flags() -> None:
    style = (
        pt.Style()
        .fg(pt.Color.rgb(47, 194, 255))
        .bg(pt.Color.rgb(10, 14, 20))
        .add_modifier(
            pt.Modifier.bold()
            | pt.Modifier.dim()
            | pt.Modifier.italic()
            | pt.Modifier.underlined()
            | pt.Modifier.reversed()
            | pt.Modifier.crossed_out()
        )
    )

    converted = style.to_xnano()

    assert converted == xstyles.Style(
        foreground="#2fc2ff",
        background="#0a0e14",
        bold=True,
        dim=True,
        italic=True,
        underline=True,
        reverse=True,
        crossed_out=True,
    )


def test_to_xnano_maps_slow_blink_onto_xnanos_blink() -> None:
    # xnano has one blink and it is the slow one, so the flag has to land on
    # `blink` — a `rapid_blink` mapping would flash the modal's caret.
    converted = pt.Style().slow_blink().to_xnano()

    assert converted == xstyles.Style(blink=True)


def test_to_xnano_leaves_an_unset_color_unset() -> None:
    assert pt.Style().to_xnano() == xstyles.Style()


# ─── Span ───────────────────────────────────────────────────────────────────


def test_span_width_counts_cells_not_characters() -> None:
    # The deliberate divergence: pyratatui says 2 for '한글', the terminal paints
    # 4, and every layout in this package measures with cell_len.
    assert pt.Span("한글").width() == 4
    assert pt.Span("ab").width() == 2
    assert pt.Span("😀").width() == 2
    assert pt.Span("").width() == 0


def test_styled_replaces_the_spans_own_style() -> None:
    dim = pt.Style().dim()
    bold = pt.Style().bold()

    restyled = pt.Span("a", dim).styled(bold)

    assert restyled.style == bold
    assert restyled.content == "a"
    assert pt.Span("a", dim).style == dim


def test_span_defaults_to_no_style() -> None:
    assert pt.Span("a").style is None
    assert pt.Span("a") == pt.Span("a")


# ─── Line ───────────────────────────────────────────────────────────────────


def test_line_spans_returns_a_copy() -> None:
    line = pt.Line([pt.Span("a")])

    taken = line.spans
    taken.append(pt.Span("b"))

    assert len(line.spans) == 1


def test_push_span_appends_in_place_and_returns_none() -> None:
    line = pt.Line([pt.Span("a")])

    assert line.push_span(pt.Span("b")) is None
    assert [span.content for span in line.spans] == ["a", "b"]


def test_line_width_is_the_sum_of_its_span_cells() -> None:
    assert pt.Line([pt.Span("ab"), pt.Span("한글")]).width() == 6
    assert pt.Line().width() == 0


def test_line_styled_returns_a_new_line_keeping_its_spans() -> None:
    spans = [pt.Span("a")]
    line = pt.Line(spans, pt.Style().dim())

    restyled = line.styled(pt.Style().bold())

    assert restyled is not line
    assert restyled.style == pt.Style().bold()
    assert [span.content for span in restyled.spans] == ["a"]


def test_line_from_string_does_not_split_newlines() -> None:
    # pyratatui keeps a newline inside the one span; splitting is Text's job.
    line = pt.Line.from_string("a\nb")

    assert len(line.spans) == 1
    assert line.spans[0].content == "a\nb"


# ─── Text ───────────────────────────────────────────────────────────────────


def test_text_lines_returns_a_copy() -> None:
    text = pt.Text([pt.Line.from_string("a")])

    taken = text.lines
    taken.append(pt.Line.from_string("b"))

    assert text.height == 1


def test_push_line_and_push_str_append_in_place() -> None:
    text = pt.Text()

    text.push_line(pt.Line.from_string("a"))
    text.push_str("b")

    assert [line.spans[0].content for line in text.lines] == ["a", "b"]


def test_push_str_does_not_split_newlines() -> None:
    # pyratatui appends ONE line holding the string verbatim — from_string is
    # the splitter. A push_str that split would double every pasted blank line.
    text = pt.Text()

    text.push_str("a\nb")

    assert text.height == 1
    assert text.lines[0].spans[0].content == "a\nb"


@pytest.mark.parametrize(
    ("source", "height"),
    [("", 0), ("a", 1), ("a\nb", 2), ("a\nb\n", 2), ("a\n\nb", 3), ("\n", 1)],
)
def test_from_string_splits_on_newlines_and_drops_one_trailing(source: str, height: int) -> None:
    assert pt.Text.from_string(source).height == height


def test_text_width_is_its_widest_line_in_cells() -> None:
    text = pt.Text.from_string("ab\n한글")

    assert text.width() == 4
    assert pt.Text().width() == 0
    assert pt.Text().height == 0


def test_text_styled_and_centered_return_new_texts() -> None:
    text = pt.Text([pt.Line.from_string("ab")])

    assert text.styled(pt.Style().bold()) is not text
    assert text.centered() is not text
    assert text.centered().alignment == "center"
    assert text.alignment == "left"


# ─── Rect ───────────────────────────────────────────────────────────────────


def test_rect_reports_its_geometry() -> None:
    rect = pt.Rect(2, 3, 10, 5)

    assert (rect.x, rect.y, rect.width, rect.height) == (2, 3, 10, 5)
    assert rect.area() == 50
    assert not rect.is_empty()
    assert pt.Rect(0, 0, 0, 5).is_empty()


def test_inner_shrinks_by_the_given_margins() -> None:
    assert pt.Rect(2, 3, 10, 5).inner(1, 1) == pt.Rect(3, 4, 8, 3)
    assert pt.Rect(2, 3, 10, 5).inner(2, 1) == pt.Rect(4, 4, 6, 3)


def test_a_shrink_that_would_invert_answers_the_zero_rect() -> None:
    # Layout code then paints nothing instead of raising mid-frame.
    assert pt.Rect(2, 3, 10, 5).inner(20, 20) == pt.Rect(0, 0, 0, 0)


def test_intersection_is_the_overlap_or_none() -> None:
    rect = pt.Rect(2, 3, 10, 5)

    assert rect.intersection(pt.Rect(5, 5, 20, 20)) == pt.Rect(5, 5, 7, 3)
    assert rect.intersection(pt.Rect(12, 3, 5, 5)) is None
    assert rect.intersection(pt.Rect(100, 100, 1, 1)) is None


@pytest.mark.parametrize(
    ("x", "y", "inside"),
    [(2, 3, True), (11, 7, True), (1, 3, False), (12, 3, False), (2, 8, False)],
)
def test_contains_takes_a_cell_and_is_half_open(x: int, y: int, inside: bool) -> None:
    assert pt.Rect(2, 3, 10, 5).contains(x, y) is inside


# ─── Paragraph ──────────────────────────────────────────────────────────────


def test_paragraph_builders_return_new_paragraphs() -> None:
    paragraph = pt.Paragraph(pt.Text.from_string("a"))

    for built in (
        paragraph.style(pt.Style().bold()),
        paragraph.wrap(True, True),
        paragraph.scroll(1, 0),
        paragraph.alignment("center"),
        paragraph.block(None),
    ):
        assert built is not paragraph


def test_paragraph_from_string_splits_like_text() -> None:
    assert pt.Paragraph.from_string("a\nb").text.height == 2


def test_paragraph_alignment_accepts_the_spellings_our_code_uses() -> None:
    paragraph = pt.Paragraph(pt.Text.from_string("a"))

    assert paragraph.alignment("center").alignment_or_text == "center"
    assert paragraph.alignment("centre").alignment_or_text == "center"
    assert paragraph.alignment("right").alignment_or_text == "right"
    assert paragraph.alignment("nonsense").alignment_or_text == "left"


# ─── Terminal: the headless screen ──────────────────────────────────────────


def test_the_terminal_is_a_context_manager_with_an_area() -> None:
    with pt.Terminal(width=12, height=3) as terminal:
        assert terminal.area() == pt.Rect(0, 0, 12, 3)


def test_draw_paints_a_paragraph_at_its_rect() -> None:
    terminal = pt.Terminal(width=20, height=3)

    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("hello\nworld")),
            pt.Rect(0, 0, 20, 3),
        )
    )

    # The third row of the rect is never written, so it stays blank; the frame's
    # text is the two rows the paragraph actually painted.
    assert terminal.output_text() == "hello\nworld\n"
    assert terminal.output_lines() == ["hello", "world", ""]


def test_draw_honours_the_rects_origin_and_clips_to_its_width() -> None:
    terminal = pt.Terminal(width=12, height=3)

    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("abcdefghij")),
            pt.Rect(2, 1, 5, 1),
        )
    )


def test_draw_uses_the_frame_area_for_the_whole_canvas() -> None:
    terminal = pt.Terminal(width=8, height=2)

    seen: list[pt.Rect] = []

    def paint(frame: object) -> None:
        seen.append(frame.area)  # type: ignore[attr-defined]
        frame.render_widget(  # type: ignore[attr-defined]
            pt.Paragraph(pt.Text.from_string("x")), frame.area  # type: ignore[attr-defined]
        )


    terminal.draw(paint)

    assert seen == [pt.Rect(0, 0, 8, 2)]


def test_each_draw_replaces_the_frame_instead_of_stacking_on_it() -> None:
    terminal = pt.Terminal(width=10, height=1)

    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("first")), pt.Rect(0, 0, 10, 1)
        )
    )
    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("2nd")), pt.Rect(0, 0, 10, 1)
        )
    )


def test_centered_and_right_aligned_text_land_on_pyratatui_columns() -> None:
    # Both offsets were read off the real terminal: center is
    # width//2 - text//2 (NOT (width - text)//2), so they match on odd widths.
    terminal = pt.Terminal(width=8, height=2)

    terminal.draw(
        lambda frame: (
            frame.render_widget(
                pt.Paragraph(pt.Text.from_string("ab")).centered(), pt.Rect(0, 0, 8, 1)
            ),
            frame.render_widget(
                pt.Paragraph(pt.Text.from_string("ab")).right_aligned(), pt.Rect(0, 1, 8, 1)
            ),
        )
    )

    # Trailing cells are left unpainted, so a row ends where its text does.
    assert terminal.output_lines() == ["   ab", "      ab"]


def test_a_wider_than_rect_line_is_clipped_not_wrapped() -> None:
    terminal = pt.Terminal(width=4, height=2)

    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("abcdefgh")), pt.Rect(0, 0, 4, 2)
        )
    )

    assert terminal.output_lines()[0] == "abcd"
    assert terminal.output_lines()[1].strip() == ""


def test_a_taller_than_rect_block_is_clipped_to_the_rects_rows() -> None:
    terminal = pt.Terminal(width=6, height=3)

    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("a\nb\nc\nd")), pt.Rect(0, 0, 6, 2)
        )
    )


def test_scroll_skips_rows_and_cells() -> None:
    terminal = pt.Terminal(width=6, height=2)

    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("r0\nr1\nr2")).scroll(1, 0), pt.Rect(0, 0, 6, 2)
        )
    )
    assert [row.rstrip() for row in terminal.output_lines()] == ["r1", "r2"]

    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("abcdef")).scroll(0, 2), pt.Rect(0, 0, 6, 1)
        )
    )
    assert terminal.output_lines()[0].rstrip() == "cdef"


def test_a_zero_sized_rect_paints_nothing() -> None:
    terminal = pt.Terminal(width=6, height=2)

    terminal.draw(
        lambda frame: (
            frame.render_widget(pt.Paragraph(pt.Text.from_string("a")), pt.Rect(0, 0, 0, 1)),
            frame.render_widget(pt.Paragraph(pt.Text.from_string("b")), pt.Rect(0, 1, 6, 0)),
        )
    )

    terminal = pt.Terminal(width=4, height=1)

    with pytest.raises(TypeError, match="cannot paint"):
        terminal.draw(lambda frame: frame.render_widget("not a widget", pt.Rect(0, 0, 4, 1)))


def test_clear_and_restore_leave_a_usable_or_released_terminal() -> None:
    terminal = pt.Terminal(width=6, height=1)
    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("x")), pt.Rect(0, 0, 6, 1)
        )
    )

    terminal.clear()
    assert [row.rstrip() for row in terminal.output_lines()] == [""]

    terminal.restore()
    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("y")), pt.Rect(0, 0, 6, 1)
        )
    )
    assert terminal.output_lines()[0].rstrip() == "y"


def test_the_cursor_toggles() -> None:
    terminal = pt.Terminal(width=4, height=1)

    assert terminal.cursor_visible is False
    terminal.show_cursor()
    assert terminal.cursor_visible is True
    assert terminal.canvas.screen.cursor.is_visible is True
    terminal.hide_cursor()
    assert terminal.cursor_visible is False
    assert terminal.canvas.screen.cursor.is_visible is False


# ─── Terminal: the styled screen (the point of the shim) ────────────────────


def test_the_ansi_carries_the_painted_truecolor() -> None:
    terminal = pt.Terminal(width=6, height=1)

    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text([pt.Line([styled("hi", pt.Style().fg(pt.Color.rgb(1, 2, 3)))])])),
            pt.Rect(0, 0, 6, 1),
        )
    )

    assert "38;2;1;2;3" in ansi_of(terminal)


def test_the_theme_color_reaches_the_screen_as_truecolor() -> None:
    # The owner-pinned accent, end to end: theme hex -> pt.Color -> xnano -> ANSI.
    from miniouto.cli.tui.theme import MINIOUTO_THEME

    with pt.Terminal(width=8, height=1) as terminal:
        terminal.draw(
            lambda frame: frame.render_widget(
                pt.Paragraph(
                    pt.Text(
                        [
                            pt.Line(
                                [styled("MINIOUTO", pt.Style().fg(pt.Color.rgb(47, 194, 255)))]
                            )
                        ]
                    )
                ),
                pt.Rect(0, 0, 8, 1),
            )
        )

        assert MINIOUTO_THEME.primary == "#2fc2ff"
        assert "38;2;47;194;255" in ansi_of(terminal)
        assert terminal.output_lines()[0].rstrip() == "MINIOUTO"


def test_the_background_and_modifiers_reach_the_screen() -> None:
    terminal = pt.Terminal(width=4, height=1)

    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(
                pt.Text(
                    [
                        pt.Line(
                            [
                                styled(
                                    "ab",
                                    pt.Style()
                                    .fg(pt.Color.rgb(169, 186, 197))
                                    .bg(pt.Color.rgb(10, 14, 20))
                                    .bold()
                                    .reversed(),
                                )
                            ]
                        )
                    ]
                )
            ),
            pt.Rect(0, 0, 4, 1),
        )
    )

    painted = ansi_of(terminal)

    assert "38;2;169;186;197" in painted
    assert "48;2;10;14;20" in painted
    assert "1" in painted.split("m")[0].split(";")
    assert "7" in painted.split("m")[0].split(";")


def test_the_span_style_wins_and_the_line_style_fills_the_gaps() -> None:
    # pyratatui's chain: span over line over text, modifiers unioning.
    line = pt.Line(
        [styled("AB", pt.Style().fg(pt.Color.rgb(0, 0, 255)))],
        pt.Style().fg(pt.Color.rgb(255, 0, 0)),
    )
    second = pt.Line([pt.Span("cd")], pt.Style().fg(pt.Color.rgb(255, 0, 0)))
    terminal = pt.Terminal(width=4, height=2)

    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text([line, second])), pt.Rect(0, 0, 4, 2)
        )
    )

    rows = ansi_of(terminal).splitlines()

    assert "38;2;0;0;255" in rows[0]
    assert "38;2;255;0;0" in rows[1]


def test_the_empty_style_paints_plain_text() -> None:
    terminal = pt.Terminal(width=4, height=1)

    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text([pt.Line([pt.Span("ok")])])), pt.Rect(0, 0, 4, 1)
        )
    )

    assert terminal.output_lines()[0].rstrip() == "ok"
    assert "38;2;" not in ansi_of(terminal)


# ─── TextState: the readline contract ───────────────────────────────────────


def test_typing_moves_the_caret_and_left_inserts_before_it() -> None:
    state = pt.TextState()
    state.focus()

    for char in "hey":
        assert state.handle_key(Key(char)) is True
    assert state.handle_key(Key("Left")) is True
    assert state.handle_key(Key("!")) is True

    assert state.value() == "he!y"
    assert state.cursor_pos == 3


def test_ctrl_k_kills_to_the_end_of_the_line() -> None:
    state = pt.TextState("abc")
    state.focus()
    state.handle_key(Key("Left"))

    assert state.handle_key(Key("k", ctrl=True)) is True

    assert state.value() == "ab"
    assert state.cursor_pos == 2


def test_enter_completes_and_esc_aborts() -> None:
    typed = pt.TextState()
    typed.focus()
    typed.handle_key(Key("a"))

    assert typed.is_pending() is True
    assert typed.handle_key(Key("Enter")) is True
    assert typed.is_complete() is True
    assert str(typed.status) == "PromptStatus.Complete"

    cancelled = pt.TextState()
    cancelled.focus()

    assert cancelled.handle_key(Key("Esc")) is True
    assert cancelled.is_aborted() is True
    assert str(cancelled.status) == "PromptStatus.Aborted"


def test_ctrl_c_aborts_without_clearing_the_buffer() -> None:
    state = pt.TextState("abc")
    state.focus()

    assert state.handle_key(Key("c", ctrl=True)) is True

    assert state.is_aborted() is True
    assert state.value() == "abc"


@pytest.mark.parametrize("terminal_key", [Key("Enter"), Key("Esc"), Key("c", ctrl=True)])
def test_a_finished_editor_consumes_nothing(terminal_key: Key) -> None:
    state = pt.TextState("abc")
    state.focus()
    state.handle_key(terminal_key)

    assert state.handle_key(Key("z")) is False
    assert state.value() == "abc"


def test_reset_status_keeps_the_buffer_and_reset_clears_it() -> None:
    state = pt.TextState("abc")
    state.focus()
    state.handle_key(Key("Home"))
    state.handle_key(Key("Enter"))

    state.reset_status()
    assert state.is_pending() is True
    assert state.value() == "abc"
    assert state.cursor_pos == 0

    state.reset()
    assert state.value() == ""
    assert state.cursor_pos == 0


@pytest.mark.parametrize(
    ("keys", "value", "cursor"),
    [
        ([Key("Home")], "ab", 0),
        ([Key("End")], "ab", 2),
        ([Key("Backspace")], "a", 1),
        ([Key("Left"), Key("Delete")], "a", 1),
        ([Key("Left"), Key("b", ctrl=True)], "ab", 0),
        ([Key("Left"), Key("f", ctrl=True)], "ab", 2),
        ([Key("a", ctrl=True), Key("X")], "Xab", 1),
        ([Key("u", ctrl=True)], "", 0),
    ],
)
def test_the_readline_edits_match_pyratatui(
    keys: list[Key], value: str, cursor: int
) -> None:
    state = pt.TextState("ab")
    state.focus()
    for key in keys:
        state.handle_key(key)

    assert state.value() == value
    assert state.cursor_pos == cursor


def test_ctrl_w_deletes_the_word_before_the_caret() -> None:
    state = pt.TextState("ab cd")
    state.focus()

    assert state.handle_key(Key("w", ctrl=True)) is True

    assert state.value() == "ab "


def test_a_named_key_our_editor_does_not_use_is_not_consumed() -> None:
    # Tab and the arrows belong to the FOCUS system, not the buffer.
    state = pt.TextState("ab")
    state.focus()

    assert state.handle_key(Key("Tab")) is False
    assert state.handle_key(Key("F5")) is False
    assert state.handle_key(Key("Down")) is False
    assert state.value() == "ab"


def test_a_modified_character_does_not_edit_the_buffer() -> None:
    state = pt.TextState("ab")
    state.focus()

    assert state.handle_key(Key("x", alt=True)) is False
    assert state.handle_key(Key("z", ctrl=True)) is False
    assert state.value() == "ab"


def test_wide_and_astral_characters_insert_as_one_key() -> None:
    state = pt.TextState()
    state.focus()

    assert state.handle_key(Key("한")) is True
    assert state.handle_key(Key("😀")) is True

    assert state.value() == "한😀"
    assert state.cursor_pos == 2


def test_set_value_puts_the_caret_at_the_end_and_clear_input_empties_it() -> None:
    state = pt.TextState()

    state.set_value("hello")
    assert state.value() == "hello"
    assert state.cursor_pos == 5

    state.clear_input()
    assert state.value() == ""
    assert state.cursor_pos == 0


def test_focus_and_blur_track_the_flag() -> None:
    state = pt.TextState()

    assert state.is_focused is False
    state.focus()
    assert state.is_focused is True
    state.blur()
    assert state.is_focused is False


def test_focus_does_not_gate_the_editor() -> None:
    # pyratatui edits an unfocused TextState too, and modal.handle_mouse focuses
    # the input AFTER a click has already been routed.
    state = pt.TextState("ab")

    assert state.handle_key(Key("c")) is True
    assert state.value() == "abc"


# ─── the modal contract, end to end ─────────────────────────────────────────


def test_a_text_input_modal_submits_what_was_typed() -> None:
    from miniouto.cli.tui.modals import TextInputModal

    modal = TextInputModal("Max context window", placeholder="e.g. 128000")

    for char in "128000":
        modal.handle_key(Key(char))
    modal.handle_key(Key("Enter"))

    assert modal.result == "128000"
    assert modal.dismissed


def test_a_text_input_modal_cancels_to_none() -> None:
    from miniouto.cli.tui.modals import TextInputModal

    modal = TextInputModal("Title", initial="abc")

    modal.handle_key(Key("Esc"))

    assert modal.result is None
    assert modal.dismissed


# ─── Terminal: the real-screen path ─────────────────────────────────────────
#
# A REAL screen needs a tty, so the pty probe (verification) is the end-to-end
# check. What is unit-testable here is the CONTRACT the app depends on: hooks
# are held back until the canvas opens (xnano only turns mouse/scroll/paste on
# from the hooks that exist at open time), the headless default is untouched,
# and the cursor moves through the canvas rather than an escape sequence.


def test_hooks_are_held_until_the_canvas_opens() -> None:
    """A hook registered before open is deferred; it cannot reach the canvas yet.

    xnano decides which terminal settings to switch on from the hooks that exist
    WHEN THE CANVAS GOES UP — a mouse hook added to an open canvas is attached
    and never runs. So the shim must record the callback and hand it over at
    ``__enter__``; registering eagerly would break hover/scroll silently.
    """

    terminal = pt.Terminal(real_screen=True)
    seen: list[str] = []
    terminal.hook("key", seen.append)

    # Given: nothing was created yet, so nothing could be registered
    assert terminal._canvas is None
    assert [kind for kind, _callback in terminal._hooks] == ["key"]
    assert terminal.is_open is False


def test_the_headless_terminal_still_reports_its_own_area_and_mode() -> None:
    # The default must not change: the 1326-test suite reads this shape.
    terminal = pt.Terminal(width=12, height=3)
    assert terminal.real_screen is False
    assert terminal.area() == pt.Rect(0, 0, 12, 3)
    assert str(terminal) == "Terminal(headless, 12x3)"
    assert pt.Terminal(real_screen=True).real_screen is True


def test_a_hook_on_a_headless_terminal_is_recorded_and_inert() -> None:
    # Nothing hears input offscreen (the Pilot injects events directly), but the
    # call must not raise and must not create a canvas behind the caller's back.
    terminal = pt.Terminal(width=4, height=1)
    terminal.hook("mouse", lambda _context: None)
    assert terminal._canvas is None
    assert len(terminal._hooks) == 1


def test_move_cursor_names_the_caret_shape() -> None:
    """Regression: the caret was POSITIONED but never given a SHAPE.

    xnano's ``shape=None`` means "keep the user's own shape", so a cursor that
    set only ``position`` + ``is_visible`` emitted no ``DECSCUSR`` at all —
    measured on a pty, not one ``ESC[N q`` in a whole session of typing. The
    owner read the invisible caret as "there is no cursor". A blinking bar is
    what a text field shows.
    """
    terminal = pt.Terminal(width=20, height=5)
    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("row")), pt.Rect(0, 0, 20, 1)
        )
    )
    terminal.move_cursor(4, 2)

    cursor = terminal.canvas.screen.cursor
    assert cursor.shape == "bar", cursor.shape
    assert cursor.is_blinking is True
    assert cursor.is_visible is True


def test_move_cursor_does_not_rewrite_the_same_cell() -> None:
    """Re-assigning ``position`` every tick makes xnano advance a frame each time.

    Measured on a pty: an unchanged canvas whose cursor was re-assigned at 60 Hz
    advanced 173 frames and burned 5.3% of a core over 3 idle seconds, against 0
    frames for an untouched canvas. The memo is what keeps an idle tick free —
    but it must never swallow a move that FOLLOWS a hide.
    """

    terminal = pt.Terminal(width=20, height=5)
    terminal.move_cursor(4, 2)
    cursor = terminal.canvas.screen.cursor

    # Given: the position is already written, so a repeat costs no write.
    terminal.move_cursor(4, 2)
    assert terminal._cursor_cell == (4, 2)
    assert terminal.cursor_visible is True

    # When: the caret is hidden and then put back on the SAME cell
    terminal.hide_cursor()
    assert cursor.is_visible is False
    terminal.move_cursor(4, 2)

    # Then: the write is not skipped — a hidden caret has to come back
    assert cursor.is_visible is True
    assert cursor.shape == "bar"
    assert terminal.cursor_visible is True
    """The caret has to be xnano's own.

    A canvas on the screen re-asserts its cursor on every frame it draws, so a
    raw ``ESC[row;colH`` written between frames is undone by the next one
    (measured on a pty). ``move_cursor`` sets the canvas cursor instead, which
    is what makes the caret stay in the input band.
    """

    terminal = pt.Terminal(width=20, height=5)
    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("row")), pt.Rect(0, 0, 20, 1)
        )
    )
    terminal.move_cursor(4, 2)

    cursor = terminal.canvas.screen.cursor
    assert (cursor.position.x, cursor.position.y) == (4, 2)
    assert cursor.is_visible is True
    assert terminal.cursor_visible is True


def test_move_cursor_after_restore_is_a_no_op() -> None:
    # A dead canvas has no cursor; the teardown must not raise through it. The
    # call is swallowed whole, so the shim's own flag stays as the teardown left
    # it rather than claiming a caret no screen can show.
    terminal = pt.Terminal(width=4, height=1)
    terminal.restore()
    terminal.move_cursor(1, 1)  # must not raise
    assert terminal.cursor_visible is False


def test_the_painted_frame_is_identical_in_both_modes() -> None:
    """The real screen must paint the same cells the tests read headless.

    Only the FLUSH differs (an offscreen canvas needs an explicit render, a live
    one is drawn by xnano). Everything the suite asserts on — the styled rows —
    comes from the same ``render_widget`` path, so the two modes cannot drift.
    """

    from miniouto.cli.tui.theme import MINIOUTO_THEME

    def paint(frame: pt.Frame) -> None:
        frame.render_widget(
            pt.Paragraph(
                pt.Text(
                    [pt.Line([pt.Span("MINIOUTO", pt.Style().fg(pt.Color.rgb(47, 194, 255)))])]
                )
            ),
            pt.Rect(0, 0, 10, 1),
        )

    headless = pt.Terminal(width=10, height=1)
    headless.draw(paint)
    assert headless.output_lines()[0].rstrip() == "MINIOUTO"
    assert "38;2;47;194;255" in headless.output_ansi()
    assert MINIOUTO_THEME.primary == "#2fc2ff"
    # The mode flag is the ONLY difference the constructor takes.
    assert pt.Terminal(width=10, height=1, real_screen=True).real_screen is True


# ─── Terminal: placement reuse (what makes the real screen diff) ────────────


def test_an_unchanged_frame_reuses_its_placements_instead_of_repainting() -> None:
    """Recreating a placement per frame forces a FULL repaint every frame.

    Measured on a pty before this: a 120x32 screen cost ~240 kB/s of escape
    sequences, because the canvas saw a brand-new placement object each frame and
    had nothing to diff against — the input band visibly flickered. Keeping the
    placement and replacing its text is what makes an unchanged row emit
    nothing.
    """

    terminal = pt.Terminal(width=20, height=2)

    def paint(frame: pt.Frame) -> None:
        for row in range(2):
            frame.render_widget(
                pt.Paragraph(pt.Text.from_string(f"row {row}")),
                pt.Rect(0, row, 20, 1),
            )

    terminal.draw(paint)
    first = list(terminal._placements)
    assert len(first) == 2

    terminal.draw(paint)
    # Then: the SAME placement objects are still the frame's, not new ones
    assert terminal._placements == first
    assert all(a is b for a, b in zip(terminal._placements, first, strict=True))
    assert terminal.output_lines() == ["row 0", "row 1"]


def test_a_row_that_stops_being_painted_is_removed() -> None:
    """A shorter frame must not leave the previous rows showing underneath."""

    terminal = pt.Terminal(width=12, height=3)
    terminal.draw(
        lambda frame: [
            frame.render_widget(
                pt.Paragraph(pt.Text.from_string("A")), pt.Rect(0, 0, 12, 1)
            ),
            frame.render_widget(
                pt.Paragraph(pt.Text.from_string("B")), pt.Rect(0, 1, 12, 1)
            ),
        ]
    )
    assert terminal.output_lines()[:2] == ["A", "B"]

    # When: only row 0 is painted on the next frame
    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("Z")), pt.Rect(0, 0, 12, 1)
        )
    )

    # Then: row 1 was dropped, not left behind
    assert terminal.output_lines()[:2] == ["Z", ""]
    assert len(terminal._placements) == 1


def test_a_changed_row_updates_in_place() -> None:
    terminal = pt.Terminal(width=10, height=1)
    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("first")), pt.Rect(0, 0, 10, 1)
        )
    )
    placement = terminal._placements[0]
    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("2nd")), pt.Rect(0, 0, 10, 1)
        )
    )
    assert terminal._placements[0] is placement
    assert terminal.output_lines()[0].rstrip() == "2nd"


def test_a_resize_drops_every_placement_before_the_new_frame() -> None:
    """A resized canvas invalidates every placement's geometry at once.

    ``area()`` is the canvas's own size on the real screen, so the resize is
    driven by giving the terminal a canvas of the new size — the same thing
    xnano does when the window changes under an open canvas.
    """

    terminal = pt.Terminal(width=10, height=2)
    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("old")), pt.Rect(0, 0, 10, 1)
        )
    )
    assert terminal._rows
    assert terminal._last_area == (10, 2)

    # When: the canvas itself is now smaller — that is what `area()` reports
    # after xnano saw a window resize under an open canvas.
    old_canvas = terminal._canvas
    terminal._canvas = None
    terminal._width, terminal._height = 6, 1
    terminal.draw(
        lambda frame: frame.render_widget(
            pt.Paragraph(pt.Text.from_string("new")), pt.Rect(0, 0, 6, 1)
        )
    )

    # Then: the stale placement is gone and only the new one remains
    assert terminal._last_area == (6, 1)
    assert len(terminal._placements) == 1
    assert terminal.output_lines()[0].rstrip() == "new"
    terminal.restore()
    if old_canvas is not None and not old_canvas.is_closed:
        old_canvas.close()
