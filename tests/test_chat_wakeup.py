"""`miniouto chat` is a WAKEUP model: it answers and it dies.

The command that once waited is gone. A queued message — a detached run's
completion alarm, a human's interjection, a monitor's poke — is a wakeup for
whoever is awake, and nothing in a one-shot chat may sit on it: the run's turn
runs, the answer prints behind ONE `------finish------`, and the process exits.
Waiting could only ever have been satisfied by the markers this run happened to
start, which is a closed-ecosystem assumption: any producer anywhere may
`miniouto message insert` into any inbox.

What replaces waiting is `--alarms -c`: an explicit wakeup that drains one
session's inbox into the turn's prompt. `--continue` is mandatory with it,
because history is the operator's explicit choice and never an implied one; and
the named session must exist, or there is nothing to wake.

The one loop that survives is the summarization hand-off (`core/summary.py`): a
session change inside the SAME run, bounded by `HANDOFF_MAX_TRANSITIONS`, whose
output contract is the same finish-once rule.

The promises locked here:

- pending spawns + queued alarms do not keep the run alive, and do not cost it
  a single message;
- `--alarms` validation costs no model call and writes no session file;
- a wakeup's turn prompt IS the drained inbox, blank-line joined, and the
  history rides in only because `-c` was passed;
- an empty inbox is a no-op, not a turn;
- `------finish------` is printed exactly once, as the last marker of a run,
  after the delegated completion line.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import coreouto as co
import pytest
from typer.testing import CliRunner

import harness
from miniouto.cli import app
from miniouto.cli import chat as chat_mod
from miniouto.core import inbox, spawn_state
from miniouto.core import summary as summary_mod
from miniouto.core.chat import ChatOptions
from miniouto.core.spawn_state import SpawnMarker
from miniouto.storage import sessions as session_store
from miniouto.storage import settings as settings_store
from miniouto.storage import toml_io
from miniouto.storage.paths import SETTINGS_FILE


def _session() -> str:
    return f"wakeup-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _restore_settings() -> Any:
    """Own `settings.toml`: a wakeup republishes the session it continues."""

    saved = toml_io.load(SETTINGS_FILE)
    yield
    toml_io.save(SETTINGS_FILE, saved)
    summary_mod.clear_handoff()


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


def _answering(monkeypatch: pytest.MonkeyPatch, answer: str = "ANSWER") -> list[ChatOptions]:
    """One turn, one answer — and a record of the options it was handed."""

    seen: list[ChatOptions] = []

    def fake_run_chat(opts: ChatOptions, sink: Any = None) -> str:
        seen.append(opts)
        return answer

    monkeypatch.setattr(chat_mod, "run_chat", fake_run_chat)
    return seen


# ── A. a run never waits ─────────────────────────────────────────────────────


def test_pending_spawns_and_queued_alarms_do_not_keep_the_run_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a detached run this session still owes, and two of its messages
    # already queued — the exact state the old chain waited on
    session = _session()
    marker = SpawnMarker(
        id=uuid.uuid4().hex[:6],
        alarm_session=session,
        started="2026-01-01T00:00:00",
        role="editor",
        pid=os.getpid(),
    )
    spawn_state.write_marker(marker)
    inbox.insert(session, "alarm one", kind="alarm", source="spawn")
    inbox.insert(session, "alarm two", kind="alarm", source="spawn")
    seen = _answering(monkeypatch)

    # When: the whole run goes through the CLI
    code, output, _ = _chat("do the work", "--name", session)

    # Then: exactly one turn, one finish marker, and the queued work untouched
    assert code == 0
    assert [o.prompt for o in seen] == ["do the work"]
    assert output == f"------{session}------\n------finish------\nANSWER\n"
    assert inbox.pending_count(session) == 2


def test_a_dead_run_leaving_a_running_marker_still_does_not_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a marker promising an alarm from a process that is gone for good
    session = _session()
    spawn_state.write_marker(
        SpawnMarker(
            id=uuid.uuid4().hex[:6],
            alarm_session=session,
            started="2026-01-01T00:00:00",
            pid=1,
        )
    )
    seen = _answering(monkeypatch)

    # When / Then: the run answered and returned instead of polling a promise
    code, _, _ = _chat("do the work", "--name", session)
    assert code == 0
    assert [o.prompt for o in seen] == ["do the work"]


def test_the_waiting_machinery_is_gone_from_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given / When: the module that used to own the wait
    # Then: no chain loop, no bounds, no polling — nothing to reinstate
    assert not hasattr(chat_mod, "_run_followups")
    assert not [name for name in dir(chat_mod) if name.startswith("FOLLOWUP_")]
    seen = _answering(monkeypatch)
    chat_mod.run_turn_chain(ChatOptions(prompt="go", session=_session()), quiet=True)
    assert len(seen) == 1


# ── B. `--alarms` wakes a session from its queued messages ────────────────────


def test_alarms_without_continue_is_rejected_before_anything_happens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a session with messages waiting, and a wakeup that forgot -c
    session = _session()
    session_store.begin_turn(session, "earlier")
    session_store.finish_turn(session, assistant="earlier answer")
    inbox.insert(session, "wake up", kind="alarm", source="spawn")
    seen = _answering(monkeypatch)

    # When: the wakeup goes through the CLI
    code, output, err = _chat("--alarms", "--name", session)

    # Then: rejected with the one sentence that says why, and nothing ran
    assert code == 1
    assert err == "✗ a wakeup continues an existing session — pass --continue\n"
    assert output == ""
    assert seen == []
    assert inbox.pending_count(session) == 1, "a rejected wakeup must not consume the inbox"


def test_alarms_with_a_prompt_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a prompt alongside --alarms — two sources for one user message
    session = _session()
    seen = _answering(monkeypatch)

    # When / Then: rejected, with no session file written
    code, _, err = _chat("also do this", "--alarms", "--continue", "--name", session)
    assert code == 1
    assert "--alarms takes no prompt argument" in err
    assert seen == []
    assert not session_store.path_for(session).exists()


def test_alarms_on_a_session_that_does_not_exist_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a named session that was never written
    missing = _session()
    inbox.insert(missing, "wake up", kind="alarm", source="spawn")
    seen = _answering(monkeypatch)

    # When / Then: rejected by name, and the queue it cannot use is intact
    code, _, err = _chat("--alarms", "--continue", "--name", missing)
    assert code == 1
    assert err == f"✗ nothing to wake: session '{missing}' does not exist\n"
    assert seen == []
    assert inbox.pending_count(missing) == 1


def test_alarms_on_an_empty_inbox_is_a_note_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an existing session with an empty inbox
    session = _session()
    session_store.begin_turn(session, "earlier")
    session_store.finish_turn(session, assistant="earlier answer")
    seen = _answering(monkeypatch)

    # When: it is woken
    code, output, _ = _chat("--alarms", "--continue", "--name", session)

    # Then: one line, exit 0, no turn — so no session marker and no finish
    assert code == 0
    assert output == f"no pending messages in session '{session}'.\n"
    assert seen == []
    assert "------finish------" not in output


def test_alarms_drains_the_queue_into_the_turn_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: two queued messages on an existing session
    session = _session()
    session_store.begin_turn(session, "earlier")
    session_store.finish_turn(session, assistant="earlier answer")
    inbox.insert(session, "alarm one", kind="alarm", source="spawn")
    inbox.insert(session, "alarm two", kind="alarm", source="spawn")
    seen = _answering(monkeypatch)

    # When: it is woken
    code, output, _ = _chat("--alarms", "--continue", "--name", session)

    # Then: the joined texts ARE the turn's user message, and history rides in
    # only because -c was passed
    assert code == 0
    assert [o.prompt for o in seen] == ["alarm one\n\nalarm two"]
    assert seen[0].continue_session is True
    assert seen[0].session == session
    assert output == (
        f"------{session}------\n------finish------\nANSWER\n"
    )
    assert inbox.pending_count(session) == 0, "a delivered wakeup is consumed once"


def test_alarms_still_ends_with_exactly_one_finish_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given / When: a wakeup whose turn arms a further alarm of its own
    session = _session()
    session_store.begin_turn(session, "earlier")
    session_store.finish_turn(session, assistant="earlier answer")
    inbox.insert(session, "alarm one", kind="alarm", source="spawn")

    def fake_run_chat(opts: ChatOptions, sink: Any = None) -> str:
        inbox.insert(opts.session, "and one more", kind="alarm", source="spawn")
        return "ANSWER"

    monkeypatch.setattr(chat_mod, "run_chat", fake_run_chat)

    code, output, _ = _chat("--alarms", "--continue", "--name", session)

    # Then: the run answered its one wakeup and stopped; the next alarm is the
    # next wakeup's business
    assert code == 0
    assert output == f"------{session}------\n------finish------\nANSWER\n"
    assert inbox.pending_count(session) == 1


def test_a_wakeup_asks_the_model_the_queued_question_over_its_own_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a real session with a real turn behind it, and one queued message
    session = "e2e-wakeup-history"
    asked: list[list[str]] = []

    async def handler(req: harness.Request) -> co.LLMResponse:
        asked.append(req.user_texts())
        return co.LLMResponse(content="answered the wakeup")

    provider = harness.install(monkeypatch, handler, session=session)
    code, _, _ = _chat("what did we decide about the cache?", "--name", session)
    assert code == 0
    inbox.insert(session, "and now the logs", kind="alarm", source="spawn")

    # When: the session is woken
    code, _, _ = _chat("--alarms", "--continue", "--name", session)

    # Then: the model saw BOTH turns — the earlier question from `-c`, and the
    # drained wakeup as this turn's user message
    assert code == 0
    assert asked[0] == ["what did we decide about the cache?"]
    assert asked[1] == ["what did we decide about the cache?", "and now the logs"]
    assert [m.role for m in provider.for_actor("outo")[1].messages][-1] == "user"


# ── C. the output contract: one finish, at the true end ───────────────────────


def test_a_single_turn_run_keeps_the_finish_then_answer_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a plain run with nothing queued behind it
    _answering(monkeypatch, answer="THE ANSWER")

    # When / Then: the marker, then the answer, and nothing after them
    code, output, _ = _chat("do the work", "--name", _session())
    assert code == 0
    assert output.endswith("------finish------\nTHE ANSWER\n")
    assert output.count("------finish------") == 1


def test_answer_only_prints_the_answer_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given / When: the same run under -a
    _answering(monkeypatch, answer="THE ANSWER")

    code, output, _ = _chat("do the work", "--name", _session(), "--answer-only")
    assert code == 0
    assert output == "THE ANSWER\n"


def test_a_handoff_chain_prints_finish_once_after_the_completion_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: a delegated run that summarizes mid-work, so the chain continues
    # in a new session and the post-step has not fired yet
    calls: list[ChatOptions] = []

    def summarizing(opts: ChatOptions, sink: Any = None) -> str:
        calls.append(opts)
        if len(calls) == 1:
            new_session = f"cont-{uuid.uuid4().hex[:6]}"
            summary_mod.request_handoff(
                new_session, from_session=opts.session, summary_dir=str(tmp_path)
            )
            inbox.insert(new_session, "CONTINUE", kind="user", source="tool")
        return "ANSWER"

    monkeypatch.setattr(chat_mod, "run_chat", summarizing)
    result = tmp_path / "result.md"

    # When: it completes
    code, output, _ = _chat(
        "brief", "--name", _session(), "--return", str(result), "--alarm-session", "someone"
    )

    # Then: two turns, the transition mid-run, the completion line, then ONE
    # finish marker — and nothing whatsoever after the answer
    final = calls[-1].session
    assert code == 0
    assert len(calls) == 2
    assert output.index(f"------summary → {final}------") < output.index("complete -----")
    assert output.index("complete -----") < output.index("------finish------")
    assert output.count("------finish------") == 1
    assert output.endswith("------finish------\nANSWER\n")


def test_a_handoff_whose_continuation_message_is_missing_still_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: a hand-off whose inbox holds nothing — the message the tool was
    # supposed to drop never arrived
    calls: list[ChatOptions] = []

    def summarizing(opts: ChatOptions, sink: Any = None) -> str:
        calls.append(opts)
        if len(calls) == 1:
            summary_mod.request_handoff(
                f"cont-{uuid.uuid4().hex[:6]}", from_session=opts.session, summary_dir=str(tmp_path)
            )
        return "ANSWER"

    monkeypatch.setattr(chat_mod, "run_chat", summarizing)

    # When / Then: the run still continues, told by the contract itself
    chat_mod.run_turn_chain(ChatOptions(prompt="go", session=_session()), quiet=True)
    assert len(calls) == 2
    assert calls[1].prompt.startswith(summary_mod.ALARM_HEADER)
    assert str(tmp_path) in calls[1].prompt


def test_the_settings_session_follows_a_wakeup_it_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a session named explicitly, with history to continue
    session = _session()
    session_store.begin_turn(session, "earlier")
    session_store.finish_turn(session, assistant="earlier answer")
    inbox.insert(session, "wake up", kind="alarm", source="spawn")
    _answering(monkeypatch)

    # When / Then: the woken session became the active one
    code, _, _ = _chat("--alarms", "--continue", "--name", session)
    assert code == 0
    assert settings_store.load().session == session
