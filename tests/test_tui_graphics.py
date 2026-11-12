"""Graphics layer tests for the TUI image lane.

The image lane is escape sequences emitted AFTER each frame: the TUI paints its
own screen, so a picture is placed the way the terminal's graphics protocol
defines it, out of band. These tests pin the four things that make that safe and
cheap:

* the mode is probed ONCE (Sixel before TGP) before the terminal owns stdin, and
  the render path never probes;
* the kitty pixels are uploaded ONCE and merely RE-PLACED per repaint — the whole
  reason `cli/graphics.py:CachedTGPImage` existed, since re-uploading a 1280x720
  capture cost ~1.8 MB per frame and made scrolling lag. The grid is also
  ENCODED once, at creation;
* the image is DELETEd from the terminal when its row goes away (`xnano_image`
  never sends `a=d`, so the lane must);
* every failure (no probe answer, missing PNG, bad protocol) degrades to the TEXT
  lane, because the `.art` text render is always available and the image is a
  bonus that must never raise into the render path.

`os.write` is the single transport choke point, so most assertions capture that
one call instead of sniffing a real terminal; the probe is behind
`_probe_terminal_graphics`, the one seam a test can replace (the xnano types are
immutable native classes).
"""

from __future__ import annotations

import subprocess
import sys
from typing import NamedTuple

import pytest

from miniouto.cli.tui import graphics as gfx

sys.path.insert(0, "tests")
from test_ascii_art import make_png

# A 64x36 capture, the shape of a real (scaled-down) desktop screenshot.
PNG_W, PNG_H = 64, 36


class CellSize(NamedTuple):
    """A stand-in for a probed cell size: 10x20 keeps the maths arithmetic."""

    width: int
    height: int


def _png(tmp_path, width: int = PNG_W, height: int = PNG_H):
    path = tmp_path / "shot.png"
    path.write_bytes(make_png([[(200, 40, 40, 255)] * width for _ in range(height)], 6))
    return path


@pytest.fixture
def cell_size() -> CellSize:
    return CellSize(10, 20)


@pytest.fixture
def writes(monkeypatch) -> list[bytes]:
    """Capture everything the overlay would put on stdout."""

    captured: list[bytes] = []
    monkeypatch.setattr(gfx, "_write", captured.append)
    return captured


@pytest.fixture(autouse=True)
def _reset_mode(monkeypatch):
    """Every test starts undetected, so caching is what a test proves, not luck."""

    monkeypatch.setattr(gfx, "_GRAPHICS_MODE", None, raising=False)
    monkeypatch.setattr(gfx, "_DETECTED", None, raising=False)
    # A writer seam left set would silently capture the next test's bytes.
    monkeypatch.setattr(gfx, "_WRITER", None, raising=False)
    # tmux's passthrough envelope would wrap every command; keep the raw bytes.
    monkeypatch.delenv("TMUX", raising=False)


def _caps(*protocols: str, cell_size: tuple[int, int] = (10, 20)) -> gfx._Capabilities:
    return gfx._Capabilities(frozenset(protocols), cell_size)


def _probe(monkeypatch, capabilities: gfx._Capabilities) -> None:
    monkeypatch.setattr(gfx, "_probe_terminal_graphics", lambda: capabilities)


# ── mode detection: one probe, before the render path, never again ──────────


def test_a_failed_probe_is_the_text_lane_not_an_error(monkeypatch) -> None:
    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(gfx, "_probe_terminal_graphics", boom)
    assert gfx.detect_graphics_mode() == gfx.GRAPHICS_MODE_TEXT
    assert gfx.terminal_graphics_supported() is False


def test_sixel_wins_when_the_terminal_offers_both(monkeypatch) -> None:
    """Konsole and wezterm REPORT TGP but do not implement its placement.

    Their placeholder scheme is broken, so a TGP image there renders nothing
    while Sixel works. Sixel therefore wins whenever it is available; TGP is for
    terminals with Sixel absent — in practice Kitty.
    """

    _probe(monkeypatch, _caps("sixel", "kitty"))
    assert gfx.detect_graphics_mode() == gfx.GRAPHICS_MODE_SIXEL


def test_tgp_is_used_when_sixel_is_absent(monkeypatch) -> None:
    _probe(monkeypatch, _caps("kitty"))
    assert gfx.detect_graphics_mode() == gfx.GRAPHICS_MODE_TGP


