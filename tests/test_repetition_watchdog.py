"""Lock the repetition watchdog: core/repetition.py + supervised_run's restart path.

A model that stops making progress rarely goes quiet — it keeps talking and
keeps calling tools, so the stall watchdog never fires for it. The
repetition watchdog is the other half of that failure: it counts identical
phrases and identical tool calls per actor, force-stops the loop on the
spot, and resumes the turn with an English `[SYSTEM ALARM]` that tells the
model to change pattern.

The counts are the user-facing contract (a phrase UNIT repeated >=50 times in
a row, >=30 identical tool calls in a row, at most 3 restarts), so the
boundary cases and the two alarm texts are pinned by exact equality rather
than by "contains".
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import coreouto as co
import pytest

from miniouto.core import repetition, subagents
from miniouto.core import runtime as rt
from miniouto.core.providers import API_STALL_TIMEOUT_SECONDS

PHRASE = "I will inspect the failing module again"
# The lead-in a model says once per tool iteration: the same segment in every
# message, while the messages themselves are about DIFFERENT work.
BOILERPLATE = "let me run the next command and read the output"
TOOL_ARGS = {"command": "ls -la"}


@pytest.fixture(autouse=True)
def _clean_counters() -> Iterator[None]:
    repetition.reset()
    yield
    repetition.reset()


class _Activity:
    """Watchdog activity stub. `silent` is what the stall check would see."""

    def __init__(self, silent: float = 0.0) -> None:
        self.silent = silent

    def seconds_since_last_activity(self) -> float:
        return self.silent


class _Progress:
    """Watchdog progress stub — `phase` decides the stall check's tool exemption."""

    phase: str | None = "llm_call"


def _states(
    silent: float = 0.0, live: list[co.Message] | None = None
) -> tuple[Any, Any, dict[str, Any]]:
    return (_Activity(silent), _Progress(), {"outo": live or []})


def _drive(coro: Any) -> Any:
    return asyncio.run(coro)


def test_forty_nine_repeats_of_a_phrase_do_not_trip_but_the_fiftieth_does() -> None:
    # Given: one phrase seen REPEAT_PHRASE_THRESHOLD - 1 times IN A ROW
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD - 1):
        repetition.note_text("outo", f"{PHRASE}.")
    assert repetition.check("outo") is None

    # When: the threshold-th occurrence arrives
    repetition.note_text("outo", f"{PHRASE}.")

    # Then: the verdict trips exactly on the documented boundary, and the
    # detail keeps its shape (a p=1 unit renders as the segment itself)
    verdict = repetition.check("outo")
    assert verdict is not None
    assert verdict.kind == "phrase"
    assert verdict.detail == f"{repetition.REPEAT_PHRASE_THRESHOLD}x: {PHRASE.lower()}"
    assert repetition.REPEAT_PHRASE_THRESHOLD == 50


def test_twenty_nine_identical_tool_calls_do_not_trip_but_the_thirtieth_does() -> None:
    # Given: one tool call seen REPEAT_TOOL_THRESHOLD - 1 times
    for _ in range(repetition.REPEAT_TOOL_THRESHOLD - 1):
        repetition.note_tool_call("outo", "Bash", dict(TOOL_ARGS))

    # When: the threshold-th occurrence arrives
    repetition.note_tool_call("outo", "Bash", dict(TOOL_ARGS))

    # Then: the verdict trips exactly on the documented boundary
    verdict = repetition.check("outo")
    assert verdict is not None
    assert verdict.kind == "tool"
    assert "Bash" in verdict.detail
    assert repetition.REPEAT_TOOL_THRESHOLD == 30


def test_identical_tool_calls_scattered_by_other_calls_do_not_trip() -> None:
    # Given: two long batches of one identical call, split by a DIFFERENT
    # call in between — 58 identical calls in a turn that is not looping
    batch = {"command": "git status"}
    for i in range(2 * (repetition.REPEAT_TOOL_THRESHOLD - 1)):
        repetition.note_tool_call("outo", "Bash", dict(batch))
        if i % 2 == 0:
            repetition.note_tool_call("outo", "Bash", {"command": f"ls {i}"})

    # When/Then: nothing accumulates across the interruption — a legitimately
    # repeated utility call scattered over a long turn must never warn
    assert repetition.check("outo") is None

    # And a genuine back-to-back streak still trips on the documented boundary
    repetition.note_tool_call("outo", "Bash", {"command": "ls -la"})
    for _ in range(repetition.REPEAT_TOOL_THRESHOLD - 1):
        repetition.note_tool_call("outo", "Bash", dict(batch))
    assert repetition.check("outo") is None
    repetition.note_tool_call("outo", "Bash", dict(batch))
    verdict = repetition.check("outo")
    assert verdict is not None
    assert verdict.kind == "tool"


