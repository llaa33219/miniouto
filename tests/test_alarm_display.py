"""Alarm events are recorded AND displayed: the session file and the log match.

A `kind="alarm"` LoopEvent is persisted in the turn record
(`core/chat.py:_make_alarm_dispatcher`), so the TUI must render it too —
otherwise a reloaded session shows rows the live run never showed, and the
visible log disagrees with the file. Two sources emit alarms:

- a background subagent job finishing (carries `subagent_id` /
  `subagent_name`) — v1 has no `subagent_end`, so the alarm is the ONLY
  thing that closes the spinning `SubagentRow`;
- the repetition watchdog (`actor="watchdog"`, no `subagent_id`).

The alarm text is a *pointer* to the result file, not the result, so the
unbounded result box is mounted for `subagent_end` but never for an alarm.
Live and replay must route identically, or the same session looks different
depending on when it is opened.
"""

from __future__ import annotations

import asyncio
from typing import Any

from miniouto.cli.tui import (
    ChatTUI,
    EventRow,
    SubagentRow,
    ToolRow,
    TUIEventSink,
)
from miniouto.core.events import LoopEvent
from miniouto.core.subagents import ALARM_HEADER
from miniouto.storage import sessions as session_store

ALARM_TEXT = (
    f"{ALARM_HEADER}\nSubagent `editor-abc123` finished.\n"
    "Result: ~/.miniouto/subagent-results/abc123-2026-10-04-01-02.md"
)
SID = "abc123"


def _rendered(row: Any) -> str:
    strips = row._render_cache.lines
    return "\n".join(strip.text.rstrip() for strip in strips)


def _plain_rows(app: ChatTUI) -> list[Any]:
    """EventRows that are not ToolRows (ToolRow subclasses EventRow)."""

    return [r for r in app._window.rows if isinstance(r, EventRow) and not isinstance(r, ToolRow)]


def _result_boxes(app: ChatTUI) -> list[ToolRow]:
    return [r for r in app._window.rows if isinstance(r, ToolRow)]


def _start_subagent(app: ChatTUI, sink: TUIEventSink) -> SubagentRow:
    sink._post_loop_event(
        LoopEvent(
            actor="outo",
            kind="subagent_start",
            text="edit the file",
            subagent_id=SID,
            subagent_name="editor",
        )
    )
    return app._subagent_rows[SID]


def test_live_alarm_renders_in_the_main_chat_and_stops_its_subagent_row() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            sink = TUIEventSink(app)

            # Given: a running background job, showing as a spinning row
            row = _start_subagent(app, sink)
            await pilot.pause()
            assert row.running
            assert len(app._window.rows) == 1

            # When: its completion alarm arrives
            sink._post_loop_event(
                LoopEvent(
                    actor="outo",
                    kind="alarm",
                    text=ALARM_TEXT,
                    subagent_id=SID,
                    subagent_name="editor",
                )
            )
            await pilot.pause()

            # Then: the spinner stopped and the alarm is visible in main chat
            assert not app._subagent_rows[SID].running
            assert "✓" in _rendered(app._subagent_rows[SID])
            rows = _plain_rows(app)
            assert len(rows) == 1
            assert "SYSTEM ALARM" in _rendered(rows[0])
            # It carries its own style so it reads as a system notice, not
            # ordinary tool noise.
            assert rows[0].has_class("-alarm")
            # And the alarm is a pointer, not a result: no result box for it
            assert _result_boxes(app) == []

    asyncio.run(body())


def test_watchdog_alarm_without_a_subagent_row_still_renders() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            sink = TUIEventSink(app)

            # When: a repetition alarm fires (no job, no row to finish)
            sink._post_loop_event(
                LoopEvent(
                    actor="watchdog",
                    kind="alarm",
                    text=f"{ALARM_HEADER}\nYou are looping; restart the attempt.",
                )
            )
            await pilot.pause()

            # Then: it renders as a main-chat row instead of crashing
            rows = _plain_rows(app)
            assert len(rows) == 1
            assert "looping" in _rendered(rows[0])

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
                    kind="subagent_start",
                    text="edit the file",
                    subagent_id=SID,
                    subagent_name="editor",
                ).to_dict(),
                LoopEvent(
                    actor="outo",
                    kind="alarm",
                    text=ALARM_TEXT,
                    subagent_id=SID,
                    subagent_name="editor",
                ).to_dict(),
            ],
        )
        session_store.finish_turn(name, assistant="done")

        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            # When: the session is reloaded from disk
            app._load_session_history(name)
            await pilot.pause()

            # Then: the alarm shows in the main chat, the row is finished,
            # and the alarm's pointer text is not mounted as a result box.
            subagent_rows = [r for r in app._window.rows if isinstance(r, SubagentRow)]
            assert len(subagent_rows) == 1
            assert not subagent_rows[0].running
            rows = _plain_rows(app)
            assert len(rows) == 1
            assert "SYSTEM ALARM" in _rendered(rows[0])
            assert _result_boxes(app) == []

    asyncio.run(body())


def test_non_alarm_subagent_events_stay_out_of_the_main_chat() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            sink = TUIEventSink(app)
            _start_subagent(app, sink)
            await pilot.pause()

            # When: the job's own internals stream through
            sink._post_loop_event(
                LoopEvent(
                    actor="editor",
                    kind="tool",
                    text="Bash(ls)",
                    tool_name="Bash",
                    subagent_id=SID,
                )
            )
            sink._post_loop_event(
                LoopEvent(actor="editor", kind="thinking", text="hmm", subagent_id=SID)
            )
            await pilot.pause()

            # Then: they belong to the detail screen only, and the row is
            # still running (only an alarm closes it).
            assert len(app._window.rows) == 1
            assert app._subagent_rows[SID].running
            assert [e.kind for e in app._subagent_events[SID]] == [
                "subagent_start",
                "tool",
                "thinking",
            ]

    asyncio.run(body())


def test_mount_result_flag_decides_whether_the_result_box_is_mounted() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            sink = TUIEventSink(app)
            _start_subagent(app, sink)
            await pilot.pause()

            # When: the same finishing event is routed with the flag off
            app._finish_subagent_row(
                LoopEvent(
                    actor="outo",
                    kind="alarm",
                    text=ALARM_TEXT,
                    subagent_id=SID,
                    subagent_name="editor",
                ),
                mount_result=False,
            )
            await pilot.pause()

            # Then: no result box, and the row still finished
            assert _result_boxes(app) == []
            assert not app._subagent_rows[SID].running

            # And the default (subagent_end replay) still mounts one, so the
            # flag is what makes the difference — not the event's emptiness.
            app._finish_subagent_row(
                LoopEvent(
                    actor="outo",
                    kind="subagent_end",
                    text="the full unbounded subagent answer",
                    subagent_id=SID,
                    subagent_name="editor",
                )
            )
            await pilot.pause()
            boxes = _result_boxes(app)
            assert len(boxes) == 1
            assert boxes[0]._result == "the full unbounded subagent answer"

    asyncio.run(body())
