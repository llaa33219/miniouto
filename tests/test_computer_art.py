"""Lock the Computer tool's `screen` action: screenshot -> .art file.

The compositor cannot be driven from a test, so the action is exercised
through a fake `Image`-like object whose `screenshot()` returns a real PNG
built by the encoder in `tests/test_ascii_art.py` (imported, never copied).
Every assertion is on what the model and the `miniouto computer show` lane
actually consume: the file on disk and the tool-result string.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from miniouto.cli.tui import art_path_in_result
from miniouto.storage.paths import COMPUTER_SCREENS_DIR
from miniouto.tools import computer as computer_mod
from miniouto.tools.ascii_art import (
    ART_BEGIN,
    OCTANT_FIRST_CODEPOINT,
    OCTANT_LAST_CODEPOINT,
    parse_art,
    strip_sgr,
)
from miniouto.tools.computer import (
    ART_MAX_CHARS,
    ART_MIN_CHARS,
    ComputerUseError,
    computer,
)
from miniouto.tools.registry import _computer_handler
from test_ascii_art import make_png

_FILENAME_RE = re.compile(r"^wl-1-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.art$")


class FakeImage:
    """Stands in for py-Vwayland's `Image`: PNG bytes plus dimensions."""

    def __init__(self, png: bytes, width: int, height: int) -> None:
        self.png_bytes = png
        self.width = width
        self.height = height


class FakeCompositor:
    def __init__(self, png: bytes, width: int, height: int) -> None:
        self._image = FakeImage(png, width, height)
        self.screenshots = 0

    def screenshot(self) -> FakeImage:
        self.screenshots += 1
        return self._image


def _flat_png(width: int, height: int, rgba=(30, 60, 90, 255)) -> bytes:
    return make_png([[rgba] * width for _ in range(height)], 6)


@pytest.fixture
def one_screen(monkeypatch):
    """Register a single fake screen and return its compositor."""
    comp = FakeCompositor(_flat_png(1280, 720), 1280, 720)
    monkeypatch.setattr(
        computer_mod, "_resolve_screen", lambda screen, *, autospawn: ("wl-1", comp)
    )
    return comp


@pytest.fixture
def isolated_screens_dir(monkeypatch, tmp_path):
    """Point the `.art` writes at a throwaway dir.

    The session-wide one is shared with the `screen` tests, one of which counts
    the files in it; a screenshot test must not perturb that count.
    """
    target = tmp_path / "computer-screens"
    monkeypatch.setattr(computer_mod, "COMPUTER_SCREENS_DIR", target)
    return target


# ── end to end through computer() ────────────────────────────────────────────


def test_screen_action_writes_art_file_into_the_screens_dir(one_screen, monkeypatch) -> None:
    # Given: one screen up and a flat 1280x720 capture
    monkeypatch.setattr(
        computer_mod, "COMPUTER_SCREENS_DIR", COMPUTER_SCREENS_DIR, raising=False
    )

    # When: the screen action runs
    result = computer("screen")

    # Then: exactly one .art file lands there, named <screen-id>-<timestamp>.art
    files = sorted(COMPUTER_SCREENS_DIR.glob("*.art"))
    assert len(files) == 1
    assert _FILENAME_RE.match(files[0].name)
    assert str(files[0]) in result
    assert parse_art(files[0].read_text(encoding="utf-8")).meta["screen"] == "wl-1"


def test_screen_action_grid_maths_match_the_render(one_screen) -> None:
    # Given: a 1280x720 screen
    # When: the screen action renders at 100 cells
    result, art_text, path = computer_mod._render_screen_art(
        one_screen.screenshot().png_bytes, "wl-1", 100, True
    )
    art = parse_art(art_text)

    # Then: 1280x720 -> 100x28 with a 6x6 source pixel per sub-cell, and the
    # `subcell-truecolor` mode — a cell is 2x4 square pixels, so the row count
    # is the same one half-block gives and the sub-cells stay square
    assert art.meta["source"] == "1280x720"
    assert art.meta["grid"] == "100x28"
    assert art.meta["cell"] == f"{round(1280 / 200)}x{round(720 / 112)}"
    assert art.meta["cell"] == "6x6"
    assert art.meta["mode"] == "subcell-truecolor"
    assert (art.cols, art.rows) == (100, 28)
    assert len(art.body.split("\n")) == 28
    assert f"{path}" in result


