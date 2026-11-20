"""Ctrl-C on a plain `miniouto chat`: a stop is recorded, not leaked.

`run_chat`'s failure handlers catch `Exception` — `KeyboardInterrupt` is a
`BaseException`, so an interrupt reaches `_run_foreground`'s plain branch with
the turn still `status="running"` on disk. Before this change nothing stamped
it: the record survived as a stale running turn until some later `begin_turn`
(possibly in another process) reaped it, which is exactly what a wrapper TUI
reading the session file cannot wait for.

The promises locked here:

- the running turn is stamped `interrupted` IMMEDIATELY, in the session the
  run ended in (`ChainState.session` — a hand-off may have moved it);
- an already-finished turn is never re-stamped (`finish_turn` would otherwise
  append a synthetic one, inventing a stop that never happened);
- stdout still ends with `------finish------` followed by the delegated
  branch's own `stopped: cancelled` line, and the process exits 130;
- quiet (`--answer-only`) adds NOTHING to stdout — the stamp still lands;
- a mid-stream interrupt closes the open stream line before the markers.

The hand-off marker persistence (the OLD session's on-disk record) is pinned
in `tests/test_handoff.py`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from typer.testing import CliRunner

from miniouto.cli import app
from miniouto.cli import chat as chat_mod
from miniouto.core.chat import ChatOptions
from miniouto.storage import sessions as session_store
from miniouto.storage import toml_io
from miniouto.storage.paths import SETTINGS_FILE
from miniouto.storage.sessions import STATUS_INTERRUPTED, STATUS_RUNNING


def _session() -> str:
    return f"kbi-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _own_settings() -> Any:
    """Own `settings.toml`: a plain chat republishes the session it ran in."""

    saved = toml_io.load(SETTINGS_FILE)
    yield
    toml_io.save(SETTINGS_FILE, saved)


def _chat(*args: str) -> tuple[int, str, str]:
    """Invoke the real entrypoint, `app(argv)` — argv normalisation included."""

    with CliRunner().isolation() as streams:
        code = 0
        try:
            app(["chat", *args])
        except SystemExit as exit_signal:
            code = exit_signal.code if isinstance(exit_signal.code, int) else 1
        out = streams[0].getvalue().decode("utf-8", "replace")
        err = streams[1].getvalue().decode("utf-8", "replace")
    return code, out, err


def _interrupting(monkeypatch: pytest.MonkeyPatch, *, stamp_running: bool) -> list[ChatOptions]:
    """A turn that leaves a running record behind and raises KeyboardInterrupt.

    `stamp_running` reproduces the REAL interrupted state: the loop never
    reached `finish_turn`, so the turn on disk is whatever `begin_turn` wrote.
    """

    seen: list[ChatOptions] = []

    def fake_run_chat(opts: ChatOptions, sink: Any = None) -> str:
        seen.append(opts)
        if stamp_running:
            session_store.begin_turn(opts.session or "", opts.prompt)
        raise KeyboardInterrupt

    monkeypatch.setattr(chat_mod, "run_chat", fake_run_chat)
    return seen


# ── the turn record ───────────────────────────────────────────────────────────


def test_an_interrupted_turn_is_stamped_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a plain chat whose turn is interrupted mid-loop
    session = _session()
    _interrupting(monkeypatch, stamp_running=True)

    # When: the interrupt propagates out of the chain
    code, _, _ = _chat("do the work", "--name", session)

    # Then: the turn on disk says interrupted NOW, not "running until reaped"
    turns = session_store.load(session).turns
    assert code == 130
    assert turns and turns[-1].status == STATUS_INTERRUPTED
    assert session_store.load(session).turns[-1].status != STATUS_RUNNING


def test_a_finished_turn_is_not_stamped_again(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: the interrupt arrives AFTER the turn record was already finished
    # (a hand-off turn is stamped done by `run_chat` before the chain moves on)
    session = _session()

    def fake_run_chat(opts: ChatOptions, sink: Any = None) -> str:
        session_store.begin_turn(opts.session or "", opts.prompt)
        session_store.finish_turn(opts.session or "", assistant="all done")
        raise KeyboardInterrupt

    monkeypatch.setattr(chat_mod, "run_chat", fake_run_chat)

    # When: the run is interrupted anyway
    code, _, _ = _chat("do the work", "--name", session)

    # Then: no synthetic second turn — the stop must not invent history
    turns = session_store.load(session).turns
    assert code == 130
    assert len(turns) == 1
    assert turns[0].status == "done"
    assert turns[0].assistant == "all done"


def test_no_turn_at_all_costs_no_synthetic_one(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: the interrupt fires before any turn was recorded
    session = _session()
    _interrupting(monkeypatch, stamp_running=False)

    # When: the run dies with nothing on disk
    code, _, _ = _chat("do the work", "--name", session)

    # Then: nothing was invented — there was no running turn to stamp
    turns = session_store.load(session).turns
    assert code == 130
    assert turns == []


# ── the stdout contract ───────────────────────────────────────────────────────


def test_the_stop_line_and_finish_marker_reach_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an interrupted plain chat
    session = _session()
    _interrupting(monkeypatch, stamp_running=True)

    # When: the run exits
    code, output, _ = _chat("do the work", "--name", session)

    # Then: the session marker, then the run's single finish marker, then the
    # delegated branch's own stop wording — and nothing after it
    assert code == 130
    assert output.startswith(f"------{session}------\n")
    assert "stopped: cancelled" in output
    assert output.count("------finish------") == 1
    assert output.index("------finish------") < output.index("stopped: cancelled")
    assert output.rstrip("\n").endswith("stopped: cancelled")


def test_quiet_adds_nothing_to_stdout_but_still_stamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an interrupted `--answer-only` run
    session = _session()
    _interrupting(monkeypatch, stamp_running=True)

    # When: it exits quietly
    code, output, err = _chat("do the work", "--name", session, "--answer-only")

    # Then: byte-identical quiet output (no markers, no stop line) — but the
    # session stamp still landed
    assert code == 130
    assert output == ""
    assert err == ""
    turns = session_store.load(session).turns
    assert turns and turns[-1].status == STATUS_INTERRUPTED


def test_a_mid_stream_interrupt_closes_the_line_before_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a turn whose answer tokens already streamed part of a line
    session = _session()

    def streaming_then_interrupt(opts: ChatOptions, sink: Any = None) -> str:
        session_store.begin_turn(opts.session or "", opts.prompt)
        sink.emit_stream_delta("text", "partial answer without a newline")
        raise KeyboardInterrupt

    monkeypatch.setattr(chat_mod, "run_chat", streaming_then_interrupt)

    # When: the interrupt lands mid-stream
    code, output, _ = _chat("do the work", "--name", session)

    # Then: the finish marker starts on its OWN line — the stream's dangling
    # half-line is closed first, so no marker ever shares a line with answer
    # text (the same contract `_print_answer` keeps for a streamed answer)
    assert code == 130
    assert "partial answer without a newline\n------finish------\n" in output


# ── exit code ─────────────────────────────────────────────────────────────────


def test_the_process_exits_130_not_1(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given / When: an interrupted plain chat
    session = _session()
    _interrupting(monkeypatch, stamp_running=True)
    code, _, _ = _chat("do the work", "--name", session)

    # Then: 130 — SIGINT's conventional exit status, distinct from the
    # delegated branch's exit 1 (a stop is not an error)
    assert code == 130


def test_a_delegated_run_keeps_its_own_interrupt_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an interrupted DELEGATED run (the post-step branch)
    session = _session()

    def cancel(opts: ChatOptions, sink: Any = None) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(chat_mod, "run_chat", cancel)

    # When: it exits
    code, output, _ = _chat("brief", "--name", session, "--alarm-session", f"{session}-rx")

    # Then: unchanged — the answer says stopped, the run exits 1
    assert code == 1
    assert "stopped: cancelled" in output
