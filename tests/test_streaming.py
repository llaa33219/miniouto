"""Live token streaming: display-only deltas, never persisted.

coreouto fires ON_STREAM_TEXT / ON_STREAM_THINKING once per incremental
fragment. These tests pin the three properties that make the feature safe:

1. every EventSink implementer grew the method (a missing one raises
   AttributeError the instant the first delta fires, since hooks are global
   and there is no `__getattr__` delegation);
2. a delta never reaches the session file — `_RecordingSink` forwards it to
   the wrapped sink but bypasses both `self.events` and the persister;
3. the TUI folds a delta burst into ONE row updated in place, and the
   canonical whole-message rendering replaces it without duplicating text.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
from typing import Any

import pytest
from rich.markdown import Markdown

from miniouto.cli.tui import (
    _WINDOW_MAX_ROWS,
    AnswerRow,
    ChatTUI,
    EventRow,
    StreamRow,
    ThinkingRow,
    TUIEventSink,
    _StreamCoalescer,
)
from miniouto.core import chat as chat_mod
from miniouto.core.events import ConsoleEventSink, LoopEvent, NullSink
from miniouto.storage import sessions as session_store
from miniouto.storage.paths import SESSION_DIR
from miniouto.storage.sessions import STATUS_DONE


class _RawSink:
    """Minimal EventSink that records what it was asked to display."""

    def __init__(self) -> None:
        self.deltas: list[tuple[str, str]] = []
        self.events: list[LoopEvent] = []
        self.answers: list[str] = []

    def begin_working(self) -> None:
        pass

    def update_activity(self, text: str) -> None:
        pass

    def end_working(self) -> None:
        pass

    def emit_loop_event(self, event: LoopEvent) -> None:
        self.events.append(event)

    def emit_final_answer(self, content: str, session_name: str) -> None:
        self.answers.append(content)

    def emit_stream_delta(self, kind: str, text: str) -> None:
        self.deltas.append((kind, text))


class _FakeApp:
    """Stands in for ChatTUI: counts UI-thread hops without running an app."""

    def __init__(self) -> None:
        self.hops: list[tuple[str, tuple[Any, ...]]] = []
        self.text = ""
        self.thinking = ""

    def call_from_thread(self, callback: Any, *args: Any) -> None:
        # A queued UI-thread callback always runs; the hop is what is counted.
        self.hops.append((callback.__name__, args))
        callback(*args)

    def _apply_stream_text(self, delta: str) -> None:
        self.text += delta

    def _apply_stream_thinking(self, delta: str) -> None:
        self.thinking += delta


# ── protocol parity ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "make_sink",
    [lambda: NullSink(), lambda: ConsoleEventSink()],
    ids=["null", "console"],
)
def test_emit_stream_delta_is_callable_on_every_sink(make_sink: Any) -> None:
    # Given: each shipped sink implementation
    sink = make_sink()
    # When: the transient stream hook fires
    # Then: the method exists and accepts a delta without raising
    assert callable(sink.emit_stream_delta)
    sink.emit_stream_delta("text", "hi")


def test_tui_sink_implements_the_stream_method() -> None:
    # Given: the TUI sink with its flush gate closed (no running app needed)
    sink = TUIEventSink(ChatTUI())
    sink._stream = _StreamCoalescer(clock=lambda: 0.0)
    # When: a delta is emitted
    assert callable(sink.emit_stream_delta)
    sink.emit_stream_delta("text", "hi")
    # Then: it is buffered for display, with nothing dispatched
    assert sink._stream.drain() == ("hi", "")


def test_recording_sink_is_the_fourth_implementer() -> None:
    # Given: the wrapper core.chat puts around every caller-provided sink
    # When/Then: it implements the same transient method
    assert callable(chat_mod._RecordingSink(NullSink()).emit_stream_delta)


# ── non-persistence ─────────────────────────────────────────────────────────


def test_recording_sink_forwards_delta_without_recording_it() -> None:
    # Given: a recording sink wired to a persister callback
    raw = _RawSink()
    rec = chat_mod._RecordingSink(raw)
    persisted: list[LoopEvent] = []
    rec.on_event = persisted.append

    # When: a delta arrives
    rec.emit_stream_delta("text", "partial")

    # Then: the wrapped sink saw it, but neither the event list nor the
    # persister did — the session record is the message, never fragments.
    assert raw.deltas == [("text", "partial")]
    assert rec.events == []
    assert persisted == []


def test_streamed_turn_leaves_no_delta_content_in_the_session_file() -> None:
    # Given: a turn that streams every character of its answer
    name = "stream-non-persistence"
    raw = _RawSink()
    rec = chat_mod._RecordingSink(raw)
    persister = chat_mod._TurnPersister(name)
    persister.track(rec.events)
    persister.begin("summarize the repo")
    rec.on_event = persister.on_event
    for char in "DARKROOMTOKEN":
        rec.emit_stream_delta("text", char)
    rec.emit_loop_event(LoopEvent(actor="outo", kind="context", text="iter 1"))
    session_store.finish_turn(
        name,
        prompt="summarize the repo",
        assistant="all done",
        status=STATUS_DONE,
        events=rec.events,
        history=[],
    )

    # When: the session file is read back
    blob = json.dumps(json.loads((SESSION_DIR / f"{name}.json").read_text()))

    # Then: no fragment, and no trace of the stream machinery itself
    assert "DARKROOMTOKEN" not in blob
    assert "delta" not in blob.lower()
    assert "all done" in blob


def test_whole_message_event_is_still_recorded_next_to_streamed_deltas() -> None:
    # Given: deltas for a message that then completes with a tool call
    raw = _RawSink()
    rec = chat_mod._RecordingSink(raw)
    pending: dict[Any, Any] = {}
    dispatcher = chat_mod._make_response_dispatcher(rec, pending)
    for char in "step":
        rec.emit_stream_delta("text", char)

    # When: coreouto reports the completed response
    dispatcher("step one two", True)

    # Then: exactly one whole-message event is stashed for the iteration
    # flush — the delta stream added nothing to it.
    assert list(pending.values()) == [("outo", "step one two", None)]


# ── dispatcher ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "delta"), [("text", "tok"), ("thinking", "hmm")], ids=["text", "thinking"]
)
def test_stream_dispatcher_routes_delta_to_the_matching_kind(kind: str, delta: str) -> None:
    # Given: a sink and a dispatcher for one stream kind
    raw = _RawSink()
    on_delta = chat_mod._make_stream_dispatcher(raw, kind)

    # When: coreouto fires a fragment for that event
    on_delta(delta)
    on_delta("")

    # Then: only the non-empty fragment is forwarded, tagged with its kind
    assert raw.deltas == [(kind, delta)]


def test_stream_dispatcher_forwards_every_delta_it_is_given() -> None:
    # Given: a dispatcher wired to the repetition detector and a raw sink
    raw = _RawSink()
    on_delta = chat_mod._make_stream_dispatcher(raw, "text")

    # When: coreouto fires a fragment
    on_delta("hello")

    # Then: it is forwarded — there is one actor per process now, so no delta
    # can belong to "someone else" and be dropped
    assert raw.deltas == [("text", "hello")]


def test_build_runtime_forwards_coreouto_stream_hooks_to_the_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a real runtime built with both stream dispatchers wired in
    import coreouto as co

    from miniouto.core import context, reasoning
    from miniouto.core import runtime as rt
    from miniouto.storage import providers as provider_store
    from miniouto.storage import styles as style_store

    provider_store.upsert(
        provider_store.Provider(
            name="stream-probe",
            api_format="openai",
            base_url="http://127.0.0.1:9/v1",
            api_key="k",
            default_model="m",
            max_context_window=200_000,
        )
    )
    style_store.write("stream-probe", "<outo>body</outo>", overwrite=True)
    monkeypatch.setattr(context, "get_max_output_tokens", lambda m, p=None: 4_096)
    monkeypatch.setattr(
        reasoning, "resolve_reasoning_passthrough", lambda f, m, p, c: {}
    )
    raw = _RawSink()
    rt.build_runtime(
        rt.RuntimeConfig(
            provider_name="stream-probe", model="m", style_name="stream-probe",
            session="stream-probe",
        ),
        on_stream_text=chat_mod._make_stream_dispatcher(raw, "text"),
        on_stream_thinking=chat_mod._make_stream_dispatcher(raw, "thinking"),
    )

    # When: coreouto fires its per-delta stream hooks with its own payload
    async def fire() -> None:
        await co.hooks.trigger(co.ON_STREAM_TEXT, text="tok", messages=[], model="m")
        await co.hooks.trigger(
            co.ON_STREAM_THINKING, text="hmm", messages=[], model="m"
        )

    asyncio.run(fire())

    # Then: both reached the sink tagged with their kind — the hook kwarg
    # names coreouto really sends are part of the contract.
    assert raw.deltas == [("text", "tok"), ("thinking", "hmm")]
    # And the watchdog's own activity-tracker registration is still there.
    assert len(co.get_hooks(co.ON_STREAM_TEXT)) >= 2


# ── TUI coalescing ──────────────────────────────────────────────────────────


def _frozen_sink(clock: Any) -> tuple[TUIEventSink, _FakeApp]:
    app = _FakeApp()
    sink = TUIEventSink.__new__(TUIEventSink)
    sink._app = app
    sink._stream = _StreamCoalescer(flush_interval=0.033, clock=clock)
    return sink, app


def test_burst_of_deltas_becomes_a_single_ui_hop() -> None:
    # Given: a coalescer whose clock is frozen inside one flush window
    sink, app = _frozen_sink(lambda: 100.0)

    # When: 200 tokens arrive in the same instant
    for _ in range(200):
        sink.emit_stream_delta("text", "a")

    # Then: exactly one hop, carrying only what had arrived when the gate
    # opened — the other 199 fragments are still buffered, not dispatched.
    assert len(app.hops) == 1
    assert app.hops[0] == ("_post_stream", ("a", ""))

    # And the turn's tail is drained on the UI thread, in place, with no
    # extra hop: the row ends up holding all 200 tokens exactly once.
    sink._drain_stream()
    assert len(app.hops) == 1
    assert app.text == "a" * 200


def test_next_window_flushes_and_trailing_fragments_are_drained_on_demand() -> None:
    # Given: a coalescer that has already flushed once
    now = [100.0]
    sink, app = _frozen_sink(lambda: now[0])
    sink.emit_stream_delta("text", "a")
    now[0] += 0.05
    sink.emit_stream_delta("text", "b")

    # When: the turn's last sub-window fragment arrives and the turn ends
    sink.emit_stream_delta("thinking", "why")
    sink._drain_stream()

    # Then: the flush gate fired once per elapsed window, and the drain
    # closed the turn in place, so the last fragment is displayed rather
    # than dropped.
    assert [h[0] for h in app.hops] == ["_post_stream", "_post_stream"]
    assert [h[1] for h in app.hops] == [("a", ""), ("b", "")]
    assert (app.text, app.thinking) == ("ab", "why")


# ── TUI live row ────────────────────────────────────────────────────────────


def _rendered(row: Any) -> str:
    strips = row._render_cache.lines
    return "\n".join(strip.text.rstrip() for strip in strips)


def test_stream_deltas_update_one_row_in_place() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()

            # When: the first delta mounts a live row, then a burst appends
            app._apply_stream_text("Hel")
            await pilot.pause()
            for char in "lo world":
                app._apply_stream_text(char)
            await pilot.pause()

            # Then: exactly one row, holding the whole message
            assert len(app._window.rows) == 1
            row = app._window.rows[0]
            assert isinstance(row, StreamRow)
            assert row._text == "Hello world"
            assert _rendered(row) == "Hello world"

    asyncio.run(body())


def test_thinking_deltas_extend_the_live_reasoning_row() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()

            # When: reasoning streams in fragments
            app._apply_stream_thinking("first ")
            await pilot.pause()
            app._apply_stream_thinking("second")
            await pilot.pause()

            # Then: one collapsed row with a live marker and the reasoning's
            # last words as a one-line tail beside it
            assert len(app._window.rows) == 1
            row = app._window.rows[0]
            assert isinstance(row, ThinkingRow)
            assert row._thinking == "first second"
            assert _rendered(row).startswith("▶ thinking ●")
            assert _rendered(row).endswith("first second")

            # And: the completed message replaces the streamed text in place
            app._add_event_row(LoopEvent(actor="outo", kind="thinking", text="first second (full)"))
            await pilot.pause()
            assert len(app._window.rows) == 1
            assert app._window.rows[0]._thinking == "first second (full)"
            assert _rendered(app._window.rows[0]) == "▶ thinking"

    asyncio.run(body())


def test_response_event_is_suppressed_when_the_text_already_streamed() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            app._apply_stream_text("intermediate text")
            await pilot.pause()

            # When: the completed intermediate message is flushed
            app._add_event_row(
                LoopEvent(actor="outo", kind="response", text="intermediate text")
            )
            await pilot.pause()

            # Then: the live row is the single rendering of that message
            assert len(app._window.rows) == 1
            assert isinstance(app._window.rows[0], StreamRow)

            # And: the next message's deltas start a new live row
            app._apply_stream_text("next")
            await pilot.pause()
            assert len(app._window.rows) == 2
            assert app._window.rows[1]._text == "next"

    asyncio.run(body())


def test_response_event_still_renders_when_nothing_streamed() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()

            # When: a non-streaming provider reports intermediate text
            app._add_event_row(
                LoopEvent(actor="outo", kind="response", text="one shot")
            )
            await pilot.pause()

            # Then: the pre-existing behavior is unchanged
            assert len(app._window.rows) == 1
            assert not isinstance(app._window.rows[0], StreamRow)
            assert _rendered(app._window.rows[0]) == "one shot"

    asyncio.run(body())


def test_deltas_land_before_any_reflow_has_run() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()

            # When: deltas arrive in the same event-loop tick as the mount,
            # before any reflow has run
            app._apply_stream_text("f")
            for char in "ast":
                app._apply_stream_text(char)
            await pilot.pause()

            # Then: no fragment was dropped on the way
            assert len(app._window.rows) == 1
            assert app._window.rows[0]._text == "fast"
            assert _rendered(app._window.rows[0]) == "fast"

    asyncio.run(body())


def test_live_row_survives_window_pressure_at_the_tail() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            app._apply_stream_text("live")
            await pilot.pause()
            live = app._window.rows[0]

            # When: a long turn buries the live row under more rows. Reflows
            # between chunks are what let the anchored view scroll down and
            # the window start evicting from the top.
            for chunk in range(_WINDOW_MAX_ROWS + 50):
                app._mount_row(EventRow(f"row {chunk}"))
                if chunk % 25 == 0:
                    await pilot.pause()
            await pilot.pause()

            # Then: windowing is actually evicting from the top
            assert app._window.first > 0
            rows_before = len(app._window.rows)

            # And the deltas that follow neither re-create a row nor reorder
            # the log — they keep extending the row that already exists.
            app._apply_stream_text(" more")
            await pilot.pause()
            assert len(app._window.rows) == rows_before
            assert live._text == "live more"
            assert app._window.rows[0] is live

    asyncio.run(body())


def test_stream_row_is_not_recreated_after_the_log_is_reset() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            app._apply_stream_text("before reload")
            await pilot.pause()

            # When: the session is reloaded mid-stream
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            app._apply_stream_text("after reload")
            await pilot.pause()

            # Then: a fresh live row starts from the new content — the
            # detached one is not remounted into a log it no longer belongs to
            assert len(app._window.rows) == 1
            assert app._window.rows[0]._text == "after reload"

    asyncio.run(body())


def test_final_answer_replaces_the_live_row_without_duplicating_text() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            sink = TUIEventSink(app)
            app._apply_stream_text("The answer is 42")
            await pilot.pause()
            assert isinstance(app._window.rows[0], StreamRow)

            # When: the turn's final answer arrives
            sink._post_final_answer("The answer is 42", "s")
            await pilot.pause()

            # Then: the live row is gone and the answer renders once
            assert not any(isinstance(r, StreamRow) for r in app._window.rows)
            answers = [r for r in app._window.rows if isinstance(r, AnswerRow)]
            assert len(answers) == 1
            assert app._window.first == 0
            assert "42" in _rendered(answers[0])

    asyncio.run(body())


def test_row_window_remove_keeps_the_mounted_suffix_intact() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            app._apply_stream_text("streamed")
            await pilot.pause()
            app._apply_stream_thinking("because")
            await pilot.pause()
            assert len(app._window.rows) == 2

            # When: both live rows are finalized
            app._finalize_stream()
            app._mount_row(AnswerRow(Markdown("streamed")))
            await pilot.pause()

            # Then: the window holds only the answer, and the bookkeeping
            # index still describes a mounted-from-zero window.
            assert len(app._window.rows) == 1
            assert app._window.first == 0
            assert app._window.rows[0].is_attached

    asyncio.run(body())


# ── replay ──────────────────────────────────────────────────────────────────


def test_replay_of_a_streamed_session_has_no_stream_rows() -> None:
    async def body() -> None:
        name = "stream-replay"
        raw = _RawSink()
        rec = chat_mod._RecordingSink(raw)
        persister = chat_mod._TurnPersister(name)
        persister.track(rec.events)
        persister.begin("hello")
        rec.on_event = persister.on_event
        for char in "hello":
            rec.emit_stream_delta("text", char)
        rec.emit_loop_event(
            LoopEvent(actor="outo", kind="response", text="hello there")
        )
        session_store.finish_turn(
            name,
            prompt="hello",
            assistant="hello there",
            status=STATUS_DONE,
            events=rec.events,
            history=[],
        )

        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._load_session_history(name)
            await pilot.pause()

            # Then: the transcript renders exactly as before streaming existed
            assert not any(isinstance(r, StreamRow) for r in app._window.rows)
            assert not any(isinstance(r, ThinkingRow) for r in app._window.rows)
            assert any(isinstance(r, AnswerRow) for r in app._window.rows)

    asyncio.run(body())


class _Clock:
    """A manual monotonic clock for the coalescer's time gate."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# ── message-boundary flush: a stream tail never leaks into the next message ──


