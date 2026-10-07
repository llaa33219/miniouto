"""Real-time phrase repetition on the STREAM, and the truncated answer it saves.

Counting phrases only at message boundaries (whole thinking/content once the
LLM call returns) cannot see the failure that actually hurts: ONE answer
that never ends, repeating the same sentence forever inside itself. The
message only returns at max_tokens — minutes and tens of thousands of
tokens later.

So the deltas feed the detector too (`repetition.note_stream_delta`), and
because deltas fragment mid-segment they go through a buffering splitter
that counts each COMPLETED segment the moment it completes. The whole-message
`note_text` that still fires for the same message must then count only what
is left over, or every repeat is counted twice and the threshold trips at
half of it.

The second half of the contract is the record. When the force-stop cuts the
stream mid-flight the message never completes, so its whole-message event
never fires and the text the user actually watched would vanish from the
turn. The truncated answer is therefore emitted AS-IS (verbatim concatenated
deltas, `msg_ref=None`, so no pruning path can ever take it back) before the
alarm.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import coreouto as co
import pytest

import harness
from miniouto.cli.tui import ChatTUI, StreamRow
from miniouto.core import chat as chat_mod
from miniouto.core import repetition, subagents
from miniouto.core import runtime as rt
from miniouto.core.events import LoopEvent

FRAGMENTS = ("Let me check ", "the parser ", "again. ")
REPEATED = "".join(FRAGMENTS)
SEGMENT = "let me check the parser again"


@pytest.fixture(autouse=True)
def _clean_counters() -> Iterator[None]:
    repetition.reset()
    yield
    repetition.reset()


def _trip_notifier() -> tuple[list[str], Any]:
    """A trip notifier that records the actor keys it was woken for."""

    fired: list[str] = []
    return fired, fired.append


# ── 1. real-time: the verdict trips while the deltas are still arriving ─────


def test_deltas_trip_the_phrase_detector_before_the_message_ends() -> None:
    # Given: a notifier the supervisor would wake on, and one phrase per
    # iteration split mid-segment, the way a token stream really arrives
    fired, notifier = _trip_notifier()
    repetition.set_trip_notifier(notifier)
    try:
        # When: the phrase is streamed REPEAT_PHRASE_THRESHOLD times, and the
        # message is still open — no message end, no whole-message note
        for _ in range(repetition.REPEAT_PHRASE_THRESHOLD):
            for fragment in FRAGMENTS:
                repetition.note_stream_delta("outo", fragment)

        # Then: the verdict tripped on the spot, from the stream alone
        verdict = repetition.check("outo")
        assert verdict is not None
        assert verdict.kind == "phrase"
        assert verdict.detail == f"{repetition.REPEAT_PHRASE_THRESHOLD}x: {SEGMENT}"

        # And exactly one notification: the verdict is sticky, so the
        # increments after the trip stay silent
        assert fired == ["outo"]

        # And a second message cannot re-notify while the verdict holds
        for fragment in FRAGMENTS:
            repetition.note_stream_delta("outo", fragment)
        assert fired == ["outo"]
    finally:
        repetition.set_trip_notifier(None)


def test_a_still_unfinished_phrase_does_not_trip() -> None:
    # Given: the phrase typed out REPEAT_PHRASE_THRESHOLD times but never
    # terminated — no segment break, so no segment has completed
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD * len(FRAGMENTS)):
        repetition.note_stream_delta("outo", FRAGMENTS[0])

    # Then: nothing is counted yet
    assert repetition.check("outo") is None


# ── 2. no double count with the whole-message path ─────────────────────────


def test_a_whole_message_note_counts_only_what_the_deltas_left_over() -> None:
    # Given: the same phrase streamed REPEAT_PHRASE_THRESHOLD/2 times
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD // 2):
        for fragment in FRAGMENTS:
            repetition.note_stream_delta("outo", fragment)

    # When: the message completes and coreouto's whole-message note fires
    # with every one of those repeats plus REPEAT_PHRASE_THRESHOLD more
    whole = " ".join(f"{SEGMENT}." for _ in range(repetition.REPEAT_PHRASE_THRESHOLD))
    repetition.note_text("outo", whole)

    # Then: it did NOT trip — the already-counted repeats were not counted
    # a second time, so the threshold is still half the occurrences away
    assert repetition.check("outo") is None

    # And the count is honest: exactly REPEAT_PHRASE_THRESHOLD occurrences
    # in total (half from the stream + half from the tail the whole-message
    # note legitimately added), not one and a half times that.
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD // 2):
        for fragment in FRAGMENTS:
            repetition.note_stream_delta("outo", fragment)
    verdict = repetition.check("outo")
    assert verdict is not None
    assert verdict.detail == f"{repetition.REPEAT_PHRASE_THRESHOLD}x: {SEGMENT}"


def test_a_non_streaming_provider_still_counts_the_whole_message() -> None:
    # Given: a provider that never fires a delta (no buffer at all)
    # When: the whole message arrives at once
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD):
        repetition.note_text("outo", f"{SEGMENT}.")

    # Then: the pre-existing whole-message behavior is unchanged
    verdict = repetition.check("outo")
    assert verdict is not None
    assert verdict.detail == f"{repetition.REPEAT_PHRASE_THRESHOLD}x: {SEGMENT}"


# ── 3. the trailing segment is not lost ────────────────────────────────────


def test_flush_stream_counts_the_unfinished_tail() -> None:
    # Given: REPEAT_PHRASE_THRESHOLD-1 complete segments, then a tail with
    # no segment break — the shape of a message cut mid-phrase
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD - 1):
        for fragment in FRAGMENTS:
            repetition.note_stream_delta("outo", fragment)
    repetition.note_stream_delta("outo", SEGMENT)
    assert repetition.check("outo") is None

    # When: the message ends and the tail is flushed
    repetition.flush_stream("outo")

    # Then: the tail counted as the threshold-th occurrence
    verdict = repetition.check("outo")
    assert verdict is not None
    assert verdict.detail == f"{repetition.REPEAT_PHRASE_THRESHOLD}x: {SEGMENT}"

    # And a second flush is a no-op — the buffer was cleared
    repetition.flush_stream("outo")
    assert repetition.check("outo").detail.startswith(  # type: ignore[union-attr]
        f"{repetition.REPEAT_PHRASE_THRESHOLD}x:"
    )


def test_a_flushed_actor_starts_over_with_the_next_message() -> None:
    # Given: a message that streamed and was closed
    repetition.note_stream_delta("outo", "one two three four")
    repetition.flush_stream("outo")

    # When: the NEXT message is reported whole (it never streamed)
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD):
        repetition.note_text("outo", "one two three four.")

    # Then: it is counted in full, exactly as a non-streaming turn always was
    assert repetition.check("outo") is not None


def test_a_streamed_run_spans_the_message_boundary() -> None:
    # Given: half a run streamed, then the boundary flushed, then the rest
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD // 2):
        for fragment in FRAGMENTS:
            repetition.note_stream_delta("outo", fragment)
    repetition.flush_stream("outo")
    assert repetition.check("outo") is None

    # When: the next message streams the same phrase up to the threshold
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD // 2):
        for fragment in FRAGMENTS:
            repetition.note_stream_delta("outo", fragment)

    # Then: the run spans the boundary, so the 50th segment trips
    verdict = repetition.check("outo")
    assert verdict is not None
    assert verdict.detail == f"{repetition.REPEAT_PHRASE_THRESHOLD}x: {SEGMENT}"


# ── 4. E2E: a parked, never-ending stream is cut and recorded ──────────────


def test_a_never_ending_stream_is_cut_mid_flight_and_the_truncated_answer_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a model that streams the same sentence forever in fragments
    # and then PARKS — the call never returns, so no message end, no
    # whole-message note, and nothing but a real-time detector can end it
    session = "e2e-stream-repetition"
    monkeypatch.setattr(rt, "WATCHDOG_CHECK_SECONDS", 0.01)
    final = "I read the AST instead of grepping."
    calls = 0

    async def handler(req: harness.Request) -> co.LLMResponse:
        nonlocal calls
        if req.actor != "outo":
            return co.LLMResponse(content="unused")
        if any(t.startswith(subagents.ALARM_HEADER) for t in req.user_texts()):
            return co.LLMResponse(content=final)
        calls += 1
        for _ in range(repetition.REPEAT_PHRASE_THRESHOLD):
            for fragment in FRAGMENTS:
                await req.stream(fragment)
        # The message never completes: only the force-stop can end this.
        for _ in range(2_000):
            await asyncio.sleep(0.005)
        raise AssertionError("the supervisor never force-stopped the runaway stream")

    provider = harness.install(monkeypatch, handler, session=session)
    sink = harness.CaptureSink()

    # When: the turn runs
    answer = chat_mod.run_chat(chat_mod.ChatOptions(prompt="find the bug", session=session), sink)

    # Then: the second attempt's answer is what the user got — the runaway
    # stream never reached run_chat
    assert calls == 1
    assert answer == final
    assert answer != REPEATED

    # And the resumed attempt was told why, with the shared alarm header
    delivered = [
        t
        for t in provider.for_actor("outo")[-1].user_texts()
        if t.startswith(subagents.ALARM_HEADER)
    ]
    assert len(delivered) == 1
    assert [e.actor for e in sink.of_kind("alarm")] == ["watchdog"]

    # And the TRUNCATED ANSWER was recorded verbatim, as an ordinary
    # message-boundary event: the user watched it, so it must be in the
    # turn. `msg_ref` is None because the assistant message never existed,
    # which also makes this row permanently unprunable.
    truncated = sink.of_kind("response")
    assert [e.text for e in truncated] == [REPEATED * repetition.REPEAT_PHRASE_THRESHOLD]
    assert truncated[0].msg_ref is None
    assert truncated[0].actor == "outo"

    # And it reached the session file with the turn, which was not removed
    events = harness.turn_events(session)
    recorded = [e for e in events if e["kind"] == "response"]
    assert [e["text"] for e in recorded] == [REPEATED * repetition.REPEAT_PHRASE_THRESHOLD]
    assert "msg_ref" not in recorded[0]
    blob = json.dumps(harness.session_blob(session))
    assert REPEATED * 3 in blob
    # The deltas themselves are still never a record — no fragment has an
    # event of its own; only the flushed truncated message was recorded.
    assert all(e["text"] not in FRAGMENTS for e in events)
    assert len(recorded) == 1
    # And the turn is intact: a done turn carries no status key, so its
    # presence would mean the turn had been interrupted instead.
    assert "status" not in harness.session_blob(session)["turns"][-1]


# ── 5. the partial buffers are consumed by the flush ───────────────────────


def test_the_force_stop_flush_leaves_no_stale_partial_behind() -> None:
    # Given: a run whose stream dispatcher buffered a partial answer
    rec = chat_mod._RecordingSink(harness.CaptureSink())
    partials = chat_mod._PartialStreams()
    on_delta = chat_mod._make_stream_dispatcher(rec, "text", partials)
    on_delta("half a sentence that never ends")

    # When: the force-stop drains it (what the on_repetition handler does)
    drained = partials.drain()

    # Then: the verbatim text came back once, and nothing is left over
    assert drained == [("text", "outo", "half a sentence that never ends")]
    assert partials.drain() == []


def test_a_completed_message_does_not_leave_a_partial_behind() -> None:
    # Given: a streamed message that completes normally
    rec = chat_mod._RecordingSink(harness.CaptureSink())
    partials = chat_mod._PartialStreams()
    on_delta = chat_mod._make_stream_dispatcher(rec, "text", partials)
    on_delta("all done here")
    partials.close_message("outo")

    # Then: the completed message owns the text now, so a later force-stop
    # has nothing to re-emit and the completion path stays silent
    assert partials.drain() == []


def test_the_truncated_event_finalizes_the_live_row_instead_of_duplicating_it() -> None:
    async def body() -> None:
        # Given: a live stream row, still showing the partial answer
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            app._apply_stream_text("a partial answer")
            await pilot.pause()
            assert isinstance(app._window.rows[0], StreamRow)

            # When: the force-stop emits the truncated message (msg_ref None,
            # the assistant message never existed) and then the alarm
            app._add_event_row(
                LoopEvent(
                    actor="outo", kind="response", text="a partial answer", msg_ref=None
                )
            )
            app._add_event_row(
                LoopEvent(actor="watchdog", kind="alarm", text="stopped by watchdog")
            )
            await pilot.pause()

            # Then: the live row IS the record of that message — no second
            # copy — and the alarm renders under it
            rows = app._window.rows
            assert len(rows) == 2
            assert isinstance(rows[0], StreamRow)
            assert rows[0]._text == "a partial answer"
            assert rows[0].msg_ref is None
            assert "stopped by watchdog" in _render(rows[1])

    asyncio.run(body())


def _render(row: Any) -> str:
    strips = getattr(row, "_render_cache", None)
    if strips is None:  # a plain EventRow renders its Text directly
        return str(getattr(row, "renderable", ""))
    return "\n".join(strip.text.rstrip() for strip in strips.lines)
