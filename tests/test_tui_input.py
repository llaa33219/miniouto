"""Parser tests for the PyRatatui TUI input layer.

Every terminal form the app relies on is locked here as a byte string →
expected-event mapping, because the parser is the one place where a wrong
constant silently eats a keystroke. Two rules run through the whole file:

1. The parser is PURE, so a test never needs a tty — it feeds bytes and reads
   events.
2. A sequence may arrive in ANY number of pieces: a 4096-byte ``os.read`` can
   land in the middle of a CSI sequence, a UTF-8 character or a paste payload.
   Every family therefore has a split-across-feeds case.
"""

from __future__ import annotations

import asyncio
import os
import struct
from typing import Any

import pytest

from miniouto.cli.tui import input as input_module
from miniouto.cli.tui.input import (
    DEFAULT_SIZE,
    MOUSE_PASTE_OFF,
    MOUSE_PASTE_ON,
    InputParser,
    InputReader,
    Key,
    Mouse,
    Paste,
    key_from_xnano,
    mouse_from_xnano,
    scroll_from_xnano,
    terminal_size,
)


def feed(parser: InputParser, data: bytes | str) -> list[Any]:
    """Feed a byte/str payload and return the events it produced."""

    payload = data.encode() if isinstance(data, str) else data
    return parser.feed(payload)


def parse(data: bytes | str) -> list[Any]:
    """Parse one complete payload with a fresh parser."""

    return feed(InputParser(), data)


# ─── printable input ─────────────────────────────────────────────────────────


def test_printable_characters_arrive_as_their_own_code() -> None:
    assert parse(b"a") == [Key("a")]
    assert parse(b"Z") == [Key("Z", shift=True)]


def test_cjk_and_emoji_are_single_key_events() -> None:
    # Multi-byte UTF-8 must decode to ONE Key whose code is the character.
    assert parse("한".encode()) == [Key("한")]
    assert parse("日本語".encode()) == [Key("日"), Key("本"), Key("語")]
    assert parse("👍".encode()) == [Key("👍")]


def test_a_utf8_character_split_across_two_feeds_is_reassembled() -> None:
    parser = InputParser()
    encoded = "한".encode()
    assert parser.feed(encoded[:2]) == []
    assert parser.feed(encoded[2:]) == [Key("한")]


# ─── named keys ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("payload", [b"\r", b"\n"])
def test_enter_arrives_as_cr_or_lf(payload: bytes) -> None:
    assert parse(payload) == [Key("Enter")]


def test_tab_and_backtab() -> None:
    assert parse(b"\t") == [Key("Tab")]
    assert parse(b"\x1b[Z") == [Key("BackTab", shift=True)]


def test_backspace_is_del_but_ctrl_h_stays_ctrl_h() -> None:
    # Real terminals send 0x7f for Backspace; 0x08 is Ctrl+H and must not be
    # silently rewritten, so the app can decide what it means.
    assert parse(b"\x7f") == [Key("Backspace")]
    assert parse(b"\x08") == [Key("h", ctrl=True)]


def test_escape_alone_flushes_after_the_timeout() -> None:
    parser = InputParser()
    assert parser.flush_timeout() == []  # nothing buffered: nothing to flush
    assert parser.feed(b"\x1b") == []  # held: could still become a sequence
    assert parser.flush_timeout() == [Key("Esc")]
    assert parser.flush_timeout() == []  # exactly once


