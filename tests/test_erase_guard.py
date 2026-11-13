"""The erase guard (pt._guard_erases / _unfinished_tail).

On a back-colour-erase terminal (Konsole) an erase paints the CURRENT SGR
background. xnano emits ESC[2J on a resize AFTER a frame that resets the pen, so
a separate hook's SGR loses the race and the clear flashes the terminal default.
The guard prefixes the theme-bg SGR to every erase IN THE SAME WRITE. These tests
pin the pure guarding logic (the fd-1 tap that carries it is exercised in a pty).
"""

from __future__ import annotations

from miniouto.cli.tui import pt

SGR = b"\x1b[48;2;10;14;20m"


def test_every_erase_is_prefixed_with_the_sgr() -> None:
    data = b"\x1b[2Jhello\x1b[2J"
    out = pt._guard_erases(data, SGR)
    assert out == SGR + b"\x1b[2Jhello" + SGR + b"\x1b[2J"


def test_all_erase_forms_are_guarded() -> None:
    for seq in (b"\x1b[2J", b"\x1b[3J", b"\x1b[J", b"\x1b[0J", b"\x1b[1J"):
        out = pt._guard_erases(b"x" + seq + b"y", SGR)
        assert out == b"x" + SGR + seq + b"y", seq


def test_no_erase_passes_through_unchanged() -> None:
    data = b"\x1b[38;2;1;2;3mhello world\x1b[0m"
    assert pt._guard_erases(data, SGR) == data


def test_empty_sgr_is_a_noop() -> None:
    data = b"\x1b[2J"
    assert pt._guard_erases(data, b"") == data


def test_unfinished_csi_tail_is_held_back() -> None:
    # A write that ends mid-CSI: the tail must be held so an erase split across
    # two writes is still guarded once whole.
    data = b"hello\x1b[48;2;10;14;"
    cut = pt._unfinished_tail(data)
    assert cut == len(b"hello")  # the incomplete CSI is held back
    assert data[:cut] == b"hello"


def test_complete_csi_is_not_held_back() -> None:
    data = b"hello\x1b[2J"
    assert pt._unfinished_tail(data) == len(data)


def test_lone_esc_is_held_back() -> None:
    data = b"hello\x1b"
    assert pt._unfinished_tail(data) == len(b"hello")


def test_split_erase_is_guarded_once_reassembled() -> None:
    # Simulate the tap's carry: first write ends mid-erase, second completes it.
    first = b"\x1b[2"
    cut = pt._unfinished_tail(first)
    carry = first[cut:]
    assert carry == b"\x1b[2"  # held back
    second = carry + b"Jrest"
    out = pt._guard_erases(second, SGR)
    assert out == SGR + b"\x1b[2Jrest"


def test_osc_tail_terminated_by_bel_is_complete() -> None:
    data = b"\x1b]0;title\x07"
    assert pt._unfinished_tail(data) == len(data)


def test_osc_tail_unterminated_is_held_back() -> None:
    data = b"\x1b]0;title"
    assert pt._unfinished_tail(data) == 0