def test_all_different_tool_calls_never_trip() -> None:
    # Given: far more calls than the threshold, none of them alike
    for i in range(3 * repetition.REPEAT_TOOL_THRESHOLD):
        repetition.note_tool_call("outo", "Bash", {"command": f"echo {i}"})

    # When/Then: the verdict stays None — locked through the public API so a
    # future key regression (argument details dropped from the key) is caught
    assert repetition.check("outo") is None


def test_a_different_tool_name_resets_the_streak_too() -> None:
    # Given: one tool's streak interrupted by a single call to another tool
    batch = {"command": "git status"}
    for _ in range(repetition.REPEAT_TOOL_THRESHOLD - 1):
        repetition.note_tool_call("outo", "Bash", dict(batch))
    repetition.note_tool_call("outo", "Computer", {"action": "screenshot"})

    # When: the same call resumes, it starts a fresh streak of one
    for i in range(repetition.REPEAT_TOOL_THRESHOLD - 2):
        repetition.note_tool_call("outo", "Bash", dict(batch))
        if i % 5 == 0:
            repetition.note_tool_call("outo", "Computer", {"action": "screenshot"})

    # Then: 57 identical Bash calls, no streak of them long enough to trip
    assert repetition.check("outo") is None


def test_the_same_phrase_with_other_case_and_whitespace_counts_once() -> None:
    # Given: the same sentence written with varying case and inner spacing
    variants = [
        "Let me   check the file",
        "let me check   the file",
        "LET ME CHECK THE FILE",
        "let me check the file",
        "Let\tMe Check The File",
    ]

    # When: they are fed back to back until the threshold
    for i in range(repetition.REPEAT_PHRASE_THRESHOLD):
        repetition.note_text("outo", variants[i % len(variants)])

    # Then: they normalized to ONE segment, so the boundary still trips
    verdict = repetition.check("outo")
    assert verdict is not None
    assert verdict.kind == "phrase"
    assert "let me check the file" in verdict.detail


def test_counters_are_isolated_per_actor() -> None:
    # Given: outo and two subagent sids each at the phrase threshold
    for key in ("outo", "abc123", "def456"):
        for _ in range(repetition.REPEAT_PHRASE_THRESHOLD):
            repetition.note_text(key, f"{PHRASE}.")

    # When: each actor is checked
    # Then: only the actor that was fed trips — no cross-talk either way
    assert repetition.check("outo") is not None
    assert repetition.check("abc123") is not None
    assert repetition.check("def456") is not None
    assert repetition.check("999999") is None

    # And an unfed actor's outo-level repeat never happens
    repetition.reset_actor("outo")
    for _ in range(repetition.REPEAT_TOOL_THRESHOLD):
        repetition.note_tool_call("abc123", "Bash", dict(TOOL_ARGS))
    assert repetition.check("abc123") is not None
    assert repetition.check("outo") is None


def test_verdict_is_sticky_until_consumed_and_the_per_turn_reset_clears_counters() -> None:
    # Given: a tripped tool verdict
    for _ in range(repetition.REPEAT_TOOL_THRESHOLD):
        repetition.note_tool_call("outo", "Bash", dict(TOOL_ARGS))
    verdict = repetition.check("outo")

    # When: it is polled repeatedly without being consumed
    # Then: the same verdict comes back every time (the supervisor polls it)
    assert verdict is not None
    assert repetition.check("outo") is verdict
    assert repetition.check("outo") is verdict

    # And consuming it clears the verdict AND the counts, so the resumed
    # attempt must earn its own verdict from scratch
    assert repetition.consume_verdict("outo") is verdict
    assert repetition.check("outo") is None
    for _ in range(repetition.REPEAT_TOOL_THRESHOLD - 1):
        repetition.note_tool_call("outo", "Bash", dict(TOOL_ARGS))
    assert repetition.check("outo") is None

    # And the per-turn reset (build_runtime → _register_watchdog_trackers,
    # where the live message lists are cleared) drops every actor
    repetition.note_text("abc123", f"{PHRASE}.")
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD):
        repetition.note_text("outo", f"{PHRASE}.")
    rt._register_watchdog_trackers()
    assert repetition.check("outo") is None
    assert repetition.check("abc123") is None


