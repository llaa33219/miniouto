"""Lock the screen-art codec contract from src/miniouto/tools/ascii_art.py.

Every PNG fixture is BUILT here by the tiny encoder below (zlib + struct +
CRC, no Pillow) so the decoder under test has exactly known pixels to prove
itself against — an encoder that shares a bug with the decoder proves
nothing, so the encoder is written independently and straightforwardly.
"""

from __future__ import annotations

import re
import struct
import zlib

import pytest

from miniouto.tools import ascii_art
from miniouto.tools.ascii_art import (
    OCTANT_FIRST_CODEPOINT,
    OCTANT_GLYPHS,
    OCTANT_LAST_CODEPOINT,
    ArtFile,
    art_to_plain,
    build_art,
    decode_png_rgba,
    parse_art,
    render_halfblock,
    render_subcell,
    strip_sgr,
    subcell_rows,
)

# ── a minimal, independent PNG encoder (test fixture only) ───────────────────

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _pack_row(pixels: list[tuple[int, ...]], color_type: int) -> bytes:
    out = bytearray()
    for px in pixels:
        if color_type == 0:
            out.append(px[0])
        elif color_type == 2:
            out.extend(px[:3])
        elif color_type == 3:
            out.append(px[0])
        elif color_type == 4:
            out.extend((px[0], px[1]))
        else:
            out.extend(px[:4])
    return bytes(out)


def _apply_filter(ftype: int, raw: bytes, prev: bytes, bpp: int) -> bytes:
    stride = len(raw)
    out = bytearray(stride)
    for i in range(stride):
        a = raw[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        x = raw[i]
        if ftype == 0:
            v = x
        elif ftype == 1:
            v = (x - a) & 0xFF
        elif ftype == 2:
            v = (x - b) & 0xFF
        elif ftype == 3:
            v = (x - ((a + b) >> 1)) & 0xFF
        else:
            v = (x - _paeth(a, b, c)) & 0xFF
        out[i] = v
    return bytes(out)


def make_png(
    rows: list[list[tuple[int, ...]]],
    color_type: int,
    *,
    bit_depth: int = 8,
    filters: int | list[int] | None = None,
    palette: bytes | None = None,
    trns: bytes | None = None,
    interlace: int = 0,
) -> bytes:
    """Encode `rows` (one list of pixels per scanline) as a PNG."""
    height = len(rows)
    width = len(rows[0])
    bpp = max(1, _CHANNELS[color_type] * bit_depth // 8)
    if filters is None:
        filters = 0
    if isinstance(filters, int):
        filters = [filters] * height

    packed = [_pack_row(r, color_type) for r in rows]
    stream = bytearray()
    prev = bytes(len(packed[0]))
    for ftype, raw in zip(filters, packed, strict=True):
        stream.append(ftype)
        stream += _apply_filter(ftype, raw, prev, bpp)
        prev = raw

    out = bytearray(_PNG_SIG)
    out += _chunk(
        b"IHDR",
        struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace),
    )
    if palette is not None:
        out += _chunk(b"PLTE", palette)
    if trns is not None:
        out += _chunk(b"tRNS", trns)
    out += _chunk(b"IDAT", zlib.compress(bytes(stream)))
    out += _chunk(b"IEND", b"")
    return bytes(out)


# ── A) PNG decoding ──────────────────────────────────────────────────────────


def test_decode_rgb8_round_trip_gives_exact_pixels() -> None:
    # Given: a known 4x4 RGB8 image (red, green, blue, white, by row)
    rows = [
        [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)],
        [(1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12)],
        [(13, 14, 15), (16, 17, 18), (19, 20, 21), (22, 23, 24)],
        [(25, 26, 27), (28, 29, 30), (31, 32, 33), (34, 35, 36)],
    ]
    png = make_png(rows, 2)

    # When: the decoder decodes it
    width, height, rgba = decode_png_rgba(png)

    # Then: dimensions are right and every pixel round-trips as opaque RGBA
    assert (width, height) == (4, 4)
    assert len(rgba) == 4 * 4 * 4
    expected = bytearray()
    for row in rows:
        for r, g, b in row:
            expected += bytes((r, g, b, 255))
    assert rgba == expected
    assert rgba[(3 * 4 + 0) * 4 : (3 * 4 + 1) * 4] == bytes((25, 26, 27, 255))


