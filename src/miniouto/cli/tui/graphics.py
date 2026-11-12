"""Terminal graphics for the TUI — real kitty/sixel images, via ``xnano_image``.

Why this module exists
----------------------
The TUI paints its own screen: every cell of a frame is written by the terminal
backend's canvas. There is no widget to mount an image into and no canvas region
the renderer will agree to leave alone, so a picture is placed the way the
terminal's graphics protocol defines it — by emitting escape sequences the canvas
never sees.

The ORDER is the whole design. ``GraphicsOverlay.emit_post_draw()`` runs AFTER
``Terminal.draw()`` has returned, i.e. once the frame's cell output is already on
the wire. A sequence written between two frames can therefore never land in the
middle of one: the terminal sees a complete frame, then a complete image command.
The image occupies cells the app does not paint — the app reads
``GraphicsOverlay.clear_regions()`` and fills those rects with blanks in the frame
it draws, because the next frame's diff would otherwise rewrite the image's cells
as spaces and erase the picture.

Where the pixels come from
--------------------------
``xnano_image.TerminalImage.open(png).to_cell_grid(cols, rows, graphics=…)``
returns a packed ``XCG1`` cell grid in which the kitty/sixel protocol payload IS
a cell's symbol, and ``xnano_core.cell_grid_to_ansi`` turns that grid into the
rows a terminal understands. ``xnano_image`` places a picture; this module owns
the terminal-side LIFECYCLE that a one-shot "render this image" call leaves out.

Upload once, place many
-----------------------
This is the ``cli/graphics.py:CachedTGPImage`` design, carried over and now
enforced here. Kitty's protocol separates a TRANSMISSION (the base64 pixels —
~1.8 MB for a 1280x720 capture) from a PLACEMENT (a few control parameters plus
one placeholder cell per grid cell). ``xnano_image``'s grid is a
transmit-and-display (``a=T``), so re-emitting it every frame would re-send the
whole payload: that is the "opening a screenshot lags" symptom the old TUI had,
where every repaint re-encoded and re-transmitted the PNG.

So the grid is encoded ONCE, when the placement is created, and split into two
row sets:

* the UPLOAD rows — the grid exactly as ``xnano_image`` produced it, carrying the
  ``a=T`` transmission;
* the PLACE rows — the same grid with that transmission replaced by a place-only
  ``a=p`` command keeping the same ``i=<id>``.

The first frame emits the upload; every later frame emits only the place-only
copy, which is a few hundred bytes of placeholders instead of megabytes of
base64. The image id is read out of the transmission, and that single number is
what lets the re-place and the delete address the very image the terminal already
holds. Nothing re-encodes on scroll or resize: the grid is a function of the
image and its cell size, both fixed at creation.

Sixel has no such split — it is a byte stream the terminal renders and forgets —
so its ENCODING is what gets cached, and no delete is ever needed.

Deleting
--------
A kitty terminal keeps an image until told otherwise, and ``xnano_image`` never
tells it: the grid carries no ``a=d`` and dropping a buffer sends no command. So
every capture ever expanded would sit in the terminal's image store for the life
of the session. ``ImagePlacement.cleanup()`` therefore sends ``a=d,d=I,i=<id>``
itself, and that is why the row's ``release_image()`` calls it when the row goes
away.

Where the bytes go
------------------
``emit_post_draw()`` writes the protocol bytes after the draw, through this
module's transport: by default raw bytes on stdout at ``_write``. That is the
whole interface — the overlay needs no canvas, no buffer and no cooperation from
the render loop, so the terminal backend stays free to paint however it likes.

The transport is ONE seam for the whole module, not one per overlay: a caller that
owns the output (a real-screen backend, an inline screen, a recorder, a test)
calls ``set_writer()`` and from then on EVERY escape the lane emits goes through
it — the frame's placements AND the delete a row's removal sends, which is why
the writer cannot live on the overlay alone. ``GraphicsOverlay(write=…)`` takes
the same callable for a caller that only wants to redirect one overlay.

Inside tmux the protocol commands are wrapped in the passthrough envelope, because
tmux swallows an APC/DCS it does not implement.

The `.art` text lane is untouched
---------------------------------
GRAPHICS_MODE_TEXT makes ``ImagePlacement.create`` return None, and the row then
renders the ``.art`` truecolour text render it always did. TEXT is the answer to
every failure here — no tty, no probe answer, an undecodable PNG, a protocol the
library will not encode — because the image is a bonus and the chat must never
break for it.
"""

