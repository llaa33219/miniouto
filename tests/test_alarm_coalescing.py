"""Lock the completion-alarm delivery hook in src/miniouto/core/runtime.py.

A background subagent that finishes mid-turn (or between turns) must reach
outo's next LLM call as ONE synthetic user message. The rules under test:
coalescing (N drained alarms -> 1 message), folding (never two consecutive
user messages — the hazard `_cut_poisoned_turn` documents), depth gating
(a subagent's own loop must not steal outo's alarms), and the LoopEvent each
drained alarm produces for the session record and the TUI.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator

import coreouto as co
import pytest

from miniouto.core import chat as chat_mod
from miniouto.core import runtime as rt
from miniouto.core import subagents


class StubAgent:
    """Async `call()` gated by a threading.Event, with a scripted answer."""

    def __init__(self, *, result: str = "stub-result") -> None:
        self.gate = threading.Event()
        self.result = result

    async def call(
        self,
        user_message: str | None = None,
        *,
        history: list[co.Message] | None = None,
    ) -> co.Response:
        while not self.gate.is_set():
            await asyncio.sleep(0.005)
        return co.Response(content=self.result, messages=[], iterations=1)


class RecordingSink:
    """Minimal EventSink: keeps every loop event it is handed."""

    def __init__(self) -> None:
        self.events: list[co.Message | object] = []

    def begin_working(self) -> None:
        pass

    def update_activity(self, text: str) -> None:
        pass

    def end_working(self) -> None:
        pass

    def emit_loop_event(self, event: object) -> None:
        self.events.append(event)

    def emit_final_answer(self, content: str, session_name: str) -> None:
        pass


def _alarm(text: str, sid: str = "aaa111", name: str = "editor") -> subagents.Alarm:
    return subagents.Alarm(text=text, sid=sid, name=name)


def _queue(*alarms: subagents.Alarm, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the alarm queue with exactly `alarms`."""
    monkeypatch.setattr(subagents, "take_pending_alarms", lambda: list(alarms))


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    subagents.reset()
    yield
    subagents.reset()


def test_two_pending_alarms_land_in_one_user_message(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: two jobs finished between iterations, mid-loop
    first, second = _alarm("alarm one"), _alarm("alarm two", sid="bbb222", name="reviewer")
    _queue(first, second, monkeypatch=monkeypatch)
    messages = [
        co.Message(role="user", content="do the work"),
        co.Message(role="assistant", content="on it"),
    ]

    # When: the hook fires
    rt._make_alarm_hook(None)(messages=messages)

    # Then: exactly one new message carries both alarms, in drain order
    assert len(messages) == 3
    assert messages[-1].role == "user"
    assert messages[-1].content == "alarm one\n\nalarm two"


def test_alarm_folds_into_a_trailing_user_message(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: the turn's own prompt is still the last message (a job finished
    # before the model answered), where appending would make two in a row
    _queue(_alarm("alarm one"), _alarm("alarm two", sid="bbb222"), monkeypatch=monkeypatch)
    messages = [
        co.Message(role="user", content="do the work"),
        co.Message(role="assistant", content="on it"),
        co.Message(role="user", content="any update?"),
    ]

    # When: the hook fires
    rt._make_alarm_hook(None)(messages=messages)

    # Then: nothing is appended; both alarms are folded into the last message
    assert len(messages) == 3
    assert messages[-1].content == "any update?\n\nalarm one\n\nalarm two"


def test_alarm_appends_after_an_assistant_message(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a normal mid-loop position (last message is the model's)
    _queue(_alarm("alarm one"), monkeypatch=monkeypatch)
    messages = [
        co.Message(role="user", content="do the work"),
        co.Message(role="assistant", content="on it"),
    ]

    # When: the hook fires
    rt._make_alarm_hook(None)(messages=messages)

    # Then: one new user message is appended
    assert len(messages) == 3
    assert messages[-1].role == "user"
    assert messages[-1].content == "alarm one"


def test_nothing_is_drained_for_a_subagents_own_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: we are inside a subagent, whose loop must not steal outo's alarms
    drained: list[bool] = []
    monkeypatch.setattr(
        subagents, "take_pending_alarms", lambda: drained.append(True) or [_alarm("alarm one")]
    )
    depth_token = rt._SUBAGENT_DEPTH.set(1)
    try:
        messages = [co.Message(role="assistant", content="working")]
        rt._make_alarm_hook(None)(messages=messages)
    finally:
        rt._SUBAGENT_DEPTH.reset(depth_token)

    # Then: the queue was never touched and the history is untouched
    assert drained == []
    assert len(messages) == 1


def test_each_drained_alarm_emits_one_annotated_loop_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: two drained alarms and a sink fed by the chat dispatcher
    first = _alarm("alarm one", sid="aaa111", name="editor")
    second = _alarm("alarm two", sid="bbb222", name="reviewer")
    _queue(first, second, monkeypatch=monkeypatch)
    sink = RecordingSink()
    on_alarms = chat_mod._make_alarm_dispatcher(sink)
    messages = [co.Message(role="assistant", content="working")]

    # When: the hook fires and forwards them
    rt._make_alarm_hook(on_alarms)(messages=messages)

    # Then: one `alarm` event per drained alarm, carrying its identity
    assert [(e.kind, e.subagent_id, e.subagent_name) for e in sink.events] == [
        ("alarm", "aaa111", "editor"),
        ("alarm", "bbb222", "reviewer"),
    ]
    assert [e.text for e in sink.events] == ["alarm one", "alarm two"]
    assert all(e.actor == "outo" for e in sink.events)


def test_delivered_text_is_the_alarm_text_verbatim() -> None:
    # Given: a REAL completed job, so the text comes from the engine itself
    agent = StubAgent(result="all checks passed")
    job = subagents.submit(agent, "validator", "run the suite")
    agent.gate.set()
    deadline = time.monotonic() + 5.0
    while job.status == "running" and time.monotonic() < deadline:
        time.sleep(0.005)

    # When: its alarm is delivered to a live message list
    messages = [co.Message(role="assistant", content="working")]
    rt._make_alarm_hook(None)(messages=messages)

    # Then: the user message is exactly what `format_completion_alarm` built
    assert job.status == "done"
    assert messages[-1].content == subagents.format_completion_alarm(job)
    assert messages[-1].content.startswith(subagents.ALARM_HEADER)


def test_a_broken_sink_cannot_break_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: an event callback that raises, as a failing sink would
    _queue(_alarm("alarm one"), monkeypatch=monkeypatch)

    def boom(_alarms: list[subagents.Alarm]) -> None:
        raise RuntimeError("sink is gone")

    messages = [co.Message(role="assistant", content="working")]

    # When/Then: the alarm still reaches the model and nothing propagates
    rt._make_alarm_hook(boom)(messages=messages)
    assert messages[-1].content == "alarm one"


def test_no_pending_alarm_leaves_the_history_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an empty queue (the common case, every other iteration)
    _queue(monkeypatch=monkeypatch)
    messages = [co.Message(role="user", content="do the work")]

    # When: the hook fires
    rt._make_alarm_hook(None)(messages=messages)

    # Then: no message is added or rewritten
    assert len(messages) == 1
    assert messages[0].content == "do the work"