def test_a_sequence_prefix_is_not_flushed_as_escape() -> None:
    # ESC[ might be the head of a CSI sequence still in flight.
    parser = InputParser()
    assert parser.feed(b"\x1b[") == []
    assert parser.flush_timeout() == []
    assert parser.feed(b"A") == [Key("Up")]


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"\x1b[A", "Up"),
        (b"\x1b[B", "Down"),
        (b"\x1b[C", "Right"),
        (b"\x1b[D", "Left"),
        (b"\x1bOA", "Up"),
        (b"\x1bOB", "Down"),
        (b"\x1bOC", "Right"),
        (b"\x1bOD", "Left"),
    ],
)
def test_arrow_keys_in_csi_and_ss3_forms(payload: bytes, code: str) -> None:
    assert parse(payload) == [Key(code)]


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"\x1b[H", "Home"),
        (b"\x1b[F", "End"),
        (b"\x1b[1~", "Home"),
        (b"\x1b[4~", "End"),
        (b"\x1bOH", "Home"),
        (b"\x1bOF", "End"),
        (b"\x1b[5~", "PageUp"),
        (b"\x1b[6~", "PageDown"),
        (b"\x1b[2~", "Insert"),
        (b"\x1b[3~", "Delete"),
    ],
)
def test_navigation_and_editing_keys(payload: bytes, code: str) -> None:
    assert parse(payload) == [Key(code)]


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"\x1bOP", "F1"),
        (b"\x1bOQ", "F2"),
        (b"\x1bOR", "F3"),
        (b"\x1bOS", "F4"),
        (b"\x1b[11~", "F1"),
        (b"\x1b[12~", "F2"),
        (b"\x1b[13~", "F3"),
        (b"\x1b[14~", "F4"),
        (b"\x1b[15~", "F5"),
        (b"\x1b[17~", "F6"),
        (b"\x1b[18~", "F7"),
        (b"\x1b[19~", "F8"),
        (b"\x1b[20~", "F9"),
        (b"\x1b[21~", "F10"),
        (b"\x1b[23~", "F11"),
        (b"\x1b[24~", "F12"),
    ],
)
def test_function_keys_in_ss3_and_csi_forms(payload: bytes, code: str) -> None:
    assert parse(payload) == [Key(code)]


# ─── modifiers ───────────────────────────────────────────────────────────────


def test_ctrl_letters_map_onto_the_lowercase_letter() -> None:
    assert parse(b"\x01") == [Key("a", ctrl=True)]
    assert parse(b"\x03") == [Key("c", ctrl=True)]
    assert parse(b"\x1a") == [Key("z", ctrl=True)]


def test_escape_prefixed_printable_is_alt() -> None:
    assert parse(b"\x1ba") == [Key("a", alt=True)]
    assert parse(b"\x1bA") == [Key("A", alt=True, shift=True)]


def test_alt_ctrl_letter_composes() -> None:
    assert parse(b"\x1b\x01") == [Key("a", ctrl=True, alt=True)]


def test_two_escapes_are_two_escape_events() -> None:
    # The first is committed at once; the second stays buffered (it could still
    # begin a sequence) and the idle flush commits it — two Esc events total.
    parser = InputParser()
    assert parser.feed(b"\x1b\x1b") == [Key("Esc")]
    assert parser.flush_timeout() == [Key("Esc")]


def test_escape_escape_csi_still_parses_the_trailing_sequence() -> None:
    # Committing one Esc must not swallow the rest: "ESC ESC [ A" = Esc + Up.
    assert parse(b"\x1b\x1b[A") == [Key("Esc"), Key("Up")]


@pytest.mark.parametrize(
    ("modifier", "ctrl", "alt", "shift"),
    [
        (2, False, False, True),
        (3, False, True, False),
        (5, True, False, False),
        (6, True, False, True),
        (7, True, True, False),
        (8, True, True, True),
    ],
)
def test_csi_modifier_parameter_decodes_to_a_bitmask(
    modifier: int, ctrl: bool, alt: bool, shift: bool
) -> None:
    payload = f"\x1b[1;{modifier}C".encode()
    assert parse(payload) == [Key("Right", ctrl=ctrl, alt=alt, shift=shift)]


def test_csi_modifier_survives_a_split_feed() -> None:
    # A read can land anywhere, including between the params and the final.
    parser = InputParser()
    assert parser.feed(b"\x1b") == []
    assert parser.feed(b"[1;5") == []
    assert parser.feed(b"C") == [Key("Right", ctrl=True)]


def test_modifier_on_a_tilde_key() -> None:
    assert parse(b"\x1b[3;5~") == [Key("Delete", ctrl=True)]


# ─── mouse ───────────────────────────────────────────────────────────────────


def test_sgr_press_converts_one_based_coordinates_to_zero_based() -> None:
    assert parse(b"\x1b[<0;10;5M") == [Mouse("down", 9, 4, button=0)]


@pytest.mark.parametrize(
    ("code", "button"), [(0, 0), (1, 1), (2, 2)]
)
def test_sgr_button_codes(code: int, button: int) -> None:
    events = parse(f"\x1b[<{code};3;3M".encode())
    assert events == [Mouse("down", 2, 2, button=button)]


