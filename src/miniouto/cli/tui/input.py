"""Raw-terminal input layer for the TUI — the event vocabulary, and the xnano bridge.

WHY this module exists at all: pyratatui's ``Terminal.poll_event`` returns key
events only — crossterm swallows mouse, paste and resize events *before* Python
ever sees them. The PyRatatui port therefore owned stdin itself: :class:`InputParser`
turned the raw byte stream into the events the app needs, and
:class:`InputReader` pumped those bytes off the tty on a daemon thread into the
asyncio loop.

That reader is RETIRED on the real terminal. The substrate is xnano now, and
xnano OWNS stdin (it reads the keyboard on a thread of its own as soon as a
canvas is opened), so a second reader on fd 0 would never see a byte. The
events the app needs still exist — the handlers, the modal layer and the
headless ``Pilot`` all speak :class:`Key` / :class:`Mouse` / :class:`Paste` /
:class:`Resize` — so what changed is the FRONT END: :func:`key_from_xnano` and
:func:`mouse_from_xnano` translate xnano's ``KeyEvent`` / ``MouseEvent``
readings into these dataclasses, and the app registers one hook per kind
(``on='key'``, ``'mouse'``, ``'scroll'``, ``'paste'``, ``'resize'``).

The translations are pure functions over duck-typed readings, so they stay
unit-testable without a terminal, and the mapping itself is the specification
in ``.miniouto/plans/xnano-input-mapping.md`` — every rule below was confirmed
against xnano 2.3.0 on a pty, including the two that are easy to get wrong:
``shift+tab`` arrives as ``key='tab'`` + the ``shift`` modifier (there is no
``backtab`` KeyName), and the wheel reports ``scroll.y`` positive DOWNWARD, so
``y < 0`` is ``wheel_up``.

:class:`InputParser` and :class:`InputReader` stay in the module, unused by the
real terminal, because the parser is still the decoder the tests pin for the
BYTES a terminal sends and :class:`InputReader` is still what the headless
loop tests double. Nothing in the app constructs either of them any more.
"""

from __future__ import annotations

import codecs
import contextlib
import os
import select
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

try:  # POSIX only; the parser itself must import on any platform.
    import fcntl
    import termios
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]

STDOUT_FILENO = 1
"""The fd the terminal modes are written to (pyratatui draws on stdout)."""

DEFAULT_SIZE = (80, 24)
"""Fallback ``(width, height)`` when the terminal cannot report its size."""

MAX_PASTE_CHARS = 1 << 20
"""Bracketed-paste ceiling (1 Mi characters).

A paste bomb must not grow the buffer without bound: one stray ``ESC[201~``
would otherwise turn every later keystroke into paste text.
"""

MOUSE_PASTE_ON = "\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?2004h"
"""Enable press, drag, any-motion, SGR coordinates and bracketed paste.

The real terminal no longer writes these: xnano turns mouse/paste reporting on
itself when a canvas is opened with ``mouse``/``scroll``/``paste``. The
constants stay because :class:`InputReader` (the headless-loop double) still
emits them and the tests pin their exact bytes.
"""

MOUSE_PASTE_OFF = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2004l"
"""The matching reset sequence — must run on every exit path."""


_READ_CHUNK = 4096
_READ_TIMEOUT = 0.1
"""Seconds per ``select`` wait; bounds how long ``stop()`` can block."""

_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"
_CTRL_LOWER_OFFSET = 0x60
_CTRL_MAX = 0x1A

MOUSE_KINDS = ("down", "up", "drag", "move", "wheel_up", "wheel_down")
"""Every value :attr:`Mouse.kind` may take."""


@dataclass(frozen=True)
class Key:
    """One key press.

    ``code`` is PascalCase for named keys (``"Enter"``, ``"Esc"``,
    ``"Backspace"``, ``"F5"``, ``"PageUp"`` …) and the literal character for
    printable input, so ``Key.code`` can be handed straight to pyratatui's
    duck-typed ``TextState.handle_key``.
    """

    code: str
    ctrl: bool = False
    alt: bool = False
    shift: bool = False


