"""A completed background job's alarm must reach the model as a user message.

The mid-turn delivery path (`core/runtime.py:_make_alarm_hook`, on
BEFORE_LLM_CALL) only runs while a turn is in flight. A job that finishes
between turns — the common case, because outo ends its turn saying "I'll
wait" — used to leave its alarm sitting in `subagents._PENDING_ALARMS`
until the user typed something: nothing appeared in the chat and the model
never woke up.

`ChatTUI._deliver_pending_alarms` closes that gap. The alarm text becomes
the prompt of a real turn (so `turn.user` is the alarm, the live row is
the alarm, and the model sees the alarm), and a completion observer keeps
the delivery prompt when the job finishes while the TUI is idle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest

from miniouto.cli import tui as tui_module
from miniouto.cli.tui import ChatTUI
from miniouto.core import subagents
from miniouto.core.subagents import ALARM_HEADER, Alarm, Job
from miniouto.storage import sessions as session_store
from miniouto.storage import settings as settings_store


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    subagents.reset()
    yield
    subagents.reset()


def _finished_job(name: str = "editor", sid: str = "abc123") -> Job:
    """A job record the real completion path can retire."""

    return Job(
        sid=sid,
        name=name,
        brief="fix the bug",
        started=datetime.now(),
        generation=subagents._GENERATION,
    )


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


def test_idle_completion_arrives_as_a_user_message_and_starts_a_turn(
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

            # Given: a background job that finished while no turn was running
            job = _finished_job()
            assert not app._busy
            subagents._finish(job, "done", "the editor rewrote three functions")
            expected = subagents.format_completion_alarm(job)
            assert expected.startswith(ALARM_HEADER)

            # When: the app picks the queued alarm up
            app._deliver_pending_alarms()
            await _settle(pilot, want=1, have=prompts)

            # Then: the alarm is a user message, and it IS the turn's prompt
            rows = _prompt_rows(app)
            assert len(rows) == 1
            assert _rendered(rows[0]).lstrip().startswith("> ")
            assert ALARM_HEADER in _rendered(rows[0])
            assert prompts == [expected]
            # And the session records it as the turn's user message
            data = session_store.load("alarm-idle")
            assert [t.user for t in data.turns] == [expected]
            # The queue is drained: the hook or this path delivers, never both
            assert subagents.take_pending_alarms() == []

    asyncio.run(body())


def test_busy_turn_keeps_the_queue_for_the_in_turn_hook() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            app._chat_started = True

            # Given: a turn is in flight and a job completes mid-turn
            app._busy = True
            subagents._finish(_finished_job(), "done", "result")

            # When: the idle delivery path runs anyway
            app._deliver_pending_alarms()
            await pilot.pause()

            # Then: it drained nothing — the BEFORE_LLM_CALL hook owns it
            assert len(subagents.take_pending_alarms()) == 1
            assert _prompt_rows(app) == []

    asyncio.run(body())


def test_dispatch_tail_delivers_an_alarm_the_hook_never_saw(
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

            # Given: a job that finished after the turn's last LLM call, so no
            # BEFORE_LLM_CALL ever ran again
            subagents._finish(_finished_job(), "done", "result")

            # When: a turn runs and ends
            await app._dispatch("what did you find?")
            await _settle(pilot, want=2, have=prompts)

            # Then: the tail catch-up started a second turn with the alarm
            assert len(prompts) == 2
            assert prompts[0] == "what did you find?"
            assert ALARM_HEADER in prompts[1]
            assert len(_prompt_rows(app)) == 1
            data = session_store.load("alarm-tail")
            assert [t.user for t in data.turns] == prompts

    asyncio.run(body())


def test_simultaneous_completions_coalesce_into_one_message_and_one_turn(
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

            # Given: two jobs finishing back to back while the TUI is idle
            subagents._finish(_finished_job("editor", "aaa111"), "done", "one")
            subagents._finish(_finished_job("reviewer", "bbb222"), "done", "two")

            # When: delivery runs once
            app._deliver_pending_alarms()
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


def test_completion_observer_is_installed_on_mount_and_cleared_on_unmount() -> None:
    async def body() -> None:
        app = ChatTUI()
        # Given: no observer installed
        assert subagents._COMPLETION_OBSERVER is None

        async with app.run_test(size=(80, 24)) as pilot:
            # When: the app mounts
            await pilot.pause()
            # Then: it listens for completions
            assert subagents._COMPLETION_OBSERVER is not None

        # And when the app is gone, the listener is gone with it
        assert subagents._COMPLETION_OBSERVER is None

    asyncio.run(body())


def test_set_completion_observer_fires_once_per_completed_job() -> None:
    # Given: a completion observer
    seen: list[Alarm] = []
    subagents.set_completion_observer(seen.append)
    job = _finished_job()

    # When: one job completes
    subagents._finish(job, "done", "the result")
    # Then: exactly one alarm, carrying the job's identity
    assert len(seen) == 1
    assert seen[0] is subagents.take_pending_alarms()[0]
    assert seen[0].sid == job.sid
    assert seen[0].name == "editor"

    # And the idempotent second pass notifies nothing
    subagents._finish(job, "done", "the result")
    assert len(seen) == 1


def test_reset_clears_the_completion_observer() -> None:
    # Given: an installed observer
    seen: list[Alarm] = []
    subagents.set_completion_observer(seen.append)

    # When: the registry is reset
    subagents.reset()

    # Then: a later completion notifies nobody
    subagents._finish(_finished_job(), "done", "result")
    assert seen == []
    assert subagents._COMPLETION_OBSERVER is None