def test_a_message_boundary_event_flushes_the_buffered_stream_tail() -> None:
    """A gated tail must land on ITS message's row before the boundary retires it.

    The coalescer holds the last (< flush-interval) window until the next
    forced flush or `end_working`. Without a boundary flush, a message that
    ends in a tool call leaves its tail buffered: the `response`/`tool`
    events retire the live row, and the tail rides the NEXT message's first
    forced flush onto the new row — the user sees the previous answer's
    ending reappear at the top of the following message.
    """

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            clock = _Clock()
            sink = TUIEventSink(app)
            sink._stream = _StreamCoalescer(clock=clock)
            sink._stream._last_flush = -1.0  # the first delta flushes at once

            async def from_worker(fn: Any) -> None:
                await asyncio.to_thread(fn)
                await pilot.pause(0.05)

            # Given: message 1 streamed, with its tail still inside the window
            await from_worker(lambda: sink.emit_stream_delta("text", "msg1 body."))
            assert app._stream_text_row is not None
            clock.t += 0.001
            await from_worker(lambda: sink.emit_stream_delta("text", " TAIL"))
            assert "TAIL" not in app._stream_text_row.text

            # When: the message completes and the loop moves on (tool call,
            # next iteration) — each a message boundary
            await from_worker(lambda: sink.emit_loop_event(
                LoopEvent(actor="outo", kind="response", text="msg1 body. TAIL", msg_ref="r1")
            ))
            await from_worker(lambda: sink.emit_loop_event(
                LoopEvent(actor="outo", kind="tool", text="Bash ls", tool_name="Bash", msg_ref="r1")
            ))
            await from_worker(lambda: sink.emit_loop_event(
                LoopEvent(actor="outo", kind="context", text="iter 2", msg_ref="r1")
            ))
            # And: message 2 streams
            clock.t += 1.0
            await from_worker(lambda: sink.emit_stream_delta("text", "msg2 body."))
            sink._drain_stream()
            await pilot.pause(0.05)

            # Then: each message's row holds exactly its own text — the tail
            # closed message 1's row instead of prepending message 2's.
            rows = [r for r in app._window.rows if isinstance(r, StreamRow)]
            contents = [r.text for r in rows]
            assert contents == ["msg1 body. TAIL", "msg2 body."]

    asyncio.run(body())