def test_sgr_release_drag_and_motion() -> None:
    assert parse(b"\x1b[<0;10;5m") == [Mouse("up", 9, 4, button=0)]
    assert parse(b"\x1b[<32;10;5M") == [Mouse("drag", 9, 4, button=0)]
    # b&3 == 3 with the motion bit: no button is held, so it is a hover move.
    assert parse(b"\x1b[<35;10;5M") == [Mouse("move", 9, 4, button=0)]


def test_sgr_wheel_directions() -> None:
    assert parse(b"\x1b[<64;3;4M") == [Mouse("wheel_up", 2, 3)]
    assert parse(b"\x1b[<65;3;4M") == [Mouse("wheel_down", 2, 3)]


def test_sgr_mouse_split_across_feeds() -> None:
    parser = InputParser()
    assert parser.feed(b"\x1b[<0;1") == []
    assert parser.feed(b"0;5M") == [Mouse("down", 9, 4, button=0)]


def test_legacy_x10_mouse_is_accepted_as_a_fallback() -> None:
    # ESC [ M + (b+32), (x+33), (y+33) — 1-based, offset by the 32 encoding.
    assert parse(b"\x1b[M\x20\x21\x21") == [Mouse("down", 0, 0, button=0)]
    assert parse(b"\x1b[M\x20\x31\x35") == [Mouse("down", 16, 20, button=0)]
    # X10 encodes a release as button 3.
    assert parse(b"\x1b[M\x23\x21\x21") == [Mouse("up", 0, 0, button=0)]


def test_out_of_bounds_coordinates_are_clamped_not_negative() -> None:
    # A 1-based (0,0) would otherwise go negative and index the wrong row.
    assert parse(b"\x1b[<0;1;1M") == [Mouse("down", 0, 0, button=0)]
    assert parse(b"\x1b[M\x20\x20\x20") == [Mouse("down", 0, 0, button=0)]


# ─── bracketed paste ─────────────────────────────────────────────────────────


def test_bracketed_paste_is_one_event_with_the_decoded_text() -> None:
    assert parse(b"\x1b[200~hello\nworld\x1b[201~") == [Paste("hello\nworld")]


def test_paste_keeps_unicode_intact() -> None:
    assert parse("\x1b[200~한글 👍\x1b[201~".encode()) == [Paste("한글 👍")]


def test_paste_split_across_feeds_and_across_the_end_marker() -> None:
    parser = InputParser()
    assert parser.feed(b"\x1b[200~ab") == []
    assert parser.feed(b"cd") == []
    assert parser.feed(b"\x1b[20") == []  # half of the end marker
    assert parser.feed(b"1~") == [Paste("abcd")]


def test_text_after_a_paste_parses_normally() -> None:
    assert parse(b"\x1b[200~x\x1b[201~a") == [Paste("x"), Key("a")]


def test_paste_does_not_leak_into_keys() -> None:
    # ESC inside a paste is TEXT, not the start of a sequence.
    assert parse(b"\x1b[200~\x1b[A\x1b[201~") == [Paste("\x1b[A")]


# ─── robustness ──────────────────────────────────────────────────────────────


def test_unrecognised_sequences_are_swallowed_without_stalling() -> None:
    # Device attributes / focus reports / cursor position queries.
    assert parse(b"\x1b[?1;2c") == []
    assert parse(b"\x1b[?1004h") == []
    assert parse(b"\x1b[12;40R") == []
    # …and the stream keeps working afterwards.
    assert parse(b"\x1b[?1;2ca") == [Key("a")]


def test_unnamed_control_bytes_do_not_block_the_stream() -> None:
    assert parse(b"\x00a") == [Key("a")]


def test_empty_feed_returns_nothing() -> None:
    assert parse(b"") == []


def test_reset_clears_a_half_parsed_sequence() -> None:
    parser = InputParser()
    assert parser.feed(b"\x1b[1;5") == []
    parser.reset()
    assert parser.feed(b"A") == [Key("A", shift=True)]


# ─── terminal size ───────────────────────────────────────────────────────────