from __future__ import annotations

import contextlib
import os
import re
from typing import Any, NamedTuple

# ── graphics mode ───────────────────────────────────────────────────────────
#
# The mode is decided ONCE, BEFORE the app's terminal starts: the probe asks the
# terminal for its capabilities and reads the answer from standard input, and a
# full-screen session owns stdin the moment it boots — after that the answer is
# never seen and the probe only burns its timeout. So the check belongs at boot,
# not in the render path (invariant 15).
#
# `TEXT`  — the `.art` truecolour text render (always available)
# `TGP`   — Kitty's Terminal Graphics Protocol (pixel-exact images)
# `SIXEL` — the Sixel graphics protocol
#
# The mode is a module-level value, set by `detect_graphics_mode()` and read by
# the render path. A failed probe (no library, no tty, a dead stdout, a timeout)
# is TEXT — the unchanged fallback, never an error.
GRAPHICS_MODE_TEXT = "text"
GRAPHICS_MODE_TGP = "tgp"
GRAPHICS_MODE_SIXEL = "sixel"

_GRAPHICS_MODE: str | None = None

_GRAPHICS_PROTOCOLS = (GRAPHICS_MODE_TGP, GRAPHICS_MODE_SIXEL)

# Our mode name → the protocol name `xnano_image` knows it by.
_PROTOCOL_NAMES = {GRAPHICS_MODE_TGP: "kitty", GRAPHICS_MODE_SIXEL: "sixel"}

# Cursor save/restore: DECSC/DECRC (ESC 7 / ESC 8). The overlay must leave the
# cursor exactly where the canvas left it, or the next frame's diff writes every
# dirty cell one row too low.
_SAVE_CURSOR = b"\x1b7"
_RESTORE_CURSOR = b"\x1b8"

# The protocol delimiters this module splits a grid on.
_APC_START = "\x1b_G"
_DCS_START = "\x1bP"
_APC_END = "\x1b\\"

_STDOUT_FD = 1

# `xnano_image` caps each side of a cell box at 1000 cells; keep our derived box
# inside what it will accept rather than letting it raise.
_MAX_CELLS = 1000

# The cell size used when the probe never ran: the 10x20 that
# `xnano_image.TerminalGraphics.half_blocks()` assumes.
_DEFAULT_CELL_SIZE = (10, 20)

_ID_RE = re.compile(r"\bi=(\d+)")

# A protocol command runs from its introducer to its string terminator. Kitty
# payloads are base64 and sixel payloads contain no ESC, so the non-greedy run
# can never stop early. Used ONLY for the tmux passthrough envelope; the data
# itself is built elsewhere.
_PROTOCOL_RUN_RE = re.compile(
    re.escape(_APC_START.encode())
    + rb".*?"
    + re.escape(_APC_END.encode())
    + b"|"
    + re.escape(_DCS_START.encode())
    + rb".*?"
    + re.escape(_APC_END.encode()),
    re.DOTALL,
)


class _Capabilities(NamedTuple):
    """What the boot probe learned: which protocols exist, and the cell size.

    The probe hands back a ``TerminalGraphics``, but that type cannot be built
    from a capability LIST (``from_capabilities`` leaves ``.capabilities`` empty)
    — and the Sixel-over-TGP preference is decided from that list. So the probe's
    answer is normalised here, once, which is also the single seam a test can
    replace: real terminal probing cannot be faked.
    """

    protocols: frozenset[str]
    """The graphics protocols the terminal answered with, e.g. ``{"kitty"}``."""

    cell_size_in_pixels: tuple[int, int]
    """One terminal cell in pixels, ``(width, height)``."""