# ── CLI live streaming ───────────────────────────────────────────────────────


def _render_cli_turn(
    *,
    with_deltas: bool,
    quiet: bool = False,
    answer: str = "final answer",
    final: str | None = None,
    thinking: str = "thinking out loud",
) -> str:
    """One verbose CLI turn, in the order the loop really produces it.

    A tool call ran in a PREVIOUS iteration; the current message's reasoning
    streams first (`thinking` deltas), then its text (`text` deltas), and
    ON_ITERATION flushes the completed message `thinking` → `response` →
    `context`. `final` overrides what `emit_final_answer` receives (the
    exception-path case: the answer the run reports differs from what
    streamed).
    """

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sink = ConsoleEventSink(quiet=quiet)
        sink.emit_loop_event(
            LoopEvent(actor="outo", kind="tool", text="Bash ls", tool_name="Bash")
        )
        if with_deltas:
            sink.emit_stream_delta("thinking", thinking)
            for char in answer:
                sink.emit_stream_delta("text", char)
        sink.emit_loop_event(LoopEvent(actor="outo", kind="thinking", text=thinking))
        sink.emit_loop_event(
            LoopEvent(
                actor="outo", kind="response", text=answer if with_deltas else "other"
            )
        )
        sink.emit_loop_event(
            LoopEvent(actor="outo", kind="context", text="iter 1 · 42 tokens")
        )
        sink.emit_final_answer(final if final is not None else answer, "s")
    return buf.getvalue()