@dataclass(frozen=True)
class Mouse:
    """One mouse report, already converted to 0-based cell coordinates."""

    kind: str
    x: int
    y: int
    button: int = 0
    """``0`` left, ``1`` middle, ``2`` right (``0`` when there is no button)."""

    def __post_init__(self) -> None:
        if self.kind not in MOUSE_KINDS:
            raise ValueError(f"unknown mouse kind: {self.kind!r}")


@dataclass(frozen=True)
class Paste:
    """A bracketed-paste payload, delivered as ONE event."""

    text: str


@dataclass(frozen=True)
class Resize:
    """A size change.

    Not produced by this parser — there is no stdin event for it — but part of
    the event vocabulary so the app can queue it from its ``TIOCGWINSZ`` poll.
    """

    width: int
    height: int


Event = Key | Mouse | Paste | Resize


# ─── the xnano bridge (the real terminal's front end) ───────────────────────

_XNANO_KEY_CODES: dict[str, str] = {
    "enter": "Enter",
    "escape": "Esc",
    "backspace": "Backspace",
    "tab": "Tab",
    "delete": "Delete",
    "insert": "Insert",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "page_up": "PageUp",
    "page_down": "PageDown",
    **{f"f{index}": f"F{index}" for index in range(1, 13)},
}
"""xnano ``KeyName`` → our PascalCase code, for every NAMED key.

A key that is not here is printable: xnano reports ``'a'``..``'z'``, ``'0'``..
``'9'``, and — as a probe on a pty showed — the literal character for anything
else the keyboard produces (``','``, ``'~'``, ``'한'``), plus ``'space'`` for
the space bar. ``space`` is the one name that carries a character rather than a
name, so it maps to the character itself.
"""

_XNANO_BUTTONS = {"left": 0, "middle": 1, "right": 2}
"""xnano button name → the SGR button number our handlers compare."""

_XNANO_MOUSE_KINDS = {"press": "down", "release": "up", "drag": "drag", "move": "move"}
"""xnano ``MouseEventKind`` → our :data:`MOUSE_KINDS`."""


def key_from_xnano(key: object, kind: object, modifiers: object, text: object) -> Key | None:
    """One xnano key reading as our :class:`Key`, or ``None`` for a release.

    ``key`` is xnano's ``KeyName``, ``modifiers`` its frozenset of
    ``'control'``/``'shift'``/``'alt'``/... names, ``text`` the character it
    types (``None`` for control keys). Releases are dropped: our app is a
    press/repeat consumer, and xnano only reports releases when the canvas asks
    for them.

    Two details were read off a pty rather than assumed:

    * **Shift+Tab is not BackTab.** xnano has no ``backtab`` KeyName — it
      reports ``key='tab'`` with the ``shift`` modifier — while
      :meth:`ChatTUI.handle_key` tests ``code == "BackTab"`` for the reverse
      cycle. So the pair is folded into our own code here.
    * **Letters carry their case in ``text``.** ``shift+h`` reads as
      ``key='h'``, ``modifiers={'shift'}``, ``text='H'``; a Ctrl chord reads as
      the lowercase letter because that is the byte the terminal sends. Our
      router compares a ctrl chord against ``"c"``/``"p"``/``"l"``, so the code
      stays LOWERCASE whenever ctrl or alt is held.
    """

    if str(kind) == "release":
        return None
    name = str(key or "")
    if not name:
        return None
    flags = frozenset(str(item) for item in (modifiers or ()))
    ctrl = "control" in flags
    alt = "alt" in flags
    shift = "shift" in flags
    if name == "space":
        name = " "
    if name == "tab" and shift:
        return Key("BackTab", ctrl=ctrl, alt=alt, shift=True)
    code = _XNANO_KEY_CODES.get(name)
    if code is not None:
        return Key(code, ctrl=ctrl, alt=alt, shift=shift)
    char = str(text) if text else name
    if ctrl or alt:
        # A chord is spelled the way its byte arrives: Ctrl+Shift+P is "p".
        char = char.lower()
    return Key(char, ctrl=ctrl, alt=alt, shift=shift)


