"""TUI indicator lifecycle: the animation ticker and the wizard status.

Two user-visible rules are locked here:

1. A spinner must ANIMATE while work is in progress — never sit frozen. The
   ticker therefore belongs to the app (`ChatTUI`), which outlives a
   dispatch, not to the per-dispatch `TUIEventSink`.
2. When work completes and nothing needs showing, the spot shows NOTHING —
   the bottom spinner row goes blank instead of holding a stale frame or a
   frozen glyph.

The background-job spinner that used to live here is gone with the
in-process engine: a `miniouto chat --bg` run is another process, so there
is no row to animate. Its replacement is the inbox poll
(`tests/test_alarm_delivery.py`), and the jump button / row toggles are
covered in `tests/test_tui_navigation.py`.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from textual.widgets import Static

from miniouto.cli.tui import ChatTUI, TUIEventSink


def _rendered(widget: Any) -> str:
    return "\n".join(strip.text.rstrip() for strip in widget._render_cache.lines)


def _bottom_text(app: ChatTUI) -> str:
    assert app._panel is not None
    return _rendered(app._panel.query_one("#spinner-row", Static))


async def _tick(app: ChatTUI, pilot: Any, times: int) -> list[str]:
    """Drive the ticker by hand (the real timer is paused) and collect frames."""

    assert app._spin_timer is not None
    app._spin_timer.pause()
    frames: list[str] = []
    for _ in range(times):
        app._tick_spin()
        await pilot.pause()
        frames.append(_bottom_text(app).split(" ")[0])
    return frames


def _run(body: Any) -> None:
    asyncio.run(body())


def test_the_spinner_animates_while_a_turn_is_in_flight() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()

            # Given: a working chat turn
            app._set_activity("thinking…")
            app._set_chat_working(True)
            await pilot.pause()

            # Then: the ticker is alive and the glyph actually moves
            assert app._spin_timer is not None
            frames = await _tick(app, pilot, 4)
            assert len(set(frames)) > 1
            assert "thinking…" in _bottom_text(app)

    _run(body)


def test_the_ticker_stops_and_the_bottom_row_is_blank_when_nothing_runs() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            app._set_activity("thinking…")
            app._set_chat_working(True)
            await pilot.pause()
            assert app._spin_timer is not None
            app._spin_timer.pause()

            # When: the turn ends
            app._set_chat_working(False)
            app._tick_spin()
            await pilot.pause()

            # Then: no ticker is left running, and the row shows nothing
            assert app._spin_timer is None
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