def test_supervised_run_force_stops_the_loop_and_resumes_with_the_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an attempt that issues the same tool call 30 times, then hangs
    monkeypatch.setattr(rt, "WATCHDOG_CHECK_SECONDS", 0.01)
    prompts: list[str | None] = []
    histories: list[list[co.Message] | None] = []
    repeats: list[tuple[repetition.LoopVerdict, str]] = []
    live = [
        co.Message(role="user", content="do the work"),
        co.Message(role="assistant", content="looking"),
    ]

    async def start(p: str | None, h: list[co.Message] | None) -> co.Response:
        prompts.append(p)
        histories.append(h)
        if len(prompts) == 1:
            for _ in range(repetition.REPEAT_TOOL_THRESHOLD):
                repetition.note_tool_call("outo", "Bash", dict(TOOL_ARGS))
            await asyncio.sleep(3600)  # never resolves on its own
        return co.Response(content="did it differently", messages=[], iterations=1)

    # When: the turn runs under the supervisor
    response = _drive(
        rt.supervised_run(
            start,
            "do the work",
            None,
            live_key="outo",
            level=0,
            on_wakeup=lambda *_args: None,
            restart_on_repetition="outo",
            on_repetition=lambda verdict, alarm: repeats.append((verdict, alarm)),
            states=_states(live=live),
            )
    )

    # Then: the loop was cancelled and resumed exactly once, with the alarm
    # as the resumed attempt's user message and the sanitized transcript
    assert response.content == "did it differently"
    assert prompts == ["do the work", repeats[0][1]]
    assert histories[1] == live
    assert repeats[0][0].kind == "tool"
    assert repeats[0][1] == repetition.alarm_text(repeats[0][0])
    assert repeats[0][1].startswith(subagents.ALARM_HEADER)
    assert rt._ACTIVE_SUPERVISORS == {}


def test_repeating_restarts_are_exhausted_without_touching_the_stall_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: every attempt immediately re-trips the verdict, and the stall
    # condition is ARMED (silence past the limit, not inside a tool) with a
    # sentinel wakeup budget of 1 — a shared counter would spend its first
    # repetition restart and raise LoopStalledError instead
    monkeypatch.setattr(rt, "WATCHDOG_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(rt, "WATCHDOG_MAX_WAKEUPS", 1)
    attempts = 0

    async def start(p: str | None, h: list[co.Message] | None) -> co.Response:
        nonlocal attempts
        attempts += 1
        for _ in range(repetition.REPEAT_TOOL_THRESHOLD):
            repetition.note_tool_call("outo", "Bash", dict(TOOL_ARGS))
        await asyncio.sleep(3600)

    # When: the supervisor gives up on the repetition
    with pytest.raises(rt.LoopRepeatedError) as raised:
        _drive(
            rt.supervised_run(
                start,
                "do the work",
                None,
                live_key="outo",
                level=0,
                on_wakeup=lambda *_args: None,
                restart_on_repetition="outo",
                states=_states(silent=API_STALL_TIMEOUT_SECONDS * 10),
            )
        )

    # Then: exactly REPEAT_MAX_RESTARTS restarts happened, then the
    # repetition error — the stall counter and its sentinel are untouched
    assert attempts == repetition.REPEAT_MAX_RESTARTS + 1
    assert repetition.REPEAT_MAX_RESTARTS == 3
    assert "repeating" in str(raised.value)
    assert rt.WATCHDOG_MAX_WAKEUPS == 1
    assert rt._ACTIVE_SUPERVISORS == {}


def test_loop_repeated_error_is_not_a_loop_cancelled_error() -> None:
    # Given: the two terminal loop errors exist side by side
    # Then: cancellation handling can never swallow a repetition stop
    assert not issubclass(rt.LoopRepeatedError, rt.LoopCancelledError)
    assert not isinstance(rt.LoopRepeatedError(), rt.LoopCancelledError)
    assert not issubclass(rt.LoopStalledError, rt.LoopCancelledError)


def test_a_looping_background_job_is_cancelled_and_never_restarted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a live background job whose sid is the actor key, and a
    # cancel_job that records the call instead of touching a real job
    cancels: list[tuple[str, str]] = []
    monkeypatch.setattr(subagents, "is_running", lambda sid: sid == "abc123")
    monkeypatch.setattr(
        subagents,
        "cancel_job",
        lambda sid, reason="cancelled": bool(cancels.append((sid, reason))),
    )
    # A hook polls the policy on every tool call, so polling a job that has
    # not tripped must not touch its counters
    for i in range(repetition.REPEAT_TOOL_THRESHOLD):
        repetition.note_tool_call("abc123", "Bash", dict(TOOL_ARGS))
        verdict = repetition.cancel_background_loop("abc123")
        if i < repetition.REPEAT_TOOL_THRESHOLD - 1:
            assert verdict is None
            assert cancels == []

    # Then: the job is cancelled with a repetition reason, and the verdict
    # is consumed so the job cannot be "restarted" anywhere
    assert verdict is not None
    assert verdict.kind == "tool"
    assert len(cancels) == 1
    assert cancels[0][0] == "abc123"
    assert "repetition" in cancels[0][1]
    assert repetition.check("abc123") is None

    # And a supervised actor is left strictly alone — its verdict stays
    # sticky for supervised_run to poll and resume
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD):
        repetition.note_text("outo", f"{PHRASE}.")
    assert repetition.check("outo") is not None
    assert repetition.cancel_background_loop("outo") is None
    assert cancels == [cancels[0]]
    assert repetition.check("outo") is not None