_DETECTED: _Capabilities | None = None
"""The probe's answer, for the placements built after it. None = never probed."""


def _warm_protocol_imports() -> None:
    """Import the image libraries now, at boot, on purpose.

    ``xnano_image`` and ``xnano_core`` are what decode and pack the pixels;
    settling them while the app boots is better than paying the import on the
    first screenshot the user is waiting for (the same reason the old TUI
    imported its image widgets inside ``detect_graphics_mode()``). A failure here
    means the image lane cannot work at all, and ``detect_graphics_mode`` turns it
    into TEXT.
    """

    import xnano_core  # noqa: F401
    import xnano_image  # noqa: F401


def _probe_terminal_graphics() -> _Capabilities:
    """Ask the terminal what it can draw, ONCE.

    ``TerminalGraphics.detect`` writes an escape query and reads the answer off
    stdin with a two-second timeout. It raises ``TerminalQueryError`` when stdin
    or stdout is not a tty, and answers ``half_blocks`` (an empty capability list)
    when the terminal does not reply at all — both of which this module reads as
    "no graphics".

    A separate function rather than an inline call because it is the probe SEAM:
    the xnano types are immutable native classes and cannot themselves be patched.
    """

    from xnano_image import TerminalGraphics

    graphics = TerminalGraphics.detect()
    width, height = graphics.cell_size_in_pixels
    protocols = frozenset(graphics.capabilities) & set(_PROTOCOL_NAMES.values())
    return _Capabilities(protocols, (int(width), int(height)))


def detect_graphics_mode() -> str:
    """Probe the terminal ONCE and pick the graphics mode.

    Call this BEFORE the terminal and its input loop start (see ``run_tui``). The
    probe reads the terminal's answer off stdin, and the app's input loop takes
    stdin over the moment it runs; after that the answer is never seen and the
    probe only burns its timeout. So the check belongs here, not in the render
    path.

    Returns ``GRAPHICS_MODE_TGP`` for Kitty-style graphics,
    ``GRAPHICS_MODE_SIXEL`` for Sixel, and ``GRAPHICS_MODE_TEXT`` when neither is
    available — also the result of ANY failure (a missing library, a piped stdout,
    a timeout, an unparsable reply). The result is cached for the process,
    together with the cell size every placement needs.

    **Sixel wins when the terminal offers both.** TGP is the better protocol, but
    its "image placement" scheme needs a terminal that implements the placeholder
    protocol properly, and several terminals only *report* TGP support (the query
    is answered) without implementing it — Konsole and wezterm are the known
    cases, and there the TGP image never appears. Sixel is a plain byte stream
    that any Sixel terminal renders, so it is the safe choice whenever it is
    available. Only a terminal with Sixel *absent* gets TGP — in practice Kitty,
    which is exactly the terminal that implements the placeholder scheme
    properly. (ratatui-image's own picker prefers Sixel for the same reason.)

    When a graphics mode is found, the image libraries are imported HERE too:
    paying their import during boot is better than paying it on the first
    screenshot the user is waiting for.
    """

    global _GRAPHICS_MODE, _DETECTED
    if _GRAPHICS_MODE is not None:
        return _GRAPHICS_MODE
    try:
        capabilities = _probe_terminal_graphics()
    except Exception:
        # No library, no tty, a closed stdout, a timeout: the text lane is the
        # answer, and it is always available.
        _DETECTED = None
        _GRAPHICS_MODE = GRAPHICS_MODE_TEXT
        return _GRAPHICS_MODE
    mode = GRAPHICS_MODE_TEXT
    if "sixel" in capabilities.protocols:
        mode = GRAPHICS_MODE_SIXEL
    elif "kitty" in capabilities.protocols:
        mode = GRAPHICS_MODE_TGP
    if mode != GRAPHICS_MODE_TEXT:
        try:
            # Warm the imports now so the first screenshot renders without an
            # import stall.
            _warm_protocol_imports()
        except Exception:
            # A library that will not import cannot place anything, so the text
            # lane is the honest answer rather than a broken image.
            mode = GRAPHICS_MODE_TEXT
    _DETECTED = capabilities if mode != GRAPHICS_MODE_TEXT else None
    _GRAPHICS_MODE = mode
    return mode