def test_console_streams_text_deltas_live() -> None:
    # Given: a turn whose answer arrives as text deltas
    # When: the CLI sink renders it
    out = _render_cli_turn(with_deltas=True)

    # Then: every token went out as it arrived (the wrapper process reads
    # the token stream off stdout, tty or not), and the answer is NOT
    # printed a second time — only the boundary line and the finish
    # marker follow the stream, and nothing trails the marker.
    assert out == (
        "outo: Bash ls\n"
        "outo:thinking| thinking out loud\n"
        "final answer\n"
        "outo: iter 1 · 42 tokens\n"
        "------finish------\n"
    )
    # And: the thinking fragments went out on their own prefixed lane
    # (ONE physical line per line of the fragment; the fragment carries
    # no trailing newline, so its line ends on the unclosed mark) and the
    # completed `thinking` event whose text they already wrote is NOT
    # printed again — the stream was the rendering, exactly like the
    # answer lane.
    assert out.count("outo:thinking| thinking out loud") == 1


def test_console_output_is_unchanged_without_deltas() -> None:
    # Given: a turn from a non-streaming provider
    # When: the CLI sink renders it
    out = _render_cli_turn(with_deltas=False)

    # Then: the whole message renders from its completed events, exactly
    # as before streaming — the streamed path changed nothing here.
    assert out == (
        "outo: Bash ls\n"
        "outo:thinking: thinking out loud\n"
        "outo: other\n"
        "outo: iter 1 · 42 tokens\n"
        "------finish------\n"
        "final answer\n"
    )


