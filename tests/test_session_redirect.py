"""Persistent session redirects: a summarized session points at its successor.

The summary hand-off used to live in an in-process slot, so it evaporated
with the process. `redirect_to` persists the link in the session file
itself: interactive use (alarm, resume, chat) resolves it to land on the
session that actually continues the work, chains included (A→B→C→D),
while plain viewing never follows it. The storage layer only stores and
resolves the field; the TRIGGERS — the `continue_session` tool recording
the hand-off, the CLI's `-c`/`--alarms` resume, the delegated alarm's
delivery, and the TUI's chat input — are wired on top and locked below."""


from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import tui_harness
from miniouto.cli import app as cli_app
from miniouto.cli import chat as chat_cli
from miniouto.cli.chat import _deliver_alarm
from miniouto.cli.tui import ChatInput, ChatTUI
from miniouto.core import inbox, summary
from miniouto.core.chat import ChatOptions
from miniouto.storage import sessions as session_store
from miniouto.storage import settings as settings_store
from miniouto.storage.paths import SESSION_DIR
from miniouto.tools.continue_session import continue_session


def _chat(*args: str) -> tuple[int, str]:
    """Invoke the real entrypoint, `app(argv)` — argv normalisation included."""

    runner = CliRunner()
    with runner.isolation() as streams:
        code = 0
        try:
            cli_app(["chat", *args])
        except SystemExit as exit_signal:
            code = exit_signal.code if isinstance(exit_signal.code, int) else 1
        output = streams[0].getvalue().decode("utf-8", "replace")
    return code, output


def _run(alarm_session: str) -> Any:
    """A minimal `_Run` shaped just enough for `_deliver_alarm`."""

    return chat_cli._Run(
        prompt="brief",
        session="redir-worker",
        style="default",
        role=None,
        provider=None,
        model=None,
        reasoning=None,
        use_subagent_provider=False,
        max_tokens=None,
        temperature=None,
        continue_session=False,
        answer_only=False,
        with_session=False,
        alarm_session=alarm_session,
        path=None,
    )


@pytest.fixture
def summary_folder(tmp_path: Path) -> Path:
    """A valid summary folder, the shape `continue_session` accepts."""

    directory = tmp_path / "summary" / "redir-tool-from"
    directory.mkdir(parents=True)
    (directory / "00-overview.md").write_text("# work\n", encoding="utf-8")
    return directory


@pytest.fixture(autouse=True)
def _restore_state() -> Any:
    """Own the shared slots: a trigger test must not leak into the next."""

    saved_session = settings_store.load().session
    summary.clear_handoff()
    summary.clear_run_contract()
    yield
    summary.clear_handoff()
    summary.clear_run_contract()
    if saved_session:
        settings_store.update(session=saved_session)
    for directory in SESSION_DIR.glob("cont-*"):
        shutil.rmtree(directory, ignore_errors=True)
    for directory in inbox.INBOX_DIR.glob("redir-*"):
        if directory.is_dir():
            for message in directory.iterdir():
                message.unlink(missing_ok=True)