def graphics_mode() -> str:
    """The graphics mode for the render path — NEVER probes.

    The probe is a terminal round-trip, and doing it lazily here (i.e. after the
    app has started) is exactly the stall this design avoids. So this reads the
    detected mode and reports TEXT when detection never ran: an undetected
    terminal is treated as a non-graphics one, which is the safe,
    always-available lane.
    """

    return _GRAPHICS_MODE or GRAPHICS_MODE_TEXT


def terminal_graphics_supported() -> bool:
    """True when the terminal can show REAL graphics (Kitty TGP or Sixel)."""

    return graphics_mode() != GRAPHICS_MODE_TEXT


def cell_size_in_pixels() -> tuple[int, int]:
    """The terminal cell in pixels — the probed one, else the 10x20 default.

    This is what turns a cell count into a pixel box: the encoder needs it to
    derive one side from the other through the capture's aspect ratio, and it must
    be the SAME size the grid was encoded with, or the placeholder grid would not
    line up with the pixels.
    """

    if _DETECTED is not None:
        return _DETECTED.cell_size_in_pixels
    return _DEFAULT_CELL_SIZE


# ── transport ───────────────────────────────────────────────────────────────

_WRITER: Any = None
"""A caller-supplied transport (see ``set_writer``). None = raw stdout."""


def set_writer(write: Any) -> None:
    """Route EVERY escape this module emits through ``write`` (``None`` = stdout).

    The lane's one transport seam. It is module-level rather than per-overlay
    because two very different moments emit bytes — the frame's placements and the
    delete a removed row sends later — and a backend that owns the output has to
    capture both. A test replaces ``_write`` directly instead, which is the same
    idea one level down.
    """

    global _WRITER
    _WRITER = write


def _emit(data: bytes) -> None:
    """Hand ``data`` to the current transport — the seam's single entry point.

    Everything this module writes goes through here, so redirecting the lane is
    one assignment and no call site can miss it.
    """

    writer = _WRITER
    if writer is None:
        _write(data)
        return
    writer(data)


def _write(data: bytes) -> None:
    """Write raw bytes to stdout.

    ``os.write`` on fd 1, not ``print``/``sys.stdout.write``: the payload is a
    binary protocol message (base64 inside a control sequence, or raw sixel
    bytes), and the overlay runs after the frame, so nothing may buffer or reorder
    it behind the canvas's own output.

    The loop handles a short write: ``os.write`` may accept fewer bytes than it
    was handed (a full pty buffer under a large image), and a TRUNCATED control
    sequence or sixel stream corrupts the terminal's parser instead of merely
    dropping a picture.

    This is the single choke point — every escape this module emits goes through
    it (or through a writer a caller supplied).
    """

    view = memoryview(data)
    while view:
        written = os.write(_STDOUT_FD, view)
        if written <= 0:  # pragma: no cover - a closed stdout raises instead
            raise OSError("stdout accepted no bytes")
        view = view[written:]


def _wrap_for_tmux(data: bytes) -> bytes:
    """Wrap every protocol command in tmux's passthrough envelope.

    tmux parses the pane's output itself and drops an APC/DCS it does not
    implement, so an image command never reaches the outer terminal. The
    passthrough (``ESC P tmux;`` … ``ESC \\``, with every ESC inside doubled) is
    how the bytes get through. Only the protocol RUNS are wrapped: SGR and the
    placeholder text are ordinary pane output and must stay unwrapped.

    A no-op outside tmux, which is the common case.
    """

    if not os.environ.get("TMUX"):
        return data

    def envelope(match: re.Match[bytes]) -> bytes:
        return b"\x1bPtmux;" + match.group(0).replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"

    return _PROTOCOL_RUN_RE.sub(envelope, data)