def test_screen_result_carries_path_preview_and_more_rows_line(one_screen) -> None:
    # Given: a 1280x720 screen at the default width (28 rows of art)
    result = computer("screen")

    # Then: the result names the file, labels the new mode and previews rows
    assert "Screen art captured (1280x720 -> 100x28 chars, subcell truecolor)." in result
    assert "Saved to: " in result
    # And: the preview is capped by the row budget, and the rows it left out are
    # named — the art itself is far taller than one tool result can carry
    shown = int(result.split("Preview (first ", 1)[1].split(" rows)", 1)[0])
    assert shown == 24
    preview = result.split("Preview (first 24 rows):\n", 1)[1].split("\n")
    assert len(preview) == 25  # 24 art rows plus the "more rows" note
    assert preview[-1] == "… 4 more rows are in the file above."
    assert ART_BEGIN not in result

    # And: a wider render whose art outgrows the 3800-char preview budget says
    # how many rows it kept back
    wide = computer("screen", chars=200)
    assert "more rows are in the file above." in wide


def test_screen_result_stays_under_the_tool_result_cap(one_screen) -> None:
    # Given: a flat frame rendered at the widest allowed art (200 cells)
    result = computer("screen", chars=200)

    # Then: the string fits the 4000-char tool-result cap with room to spare
    assert len(result) < 3800
    assert "200x56 chars" in result


def test_screen_plain_mode_records_ramp_plain_and_emits_no_sgr(one_screen) -> None:
    # Given: the same capture rendered with colour turned off
    result = computer("screen", chars=40, color=False)
    path = Path(result.split("Saved to: ", 1)[1].split("\n", 1)[0])
    art = parse_art(path.read_text(encoding="utf-8"))

    # Then: the meta records ramp-plain and no escape byte survives anywhere
    assert art.meta["mode"] == "ramp-plain"
    assert "\x1b" not in art.body
    assert "\x1b" not in strip_sgr(result)
    assert "ramp plain" in result


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(5, ART_MIN_CHARS), (9999, ART_MAX_CHARS), (60, 60)],
)
def test_screen_chars_are_clamped_to_the_allowed_range(
    one_screen, requested: int, expected: int
) -> None:
    # Given: a requested art width outside (or inside) the allowed range
    # When: the screen action renders
    art = parse_art(computer_mod._render_screen_art(
        one_screen.screenshot().png_bytes, "wl-1", requested, False
    )[1])

    # Then: the rendered width is the clamped value
    assert art.cols == expected
    assert all(len(line) == expected for line in art.body.split("\n"))


def test_screen_returns_a_str_so_it_bypasses_the_screenshot_gate(monkeypatch) -> None:
    # Given: a handler whose screenshot gate is CLOSED (the openai chat-
    # completions degradation path) and a screen that is up
    monkeypatch.setattr(
        computer_mod, "_resolve_screen", lambda screen, *, autospawn: ("wl-1", FakeCompositor(_flat_png(64, 48), 64, 48))
    )
    monkeypatch.setattr("miniouto.tools.registry._SCREENSHOTS_SUPPORTED", False)

    # When: the registered handler runs the screen action
    result = _computer_handler(action="screen", screen="wl-1")

    # Then: the text result comes through untouched, not the screenshot notice
    assert isinstance(result, str)
    assert "Saved to: " in result

    # And: the closed gate still refuses the screenshot action itself
    assert "unavailable RIGHT NOW" in _computer_handler(action="screenshot", screen="wl-1")


def test_screen_action_respects_the_screen_registry_selection() -> None:
    # Given: the compositor mock is never consulted for an unknown screen
    # When: a screen id that was never spawned is requested
    # Then: the registry's own error surfaces, unchanged
    with pytest.raises(ComputerUseError, match="No screens"):
        computer_mod._resolve_screen("browser", autospawn=False)


# ── `screenshot` writes the same `.art` sibling ──────────────────────────────


def _broken_art(*_args, **_kwargs):
    raise RuntimeError("no art today")