def test_quiet_console_outputs_no_deltas_and_stays_byte_identical() -> None:
    # Given: the byte-identical output modes (-a / --with-session)
    # When: the same turn renders quiet and verbose, deltas included
    quiet = _render_cli_turn(with_deltas=True, quiet=True)
    quiet_no_stream = _render_cli_turn(with_deltas=False, quiet=True)

    # Then: no delta, no marker, no loop event — answer only, byte-identical
    # whether the provider streamed or not.
    assert quiet == "final answer\n"
    assert quiet == quiet_no_stream


def test_console_prints_the_full_answer_when_it_did_not_stream() -> None:
    # Given: a turn whose tokens streamed a DIFFERENT text than the final
    # answer (an exception path rewrote it)
    # When: the CLI sink renders it
    out = _render_cli_turn(with_deltas=True, answer="streamed text", final="final answer")

    # Then: both render, each exactly once — the stream is not retracted,
    # and the answer is not swallowed by the duplicate guard. The
    # streamed message's own `response` event IS suppressed (its text
    # matches the tokens), so between the stream and the boundary line
    # only the newline the boundary added appears. The thinking lane
    # rendered once, from its stream.
    assert out == (
        "outo: Bash ls\n"
        "outo:thinking| thinking out loud\n"
        "streamed text\n"
        "outo: iter 1 · 42 tokens\n"
        "------finish------\n"
        "final answer\n"
    )