def test_decode_rgba8_round_trip_preserves_alpha() -> None:
    # Given: a 2x2 RGBA8 image with distinct alphas
    rows = [
        [(255, 0, 0, 0), (0, 255, 0, 128)],
        [(0, 0, 255, 255), (255, 255, 255, 7)],
    ]
    png = make_png(rows, 6)

    # When: the decoder decodes it
    width, height, rgba = decode_png_rgba(png)

    # Then: RGBA bytes come back unchanged
    assert (width, height) == (2, 2)
    assert rgba == bytearray(
        bytes((255, 0, 0, 0)) + bytes((0, 255, 0, 128))
        + bytes((0, 0, 255, 255)) + bytes((255, 255, 255, 7))
    )


def test_decode_greyscale_expands_to_rgb_and_opaque() -> None:
    # Given: a greyscale (colour type 0) image
    rows = [[(0,), (64,)], [(128,), (255,)]]
    png = make_png(rows, 0)

    # When: the decoder decodes it
    _, _, rgba = decode_png_rgba(png)

    # Then: each grey level is replicated into R, G and B at alpha 255
    assert rgba == bytearray(
        bytes((0, 0, 0, 255)) + bytes((64, 64, 64, 255))
        + bytes((128, 128, 128, 255)) + bytes((255, 255, 255, 255))
    )


def test_decode_greyscale_alpha_keeps_alpha_channel() -> None:
    # Given: a greyscale+alpha (colour type 4) image
    rows = [[(10, 20), (30, 40)], [(50, 60), (70, 80)]]
    png = make_png(rows, 4)

    # When: the decoder decodes it
    _, _, rgba = decode_png_rgba(png)

    # Then: grey and alpha are kept side by side
    assert rgba == bytearray(
        bytes((10, 10, 10, 20)) + bytes((30, 30, 30, 40))
        + bytes((50, 50, 50, 60)) + bytes((70, 70, 70, 80))
    )


def test_decode_palette_uses_plte_and_trns() -> None:
    # Given: a palette image (colour type 3) with a tRNS table
    palette = bytes((255, 0, 0)) + bytes((0, 255, 0)) + bytes((0, 0, 255))
    trns = bytes((255, 128, 0))
    rows = [[(0,), (1,)], [(2,), (0,)]]
    png = make_png(rows, 3, palette=palette, trns=trns)

    # When: the decoder decodes it
    _, _, rgba = decode_png_rgba(png)

    # Then: indices resolve through PLTE and tRNS supplies per-entry alpha
    assert rgba == bytearray(
        bytes((255, 0, 0, 255)) + bytes((0, 255, 0, 128))
        + bytes((0, 0, 255, 0)) + bytes((255, 0, 0, 255))
    )


@pytest.mark.parametrize("ftype", [0, 1, 2, 3, 4])
def test_decode_every_filter_type_defilters_correctly(ftype: int) -> None:
    # Given: a 5x3 RGB8 image encoded with one filter type on every scanline
    rows = [
        [(i * 7 % 256, i * 13 % 256, i * 29 % 256) for i in range(5)],
        [(i * 31 % 256, i * 17 % 256, i * 3 % 256) for i in range(5)],
        [(i * 5 % 256, i * 41 % 256, i * 11 % 256) for i in range(5)],
    ]
    png = make_png(rows, 2, filters=ftype)

    # When: the decoder decodes it
    _, _, rgba = decode_png_rgba(png)

    # Then: the pixels are identical to the unfiltered source
    expected = bytearray()
    for row in rows:
        for r, g, b in row:
            expected += bytes((r, g, b, 255))
    assert rgba == expected


def test_decode_odd_width_has_no_stride_bug() -> None:
    # Given: a 5x3 image — the width is not a multiple of any filter stride
    rows = [[(i, i + 1, i + 2) for i in range(5)] for _ in range(3)]
    png = make_png(rows, 2, filters=3)

    # When: the decoder decodes it
    width, height, rgba = decode_png_rgba(png)

    # Then: dimensions are exact and the row-major RGBA buffer is flat
    assert (width, height) == (5, 3)
    assert len(rgba) == 5 * 3 * 4
    assert rgba[:4] == bytes((0, 1, 2, 255))
    assert rgba[4:8] == bytes((1, 2, 3, 255))
    assert rgba[-4:] == bytes((4, 5, 6, 255))


def test_decode_16_bit_depth_raises_actionable_error() -> None:
    # Given: a PNG declaring 16-bit depth
    png = make_png([[(255, 0, 0)], [(0, 255, 0)]], 2, bit_depth=16)

    # When: the decoder reads it
    with pytest.raises(ValueError, match="16-bit depth"):
        decode_png_rgba(png)