class _BoomError(RuntimeError):
    """A provider-shaped failure of one attempt."""


def test_a_phrase_loop_force_stops_a_parked_attempt_and_resumes_with_the_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an attempt that says the same sentence the documented number of
    # times — the PHRASE detector, not the tool one — and then parks forever,
    # so only a force-stop can end it
    monkeypatch.setattr(rt, "WATCHDOG_CHECK_SECONDS", 0.01)
    prompts: list[str | None] = []
    repeats: list[tuple[repetition.LoopVerdict, str]] = []
    live = [
        co.Message(role="user", content="do the work"),
        co.Message(role="assistant", content="looking"),
    ]

    async def start(p: str | None, h: list[co.Message] | None) -> co.Response:
        prompts.append(p)
        if len(prompts) == 1:
            for _ in range(repetition.REPEAT_PHRASE_THRESHOLD):
                repetition.note_text("outo", f"{PHRASE}.")
            await asyncio.sleep(3600)  # never resolves on its own
        return co.Response(content="answered differently", messages=[], iterations=1)

    # When: the turn runs under the supervisor
    response = _drive(
        rt.supervised_run(
            start,
            "do the work",
            None,
            live_key="outo",
            level=0,
            on_wakeup=lambda *_args: None,
            restart_on_repetition="outo",
            on_repetition=lambda verdict, alarm: repeats.append((verdict, alarm)),
            states=_states(live=live),
        )
    )

    # Then: the phrase verdict — not the tool one — force-stopped the attempt
    # and the PHRASE alarm is the resumed attempt's user message
    assert response.content == "answered differently"
    assert prompts == ["do the work", repeats[0][1]]
    assert repeats[0][0].kind == "phrase"
    assert repeats[0][1] == repetition.alarm_text(repeats[0][0])
    assert repeats[0][1].startswith(subagents.ALARM_HEADER)
    assert rt._ACTIVE_SUPERVISORS == {}


def test_a_trip_wakes_the_supervisor_without_waiting_for_the_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a poll interval far longer than this test may take, so a restart
    # can ONLY come from the trip notification — the poll would never fire
    monkeypatch.setattr(rt, "WATCHDOG_CHECK_SECONDS", 60.0)
    attempts = 0

    async def start(p: str | None, h: list[co.Message] | None) -> co.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            for _ in range(repetition.REPEAT_TOOL_THRESHOLD):
                repetition.note_tool_call("outo", "Bash", dict(TOOL_ARGS))
            await asyncio.sleep(3600)  # only a force-stop can end this
        return co.Response(content="stopped the loop", messages=[], iterations=1)

    # When: the turn runs under the supervisor
    started = time.monotonic()
    response = _drive(
        rt.supervised_run(
            start,
            "do the work",
            None,
            live_key="outo",
            level=0,
            on_wakeup=lambda *_args: None,
            restart_on_repetition="outo",
            states=_states(),
        )
    )
    elapsed = time.monotonic() - started

    # Then: enforcement was immediate, not one 60s poll later
    assert response.content == "stopped the loop"
    assert attempts == 2
    assert elapsed < 10.0


