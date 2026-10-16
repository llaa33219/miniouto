"""Lock the `continue_session` tool: validate the folder, open a new session, hand off."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import coreouto as co
import pytest

import harness
import miniouto.tools.continue_session as tool_mod
from miniouto.core import chat as chat_mod
from miniouto.core import inbox, summary
from miniouto.core.summary import Handoff
from miniouto.storage import session_config
from miniouto.storage import sessions as session_store
from miniouto.storage.paths import INBOX_DIR
from miniouto.tools.continue_session import continue_session


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    summary.clear_handoff()
    summary.clear_run_contract()
    yield
    summary.clear_handoff()
    summary.clear_run_contract()
    for directory in INBOX_DIR.glob("cont-*"):
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def summary_folder(tmp_path: Path) -> Path:
    directory = tmp_path / "summary" / "s-original"
    directory.mkdir(parents=True)
    (directory / "00-overview.md").write_text("# work\n", encoding="utf-8")
    return directory


def _delivered(handoff: Handoff) -> inbox.InboxMessage:
    messages = inbox.drain(handoff.new_session)
    assert len(messages) == 1
    return messages[0]


def _cont_dirs() -> list[str]:
    return sorted(p.name for p in INBOX_DIR.glob("cont-*") if p.is_dir())


def _missing(base: Path) -> Path:
    return base / "missing"


def _a_file(base: Path) -> Path:
    target = base / "afile.md"
    target.write_text("not a folder", encoding="utf-8")
    return target


def _no_markdown(base: Path) -> Path:
    directory = base / "empty"
    directory.mkdir()
    (directory / "notes.txt").write_text("no summary here", encoding="utf-8")
    return directory


def test_a_valid_folder_opens_a_cont_session_left_pending_for_the_entry_layer(
    summary_folder: Path,
) -> None:
    # Given: a summary folder with documents, and no registered run contract
    # When: the model hands off through it
    ack = continue_session(str(summary_folder))
    # Then: a `cont-*` session was opened and left PENDING for the entry layer
    handoff = summary.pending_handoff()
    assert handoff is not None
    assert handoff.new_session.startswith("cont-")
    assert handoff.summary_dir == str(summary_folder.resolve())
    assert handoff.new_session in ack


def test_the_continuation_message_points_at_the_folder_and_names_the_previous_session(
    summary_folder: Path,
) -> None:
    # Given: a summary folder with documents
    # When: the model hands off through it
    continue_session(str(summary_folder))
    handoff = summary.pending_handoff()
    assert handoff is not None
    message = _delivered(handoff)
    # Then: the new session is told to read that folder and continue THAT session
    assert message.kind == inbox.ALARM_KIND
    assert message.source == "summary"
    assert message.meta == {"from": summary_folder.name}
    assert message.text.startswith(inbox.ALARM_HEADER)
    assert str(summary_folder.resolve()) in message.text
    assert summary_folder.name in message.text
    assert "DELEGATION CONTRACT" not in message.text


def test_from_session_comes_from_the_run_contract_when_one_is_registered(
    summary_folder: Path,
) -> None:
    # Given: a run that registered its identity (the entry layer does this)
    summary.set_run_contract(session="chat-1234", role="editor", style="coding")
    # When: the model hands off
    continue_session(str(summary_folder))
    handoff = summary.pending_handoff()
    assert handoff is not None
    # Then: the continuation names the RUN's session, not the folder
    assert handoff.from_session == "chat-1234"
    assert "chat-1234" in _delivered(handoff).text


def test_from_session_falls_back_to_the_folder_name_without_a_contract(
    summary_folder: Path,
) -> None:
    # Given: a hand-off from a run that registered no contract (a plain outo turn)
    # When: the model hands off
    continue_session(str(summary_folder))
    handoff = summary.pending_handoff()
    assert handoff is not None
    # Then: the folder's own name identifies where the work came from
    assert handoff.from_session == summary_folder.name


def test_every_contract_key_carries_over_into_the_continuation_message(
    summary_folder: Path,
) -> None:
    # Given: a delegated run that owes its caller an alarm and a result file
    summary.set_run_contract(
        session="spawn-1",
        role="validator",
        style="coding-work",
        alarm_session="parent-session",
        return_path="/tmp/out.md",
    )
    # When: the model hands off
    continue_session(str(summary_folder))
    handoff = summary.pending_handoff()
    assert handoff is not None
    text = _delivered(handoff).text
    # Then: the new session is told it must finish the same contract
    assert "DELEGATION CONTRACT (carried over)" in text
    for value in ("validator", "coding-work", "parent-session", "/tmp/out.md"):
        assert value in text


def test_the_ack_closes_the_conversation_and_carries_the_contract(
    summary_folder: Path,
) -> None:
    # Given: a delegated run that hands off
    summary.set_run_contract(
        session="spawn-1", role="validator", style="coding-work", alarm_session="parent"
    )
    # When: the model hands off
    ack = continue_session(str(summary_folder))
    # Then: the ack ends the conversation here and names the carried contract
    assert ack.startswith("Handoff complete — this conversation is now closed.")
    assert "Delegation contract carried over (alarm session 'parent', " in ack
    assert ack.endswith("Stop working in this session now.")


def test_the_new_session_inherits_the_source_sessions_provider_pin(
    summary_folder: Path,
) -> None:
    # Given: a hand-off from a session that was pinned to a provider
    summary.set_run_contract(session="chat-pinned", role="editor", style="coding")
    session_config.set_fields(
        "chat-pinned", provider="probe-prov", model="probe-model"
    )

    # When: the model hands off
    continue_session(str(summary_folder))
    handoff = summary.pending_handoff()
    assert handoff is not None

    # Then: the continuation runs on the SAME settings — a hand-off moves
    # the conversation, never the provider configuration under it
    assert session_config.read(handoff.new_session) == session_config.read("chat-pinned")
    session_config.clear(handoff.new_session)


@pytest.mark.parametrize(
    ("make_target", "reason"),
    [
        pytest.param(_missing, "does not exist", id="missing"),
        pytest.param(_a_file, "not a folder", id="not-a-folder"),
        pytest.param(_no_markdown, "*.md", id="no-markdown"),
    ],
)
def test_an_unusable_folder_returns_an_error_and_hands_off_nothing(
    tmp_path: Path, make_target, reason: str
) -> None:
    # Given: a folder argument the summary request's layout cannot be found in
    target = make_target(tmp_path)
    # When: the model hands off through it
    ack = continue_session(str(target))
    # Then: a clear error comes back, and NOTHING was handed off
    assert ack.startswith("continue_session failed — no handoff was made.")
    assert reason in ack
    assert "write the summary documents first, then call continue_session" in ack
    assert summary.pending_handoff() is None
    assert _cont_dirs() == []


def test_a_blank_folder_argument_is_rejected() -> None:
    # Given: no folder at all
    # When: the model hands off
    ack = continue_session("   ")
    # Then: the error names the missing argument and hands off nothing
    assert "summary_dir" in ack
    assert summary.pending_handoff() is None
    assert _cont_dirs() == []


def test_a_relative_folder_resolves_against_the_invocation_cwd(
    summary_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the model passing the relative path the summary request named
    monkeypatch.setattr(tool_mod, "INVOCATION_CWD", summary_folder.parents[1])
    # When: it hands off
    continue_session("summary/s-original")
    handoff = summary.pending_handoff()
    # Then: the folder is resolved to its absolute location
    assert handoff is not None
    assert handoff.summary_dir == str(summary_folder.resolve())


# ── the loop side: a hand-off ends the turn cleanly ──────────────────────────


def _tool_call_response(call_id: str, name: str, **arguments: object) -> co.LLMResponse:
    return co.LLMResponse(
        content="",
        tool_calls=[co.ToolCall(id=call_id, name=name, arguments=dict(arguments))],
        raw=None,
    )


def test_a_turn_that_hands_off_stops_before_another_llm_call_and_finishes_done(
    monkeypatch: pytest.MonkeyPatch, summary_folder: Path
) -> None:
    # Given: a turn that hands off through the real tool, and would otherwise
    # keep working (the model has a second answer scripted)
    session = "handoff-turn"
    calls: list[str] = []

    async def handler(request: harness.Request) -> co.LLMResponse:
        calls.append(request.actor)
        if len(calls) == 1:
            return _tool_call_response(
                "call-1", "continue_session", summary_dir=str(summary_folder)
            )
        return co.LLMResponse(content="more work in the old session", tool_calls=[], raw=None)

    harness.install(monkeypatch, handler, session=session)

    # When: the turn runs
    answer = chat_mod.run_chat(chat_mod.ChatOptions(prompt="keep going", session=session))

    # Then: the loop stopped at the hand-off — the second answer never happened
    assert calls == ["outo"]
    assert "more work" not in answer

    # And the turn finished DONE (not cancelled/interrupted), with the
    # hand-off still PENDING: core never consumes it — the entry layer does.
    assert session_store.load(session).turns[-1].status == session_store.STATUS_DONE
    handoff = summary.pending_handoff()
    assert handoff is not None
    assert handoff.from_session == summary_folder.name
    assert handoff.new_session in answer
