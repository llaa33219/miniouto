"""Screen-art codec: PNG -> Unicode screen art -> `.art` file text.

Pure stdlib on purpose. py-Vwayland exposes a compositor screenshot ONLY as
`Image.png_bytes`, and Pillow is deliberately not a miniouto dependency (the
project pins eight packages and anything more needs discussion), so this
module is the decoder. It must therefore import nothing from `miniouto` and
nothing from coreouto: the tools layer rule is that only
`tools/registry.py` speaks coreouto, and `tools/computer.py` (the caller) sits
below this file in the dependency graph.

Nothing here touches the filesystem either — it is a pure codec over bytes and
strings. `tools/computer.py` owns the `.art` files on disk.

Layering, top to bottom:

    decode_png_rgba   bytes        -> (width, height, flat RGBA bytearray)
    render_halfblock  RGBA + grid  -> the art body (SGR + glyphs), 2 rows/cell
    render_subcell    RGBA + grid  -> the art body (SGR + glyphs), 4 rows/cell
    build_art         meta + body  -> a whole `.art` file text
    parse_art         file text    -> ArtFile (meta + verbatim body)
    art_to_plain      ArtFile      -> the same art without any SGR

The decode runs on a worker thread in the caller (`asyncio.to_thread`), so the
straightforward pure-Python defilter below is the right trade: correctness and
an actionable `ValueError` on every unsupported input beat a hand-tuned inner
loop nobody can read.

Two renderers, because a terminal cell is a 2x4 sub-pixel grid and only a
truecolour cell can show all eight sub-cells at once:

* `render_halfblock` puts 2 vertical pixels in one cell as `▀` + fg/bg. It is
  the compat path and stays.
* `render_subcell` (the default the Computer tool uses) puts all EIGHT
  sub-cells in one cell, choosing between two glyph families per cell so that
  colour is preserved rather than merely encoded. Both families are indexed by
  the same COLUMN-major mask (`bit i` = sub-cell `i`, left column rows 0-3
  then right column rows 0-3 — the braille dot order), so the two are
  interchangeable and one mask drives both.

Glyph selection, per cell, after box-averaging the 8 sub-cells and reducing
them to two palette entries (fg, bg) with a 2-means pass:

* uniform (every sub-cell within `UNIFORM_SPREAD` of the cell mean) -> `█`
  with fg=bg=that colour. The universal glyph, valid in every font.
* else, clean two-tone (centres at least `OCTANT_MIN_SEPARATION` apart AND
  every sub-cell within `OCTANT_MAX_INTRA_ERROR` of its own centre) -> a
  BLOCK OCTANT (U+1CD00..U+1CDE5, Unicode 16.0): crisp solid sub-rectangles,
  the right answer for a window border, a button face, a hard edge.
* else (tones close together, or many intermediate tones inside the clusters)
  -> a BRAILLE pattern (U+2800 + mask): 8 halftone dots that optically blend
  the two colours, the right answer for an antialiased edge, a gradient, text.

Font support: BLOCK OCTANT is Unicode 16.0, so a font or terminal predating it
renders nothing (tofu) where an octant was chosen, and Unicode leaves 25 masks
unencoded because existing block glyphs already cover them. The `glyph`
argument is the escape hatch — `"braille"` forces dots everywhere (never
worse than half-block), `"octant"` forces blocks wherever a mask applies.

Geometry is shared with half-block on purpose. A cell is 2x4 source pixels but
those sub-pixels are square, so one cell covers the same source rect in both
renderers and `rows = max(1, round(cols * height / width / 2.0))` for both —
sub-cell art buys resolution INSIDE the cell, never fewer rows (halving them
would render every screen at half height).

Contrast, applied to the pair AFTER the glyph class is chosen, so a soft
gradient still picks braille and a crisp pair still picks octant: 2-means
minimises error, so a subtle cell lands on two nearly identical entries and its
mask becomes invisible. A patterned pair is therefore widened until it spans at
least `MIN_LUMA_DELTA` of luminance (the primary criterion) and
`MIN_PAIR_DISTANCE` of colour, symmetrically about the cell mean: first along
its own direction, amplified at most `MAX_CONTRAST_BOOST`, then along the
neutral axis for the luminance shortfall left over, both clamped to the
`[0,255]` room around the mean. A pair that cannot reach the floor inside that
clamp — pinned at black or white, or so noisy that any boost is an explosion —
collapses the cell to the mean and draws `█`. Uniform cells never change, and a
cell whose mask is empty or full carries no pattern, so neither is boosted.
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass

# ── the .art file format ─────────────────────────────────────────────────────

ART_VERSION = "#miniouto-screen-art v1"
ART_BEGIN = "--- art ---"
ART_END = "--- end ---"

#: Canonical header order. A `meta` dict is written in this order (missing keys
#: are skipped), then any extra keys alphabetically, so two runs with the same
#: meta always produce byte-identical files.
_META_ORDER = ("screen", "captured", "source", "grid", "cell", "mode")

MODE_COLOR = "halfblock-truecolor"
MODE_COLOR_SUBCELL = "subcell-truecolor"
MODE_PLAIN = "ramp-plain"

_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
_SGR_SPLIT_RE = re.compile(r"(\x1b\[[0-9;]*m)")

# ── the glyphs ───────────────────────────────────────────────────────────────

#: Upper half block: fg paints the top pixel row, bg the bottom one — two
#: vertical pixels per terminal cell.
HALF_BLOCK = "▀"
#: Full block: fg and bg are the same colour, so one glyph draws both halves.
FULL_BLOCK = "█"

#: Darkest -> brightest. Index comes from Rec. 601 luma scaled 0-255 -> 0-9.
RAMP = " .:-=+*#%@"

_SGR_RESET = "\x1b[0m"

# ── the 2x4 sub-cell glyph families ──────────────────────────────────────────

#: One terminal cell carries this many source pixels.
SUBCELL_COLS = 2
SUBCELL_ROWS = 4
SUBCELL_COUNT = SUBCELL_COLS * SUBCELL_ROWS

#: Braille dots: U+2800 + mask, bit 0 = dot 1 = left column top row. The
#: internal mask IS the braille mask, so no permutation is needed.
BRAILLE_BASE = 0x2800

#: BLOCK OCTANT, Unicode 16.0 "Symbols for Legacy Computing Supplement",
#: U+1CD00..U+1CDE5 — 230 of the 256 masks; the 26 left out (blank, full, and
#: shapes an older block glyph already draws) have no character here.
OCTANT_FIRST_CODEPOINT = 0x1CD00
OCTANT_LAST_CODEPOINT = OCTANT_FIRST_CODEPOINT + 229

#: The mask of each BLOCK OCTANT, in codepoint order, exactly as Unicode 16.0
#: `UnicodeData.txt` spells it in the character names. Generated once from that
#: file: `1CD00;BLOCK OCTANT-3` -> 0b00000100, `1CDE5;BLOCK OCTANT-2345678` ->
#: 0xFE. Digits are the ON sub-cells in ROW-major order (1,2 = top row left to
#: right; 3,4 = second row; 5,6 = third; 7,8 = bottom row) — verified against
#: the Unicode 16.0 chart: U+1CD19 `BLOCK OCTANT-16` draws a top-left square
#: plus a middle-right square, which row-major naming explains and
#: column-major (sextant/braille) naming does not.
_OCTANT_NAME_MASKS = (
    0x02, 0x12, 0x13, 0x20, 0x21, 0x31, 0x22, 0x23, 0x32, 0x04,
    0x05, 0x14, 0x15, 0x07, 0x16, 0x17, 0x24, 0x25, 0x34, 0x35,
    0x26, 0x27, 0x36, 0x37, 0x40, 0x41, 0x50, 0x51, 0x42, 0x43,
    0x52, 0x53, 0x61, 0x70, 0x71, 0x62, 0x63, 0x72, 0x73, 0x44,
    0x45, 0x54, 0x55, 0x46, 0x47, 0x56, 0x57, 0x64, 0x65, 0x74,
    0x75, 0x66, 0x67, 0x76, 0x09, 0x18, 0x19, 0x0A, 0x0B, 0x1A,
    0x1B, 0x28, 0x29, 0x38, 0x39, 0x2A, 0x2B, 0x3A, 0x3B, 0x0D,
    0x1C, 0x1D, 0x0E, 0x1E, 0x1F, 0x2C, 0x2D, 0x3D, 0x2E, 0x2F,
    0x3E, 0x48, 0x49, 0x58, 0x59, 0x4A, 0x4B, 0x5A, 0x5B, 0x68,
    0x69, 0x78, 0x79, 0x6A, 0x6B, 0x7A, 0x7B, 0x4C, 0x4D, 0x5C,
    0x5D, 0x4E, 0x4F, 0x5E, 0x5F, 0x6C, 0x6D, 0x7C, 0x7D, 0x6E,
    0x6F, 0x7E, 0x7F, 0x81, 0x90, 0x91, 0x82, 0x83, 0x92, 0x93,
    0xA0, 0xA1, 0xB0, 0xB1, 0xA2, 0xA3, 0xB2, 0xB3, 0x84, 0x85,
    0x94, 0x95, 0x86, 0x87, 0x96, 0x97, 0xA4, 0xA5, 0xB4, 0xB5,
    0xA6, 0xA7, 0xB6, 0xB7, 0xC1, 0xD0, 0xD1, 0xC2, 0xD2, 0xD3,
    0xE0, 0xE1, 0xF1, 0xE2, 0xE3, 0xF2, 0xC4, 0xC5, 0xD4, 0xD5,
    0xC6, 0xC7, 0xD6, 0xD7, 0xE4, 0xE5, 0xF4, 0xF5, 0xE6, 0xE7,
    0xF6, 0xF7, 0x89, 0x98, 0x99, 0x8A, 0x8B, 0x9A, 0x9B, 0xA8,
    0xA9, 0xB8, 0xB9, 0xAA, 0xAB, 0xBA, 0xBB, 0x8C, 0x8D, 0x9C,
    0x9D, 0x8E, 0x8F, 0x9E, 0x9F, 0xAC, 0xAD, 0xBC, 0xBD, 0xAE,
    0xAF, 0xBE, 0xBF, 0xC8, 0xC9, 0xD8, 0xD9, 0xCA, 0xCB, 0xDA,
    0xDB, 0xE8, 0xE9, 0xF8, 0xF9, 0xEA, 0xEB, 0xFA, 0xFB, 0xCD,
    0xDC, 0xDD, 0xCE, 0xDE, 0xDF, 0xEC, 0xED, 0xFD, 0xEF, 0xFE,
)

#: Glyph family choices for `render_subcell(glyph=...)`.
GLYPH_MIX = "mix"
GLYPH_OCTANT = "octant"
GLYPH_BRAILLE = "braille"
GLYPH_MODES = (GLYPH_MIX, GLYPH_OCTANT, GLYPH_BRAILLE)

#: A cell whose sub-cells all sit within this distance (0-441, the maximum
#: between black and white) of their own mean is one colour: `█`.
UNIFORM_SPREAD = 12.0

#: Octant (crisp blocks) needs the two palette entries this far apart ...
OCTANT_MIN_SEPARATION = 60.0
#: ... and every sub-cell within this distance of the entry it was assigned to.
OCTANT_MAX_INTRA_ERROR = 24.0

#: Readability floor for a PATTERNED cell. 2-means minimises error, so a
#: subtle cell lands on two nearly identical entries and its octant/braille
#: mask is invisible — the dump reads as assorted coloured blocks. These two
#: are what a pattern needs to read: `MIN_LUMA_DELTA` on Rec. 601 luminance
#: (the primary criterion — brightness is what the eye separates glyph
#: sub-shapes by) and `MIN_PAIR_DISTANCE` as a secondary guard so a pair that
#: only moves in hue still reads as two colours.
MIN_LUMA_DELTA = 40.0
MIN_PAIR_DISTANCE = 50.0
#: How far the pair may be pushed ALONG ITS OWN difference vector. Chroma is
#: amplified at most this much; the luminance shortfall that remains is made
#: up along the neutral axis instead (see `_boost_pair`), which is gamut
#: bounded and therefore cannot be capped the same way.
MAX_CONTRAST_BOOST = 2.0

#: Rounding to integer RGB can shave the last fraction of a luma unit off the
#: pair, so the neutral lift is re-tried a couple of times before giving up.
_BOOST_STEPS = 8

#: Lloyd iterations of the 2-means pass. Three is enough for eight points.
_KMEANS_ITERATIONS = 3


def _name_mask_to_internal(name_mask: int) -> int:
    """Re-index a BLOCK OCTANT name mask into the internal (braille) order.

    Unicode numbers the octant digits row by row (`digit - 1 == row * 2 + col`)
    while the internal mask numbers sub-cells column by column (`bit == col * 4
    + row`, braille's order), so the two need this one permutation.
    """
    internal = 0
    for digit in range(SUBCELL_COUNT):
        if name_mask >> digit & 1:
            row, col = divmod(digit, SUBCELL_COLS)
            internal |= 1 << (col * SUBCELL_ROWS + row)
    return internal


def _build_octant_glyphs() -> tuple[str, ...]:
    table = [""] * 256
    for offset, name_mask in enumerate(_OCTANT_NAME_MASKS):
        table[_name_mask_to_internal(name_mask)] = chr(OCTANT_FIRST_CODEPOINT + offset)
    return tuple(table)


#: Internal mask -> the BLOCK OCTANT that draws it, or "" for a mask Unicode
#: left unencoded (that cell falls back to a braille pattern).
OCTANT_GLYPHS: tuple[str, ...] = _build_octant_glyphs()


@dataclass(frozen=True)
class ArtFile:
    """A parsed `.art` file: its header meta and its body verbatim.

    `cols`/`rows` come from the `grid` header field (`"<cols>x<rows>"`) and are
    0 when that field is absent or malformed — a body without a usable grid
    still renders, it just cannot be re-quantised to another width.
    """

    meta: dict[str, str]
    body: str
    cols: int
    rows: int


# ── A) PNG decoding ──────────────────────────────────────────────────────────

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Colour type -> bytes per pixel at 8-bit depth.
_COLOR_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def decode_png_rgba(png_bytes: bytes) -> tuple[int, int, bytearray]:
    """Decode an 8-bit, non-interlaced PNG into a flat top-down RGBA buffer.

    Supports colour types 0 (grey), 2 (RGB), 3 (palette, with optional tRNS),
    4 (grey+alpha) and 6 (RGBA), and all five scanline filters. Returns
    `(width, height, rgba)` where `rgba` is a `bytearray` of exactly
    `width * height * 4` bytes in row-major, top-down order.

    Every input this decoder cannot faithfully reproduce raises `ValueError`
    naming the offending property — a screenshot that decodes to garbage is
    worse than one that refuses to decode.
    """
    if not isinstance(png_bytes, (bytes, bytearray)):
        raise ValueError(f"not a PNG: expected bytes, got {type(png_bytes).__name__}")
    if bytes(png_bytes[:8]) != _PNG_SIGNATURE:
        raise ValueError("not a PNG: bad signature — the data does not start with the 8-byte PNG magic")

    width, height, depth, color_type, interlace, idat, plte, trns = _read_chunks(
        bytes(png_bytes)
    )

    if interlace != 0:
        raise ValueError(
            "unsupported PNG: interlaced (Adam7) — expected non-interlaced. "
            "Re-encode the screenshot with interlacing disabled."
        )
    if depth != 8:
        raise ValueError(f"unsupported PNG: {depth}-bit depth — expected 8-bit")
    if color_type not in _COLOR_CHANNELS:
        raise ValueError(
            f"unsupported PNG: colour type {color_type} — expected one of "
            "0 (grey), 2 (RGB), 3 (palette), 4 (grey+alpha), 6 (RGBA)"
        )
    if color_type == 3 and plte is None:
        raise ValueError("unsupported PNG: palette colour type without a PLTE chunk")

    raw = zlib.decompress(idat)
    stride = width * _COLOR_CHANNELS[color_type]
    if len(raw) != (stride + 1) * height:
        raise ValueError(
            f"corrupt PNG: decompressed IDAT is {len(raw)} bytes, expected "
            f"{(stride + 1) * height} for a {width}x{height} colour type {color_type} image"
        )

    rgba = bytearray(width * height * 4)
    _expand_rows(raw, width, height, color_type, plte, trns, rgba)
    return width, height, rgba


def _read_chunks(
    data: bytes,
) -> tuple[int, int, int, int, int, bytes, bytes | None, bytes | None]:
    width = height = depth = color_type = interlace = 0
    seen_ihdr = False
    idat = bytearray()
    plte: bytes | None = None
    trns: bytes | None = None

    pos = 8
    total = len(data)
    while pos + 8 <= total:
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        start = pos + 8
        end = start + length
        if end + 4 > total:
            raise ValueError(f"corrupt PNG: {tag.decode('latin-1')} chunk runs past the end of the data")
        payload = data[start:end]

        if tag == b"IHDR":
            if length != 13:
                raise ValueError(f"corrupt PNG: IHDR chunk is {length} bytes, expected 13")
            width, height, depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            seen_ihdr = True
        elif tag == b"PLTE":
            plte = payload
        elif tag == b"tRNS":
            trns = payload
        elif tag == b"IDAT":
            idat += payload
        elif tag == b"IEND":
            break

        pos = end + 4

    if not seen_ihdr:
        raise ValueError("corrupt PNG: no IHDR chunk — this is not a PNG image")
    if width <= 0 or height <= 0:
        raise ValueError(f"corrupt PNG: degenerate size {width}x{height}")
    if not idat:
        raise ValueError("corrupt PNG: no IDAT data — the image has no pixels")
    return width, height, depth, color_type, interlace, bytes(idat), plte, trns


def _expand_rows(
    raw: bytes,
    width: int,
    height: int,
    color_type: int,
    plte: bytes | None,
    trns: bytes | None,
    rgba: bytearray,
) -> None:
    """Defilter scanline by scanline and expand each row into `rgba`.

    Only the previous row is retained: at 8-bit RGBA a 1280x720 frame is
    3.5 MB, and keeping every unfiltered row alive until the end would double
    that for no benefit.
    """
    channels = _COLOR_CHANNELS[color_type]
    stride = width * channels
    bpp = channels
    prev = bytes(stride)
    pos = 0
    out = 0

    for y in range(height):
        ftype = raw[pos]
        pos += 1
        cur = _defilter_row(raw[pos : pos + stride], prev, ftype, bpp, y)
        pos += stride
        _expand_row(cur, width, color_type, plte, trns, rgba, out)
        out += width * 4
        prev = cur


def _defilter_row(raw: bytes, prev: bytes, ftype: int, bpp: int, y: int) -> bytes:
    stride = len(raw)
    if ftype == 0:
        return bytes(raw)

    cur = bytearray(raw)
    if ftype == 1:
        for i in range(bpp, stride):
            cur[i] = (cur[i] + cur[i - bpp]) & 0xFF
    elif ftype == 2:
        for i in range(stride):
            cur[i] = (cur[i] + prev[i]) & 0xFF
    elif ftype == 3:
        for i in range(stride):
            left = cur[i - bpp] if i >= bpp else 0
            cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:
        for i in range(stride):
            left = cur[i - bpp] if i >= bpp else 0
            upleft = prev[i - bpp] if i >= bpp else 0
            cur[i] = (cur[i] + _paeth(left, prev[i], upleft)) & 0xFF
    else:
        raise ValueError(
            f"corrupt PNG: unknown scanline filter type {ftype} on row {y} "
            "— expected 0 (None), 1 (Sub), 2 (Up), 3 (Average) or 4 (Paeth)"
        )
    return bytes(cur)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _expand_row(
    cur: bytes,
    width: int,
    color_type: int,
    plte: bytes | None,
    trns: bytes | None,
    rgba: bytearray,
    out: int,
) -> None:
    if color_type == 3:
        palette = plte or b""
        alpha_table = trns or b""
        for i in range(width):
            index = cur[i]
            base = index * 3
            if base + 2 >= len(palette):
                raise ValueError(
                    f"corrupt PNG: palette index {index} is outside the "
                    f"{len(palette) // 3}-entry PLTE chunk"
                )
            o = out + i * 4
            rgba[o] = palette[base]
            rgba[o + 1] = palette[base + 1]
            rgba[o + 2] = palette[base + 2]
            rgba[o + 3] = alpha_table[index] if index < len(alpha_table) else 255
        return

    if color_type == 0:
        for i in range(width):
            o = out + i * 4
            g = cur[i]
            rgba[o] = g
            rgba[o + 1] = g
            rgba[o + 2] = g
            rgba[o + 3] = 255
        return

    if color_type == 4:
        for i in range(width):
            o = out + i * 4
            g = cur[i * 2]
            rgba[o] = g
            rgba[o + 1] = g
            rgba[o + 2] = g
            rgba[o + 3] = cur[i * 2 + 1]
        return

    if color_type == 2:
        for i in range(width):
            o = out + i * 4
            s = i * 3
            rgba[o] = cur[s]
            rgba[o + 1] = cur[s + 1]
            rgba[o + 2] = cur[s + 2]
            rgba[o + 3] = 255
        return

    for i in range(width):
        o = out + i * 4
        s = i * 4
        rgba[o] = cur[s]
        rgba[o + 1] = cur[s + 1]
        rgba[o + 2] = cur[s + 2]
        rgba[o + 3] = cur[s + 3]


# ── B) half-block rendering ──────────────────────────────────────────────────


def render_halfblock(
    rgba: bytearray, width: int, height: int, cols: int, color: bool = True
) -> str:
    """Render an RGBA frame as half-block art — the BODY only, no header.

    Terminal cells are about twice as tall as they are wide, so one cell
    carries two vertical source pixels: `rows = max(1, round(cols * height /
    width / 2))` cells rows over `cols` cells columns. Each half samples its
    own source rect by BOX AVERAGE (never point sampling — GUI text at 13x13
    reduction degenerates into noise the moment it is point sampled).

    `color=True` emits `▀` with a truecolour fg/bg pair, collapsing to `█`
    when the two halves are the same colour. SGR sequences are emitted only
    when the colour actually changes versus the previous cell, which is what
    keeps a 100-column dump near 7 KB instead of hundreds of KB. `color=False`
    emits the `RAMP` glyph per cell and no escape sequence at all.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"cannot render a {width}x{height} image")
    if cols <= 0:
        raise ValueError(f"cols must be a positive integer, got {cols}")
    if len(rgba) != width * height * 4:
        raise ValueError(
            f"rgba buffer is {len(rgba)} bytes, expected width*height*4 = {width * height * 4}"
        )

    rows = max(1, round(cols * height / width / 2.0))
    cell_h = height / (2 * rows)

    out: list[str] = []
    for row in range(rows):
        y_top0 = int(row * cell_h)
        y_top1 = max(y_top0 + 1, int((row + 1) * cell_h))
        y_bot0 = int((row + 1) * cell_h)
        y_bot1 = max(y_bot0 + 1, int((row + 2) * cell_h))

        if color:
            out.append(_color_row(rgba, width, cols, y_top0, y_top1, y_bot0, y_bot1))
        else:
            out.append(_plain_row(rgba, width, cols, y_top0, y_top1, y_bot0, y_bot1))
    return "\n".join(out)