def test_screenshot_action_writes_an_art_sibling(one_screen, isolated_screens_dir) -> None:
    # Given: one screen up and a flat 1280x720 capture
    # When: the screenshot action runs
    shot = computer("screenshot")

    # Then: the pixels are untouched and a matching .art file was written
    assert (shot.data, shot.width, shot.height) == (
        one_screen.screenshot().png_bytes,
        1280,
        720,
    )
    assert shot.art_path is not None
    assert _FILENAME_RE.match(shot.art_path.name)
    assert parse_art(shot.art_path.read_text(encoding="utf-8")).meta["screen"] == "wl-1"


def test_screenshot_tool_result_carries_the_saved_to_line(one_screen, monkeypatch) -> None:
    # Given: a provider whose format can carry image tool results
    monkeypatch.setattr("miniouto.tools.registry._SCREENSHOTS_SUPPORTED", True)

    # When: the registered handler runs the screenshot action
    blocks = _computer_handler(action="screenshot", screen="wl-1")

    # Then: the text block names the .art file in the screen action's line
    # shape, on its own last line — what the TUI keys on to render the screen —
    # and the raw-PNG sibling rides the line just above it
    text = blocks[0].text
    assert text.splitlines()[-1].startswith("Saved to: ")
    assert art_path_in_result(text) == Path(
        text.split("Saved to: ", 1)[1].splitlines()[-1]
    ).as_posix()
    assert "Image saved to: " in text


def test_screenshot_art_failure_costs_only_the_saved_to_line(one_screen, monkeypatch) -> None:
    # Given: an art renderer that cannot cope with the capture
    monkeypatch.setattr(computer_mod, "_render_screen_art", _broken_art)
    monkeypatch.setattr("miniouto.tools.registry._SCREENSHOTS_SUPPORTED", True)

    # When: the screenshot action runs, directly and through the handler
    shot = computer("screenshot")
    blocks = _computer_handler(action="screenshot", screen="wl-1")

    # Then: the pixels still come back with no art path, and no line appended
    # for a sibling that was never written — the png write shares the broken
    # render only if it too breaks, which it does not, so the png line stays
    assert (shot.width, shot.art_path) == (1280, None)
    assert len(blocks) == 2
    assert art_path_in_result(blocks[0].text) is None
    # the art failure costs ONLY the art line; the raw png sibling is
    # independent of the art renderer and is still written and named
    assert blocks[0].text.rstrip().endswith(".png")


def test_screenshot_chars_and_color_reach_the_art_render(one_screen, isolated_screens_dir) -> None:
    # Given: a narrower, monochrome capture
    # When: the screenshot action renders its art
    shot = computer("screenshot", chars=40, color=False)

    # Then: the .art file honours both parameters
    art = parse_art(shot.art_path.read_text(encoding="utf-8"))
    assert art.meta["mode"] == "ramp-plain"
    assert art.cols == 40
    assert "\x1b" not in art.body