def test_no_protocol_at_all_is_text(monkeypatch) -> None:
    _probe(monkeypatch, _caps())
    assert gfx.detect_graphics_mode() == gfx.GRAPHICS_MODE_TEXT
    assert gfx.graphics_mode() == gfx.GRAPHICS_MODE_TEXT
    assert gfx.terminal_graphics_supported() is False


def test_detection_is_cached_so_the_probe_runs_once(monkeypatch) -> None:
    calls: list[int] = []

    def probe():
        calls.append(1)
        return _caps("kitty")

    monkeypatch.setattr(gfx, "_probe_terminal_graphics", probe)
    assert gfx.detect_graphics_mode() == gfx.GRAPHICS_MODE_TGP
    assert gfx.detect_graphics_mode() == gfx.GRAPHICS_MODE_TGP
    assert len(calls) == 1


def test_detection_remembers_the_cell_size_for_the_encoders(monkeypatch) -> None:
    """The probed cell size is what turns a cell count into a pixel box.

    Losing it would make every placement fall back to 10x20 and derive a height
    from the wrong aspect — a picture one row taller than the rect the app blanked.
    """

    _probe(monkeypatch, _caps("kitty", cell_size=(9, 21)))
    assert gfx.detect_graphics_mode() == gfx.GRAPHICS_MODE_TGP
    assert gfx.cell_size_in_pixels() == (9, 21)


def test_an_undetected_terminal_has_the_default_cell_size(monkeypatch) -> None:
    _probe(monkeypatch, _caps())
    assert gfx.detect_graphics_mode() == gfx.GRAPHICS_MODE_TEXT
    assert gfx.cell_size_in_pixels() == (10, 20)


def test_detection_warms_the_protocol_imports(monkeypatch) -> None:
    """The libraries' import is paid at boot, not on the screenshot the user waits for."""

    warmed: list[int] = []
    _probe(monkeypatch, _caps("kitty"))
    monkeypatch.setattr(gfx, "_warm_protocol_imports", lambda: warmed.append(1))
    assert gfx.detect_graphics_mode() == gfx.GRAPHICS_MODE_TGP
    assert warmed == [1]


def test_a_broken_protocol_import_degrades_to_text(monkeypatch) -> None:
    _probe(monkeypatch, _caps("kitty"))

    def boom() -> None:
        raise ImportError("xnano_image vanished")

    monkeypatch.setattr(gfx, "_warm_protocol_imports", boom)
    assert gfx.detect_graphics_mode() == gfx.GRAPHICS_MODE_TEXT
    # …and nothing downstream thinks it can draw
    assert gfx.cell_size_in_pixels() == (10, 20)


def test_the_render_path_never_probes(monkeypatch) -> None:
    """A probe is a terminal round-trip; running one per frame is the stall.

    `graphics_mode()` must only READ the detected value, and an undetected
    terminal is the safe text lane.
    """

    def boom():
        raise AssertionError("the render path must not probe the terminal")

    monkeypatch.setattr(gfx, "_probe_terminal_graphics", boom)
    assert gfx.graphics_mode() == gfx.GRAPHICS_MODE_TEXT
    assert gfx.terminal_graphics_supported() is False