def _color_row(
    rgba: bytearray,
    width: int,
    cols: int,
    y_top0: int,
    y_top1: int,
    y_bot0: int,
    y_bot1: int,
) -> str:
    parts: list[str] = []
    prev_fg: tuple[int, int, int] | None = None
    prev_bg: tuple[int, int, int] | None = None
    cell_w = width / cols

    for col in range(cols):
        x0 = int(col * cell_w)
        x1 = max(x0 + 1, int((col + 1) * cell_w))
        top = _box_average(rgba, width, x0, x1, y_top0, y_top1)
        bottom = _box_average(rgba, width, x0, x1, y_bot0, y_bot1)

        if top == bottom:
            glyph, fg, bg = FULL_BLOCK, top, bottom
        else:
            glyph, fg, bg = HALF_BLOCK, top, bottom

        if fg != prev_fg:
            parts.append(f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]}m")
            prev_fg = fg
        if bg != prev_bg:
            parts.append(f"\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m")
            prev_bg = bg
        parts.append(glyph)

    parts.append(_SGR_RESET)
    return "".join(parts)


def _plain_row(
    rgba: bytearray,
    width: int,
    cols: int,
    y_top0: int,
    y_top1: int,
    y_bot0: int,
    y_bot1: int,
) -> str:
    cells: list[str] = []
    cell_w = width / cols
    for col in range(cols):
        x0 = int(col * cell_w)
        x1 = max(x0 + 1, int((col + 1) * cell_w))
        top = _box_average(rgba, width, x0, x1, y_top0, y_top1)
        bottom = _box_average(rgba, width, x0, x1, y_bot0, y_bot1)
        luma = 0.299 * ((top[0] + bottom[0]) / 2) + 0.587 * ((top[1] + bottom[1]) / 2) + 0.114 * (
            (top[2] + bottom[2]) / 2
        )
        cells.append(RAMP[_ramp_index(luma)])
    return "".join(cells)