def _write(name: str, payload: dict[str, object]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    (SESSION_DIR / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def _raw(name: str) -> dict[str, object]:
    return json.loads(session_store.path_for(name).read_text(encoding="utf-8"))


def _v2(name: str, **extra: object) -> dict[str, object]:
    """A minimal pre-redirect v2 session file, as older builds wrote them."""

    return {"version": 2, "session": name, "history": [], "turns": [], **extra}


def test_set_redirect_persists_and_load_returns_it() -> None:
    # Given: an existing session
    name, target = "redir-a", "redir-b"
    session_store.create(name)

    # When: a redirect is stamped
    assert session_store.set_redirect(name, target) is True

    # Then: it survives on disk and comes back through load
    assert session_store.load(name).redirect_to == target
    assert _raw(name)["redirect_to"] == target


def test_set_redirect_does_not_create_missing_session() -> None:
    # Given: a session that never existed
    name = "redir-ghost"

    # When: a redirect is attempted
    assert session_store.set_redirect(name, "redir-anywhere") is False

    # Then: no file was created — redirects belong to existing sessions
    assert not session_store.path_for(name).exists()


def test_set_redirect_rejects_self_reference() -> None:
    # Given: an existing session
    name = "redir-self"
    session_store.create(name)

    # When: it is pointed at itself
    assert session_store.set_redirect(name, name) is False

    # Then: nothing was written and the walk would not pin on step one
    assert "redirect_to" not in _raw(name)
    assert session_store.resolve_redirect(name) == (name, [name])


def test_set_redirect_empty_target_clears() -> None:
    # Given: a session with a redirect on disk
    name = "redir-clear"
    session_store.create(name)
    session_store.set_redirect(name, "redir-other")

    # When: an empty target clears it
    assert session_store.set_redirect(name, "") is True

    # Then: the field is gone from memory and from the file
    assert session_store.load(name).redirect_to == ""
    assert "redirect_to" not in _raw(name)


def test_save_omits_empty_redirect_to() -> None:
    # Given: a session whose redirect was never set (or was cleared)
    name = "redir-plain"
    session_store.create(name)

    # When: it is re-saved as-is
    session_store.save(name, session_store.load(name))

    # Then: no empty field is injected into the file
    assert "redirect_to" not in _raw(name)


def test_load_backward_compatible_file_without_redirect() -> None:
    # Given: a v2 file written before the field existed
    name = "redir-legacy"
    _write(name, _v2(name, cwd="/tmp/old"))

    # When: it is loaded
    data = session_store.load(name)

    # Then: everything reads as before and redirect defaults to ""
    assert data.cwd == "/tmp/old"
    assert data.redirect_to == ""
    assert session_store.resolve_redirect(name) == (name, [name])


def test_resolve_redirect_without_link_returns_identity() -> None:
    name = "redir-unlinked"
    session_store.create(name)
    assert session_store.resolve_redirect(name) == (name, [name])


def test_resolve_redirect_follows_single_link() -> None:
    a, b = "redir-one", "redir-two"
    session_store.create(a)
    session_store.create(b)
    session_store.set_redirect(a, b)
    assert session_store.resolve_redirect(a) == (b, [a, b])


def test_resolve_redirect_follows_chain_to_final() -> None:
    # Given: A→B→C→D, and every intermediate session exists
    a, b, c, d = "redir-ca", "redir-cb", "redir-cc", "redir-cd"
    for name in (a, b, c, d):
        session_store.create(name)
    session_store.set_redirect(a, b)
    session_store.set_redirect(b, c)
    session_store.set_redirect(c, d)

    # Then: the walk lands on D and reports every hop in order
    assert session_store.resolve_redirect(a) == (d, [a, b, c, d])
    # And mid-chain starts resolve from their own position
    assert session_store.resolve_redirect(b) == (d, [b, c, d])
    # And the convenience wrapper returns just the chain
    assert session_store.redirect_chain(a) == [a, b, c, d]


def test_resolve_redirect_stops_on_cycle() -> None:
    # Given: A→B and B→A — only reachable by hand-editing a file
    a, b = "redir-loop-a", "redir-loop-b"
    _write(a, _v2(a, **{"redirect_to": b}))
    _write(b, _v2(b, **{"redirect_to": a}))

    # Then: the walk stops at the last valid session instead of looping
    assert session_store.resolve_redirect(a) == (b, [a, b])
    assert session_store.resolve_redirect(b) == (a, [b, a])


def test_resolve_redirect_stops_on_hand_edited_self_reference() -> None:
    # Given: a file pointing at itself (set_redirect refuses this, so
    # the defense matters for hand-edited or corrupted files)
    name = "redir-loop-self"
    _write(name, _v2(name, **{"redirect_to": name}))

    # Then: the walk stops immediately
    assert session_store.resolve_redirect(name) == (name, [name])


def test_resolve_redirect_stops_when_successor_missing() -> None:
    # Given: A→B where B was never created
    a = "redir-dangling"
    _write(a, _v2(a, **{"redirect_to": "redir-nowhere"}))

    # Then: the walk stops at the dangling link's target (load of a
    # missing session has no redirect, so nothing follows)
    final, chain = session_store.resolve_redirect(a)
    assert final == "redir-nowhere"
    assert chain == [a, "redir-nowhere"]


def test_resolve_redirect_respects_chain_cap(monkeypatch) -> None:
    # Given: a chain longer than the hard cap
    first = "redir-cap-0"
    for i in range(10):
        _write(f"redir-cap-{i}", _v2(f"redir-cap-{i}", **{"redirect_to": f"redir-cap-{i + 1}"}))
    monkeypatch.setattr(session_store, "REDIRECT_MAX_CHAIN", 5)

    # Then: the walk gives up at the cap instead of running to the end
    final, chain = session_store.resolve_redirect(first)
    assert len(chain) == 5
    assert final == "redir-cap-4"


# ── the triggers: interaction follows the redirect, viewing does not ──────────


def test_continue_session_records_the_redirect(summary_folder) -> None:
    # Given: a real hand-off from an existing session
    summary.set_run_contract(session="redir-tool-from")
    session_store.create("redir-tool-from")
    ack = continue_session(str(summary_folder))
    handoff = summary.pending_handoff()
    assert handoff is not None

    # Then: the summarized session PERSISTS the pointer to its successor —
    # the ack names the same destination the redirect resolves to
    assert session_store.resolve_redirect("redir-tool-from") == (
        handoff.new_session,
        ["redir-tool-from", handoff.new_session],
    )
    assert handoff.new_session in ack
    summary.clear_handoff()
    summary.clear_run_contract()


def test_a_delegated_alarm_lands_in_the_redirected_session() -> None:
    # Given: a summarized receiver — the alarm's target moved on
    session_store.create("redir-alarm-old")
    session_store.create("redir-alarm-new")
    session_store.set_redirect("redir-alarm-old", "redir-alarm-new")
    run = _run(alarm_session="redir-alarm-old")

    # When: the subagent finishes and its alarm is delivered
    _deliver_alarm(run, status="done", answer="RESULT")

    # Then: the message went to the FINAL session's inbox, and the text says
    # the receiver was moved
    old = inbox.drain("redir-alarm-old")
    new = inbox.drain("redir-alarm-new")
    assert old == []
    assert len(new) == 1
    assert "session redir-alarm-old was summarized → continuing in redir-alarm-new" in new[0].text


def test_a_cli_wakeup_follows_the_redirect_to_the_final_session(
    monkeypatch, tmp_path
) -> None:
    # Given: A→B→C, with C holding the queued message and the history
    session_store.begin_turn("redir-cli-a", "earlier")
    session_store.finish_turn("redir-cli-a", assistant="earlier answer")
    session_store.create("redir-cli-b")
    session_store.create("redir-cli-c")
    session_store.set_redirect("redir-cli-a", "redir-cli-b")
    session_store.set_redirect("redir-cli-b", "redir-cli-c")
    inbox.insert("redir-cli-c", "wake up", kind="alarm", source="spawn")
    seen: list[ChatOptions] = []

    def fake_run_chat(opts: ChatOptions, sink: object = None) -> str:
        seen.append(opts)
        return "ANSWER"

    monkeypatch.setattr(chat_cli, "run_chat", fake_run_chat)

    # When: the summarized session is woken
    code, output = _chat("--alarms", "--continue", "--name", "redir-cli-a")

    # Then: the turn ran in the FINAL session, over ITS history, and the
    # operator was told about the whole hop path
    assert code == 0
    assert [o.session for o in seen] == ["redir-cli-c"]
    assert seen[0].continue_session is True
    assert (
        "session redir-cli-a was summarized → continuing in redir-cli-c (via redir-cli-b)"
        in output
    )


def test_a_cli_continue_with_prompt_also_follows(monkeypatch) -> None:
    # Given: a summarized session and a plain `-c` resume with a prompt
    session_store.begin_turn("redir-cli-d", "earlier")
    session_store.finish_turn("redir-cli-d", assistant="earlier answer")
    session_store.create("redir-cli-e")
    session_store.set_redirect("redir-cli-d", "redir-cli-e")
    seen: list[ChatOptions] = []

    def fake_run_chat(opts: ChatOptions, sink: object = None) -> str:
        seen.append(opts)
        return "ANSWER"

    monkeypatch.setattr(chat_cli, "run_chat", fake_run_chat)

    # When: the operator continues the conversation by name
    code, output = _chat("what is left?", "--continue", "--name", "redir-cli-d")

    # Then: the interaction went to the session that continues the work
    assert code == 0
    assert [o.session for o in seen] == ["redir-cli-e"]
    assert "session redir-cli-d was summarized → continuing in redir-cli-e" in output


def test_a_plain_new_chat_does_not_follow_anything(monkeypatch) -> None:
    # Given: a redirected session in the tree
    session_store.create("redir-plain-old")
    session_store.create("redir-plain-new")
    session_store.set_redirect("redir-plain-old", "redir-plain-new")
    seen: list[ChatOptions] = []

    def fake_run_chat(opts: ChatOptions, sink: object = None) -> str:
        seen.append(opts)
        return "ANSWER"

    monkeypatch.setattr(chat_cli, "run_chat", fake_run_chat)

    # When: a fresh chat runs with NO -c and NO --name
    code, output = _chat("do the work")

    # Then: it kept its own minted session and printed no redirect notice
    assert code == 0
    assert len(seen) == 1
    assert seen[0].session != "redir-plain-new"
    assert "was summarized" not in output


def test_a_tui_message_moves_the_conversation_and_keeps_the_input() -> None:
    # Given: a TUI pinned to a summarized session, mid-chain A→B→C
    async def body() -> None:
        session_store.create("redir-tui-a")
        session_store.create("redir-tui-b")
        session_store.create("redir-tui-c")
        session_store.set_redirect("redir-tui-a", "redir-tui-b")
        session_store.set_redirect("redir-tui-b", "redir-tui-c")
        app = ChatTUI()
        app = ChatTUI()
        async with app.run_test(size=(120, 32)) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "redir-tui-a")
            # And: the user typed a message and submitted it
            app.on_chat_input_submitted(ChatInput.Submitted("keep going"))

            # Then: the conversation moved to the FINAL session, the notice is
            # on screen naming the hop, the history was reloaded, and the
            # submitted text survived in the input AND went to dispatch
            assert app._pinned_session() == "redir-tui-c"
            assert app._input.text == "keep going"
            texts = [tui_harness.row_text(r) for r in app._log.rows]
            assert any(
                "session redir-tui-a was summarized → moved to redir-tui-c "
                "(via redir-tui-b). Continue here." in t
                for t in texts
            ), texts
            assert settings_store.load().session == "redir-tui-c"

    asyncio.run(body())


def test_a_tui_session_picker_load_does_not_redirect() -> None:
    # Given: a summarized session in the tree (plain viewing)
    async def body() -> None:
        session_store.create("redir-view-old")
        session_store.create("redir-view-new")
        session_store.set_redirect("redir-view-old", "redir-view-new")
        app = ChatTUI()
        async with app.run_test(size=(120, 32)) as pilot:
            await pilot.pause()
            # When: the session's history is loaded the way the picker does
            app._load_session_history("redir-view-old")
            await pilot.pause()
            # Then: the view shows the OLD transcript — no move, no adoption
            assert app._session_name != "redir-view-new"

    asyncio.run(body())
