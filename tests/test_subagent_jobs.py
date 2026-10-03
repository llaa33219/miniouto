"""Lock the background-subagent job registry contract in core/subagents.py.

A stub Agent stands in for `coreouto.Agent`: the job engine only needs an
async `call()` whose resolution the test controls by hand, and building a
real agent would drag in provider registration the registry must not need.

The two tests that matter most are the spawn handshake (call() is entered
while the submitting turn's provider/tool registries are still intact) and
the `clear_coreouto_state()` regression lock (a job that already entered
call() keeps running across a later turn's registry rebuild).
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import coreouto as co
import pytest

from miniouto.core import runtime as rt
from miniouto.core import subagents
from miniouto.storage.paths import SUBAGENT_RESULTS_DIR

SID_PATTERN = re.compile(r"[0-9a-f]{6}")


class StubAgent:
    """Async `call()` gated by a threading.Event, with a scripted outcome."""

    def __init__(
        self,
        *,
        gate: threading.Event | None = None,
        result: str = "stub-result",
        raises: BaseException | None = None,
    ) -> None:
        self.gate = gate or threading.Event()
        self.result = result
        self.raises = raises
        self.entered = threading.Event()
        self.briefs: list[str] = []

    async def call(
        self,
        user_message: str | None = None,
        *,
        history: list[co.Message] | None = None,
    ) -> co.Response:
        self.entered.set()
        self.briefs.append(user_message or "")
        while not self.gate.is_set():
            await asyncio.sleep(0.005)
        if self.raises is not None:
            raise self.raises
        return co.Response(content=self.result, messages=[], iterations=1)


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    subagents.reset()
    yield
    subagents.reset()
    rt.set_subagent_observer(None)


def _await_alarms(count: int = 1, timeout: float = 5.0) -> list[str]:
    """Drain the alarm queue until `count` alarms arrived; return their texts."""
    deadline = time.monotonic() + timeout
    drained: list[str] = []
    while time.monotonic() < deadline:
        drained.extend(a.text for a in subagents.take_pending_alarms())
        if len(drained) >= count:
            return drained
        time.sleep(0.005)
    raise AssertionError(f"expected {count} alarm(s), got {drained}")


def _result_path(sid: str, started: datetime) -> Path:
    return SUBAGENT_RESULTS_DIR / f"{sid}-{started.strftime('%Y-%m-%d-%H-%M')}.md"


def _expected_alarm(job: subagents.Job, running: str) -> str:
    return (
        "[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]\n"
        f"Background subagent '{job.name}' (id: {job.sid}) finished (status: {job.status}).\n"
        f"Result file: {job.result_path} — read this file (e.g. with `cat`) to see what it "
        f"produced.\n"
        f"Currently running background subagents: {running}"
    )


def test_submit_returns_job_with_sid_before_the_call_resolves() -> None:
    # Given: a stub whose call() is still gated (has not resolved)
    agent = StubAgent(result="late-result")

    # When: a background job is submitted
    job = subagents.submit(agent, "editor", "fix the bug")

    # Then: the 6-hex id is known immediately, while the job is still running
    assert SID_PATTERN.fullmatch(job.sid)
    assert job.name == "editor"
    assert job.status == "running"
    assert [sid for sid, _ in subagents.running_jobs()] == [job.sid]
    assert subagents.take_pending_alarms() == []

    agent.gate.set()
    assert _await_alarms() == [_expected_alarm(job, "(none)")]
    assert job.status == "done"


def test_spawn_handshake_proves_call_was_entered_before_submit_returns() -> None:
    # Given: a stub that records entry to call() as its first statement
    agent = StubAgent()

    # When: submit() returns (no sleeping, no polling in this test)
    job = subagents.submit(agent, "researcher", "survey the repo")

    # Then: call() has already been entered and the handshake event is set
    assert agent.entered.is_set()
    assert job.spawned.is_set()
    assert agent.briefs == ["survey the repo"]

    agent.gate.set()
    _await_alarms()


def test_spawn_handshake_times_out_and_still_returns_a_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a job that never starts (its coroutine never reaches the handshake)
    monkeypatch.setattr(subagents, "SPAWN_HANDSHAKE_TIMEOUT_SECONDS", 0.05)

    async def never_starts(job: subagents.Job, agent: StubAgent) -> str:
        await asyncio.sleep(3600)
        return ""

    monkeypatch.setattr(subagents, "_run_job", never_starts)

    # When: submit() waits out the handshake timeout
    job = subagents.submit(StubAgent(), "editor", "never starts")

    # Then: it returns a Job rather than hanging forever
    assert SID_PATTERN.fullmatch(job.sid)
    assert job.status == "running"
    assert subagents.running_jobs() == [(job.sid, "editor")]


def test_completed_job_writes_result_file_at_the_exact_path_and_format() -> None:
    # Given: a job whose stub returns a known result
    agent = StubAgent(result="the editor rewrote three functions")
    job = subagents.submit(agent, "editor", "fix the bug")
    agent.gate.set()

    # When: the job completes
    _await_alarms()

    # Then: the file sits at <sid>-<YYYY-MM-DD-HH-MM>.md with the exact header
    path = _result_path(job.sid, job.started)
    assert path.is_file()
    assert job.result_path == str(path)
    text = path.read_text(encoding="utf-8")
    assert text == (
        f"# Subagent result — editor-{job.sid}\n"
        "\n"
        f"- id: {job.sid}\n"
        "- name: editor\n"
        "- status: done\n"
        f"- started: {job.started.isoformat(timespec='seconds')}\n"
        f"- finished: {job.finished.isoformat(timespec='seconds')}\n"
        "\n"
        "## Brief\n"
        "\n"
        "fix the bug\n"
        "\n"
        "## Result\n"
        "\n"
        "the editor rewrote three functions\n"
    )


def test_failed_job_writes_error_result_and_exactly_one_alarm() -> None:
    # Given: a stub that raises
    agent = StubAgent(raises=RuntimeError("provider exploded"))
    job = subagents.submit(agent, "editor", "fix the bug")
    agent.gate.set()

    # When: the job fails
    alarms = _await_alarms()

    # Then: one alarm, status error, and the error text in the result file
    assert len(alarms) == 1
    assert job.status == "error"
    assert job.error == "error: RuntimeError: provider exploded"
    text = Path(job.result_path).read_text(encoding="utf-8")
    assert "- status: error" in text
    assert "## Result\n\nerror: RuntimeError: provider exploded\n" in text
    assert alarms == [_expected_alarm(job, "(none)")]


def test_success_alarm_text_is_exact() -> None:
    # Given: one job that finishes with nothing else running
    agent = StubAgent()
    job = subagents.submit(agent, "validator", "check the diff")
    agent.gate.set()

    # When: the completion alarm is drained
    alarms = _await_alarms()

    # Then: the alarm matches the shared template exactly
    assert alarms == [
        "[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]\n"
        f"Background subagent 'validator' (id: {job.sid}) finished (status: done).\n"
        f"Result file: {job.result_path} — read this file (e.g. with `cat`) to see what it "
        f"produced.\n"
        "Currently running background subagents: (none)"
    ]


def test_alarm_lists_the_still_running_siblings() -> None:
    # Given: two jobs; the first is still gated when the second finishes
    first_agent, second_agent = StubAgent(), StubAgent()
    first = subagents.submit(first_agent, "editor", "slice one")
    second = subagents.submit(second_agent, "reviewer", "slice two")
    second_agent.gate.set()

    # When: the second job's alarm is drained
    alarms = _await_alarms()

    # Then: the first job is named as still running
    assert alarms == [_expected_alarm(second, f"editor-{first.sid}")]
    first_agent.gate.set()
    _await_alarms()


def test_five_concurrent_jobs_lose_no_alarms_or_result_files() -> None:
    # Given: five jobs, each gated by its own event
    agents = [StubAgent(result=f"result-{i}") for i in range(5)]
    jobs = [subagents.submit(agent, f"editor-{i}", f"brief {i}") for i, agent in enumerate(agents)]

    # When: they are released one by one, each completion drained separately
    alarms: list[str] = []
    for agent in agents:
        agent.gate.set()
        alarms.extend(_await_alarms())

    # Then: five distinct alarms and five distinct result files
    assert len(alarms) == 5
    assert sorted(alarm.split("id: ")[1][:6] for alarm in alarms) == sorted(j.sid for j in jobs)
    assert len({job.result_path for job in jobs}) == 5
    assert all(Path(job.result_path).is_file() for job in jobs)
    assert all(job.status == "done" for job in jobs)


def test_job_survives_clear_coreouto_state_mid_call() -> None:
    # Given: a job that has entered call() and is gated mid-flight
    from miniouto.core.providers import clear_coreouto_state

    agent = StubAgent(result="survived the rebuild")
    job = subagents.submit(agent, "editor", "long job")
    assert agent.entered.is_set()

    # When: a later turn rebuilds the runtime while the job is inside call()
    clear_coreouto_state()
    agent.gate.set()

    # Then: the job still completes and writes its result file
    _await_alarms()
    assert job.status == "done"
    text = Path(job.result_path).read_text(encoding="utf-8")
    assert "survived the rebuild" in text


def test_cancel_job_stops_the_job_and_still_reports_completion() -> None:
    # Given: a gated job
    agent = StubAgent()
    job = subagents.submit(agent, "editor", "endless job")

    # When: it is cancelled
    assert subagents.cancel_job(job.sid) is True
    alarms = _await_alarms()

    # Then: status stopped, the reason is in the file, one alarm was queued
    assert job.status == "stopped"
    assert len(alarms) == 1
    assert alarms == [_expected_alarm(job, "(none)")]
    text = Path(job.result_path).read_text(encoding="utf-8")
    assert "- status: stopped" in text
    assert "## Result\n\nstopped: cancelled\n" in text
    assert subagents.running_jobs() == []
    assert subagents.cancel_job("ffffff") is False


def test_submit_emits_a_start_observer_event_and_no_end_event() -> None:
    # Given: a lifecycle observer installed for the current turn
    events: list[tuple[str, str, str, str]] = []
    rt.set_subagent_observer(lambda phase, sid, name, text: events.append((phase, sid, name, text)))
    agent = StubAgent()

    # When: a job is submitted and completes
    job = subagents.submit(agent, "editor", "brief body text")
    agent.gate.set()
    _await_alarms()

    # Then: exactly one "start" event, and completion is signalled only by the alarm
    assert [phase for phase, _, _, _ in events] == ["start"]
    assert events[0][1] == job.sid
    assert events[0][2] == "editor"
    assert events[0][3] == "brief body text"


def test_reset_cancels_jobs_and_clears_the_registry() -> None:
    # Given: a gated job and a queued alarm
    agent = StubAgent()
    job = subagents.submit(agent, "editor", "brief")
    agent.gate.set()
    _await_alarms()

    # When: the registry is reset
    subagents.reset()

    # Then: nothing is tracked and the queue is empty
    assert subagents.running_jobs() == []
    assert subagents.take_pending_alarms() == []
    assert subagents.cancel_job(job.sid) is False


def test_format_completion_alarm_renders_from_the_job_record_alone() -> None:
    # Given: a job record that was never submitted (so nothing else is running)
    job = subagents.Job(
        sid="abc123", name="editor", brief="b", started=datetime.now(), generation=0
    )

    # Then: the template renders from the record, with "(none)" for running jobs
    assert subagents.format_completion_alarm(job) == (
        "[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]\n"
        "Background subagent 'editor' (id: abc123) finished (status: running).\n"
        "Result file: None — read this file (e.g. with `cat`) to see what it produced.\n"
        "Currently running background subagents: (none)"
    )