def _ramp_index(luma: float) -> int:
    return max(0, min(9, int(luma * 9 / 255 + 0.5)))


# ── B2) 2x4 sub-cell rendering ───────────────────────────────────────────────


def subcell_rows(width: int, height: int, cols: int) -> int:
    """Cell rows for a `cols`-wide sub-cell art of a `width`x`height` frame.

    Identical to `render_halfblock`'s geometry: `rows = cols * height / width /
    2.0`. The `/ 2.0` is the terminal cell's 1:2 aspect correction and nothing
    else — a cell is 2x4 source pixels, but those sub-pixels are square, so the
    cell still covers the same source rect either way and the two renderers
    always agree on the row count for one image at one width. (Sub-cell art
    buys resolution inside the cell, not fewer rows: halving the rows would
    squash every screen to half height.)
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"cannot render a {width}x{height} image")
    if cols <= 0:
        raise ValueError(f"cols must be a positive integer, got {cols}")
    return max(1, round(cols * height / width / 2.0))


def render_subcell(
    rgba: bytearray,
    width: int,
    height: int,
    cols: int,
    *,
    color: bool = True,
    glyph: str = GLYPH_MIX,
) -> str:
    """Render an RGBA frame as 2x4 sub-cell art — the BODY only, no header.

    One terminal cell carries eight source pixels instead of half-block's two,
    so each cell row resolves four source pixel rows and the cell count is
    exactly `render_halfblock`'s (`subcell_rows`): the sub-pixels are square,
    hence the same `/ 2.0` aspect correction, hence no vertical squash. Each
    sub-cell is box-averaged over its own source rect, never point sampled.

    With `color=True` each cell is reduced to two palette entries (fg, bg) by a
    2-means pass over its eight sub-cell colours, the resulting 8-bit mask
    selects the glyph, and the pair is then contrast-boosted (see
    `_boost_pair`). `█` for a uniform cell, a BLOCK OCTANT for a clean
    two-tone cell, a BRAILLE pattern for anything that should blend. See the
    module docstring for the rule and the `glyph` escape hatch for fonts with
    no octant coverage. SGR pairs are emitted only when a colour actually
    changes, and `color=False` emits the `RAMP` glyph per cell with no escape
    byte at all.
    """
    rows = subcell_rows(width, height, cols)
    if len(rgba) != width * height * 4:
        raise ValueError(
            f"rgba buffer is {len(rgba)} bytes, expected width*height*4 = {width * height * 4}"
        )
    mode = glyph if glyph in GLYPH_MODES else GLYPH_MIX

    out: list[str] = []
    for row in range(rows):
        y_bounds = [_axis_bounds(row * SUBCELL_ROWS + part, SUBCELL_ROWS * rows, height) for part in range(SUBCELL_ROWS)]
        if color:
            out.append(_subcell_color_row(rgba, width, cols, y_bounds, mode))
        else:
            out.append(_subcell_plain_row(rgba, width, cols, y_bounds))
    return "\n".join(out)


def _axis_bounds(index: int, count: int, total: int) -> tuple[int, int]:
    """The pixel slice `index` of `count` even slices over `total` pixels.

    Always at least one pixel, and always inside the image: a 2-pixel-tall
    frame still has to yield four sub-cell rows.
    """
    start = max(0, min(total - 1, index * total // count))
    end = max(start + 1, min(total, (index + 1) * total // count))
    return start, end


def _subcell_samples(
    rgba: bytearray, width: int, x_bounds: list[tuple[int, int]], y_bounds: list[tuple[int, int]]
) -> list[tuple[int, int, int]]:
    """The eight sub-cell colours of one cell, in COLUMN-major order."""
    samples = []
    for x0, x1 in x_bounds:
        for y0, y1 in y_bounds:
            samples.append(_box_average(rgba, width, x0, x1, y0, y1))
    return samples


def _subcell_color_row(
    rgba: bytearray,
    width: int,
    cols: int,
    y_bounds: list[tuple[int, int]],
    mode: str,
) -> str:
    parts: list[str] = []
    prev_fg: tuple[int, int, int] | None = None
    prev_bg: tuple[int, int, int] | None = None

    for col in range(cols):
        x_bounds = [
            _axis_bounds(col * SUBCELL_COLS + part, SUBCELL_COLS * cols, width)
            for part in range(SUBCELL_COLS)
        ]
        glyph, fg, bg = _quantise_cell(_subcell_samples(rgba, width, x_bounds, y_bounds), mode)

        if fg != prev_fg:
            parts.append(f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]}m")
            prev_fg = fg
        if bg != prev_bg:
            parts.append(f"\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m")
            prev_bg = bg
        parts.append(glyph)

    parts.append(_SGR_RESET)
    return "".join(parts)


def _subcell_plain_row(
    rgba: bytearray,
    width: int,
    cols: int,
    y_bounds: list[tuple[int, int]],
) -> str:
    cells: list[str] = []
    for col in range(cols):
        x_bounds = [
            _axis_bounds(col * SUBCELL_COLS + part, SUBCELL_COLS * cols, width)
            for part in range(SUBCELL_COLS)
        ]
        samples = _subcell_samples(rgba, width, x_bounds, y_bounds)
        cells.append(RAMP[_ramp_index(_mean_luma(samples))])
    return "".join(cells)


def _quantise_cell(
    samples: list[tuple[int, int, int]], mode: str
) -> tuple[str, tuple[int, int, int], tuple[int, int, int]]:
    """Pick one cell's glyph and its fg/bg colours from its 8 sub-cells."""
    mean = _mean_colour(samples)
    if _max_distance(samples, mean) <= UNIFORM_SPREAD:
        return FULL_BLOCK, mean, mean

    fg, bg, mask, separation, intra = _two_means(samples)
    if mask == 0:
        return " ", bg, bg
    if mask == 0xFF:
        return FULL_BLOCK, fg, fg

    octant = OCTANT_GLYPHS[mask]
    if octant and (
        mode == GLYPH_OCTANT
        or (mode == GLYPH_MIX and separation >= OCTANT_MIN_SEPARATION and intra <= OCTANT_MAX_INTRA_ERROR)
    ):
        glyph = octant
    else:
        glyph = chr(BRAILLE_BASE + mask)

    boosted = _boost_pair(mean, fg, bg)
    if boosted is None:
        return FULL_BLOCK, mean, mean
    return glyph, boosted[0], boosted[1]