def mouse_from_xnano(
    kind: object, column: object, row: object, button: object
) -> Mouse | None:
    """One xnano pointer reading as our :class:`Mouse`, or ``None`` for the wheel.

    Wheel reports arrive on their own hook (``on='scroll'``) because xnano
    routes them there, so :func:`scroll_from_xnano` owns them and this one
    answers ``None`` rather than inventing a button.
    """

    name = _XNANO_MOUSE_KINDS.get(str(kind))
    if name is None:
        return None
    return Mouse(
        name,
        max(0, int(column)),
        max(0, int(row)),
        button=_XNANO_BUTTONS.get(str(button), 0),
    )


def scroll_from_xnano(steps: object, x: object = 0, y: object = 0) -> Mouse | None:
    """One wheel report as a ``wheel_up``/``wheel_down`` :class:`Mouse`.

    ``steps`` is ``scroll.y``: xnano's own docstring says positive scrolls DOWN,
    which a pty probe confirmed (one ``turn(y=-1)`` reaches the hook as ``-1``
    and moves the log up). A zero step is not a detent and answers ``None``.
    """

    amount = int(steps or 0)
    if amount == 0:
        return None
    return Mouse(
        "wheel_up" if amount < 0 else "wheel_down",
        max(0, int(x)),
        max(0, int(y)),
    )


def terminal_size() -> tuple[int, int]:
    """Return the terminal ``(width, height)`` in cells.

    Resize has no escape sequence of its own, so the app polls this each tick
    and synthesises a :class:`Resize` when the numbers move. Any failure —
    redirected stdout, a pipe, a 0x0 window in a brand-new pty — falls back to
    :data:`DEFAULT_SIZE` rather than raising inside the render loop.
    """

    if fcntl is None or termios is None:  # pragma: no cover - Windows
        return DEFAULT_SIZE
    try:
        packed = fcntl.ioctl(
            STDOUT_FILENO, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0)
        )
        rows, cols = struct.unpack("HHHH", packed)[:2]
    except (OSError, ValueError, struct.error):
        return DEFAULT_SIZE
    if rows <= 0 or cols <= 0:
        # A fresh pty reports 0x0 until the first resize winsize arrives.
        return DEFAULT_SIZE
    return (cols, rows)


_TILDE_KEYS = {
    1: "Home",
    2: "Insert",
    3: "Delete",
    4: "End",
    5: "PageUp",
    6: "PageDown",
    7: "Home",
    8: "End",
    11: "F1",
    12: "F2",
    13: "F3",
    14: "F4",
    15: "F5",
    17: "F6",
    18: "F7",
    19: "F8",
    20: "F9",
    21: "F10",
    23: "F11",
    24: "F12",
}

_CSI_LETTER_KEYS = {
    "A": "Up",
    "B": "Down",
    "C": "Right",
    "D": "Left",
    "H": "Home",
    "F": "End",
}

_SS3_KEYS = {
    "A": "Up",
    "B": "Down",
    "C": "Right",
    "D": "Left",
    "H": "Home",
    "F": "End",
    "P": "F1",
    "Q": "F2",
    "R": "F3",
    "S": "F4",
}


