"""A queued inbox message must reach the model as a user message.

The mid-turn delivery path (`core/runtime.py:_make_alarm_hook`, on
BEFORE_LLM_CALL) only runs while a turn is in flight. A message that lands
between turns — the common case, because outo ends its turn saying "I'll
wait" — would otherwise sit invisible in `~/.miniouto/inbox/<session>/`
until the user typed something: nothing appeared in the chat and the model
never woke up.

`ChatTUI._deliver_pending_messages` closes that gap, and it is now a poll
(`set_interval(_INBOX_POLL_INTERVAL, …)`) because the writer is another
process that may finish at any moment — no registry callback pushes it any
more. The drained text becomes the prompt of a real turn, so `turn.user`,
the live row and the model's own view are the same string by construction.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from miniouto.cli import tui as tui_module
from miniouto.cli.tui import ChatTUI
from miniouto.core import inbox
from miniouto.core.inbox import ALARM_HEADER
from miniouto.storage import sessions as session_store
from miniouto.storage import settings as settings_store

ALARM_TEXT = (
    f"{ALARM_HEADER}\nSpawned run 'editor' (id: abc123) finished (status: done).\n"
    "Result file: ~/.miniouto/subagent-results/abc123-2026-10-04-01-02.md"
)


@pytest.fixture(autouse=True)
def _empty_inboxes() -> Any:
    """The inbox is on-disk state in a shared test home: clear it per test."""

    yield
    for directory in inbox.INBOX_DIR.glob("*"):
        if directory.is_dir():
            for message in directory.iterdir():
                message.unlink(missing_ok=True)


def _deliver(text: str, session: str, *, kind: str = inbox.ALARM_KIND) -> str:
    """Post one message, the way a spawned process would."""

    return inbox.insert(session, text, kind=kind, source="spawn")


def _prompt_rows(app: ChatTUI) -> list[Any]:
    """User-message rows — the `> ` prefix `_post_user` renders."""

    return [r for r in app._window.rows if _rendered(r).lstrip().startswith("> ")]


def _rendered(row: Any) -> str:
    return "\n".join(strip.text.rstrip() for strip in row._render_cache.lines)


def _assign_session(app: ChatTUI, name: str) -> None:
    session_store.create(name)
    settings_store.update(session=name)
    app._chat_started = True
    app._session_assigned = True


def _stub_run_chat(monkeypatch: pytest.MonkeyPatch, prompts: list[str]) -> None:
    """Stand in for the turn driver: record the prompt, persist the turn.

    `run_chat` is what writes the turn record, so the stub does the same
    two calls with the same `ChatOptions` fields the TUI passed — a turn's
    `user` is then provably the prompt the TUI handed over.
    """

    def fake_run_chat(opts: Any, sink: Any = None) -> str:
        prompts.append(opts.prompt)
        session_store.begin_turn(opts.session, opts.prompt)
        session_store.finish_turn(opts.session, assistant="")
        return ""

    monkeypatch.setattr(tui_module, "run_chat", fake_run_chat)


async def _settle(pilot: Any, *, want: int, have: list[str]) -> None:
    """Let the app pump until `want` turns have been dispatched."""

    for _ in range(200):
        if len(have) >= want:
            return
        await pilot.pause(0.01)
    raise AssertionError(f"expected {want} dispatch(es), got {have}")


def test_idle_message_arrives_as_a_user_message_and_starts_a_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    async def body() -> None:
        _stub_run_chat(monkeypatch, prompts)
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            _assign_session(app, "alarm-idle")

            # Given: a background run that finished while no turn was running
            assert not app._busy
            _deliver(ALARM_TEXT, "alarm-idle")
            assert inbox.pending_count("alarm-idle") == 1

            # When: the idle delivery path picks the message up
            app._deliver_pending_messages()
            await _settle(pilot, want=1, have=prompts)

            # Then: the message is a user message, and it IS the turn's prompt
            rows = _prompt_rows(app)
            assert len(rows) == 1
            assert _rendered(rows[0]).lstrip().startswith("> ")
            assert ALARM_HEADER in _rendered(rows[0])
            assert prompts == [ALARM_TEXT]
            # And the session records it as the turn's user message
            data = session_store.load("alarm-idle")
            assert [t.user for t in data.turns] == [ALARM_TEXT]
            # The inbox is drained: the hook or this path delivers, never both
            assert inbox.pending_count("alarm-idle") == 0

    asyncio.run(body())


def test_busy_turn_keeps_the_inbox_for_the_in_turn_hook() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            _assign_session(app, "alarm-busy")
            app._chat_started = True
            _deliver(ALARM_TEXT, "alarm-busy")

            # Given: a turn is in flight and a run finishes mid-turn
            app._busy = True

            # When: the idle delivery path runs anyway
            app._deliver_pending_messages()
            await pilot.pause()

            # Then: it drained nothing — the BEFORE_LLM_CALL hook owns it
            assert inbox.pending_count("alarm-busy") == 1
            assert _prompt_rows(app) == []

    asyncio.run(body())


def test_dispatch_tail_delivers_a_message_the_hook_never_saw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    async def body() -> None:
        _stub_run_chat(monkeypatch, prompts)
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            _assign_session(app, "alarm-tail")

            # Given: a message that landed after the turn's last LLM call, so
            # no BEFORE_LLM_CALL ever ran again
            _deliver(ALARM_TEXT, "alarm-tail")

            # When: a turn runs and ends
            await app._dispatch("what did you find?")
            await _settle(pilot, want=2, have=prompts)

            # Then: the tail catch-up started a second turn with the message
            assert len(prompts) == 2
            assert prompts[0] == "what did you find?"
            assert ALARM_HEADER in prompts[1]
            assert len(_prompt_rows(app)) == 1
            data = session_store.load("alarm-tail")
            assert [t.user for t in data.turns] == prompts

    asyncio.run(body())


def test_simultaneous_messages_coalesce_into_one_prompt_and_one_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    async def body() -> None:
        _stub_run_chat(monkeypatch, prompts)
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            _assign_session(app, "alarm-coalesce")

            # Given: two runs finishing back to back while the TUI is idle
            _deliver(ALARM_TEXT.replace("editor", "editor"), "alarm-coalesce")
            _deliver(ALARM_TEXT.replace("editor", "reviewer"), "alarm-coalesce")

            # When: delivery runs once
            app._deliver_pending_messages()
            await _settle(pilot, want=1, have=prompts)

            # Then: one user row carrying both, and one turn
            rows = _prompt_rows(app)
            assert len(rows) == 1
            rendered = _rendered(rows[0])
            assert "editor" in rendered
            assert "reviewer" in rendered
            assert len(prompts) == 1
            assert "editor" in prompts[0] and "reviewer" in prompts[0]
            data = session_store.load("alarm-coalesce")
            assert len(data.turns) == 1

    asyncio.run(body())


def test_the_idle_poll_picks_a_late_message_up_without_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    async def body() -> None:
        _stub_run_chat(monkeypatch, prompts)
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            _assign_session(app, "alarm-poll")

            # Given: a run that finishes later, with the writer being another
            # process — nothing can notify this one, so only the poll can
            # deliver. The test's own `_deliver_pending_messages` is never
            # called below.
            _deliver(ALARM_TEXT, "alarm-poll")

            # When: the scheduled poll fires on its own
            await _settle(pilot, want=1, have=prompts)

            # Then: the message became the turn's prompt without help
            assert prompts == [ALARM_TEXT]
            assert inbox.pending_count("alarm-poll") == 0

    asyncio.run(body())


def test_a_plain_user_kind_message_is_delivered_without_a_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    async def body() -> None:
        _stub_run_chat(monkeypatch, prompts)
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            _assign_session(app, "inbox-user")

            # Given: a `miniouto message insert` from another run — a plain
            # user message, with no alarm banner of its own
            _deliver("proceed with plan B", "inbox-user", kind=inbox.USER_KIND)

            # When: the idle delivery path runs
            app._deliver_pending_messages()
            await _settle(pilot, want=1, have=prompts)

            # Then: it rides verbatim, banner or not
            assert prompts == ["proceed with plan B"]
            assert ALARM_HEADER not in prompts[0]
            assert inbox.pending_count("inbox-user") == 0

    asyncio.run(body())