def _boost_pair(
    mean: tuple[int, int, int], fg: tuple[int, int, int], bg: tuple[int, int, int]
) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    """Make a clustered pair readable, symmetrically about `mean`.

    Returns a pair at least `MIN_LUMA_DELTA` apart in luminance and
    `MIN_PAIR_DISTANCE` apart in colour, or None when no in-gamut pair reaches
    that — the caller then collapses the cell to `mean`, because a smooth
    block beats visible noise.

    The half vector `h = (fg - bg) / 2` is scaled along its OWN direction by up
    to `MAX_CONTRAST_BOOST` — the chroma cap, so a noisy cell cannot explode —
    then whatever luminance shortfall is left is added along the NEUTRAL axis
    `(1,1,1)`, the cheapest way to buy luminance per unit of colour error. Both
    steps are clamped to the `[0, 255]` room around `mean`, which is what makes
    a pair pinned at black (or white) unable to meet the floor. A pair that is
    already readable is returned untouched.
    """
    if _readable(fg, bg):
        return fg, bg

    h = _scaled_half_vector(mean, fg, bg)
    room = min(min(mean[i] - h[i], 255 - mean[i] - h[i]) for i in range(3))
    lift = max(0.0, MIN_LUMA_DELTA / 2 - _luma3(h[0], h[1], h[2]))
    for _ in range(_BOOST_STEPS):
        step = min(lift, room)
        half = [component + step for component in h]
        bright = (round(mean[0] + half[0]), round(mean[1] + half[1]), round(mean[2] + half[2]))
        dark = (round(mean[0] - half[0]), round(mean[1] - half[1]), round(mean[2] - half[2]))
        if _readable(bright, dark):
            return bright, dark
        if step >= room:
            return None
        lift += 1.0
    return None