def test_terminal_size_reads_tiocgwinsz(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_ioctl(fd: int, request: int, arg: bytes) -> bytes:
        return struct.pack("HHHH", 40, 120, 0, 0)  # rows, cols, xpix, ypix

    monkeypatch.setattr(input_module.fcntl, "ioctl", fake_ioctl)
    assert terminal_size() == (120, 40)


def test_terminal_size_falls_back_when_ioctl_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(fd: int, request: int, arg: bytes) -> bytes:
        raise OSError("not a tty")

    monkeypatch.setattr(input_module.fcntl, "ioctl", boom)
    assert terminal_size() == DEFAULT_SIZE


def test_terminal_size_treats_a_zero_window_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # A brand-new pty reports 0x0 until the first winsize arrives.
    monkeypatch.setattr(
        input_module.fcntl, "ioctl", lambda fd, request, arg: struct.pack("HHHH", 0, 0, 0, 0)
    )
    assert terminal_size() == DEFAULT_SIZE


# ─── reader thread ───────────────────────────────────────────────────────────


def test_reader_delivers_events_on_the_loop_thread() -> None:
    read_fd, write_fd = os.pipe()
    out_read, out_write = os.pipe()
    received: list[Any] = []

    async def body() -> None:
        loop = asyncio.get_running_loop()
        reader = InputReader(received.append, loop, fd=read_fd, out_fd=out_write)
        reader.start()
        try:
            os.write(write_fd, b"a\x1b[A")
            for _ in range(100):
                if len(received) >= 2:
                    break
                await asyncio.sleep(0.01)
        finally:
            reader.stop()

    try:
        asyncio.run(body())
        assert received == [Key("a"), Key("Up")]
    finally:
        for fd in (read_fd, write_fd, out_read, out_write):
            os.close(fd)


def test_reader_stop_is_prompt_and_restores_blocking_mode() -> None:
    read_fd, write_fd = os.pipe()
    out_read, out_write = os.pipe()

    async def body() -> None:
        loop = asyncio.get_running_loop()
        reader = InputReader(lambda event: None, loop, fd=read_fd, out_fd=out_write)
        reader.start()
        await asyncio.sleep(0.05)  # parked in select() with nothing to read
        started = loop.time()
        reader.stop()
        assert loop.time() - started < 1.0, "stop() must not wait for a keystroke"
        # The descriptor's blocking mode belongs to whoever had it before us.
        assert os.get_blocking(read_fd) is True

    try:
        asyncio.run(body())
    finally:
        for fd in (read_fd, write_fd, out_read, out_write):
            os.close(fd)


def test_reader_enable_and_disable_write_the_terminal_modes() -> None:
    read_fd, write_fd = os.pipe()
    out_read, out_write = os.pipe()

    async def body() -> None:
        reader = InputReader(lambda event: None, asyncio.get_running_loop(), fd=read_fd, out_fd=out_write)
        reader.enable()
        reader.disable()

    try:
        asyncio.run(body())
        os.set_blocking(out_read, False)
        written = os.read(out_read, 4096)
        assert written == (MOUSE_PASTE_ON + MOUSE_PASTE_OFF).encode()
        # SGR coordinates + any-motion + bracketed paste, and their resets.
        for mode in ("1000", "1002", "1003", "1006", "2004"):
            assert f"\x1b[?{mode}h" in MOUSE_PASTE_ON
            assert f"\x1b[?{mode}l" in MOUSE_PASTE_OFF
    finally:
        for fd in (read_fd, write_fd, out_read, out_write):
            os.close(fd)


def test_mouse_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError):
        Mouse("wiggle", 0, 0)


# ─── the xnano bridge (the real terminal's front end) ───────────────────────
#
# Every mapping below was read off xnano 2.3.0 on a pty, not inferred from the
# docs: the readings are the ones a real keyboard/mouse produced.


def test_a_printable_key_keeps_its_character() -> None:
    assert key_from_xnano("h", "press", frozenset(), "h") == Key("h")
    assert key_from_xnano("9", "press", frozenset(), "9") == Key("9")


def test_a_shifted_letter_carries_both_the_shift_flag_and_its_case() -> None:
    # xnano reports key='h' with shift, and text='H' — the app's editor inserts
    # the code, so the CODE is what must be capitalised.
    assert key_from_xnano("h", "press", frozenset({"shift"}), "H") == Key(
        "h".upper(), shift=True
    )


def test_a_control_chord_is_lowercase_because_that_is_the_byte() -> None:
    # `handle_key` compares a ctrl chord against "c"/"p"/"l"; Ctrl+Shift+P must
    # still reach the palette branch.
    assert key_from_xnano("p", "press", frozenset({"control"}), None) == Key("p", ctrl=True)
    assert key_from_xnano("p", "press", frozenset({"control", "shift"}), None) == Key(
        "p", ctrl=True, shift=True
    )