class InputParser:
    """Incremental byte-stream → :data:`Event` decoder.

    Sequences and multi-byte characters routinely straddle two ``feed`` calls
    (a 4096-byte read can land mid-CSI), so the parser keeps the undecoded tail
    in its buffer and resumes from there. ``feed`` never blocks and never reads
    a descriptor.
    """

    def __init__(self) -> None:
        # 'replace' rather than 'strict': one mangled byte must not kill the
        # whole TUI session. A partial UTF-8 tail is buffered by the incremental
        # decoder until the rest of the character arrives.
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buf = ""
        self._paste: list[str] | None = None
        self._paste_len = 0

    # ── public surface ────────────────────────────────────────────────────
    def feed(self, data: bytes) -> list[Event]:
        """Decode ``data`` and return every event it completes."""

        if not data:
            return []
        self._buf += self._decoder.decode(data)
        return self._drain()

    def flush_timeout(self) -> list[Event]:
        """Resolve a lone ESC once the keyboard has been quiet for ~25 ms.

        A bare ESC and the first byte of a CSI/SS3 sequence are the same byte,
        so the parser must wait; the loop calls this after its idle timeout to
        decide nothing else is coming and commit the escape. Anything longer
        than a single pending ESC is a real sequence in flight and stays put.
        """

        if self._buf == "\x1b":
            self._buf = ""
            return [Key("Esc")]
        return []

    def reset(self) -> None:
        """Drop buffered input (used when the reader stops mid-sequence)."""

        self._decoder.reset()
        self._buf = ""
        self._paste = None
        self._paste_len = 0

    # ── buffer draining ───────────────────────────────────────────────────
    def _drain(self) -> list[Event]:
        events: list[Event] = []
        while self._buf:
            if self._paste is not None:
                if not self._drain_paste(events):
                    break
                continue
            parsed = self._parse_one()
            if parsed is None:  # sequence still incomplete
                break
            chunk, consumed = parsed
            events.extend(chunk)
            if consumed <= 0:  # pragma: no cover - defensive against a spin
                break
            self._buf = self._buf[consumed:]
        return events

    def _parse_one(self) -> tuple[list[Event], int] | None:
        """Parse one event off the head of the buffer.

        ``None`` means "needs more bytes" — the caller keeps the buffer.
        """

        char = self._buf[0]
        if char == "\x1b":
            return self._parse_escape()
        single = _single_char_event(char)
        if single is None:
            # An unnamed control byte (NUL, Ctrl+K …): swallow it so a stray
            # byte can never stall the stream forever.
            return [], 1
        return [single], 1

    def _parse_escape(self) -> tuple[list[Event], int] | None:
        buf = self._buf
        if len(buf) < 2:
            return None  # could be a lone Esc — flush_timeout() decides
        nxt = buf[1]
        if nxt == "\x1b":
            # Two Escapes. Commit only the FIRST and leave the second buffered:
            # it may still be the head of a real sequence ("ESC ESC [ A" is
            # Esc then Up — a user who taps Esc and then an arrow key must not
            # get junk). A second Esc that really is alone is committed by
            # flush_timeout(), so a double tap still arrives as two Esc events.
            return [Key("Esc")], 1
        if nxt == "[":
            return self._parse_csi()
        if nxt == "O":
            return self._parse_ss3()
        single = _single_char_event(nxt)
        if single is None:
            return [], 2
        return [replace(single, alt=True)], 2

    def _parse_ss3(self) -> tuple[list[Event], int] | None:
        buf = self._buf
        if len(buf) < 3:
            return None
        name = _SS3_KEYS.get(buf[2])
        if name is None:
            return [], 3  # unrecognised SS3 (keypad, mouse-free) — swallow it
        return [Key(name)], 3

    def _parse_csi(self) -> tuple[list[Event], int] | None:
        buf = self._buf
        if len(buf) < 3:
            return None
        if buf[2] == "M":
            # Legacy X10 mouse: ESC [ M followed by exactly three raw bytes.
            if len(buf) < 6:
                return None
            button = ord(buf[3]) - 32
            x = ord(buf[4]) - 33  # 1-based, then offset by the 32 of the encoding
            y = ord(buf[5]) - 33
            return self._mouse_event(button, x, y, press=True), 6

        index = 2
        while index < len(buf) and _is_param_byte(buf[index]):
            index += 1
        while index < len(buf) and _is_intermediate_byte(buf[index]):
            index += 1
        if index >= len(buf):
            return None  # final byte still in flight
        if not _is_final_byte(buf[index]):
            return [], index + 1  # malformed: discard what we scanned

        final = buf[index]
        params = buf[2:index]
        consumed = index + 1

        if params.startswith("<"):
            return self._sgr_mouse(params[1:], final), consumed
        if "?" in params or ">" in params or "=" in params:
            return [], consumed  # mode report / device attributes: not ours

        try:
            numbers = [int(part) for part in params.split(";")] if params else []
        except ValueError:
            return [], consumed

        decoded = _decode_modifier(numbers)
        if decoded is None:
            # Not a modifier we know: a CSI cursor-position report
            # ("ESC[12;40R") or a private report that happens to end in a
            # letter we use. Swallow it rather than mint a nonsense key.
            return [], consumed
        shift, alt, ctrl = decoded

        if final == "~":
            code = numbers[0] if numbers else -1
            if code == 200:
                self._begin_paste()
                return [], consumed
            if code == 201:
                return [], consumed  # stray end marker; nothing to close
            name = _TILDE_KEYS.get(code)
            if name is None:
                return [], consumed
            return [Key(name, ctrl=ctrl, alt=alt, shift=shift)], consumed
        if final == "Z":
            # BackTab IS shift+Tab; carrying the flag keeps it unambiguous.
            return [Key("BackTab", shift=True)], consumed
        if final in ("P", "Q", "R", "S"):
            # F1-F4 also have CSI forms (ESC[11~ handled above via '~').
            return [Key(_SS3_KEYS[final], ctrl=ctrl, alt=alt, shift=shift)], consumed
        name = _CSI_LETTER_KEYS.get(final)
        if name is None:
            return [], consumed  # focus in/out, cursor position report, …
        return [Key(name, ctrl=ctrl, alt=alt, shift=shift)], consumed

    def _sgr_mouse(self, params: str, final: str) -> list[Event]:
        parts = params.split(";")
        if len(parts) != 3:
            return []
        try:
            button, x, y = (int(part) for part in parts)
        except ValueError:
            return []
        return self._mouse_event(button, x - 1, y - 1, press=final == "M")

    def _mouse_event(self, button_bits: int, x: int, y: int, press: bool) -> list[Event]:
        """Decode the shared (SGR and X10) button / coordinate bit fields."""

        x = max(0, x)
        y = max(0, y)
        if button_bits & 64:
            # Only the vertical wheel is mapped; the horizontal one (66/67)
            # degrades to up/down instead of being dropped on the floor.
            kind = "wheel_up" if (button_bits & 1) == 0 else "wheel_down"
            return [Mouse(kind, x, y)]
        button = button_bits & 3  # 0 left, 1 middle, 2 right, 3 "no button"
        if button_bits & 32:
            kind = "move" if button == 3 else "drag"
        elif press and button != 3:
            kind = "down"
        else:
            # 'm' releases carry the released button; X10 reports a release as
            # button 3, so both land here.
            kind = "up"
        return [Mouse(kind, x, y, button=0 if button == 3 else button)]

    # ── bracketed paste ───────────────────────────────────────────────────
    def _begin_paste(self) -> None:
        self._paste = []
        self._paste_len = 0

    def _drain_paste(self, events: list[Event]) -> bool:
        """Collect paste text; ``True`` once the end marker was consumed.

        The end marker can itself be split across reads, so up to
        ``len(marker) - 1`` trailing characters stay in the parse buffer until
        the next chunk proves whether they were the marker.
        """

        buf = self._buf
        end = buf.find(_PASTE_END)
        if end < 0:
            keep = len(_PASTE_END) - 1
            if len(buf) > keep:
                self._append_paste(buf[: len(buf) - keep])
                self._buf = buf[len(buf) - keep :]
            return False
        self._append_paste(buf[:end])
        self._buf = buf[end + len(_PASTE_END) :]
        text = "".join(self._paste or [])
        self._paste = None
        self._paste_len = 0
        events.append(Paste(text))
        return True

    def _append_paste(self, chunk: str) -> None:
        if self._paste is None or not chunk:
            return
        room = MAX_PASTE_CHARS - self._paste_len
        if room <= 0:
            return  # over the ceiling: drop the excess, keep scanning for the end
        piece = chunk[:room]
        self._paste.append(piece)
        self._paste_len += len(piece)


