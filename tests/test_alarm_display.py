"""Alarm events are recorded AND displayed: the session file and the log match.

A message drained from the inbox becomes a `kind="alarm"` (or `kind="user"`)
LoopEvent in the turn record (`core/chat.py:_make_alarm_dispatcher`), so the
TUI must render it too — otherwise a reloaded session shows rows the live run
never showed, and the visible log disagrees with the file. Two sources emit
alarms: a spawned run finishing (carrying its `inbox:<id>` detail) and the
repetition watchdog (`actor="watchdog"`, no detail).

An alarm REACHES THE MODEL AS A USER MESSAGE (the core hook appends it to the
live history as `role="user"`; the idle path makes it the whole turn's prompt),
so it renders the way a user message renders — the `> ` prompt prefix — plus
the `-alarm` class that keeps it identifiable as a system notice. Live and
replay must agree, or the same session looks different depending on when it is
opened.

The `detail="inbox:<id>"` handle is the de-duplication seam: text the user
typed while a turn was running is rendered optimistically at the input, and
the real event must NOT add a second row for it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from miniouto.cli.tui import (
    ChatTUI,
    EventRow,
    ToolRow,
    TUIEventSink,
)
from miniouto.core import inbox
from miniouto.core.events import LoopEvent
from miniouto.core.inbox import ALARM_HEADER
from miniouto.storage import sessions as session_store
from miniouto.storage import settings as settings_store

ALARM_TEXT = (
    f"{ALARM_HEADER}\nSpawned run 'editor' (id: abc123) finished.\n"
    "Result: ~/.miniouto/subagent-results/abc123-2026-10-04-01-02.md"
)
MESSAGE_ID = "20260101-000000-abc123"


def _rendered(row: Any) -> str:
    strips = row._render_cache.lines
    return "\n".join(strip.text.rstrip() for strip in strips)


def _plain_rows(app: ChatTUI) -> list[Any]:
    """EventRows that are not ToolRows (ToolRow subclasses EventRow)."""

    return [r for r in app._window.rows if isinstance(r, EventRow) and not isinstance(r, ToolRow)]


def _alarm_rows(app: ChatTUI) -> list[Any]:
    return [r for r in app._window.rows if r.has_class("-alarm")]


def _result_boxes(app: ChatTUI) -> list[ToolRow]:
    return [r for r in app._window.rows if isinstance(r, ToolRow)]


def _inbox_alarm(app: ChatTUI, sink: TUIEventSink, text: str = ALARM_TEXT) -> None:
    """Deliver one message through the real sink, the way a drained one arrives."""

    sink._post_loop_event(
        LoopEvent(actor="outo", kind="alarm", text=text, detail=f"inbox:{MESSAGE_ID}")
    )


def test_a_live_alarm_renders_in_the_main_chat_as_a_user_row() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            sink = TUIEventSink(app)

            # When: a spawned run's completion alarm arrives
            _inbox_alarm(app, sink)
            await pilot.pause()

            # Then: it is visible in the main chat
            rows = _alarm_rows(app)
            assert len(rows) == 1
            assert "SYSTEM ALARM" in _rendered(rows[0])
            # The model receives it as a user message, so it renders like one
            # — the `> ` prompt prefix, not the muted EventRow treatment.
            assert _rendered(rows[0]).lstrip().startswith("> ")
            assert _plain_rows(app) == []
            # And the alarm is a pointer, not a result: no result box for it
            assert _result_boxes(app) == []

    asyncio.run(body())


def test_watchdog_alarm_without_an_inbox_detail_still_renders() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            sink = TUIEventSink(app)

            # When: a repetition alarm fires (no run, no row to finish)
            sink._post_loop_event(
                LoopEvent(
                    actor="watchdog",
                    kind="alarm",
                    text=f"{ALARM_HEADER}\nYou are looping; restart the attempt.",
                )
            )
            await pilot.pause()

            # Then: it renders as a main-chat user row instead of crashing
            rows = _alarm_rows(app)
            assert len(rows) == 1
            assert "looping" in _rendered(rows[0])
            assert _rendered(rows[0]).lstrip().startswith("> ")

    asyncio.run(body())


def test_an_inbox_user_kind_event_renders_as_a_plain_user_row() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            sink = TUIEventSink(app)

            # When: a `miniouto message insert` message is drained mid-turn
            sink._post_loop_event(
                LoopEvent(
                    actor="outo",
                    kind=inbox.USER_KIND,
                    text="proceed with plan B",
                    detail=f"inbox:{MESSAGE_ID}",
                )
            )
            await pilot.pause()

            # Then: it shows as the user message the model receives, and it is
            # NOT dressed as a system notice — the `-alarm` class is the only
            # thing separating the two, so an unmarked user row means the
            # distinction survived.
            rows = [r for r in app._window.rows if _rendered(r).lstrip().startswith("> ")]
            assert len(rows) == 1
            assert "proceed with plan B" in _rendered(rows[0])
            assert _alarm_rows(app) == []

    asyncio.run(body())


def test_an_optimistic_user_row_suppresses_the_event_that_replaces_it() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            session_store.create("alarm-optimistic")
            settings_store.update(session="alarm-optimistic")
            app._chat_started = True
            sink = TUIEventSink(app)

            # Given: text typed while a turn was running, shown at once
            app._busy = True
            app._queue_inbox_message("actually, do plan B")
            await pilot.pause()
            shown = [r for r in app._window.rows if _rendered(r).lstrip().startswith("> ")]
            assert len(shown) == 1
            message_id = next(iter(app._optimistic_rows))

            # When: the core drains it and the real event arrives
            app._busy = False
            sink._post_loop_event(
                LoopEvent(
                    actor="outo",
                    kind=inbox.USER_KIND,
                    text="actually, do plan B",
                    detail=f"inbox:{message_id}",
                )
            )
            await pilot.pause()

            # Then: the same text is shown exactly once, not twice
            shown = [r for r in app._window.rows if _rendered(r).lstrip().startswith("> ")]
            assert len(shown) == 1
            assert message_id not in app._optimistic_rows

    asyncio.run(body())


def test_replayed_alarm_renders_exactly_like_the_live_one() -> None:
    async def body() -> None:
        name = "alarm-replay"
        session_store.create(name)
        session_store.begin_turn(name, "fix the bug")
        session_store.update_turn_events(
            name,
            [
                LoopEvent(
                    actor="outo",
                    kind="alarm",
                    text=ALARM_TEXT,
                    detail=f"inbox:{MESSAGE_ID}",
                ).to_dict(),
            ],
        )
        session_store.finish_turn(name, assistant="done")

        live = ChatTUI()
        replay = ChatTUI()
        async with live.run_test(size=(80, 24)) as pilot, replay.run_test(
            size=(80, 24)
        ) as p2:
            live._log.remove_children()
            live._reset_rows()
            await pilot.pause()
            live._post_alarm_row(ALARM_TEXT)
            await pilot.pause()

            replay._load_session_history(name)
            await p2.pause()

            # Then: the same alarm reads identically either way
            assert _rendered(_alarm_rows(live)[0]) == _rendered(_alarm_rows(replay)[0])

    asyncio.run(body())