def test_console_streamed_response_event_is_suppressed() -> None:
    # Given: a message that streamed and then completed
    sink = ConsoleEventSink()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sink.emit_stream_delta("text", "intermediate")
        # The completed message's whole-text event — the same text
        sink.emit_loop_event(
            LoopEvent(actor="outo", kind="response", text="intermediate")
        )
        sink.emit_loop_event(LoopEvent(actor="outo", kind="context", text="iter 1"))

    # Then: the response line is suppressed (its tokens already went out),
    # while the context line — the boundary — still renders.
    assert buf.getvalue() == "intermediate\nouto: iter 1\n"


def test_console_stream_line_is_closed_before_the_next_line() -> None:
    # Given: a stream that ends mid-line
    sink = ConsoleEventSink()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sink.emit_stream_delta("text", "partial line")
        sink.emit_loop_event(
            LoopEvent(actor="outo", kind="thinking", text="a reasoning line")
        )

    # Then: one newline separates the streamed text from the next line —
    # no gluing, no blank line.
    assert buf.getvalue() == "partial line\nouto:thinking: a reasoning line\n"


def test_console_stream_stops_and_reignites_the_spinner_at_boundaries() -> None:
    # Given: a verbose sink whose spinner is running
    sink = ConsoleEventSink()
    status_log: list[str] = []

    class _Status:
        def __init__(self, text: str) -> None:
            self.text = text
        def update(self, text: str) -> None:
            status_log.append("update:" + text)
        def __enter__(self) -> _Status:
            status_log.append("enter")
            return self
        def __exit__(self, *_args: Any) -> None:
            status_log.append("exit")

    class _Console:
        def status(self, text: str, **_kwargs: Any) -> _Status:
            return _Status(text)
        def print(self, *_a: Any, **_k: Any) -> None:
            pass

    sink._console = _Console()  # type: ignore[assignment]

    # When: a delta arrives while the spinner is live, then the message
    # completes (its boundary events fire)
    sink.begin_working()
    assert status_log == ["enter"]
    sink.emit_stream_delta("text", "token")
    assert status_log == ["enter", "exit"]  # stopped, not re-lit mid-stream
    sink.emit_stream_delta("text", "s")
    assert status_log == ["enter", "exit"]  # STILL stopped: tokens are life
    # The suppressed `response` is a boundary but the guard is still open
    # (its `context` has not fired), so the spinner stays down and the
    # stop-flag survives for the event that actually closes the message.
    sink.emit_loop_event(LoopEvent(actor="outo", kind="response", text="tokens"))
    assert status_log == ["enter", "exit"]
    sink.emit_loop_event(LoopEvent(actor="outo", kind="context", text="iter 1"))
    assert status_log == ["enter", "exit", "enter"]  # boundary re-ignites it
    sink.emit_stream_delta("text", "x")  # message 2 streams
    assert status_log == ["enter", "exit", "enter", "exit"]
    sink.emit_loop_event(LoopEvent(actor="outo", kind="context", text="iter 2"))
    assert status_log == ["enter", "exit", "enter", "exit", "enter"]

    # And quiet never touches the spinner: a quiet sink's deltas are no-ops
    quiet = ConsoleEventSink(quiet=True)
    quiet._console = _Console()  # type: ignore[assignment]
    quiet.begin_working()
    quiet.emit_stream_delta("text", "t")
    assert status_log == ["enter", "exit", "enter", "exit", "enter"]