def test_the_module_imports_without_textual_or_the_image_libraries() -> None:
    """The module must stay importable in a runtime with no Textual at all.

    Checked in a SUBPROCESS: an in-process check would really be asserting that no
    OTHER test had imported Textual yet, which is ordering, not the contract. The
    image libraries are lazy on purpose too — the module is imported by every chat
    row, and only a graphics terminal should pay for them.
    """

    code = (
        "import sys; import miniouto.cli.tui.graphics; "
        "print(sorted(m.split('.')[0] for m in sys.modules "
        "if m.split('.')[0] in {'textual', 'textual_image'}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


# ── sizing: the capture's pixels against the terminal's cells ───────────────


def test_natural_cell_size_is_the_pixel_size_over_the_cell_size(
    tmp_path, cell_size, writes
) -> None:
    # 64x36 pixels on a 10x20 cell grid -> 6x2 cells
    placement = gfx.ImagePlacement(
        _png(tmp_path), cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    assert (placement.width_cells, placement.height_cells) == (6, 2)


def test_a_fixed_width_derives_the_height_from_the_aspect_ratio(
    tmp_path, cell_size, writes
) -> None:
    """The old widget's `width: 100%` + `height: auto`, expressed in cells.

    A fixed cell count must never overflow the log's width, so the width is what
    the caller pins and the height follows the capture's aspect.
    """

    placement = gfx.ImagePlacement(
        _png(tmp_path), 40, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    assert placement.width_cells == 40
    # 40 cells x 10 px = 400 px wide; 64:36 aspect => 225 px tall => 11 cells
    assert placement.height_cells == 11


def test_the_cell_size_the_placement_was_built_with_is_reported_back(
    tmp_path, cell_size, writes
) -> None:
    """The app blanks exactly `width_cells x height_cells`; it must be the grid's size."""

    placement = gfx.ImagePlacement(
        _png(tmp_path), 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    probe = gfx.ImagePlacement(
        _png(tmp_path), 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    assert (placement.width_cells, placement.height_cells) == (20, 6)
    # …deterministic: the same image and box encode to the same rect
    assert (probe.width_cells, probe.height_cells) == (20, 6)


def test_the_placement_remembers_its_png_and_protocol(tmp_path, cell_size, writes) -> None:
    path = _png(tmp_path)
    placement = gfx.ImagePlacement(path, 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP)
    assert placement.png_path == str(path)
    assert placement.protocol == gfx.GRAPHICS_MODE_TGP


@pytest.mark.parametrize("protocol", [gfx.GRAPHICS_MODE_TEXT, "nonsense"])
def test_an_unsupported_protocol_is_rejected_not_half_rendered(
    tmp_path, cell_size, protocol
) -> None:
    with pytest.raises(ValueError, match="no graphics protocol"):
        gfx.ImagePlacement(_png(tmp_path), 20, cell_size=cell_size, protocol=protocol)


def test_a_missing_png_is_the_text_lane_not_an_exception(tmp_path, cell_size) -> None:
    assert (
        gfx.ImagePlacement.create(
            tmp_path / "absent.png", 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
        )
        is None
    )


def test_an_undecodable_png_is_the_text_lane_too(tmp_path, cell_size) -> None:
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a png at all")
    assert (
        gfx.ImagePlacement.create(
            broken, 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
        )
        is None
    )


def test_create_swallows_a_bad_protocol_too(tmp_path, cell_size) -> None:
    assert (
        gfx.ImagePlacement.create(
            _png(tmp_path), 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TEXT
        )
        is None
    )


def test_in_text_mode_create_declines_so_the_row_uses_the_art_lane(
    tmp_path, cell_size, monkeypatch
) -> None:
    """GRAPHICS_MODE_TEXT is not an error — it is the `.art` lane taking over."""

    monkeypatch.setattr(gfx, "_GRAPHICS_MODE", gfx.GRAPHICS_MODE_TEXT)
    assert gfx.ImagePlacement.create(_png(tmp_path), 20, cell_size=cell_size) is None


# ── TGP lane: encode once, upload once, re-place many ───────────────────────


def _grid_myers(rows: list[str]) -> str:
    return "\n".join(rows)


def test_the_grid_is_encoded_exactly_once(tmp_path, cell_size, writes, monkeypatch) -> None:
    """Decoding + scaling + base64 is the expensive step; it must not repeat.

    A repaint, a scroll and a resize all reuse the same grid, because the grid is
    a function of the image and the cell size — both fixed at creation.
    """

    import xnano_image

    opens: list[str] = []
    real_open = xnano_image.TerminalImage.open

    def counting(path):
        opens.append(str(path))
        return real_open(path)

    monkeypatch.setattr(xnano_image.TerminalImage, "open", staticmethod(counting))
    placement = gfx.ImagePlacement(
        _png(tmp_path), 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    for _ in range(4):
        placement.emit_at(0, 0)
    assert len(opens) == 1


def test_the_first_emit_uploads_and_every_later_emit_re_places(
    tmp_path, cell_size, writes
) -> None:
    """`a=T` ONCE, then `a=p` forever — the upload-once guarantee.

    Re-sending the transmission is the expensive mistake: a 1280x720 capture is
    ~1.8 MB of base64 per repaint, which is what made scrolling lag.
    """

    placement = gfx.ImagePlacement(
        _png(tmp_path), 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    first = placement.emit_at(2, 3)
    later = [placement.emit_at(2, 3) for _ in range(4)]

    assert first.count(b"a=T") == 1
    assert all(b"a=T" not in frame for frame in later)
    assert all(frame.count(b"a=p") == 1 for frame in later)
    # …and the re-place is CHEAP: kilobytes of placeholders, not the payload
    assert len(first) > 20 * len(later[0])


def test_the_re_place_keeps_the_uploaded_image_id(tmp_path, cell_size, writes) -> None:
    """`a=p` must address the SAME image the upload created, or nothing shows."""

    placement = gfx.ImagePlacement(
        _png(tmp_path), 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    upload = placement.emit_at(0, 0)
    image_id = placement.image_id
    assert image_id is not None
    place = placement.emit_at(0, 1)
    assert f"i={image_id}".encode() in upload
    assert f"i={image_id}".encode() in place
    assert b"a=p" in place
    # the place command carries the geometry the grid was encoded with
    assert f"c={placement.width_cells}".encode() in place
    assert f"r={placement.height_cells}".encode() in place


def test_moving_a_placement_only_rewrites_the_cursor_addresses(
    tmp_path, cell_size, writes
) -> None:
    """Scrolling and resizing MOVE an image; they never re-upload or re-encode.

    The grid is a function of the image and its cell size, both fixed at creation,
    so a placement that lands at a new frame cell re-emits the same rows with new
    CUPs. That is what makes scrolling a log full of screenshots cheap.
    """

    import re

    placement = gfx.ImagePlacement(
        _png(tmp_path), 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    placement.emit_at(0, 0)  # the upload
    first = placement.emit_at(0, 0)
    moved = placement.emit_at(11, 27)
    strip = lambda data: re.sub(rb"\x1b\[\d+;\d+H", b"<CUP>", data)  # noqa: E731
    assert strip(first) == strip(moved)
    assert first != moved
    assert b"a=T" not in moved and b"a=p" in moved


def test_each_placement_uploads_once_and_gets_its_own_image_id(
    tmp_path, cell_size, writes
) -> None:
    """A placement owns an upload: two rows mean two images, so two ids.

    Sharing an id would let the second image overwrite the first's pixels in the
    terminal's store.
    """

    path = _png(tmp_path)
    first = gfx.ImagePlacement(path, 8, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP)
    second = gfx.ImagePlacement(path, 8, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP)

    first.emit_at(0, 0)
    second.emit_at(0, 2)
    first.emit_at(0, 0)

    assert first.image_id is not None
    assert second.image_id is not None
    assert first.image_id != second.image_id


def test_the_upload_is_a_single_transmission_of_the_whole_grid(
    tmp_path, cell_size, writes
) -> None:
    """One transmission per placement, with the payload chunked inside it.

    Kitty caps a control sequence at 4096 base64 bytes, so the payload arrives as
    a chain of `m=1` chunks ending in `m=0`. All of them belong to the SAME upload
    and must travel together — a chunk re-sent on a repaint would be the bug.
    """

    placement = gfx.ImagePlacement(
        _png(tmp_path), 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    upload = placement.emit_at(0, 0)
    assert upload.count(b"a=T") == 1
    assert b",m=0" in upload  # the chunk chain is terminated
    reupload = placement.emit_at(0, 0)
    assert b",m=" not in reupload  # no chunk of that chain is re-sent


def test_the_grid_geometry_matches_the_reported_cell_rect(tmp_path, cell_size, writes) -> None:
    """The pixels the terminal is told to draw are the cells the app blanked."""

    placement = gfx.ImagePlacement(
        _png(tmp_path), 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    upload = placement.emit_at(0, 0)
    # 20 cells x 10 px wide, 6 cells x 20 px tall
    assert b"s=200,v=120" in upload


def test_a_zero_sized_placement_emits_nothing(tmp_path, cell_size, writes) -> None:
    """Nothing to show must not move the cursor or write an empty command."""

    placement = gfx.ImagePlacement(
        _png(tmp_path), 0, 0, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    overlay = gfx.GraphicsOverlay()
    overlay.add(placement, 0, 0)
    overlay.emit_post_draw()
    assert writes == []
    assert (placement.width_cells, placement.height_cells) == (0, 0)


# ── Sixel lane: encode once, re-emit the bytes ──────────────────────────────


def test_sixel_encodes_once_and_re_emits_the_cached_bytes(
    tmp_path, cell_size, writes, monkeypatch
) -> None:
    import xnano_image

    encodings: list[int] = []
    real_grid = xnano_image.TerminalImage.to_cell_grid

    def counting(self, *args, **kwargs):
        encodings.append(1)
        return real_grid(self, *args, **kwargs)

    monkeypatch.setattr(xnano_image.TerminalImage, "to_cell_grid", counting)
    placement = gfx.ImagePlacement(
        _png(tmp_path), 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_SIXEL
    )
    first = placement.sixel_bytes()
    second = placement.sixel_bytes()
    assert first is second
    assert len(encodings) == 1
    # a repaint re-emits the SAME bytes: no re-encode, no growth
    assert placement.emit_at(1, 2).count(first) == 1
    assert len(encodings) == 1


def test_sixel_bytes_are_a_dcs_sixel_stream(tmp_path, cell_size, writes) -> None:
    placement = gfx.ImagePlacement(
        _png(tmp_path), 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_SIXEL
    )
    data = placement.sixel_bytes()
    assert data.startswith(b"\x1bP")
    assert data.endswith(b"\x1b\\")
    assert b"q" in data[:20]  # the DCS header's sixel introducer


def test_a_sixel_placement_has_no_kitty_rows(tmp_path, cell_size, writes) -> None:
    """Sixel is a byte stream, not a placeholder grid: there is nothing to place."""

    placement = gfx.ImagePlacement(
        _png(tmp_path), 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_SIXEL
    )
    assert placement.rows_ansi() == []
    assert placement.image_id is None
    assert b"\x1b_G" not in placement.emit_at(0, 0)


def test_sixel_needs_no_delete_on_cleanup(tmp_path, cell_size, writes) -> None:
    """The bytes are not retained by the terminal, so there is nothing to free."""

    placement = gfx.ImagePlacement(
        _png(tmp_path), 20, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_SIXEL
    )
    placement.emit_at(0, 0)
    placement.sixel_bytes()
    placement.cleanup()
    assert not [w for w in writes if b"\x1b_Ga=d," in w]


# ── the overlay: blank the rect, then paint it after the draw ───────────────


def test_clear_regions_are_the_cell_rects_the_app_must_blank(
    tmp_path, cell_size, writes
) -> None:
    """The frame's diff owns every cell, so the image rect must be drawn as spaces.

    Without the blank, log text paints over the pixels and the next diff rewrites
    the image's cells as blanks and erases it.
    """

    placement = gfx.ImagePlacement(
        _png(tmp_path), 8, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    overlay = gfx.GraphicsOverlay()
    overlay.add(placement, 3, 7)
    assert overlay.clear_regions() == [(3, 7, placement.width_cells, placement.height_cells)]


def test_several_placements_report_several_regions(tmp_path, cell_size, writes) -> None:
    one = gfx.ImagePlacement(_png(tmp_path), 8, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP)
    two = gfx.ImagePlacement(_png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP)
    overlay = gfx.GraphicsOverlay()
    overlay.add(one, 0, 0)
    overlay.add(two, 10, 20)
    assert overlay.clear_regions() == [
        (0, 0, one.width_cells, one.height_cells),
        (10, 20, two.width_cells, two.height_cells),
    ]


def test_the_kitty_overlay_emit_is_save_move_rows_restore(tmp_path, cell_size, writes) -> None:
    placement = gfx.ImagePlacement(
        _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    # warm the upload first: the FIRST frame legitimately transmits
    placement.emit_at(0, 0)
    overlay = gfx.GraphicsOverlay()
    overlay.add(placement, 2, 5)
    writes.clear()
    overlay.emit_post_draw()

    assert len(writes) == 1  # one syscall per frame, not one per placement
    emitted = writes[0]
    assert emitted.startswith(b"\x1b7")  # save cursor
    assert b"\x1b[6;3H" in emitted  # CUP to the 1-based cell
    assert emitted.endswith(b"\x1b8")  # restore cursor, leave the canvas's position
    assert emitted.count(b"\x1b_G") == 1  # place only, one row of cells
    assert b"a=T" not in emitted


def test_the_sixel_overlay_emit_moves_writes_bytes_and_restores(
    tmp_path, cell_size, writes
) -> None:
    placement = gfx.ImagePlacement(
        _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_SIXEL
    )
    sixels = placement.sixel_bytes()
    overlay = gfx.GraphicsOverlay()
    overlay.add(placement, 1, 2)
    writes.clear()
    overlay.emit_post_draw()

    emitted = writes[0]
    assert emitted.startswith(b"\x1b7\x1b[3;2H")
    assert sixels in emitted
    assert emitted.endswith(b"\x1b8")


def test_an_empty_overlay_writes_nothing(writes) -> None:
    gfx.GraphicsOverlay().emit_post_draw()
    assert writes == []


def test_a_broken_placement_costs_the_image_not_the_frame(
    tmp_path, cell_size, writes, monkeypatch
) -> None:
    good = gfx.ImagePlacement(
        _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    bad = gfx.ImagePlacement(
        _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )

    def explode(x, y):
        raise RuntimeError("encode blew up mid-frame")

    monkeypatch.setattr(bad, "emit_at", explode)
    overlay = gfx.GraphicsOverlay()
    overlay.add(bad, 0, 0)
    overlay.add(good, 5, 5)
    good.emit_at(0, 0)  # warm the upload so this frame only places
    writes.clear()
    overlay.emit_post_draw()
    assert len(writes) == 1
    assert b"\x1b[6;6H" in writes[0]  # the healthy placement still painted


def test_a_dead_stdout_never_raises_into_the_render_path(tmp_path, cell_size, monkeypatch) -> None:
    def boom(data: bytes) -> None:
        raise OSError("stdout is gone")

    monkeypatch.setattr(gfx, "_write", boom)
    placement = gfx.ImagePlacement(
        _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    overlay = gfx.GraphicsOverlay()
    overlay.add(placement, 0, 0)
    overlay.emit_post_draw()  # must not raise


def test_one_overlay_can_be_pinned_to_a_writer(tmp_path, cell_size, writes) -> None:
    """A caller that owns one frame's output redirects just that overlay."""

    routed: list[bytes] = []
    placement = gfx.ImagePlacement(
        _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    overlay = gfx.GraphicsOverlay(write=routed.append)
    overlay.add(placement, 1, 1)
    overlay.emit_post_draw()
    assert len(routed) == 1
    assert writes == []
    # …and the same seam can be pointed somewhere else later
    other: list[bytes] = []
    overlay.set_writer(other.append)
    overlay.emit_post_draw()
    assert len(other) == 1
    assert len(routed) == 1


def test_the_module_writer_takes_the_delete_as_well_as_the_frames(
    tmp_path, cell_size, writes, monkeypatch
) -> None:
    """The transport has to be ONE seam, or a removed row's delete escapes it.

    Two very different moments emit bytes: the frame's placements and the delete a
    removal sends afterwards. A backend that owns the output — the real-screen
    terminal, a recorder — must capture BOTH, so the seam is module-level and
    ``cleanup()`` goes through it.
    """

    routed: list[bytes] = []
    monkeypatch.setattr(gfx, "_WRITER", None, raising=False)
    gfx.set_writer(routed.append)
    try:
        placement = gfx.ImagePlacement(
            _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
        )
        placement.emit_at(0, 0)
        overlay = gfx.GraphicsOverlay()
        overlay.add(placement, 0, 0)
        overlay.emit_post_draw()
        placement.cleanup()
    finally:
        gfx.set_writer(None)
    # Then: the frame AND the delete both went to the caller's writer
    assert len(routed) == 2
    assert b"a=p" in routed[0]
    assert b"a=d,d=I" in routed[1]
    assert writes == []


def test_setting_the_writer_back_to_none_restores_stdout(
    tmp_path, cell_size, writes, monkeypatch
) -> None:
    """The seam must be releasable, or one test (or one screen) captures the next."""

    monkeypatch.setattr(gfx, "_WRITER", None, raising=False)
    routed: list[bytes] = []
    gfx.set_writer(routed.append)
    gfx.set_writer(None)
    placement = gfx.ImagePlacement(
        _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    placement.emit_at(0, 0)
    placement.cleanup()
    assert routed == []
    assert [w for w in writes if b"a=d,d=I" in w]


# ── lifecycle: the terminal's image store is freed when the row goes ────────


def test_cleanup_deletes_the_image_from_the_terminal(tmp_path, cell_size, writes) -> None:
    """A kitty terminal keeps the pixels until told otherwise.

    `xnano_image` never sends a delete and dropping its buffer sends nothing, so
    without this every capture ever expanded would sit in the terminal's image
    store for the life of the session.
    """

    placement = gfx.ImagePlacement(
        _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    placement.emit_at(0, 0)
    image_id = placement.image_id
    assert image_id is not None
    writes.clear()

    placement.cleanup()
    deletes = [w for w in writes if b"\x1b_Ga=d," in w]
    assert len(deletes) == 1
    assert f"i={image_id}".encode() in deletes[0]
    assert b"d=I" in deletes[0]
    assert placement.image_id is None


def test_a_re_emit_after_cleanup_writes_nothing(tmp_path, cell_size, writes) -> None:
    """The row is gone; a late frame must not place a deleted image."""

    placement = gfx.ImagePlacement(
        _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    placement.emit_at(0, 0)
    placement.cleanup()
    writes.clear()
    overlay = gfx.GraphicsOverlay()
    overlay.add(placement, 0, 0)
    overlay.emit_post_draw()
    assert writes == []


def test_cleanup_is_idempotent(tmp_path, cell_size, writes) -> None:
    placement = gfx.ImagePlacement(
        _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    placement.emit_at(0, 0)
    placement.cleanup()
    writes.clear()
    placement.cleanup()
    assert writes == []


def test_cleanup_of_a_never_uploaded_placement_deletes_nothing(
    tmp_path, cell_size, writes
) -> None:
    """Nothing was ever sent, so there is nothing in the terminal to free."""

    placement = gfx.ImagePlacement(
        _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    placement.cleanup()
    assert writes == []
    assert placement.image_id is None


def test_a_failed_delete_still_drops_the_id(tmp_path, cell_size, monkeypatch) -> None:
    """A dead stdout must not make the app try to delete the same image twice."""

    placement = gfx.ImagePlacement(
        _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    placement.emit_at(0, 0)
    assert placement.image_id is not None

    def boom(data: bytes) -> None:
        raise OSError("stdout is gone")

    monkeypatch.setattr(gfx, "_write", boom)
    placement.cleanup()
    assert placement.image_id is None


def test_the_row_releases_the_placement_through_cleanup(tmp_path, cell_size, writes) -> None:
    """`ToolRow.release_image()` is the caller; it must reach the delete.

    Wiring check across the two modules: a row that goes away must not leave the
    picture behind in the terminal's store.
    """

    from miniouto.cli.tui import rows as rows_module

    placement = gfx.ImagePlacement(
        _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    placement.emit_at(0, 0)
    assert placement.image_id is not None
    row = rows_module.ToolRow("Computer screenshot", tool_name="Computer")
    row.image = placement
    row.image_visible = True
    writes.clear()
    row.release_image()
    assert row.image is None
    assert [w for w in writes if b"a=d,d=I" in w]


# ── the transport itself ────────────────────────────────────────────────────


def test_tgp_messages_are_well_formed() -> None:
    message = gfx._tgp_message(i=7, a="d", d="I")
    assert message.startswith(b"\x1b_G")
    assert message.endswith(b"\x1b\\")
    assert b"i=7,a=d,d=I" in message


def test_the_move_is_a_one_based_cup() -> None:
    # cell (0, 0) is the terminal's row 1, column 1
    assert gfx._move_to(0, 0) == b"\x1b[1;1H"
    assert gfx._move_to(3, 5) == b"\x1b[6;4H"


def test_tmux_gets_the_passthrough_envelope_and_plain_output_does_not(
    monkeypatch,
) -> None:
    """tmux drops an APC/DCS it does not implement, so the commands are wrapped.

    Only the protocol RUNS are wrapped: SGR and the placeholder text are ordinary
    pane output and must stay unwrapped, and with no tmux every byte is unchanged.
    """

    payload = b'\x1b[0;38;2;1;2;3m\x1b_Gq=2,i=5,a=p\x1b\\'
    assert gfx._wrap_for_tmux(payload) == payload

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    wrapped = gfx._wrap_for_tmux(payload)
    assert wrapped.startswith(b"\x1b[0;38;2;1;2;3m")  # the SGR is untouched
    assert b"\x1bPtmux;\x1b\x1b_Gq=2,i=5,a=p\x1b\x1b\\\x1b\\" in wrapped


def test_the_overlay_wraps_for_tmux_too(tmp_path, cell_size, writes, monkeypatch) -> None:
    """The envelope must survive the emit path, not only the helper."""

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    placement = gfx.ImagePlacement(
        _png(tmp_path), 4, cell_size=cell_size, protocol=gfx.GRAPHICS_MODE_TGP
    )
    placement.emit_at(0, 0)
    writes.clear()
    overlay = gfx.GraphicsOverlay()
    overlay.add(placement, 0, 0)
    overlay.emit_post_draw()
    assert len(writes) == 1
    assert writes[0].count(b"\x1bPtmux;") == 1