def test_a_crashed_attempt_propagates_even_with_a_pending_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an attempt that trips the verdict and then fails — a repetition
    # restart must never mask a real provider error
    monkeypatch.setattr(rt, "WATCHDOG_CHECK_SECONDS", 0.01)
    attempts = 0

    async def start(p: str | None, h: list[co.Message] | None) -> co.Response:
        nonlocal attempts
        attempts += 1
        for _ in range(repetition.REPEAT_TOOL_THRESHOLD):
            repetition.note_tool_call("outo", "Bash", dict(TOOL_ARGS))
        raise _BoomError("provider exploded")

    # When/Then: the failure surfaces, un-restarted and un-consumed
    with pytest.raises(_BoomError):
        _drive(
            rt.supervised_run(
                start,
                "do the work",
                None,
                live_key="outo",
                level=0,
                on_wakeup=lambda *_args: None,
                restart_on_repetition="outo",
                states=_states(),
            )
        )
    assert attempts == 1
    assert repetition.check("outo") is not None
    assert rt._ACTIVE_SUPERVISORS == {}


# ── the phrase detector is CONSECUTIVE unit-run detection ──────────────────


def test_boilerplate_repeated_once_per_iteration_never_trips() -> None:
    # Given: many Bash executions with DIFFERENT commands, each announced by
    # the same one-sentence lead-in — the false positive that fired a
    # repetition alarm on completely normal multi-step work
    for i in range(3 * repetition.REPEAT_PHRASE_THRESHOLD):
        repetition.note_text("outo", f"{BOILERPLATE}. Now checking item {i}.")
        repetition.note_tool_call("outo", "Bash", {"command": f"echo {i}"})

    # Then: the interleaved segment breaks every run, so nothing accumulates
    assert repetition.check("outo") is None


def test_normal_speech_reusing_the_same_phrases_never_trips() -> None:
    # Given: the user's own counter-example, verbatim in shape — words
    # revisited in a different order while the model speaks normally. A run
    # only ever grows from a block that repeats ITSELF back to back
    speech = ["alpha", "bravo", "charlie", "delta", "alpha", "echo", "charlie",
              "alpha", "bravo", "bravo", "alpha"]
    for segment in speech:
        repetition.note_text("outo", f"{segment} and more words.")

    # Then: no verdict — that is just speaking normally
    assert repetition.check("outo") is None


def test_scattered_repeats_never_trip_however_many_there_are() -> None:
    # Given: the same segment 200 times, a different one between each pair
    for i in range(200):
        repetition.note_text("outo", f"{PHRASE}.")
        repetition.note_text("outo", f"I am checking item {i}.")

    # Then: 200 occurrences is 200 runs of one — the threshold is on the run,
    # never on the total, so this must stay silent forever
    assert repetition.check("outo") is None


def test_two_phrases_alternating_consecutively_trip_as_a_pair_unit() -> None:
    # Given: the user's own example — "A B A B A B ... A B". A single
    # segment never repeats back to back, but the PAIR does
    a, b = "first pattern of the loop", "second pattern of the loop"
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD - 1):
        repetition.note_text("outo", f"{a}. {b}.")
    assert repetition.check("outo") is None

    # When: the 50th pair completes the run
    repetition.note_text("outo", f"{a}. {b}.")

    # Then: it trips as a 2-segment unit, and the smallest unit wins the
    # verdict, so the detail names the whole repeated pair
    verdict = repetition.check("outo")
    assert verdict is not None
    assert verdict.kind == "phrase"
    assert verdict.detail == (
        f"{repetition.REPEAT_PHRASE_THRESHOLD}x: {a.lower()} {b.lower()}"
    )


def test_a_three_segment_unit_trips_at_fifty_and_not_at_forty_nine() -> None:
    # Given: a longer cycle, one segment short of the threshold
    unit = ("first clause of the loop", "second clause of the loop",
            "third clause of the loop")
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD - 1):
        repetition.note_text("outo", ". ".join(unit) + ".")

    # Then: 49 units is not the threshold
    assert repetition.check("outo") is None

    # When: the 50th unit lands
    repetition.note_text("outo", ". ".join(unit) + ".")

    # Then: it trips, and the detail renders the whole 3-segment unit
    verdict = repetition.check("outo")
    assert verdict is not None
    assert verdict.detail == f"{repetition.REPEAT_PHRASE_THRESHOLD}x: {' '.join(unit)}"