def test_console_streaming_works_through_a_pipe() -> None:
    # Given: stdout is a pipe, not a tty (the wrapper process's view)
    r, w = os.pipe()
    try:
        old = sys.stdout
        sys.stdout = os.fdopen(w, "w", closefd=False)
        try:
            sink = ConsoleEventSink()
            sink.begin_working()  # spinner is a no-op rendering on a pipe
            for char in "PIPE":
                sink.emit_stream_delta("text", char)
            sys.stdout.flush()
        finally:
            sys.stdout.close()
            sys.stdout = old
        data = os.read(r, 100)
    finally:
        os.close(r)
        os.close(w)

    # Then: the tokens are on the pipe as they arrived
    assert data.decode() == "PIPE"


def test_console_streams_thinking_deltas_on_the_prefixed_lane() -> None:
    # Given: a message whose reasoning arrives as thinking deltas
    sink = ConsoleEventSink()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sink.emit_stream_delta("thinking", "plan the work")
        sink.emit_stream_delta("thinking", "step one\nstep two\n")

    # Then: every fragment went out as it arrived, one prefixed PHYSICAL
    # line per line of the fragment (an embedded newline never puts the
    # prefix mid-line, so the wrapper's line parser stays sound) — and no
    # trailing phantom line follows a fragment that ended with a newline.
    # The newline-less fragments end on the unclosed mark (their line's
    # newline is framing), so a reader reassembles EXACTLY the fragments.
    assert buf.getvalue() == (
        "outo:thinking| plan the work\n"
        "outo:thinking: step one\n"
        "outo:thinking: step two\n"
    )