def _single_char_event(char: str) -> Key | None:
    """Map one character to a :class:`Key`, or ``None`` for unnamed controls."""

    if char in ("\r", "\n"):
        return Key("Enter")
    if char == "\t":
        return Key("Tab")
    if char == "\x7f":
        return Key("Backspace")
    code = ord(char)
    if 0x01 <= code <= _CTRL_MAX:
        # 0x08 is Ctrl+H on the wire (real terminals send ctrl+h here, NOT
        # Backspace — that is 0x7f); the app may treat either as a deletion.
        # 0x09 / 0x0d / 0x1b never reach this branch.
        return Key(chr(code + _CTRL_LOWER_OFFSET), ctrl=True)
    if code < 0x20:
        return None
    # Shift is not reported for printables: an uppercase letter is the only
    # reliable evidence the shift key was down.
    return Key(char, shift=char.isupper())


def _decode_modifier(numbers: list[int]) -> tuple[bool, bool, bool] | None:
    """Split the CSI ``1;<m>`` parameter into ``(shift, alt, ctrl)``.

    ``m - 1`` is a bitmask (1=shift, 2=alt, 4=ctrl); a lone parameter is a
    repeat count, not a modifier, so only two or more parameters carry one.
    ``m`` outside ``2..8`` is not a modifier at all — a cursor-position report
    such as ``ESC[12;40R`` lands in the same shape as ``F3`` and must not be
    read as "F3 with every modifier held".
    """

    if len(numbers) < 2:
        return (False, False, False)
    if not 2 <= numbers[-1] <= 8:
        return None
    mask = numbers[-1] - 1
    return (bool(mask & 1), bool(mask & 2), bool(mask & 4))


