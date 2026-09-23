"""Lock the parse_style contract from src/miniouto/storage/styles.py."""

from __future__ import annotations

import pytest

from miniouto.storage.styles import SubagentSpec, parse_style


def test_parse_style_legacy_subagent_tag_becomes_named_subagent_subagent() -> None:
    # Given: a legacy two-block style document
    content = "<outo>A</outo>\n<subagent>B</subagent>"

    # When: parse_style parses it
    outo, specs = parse_style(content)

    # Then: outo is "A" and exactly one spec named "subagent" with prompt "B"
    assert outo == "A"
    assert specs == [SubagentSpec(name="subagent", prompt="B")]


def test_parse_style_multiple_named_subagents_in_document_order() -> None:
    # Given: outo plus two named subagent blocks
    content = "<outo>A</outo>\n<editor>B</editor>\n<file-picker>C</file-picker>"

    # When: parse_style parses it
    outo, specs = parse_style(content)

    # Then: spec names are in document order and prompts are stripped
    assert outo == "A"
    assert [s.name for s in specs] == ["editor", "file-picker"]
    assert specs[0].prompt == "B"
    assert specs[1].prompt == "C"


def test_parse_style_outo_only_document_yields_empty_specs() -> None:
    # Given: an outo-only style with no subagent tags
    content = "<outo>A</outo>"

    # When: parse_style parses it
    outo, specs = parse_style(content)

    # Then: outo is preserved, specs is the empty list (call_subagent not exposed)
    assert outo == "A"
    assert specs == []


def test_parse_style_whole_document_when_no_outo_tag() -> None:
    # Given: a style document containing no XML tags at all
    content = "just prose, no tags"

    # When: parse_style parses it
    outo, specs = parse_style(content)

    # Then: the whole stripped document is the outo prompt and specs is empty
    assert outo == "just prose, no tags"
    assert specs == []


def test_parse_style_nested_tag_inside_subagent_body_is_content_not_a_subagent() -> None:
    # Given: a subagent body that itself contains a <b> tag
    content = "<outo>A</outo>\n<editor>use <b>x</b> here</editor>"

    # When: parse_style parses it
    outo, specs = parse_style(content)

    # Then: exactly one spec named "editor" whose prompt keeps the inner <b> verbatim
    assert outo == "A"
    assert len(specs) == 1
    assert specs[0].name == "editor"
    assert "<b>x</b>" in specs[0].prompt


def test_parse_style_duplicate_subagent_names_raise_value_error() -> None:
    # Given: two blocks sharing the same name "editor"
    content = "<outo>A</outo>\n<editor>B</editor>\n<editor>C</editor>"

    # When / Then: parsing raises ValueError naming the duplicate
    with pytest.raises(ValueError, match="Duplicate subagent name"):
        parse_style(content)


def test_parse_style_hyphenated_tag_names_are_supported() -> None:
    # Given: hyphenated tag names used by real bundled styles
    content = (
        "<outo>A</outo>\n"
        "<file-picker>fp body</file-picker>\n"
        "<code-searcher>cs body</code-searcher>"
    )

    # When: parse_style parses it
    outo, specs = parse_style(content)

    # Then: each hyphenated tag becomes a subagent with that exact name
    assert outo == "A"
    assert [s.name for s in specs] == ["file-picker", "code-searcher"]
    assert specs[0].prompt == "fp body"
    assert specs[1].prompt == "cs body"


def test_parse_style_strips_surrounding_whitespace_from_prompts() -> None:
    # Given: blocks with leading and trailing whitespace inside the tags
    content = "<outo>\n\n  A  \n</outo>\n<editor>\n\n  B  \n</editor>"

    # When: parse_style parses it
    outo, specs = parse_style(content)

    # Then: both prompts are stripped of surrounding whitespace
    assert outo == "A"
    assert specs == [SubagentSpec(name="editor", prompt="B")]


def test_parse_style_outo_tag_never_appears_as_subagent_name() -> None:
    # Given: a second <outo> block written as a sibling of the first
    content = "<outo>A</outo>\n<outo>B</outo>"

    # When: parse_style parses it
    outo, specs = parse_style(content)

    # Then: "outo" never appears as a spec name (it is reserved)
    assert outo == "A"
    assert all(s.name != "outo" for s in specs)
    assert specs == []


def test_parse_style_tag_inside_outo_block_is_not_a_subagent() -> None:
    # Given: a tag pair nested inside the outo body plus a real subagent block
    content = "<outo>see <editor>ex</editor></outo>\n<subagent>B</subagent>"

    # When: parse_style parses it
    outo, specs = parse_style(content)

    # Then: only the real subagent exists; the inner <editor> is just outo body content
    assert outo == "see <editor>ex</editor>"
    assert [s.name for s in specs] == ["subagent"]
    assert specs[0].prompt == "B"