@pytest.mark.parametrize(
    ("xnano_name", "code"),
    [
        ("enter", "Enter"),
        ("escape", "Esc"),
        ("backspace", "Backspace"),
        ("tab", "Tab"),
        ("delete", "Delete"),
        ("insert", "Insert"),
        ("up", "Up"),
        ("down", "Down"),
        ("left", "Left"),
        ("right", "Right"),
        ("home", "Home"),
        ("end", "End"),
        ("page_up", "PageUp"),
        ("page_down", "PageDown"),
        ("f1", "F1"),
        ("f12", "F12"),
    ],
)
def test_every_named_key_maps_to_our_pascal_case_code(xnano_name: str, code: str) -> None:
    assert key_from_xnano(xnano_name, "press", frozenset(), None) == Key(code)


def test_the_space_bar_is_a_space_character_not_a_name() -> None:
    # xnano calls it 'space' but reports ' ' as its text; the editor inserts the
    # CODE, so a Key('space') would type the word.
    assert key_from_xnano("space", "press", frozenset(), " ") == Key(" ")


def test_shift_tab_becomes_backtab_which_is_the_code_the_router_tests() -> None:
    # xnano has NO backtab KeyName: shift+tab is key='tab' plus the shift
    # modifier. `handle_key` tests code == "BackTab" for the reverse cycle, so
    # the pair has to be folded here or Shift+Tab would cycle forward.
    assert key_from_xnano("tab", "press", frozenset({"shift"}), None) == Key(
        "BackTab", shift=True
    )
    # A plain tab is still a tab, modifier free.
    assert key_from_xnano("tab", "press", frozenset(), None) == Key("Tab")


def test_a_key_release_is_dropped() -> None:
    # The canvas asks for press/repeat only; a release must not retype a key.
    assert key_from_xnano("a", "release", frozenset(), "a") is None


def test_a_repeat_is_forwarded_so_held_keys_keep_typing() -> None:
    assert key_from_xnano("a", "repeat", frozenset(), "a") == Key("a")


def test_alt_is_carried_and_lowercases_like_control() -> None:
    assert key_from_xnano("x", "press", frozenset({"alt"}), None) == Key("x", alt=True)


def test_a_multibyte_character_survives_the_bridge() -> None:
    # A CJK glyph arrives with its own name and text; it must reach the editor
    # as ONE key whose code is the character.
    assert key_from_xnano("한", "press", frozenset(), "한") == Key("한")


def test_a_pointer_event_maps_kind_cell_and_button() -> None:
    assert mouse_from_xnano("press", 29, 11, "left") == Mouse("down", 29, 11, button=0)
    assert mouse_from_xnano("release", 29, 11, "left") == Mouse("up", 29, 11, button=0)
    assert mouse_from_xnano("drag", 7, 7, "left") == Mouse("drag", 7, 7, button=0)
    assert mouse_from_xnano("move", 39, 13, None) == Mouse("move", 39, 13, button=0)
    assert mouse_from_xnano("press", 1, 2, "right") == Mouse("down", 1, 2, button=2)
    assert mouse_from_xnano("press", 1, 2, "middle") == Mouse("down", 1, 2, button=1)


def test_a_wheel_report_is_not_a_button_event() -> None:
    assert mouse_from_xnano("scroll", 4, 5, None) is None


def test_the_wheel_sign_is_positive_down_so_negative_scrolls_up() -> None:
    # xnano's own contract: "positive downward". One `turn(y=-1)` reaches the
    # hook as -1 and must scroll the log UP.
    assert scroll_from_xnano(-1, 20, 9) == Mouse("wheel_up", 20, 9)
    assert scroll_from_xnano(1, 20, 9) == Mouse("wheel_down", 20, 9)
    assert scroll_from_xnano(2, 20, 9) == Mouse("wheel_down", 20, 9)


def test_a_zero_wheel_step_is_not_a_detent() -> None:
    assert scroll_from_xnano(0, 0, 0) is None


def test_mouse_coordinates_are_clamped_non_negative() -> None:
    # A terminal can report a cell outside the window during a resize; the
    # handlers index with these, so a negative would be nonsense.
    assert mouse_from_xnano("press", -3, -1, "left") == Mouse("down", 0, 0)
    assert scroll_from_xnano(-1, -2, -4) == Mouse("wheel_up", 0, 0)