def _is_param_byte(char: str) -> bool:
    return "0" <= char <= "?"


def _is_intermediate_byte(char: str) -> bool:
    return " " <= char <= "/"


def _is_final_byte(char: str) -> bool:
    return "@" <= char <= "~"


class InputReader:
    """Daemon thread that feeds stdin bytes into the asyncio loop.

    The loop stays the single owner of app state: every event is handed over
    with ``loop.call_soon_threadsafe``, so handlers always run on the loop
    thread and never on this one.
    """

    def __init__(
        self,
        callback: Callable[[Event], Any],
        loop: Any,
        fd: int = 0,
        parser: InputParser | None = None,
        out_fd: int = STDOUT_FILENO,
    ) -> None:
        self._callback = callback
        self._loop = loop
        self._fd = fd
        self._out_fd = out_fd
        self._parser = parser if parser is not None else InputParser()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._was_blocking: bool | None = None

    @property
    def parser(self) -> InputParser:
        return self._parser

    def start(self) -> None:
        """Begin reading. Idempotent."""

        if self._thread is not None:
            return
        self._stop.clear()
        try:
            self._was_blocking = os.get_blocking(self._fd)
        except OSError:  # pragma: no cover - exotic fd
            self._was_blocking = None
        self._thread = threading.Thread(target=self._run, name="miniouto-input", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop reading and restore the descriptor's blocking mode.

        The thread is parked in ``select``/``os.read``; flipping the fd
        non-blocking makes a read that raced the stop return immediately
        instead of waiting for the next keystroke, so shutdown is prompt.
        ``select`` also carries a 0.1 s timeout as the belt to that braces.
        """

        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        with contextlib.suppress(OSError):  # fd already closed under us
            os.set_blocking(self._fd, False)
        thread.join(timeout=1.0)
        self._thread = None
        if self._was_blocking is not None:
            with contextlib.suppress(OSError):
                os.set_blocking(self._fd, self._was_blocking)

    def enable(self) -> None:
        """Turn mouse reporting and bracketed paste on."""

        self._write(MOUSE_PASTE_ON)

    def disable(self) -> None:
        """Turn them back off — call this on EVERY exit path."""

        self._write(MOUSE_PASTE_OFF)

    def _write(self, payload: str) -> None:
        # A dead terminal (SIGHUP, closed pty) has nothing to fix: the modes
        # being unset is the exit path's problem, not the write's.
        with contextlib.suppress(OSError):
            os.write(self._out_fd, payload.encode("ascii"))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], _READ_TIMEOUT)
            except (OSError, ValueError):  # fd closed under us
                return
            if not ready:
                continue
            try:
                data = os.read(self._fd, _READ_CHUNK)
            except BlockingIOError:
                continue
            except OSError:
                return
            if not data:  # EOF: the pty is gone
                return
            for event in self._parser.feed(data):
                self._deliver(event)

    def _deliver(self, event: Event) -> None:
        try:
            self._loop.call_soon_threadsafe(self._callback, event)
        except RuntimeError:  # loop already closed: drop the event
            return
