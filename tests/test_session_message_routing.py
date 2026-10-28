"""Session identity, not the published active session, routes every inbox."""

from __future__ import annotations

import asyncio
from typing import Any

import coreouto as co
import pytest
from typer.testing import CliRunner

import harness
from miniouto.cli import app as cli_app
from miniouto.cli import chat as chat_cli
from miniouto.cli.tui import ChatTUI
from miniouto.core import chat as chat_mod
from miniouto.core import inbox
from miniouto.core.chat import ChatOptions
from miniouto.storage import settings as settings_store


def _chat(*args: str) -> int:
    with CliRunner().isolation():
        code = 0
        try:
            cli_app(["chat", *args])
        except SystemExit as exit_signal:
            code = exit_signal.code if isinstance(exit_signal.code, int) else 1
    return code


def test_a_turn_keeps_its_own_inbox_when_the_active_session_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later TUI/CLI process cannot retarget a turn already bound to a session."""

    owner = "e2e-routing-owner"
    other = "e2e-routing-other"

    async def answer(_request: harness.Request) -> co.LLMResponse:
        return co.LLMResponse(content="done")

    provider = harness.install(
        monkeypatch,
        answer,
        session=owner,
        style=f"<outo>SESSION={{this_session}}</outo>\n{harness.STYLE.splitlines()[1]}",
    )
    # Simulate another process publishing its session after this turn was named.
    settings_store.update(session=other)
    inbox.insert(owner, "FOR-OWNER", kind=inbox.USER_KIND, source="test")
    inbox.insert(other, "FOR-OTHER", kind=inbox.USER_KIND, source="test")

    chat_mod.run_chat(ChatOptions(prompt="continue", session=owner))

    request = provider.requests[0]
    assert request.messages[0].content == f"SESSION={owner}"
    assert request.user_texts() == ["continue\n\nFOR-OWNER"]
    assert inbox.drain(other)[0].text == "FOR-OTHER"
    assert inbox.pending_count(owner) == 0


def test_a_guest_turn_never_republishes_the_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guest routing must not require a temporary write to shared settings."""

    harness.install(
        monkeypatch,
        lambda _request: co.LLMResponse(content="unused"),
        session="e2e-routing-boss",
    )
    observed: list[str] = []

    def turn(opts: ChatOptions, sink: Any = None) -> str:
        observed.append(settings_store.load().session)
        return "guest answer"

    monkeypatch.setattr(chat_cli, "run_chat", turn)
    code = _chat(
        "brief",
        "--name", "e2e-routing-guest",
        "--style", harness.STYLE_NAME,
        "--role", "editor",
        "--alarm-session", "e2e-routing-boss",
    )

    assert code == 0
    assert observed == ["e2e-routing-boss"]
    assert settings_store.load().session == "e2e-routing-boss"
    assert inbox.pending_count("e2e-routing-boss") == 1


def test_a_guest_cannot_continue_an_ambient_caller_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guest must name the history it continues, never inherit its caller's."""

    boss = "e2e-routing-ambient-boss"
    harness.install(
        monkeypatch,
        lambda _request: co.LLMResponse(content="unused"),
        session=boss,
    )
    inbox.insert(boss, "CALLER-PRIVATE", kind=inbox.USER_KIND, source="test")
    calls: list[ChatOptions] = []
    monkeypatch.setattr(chat_cli, "run_chat", lambda opts, sink=None: calls.append(opts) or "ok")

    code = _chat(
        "brief", "--continue", "--style", harness.STYLE_NAME,
        "--role", "editor", "--alarm-session", boss,
    )

    assert code == 1
    assert calls == []
    assert inbox.pending_count(boss) == 1
    assert settings_store.load().session == boss


def test_each_unassigned_tui_has_a_private_pending_inbox() -> None:
    """Two TUIs that have not sent a prompt must not poll a shared `default` inbox."""

    async def body() -> None:
        first = ChatTUI()
        second = ChatTUI()
        async with first.run_test(size=(100, 30)):
            first_inbox = first._inbox_session()
        async with second.run_test(size=(100, 30)):
            second_inbox = second._inbox_session()

        assert first_inbox != second_inbox
        inbox.insert(first_inbox, "FOR-FIRST", kind=inbox.USER_KIND, source="test")
        second._deliver_pending_messages()
        assert inbox.pending_count(first_inbox) == 1
        assert inbox.pending_count(second_inbox) == 0

    asyncio.run(body())


def test_explicit_message_insert_is_independent_of_the_active_session() -> None:
    target = "message-routing-target"
    settings_store.update(session="message-routing-active")

    result = CliRunner().invoke(
        cli_app,
        ["message", "insert", "FOR-TARGET", "--session", target, "--kind", "alarm"],
    )

    assert result.exit_code == 0
    assert inbox.pending_count(target) == 1
    assert inbox.pending_count("message-routing-active") == 0
