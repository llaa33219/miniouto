"""A summarization hand-off is a session change inside ONE run — and it must be visible.

`core/summary.py` owns the vocabulary; this file owns what the CLI does with a
pending hand-off, because the chain is the only thing that can keep a run
alive across the transition:

* the chain moves its explicit turn routing into the new session, independently
  of the globally published active session, so the
  continuation message the tool already put in that session's inbox becomes
  the next turn;
* the marker is printed in the established `------…------` idiom, so a
  summarization reads as loudly as `------finish------`;
* the delegation post-step fires from the FINAL session — the receiver is
  pointed at the transcript that holds the answer — and names the session the
  work continued from, in the alarm AND in the `--return` file;
* the run contract is published before the turn, so the summarization lane
  knows which session, role, alarm session and result file this run owes.

`core/summary.py` is stubbed through the module reference (`chat_mod.summary`),
which keeps these tests about the CLI's wiring and independent of which
process happens to hold the pending hand-off.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from miniouto.cli import app
from miniouto.cli import chat as chat_mod
from miniouto.core import inbox, spawn_state
from miniouto.core import summary as summary_mod
from miniouto.core.chat import ChatOptions
from miniouto.core.summary import Handoff
from miniouto.storage import sessions as session_store
from miniouto.storage import settings as settings_store
from miniouto.storage import styles as style_store
from miniouto.storage import toml_io
from miniouto.storage.paths import (
    INBOX_DIR,
    SETTINGS_FILE,
    SPAWN_LOG_DIR,
    SUBAGENT_RESULTS_DIR,
)

runner = CliRunner()

ALARM_SESSION = "handoff-alarm-session"
SUMMARY_DIR = "/tmp/miniouto-summary/handoff-fixture"


class FakeSummary:
    """The hand-off seam, standing in for `core.summary`'s in-process state.

    Only what `cli/chat.py` calls is here: the pending hand-off triple and the
    run-contract slot. `Handoff` is the real frozen dataclass, so what the CLI
    consumes is exactly what the tool produces.
    """

    def __init__(self) -> None:
        self._pending: Handoff | None = None
        self.contracts: list[dict[str, str]] = []

    def request_handoff(self, new_session: str, *, from_session: str, summary_dir: str) -> None:
        self._pending = Handoff(
            new_session=new_session, from_session=from_session, summary_dir=summary_dir
        )

    def pending_handoff(self) -> Handoff | None:
        return self._pending

    def consume_handoff(self) -> Handoff | None:
        handoff, self._pending = self._pending, None
        return handoff

    def set_run_contract(self, **values: str) -> None:
        self.contracts.append(values)


def _chat(*args: str) -> Any:
    """Invoke the real entrypoint, `app(argv)` — argv normalisation included."""

    with runner.isolation() as streams:
        code = 0
        try:
            app(["chat", *args])
        except SystemExit as exit_signal:
            code = exit_signal.code if isinstance(exit_signal.code, int) else 1
        output = streams[2].getvalue().decode("utf-8", "replace")
    return code, output


def _job_ids() -> list[str]:
    return [p.stem for p in SPAWN_LOG_DIR.glob("*.json")]


def _result_files() -> list[Path]:
    return sorted(SUBAGENT_RESULTS_DIR.glob("*.md"))


def _alarms(session: str) -> list[str]:
    directory = inbox.inbox_dir(session)
    if not directory.is_dir():
        return []
    return [
        json.loads(p.read_text(encoding="utf-8"))["text"]
        for p in sorted(directory.glob("*.json"))
    ]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSummary:
    """Replace the hand-off seam, and drop the real one so tests cannot leak."""

    stub = FakeSummary()
    summary_mod.clear_handoff()
    monkeypatch.setattr(chat_mod, "summary", stub)
    yield stub
    summary_mod.clear_handoff()


@pytest.fixture(autouse=True)
def _isolated_home() -> Any:
    """Own settings + inbox per test: a transition writes `settings.session`."""

    toml_io.save(SETTINGS_FILE, {"style": "default", "session": "handoff-fixture-session"})
    for path in SUBAGENT_RESULTS_DIR.glob("*.md"):
        path.unlink()
    for path in SPAWN_LOG_DIR.glob("*") if SPAWN_LOG_DIR.is_dir() else []:
        path.unlink()
    for directory in INBOX_DIR.iterdir() if INBOX_DIR.is_dir() else []:
        if directory.is_dir():
            for message in directory.iterdir():
                message.unlink()
    yield
    settings_store.update(session="handoff-fixture-session")


def _summarizing(
    fake_summary: FakeSummary, calls: list[ChatOptions], *, handovers: int = 1
) -> Any:
    """A turn driver that summarizes its first `handovers` turns.

    The tool's two effects are reproduced verbatim: it requests the hand-off
    and it drops the continuation message into the NEW session's inbox.
    """

    def fake_run_chat(opts: ChatOptions, sink: Any = None) -> str:
        calls.append(opts)
        if len(calls) <= handovers:
            new_session = f"cont-{len(calls)}-{uuid.uuid4().hex[:4]}"
            fake_summary.request_handoff(
                new_session, from_session=opts.session, summary_dir=SUMMARY_DIR
            )
            inbox.insert(new_session, f"CONTINUE-{len(calls)}", kind="user", source="tool")
        return "ANSWER"

    return fake_run_chat


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[ChatOptions]:
    return []


# ── the transition itself ─────────────────────────────────────────────────────


def test_the_chain_continues_in_the_new_session(
    fake: FakeSummary, calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a turn that summarizes and calls continue_session
    monkeypatch.setattr(chat_mod, "run_chat", _summarizing(fake, calls))
    opts = ChatOptions(prompt="do the work", session="handoff-old")
    state = chat_mod.ChainState(session=opts.session, contract={"session": opts.session})

    # When: the whole chain runs
    chat_mod.run_turn_chain(opts, quiet=True, state=state)

    # Then: the run continued in the NEW session, on its continuation message
    assert [c.session for c in calls] == ["handoff-old", state.session]
    assert state.session.startswith("cont-1-")
    assert calls[1].prompt == "CONTINUE-1"
    assert calls[1].continue_session is True
    assert [h.new_session for h in state.transitions] == [state.session]


def test_the_transition_moves_the_explicit_turn_routing(
    fake: FakeSummary, calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given/When: a run that hands off to a new session
    monkeypatch.setattr(chat_mod, "run_chat", _summarizing(fake, calls))
    chat_mod.run_turn_chain(ChatOptions(prompt="go", session="handoff-old"), quiet=True)

    # Then: the next turn itself carries the new name — the runtime does not
    # consult the globally active session to select its inbox. A plain run
    # still publishes its final session so an unnamed `-c` wakeup can find it.
    assert calls[1].session.startswith("cont-1-")
    assert calls[1].session != "handoff-old"


def test_a_plain_transition_publishes_the_new_active_session(
    fake: FakeSummary, calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unnamed later `-c` wakeup belongs at the plain run's final session."""

    monkeypatch.setattr(chat_mod, "run_chat", _summarizing(fake, calls))
    code, _ = _chat("go", "--name", "handoff-old")
    assert code == 0
    assert calls[-1].session.startswith("cont-1-")
    assert settings_store.load().session == calls[-1].session


