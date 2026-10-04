"""TUI indicator lifecycle: the animation ticker, job completion, wizard status.

Two user-visible rules are locked here:

1. A spinner must ANIMATE while work is in progress — never sit frozen. The
   ticker therefore belongs to the app (`ChatTUI`), which outlives a dispatch,
   not to the per-dispatch `TUIEventSink` (whose timer died at `end_working`
   and left every live `SubagentRow` frozen mid-turn).
2. When work completes and nothing needs showing, the spot shows NOTHING —
   the bottom spinner row goes blank instead of holding a stale frame or a
   frozen glyph.

Plus: a background job that finishes between turns must flip its row to
✓/✗ immediately (reconciled against `core.subagents.job_status`) instead of
spinning until the next send, and a session-replay row (an sid the registry
never saw) must be left completely alone.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from textual.widgets import Static

from miniouto.cli import tui as tui_module
from miniouto.cli.tui import ChatTUI, SubagentRow, TUIEventSink
from miniouto.core import subagents
from miniouto.core.events import LoopEvent

SID = "abc123"


def _rendered(widget: Any) -> str:
    return "\n".join(strip.text.rstrip() for strip in widget._render_cache.lines)


def _bottom_text(app: ChatTUI) -> str:
    assert app._panel is not None
    return _rendered(app._panel.query_one("#spinner-row", Static))


def _start_subagent(app: ChatTUI) -> SubagentRow:
    app._add_subagent_row(
        LoopEvent(
            actor="outo",
            kind="subagent_start",
            text="edit the file",
            subagent_id=SID,
            subagent_name="editor",
        )
    )
    return app._subagent_rows[SID]


def _alarm(app: ChatTUI, text: str = "finished") -> None:
    app._finish_subagent_row(
        LoopEvent(
            actor="outo",
            kind="alarm",
            text=text,
            subagent_id=SID,
            subagent_name="editor",
        ),
        mount_result=False,
    )


def _tick(app: ChatTUI, times: int) -> list[str]:
    """Drive the ticker by hand (the real timer is paused) and collect frames."""

    assert app._spin_timer is not None
    app._spin_timer.pause()
    frames: list[str] = []
    for _ in range(times):
        app._tick_spin()
        frames.append(app._subagent_rows[SID]._frame)
    return frames


def _run(body: Any) -> None:
    asyncio.run(body())


def test_a_running_subagent_row_keeps_animating_after_the_turn_ends() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            row = _start_subagent(app)

            # Given: a working chat turn
            app._set_chat_working(True)
            await pilot.pause()
            during = _tick(app, 4)
            assert len(set(during)) > 1

            # When: the turn ends (`end_working`) while the job is still running
            app._set_chat_working(False)
            await pilot.pause()

            # Then: the ticker is still alive and the row still animates
            assert app._spin_timer is not None
            after = _tick(app, 4)
            assert len(set(after)) > 1
            assert set(after) != set(during)
            assert row.running

    _run(body)


def _job_left_running(monkeypatch: Any, status: str, glyph: str) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            app._job_reconcile_interval = 0.0
            row = _start_subagent(app)
            app._set_chat_working(True)
            await pilot.pause()
            assert app._spin_timer is not None
            app._spin_timer.pause()
            monkeypatch.setattr(
                tui_module.subagents, "job_status", lambda _sid: status, raising=True
            )

            # When: a tick runs after the job left the running set
            app._tick_spin()
            await pilot.pause()

            # Then: the row closed itself — no alarm event was emitted
            assert not row.running
            assert glyph in _rendered(row)

    _run(body)


def test_a_finished_job_flips_its_row_ok_without_an_alarm(monkeypatch: Any) -> None:
    _job_left_running(monkeypatch, "done", "✓")


def test_a_failed_job_flips_its_row_error_without_an_alarm(monkeypatch: Any) -> None:
    _job_left_running(monkeypatch, "error", "✗")


def test_a_replay_row_whose_sid_was_never_registered_is_untouched() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            app._job_reconcile_interval = 0.0
            row = _start_subagent(app)
            app._set_chat_working(True)
            await pilot.pause()

            # Given: the registry has never heard of this sid
            assert subagents.job_status(SID) is None

            # When: ticks keep running
            _tick(app, 4)
            await pilot.pause()

            # Then: the row is left exactly as it was
            assert row.running
            assert row._frame in tui_module._SPINNER_FRAMES

    _run(body)


def test_a_replayed_row_can_still_be_closed_by_its_recorded_result() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            # Given: a row built not-running, the way `_replay_event` does
            row = SubagentRow(SID, "edit the file", running=False)
            app._subagent_rows[SID] = row
            app._mount_row(row)
            await pilot.pause()

            # When: the recorded subagent_end replays as a failure
            app._finish_subagent_row(
                LoopEvent(
                    actor="editor",
                    kind="subagent_end",
                    text="error: it broke",
                    subagent_id=SID,
                    subagent_name="editor",
                )
            )
            await pilot.pause()

            # Then: idempotency does not swallow a row's FIRST verdict
            assert "✗" in _rendered(row)

    _run(body)


def test_finishing_a_row_twice_is_harmless() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            row = _start_subagent(app)

            # Given: the poll finished the row, then a late alarm arrives
            row.finish(ok=True)
            await pilot.pause()
            first = _rendered(row)
            _alarm(app, "error: late alarm text")
            await pilot.pause()

            # Then: the alarm cannot flip the glyph back
            assert not row.running
            assert _rendered(row) == first

    _run(body)


def test_the_ticker_stops_and_the_bottom_row_is_blank_when_nothing_runs() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            _start_subagent(app)
            app._set_chat_working(True)
            await pilot.pause()
            assert app._spin_timer is not None
            app._spin_timer.pause()

            # When: the job finishes and the turn ends
            _alarm(app)
            app._set_chat_working(False)
            app._tick_spin()
            await pilot.pause()

            # Then: no ticker is left running, and the row shows nothing
            assert app._spin_timer is None
            assert _bottom_text(app) == ""

    _run(body)


def test_the_bottom_row_is_blank_while_only_a_background_job_runs() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            _start_subagent(app)
            app._set_chat_working(True)
            await pilot.pause()
            assert app._spin_timer is not None
            app._spin_timer.pause()

            # When: the turn ends but the job keeps running
            app._set_chat_working(False)
            app._tick_spin()
            await pilot.pause()

            # Then: the row still animates, the bottom row says nothing
            assert app._spin_timer is not None
            assert _bottom_text(app) == ""

    _run(body)


def test_a_wizard_working_status_animates_and_a_notice_has_no_glyph() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()

            # When: a wizard fetch is in flight
            app._spinner_status("fetching catalog…", working=True)
            await pilot.pause()

            # Then: the shared ticker animates it and the text is visible
            assert app._spin_timer is not None
            app._spin_timer.pause()
            glyphs: list[str] = []
            for _ in range(4):
                app._tick_spin()
                await pilot.pause()
                glyphs.append(_bottom_text(app).split(" ")[0])
            assert "fetching catalog…" in _bottom_text(app)
            assert len(set(glyphs)) > 1

            # And: a notice is plain text — no frozen glyph
            app._spinner_status("provider → openai")
            await pilot.pause()
            assert "⠋" not in _bottom_text(app)
            assert "provider → openai" in _bottom_text(app)

            # And: clearing leaves nothing behind
            app._clear_spinner_status()
            await pilot.pause()
            assert _bottom_text(app) == ""
            assert app._spin_timer is None

    _run(body)


def test_the_sink_drivers_change_app_state_across_the_thread_hop() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            sink = TUIEventSink(app)

            # Given/When: the worker-thread drivers the real chat loop uses
            def start() -> None:
                sink.update_activity("thinking…")
                sink.begin_working()

            thread = threading.Thread(target=start)
            thread.start()
            await pilot.pause()
            thread.join()

            # Then: the app owns the state and the ticker is running
            assert app._chat_working
            assert app._activity == "thinking…"
            assert app._spin_timer is not None

            def stop() -> None:
                sink.end_working()

            thread = threading.Thread(target=stop)
            thread.start()
            await pilot.pause()
            thread.join()

            # And: end_working still blanks the bottom row
            assert not app._chat_working
            assert _bottom_text(app) == ""

    _run(body)


class _GatedAgent:
    """Stands in for `coreouto.Agent`: a `call()` that never returns on its own."""

    def __init__(self) -> None:
        self.gate = threading.Event()

    async def call(
        self, user_message: str | None = None, *, history: Any = None
    ) -> Any:
        while not self.gate.is_set():
            await asyncio.sleep(0.005)
        raise AssertionError("not reached")


def test_job_status_reports_live_jobs_and_ignores_unknown_sids() -> None:
    agent = _GatedAgent()
    subagents.reset()
    job = subagents.submit(agent, "editor", "do the thing")
    try:
        # Given: a submitted job
        assert subagents.job_status(job.sid) == "running"
        assert subagents.is_running(job.sid)

        # Then: an unknown sid has no status at all
        assert subagents.job_status("ffffff") is None
    finally:
        agent.gate.set()
        subagents.cancel_job(job.sid, "test cleanup")
        subagents.reset()
