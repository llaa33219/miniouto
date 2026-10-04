"""One-shot `miniouto chat` must not orphan background subagents.

A background job lives on a daemon thread, so it survives `run_chat`'s
`asyncio.run` teardown but dies at process exit. A one-shot chat that just
returned after its single turn therefore killed every still-running job
mid-flight and dropped its completion alarm (the alarm deque is in-memory).
`cli.chat.run_turn_chain` therefore waits for the jobs and chains their alarms
as follow-up turns, so the model reads the results and finishes the task.

The chain must stay bounded: automation must not hang forever on a wedged
job, and it must not spin on an alarm that never stops arriving. The rules
under test: one follow-up turn per drained batch, alarms coalesced into one
prompt, `FOLLOWUP_MAX_TURNS` as a turn cap, `FOLLOWUP_WAIT_SECONDS` as a total
wait deadline, and every follow-up continuing the same session.
"""

from __future__ import annotations

from typing import Any

import pytest

from miniouto.cli import chat as chat_mod
from miniouto.core import subagents
from miniouto.core.chat import ChatOptions

BASE_OPTS = ChatOptions(prompt="do the work", session="chat-test")


def _alarm(text: str, sid: str = "aaa111", name: str = "editor") -> subagents.Alarm:
    return subagents.Alarm(text=text, sid=sid, name=name)


def _script_alarms(
    monkeypatch: pytest.MonkeyPatch, batches: list[list[subagents.Alarm]]
) -> None:
    """Feed `batches` to successive drains; the last one repeats forever."""

    queue = list(batches)

    def take() -> list[subagents.Alarm]:
        return queue.pop(0) if len(queue) > 1 else list(queue[0])

    monkeypatch.setattr(subagents, "take_pending_alarms", take)
    monkeypatch.setattr(subagents, "running_jobs", list)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[ChatOptions]:
    """Stub the real turn runner and record the options it was called with."""

    recorded: list[ChatOptions] = []

    def fake_run_chat(opts: ChatOptions, sink: Any = None) -> None:
        recorded.append(opts)

    monkeypatch.setattr(chat_mod, "run_chat", fake_run_chat)
    return recorded


def test_one_alarm_batch_runs_exactly_one_followup_turn(
    calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the main turn queued one completion alarm
    _script_alarms(monkeypatch, [[_alarm("alarm one")], []])

    # When: the whole one-shot chain runs
    chat_mod.run_turn_chain(BASE_OPTS, quiet=True)

    # Then: the main turn, then one follow-up whose prompt IS the alarm
    assert [o.prompt for o in calls] == ["do the work", "alarm one"]
    assert calls[1].continue_session is True
    assert calls[1].session == "chat-test"


def test_two_alarms_coalesce_into_one_followup_prompt(
    calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: two jobs finished before the model answered again
    _script_alarms(
        monkeypatch, [[_alarm("alarm one"), _alarm("alarm two", sid="bbb222")], []]
    )

    # When: the chain runs
    chat_mod.run_turn_chain(BASE_OPTS, quiet=True)

    # Then: both alarms ride in one follow-up turn, blank-line joined
    assert len(calls) == 2
    assert calls[1].prompt == "alarm one\n\nalarm two"


def test_followup_turn_count_is_capped(
    calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    # Given: alarms keep arriving and never go quiet
    _script_alarms(monkeypatch, [[_alarm("alarm one")]])

    # When: the chain runs
    chat_mod.run_turn_chain(BASE_OPTS, quiet=True)

    # Then: 1 main + FOLLOWUP_MAX_TURNS follow-ups, then it stops and says so
    assert len(calls) == chat_mod.FOLLOWUP_MAX_TURNS + 1
    assert str(chat_mod.FOLLOWUP_MAX_TURNS) in capsys.readouterr().err


def test_wait_deadline_expires_instead_of_hanging(
    calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    # Given: a job never finishes and no alarm ever arrives
    monkeypatch.setattr(subagents, "take_pending_alarms", list)
    monkeypatch.setattr(subagents, "running_jobs", lambda: [("aaa111", "editor")])
    monkeypatch.setattr(chat_mod, "FOLLOWUP_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(chat_mod.time, "sleep", lambda _seconds: None)

    # When/Then: only the main turn ran, and the deadline was reported
    chat_mod.run_turn_chain(BASE_OPTS, quiet=True)
    err = capsys.readouterr().err
    assert [o.prompt for o in calls] == ["do the work"]
    assert "aaa111" in err or "editor" in err


def test_chain_polls_until_an_alarm_shows_up(
    calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the job is still running for two polls, then its alarm arrives
    take_calls = {"n": 0}

    def take() -> list[subagents.Alarm]:
        take_calls["n"] += 1
        return [_alarm("alarm one")] if take_calls["n"] == 3 else []

    run_calls = {"n": 0}

    def running() -> list[tuple[str, str]]:
        run_calls["n"] += 1
        return [] if run_calls["n"] >= 3 else [("aaa111", "editor")]

    monkeypatch.setattr(subagents, "take_pending_alarms", take)
    monkeypatch.setattr(subagents, "running_jobs", running)
    slept: list[float] = []
    monkeypatch.setattr(
        chat_mod.time, "sleep", lambda seconds: slept.append(seconds)
    )

    # When: the chain runs
    chat_mod.run_turn_chain(BASE_OPTS, quiet=True)

    # Then: it waited (polling) before running the alarm's follow-up turn
    assert slept == [chat_mod.FOLLOWUP_POLL_SECONDS] * 2
    assert [o.prompt for o in calls] == ["do the work", "alarm one"]


def test_a_quiet_chain_prints_nothing_on_stdout(
    calls: list[ChatOptions], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    # Given: `--answer-only` semantics (quiet sink) and a pending alarm
    _script_alarms(monkeypatch, [[_alarm("alarm one")], []])

    # When: the chain runs
    chat_mod.run_turn_chain(BASE_OPTS, quiet=True)

    # Then: no follow-up banner — the sink owns all visible output
    assert capsys.readouterr().out == ""
