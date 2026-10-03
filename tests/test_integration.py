"""Cross-milestone integration: the seams only exist in the wiring.

Each milestone pinned its own unit (the job registry, the alarm hook, the
repetition watchdog, the removal bridge, the session file). None of those
tests can catch a seam BETWEEN them, so these run the real
`core.chat.run_chat` — real `resolve_runtime_from_settings`, real
`build_runtime`, real coreouto loop, real hooks, real session persistence
— with exactly one thing faked: the provider (see `tests/harness.py`).

The promises locked here, one seam per test:

1. `call_subagent` is fire-and-forget AND its answer is not lost: ack
   first, result file, then exactly one `[SYSTEM ALARM …]` user message
   delivered at the next LLM call, recorded as an `alarm` loop event, in
   the turn record and in the restorable history.
2. A repetition stop and a job completion share one alarm header, and the
   repetition restart delivers its alarm as the resumed attempt's user
   message.
3. A history repair that removes a message leaves the model's transcript,
   the record and the session file describing the same conversation.
4. A background job survives the end of its turn and is answered in the
   NEXT one.
5. A second send resolves the settings as they are at that moment.
6. A finished turn is returned on completion, not one watchdog poll later.
7. The retired fan-out language is gone from `src/` — the grep as a test.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import coreouto as co
import pytest

import harness
from miniouto.core import chat as chat_mod
from miniouto.core import repetition, subagents
from miniouto.core import runtime as rt
from miniouto.storage.paths import SUBAGENT_RESULTS_DIR

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "miniouto"

# Never-registered tool name: coreouto answers the call with a "tool not
# found" result instead of executing anything, and BEFORE_TOOL_CALL still
# fires — all the repetition detector needs, with no side effect.
UNREGISTERED_TOOL = "NoSuchTool"
REPEAT_ARGS = {"question": "same"}

RESULT_FILE_RE = re.compile(r"Result file: (?P<path>\S+)")
RESULT_FILE_NAME_RE = re.compile(r"[0-9a-f]{6}-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}\.md")


@pytest.fixture(autouse=True)
def _clean_cross_turn_state() -> Iterator[None]:
    """Every cross-turn module gets a clean slate around each test."""

    subagents.reset()
    repetition.reset()
    yield
    subagents.reset()
    repetition.reset()
    rt.set_subagent_observer(None)


def _answer(text: str) -> co.LLMResponse:
    return co.LLMResponse(content=text)


# ── 1. the background-subagent round trip, end to end ───────────────────────


def test_a_background_subagent_is_acknowledged_then_answered_by_one_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a turn where outo delegates a brief, and the job is held back
    # so it cannot finish before the ack has been returned
    session = "e2e-alarm-round-trip"
    brief = "rewrite the argument parser"
    work = "the editor rewrote three functions"
    final = "the editor finished the rewrite."
    release = threading.Event()
    running_at_ack: list[list[tuple[str, str]]] = []

    async def on_tool_result(name: str, result: co.ToolResult) -> None:
        # Fires while the tool result is handled: after the ack was
        # returned, before the next LLM call. That is the window in which
        # "the answer has come back" must still be false.
        if name != "call_subagent":
            return
        running_at_ack.append(subagents.running_jobs())
        release.set()
        # Deterministic ordering: the job must have finished (alarm queued)
        # before the loop's next BEFORE_LLM_CALL delivers it.
        await harness.wait_for_alarm_async()

    async def handler(req: harness.Request) -> co.LLMResponse:
        if req.actor == "subagent":
            while not release.is_set():
                await asyncio.sleep(0.005)
            return _answer(work)
        if req.index == 0:
            return co.LLMResponse(
                content="delegating the parser rewrite",
                tool_calls=[
                    co.ToolCall(
                        id="c1",
                        name="call_subagent",
                        arguments={"name": "editor", "task": brief},
                    )
                ],
            )
        for fragment in ("the editor ", "finished ", "the rewrite."):
            await req.stream(fragment)
        return _answer(final)

    provider = harness.install(
        monkeypatch, handler, session=session, after_tool_call=on_tool_result
    )
    sink = harness.CaptureSink()

    # When: the turn runs for real
    answer = chat_mod.run_chat(chat_mod.ChatOptions(prompt="fix the parser", session=session), sink)

    # Then: the tool returned an ack, and the job was still running when it did
    assert answer == final
    assert running_at_ack and running_at_ack[0], "the ack came back after the job finished"
    acks = [
        m.content
        for m in provider.for_actor("outo")[1].messages
        if m.role == "tool" and "Started background subagent" in m.content
    ]
    assert len(acks) == 1
    assert acks[0].startswith("Started background subagent 'editor-")
    assert work not in acks[0], "the ack must never carry the subagent's answer"

    # And the job wrote its result file, named <sid>-<YYYY-MM-DD-HH-MM>.md
    alarm_events = sink.of_kind("alarm")
    assert len(alarm_events) == 1
    alarm_event = alarm_events[0]
    sid = alarm_event.subagent_id
    assert sid is not None
    assert alarm_event.subagent_name == "editor"
    match = RESULT_FILE_RE.search(alarm_event.text)
    assert match is not None
    result_path = Path(match.group("path"))
    assert result_path.parent == SUBAGENT_RESULTS_DIR
    assert RESULT_FILE_NAME_RE.fullmatch(result_path.name)
    assert result_path.name.startswith(f"{sid}-")
    result_file = result_path.read_text(encoding="utf-8")
    assert brief in result_file
    assert work in result_file

    # And exactly one synthetic user message reached the model, naming that file
    delivered = [
        text
        for text in provider.for_actor("outo")[1].user_texts()
        if text.startswith(subagents.ALARM_HEADER)
    ]
    assert len(delivered) == 1
    assert str(result_path) in delivered[0]

    # And the record shows the same thing: a subagent_start row, the alarm,
    # the live deltas — and no delta fragment in the file
    assert [e.subagent_id for e in sink.of_kind("subagent_start")] == [sid]
    assert [kind for kind, _text in sink.deltas] == ["text"] * 3
    assert "".join(text for _kind, text in sink.deltas) == final
    assert harness.turn_events(session) == [e.to_dict() for e in sink.events]
    assert "the editor " not in json.dumps(harness.turn_events(session))

    # And the restorable history carries the alarm as a user message
    alarm_history = [t for t in harness.user_history(session) if subagents.ALARM_HEADER in t]
    assert len(alarm_history) == 1
    assert str(result_path) in alarm_history[0]


# ── 2. repetition: the shared header, delivered as the resumed prompt ───────


def test_a_repeating_model_is_resumed_with_the_shared_alarm_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a model that issues the same tool call over and over, so the
    # repetition detector trips, plus a fast supervisor poll so the restart
    # is observed instead of raced
    session = "e2e-repetition-restart"
    monkeypatch.setattr(rt, "WATCHDOG_CHECK_SECONDS", 0.01)
    looped = 0

    async def handler(req: harness.Request) -> co.LLMResponse:
        if req.actor != "outo":
            return _answer("unused")
        if any(t.startswith(subagents.ALARM_HEADER) for t in req.user_texts()):
            return _answer("switched to reading the parser")
        nonlocal looped
        looped += 1
        if looped <= repetition.REPEAT_TOOL_THRESHOLD:
            return co.LLMResponse(
                # The text differs every iteration, so the PHRASE detector
                # cannot be the one that trips — this test is about the tool
                # detector and its exact alarm text.
                content=f"checking pass {looped}",
                tool_calls=[
                    co.ToolCall(
                        id=f"c{looped}",
                        name=UNREGISTERED_TOOL,
                        arguments=dict(REPEAT_ARGS),
                    )
                ],
            )
        # Past the threshold the verdict is sticky, so the supervisor is
        # what ends this attempt. Park here: the restart must be the only
        # way out, which is what makes the delivery below deterministic.
        for _ in range(2_000):
            await asyncio.sleep(0.005)
        raise AssertionError("the supervisor never force-stopped the looping attempt")

    provider = harness.install(monkeypatch, handler, session=session)
    sink = harness.CaptureSink()

    # When: the turn runs
    answer = chat_mod.run_chat(chat_mod.ChatOptions(prompt="find the bug", session=session), sink)

    # Then: the exact repetition alarm — not a paraphrase — is the resumed
    # attempt's user message
    expected = repetition.alarm_text(
        repetition.LoopVerdict(
            kind="tool",
            detail=(
                f"{repetition.REPEAT_TOOL_THRESHOLD}x: {UNREGISTERED_TOOL} "
                f'{{"question":"{REPEAT_ARGS["question"]}"}}'
            ),
        )
    )
    assert answer == "switched to reading the parser"
    resumed = provider.for_actor("outo")[-1]
    assert resumed.user_texts()[-1] == expected
    assert resumed.user_texts()[-1].splitlines()[0] == subagents.ALARM_HEADER

    # And the display side shows that one alarm, attributed to the watchdog
    assert [e.to_dict() for e in sink.of_kind("alarm")] == [
        {"actor": "watchdog", "kind": "alarm", "text": expected}
    ]

    # And both alarm sources share one first line, so the model cannot tell
    # them apart structurally: a system alarm is a system alarm
    completion_alarm = subagents.format_completion_alarm(
        subagents.Job(sid="000000", name="editor", brief="b", started=datetime.now(), generation=0)
    )
    assert expected.splitlines()[0] == completion_alarm.splitlines()[0]

    # And the alarm landed in the restorable history as a user message, so a
    # later resume still shows why the turn restarted
    assert expected in harness.user_history(session)


# ── 3. repair: model, record and file agree ─────────────────────────────────


class _BadToolCallError(Exception):
    """A provider-shaped 400 the openai error rules classify as `retry`."""

    status_code = 400


def test_a_repaired_turn_leaves_model_record_and_file_agreeing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a turn whose first tool call is malformed (`arguments: null`),
    # so the provider 400s and the registered repair hook must cut it
    session = "e2e-repair-prune"
    missing = "/nonexistent/miniouto-e2e-prune.png"

    async def handler(req: harness.Request) -> co.LLMResponse:
        if req.actor != "outo":
            return _answer("unused")
        if req.index == 0:
            return co.LLMResponse(
                content="looking at the logo",
                tool_calls=[co.ToolCall(id="c1", name="Image", arguments={"file_path": missing})],
                raw={"poisoned": True},
            )
        if req.index == 1:
            raise _BadToolCallError("400 invalid request: tool call arguments must be an object")
        return _answer("recovered without re-issuing the bad call")

    provider = harness.install(monkeypatch, handler, session=session)
    sink = harness.CaptureSink()

    # When: the turn runs and the provider rejects the poisoned history
    answer = chat_mod.run_chat(
        chat_mod.ChatOptions(prompt="look at the logo", session=session), sink
    )

    # Then: the rows the user actually watched were retracted from the
    # record — the sink is append-only, the file is not
    assert answer == "recovered without re-issuing the bad call"
    assert [e.tool_name for e in sink.of_kind("tool")] == ["Image"]
    assert sink.pruned, "no message removal ever reached the sink"
    recorded = harness.turn_events(session)
    assert not [e for e in recorded if e.get("tool_name") == "Image"]
    assert missing not in json.dumps(recorded)

    # And what the model is now sent is the repaired transcript: the cut
    # assistant turn and its result are gone, with a note in their place
    retried = provider.for_actor("outo")[2].messages
    assert not [m for m in retried if m.role == "assistant" and m.tool_calls]
    assert not [m for m in retried if m.role == "tool"]
    asked = [m.content for m in retried if m.role == "user"]
    assert len(asked) == 1
    assert asked[0].startswith("look at the logo")
    assert "was rejected by the provider" in asked[0]

    # And the repaired transcript is what the file will reload next turn
    assert harness.user_history(session) == asked


# ── 4. a job that outlives its turn ─────────────────────────────────────────


def test_a_job_finishing_between_turns_is_answered_in_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: turn 1 delegates and moves on WITHOUT waiting for the job
    session = "e2e-between-turns"
    release = threading.Event()
    in_first_turn = True

    async def handler(req: harness.Request) -> co.LLMResponse:
        if req.actor == "subagent":
            while not release.is_set():
                await asyncio.sleep(0.005)
            return _answer("the editor approved the diff")
        if in_first_turn:
            if req.index == 0:
                return co.LLMResponse(
                    content="delegating the review",
                    tool_calls=[
                        co.ToolCall(
                            id="c1",
                            name="call_subagent",
                            arguments={
                                "name": "editor",
                                "task": "review the diff before I continue",
                            },
                        )
                    ],
                )
            return _answer("moved on while the review runs")
        return _answer("read the review and finished the work")

    provider = harness.install(monkeypatch, handler, session=session)
    first_sink = harness.CaptureSink()

    # When: turn 1 ends with the job still running
    first = chat_mod.run_chat(
        chat_mod.ChatOptions(prompt="review the diff", session=session), first_sink
    )
    assert first == "moved on while the review runs"
    assert subagents.running_jobs(), "the job died with the turn"
    assert not first_sink.of_kind("alarm")
    assert not [t for t in harness.user_history(session) if subagents.ALARM_HEADER in t]

    # And the job finishes only now — between the two turns
    release.set()
    harness.wait_for_alarm()

    # Then: the NEXT turn delivers it, exactly once, to the model
    in_first_turn = False
    second_sink = harness.CaptureSink()
    second = chat_mod.run_chat(
        chat_mod.ChatOptions(prompt="any news?", session=session, continue_session=True),
        second_sink,
    )
    assert second == "read the review and finished the work"
    asked = provider.for_actor("outo")[2].user_texts()
    delivered = [t for t in asked if subagents.ALARM_HEADER in t]
    assert len(delivered) == 1
    # Folded into the fresh prompt rather than appended after it: providers
    # reject two consecutive user messages.
    assert delivered[0].startswith("any news?")
    assert asked[-1] == delivered[0]

    # And the display side shows the completion in turn 2 only
    assert len(second_sink.of_kind("alarm")) == 1
    assert not first_sink.of_kind("alarm")
    assert len([t for t in harness.user_history(session) if subagents.ALARM_HEADER in t]) == 1
    assert [e["kind"] for e in harness.turn_events(session, 0)] == [
        "response",
        "context",
        "subagent_start",
        "context",
    ]
    assert [e["kind"] for e in harness.turn_events(session, 1)] == ["alarm", "context"]


# ── 5. send-time settings ───────────────────────────────────────────────────


def test_a_second_send_resolves_the_model_stored_at_that_moment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: one installed provider and a first turn
    session = "e2e-send-time-settings"

    async def handler(req: harness.Request) -> co.LLMResponse:
        return _answer(f"answered by {req.model}")

    provider = harness.install(monkeypatch, handler, session=session, model=harness.MODEL_A)
    sink = harness.CaptureSink()

    # When: it is sent, the model of the moment is used
    first = chat_mod.run_chat(chat_mod.ChatOptions(prompt="one", session=session), sink)
    assert first == f"answered by {harness.MODEL_A}"

    # And the default model is edited between the two sends, as the TUI
    # model editor does — the second send must not reuse the first turn's
    # resolution
    harness.set_default_model(harness.MODEL_B)
    second = chat_mod.run_chat(chat_mod.ChatOptions(prompt="two", session=session), sink)

    # Then: the newer setting wins on the wire, and the first turn is
    # untouched
    assert second == f"answered by {harness.MODEL_B}"
    assert provider.models() == [harness.MODEL_A, harness.MODEL_B]
    assert sink.answers == [
        (f"answered by {harness.MODEL_A}", session),
        (f"answered by {harness.MODEL_B}", session),
    ]


# ── 6. the supervisor never taxes a turn that already finished ──────────────


def test_a_finished_turn_is_not_delayed_by_the_watchdog_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a supervisor whose poll interval is far longer than the turn
    monkeypatch.setattr(rt, "WATCHDOG_CHECK_SECONDS", 30.0)
    session = "e2e-no-poll-tail"

    async def handler(req: harness.Request) -> co.LLMResponse:
        return _answer("answered immediately")

    harness.install(monkeypatch, handler, session=session)
    sink = harness.CaptureSink()

    # When: the turn runs
    started = time.monotonic()
    answer = chat_mod.run_chat(chat_mod.ChatOptions(prompt="hi", session=session), sink)

    # Then: it returned on completion, not one poll interval later — a
    # finished attempt is supervised, never waited for
    assert answer == "answered immediately"
    assert time.monotonic() - started < 5.0


# ── 7. the retired fan-out contract is gone from src/ ───────────────────────


def _src_lines() -> list[tuple[str, int, str]]:
    return [
        (str(path.relative_to(SRC_ROOT)), number, line)
        for path in sorted(SRC_ROOT.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
    ]


@pytest.mark.parametrize("pattern", [r"briefs", r"tasks="], ids=["briefs", "tasks="])
def test_no_fan_out_parameter_language_survives_in_src(pattern: str) -> None:
    # Given: every line of every module under src/miniouto
    hits = [
        f"{path}:{number}: {line.strip()}"
        for path, number, line in _src_lines()
        if re.search(pattern, line)
    ]

    # Then: nothing names the retired multi-brief fan-out parameter —
    # `call_subagent` takes ONE brief per call
    assert hits == []


def test_no_synchronous_subagent_description_survives_in_src() -> None:
    # Given: every line of every module under src/miniouto
    hits = [
        f"{path}:{number}: {line.strip()}"
        for path, number, line in _src_lines()
        if "blocks until" in line
    ]

    # Then: the model is never told to wait for a subagent that is not
    # there any more
    assert hits == []