def test_the_marker_has_the_finish_marker_dash_shape() -> None:
    # Given: a pending hand-off
    handoff = Handoff(
        new_session="cont-7", from_session="chat-1", summary_dir=SUMMARY_DIR
    )

    # When / Then: the marker is the `------…------` idiom plus one summary line
    assert chat_mod.handoff_marker(handoff) == (
        f"------summary → cont-7------\nsummary: {SUMMARY_DIR}"
    )


def test_the_transition_prints_the_marker_then_the_new_session_marker(
    fake: FakeSummary, calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a delegated run that summarizes mid-work
    monkeypatch.setattr(chat_mod, "run_chat", _summarizing(fake, calls))
    result_path = SUBAGENT_RESULTS_DIR / "handoff-marker.md"

    # When: the whole run goes through the CLI
    code, output = _chat(
        "brief", "--alarm-session", ALARM_SESSION, "--return", str(result_path)
    )

    # Then: the transition and the new session's marker are both on stdout, in
    # that order, and the completion line still names the run — all of it
    # BEFORE the run's single finish marker, which ends the output
    new_session = calls[1].session
    assert code == 0
    assert f"------summary → {new_session}------" in output
    assert f"summary: {SUMMARY_DIR}" in output
    assert output.index(f"------summary → {new_session}------") < output.index(
        f"------{new_session}------"
    )
    assert "complete -----" in output
    assert output.count("------finish------") == 1
    assert output.index("complete -----") < output.index("------finish------")


def test_two_handoffs_in_one_run_continue_twice(
    fake: FakeSummary, calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a model that summarizes again after the first continuation
    monkeypatch.setattr(chat_mod, "run_chat", _summarizing(fake, calls, handovers=2))
    state = chat_mod.ChainState(session="handoff-old")

    # When: the chain runs
    chat_mod.run_turn_chain(ChatOptions(prompt="go", session="handoff-old"), quiet=True, state=state)

    # Then: three segments, and the state reports the LAST one
    assert len(calls) == 3
    assert len(state.transitions) == 2
    assert state.session == calls[2].session
    assert [c.prompt for c in calls[1:]] == ["CONTINUE-1", "CONTINUE-2"]


def test_a_run_that_keeps_summarizing_stops_at_the_bound(
    fake: FakeSummary, calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    # Given: a model that never stops calling continue_session
    monkeypatch.setattr(chat_mod, "run_chat", _summarizing(fake, calls, handovers=99))

    # When: the chain runs
    state = chat_mod.ChainState(session="handoff-old")
    chat_mod.run_turn_chain(ChatOptions(prompt="go", session="handoff-old"), quiet=True, state=state)

    # Then: it stops at the bound and says so, leaving the last handoff pending
    assert len(state.transitions) == chat_mod.HANDOFF_MAX_TRANSITIONS
    assert fake.pending_handoff() is not None
    assert str(chat_mod.HANDOFF_MAX_TRANSITIONS) in capsys.readouterr().err


# ── contract registration ─────────────────────────────────────────────────────


def test_a_plain_run_registers_its_session_and_nothing_else(
    fake: FakeSummary, calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given / When: an ordinary `chat` with no delegation flags
    monkeypatch.setattr(chat_mod, "run_chat", _no_handoff(calls))
    code, output = _chat("hello", "--name", "handoff-plain")

    # Then: exactly one contract, identity only — and byte-identical output
    assert code == 0
    assert fake.contracts == [
        {
            "session": "handoff-plain",
            "role": "",
            "style": "default",
            "alarm_session": "",
            "return_path": "",
        }
    ]
    assert "summary →" not in output
    assert "------handoff-plain------" in output


def _no_handoff(calls: list[ChatOptions]) -> Any:
    def fake_run_chat(opts: ChatOptions, sink: Any = None) -> str:
        calls.append(opts)
        return "ANSWER"

    return fake_run_chat


def test_a_delegated_run_registers_its_whole_contract(
    fake: FakeSummary, calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a style with a role, and a run that delegates with all four flags
    style_store.write(
        "handoff-fixture", "<outo>OUTO</outo>\n<editor>EDIT</editor>", overwrite=True
    )
    monkeypatch.setattr(chat_mod, "run_chat", _no_handoff(calls))
    result_path = SUBAGENT_RESULTS_DIR / "handoff-contract.md"

    # When: the delegated run starts
    code, _ = _chat(
        "brief",
        "--style", "handoff-fixture",
        "--role", "editor",
        "--alarm-session", ALARM_SESSION,
        "--return", str(result_path),
    )

    # Then: the summarization lane can re-state every promise after a hand-off
    assert code == 0
    assert fake.contracts[0] == {
        "session": fake.contracts[0]["session"],
        "role": "editor",
        "style": "handoff-fixture",
        "alarm_session": ALARM_SESSION,
        "return_path": str(result_path),
    }


def test_the_transition_republishes_the_contract_under_the_new_session(
    fake: FakeSummary, calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a delegated run that summarized
    monkeypatch.setattr(chat_mod, "run_chat", _summarizing(fake, calls))
    state = chat_mod.ChainState(
        session="handoff-old", contract={"session": "handoff-old", "role": "editor"}
    )

    # When: the chain hands off
    chat_mod.run_turn_chain(
        ChatOptions(prompt="go", session="handoff-old"), quiet=True, state=state,
        publish_session=False,
    )

    # Then: the same contract is published again, naming the new session, so a
    # second summary targets the right one; the GUEST does not publish itself.
    assert fake.contracts == [{"session": state.session, "role": "editor"}]
    assert settings_store.load().session == "handoff-fixture-session"


# ── the delegation post-step fires from the FINAL session ─────────────────────


def test_the_completion_alarm_names_the_final_session(
    fake: FakeSummary, calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a delegated run whose work moved into a new session
    monkeypatch.setattr(chat_mod, "run_chat", _summarizing(fake, calls))
    result_path = SUBAGENT_RESULTS_DIR / "handoff-alarm.md"

    # When: it completes
    code, _ = _chat("brief", "--alarm-session", ALARM_SESSION, "--return", str(result_path))

    # Then: exactly one alarm, pointing at the FINAL transcript and naming the
    # session the work continued from
    alarms = _alarms(ALARM_SESSION)
    final = calls[-1].session
    assert code == 0
    assert len(alarms) == 1
    assert f"Session: {final} — {session_store.path_for(final)}" in alarms[0]
    assert f"Continued from: {calls[0].session} (summary: {SUMMARY_DIR})" in alarms[0]


def test_the_result_file_leads_with_the_answer_and_appends_the_note(
    fake: FakeSummary, calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a delegated run that summarized
    monkeypatch.setattr(chat_mod, "run_chat", _summarizing(fake, calls))
    result_path = SUBAGENT_RESULTS_DIR / "handoff-note.md"

    # When: it completes
    _chat("brief", "--alarm-session", ALARM_SESSION, "--return", str(result_path))

    # Then: the file still opens with the answer; the note is a trailing section
    body = result_path.read_text(encoding="utf-8")
    assert body.startswith("ANSWER")
    assert f"Continued from: {calls[0].session} (summary: {SUMMARY_DIR})" in body
    assert body.index("ANSWER") < body.index("Continued from:")


def test_the_liveness_marker_names_the_final_session(
    fake: FakeSummary, calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a delegated run that summarized (and therefore wrote a marker)
    monkeypatch.setattr(chat_mod, "run_chat", _summarizing(fake, calls))

    # When: it completes
    code, _ = _chat(
        "brief",
        "--alarm-session", ALARM_SESSION,
        "--return", str(SUBAGENT_RESULTS_DIR / "handoff-marker-run.md"),
    )

    # Then: the marker's own session field is the final one — that field is how
    # a receiver of the alarm finds the transcript
    job_id = _job_ids()[0]
    marker = spawn_state.read_marker(spawn_state.marker_path(job_id))
    assert code == 0
    assert marker is not None
    assert marker.session == calls[-1].session
    assert marker.status == "done"
    assert marker.alarm_session == ALARM_SESSION


def test_a_run_without_a_handoff_reports_nothing_about_one(
    fake: FakeSummary, calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given / When: a delegated run that never summarized
    monkeypatch.setattr(chat_mod, "run_chat", _no_handoff(calls))
    result_path = SUBAGENT_RESULTS_DIR / "handoff-none.md"

    code, _ = _chat("brief", "--alarm-session", ALARM_SESSION, "--return", str(result_path))

    # Then: the plain contract — one session line, no continuation note
    assert code == 0
    assert "Continued from" not in _alarms(ALARM_SESSION)[0]
    assert result_path.read_text(encoding="utf-8") == "ANSWER"
    assert len(_result_files()) == 1