def test_an_interrupted_run_never_joins_into_one_long_run() -> None:
    # Given: 30 pairs, ONE different segment, then 30 pairs again — twice the
    # threshold in pairs overall, but never a chain of them end to end. This
    # is what separates exact block-chain counting from anything looser
    a, b = "first pattern of the loop", "second pattern of the loop"
    for _ in range(30):
        repetition.note_text("outo", f"{a}. {b}.")
    repetition.note_text("outo", "Something else entirely happened here.")
    for _ in range(30):
        repetition.note_text("outo", f"{a}. {b}.")
    assert repetition.check("outo") is None

    # And the run restarts after the interruption, so the boundary is exact:
    # 19 more pairs reach 49, and only the 50th trips
    for _ in range(19):
        repetition.note_text("outo", f"{a}. {b}.")
    assert repetition.check("outo") is None
    repetition.note_text("outo", f"{a}. {b}.")
    assert repetition.check("outo") is not None


def test_a_message_boundary_does_not_break_a_run() -> None:
    # Given: 25 identical segments, then the message boundary flush, then 25
    # more — a model alternating two phrases across messages must still trip
    for _ in range(25):
        repetition.note_text("outo", f"{PHRASE}.")
    repetition.flush_stream("outo")
    assert repetition.check("outo") is None

    # When: the next message carries it to the threshold
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD - 25):
        repetition.note_text("outo", f"{PHRASE}.")

    # Then: the run spans the boundary and trips on its 50th segment
    verdict = repetition.check("outo")
    assert verdict is not None
    assert verdict.detail == f"{repetition.REPEAT_PHRASE_THRESHOLD}x: {PHRASE.lower()}"


def test_a_message_boundary_does_not_reset_the_tool_streak() -> None:
    # Given: one identical tool call per message — 29 messages, a streak
    # that spans them by design
    for _ in range(repetition.REPEAT_TOOL_THRESHOLD - 1):
        repetition.note_tool_call("outo", "Bash", dict(TOOL_ARGS))
        repetition.flush_stream("outo")
    assert repetition.check("outo") is None

    # When/Then: the 30th, one message later, still trips the streak
    repetition.note_tool_call("outo", "Bash", dict(TOOL_ARGS))
    verdict = repetition.check("outo")
    assert verdict is not None
    assert verdict.kind == "tool"


def test_a_stall_restart_opens_a_fresh_phrase_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a wedged attempt that left threshold-minus-one segments in the
    # run, resumed with a history sanitized of that tail
    monkeypatch.setattr(rt, "WATCHDOG_CHECK_SECONDS", 0.01)
    for _ in range(repetition.REPEAT_PHRASE_THRESHOLD - 1):
        repetition.note_text("outo", f"{PHRASE}.")
    prompts: list[str | None] = []
    attempts = 0

    async def start(p: str | None, h: list[co.Message] | None) -> co.Response:
        nonlocal attempts
        attempts += 1
        prompts.append(p)
        if attempts == 1:
            await asyncio.sleep(3600)  # wedged; only the stall check ends it
        # The resumed attempt says the phrase once — the discarded tail's
        # occurrences must not count against it
        repetition.note_text("outo", f"{PHRASE}.")
        return co.Response(content="recovered", messages=[], iterations=1)

    # When: the supervisor restarts the turn from the sanitized transcript
    response = _drive(
        rt.supervised_run(
            start,
            "do the work",
            None,
            live_key="outo",
            level=0,
            on_wakeup=lambda *_args: None,
            restart_on_repetition="outo",
            states=_states(silent=API_STALL_TIMEOUT_SECONDS * 10),
        )
    )

    # Then: the discarded tail's phrases did not leak into the new run, so
    # the resumed attempt is not immediately force-stopped for repetition
    assert response.content == "recovered"
    assert attempts == 2
    assert prompts == ["do the work", "do the work"]
    assert repetition.check("outo") is None


def test_alarm_texts_are_exact_in_english() -> None:
    # Given: the two verdict kinds
    phrase = repetition.LoopVerdict(kind="phrase", detail="10x: whatever")
    tool = repetition.LoopVerdict(kind="tool", detail="30x: Bash {}")

    # Then: the delivered text is the shared contract, verbatim
    assert repetition.alarm_text(phrase) == (
        f"{subagents.ALARM_HEADER}\n"
        "Repetition detected: the same phrase has been repeated >= 50 times. "
        "The loop was force-stopped and is being resumed. Continue the work, "
        "but proceed with a DIFFERENT pattern or approach instead of repeating "
        "the same wording."
    )
    assert repetition.alarm_text(tool) == (
        f"{subagents.ALARM_HEADER}\n"
        "Repetition detected: the same tool call has been issued >= 30 times. "
        "The loop was force-stopped and is being resumed. Continue the work "
        "differently — if repeating the same action is not producing progress, "
        "try a different method."
    )