def _readable(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> bool:
    return (
        abs(_luma(fg) - _luma(bg)) >= MIN_LUMA_DELTA and _distance(fg, bg) >= MIN_PAIR_DISTANCE
    )


def _scaled_half_vector(
    mean: tuple[int, int, int], fg: tuple[int, int, int], bg: tuple[int, int, int]
) -> list[float]:
    """`(fg - bg) / 2` scaled up to the contrast floor and gamut-clamped.

    Scaling along the pair's own direction preserves its hue; the clamp is what
    bounds the result inside `[0, 255]` around `mean`.
    """
    h = [(fg[i] - bg[i]) / 2 for i in range(3)]
    span = sum(component * component for component in h) ** 0.5
    luma_span = _luma3(h[0], h[1], h[2])
    needed = max(
        1.0,
        MIN_LUMA_DELTA / 2 / luma_span if luma_span > 0 else 1.0,
        (MIN_PAIR_DISTANCE / 2) / span if span > 0 else 1.0,
    )
    factor = min(needed, MAX_CONTRAST_BOOST)
    return [
        max(-min(mean[i], 255 - mean[i]), min(min(mean[i], 255 - mean[i]), component * factor))
        for i, component in enumerate(h)
    ]


def _luma3(r: float, g: float, b: float) -> float:
    return 0.299 * r + 0.587 * g + 0.114 * b


def _two_means(
    samples: list[tuple[int, int, int]],
) -> tuple[
    tuple[int, int, int],
    tuple[int, int, int],
    int,
    float,
    float,
]:
    """Two palette entries + the 8-bit mask that assigns each sub-cell to one.

    Seeded on the farthest pair of the eight sub-cells (the classic 2-means
    seeding, and it never picks two near-identical centroids), then a few Lloyd
    iterations. Returns `(fg, bg, mask, separation, intra)`: fg is the brighter
    entry so the glyph's dots read as highlights, `separation` is the distance
    between the entries and `intra` the worst distance of a sub-cell from the
    entry it was assigned to — the two numbers the octant/braille rule reads.
    """
    a, b = _farthest_pair(samples)
    for _ in range(_KMEANS_ITERATIONS):
        groups = [[], []]
        for sample in samples:
            groups[0 if _distance(sample, a) <= _distance(sample, b) else 1].append(sample)
        a = _mean_colour(groups[0]) if groups[0] else a
        b = _mean_colour(groups[1]) if groups[1] else b

    if (_luma(a), a) < (_luma(b), b):
        a, b = b, a
    mask = 0
    for index, sample in enumerate(samples):
        if _distance(sample, a) <= _distance(sample, b):
            mask |= 1 << index
    intra = max(_distance(s, a if _distance(s, a) <= _distance(s, b) else b) for s in samples)
    return a, b, mask, _distance(a, b), intra


def _farthest_pair(
    samples: list[tuple[int, int, int]],
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    best = (samples[0], samples[-1])
    best_d = -1.0
    for i, left in enumerate(samples):
        for right in samples[i + 1 :]:
            d = _distance(left, right)
            if d > best_d:
                best_d = d
                best = (left, right)
    return best


def _mean_colour(samples: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    count = len(samples)
    return (
        round(sum(s[0] for s in samples) / count),
        round(sum(s[1] for s in samples) / count),
        round(sum(s[2] for s in samples) / count),
    )


def _max_distance(samples: list[tuple[int, int, int]], colour: tuple[int, int, int]) -> float:
    return max(_distance(s, colour) for s in samples)


def _distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _luma(colour: tuple[int, int, int]) -> float:
    return 0.299 * colour[0] + 0.587 * colour[1] + 0.114 * colour[2]


def _mean_luma(samples: list[tuple[int, int, int]]) -> float:
    return _luma(_mean_colour(samples))


def _box_average(
    rgba: bytearray, width: int, x0: int, x1: int, y0: int, y1: int
) -> tuple[int, int, int]:
    """Average the RGB channels over the pixel rect `[x0,x1) x [y0,y1)`.

    Alpha is intentionally ignored: art is viewed on a terminal, not
    composited, and a half-transparent screenshot would only produce muddy
    averages.
    """
    r = g = b = 0
    count = 0
    for y in range(y0, y1):
        base = y * width
        for x in range(x0, x1):
            o = (base + x) * 4
            r += rgba[o]
            g += rgba[o + 1]
            b += rgba[o + 2]
            count += 1
    if count == 0:
        return (0, 0, 0)
    return (r // count, g // count, b // count)


# ── C) .art file format ──────────────────────────────────────────────────────


def build_art(meta: dict[str, str], body: str) -> str:
    """Compose a whole `.art` file text from a meta mapping and an art body.

    `meta` supplies the header keys (`screen`, `captured`, `source`, `grid`,
    `cell`, `mode`); `body` is written VERBATIM between the sentinels, SGR
    bytes and all, so `parse_art(build_art(m, b))` returns `b` unchanged.
    """
    lines = [ART_VERSION]
    for key in _META_ORDER:
        if key in meta:
            lines.append(f"{key}: {meta[key]}")
    for key in sorted(k for k in meta if k not in _META_ORDER):
        lines.append(f"{key}: {meta[key]}")
    return "\n".join(lines) + f"\n{ART_BEGIN}\n" + body + f"\n{ART_END}\n"


def parse_art(text: str) -> ArtFile:
    """Parse a `.art` file text into its meta mapping and verbatim body.

    Raises `ValueError` when the version marker or either sentinel is missing:
    a silently half-parsed art file would render as an empty screen, which is
    indistinguishable from a real blank screen.
    """
    if not text.startswith(ART_VERSION):
        raise ValueError(
            f"not a screen-art file: expected the first line to be {ART_VERSION!r}"
        )

    begin = text.find(ART_BEGIN)
    if begin < 0:
        raise ValueError(f"not a screen-art file: missing the {ART_BEGIN!r} sentinel")
    end = text.find(ART_END, begin + len(ART_BEGIN))
    if end < 0:
        raise ValueError(f"not a screen-art file: missing the {ART_END!r} sentinel")

    header = text[len(ART_VERSION) : begin]
    meta: dict[str, str] = {}
    for line in header.splitlines():
        line = line.strip()
        if not line:
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"not a screen-art file: malformed header line {line!r}")
        meta[key.strip()] = value.strip()

    # The body owns everything between the sentinels except the single newline
    # that terminates its last line — so a body ending in "\n" round-trips.
    body = text[begin + len(ART_BEGIN) + 1 : end]
    if body.endswith("\n"):
        body = body[:-1]

    cols, rows = _parse_grid(meta.get("grid", ""))
    return ArtFile(meta=meta, body=body, cols=cols, rows=rows)


def _parse_grid(grid: str) -> tuple[int, int]:
    cols_s, sep, rows_s = grid.partition("x")
    if not sep:
        return (0, 0)
    try:
        return (int(cols_s.strip()), int(rows_s.strip()))
    except ValueError:
        return (0, 0)


def strip_sgr(text: str) -> str:
    """Remove every SGR (`ESC [ ... m`) sequence, leaving the glyphs."""
    return _SGR_RE.sub("", text)


def art_to_plain(art: ArtFile, cols: int | None = None) -> str:
    """Convert an `.art` file to plain, SGR-free ramp text.

    A half-block cell has two colours but one glyph, so the grey that
    survives is the upper-half foreground for `▀` and the average of
    foreground and background for everything else (`█`, space). A 2x4 sub-cell
    glyph covers a known FRACTION of the cell — its bit count over eight — so
    its grey is the fg/bg pair blended by that fraction, which is what makes a
    braille or octant cell read as the mid-tone it looks like on screen. Rows
    without any SGR — a `ramp-plain` file — pass through untouched.

    `cols` re-quantises horizontally by sampling; pass it when the target
    terminal is narrower than the file's recorded grid.
    """
    rows: list[list[str]] = []
    for line in art.body.split("\n"):
        fg: tuple[int, int, int] | None = None
        bg: tuple[int, int, int] | None = None
        cells: list[str] = []
        for token in _SGR_SPLIT_RE.split(line):
            if not token:
                continue
            if token.startswith("\x1b["):
                fg, bg = _apply_sgr(token, fg, bg)
                continue
            for ch in token:
                cells.append(_plain_cell(ch, fg, bg))
        rows.append("".join(_requantise(cells, cols)))

    return "\n".join(rows)


def _coverage(ch: str) -> float | None:
    """The fraction of the cell `ch` paints, or None for a non-sub-cell glyph.

    Both glyph families encode their mask directly in the codepoint — braille
    as `0x2800 + mask`, octants as a run of name-ordered masks — so the
    covered fraction is a popcount either way and needs no table.
    """
    code = ord(ch)
    if ch == FULL_BLOCK:
        return 1.0
    if ch == " ":
        return 0.0
    if BRAILLE_BASE <= code <= BRAILLE_BASE + 0xFF:
        return bin(code - BRAILLE_BASE).count("1") / SUBCELL_COUNT
    if OCTANT_FIRST_CODEPOINT <= code <= OCTANT_LAST_CODEPOINT:
        return bin(code - OCTANT_FIRST_CODEPOINT).count("1") / SUBCELL_COUNT
    return None


def _apply_sgr(
    seq: str, fg: tuple[int, int, int] | None, bg: tuple[int, int, int] | None
) -> tuple[tuple[int, int, int] | None, tuple[int, int, int] | None]:
    body = seq[2:-1]
    params = [int(p) if p else 0 for p in body.split(";")] if body else [0]
    i = 0
    while i < len(params):
        p = params[i]
        if p == 0:
            fg = bg = None
        elif p in (38, 48) and i + 4 < len(params) and params[i + 1] == 2:
            colour = (params[i + 2], params[i + 3], params[i + 4])
            if p == 38:
                fg = colour
            else:
                bg = colour
            i += 4
        i += 1
    return fg, bg


def _plain_cell(
    ch: str, fg: tuple[int, int, int] | None, bg: tuple[int, int, int] | None
) -> str:
    if fg is None and bg is None:
        return ch
    coverage = _coverage(ch)
    if coverage is not None and fg is not None and bg is not None:
        colour = tuple(
            round(fg[i] * coverage + bg[i] * (1.0 - coverage)) for i in range(3)
        )
    elif ch == HALF_BLOCK and fg is not None:
        colour = fg
    else:
        parts = [c for c in (fg, bg) if c is not None]
        if not parts:
            return ch
        colour = tuple(sum(c[ch_index] for c in parts) // len(parts) for ch_index in range(3))
    return RAMP[_ramp_index(_luma(colour))]


def _requantise(cells: list[str], cols: int | None) -> list[str]:
    if not cols or cols <= 0 or not cells or cols == len(cells):
        return cells
    n = len(cells)
    return [cells[min(n - 1, (col * n) // cols)] for col in range(cols)]