def test_console_thinking_framing_round_trips_byte_for_byte() -> None:
    # Given: arbitrary reasoning — newlines inside fragments, fragments
    # that end mid-word, blank lines, CJK, a fragment that IS the prefix —
    # cut into fragments at arbitrary boundaries (the shapes providers
    # really fire ON_STREAM_THINKING with)
    import random

    from miniouto.core.events import (
        THINKING_DELTA_PREFIX,
        THINKING_DELTA_PREFIX_UNCLOSED,
    )

    texts = [
        "The user asks: \"Think briefly about 2+2, then answer.\"",
        "step one\nstep two is longer",
        "trailing newline\n",
        "blank line\n\nnext\n",
        "한글 생각을\n스트리밍합니다",
        "no newline at the end",
        "\n",
        "outo:thinking: the prefix itself as content",
        "outo:thinking| and the unclosed mark as content",
    ]
    rng = random.Random(20260121)
    cases: list[list[str]] = [[t] for t in texts]
    for text in texts:
        for _ in range(6):
            cuts = sorted(
                rng.sample(range(1, len(text) + 1), min(5, len(text)))
            )
            fragments, prev = [], 0
            for cut in cuts:
                fragments.append(text[prev:cut])
                prev = cut
            fragments.append(text[prev:])
            cases.append([f for f in fragments if f])

    for fragments in cases:
        sink = ConsoleEventSink()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for fragment in fragments:
                sink.emit_stream_delta("thinking", fragment)

        # When: a wrapper reassembles the lane — strip one prefix per
        # physical line; the plain prefix's newline is content, the
        # unclosed mark's is framing and is not re-added
        parts: list[str] = []
        for line in buf.getvalue().split("\n")[:-1]:
            if line.startswith(THINKING_DELTA_PREFIX):
                parts.append(line[len(THINKING_DELTA_PREFIX):] + "\n")
            elif line.startswith(THINKING_DELTA_PREFIX_UNCLOSED):
                parts.append(line[len(THINKING_DELTA_PREFIX_UNCLOSED):])
            else:  # pragma: no cover - the sink never writes anything else
                raise AssertionError(f"unframed line {line!r}")

        # Then: the reassembly is the original reasoning byte for byte —
        # no newline the framing invented, none it swallowed.
        assert "".join(parts) == "".join(fragments), fragments


def test_console_streamed_thinking_event_is_not_printed_twice() -> None:
    # Given: a message whose reasoning streamed, then completed
    sink = ConsoleEventSink()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sink.emit_stream_delta("thinking", "why")
        sink.emit_stream_delta("thinking", " and wherefore")
        sink.emit_loop_event(
            LoopEvent(actor="outo", kind="thinking", text="why and wherefore")
        )
        # A DIFFERENT message's thinking never streamed: it still renders
        # from its completed event, exactly as before.
        sink.emit_loop_event(
            LoopEvent(actor="outo", kind="thinking", text="one-shot reasoning")
        )
        sink.emit_loop_event(LoopEvent(actor="outo", kind="context", text="iter 1"))

    # Then: the streamed reasoning's completed event is suppressed (the
    # fragments were the rendering) and the next message's thinking is
    # printed in full — the boundary reset the duplicate guard.
    assert buf.getvalue() == (
        "outo:thinking| why\n"
        "outo:thinking|  and wherefore\n"
        "outo:thinking: one-shot reasoning\n"
        "outo: iter 1\n"
    )


def test_console_thinking_delta_closes_a_raw_text_line_first() -> None:
    # Given: an answer fragment left stdout mid-line
    sink = ConsoleEventSink()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sink.emit_stream_delta("text", "partial answer")
        sink.emit_stream_delta("thinking", "meanwhile, thinking")

    # Then: the prefixed lane starts at column 0 — a wrapper reading the
    # pipe can only recognize the prefix on a line start.
    assert buf.getvalue() == (
        "partial answer\nouto:thinking| meanwhile, thinking\n"
    )


def test_console_thinking_deltas_also_stop_and_reignite_the_spinner() -> None:
    # Given: a verbose sink whose spinner is running
    sink = ConsoleEventSink()
    status_log: list[str] = []

    class _Status:
        def __init__(self, text: str) -> None:
            self.text = text

        def update(self, text: str) -> None:
            status_log.append("update:" + text)

        def __enter__(self) -> _Status:
            status_log.append("enter")
            return self

        def __exit__(self, *_args: Any) -> None:
            status_log.append("exit")

    class _Console:
        def status(self, text: str, **_kwargs: Any) -> _Status:
            return _Status(text)

        def print(self, *_a: Any, **_k: Any) -> None:
            pass

    sink._console = _Console()  # type: ignore[assignment]

    # When: a thinking delta arrives while the spinner is live
    sink.begin_working()
    sink.emit_stream_delta("thinking", "hmm")
    assert status_log == ["enter", "exit"]  # tokens are the liveness signal
    sink.emit_stream_delta("thinking", " more")
    assert status_log == ["enter", "exit"]  # still stopped mid-stream
    sink.begin_working()
    assert status_log == ["enter", "exit"]  # and it cannot be re-lit yet

    # And: the message's boundary events return it
    sink.emit_loop_event(LoopEvent(actor="outo", kind="thinking", text="hmm more"))
    assert status_log == ["enter", "exit", "enter"]

    # And quiet never touches the spinner through the thinking lane either
    quiet = ConsoleEventSink(quiet=True)
    quiet._console = _Console()  # type: ignore[assignment]
    quiet.begin_working()
    quiet.emit_stream_delta("thinking", "t")
    assert status_log == ["enter", "exit", "enter"]