def _tgp_message(**kwargs: Any) -> bytes:
    """Build one kitty control sequence: ``ESC _ G <key=value,…> ESC \\``.

    ``None`` values are dropped, so a caller states only what it means: ``i`` and
    ``a`` for a delete, plus the geometry for a placement.
    """

    body = ",".join(f"{key}={value}" for key, value in kwargs.items() if value is not None)
    return f"{_APC_START}{body}{_APC_END}".encode()


def _delete_message(image_id: int) -> bytes:
    """``a=d,d=I,i=<id>`` — drop the image AND its pixel data from the terminal."""

    return _tgp_message(a="d", d="I", i=image_id)


def _move_to(x: int, y: int) -> bytes:
    """CUP to a 0-based cell, i.e. the 1-based ``ESC[row;colH``."""

    return f"\x1b[{y + 1};{x + 1}H".encode()


def _cell_size_arg(value: Any) -> tuple[int, int] | None:
    """Normalise a caller's cell size: a ``(w, h)`` pair or a CellSize-like."""

    if value is None:
        return None
    width = getattr(value, "width", None)
    height = getattr(value, "height", None)
    pair = (width, height) if width is not None and height is not None else tuple(value)
    return (max(1, int(pair[0])), max(1, int(pair[1])))


def _terminal_graphics(protocol: str, cell_size: tuple[int, int]) -> Any:
    """The ``TerminalGraphics`` the encoder needs for one protocol.

    Built from KNOWN capabilities rather than probed: the probe happens once at
    boot and a placement must never trigger a terminal round-trip.
    """

    from xnano_image import TerminalGraphics

    name = _PROTOCOL_NAMES.get(protocol)
    if name is None:
        raise ValueError(f"no graphics protocol for mode {protocol!r}")
    return TerminalGraphics.from_capabilities(
        protocol=name, cell_size_in_pixels=(cell_size[0], cell_size[1])
    )


def _transmission_span(row: str) -> tuple[int, int]:
    """``(start, end)`` of the kitty transmission block at the head of ``row``.

    A transmission is chunked: a 1280x720 capture is ~157 consecutive ``ESC _ G
    …`` runs, the last ending the message with ``m=0``. They are contiguous, so
    the block runs from the first introducer to the terminator of the last one,
    and that span is exactly what the place-only copy replaces.

    ``(-1, -1)`` when the row carries no transmission (nothing to place).
    """

    start = row.find(_APC_START)
    if start < 0:
        return -1, -1
    end = start
    while row.startswith(_APC_START, end):
        terminator = row.find(_APC_END, end)
        if terminator < 0:
            break
        end = terminator + len(_APC_END)
    return start, end


def _dcs_stream(rows: list[str]) -> bytes:
    """The sixel byte stream out of a grid's ANSI rows.

    A sixel grid puts the whole DCS in ONE cell's symbol and pads the rest of the
    rect with blanks, so the stream is extracted rather than emitted
    positionally: this module positions the cursor with CUP and lets the DCS paint
    from there, which is what makes a sixel placement independent of the grid's
    own internal cursor juggling (and of the cell grid at all).
    """

    text = "\n".join(rows)
    start = text.find(_DCS_START)
    if start < 0:
        return b""
    end = text.find(_APC_END, start)
    if end < 0:
        return b""
    return text[start : end + len(_APC_END)].encode("ascii")


def _cell_box(
    cell_size: tuple[int, int],
    pixel_width: int,
    pixel_height: int,
    width_cells: int | None,
    height_cells: int | None,
) -> tuple[int, int]:
    """The ``(columns, rows)`` box a capture occupies, in cells.

    An explicitly given side wins; the other follows from the capture's aspect
    ratio through the terminal's cell size, rounded to the nearest cell. This is
    the old widget's ``width: 100%`` + ``height: auto`` expressed in cells: a 64x36
    capture at 40 cells on a 10x20 cell is 40x11 cells (40 cells = 400 px wide,
    400 * 36/64 = 225 px tall, i.e. 11.25 cells), not 40x12.

    With neither side given the natural size is the capture's pixels over the
    terminal's cell, which is what the old widget did at 100% on a small capture.
    """

    cell_width, cell_height = cell_size
    aspect = pixel_height / max(1, pixel_width)
    if width_cells is not None:
        columns = int(width_cells)
        rows = round(columns * cell_width * aspect / cell_height)
    elif height_cells is not None:
        rows = int(height_cells)
        columns = round(rows * cell_height / (aspect * cell_width))
    else:
        columns = round(pixel_width / cell_width)
        rows = round(pixel_height / cell_height)
    return max(1, min(_MAX_CELLS, columns)), max(1, min(_MAX_CELLS, rows))