def test_decode_interlaced_raises_actionable_error() -> None:
    # Given: a PNG declaring Adam7 interlacing
    png = make_png([[(255, 0, 0)], [(0, 255, 0)]], 2, interlace=1)

    # When: the decoder reads it
    with pytest.raises(ValueError, match="interlaced"):
        decode_png_rgba(png)


def test_decode_bad_signature_raises_value_error() -> None:
    # Given: bytes that are not a PNG at all
    # When: the decoder reads them
    with pytest.raises(ValueError, match="PNG"):
        decode_png_rgba(b"GIF89a not a png at all")


# ── B) half-block rendering ──────────────────────────────────────────────────


def _solid(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytearray:
    return bytearray(bytes(rgba) * (width * height))


def test_render_halfblock_grid_maths_on_1280x720_at_100_cols() -> None:
    # Given: a flat 1280x720 RGBA frame
    rgba = _solid(1280, 720, (10, 20, 30, 255))

    # When: it is rendered at 100 columns
    body = render_halfblock(rgba, 1280, 720, 100, color=False)

    # Then: rows == round(100*720/1280/2) == 28 and every row is 100 cells
    lines = body.split("\n")
    assert len(lines) == 28
    assert all(len(line) == 100 for line in lines)


def test_render_halfblock_identical_halves_emit_solid_block() -> None:
    # Given: a flat 4x2 frame, so every cell's top and bottom halves agree
    rgba = _solid(4, 2, (10, 20, 30, 255))

    # When: it is rendered in colour at 2 columns
    body = render_halfblock(rgba, 4, 2, 2)

    # Then: one row of two solid blocks, each carrying the exact source colour
    lines = body.split("\n")
    assert len(lines) == 1
    assert "\x1b[38;2;10;20;30m" in lines[0]
    assert "\x1b[48;2;10;20;30m" in lines[0]
    assert "█" in lines[0]
    assert "▀" not in lines[0]
    assert lines[0].endswith("\x1b[0m")


def test_render_halfblock_colour_cell_uses_box_average_not_point_sample() -> None:
    # Given: a 2x2 frame whose top half is two different greys
    rgba = bytearray(
        bytes((0, 0, 0, 255)) + bytes((100, 100, 100, 255)) + bytes((7, 7, 7, 255)) * 2
    )

    # When: it is rendered in colour at 1 column
    body = render_halfblock(rgba, 2, 2, 1)

    # Then: the top half averages to 50 grey (not the point sample 0 or 100)
    assert "38;2;50;50;50" in body
    assert "48;2;7;7;7" in body


def test_render_halfblock_sgr_is_run_length_and_resets_per_row() -> None:
    # Given: a flat 40x40 frame — every cell in the dump is the same colour
    rgba = _solid(40, 40, (255, 255, 255, 255))

    # When: it is rendered in colour at 20 columns (10 rows of 20 cells)
    body = render_halfblock(rgba, 40, 40, 20)

    # Then: the colour is stated once per row, never once per cell
    lines = body.split("\n")
    assert body.count("\x1b[38;2;255;255;255m") == len(lines)
    assert body.count("\x1b[48;2;255;255;255m") == len(lines)
    assert body.count("\x1b[0m") == len(lines)


def test_render_halfblock_plain_mode_emits_no_sgr_and_ramps_luminance() -> None:
    # Given: a 4x2 frame that is black on the left and white on the right
    rgba = bytearray(
        bytes((0, 0, 0, 255)) * 2
        + bytes((255, 255, 255, 255)) * 2
        + bytes((0, 0, 0, 255)) * 2
        + bytes((255, 255, 255, 255)) * 2
    )

    # When: it is rendered in plain mode at 2 columns
    body = render_halfblock(rgba, 4, 2, 2, color=False)

    # Then: the dark cell is the ramp's first glyph and the bright one its last,
    # and no escape sequence survives
    assert body == " @"
    assert "\x1b" not in body


def test_render_halfblock_plain_ramp_is_monotone_in_luminance() -> None:
    # Given: six frames of increasing flat grey
    ramp = " .:-=+*#%@"
    last = " "
    for level in (0, 51, 102, 153, 204, 255):
        # When: each is rendered in plain mode
        char = render_halfblock(_solid(2, 2, (level,) * 3 + (255,)), 2, 2, 1, color=False)
        # Then: the ramp index never goes backwards as luminance rises
        assert ramp.index(char) >= ramp.index(last)
        last = char
    assert ramp.index(last) == 9


# ── C) .art file format ──────────────────────────────────────────────────────

_META = {
    "screen": "wl-1",
    "captured": "2026-10-11T16:20:31",
    "source": "1280x720",
    "grid": "100x28",
    "cell": "13x13",
    "mode": "halfblock-truecolor",
}


def test_build_art_round_trips_through_parse_art_byte_for_byte() -> None:
    # Given: a colour body full of SGR bytes plus a body that ends in a newline
    body = "\x1b[38;2;1;2;3m▀\x1b[0m\n\x1b[48;2;4;5;6m█\n"

    # When: it is composed and parsed back
    text = build_art(_META, body)
    art = parse_art(text)

    # Then: meta survives verbatim, the body survives verbatim, grid is parsed
    assert text.startswith("#miniouto-screen-art v1\n")
    assert "\n--- art ---\n" in text
    assert text.endswith("\n--- end ---\n")
    assert art.meta == _META
    assert art.body == body
    assert (art.cols, art.rows) == (100, 28)


def test_parse_art_rejects_missing_art_sentinel() -> None:
    # Given: a file whose body sentinel was never written
    text = build_art(_META, "hello").replace("--- art ---", "--- oops ---")

    # When: it is parsed
    with pytest.raises(ValueError, match="art ---"):
        parse_art(text)


def test_parse_art_rejects_missing_version_marker() -> None:
    # Given: a file with a wrong version line
    text = build_art(_META, "hello").replace("v1", "v2", 1)

    # When: it is parsed
    with pytest.raises(ValueError, match="miniouto-screen-art"):
        parse_art(text)


def test_art_to_plain_strips_sgr_and_keeps_luminance_order() -> None:
    # Given: a colour .art body with a dark cell, a mid cell and a bright cell
    body = (
        "\x1b[38;2;0;0;0m\x1b[48;2;0;0;0m█"
        "\x1b[38;2;128;128;128m\x1b[48;2;128;128;128m█"
        "\x1b[38;2;255;255;255m\x1b[48;2;255;255;255m█"
        "\x1b[0m"
    )
    art = ArtFile(meta=dict(_META), body=body, cols=3, rows=1)

    # When: it is converted to plain text
    plain = art_to_plain(art)

    # Then: no SGR survives and the ramp index rises with luminance
    ramp = " .:-=+*#%@"
    assert "\x1b" not in plain
    chars = plain.split("\n")[0]
    assert len(chars) == 3
    assert ramp.index(chars[0]) == 0
    assert ramp.index(chars[1]) == 5
    assert ramp.index(chars[2]) == 9


def test_art_to_plain_requantises_when_cols_differs() -> None:
    # Given: a 4-column plain (ramp) body
    body = " .:-"
    art = ArtFile(meta=dict(_META), body=body, cols=4, rows=1)

    # When: it is converted to 2 columns
    plain = art_to_plain(art, cols=2)

    # Then: the result is 2 cells wide, sampled from the original columns
    assert plain == " :"


def test_art_to_plain_is_identity_for_ramp_plain_files() -> None:
    # Given: a ramp-plain body with no SGR at all
    body = "@%#*+=-:. \n.-:=+*#%@"
    art = ArtFile(meta={**_META, "mode": "ramp-plain"}, body=body, cols=10, rows=2)

    # When: it is converted with no re-quantisation requested
    plain = art_to_plain(art)

    # Then: it comes back exactly as it went in
    assert plain == body


def test_strip_sgr_leaves_plain_text_untouched() -> None:
    # Given: plain text with no escape sequences at all
    plain = "  hello @%# world  \n"

    # When: it goes through strip_sgr
    stripped = strip_sgr(plain)

    # Then: it is unchanged
    assert stripped == plain


# ── D) 2x4 sub-cell rendering ───────────────────────────────────────────────
#
# A cell is 2 pixels wide and 4 tall, so a 2x4 source image rendered at one
# column IS one cell with one source pixel per sub-cell — which is how the
# fixtures below pin a mask to a glyph. Sub-cell index `i` is COLUMN-major
# (bit 0 = left column top, bit 4 = right column top), i.e. braille dot order.

BLACK = (0, 0, 0, 255)
WHITE = (255, 255, 255, 255)


def _cell_frame(rows: list[list[tuple[int, int, int, int]]]) -> bytearray:
    out = bytearray()
    for row in rows:
        for px in row:
            out += bytes(px)
    assert len(out) == len(rows[0]) * len(rows) * 4
    return out


def _two_tone_cell() -> bytearray:
    """One cell, ON = left column rows 0-1 plus right column rows 2-3.

    Asymmetric on purpose: under the octants' ROW-major digit naming those
    four sub-cells are 1, 3, 7 and 8 (`BLOCK OCTANT-1378`), while a wrong
    column-major mapping would call them 1, 2, 3, 4 — a different mask, and a
    different character.
    """
    return _cell_frame(
        [
            [WHITE, BLACK],
            [WHITE, BLACK],
            [BLACK, WHITE],
            [BLACK, WHITE],
        ]
    )


def _gradient_cell() -> bytearray:
    """One cell whose eight sub-cells step smoothly from black to grey."""
    return _cell_frame(
        [
            [(0, 0, 0, 255), (32, 32, 32, 255)],
            [(64, 64, 64, 255), (96, 96, 96, 255)],
            [(128, 128, 128, 255), (160, 160, 160, 255)],
            [(192, 192, 192, 255), (224, 224, 224, 255)],
        ]
    )


def _body_glyphs(body: str) -> str:
    return strip_sgr(body).replace("\n", "")


def _two_tone_frame(on: tuple[int, int, int], off: tuple[int, int, int]) -> bytearray:
    """One cell of two flat colours in the `_two_tone_cell` arrangement."""
    hi, lo = (*on, 255), (*off, 255)
    return _cell_frame([[hi, lo], [hi, lo], [lo, hi], [lo, hi]])


_FIRST_PAIR_RE = re.compile(r"\x1b\[38;2;(\d+);(\d+);(\d+)m\x1b\[48;2;(\d+);(\d+);(\d+)m")


def _cell_pair(body: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """The fg/bg the first rendered cell actually emitted."""
    match = _FIRST_PAIR_RE.search(body)
    assert match is not None, f"no truecolour cell pair in {body!r}"
    values = [int(group) for group in match.groups()]
    return (values[0], values[1], values[2]), (values[3], values[4], values[5])


def _pair_distance(pair: tuple[tuple[int, int, int], tuple[int, int, int]]) -> float:
    """The colour distance between a pair's two entries."""
    return sum((pair[0][i] - pair[1][i]) ** 2 for i in range(3)) ** 0.5


def _chroma_length(pair: tuple[tuple[int, int, int], tuple[int, int, int]]) -> float:
    """The pair's difference with the luminance axis removed (its saturation)."""
    difference = [(pair[0][i] - pair[1][i]) / 2 for i in range(3)]
    level = sum(difference) / 3
    return sum((component - level) ** 2 for component in difference) ** 0.5


def _is_octant(ch: str) -> bool:
    return OCTANT_FIRST_CODEPOINT <= ord(ch) <= OCTANT_LAST_CODEPOINT


def _is_braille(ch: str) -> bool:
    return 0x2800 <= ord(ch) <= 0x28FF


def test_subcell_rows_match_halfblock_rows_for_the_same_frame() -> None:
    # Given: frames and widths spanning wide, square and tall
    frames = [(1280, 720), (100, 100), (720, 1280), (1920, 1080), (64, 48)]

    # Then: both renderers always agree — a cell is 2x4 SQUARE sub-pixels, so
    # the row count is the same 1:2 aspect correction either way
    for width, height in frames:
        for cols in (16, 20, 100):
            halfblock_rows = len(
                render_halfblock(_solid(width, height, (1, 2, 3, 255)), width, height, cols).split("\n")
            )
            assert subcell_rows(width, height, cols) == halfblock_rows


def test_subcell_grid_maths_on_1280x720_at_100_cols() -> None:
    # Given: a flat 1280x720 RGBA frame
    rgba = _solid(1280, 720, (10, 20, 30, 255))

    # When: it is rendered at 100 columns
    body = render_subcell(rgba, 1280, 720, 100)

    # Then: rows == round(100*720/1280/2) == 28 and every row is 100 cells —
    # the same grid half-block gives, with four square source pixels per cell
    lines = body.split("\n")
    assert subcell_rows(1280, 720, 100) == 28
    assert len(lines) == 28
    assert all(len(strip_sgr(line)) == 100 for line in lines)


def test_subcell_source_pixels_per_cell_are_square() -> None:
    # Given: a square 100x100 frame rendered at 20 columns
    width = height = 100
    cols = 20

    # When: the grid is computed
    rows = subcell_rows(width, height, cols)

    # Then: the cell's source rect splits into 2x4 sub-pixels that are square
    sub_w = width / (2 * cols)
    sub_h = height / (4 * rows)
    assert round(sub_w) == round(sub_h)


def test_subcell_two_tone_cell_emits_the_octant_for_its_own_mask() -> None:
    # Given: one cell whose ON sub-cells are internal mask 0xC3
    rgba = _two_tone_cell()

    # When: it is rendered in colour at one column
    body = render_subcell(rgba, 2, 4, 1)

    # Then: the single glyph is the octant the table holds for mask 0xC3
    glyph = _body_glyphs(body)
    assert len(glyph) == 1
    assert OCTANT_GLYPHS[0xC3], "mask 0xC3 must be an encoded octant"
    assert glyph == OCTANT_GLYPHS[0xC3]
    assert _is_octant(glyph)


def test_subcell_mask_uses_the_octants_row_major_digit_naming() -> None:
    # Given: the same asymmetric cell (ON = left column rows 0-1, right rows 2-3)
    rgba = _two_tone_cell()

    # When: it is rendered and the glyph is looked up in unicodedata
    glyph = _body_glyphs(render_subcell(rgba, 2, 4, 1))
    name = _unicode_name(glyph)

    # Then: Unicode names it BLOCK OCTANT-1378 — row-major digits, NOT 1234
    # (skipped when this interpreter's Unicode DB predates Unicode 16.0)
    if name is None:
        pytest.skip(f"unicodedata does not know {glyph!r} (U+{ord(glyph):04X})")
    assert name == "BLOCK OCTANT-1378"


def _unicode_name(ch: str) -> str | None:
    import unicodedata

    try:
        return unicodedata.name(ch)
    except ValueError:
        return None


def test_subcell_gradient_cell_blends_with_braille_dots() -> None:
    # Given: one cell whose eight sub-cells step smoothly black -> grey
    rgba = _gradient_cell()

    # When: it is rendered in colour at one column
    glyph = _body_glyphs(render_subcell(rgba, 2, 4, 1))

    # Then: a halftone dot pattern, because no two sub-cells share one colour
    assert len(glyph) == 1
    assert _is_braille(glyph)
    assert not _is_octant(glyph)


def test_subcell_uniform_cell_is_a_full_block() -> None:
    # Given: a flat 8x8 frame, so every sub-cell of every cell agrees
    rgba = _solid(8, 8, (10, 20, 30, 255))

    # When: it is rendered in colour at 2 columns
    body = render_subcell(rgba, 8, 8, 2)

    # Then: two full blocks, one truecolour pair between them, no dots
    assert _body_glyphs(body) == "██"
    assert body.count("\x1b[38;2;10;20;30m") == 1
    assert body.count("\x1b[48;2;10;20;30m") == 1


def test_subcell_forced_braille_never_emits_an_octant_codepoint() -> None:
    # Given: one cell whose mask IS an encoded octant, and one that is not
    cells = [
        _body_glyphs(render_subcell(rgba, 2, 4, 1, glyph="braille"))
        for rgba in (_two_tone_cell(), _gradient_cell())
    ]

    # Then: every glyph is a braille dot pattern — never a Unicode-16 octant
    assert all(len(cell) == 1 and _is_braille(cell) for cell in cells)


def test_subcell_forced_octant_emits_octants_where_a_mask_applies() -> None:
    # Given: the same two cells with the octant class forced
    clean = _body_glyphs(render_subcell(_two_tone_cell(), 2, 4, 1, glyph="octant"))
    mixed = _body_glyphs(render_subcell(_gradient_cell(), 2, 4, 1, glyph="octant"))

    # Then: the clean cell renders as a block octant, and the gradient cell —
    # whose mask Unicode leaves unencoded — still degrades to dots
    assert clean == OCTANT_GLYPHS[0xC3]
    assert _is_octant(clean)
    assert _is_braille(mixed)


def test_subcell_sgr_is_run_length_and_resets_per_row() -> None:
    # Given: a flat 40x40 frame — every cell in the dump is the same colour
    rgba = _solid(40, 40, (255, 255, 255, 255))

    # When: it is rendered in colour at 20 columns
    body = render_subcell(rgba, 40, 40, 20)

    # Then: the colour is stated once per row, never once per cell
    lines = body.split("\n")
    assert body.count("\x1b[38;2;255;255;255m") == len(lines)
    assert body.count("\x1b[48;2;255;255;255m") == len(lines)
    assert body.count("\x1b[0m") == len(lines)


def test_subcell_plain_mode_emits_the_ramp_and_no_sgr() -> None:
    # Given: a 4x4 frame, black on the left and white on the right
    rgba = _cell_frame([[BLACK, BLACK, WHITE, WHITE] for _ in range(4)])

    # When: it is rendered with colour off
    body = render_subcell(rgba, 4, 4, 2, color=False)

    # Then: two ramp glyphs, darkest first, and not one escape byte
    assert "\x1b" not in body
    assert body == " @"


def test_subcell_sharpness_rule_is_two_sided(monkeypatch) -> None:
    # Given: the rule neutered so that NO cell can ever count as sharp
    monkeypatch.setattr(ascii_art, "OCTANT_MIN_SEPARATION", 1e9)
    monkeypatch.setattr(ascii_art, "OCTANT_MAX_INTRA_ERROR", -1.0)

    # When/Then: the clean two-tone cell that "mix" renders as a block octant
    # now falls back to dots — so the class decision really is this rule
    assert _is_braille(_body_glyphs(render_subcell(_two_tone_cell(), 2, 4, 1)))


def _luma(colour: tuple[int, int, int]) -> float:
    return 0.299 * colour[0] + 0.587 * colour[1] + 0.114 * colour[2]


def test_subcell_low_contrast_pair_is_widened_until_the_mask_reads() -> None:
    # Given: a patterned cell whose two blues are only 30 units apart — a real
    # pattern (past UNIFORM_SPREAD), but far below the contrast floor
    rgba = _two_tone_frame((20, 20, 140), (20, 20, 110))

    # When: it is rendered in colour at one column
    body = render_subcell(rgba, 2, 4, 1)
    fg, bg = _cell_pair(body)

    # Then: the emitted pair spans the documented luminance and colour minima,
    # so the glyph's sub-shapes are actually separable
    assert abs(_luma(fg) - _luma(bg)) >= ascii_art.MIN_LUMA_DELTA
    assert _pair_distance((fg, bg)) >= ascii_art.MIN_PAIR_DISTANCE


def test_subcell_pair_within_the_uniform_spread_stays_one_colour() -> None:
    # Given: two blues 10 units apart — inside UNIFORM_SPREAD, so no pattern
    rgba = _two_tone_frame((20, 20, 130), (20, 20, 120))

    # When: it is rendered in colour at one column
    body = render_subcell(rgba, 2, 4, 1)

    # Then: one flat block carrying a single truecolour pair, untouched
    assert _body_glyphs(body) == "█"
    assert _cell_pair(body)[0] == _cell_pair(body)[1] == (20, 20, 125)


def test_subcell_contrast_boost_never_amplifies_chroma_past_the_cap() -> None:
    # Given: a chromatic pair whose luminance gap needs more than the cap to
    # close (14 of the 40 required), so the boost saturates at the cap
    rgba = _two_tone_frame((70, 80, 150), (50, 70, 140))
    raw = _chroma_length(((70, 80, 150), (50, 70, 140)))

    # When: it is rendered in colour at one column
    fg, bg = _cell_pair(render_subcell(rgba, 2, 4, 1))

    # Then: chroma grows by at most MAX_CONTRAST_BOOST while the luminance
    # shortfall is made up along the neutral axis, and the floor is still met
    assert _chroma_length((fg, bg)) <= raw * ascii_art.MAX_CONTRAST_BOOST + 1e-6
    assert abs(_luma(fg) - _luma(bg)) >= ascii_art.MIN_LUMA_DELTA


def test_subcell_readable_pair_is_left_exactly_as_clustered() -> None:
    # Given: a pair that already spans both minima by a wide margin
    rgba = _two_tone_frame((230, 230, 235), (10, 10, 10))

    # When: it is rendered in colour at one column
    body = render_subcell(rgba, 2, 4, 1)

    # Then: nothing is amplified — the cluster means are the cluster means
    assert _cell_pair(body) == ((230, 230, 235), (10, 10, 10))


def test_subcell_pair_pinned_at_black_collapses_to_the_mean() -> None:
    # Given: a clean two-tone cell at the bottom of the gamut, where no pair
    # can open to MIN_LUMA_DELTA without leaving [0,255]
    rgba = _two_tone_frame((12, 12, 30), (0, 0, 0))
    mean = (6, 6, 15)

    # When: it is rendered in colour at one column
    body = render_subcell(rgba, 2, 4, 1)

    # Then: a smooth block at the cell mean — noise the eye cannot resolve
    # beats a pattern whose mask is invisible
    assert _body_glyphs(body) == "█"
    assert _cell_pair(body) == (mean, mean)


def test_subcell_noisy_dark_cell_collapses_instead_of_exploding() -> None:
    # Given: eight scattered near-black sub-cells, i.e. pure noise whose
    # clustered pair cannot be widened anywhere without leaving the gamut
    shades = [
        (0, 0, 0),
        (6, 4, 9),
        (0, 8, 4),
        (10, 2, 0),
        (3, 9, 8),
        (0, 5, 12),
        (11, 7, 3),
        (4, 0, 7),
    ]
    rgba = _cell_frame(
        [
            [shades[0] + (255,), shades[1] + (255,)],
            [shades[2] + (255,), shades[3] + (255,)],
            [shades[4] + (255,), shades[5] + (255,)],
            [shades[6] + (255,), shades[7] + (255,)],
        ]
    )
    mean = tuple(round(sum(colour[i] for colour in shades) / len(shades)) for i in range(3))

    # When: it is rendered in colour at one column
    body = render_subcell(rgba, 2, 4, 1)

    # Then: one block at the mean, with the pair's spread driven to zero
    assert _body_glyphs(body) == "█"
    assert _cell_pair(body) == (mean, mean)


def test_subcell_contrast_boost_does_not_change_the_glyph_class() -> None:
    # Given: two cells whose raw pairs need a boost (a crisp low-contrast pair
    # and a soft gradient), one cell class each
    crisp = _two_tone_frame((70, 110, 190), (50, 80, 140))
    gradient = _gradient_cell()

    # Then: the boosted crisp cell still draws its block octant ...
    assert _is_octant(_body_glyphs(render_subcell(crisp, 2, 4, 1)))
    # ... and the boosted gradient still draws braille dots
    gradient_glyph = _body_glyphs(render_subcell(gradient, 2, 4, 1))
    assert _is_braille(gradient_glyph)
    # And: forcing the dot family changes no colour, only the glyph
    assert _cell_pair(render_subcell(crisp, 2, 4, 1, glyph="braille")) == _cell_pair(
        render_subcell(crisp, 2, 4, 1)
    )


def test_subcell_mode_round_trips_through_parse_art_byte_for_byte() -> None:
    # Given: a subcell body with SGR, octant and braille glyphs in it
    body = render_subcell(_two_tone_cell() + _gradient_cell(), 4, 4, 2)
    meta = {
        "screen": "wl-1",
        "captured": "2026-10-11T16:20:31",
        "source": "4x4",
        "grid": "2x1",
        "cell": "2x4",
        "mode": "subcell-truecolor",
    }

    # When: it is composed into a file and parsed back
    art = parse_art(build_art(meta, body))

    # Then: meta and body survive byte for byte
    assert art.meta == meta
    assert art.body == body
    assert (art.cols, art.rows) == (2, 1)


def test_art_to_plain_blends_subcell_glyphs_between_fg_and_bg() -> None:
    # Given: three subcell cells whose covered fraction rises 0, half, full
    body = (
        "\x1b[38;2;255;255;255m\x1b[48;2;0;0;0m \x1b[0m"
        "\x1b[38;2;255;255;255m\x1b[48;2;0;0;0m⡿\x1b[0m"
        "\x1b[38;2;255;255;255m\x1b[48;2;0;0;0m⣿\x1b[0m"
    )
    art = ArtFile(meta={**_META, "mode": "subcell-truecolor"}, body=body, cols=3, rows=1)

    # When: it is converted to plain text
    plain = art_to_plain(art)

    # Then: no SGR survives and the ramp rises monotonically with coverage
    ramp = " .:-=+*#%@"
    assert "\x1b" not in plain
    cells = plain.split("\n")[0]
    assert len(cells) == 3
    assert ramp.index(cells[0]) < ramp.index(cells[1]) < ramp.index(cells[2])
    assert ramp.index(cells[0]) == 0
    assert ramp.index(cells[2]) == 9


def test_octant_table_covers_the_whole_unicode_16_block() -> None:
    # Given: the table built from Unicode 16.0 UnicodeData.txt
    encoded = [ch for ch in OCTANT_GLYPHS if ch]

    # Then: every entry is inside U+1CD00..U+1CDE5 and none is repeated
    assert encoded
    assert all(_is_octant(ch) for ch in encoded)
    assert len(set(encoded)) == len(encoded)
    assert OCTANT_LAST_CODEPOINT - OCTANT_FIRST_CODEPOINT + 1 == 230


def test_octant_table_entries_match_unicodedata_when_it_knows_them() -> None:
    # Given: the whole table, as (mask, character) pairs
    known = 0
    for mask, ch in enumerate(OCTANT_GLYPHS):
        if not ch:
            continue
        name = _unicode_name(ch)

        # When/Then: where the DB knows the character, its Unicode 16.0 name
        # lists exactly this mask's sub-cells as ROW-major digits
        if name is None:
            continue
        known += 1
        assert name.startswith("BLOCK OCTANT-")
        row_major = 0
        for digit in name.removeprefix("BLOCK OCTANT-"):
            row, col = divmod(int(digit) - 1, 2)
            row_major |= 1 << (col * 4 + row)
        assert row_major == mask
    if known == 0:
        pytest.skip("unicodedata predates Unicode 16.0 — names unavailable")
