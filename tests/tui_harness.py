"""Helpers shared by the app-level TUI tests (and by the M9 test port).

The app layer is exercised through its REAL surfaces — ``run_test`` boots a
headless ``ChatTUI``, ``pilot`` presses keys and clicks, and assertions read the
composited screen or the node tree. That means the same three things are needed
over and over: a booted app with a pinned session of the test's own, a way to
read what the terminal would actually show, and a ``run_chat`` double.

Two rules for everything here:

* **Read the DISPLAY, not the model.** :func:`painted_line` goes through
  ``screen._compositor.render_strips()`` so it sees exactly what the terminal
  gets, including the crop at the screen edge — the layout bugs worth catching
  are the ones visible on screen.
* **Pin the session.** ``ChatTUI`` mints a ``tui-…`` session on its first turn
  and persists turn records, so a test that did not pin its own name would
  share provider config and inbox with every other test in the run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from miniouto.cli.tui import ChatTUI
from miniouto.cli.tui.modals import Modal
from miniouto.storage import session_config
from miniouto.storage import sessions as session_store
from miniouto.storage import settings as settings_store

BOOT_SIZE = (120, 32)
"""The pty-baseline screen size — the layout the owner approved."""


# ── booting ─────────────────────────────────────────────────────────────────


def pin_session(app: ChatTUI, name: str) -> str:
    """Stand ``app`` on a session of this test's own and publish it.

    Mirrors the production pin: routing reads ``_session_name``, and
    ``settings.session`` is the "active session" every surface (and a later
    ``miniouto status``) reads as the seed a mint copies from.
    """

    session_store.create(name)
    app._session_name = name
    app._session_assigned = True
    app._chat_started = True
    settings_store.update(session=name)
    session_config.clear(name)
    return name


# ── reading the screen ──────────────────────────────────────────────────────


def painted_line(app: ChatTUI, y: int) -> str:
    """The composited screen line ``y`` — what the terminal actually receives."""

    return app.screen._compositor.render_strips()[y].text


def painted(app: ChatTUI) -> list[str]:
    """Every composited screen line, right-stripped, top to bottom."""

    return [strip.text.rstrip() for strip in app.screen._compositor.render_strips()]


def composed(app: ChatTUI, width: int, height: int) -> list[str]:
    """Compose a frame and flatten it to plain text lines (no compositor needed)."""

    app.compose(width, height)
    return ["".join(piece[0] for piece in line).rstrip() for line in app.compose(width, height)]


def row_text(row: Any) -> str:
    """A row's painted text, as its own render cache spells it."""

    return "\n".join(strip.text.rstrip() for strip in row._render_cache.lines)


def plain_of(node: Any) -> str:
    """``node.render().plain`` without importing rich at every call site."""

    return str(node.render().plain)


# ── the turn driver double ──────────────────────────────────────────────────


def stub_run_chat(
    monkeypatch: Any,
    turns: list[dict[str, Any]],
    *,
    answer: str = "",
) -> Callable[..., str]:
    """Replace ``run_chat`` with a recorder that persists the turn it was given.

    ``run_chat`` is also what writes the session record, so the double makes the
    same two calls with the same ``ChatOptions`` fields the TUI passed: a turn's
    ``user`` is then provably the prompt the TUI handed over, and a test can
    assert on routing (``session``) as well as on the prompt.
    """

    def fake_run_chat(opts: Any, sink: Any = None) -> str:
        turns.append(
            {
                "prompt": opts.prompt,
                "session": opts.session,
                "continue": opts.continue_session,
                # The TUI owns the alternate screen, so the turn's failure
                # diagnostics must not go to stderr. Recorded here because
                # `run_chat` is the only consumer that can see the flag.
                "quiet_diagnostics": opts.quiet_diagnostics,
            }
        )
        session_store.begin_turn(opts.session, opts.prompt)
        session_store.finish_turn(opts.session, assistant=answer)
        return answer

    monkeypatch.setattr("miniouto.cli.tui.run_chat", fake_run_chat)
    return fake_run_chat


async def settle(pilot: Any, *, want: int, turns: list[Any], rounds: int = 300) -> None:
    """Pump the app until ``want`` turns have been dispatched."""

    for _ in range(rounds):
        if len(turns) >= want:
            return
        await pilot.pause(0.01)
    raise AssertionError(f"expected {want} dispatch(es), got {turns}")


async def settle_until(pilot: Any, predicate: Callable[[], bool], rounds: int = 300) -> None:
    """Pump the app until ``predicate`` holds (or fail loudly)."""

    for _ in range(rounds):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("condition never became true")


def clear_modals(app: ChatTUI) -> None:
    """Pop every pushed screen, leaving the base screen on top."""

    for _ in range(len(app.screens)):
        if not isinstance(app.screen, Modal):
            return
        app.pop_screen(None)


def chip_rows(app: ChatTUI) -> dict[str, Any]:
    """The panel's chip nodes, by kind."""

    return dict(app._panel.chips)


def chip_values(app: ChatTUI) -> dict[str, str]:
    """The four chips' painted labels (``label: value``), by kind."""

    return {kind: chip.render().plain for kind, chip in app._panel.chips.items()}


__all__ = [
    "BOOT_SIZE",
    "chip_rows",
    "chip_values",
    "clear_modals",
    "composed",
    "painted",
    "painted_line",
    "pin_session",
    "plain_of",
    "row_text",
    "settle",
    "settle_until",
    "stub_run_chat",
]