# ── the unit the chat log renders ───────────────────────────────────────────


class ImagePlacement:
    """One image, encoded once at its cell size, placeable in any frame.

    ``png_path`` is the raw capture the Computer tool wrote. The cell size comes
    from the probed terminal (``cell_size_in_pixels()``) or the caller's
    ``cell_size=``, and ``xnano_image`` derives the cell grid from the capture's
    PIXEL size and that cell size — the same maths the old widget used. Passing
    ``width_cells`` reproduces the old widget's ``width: 100%`` + ``height: auto``:
    the image fills the log's cell width and its height follows the capture's
    aspect ratio. Passing neither gives the capture's natural cell size.

    Constructing one may raise (a missing PNG, an undecodable one, an unusable
    protocol, a cell size ``xnano_image`` rejects); callers in the render path use
    ``create()``, which turns every failure into the TEXT lane.
    """

    def __init__(
        self,
        png_path: str | os.PathLike[str],
        width_cells: int | None = None,
        height_cells: int | None = None,
        protocol: str | None = None,
        *,
        cell_size: Any | None = None,
    ) -> None:
        self.png_path = str(png_path)
        self.protocol = graphics_mode() if protocol is None else protocol
        if self.protocol not in _GRAPHICS_PROTOCOLS:
            raise ValueError(f"no graphics protocol for mode {self.protocol!r}")
        self._cell_size = _cell_size_arg(cell_size) or cell_size_in_pixels()
        self._graphics = _terminal_graphics(self.protocol, self._cell_size)
        # The rect the app must blank and the overlay place. Filled by _encode.
        self.width_cells = 0
        self.height_cells = 0
        # Kitty's handle on the pixels: what the place-only rows and the delete
        # address. None for sixel (which keeps nothing) and None once deleted.
        self.image_id: int | None = None
        self._grid: bytes = b""
        self._upload_rows: list[str] = []
        self._place_rows: list[str] = []
        self._sixel: bytes | None = None
        self._transmitted = False
        self._encode(width_cells, height_cells)

    @classmethod
    def create(
        cls,
        png_path: str | os.PathLike[str],
        width_cells: int | None = None,
        height_cells: int | None = None,
        protocol: str | None = None,
        *,
        cell_size: Any | None = None,
    ) -> ImagePlacement | None:
        """Build a placement, or None when the TEXT lane must take over.

        ANY failure — no graphics mode, a missing or unreadable PNG, a protocol
        that cannot encode it — is a silent ``None``. The image lane's whole
        contract is that it never raises into the render path and that the
        ``.art`` text render is always the fallback.
        """

        try:
            return cls(png_path, width_cells, height_cells, protocol, cell_size=cell_size)
        except Exception:
            return None

    def _encode(self, width_cells: int | None, height_cells: int | None) -> None:
        """Encode the cell grid and prepare both row sets from it.

        This is the expensive step (decode, scale, base64) and it runs ONCE per
        placement: the grid is a function of the image and the cell size, both
        fixed here, so a repaint, a scroll or a resize has nothing left to
        compute. Encoding at creation also makes ``width_cells``/``height_cells``
        the grid's REAL size rather than an estimate — the app blanks exactly
        those cells, and a one-cell estimate error shows up as a strip of log text
        painted over the picture.

        ``fit="stretch"`` fills the box the aspect maths derived, which is what
        the old code did: it scaled the PNG to exactly ``width_cells x height_cells``
        pixels, so the residual sub-cell rounding is scaled away rather than
        letterboxed into blank rows.

        A zero-sized request stays zero: there is no box to encode, nothing to
        emit, and the encoder rejects a 0-column box.
        """

        from xnano_image import TerminalImage, cell_grid_size

        if width_cells is not None and int(width_cells) <= 0:
            return
        if height_cells is not None and int(height_cells) <= 0:
            return
        image = TerminalImage.open(self.png_path)
        columns, rows = _cell_box(
            self._cell_size, image.width, image.height, width_cells, height_cells
        )
        grid = image.to_cell_grid(columns, rows, fit="stretch", graphics=self._graphics)
        self._grid = grid
        self.width_cells, self.height_cells = cell_grid_size(grid)
        self._build_rows()

    def _build_rows(self) -> None:
        """Split the grid into upload rows (once) and place-only rows (forever).

        The per-frame cost of an image is what this produces: the place-only rows
        are the grid with its transmission swapped for ``a=p``, so a repaint
        re-sends only placeholders. Building both here — once — is what keeps a
        repaint free of both the encode and the base64.
        """

        from xnano_core import cell_grid_to_ansi

        rows = cell_grid_to_ansi(self._grid).split("\n")
        if self.protocol == GRAPHICS_MODE_SIXEL:
            self._sixel = _dcs_stream(rows)
            return
        carrier = next(
            (index for index, row in enumerate(rows) if _APC_START in row), None
        )
        if carrier is None:
            # A grid with no transmission has no image to place: emitting its
            # blank cells would only paint over the rect the app already blanked.
            return
        row = rows[carrier]
        start, end = _transmission_span(row)
        match = _ID_RE.search(row[start:end])
        if match is None:
            return
        self.image_id = int(match.group(1))
        # `c`/`r` scale the already-transmitted pixels onto the placeholder grid
        # and `U=1` selects the placeholder mode the grid is built for.
        place = _tgp_message(
            q=2,
            i=self.image_id,
            a="p",
            U=1,
            c=self.width_cells,
            r=self.height_cells,
        ).decode("ascii")
        self._upload_rows = rows
        self._place_rows = [*rows[:carrier], row[:start] + place + row[end:], *rows[carrier + 1 :]]

    # ── frame output ──────────────────────────────────────────────────────

    def rows_ansi(self) -> list[str]:
        """The grid rows to emit for this frame — upload ONCE, then place only.

        Until the upload has gone out these are the rows carrying the
        transmission, because the terminal does not have the pixels yet; after it
        they are the place-only copy, which reuses the id the upload created.
        That switch is the upload-once guarantee, and losing it is what made
        scrolling re-send ~1.8 MB a frame.
        """

        if not self._upload_rows:
            return []
        return self._place_rows if self._transmitted else self._upload_rows

    def sixel_bytes(self) -> bytes:
        """The encoded sixel stream at this placement's cell size, cached.

        Sixel is a byte stream, not a terminal-side image store, so there is
        nothing to upload or delete: the ENCODING is the thing worth caching, the
        counterpart of the TGP lane's upload-once.
        """

        return self._sixel or b""

    def emit_at(self, x: int, y: int) -> bytes:
        """The escape bytes that paint this image at cell (x, y).

        Each grid row is placed with its own CUP: the TGP payload lives in a CELL,
        and a bare newline between rows would depend on the terminal's line-feed
        semantics. The cursor is saved and restored around the whole thing,
        because everything the overlay writes is out of band and the position the
        canvas believes it left must survive untouched.

        A placement with no cells emits nothing — an empty rect has no pixels to
        show, and a zero-width CUP would move the cursor for nothing.
        """

        if self.width_cells <= 0 or self.height_cells <= 0:
            return b""
        if self.protocol == GRAPHICS_MODE_SIXEL:
            # Sixel is a byte stream: the encode IS the placement.
            stream = self.sixel_bytes()
            if not stream:
                return b""
            return bytes(_SAVE_CURSOR + _move_to(x, y) + stream + _RESTORE_CURSOR)
        rows = self.rows_ansi()
        if not rows:
            # Nothing to place: a cleaned-up (or transmission-less) placement must
            # not even move the cursor, or every frame would save/restore for
            # nothing.
            return b""
        out = bytearray(_SAVE_CURSOR)
        for offset, row in enumerate(rows):
            out += _move_to(x, y + offset)
            out += row.encode("utf-8")
        # These bytes ARE the transmission when it is the upload's turn, so from
        # here on the terminal holds the pixels: every later frame may only place.
        self._transmitted = True
        out += _RESTORE_CURSOR
        return bytes(out)

    def cleanup(self) -> None:
        """Delete the image from the terminal when its row goes away.

        A kitty terminal holds the pixels until told otherwise, so without this
        every capture ever expanded would leave its image in the terminal's store
        for the session. Sixel needs no delete — its bytes are not retained.

        Only an image that was actually SENT is deleted (a row that was created
        and removed before its first frame has nothing in the terminal to free),
        and the id is dropped even if the write fails: a dead stdout must not make
        us try to delete the same image twice.
        """

        image_id = self.image_id
        if image_id is None:
            return
        # The rows address an id that is going away, so a late re-emit writes
        # nothing rather than placing a deleted (or never-sent) image.
        self.image_id = None
        self._upload_rows = []
        self._place_rows = []
        sent, self._transmitted = self._transmitted, False
        if not sent or self.protocol != GRAPHICS_MODE_TGP:
            return
        with contextlib.suppress(Exception):
            _emit(_wrap_for_tmux(_delete_message(image_id)))


