"""Lock the `continue_session` tool: validate the folder, open a new session, hand off."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import coreouto as co
import pytest

import harness
import miniouto.tools.continue_session as tool_mod
from miniouto.cli import chat as chat_cli
from miniouto.cli.tui import ChatInput, ChatTUI
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


def test_the_continuation_names_the_new_session_as_the_command_routing_identity(
    summary_folder: Path,
) -> None:
    # Given: a run handing work from an old session into a new cont-* session
    summary.set_run_contract(session="handoff-old")
    continue_session(str(summary_folder))
    handoff = summary.pending_handoff()
    assert handoff is not None

    # When: the new session reads its opening message
    message = _delivered(handoff).text

    # Then: it cannot mistake the previous session named by the work contract
    # for the destination of a NEW --alarm-session / --session command
    assert f"Current session: {handoff.new_session}" in message
    assert f"Your current session is '{handoff.new_session}'" in message
    assert "Continue the work of session 'handoff-old'." in message
    assert "'handoff-old' is the previous session" in message
    assert "Do not copy the previous session's name" in message


def test_the_continuation_reexpands_this_session_with_the_new_session_name(
    monkeypatch: pytest.MonkeyPatch, summary_folder: Path
) -> None:
    """A hand-off must change the prompt, not only the transcript destination."""

    session = "handoff-token-old"

    async def handler(request: harness.Request) -> co.LLMResponse:
        if len(request.messages) == 2:
            return _tool_call_response(
                "call-1", "continue_session", summary_dir=str(summary_folder)
            )
        return co.LLMResponse(content="continued", tool_calls=[], raw=None)

    monkeypatch.setattr(tool_mod, "_mint_session", lambda: "cont-token-new")
    style = f"<outo>{harness.OUTO_TOKEN} --alarm-session {{this_session}}</outo>"
    provider = harness.install(monkeypatch, handler, session=session, style=style)
    summary.set_run_contract(session=session, style=harness.STYLE_NAME)
    chat_cli.run_turn_chain(
        chat_mod.ChatOptions(prompt="summarize", session=session), quiet=True
    )

    handoff = summary.pending_handoff()
    assert handoff is None
    systems = [
        next(m.content for m in request.messages if m.role == "system")
        for request in provider.requests
    ]
    assert [text.rsplit(" ", 1)[-1] for text in systems] == [
        session, "cont-token-new", "cont-token-new"
    ]
    continuation = provider.requests[1].user_texts()[0]
    assert "Current session: cont-token-new" in continuation
    assert "Your current session is 'cont-token-new'" in continuation


def test_a_tui_continuation_also_reexpands_this_session(
    monkeypatch: pytest.MonkeyPatch, summary_folder: Path
) -> None:
    import asyncio

    async def body() -> None:
        async def handler(request: harness.Request) -> co.LLMResponse:
            if request.index == 0:
                return _tool_call_response(
                    "call-1", "continue_session", summary_dir=str(summary_folder)
                )
            return co.LLMResponse(content="continued", tool_calls=[], raw=None)

        monkeypatch.setattr(tool_mod, "_mint_session", lambda: "cont-token-tui")
        style = f"<outo>{harness.OUTO_TOKEN} --alarm-session {{this_session}}</outo>"
        provider = harness.install(
            monkeypatch, handler, session="tui-token-old", style=style
        )
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.on_chat_input_submitted(ChatInput.Submitted("summarize"))
            for _ in range(60):
                await pilot.pause()
                if len(provider.requests) == 2:
                    break
            systems = [
                next(m.content for m in request.messages if m.role == "system")
                for request in provider.requests
            ]
            assert len(systems) == 2
            assert systems[1].endswith("cont-token-tui")

    asyncio.run(body())


def test_a_delegated_continuation_reexpands_its_role_prompt(
    monkeypatch: pytest.MonkeyPatch, summary_folder: Path
) -> None:
    """Role mode follows the same hand-off, not the caller's old session."""

    async def handler(request: harness.Request) -> co.LLMResponse:
        if request.index == 0:
            return _tool_call_response(
                "call-1", "continue_session", summary_dir=str(summary_folder)
            )
        return co.LLMResponse(content="continued", tool_calls=[], raw=None)

    monkeypatch.setattr(tool_mod, "_mint_session", lambda: "cont-token-role")
    style = (
        f"<outo>{harness.OUTO_TOKEN}</outo>\n"
        f"<editor>{harness.PERSONA_TOKEN} --alarm-session {{this_session}}</editor>"
    )
    provider = harness.install(
        monkeypatch, handler, session="role-token-old", style=style
    )
    state = chat_cli.ChainState(session="role-token-old")
    chat_cli.run_turn_chain(
        chat_mod.ChatOptions(
            prompt="summarize",
            session="role-token-old",
            style=harness.STYLE_NAME,
            role="editor",
        ),
        quiet=True,
        state=state,
        publish_session=False,
    )

    systems = [
        next(m.content for m in request.messages if m.role == "system")
        for request in provider.requests
    ]
    assert [text.rsplit(" ", 1)[-1] for text in systems] == [
        "role-token-old", "cont-token-role"
    ]
    assert state.session == "cont-token-role"
