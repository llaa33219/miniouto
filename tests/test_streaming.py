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

            # Then: one collapsed row with a live marker
            assert len(app._window.rows) == 1
            row = app._window.rows[0]
            assert isinstance(row, ThinkingRow)
            assert row._thinking == "first second"
            assert _rendered(row) == "▶ thinking ●"

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


# ── CLI stays byte-identical ────────────────────────────────────────────────


def _render_cli_turn(*, with_deltas: bool) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sink = ConsoleEventSink()
        if with_deltas:
            for char in "TOKENSTREAM":
                sink.emit_stream_delta("text", char)
            sink.emit_stream_delta("thinking", "hidden reasoning")
        sink.emit_loop_event(
            LoopEvent(actor="outo", kind="tool", text="Bash ls", tool_name="Bash")
        )
        sink.emit_loop_event(LoopEvent(actor="outo", kind="thinking", text="thinking out loud"))
        sink.emit_loop_event(
            LoopEvent(actor="outo", kind="context", text="iter 1 · 42 tokens")
        )
        sink.emit_final_answer("final answer", "s")
    return buf.getvalue()


def test_console_output_is_unchanged_by_stream_deltas() -> None:
    # Given: the same loop-event sequence with and without live deltas
    # When: both are rendered through the CLI sink
    with_deltas = _render_cli_turn(with_deltas=True)
    without_deltas = _render_cli_turn(with_deltas=False)

    # Then: byte-identical — the CLI has no live-stream rendering at all
    assert with_deltas == without_deltas
    assert "TOKENSTREAM" not in with_deltas
    assert "hidden reasoning" not in with_deltas
    assert "final answer" in with_deltas