class GraphicsOverlay:
    """The placements of ONE frame, and the sequence that paints them.

    The app builds one per frame: ``add()`` each visible placement at its cell
    position, draw the frame (with ``clear_regions()`` blanked), then call
    ``emit_post_draw()``. Nothing here touches the frame — that is the point of
    having no widget system to integrate with.

    ``write`` is this overlay's transport. Left as ``None`` it uses the module seam
    (``set_writer()``, defaulting to raw stdout after the draw), which is what an
    app should do: the module seam also carries the DELETE a row's removal sends
    later, and only a module-level hook can cover both moments. Passing a writer
    here redirects THIS overlay alone, which is what a recorder or a one-off test
    wants.
    """

    def __init__(self, write: Any = None) -> None:
        self._placements: list[tuple[ImagePlacement, int, int]] = []
        self._write = write

    def set_writer(self, write: Any) -> None:
        """Route this overlay's output through ``write`` (``None`` = the module seam)."""

        self._write = write

    def add(self, placement: ImagePlacement, x: int, y: int) -> None:
        """Place ``placement`` with its top-left corner at cell (x, y) (0-based)."""

        self._placements.append((placement, int(x), int(y)))

    def clear_regions(self) -> list[tuple[int, int, int, int]]:
        """The ``(x, y, w, h)`` cell rects the app must BLANK in its render.

        The frame's diff owns every cell it was told about, so each image rect has
        to be rendered as spaces. Without that the log's text paints over the
        pixels, and the following diff rewrites the image's cells as blanks and
        erases the picture.
        """

        return [
            (x, y, placement.width_cells, placement.height_cells)
            for placement, x, y in self._placements
        ]

    def emit_post_draw(self) -> None:
        """Write every placement AFTER the frame has been painted.

        Running post-draw is what makes the sequences safe: the frame's cell
        output has already flushed, so an image command can never be split by a
        cell write. Everything is collected into ONE buffer and written once, so a
        frame's images cost one syscall rather than one per placement.

        ANY failure is swallowed: a broken image must cost the image, never the
        frame the user is looking at.
        """

        buffer = bytearray()
        for placement, x, y in self._placements:
            with contextlib.suppress(Exception):
                buffer += placement.emit_at(x, y)
        if not buffer:
            return
        with contextlib.suppress(Exception):
            payload = _wrap_for_tmux(bytes(buffer))
            if self._write is None:
                _emit(payload)
            else:
                self._write(payload)