def _edge_png(width: int = 128, height: int = 80, blend_from: int = 32) -> bytes:
    """A two-tone capture holding both a hard edge and a soft one.

    Left of `blend_from` the frame alternates black and white every four
    source pixels, so at 16 cells each cell straddles a hard edge in two clean
    halves. Right of it the greys step every four pixels by twenty, so those cells hold
    two tones too close to separate cleanly — which is what leaves the glyph
    family, and only the glyph family, to decide blocks against dots.
    """
    def pixel(x: int) -> tuple[int, int, int, int]:
        if x < blend_from:
            return (255, 255, 255, 255) if (x // 4) % 2 else (0, 0, 0, 255)
        level = 40 + ((x // 4) % 8) * 20
        return (level, level, level, 255)

    return make_png([[pixel(x) for x in range(width)] for _ in range(height)], 6)


def test_screen_art_mode_is_subcell_truecolor(one_screen, isolated_screens_dir) -> None:
    # Given: one screen up
    # When: the screen action writes its art
    result = computer("screen", chars=40)

    # Then: the header names the new mode and the file records it
    art = parse_art(
        Path(result.split("Saved to: ", 1)[1].split("\n", 1)[0]).read_text("utf-8")
    )
    assert art.meta["mode"] == "subcell-truecolor"
    assert "subcell truecolor" in result


def test_glyph_braille_threads_through_the_screen_action(
    one_screen, monkeypatch, isolated_screens_dir
) -> None:
    # Given: a screen whose capture is a hard two-tone edge
    png = _edge_png()
    monkeypatch.setattr(
        computer_mod,
        "_resolve_screen",
        lambda screen, *, autospawn: ("wl-1", FakeCompositor(png, 128, 80)),
    )

    # When: the screen action runs with the font-safe glyph family
    result = computer("screen", chars=16, glyph="braille")

    # Then: the written art is dots only — no Unicode-16 octant anywhere
    body = strip_sgr(
        parse_art(
            Path(result.split("Saved to: ", 1)[1].split("\n", 1)[0]).read_text("utf-8")
        ).body
    )
    assert body.strip()
    assert not [ch for ch in body if OCTANT_FIRST_CODEPOINT <= ord(ch) <= OCTANT_LAST_CODEPOINT]


def test_glyph_octant_forces_blocks_where_a_mask_applies(isolated_screens_dir) -> None:
    # Given: the same capture rendered with the octant family forced
    _result, art_text, _path = computer_mod._render_screen_art(
        _edge_png(), "wl-1", 16, True, "octant"
    )
    forced = strip_sgr(parse_art(art_text).body)

    # Then: octant codepoints appear — more of them than "mix" ever emits, the
    # rest being the masks Unicode leaves unencoded, which degrade to dots
    mixed = strip_sgr(
        parse_art(computer_mod._render_screen_art(_edge_png(), "wl-1", 16, True)[1]).body
    )
    octants = [
        ch for ch in forced if OCTANT_FIRST_CODEPOINT <= ord(ch) <= OCTANT_LAST_CODEPOINT
    ]
    mixed_octants = [
        ch for ch in mixed if OCTANT_FIRST_CODEPOINT <= ord(ch) <= OCTANT_LAST_CODEPOINT
    ]
    assert len(octants) > len(mixed_octants) > 0


def test_unknown_glyph_falls_back_to_the_default_mix(isolated_screens_dir) -> None:
    # Given: a glyph value that is not one of the three
    _r, wrong, _p = computer_mod._render_screen_art(_edge_png(), "wl-1", 16, True, "nonsense")

    # Then: it renders exactly like the default does, never raising
    _r, default, _p = computer_mod._render_screen_art(_edge_png(), "wl-1", 16, True)
    assert parse_art(wrong).body == parse_art(default).body


def test_captures_are_pruned_to_the_newest_cap(one_screen, tmp_path, monkeypatch) -> None:
    # Given: a screens dir already holding 100 capture files (50 pairs)
    monkeypatch.setattr(computer_mod, "COMPUTER_SCREENS_DIR", tmp_path)
    for i in range(50):
        stem = f"wl-1-2026-01-01-00-00-{i:02d}"
        (tmp_path / f"{stem}.art").write_text("old", encoding="utf-8")
        (tmp_path / f"{stem}.png").write_bytes(b"old")

    # When: one more screenshot lands (png + art = 102 files)
    computer("screenshot")

    # Then: the pool is back at the cap, and the oldest pair went together
    files = sorted(tmp_path.glob("*.*"))
    assert len(files) == 100
    assert not (tmp_path / "wl-1-2026-01-01-00-00-00.art").exists()
    assert not (tmp_path / "wl-1-2026-01-01-00-00-00.png").exists()
    assert (tmp_path / "wl-1-2026-01-01-00-00-02.art").exists()


def test_a_full_prune_dir_never_breaks_the_capture(one_screen, tmp_path, monkeypatch) -> None:
    # Given: a screens dir that cannot be listed or unlinked (prune failure)
    monkeypatch.setattr(computer_mod, "COMPUTER_SCREENS_DIR", tmp_path)
    monkeypatch.setattr(
        computer_mod, "_prune_captures", lambda: (_ for _ in ()).throw(OSError("boom"))
    )

    # When / Then: the screenshot still succeeds — pruning is best effort
    shot = computer("screenshot")
    assert shot.art_path is not None and shot.art_path.is_file()
