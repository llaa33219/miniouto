"""Background-subagent job registry: a daemon thread owning its own event loop.

A background job must outlive the turn that spawned it, and `core/chat.py`
runs every turn under `asyncio.run(...)`, which tears down anything still detached at
turn end. So a job cannot live on the turn's event loop: it runs on a
lazily-started daemon thread that owns its own `asyncio.new_event_loop()`
and never stops.

SPAWN HANDSHAKE. coreouto's `Agent.call()` resolves the provider and its
tools BY NAME from the global registries synchronously at call() entry, and
`core/chat.py`'s `build_runtime()` wipes those registries
(`clear_coreouto_state()`) at the start of every later turn — a job that
entered `call()` mid-rebuild would die with `KeyError: provider not
registered`. `_run_job` therefore sets a per-job `threading.Event` as its
very first statement, before touching `agent.call`, and `submit()` waits on
it for `SPAWN_HANDSHAKE_TIMEOUT_SECONDS` before acknowledging. A turn cannot
reach its next `build_runtime()` before the tool handler that called
`submit()` returns, so the handshake proves `call()` was entered while the
submitting turn's registries were still intact. Resolution happens once, at
entry — that is what makes a job safe to keep running across turns.

V1 LIMITATION — no per-job stall supervisor and no job-internal event
streaming. `runtime.supervised_run` reads watchdog state that is per-turn
and cleared by every `build_runtime`, and hooks are resolved live per event,
so neither survives a cross-turn job. A wedged job keeps showing up in
`running_jobs()` until `cancel_job()` ends it; the result file plus the
completion alarm are the only channels. Both are later milestones.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from contextvars import Token
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

import coreouto as co

from ..storage.paths import SUBAGENT_RESULTS_DIR

JobStatus = Literal["running", "done", "error", "stopped"]

# How long `submit()` blocks waiting for the job to prove it entered
# `agent.call()`. On timeout the Job is still returned: the job may yet run
# or fail, and its failure surfaces in the alarm and the result file.
SPAWN_HANDSHAKE_TIMEOUT_SECONDS = 2.0

BRIEF_PREVIEW_CHARS = 160

# First line of every completion alarm. Shared verbatim with the alarm
# delivery hook and the TUI, so it lives here as the single source of truth.
ALARM_HEADER = "[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]"


class BackgroundAgent(Protocol):
    """The slice of `coreouto.Agent` a job needs (stubbed in tests)."""

    async def call(
        self,
        user_message: str | None = None,
        *,
        history: list[co.Message] | None = None,
    ) -> co.Response: ...


@dataclass(slots=True)
class Job:
    """One background subagent invocation.

    `sid` is minted at submit time, before the job runs, so the caller gets
    the actor label immediately. The record is mutated in place by the
    completion path, so a holder sees `status`/`result_path` fill in.
    """

    sid: str
    name: str
    brief: str
    started: datetime
    generation: int
    status: JobStatus = "running"
    finished: datetime | None = None
    result_path: str | None = None
    error: str | None = None
    stop_reason: str = "cancelled"
    spawned: threading.Event = field(default_factory=threading.Event, repr=False)
    future: Future[str] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Alarm:
    """One drained completion alarm, with the identity of its job.

    `text` is the whole contract with the model — exactly what
    `format_completion_alarm` produced, verbatim. `sid`/`name` ride along so
    the alarm delivery hook can attribute the message to a job without
    re-parsing the text.
    """

    text: str
    sid: str
    name: str


# One lock for both registries. Re-entrant because the completion path
# formats its alarm (which reads the running-job list) while holding it.
_LOCK = threading.RLock()
_JOBS: dict[str, Job] = {}
_PENDING_ALARMS: deque[Alarm] = deque()
_LOOP: asyncio.AbstractEventLoop | None = None
# Bumped by `reset()`: a job from an older generation may still be finishing
# on the daemon loop, and must not re-queue an alarm into the next run.
_GENERATION = 0
# One completion listener, replacing the alarm queue. The TUI installs it so a
# job finishing between turns is delivered as a user message right away instead
# of sitting invisible until the user types something. Called on the daemon
# thread with the alarm that was just queued — the callback drains the queue
# itself and must hop to its own thread.
_COMPLETION_OBSERVER: Callable[[Alarm], None] | None = None

_ContextTokens = tuple[Token[int], Token[str | None], Token[str | None]]


def submit(agent: BackgroundAgent, name: str, brief: str) -> Job:
    """Schedule a background subagent and acknowledge once it has started."""

    global _GENERATION
    with _LOCK:
        job = Job(
            sid=secrets.token_hex(3),
            name=name,
            brief=brief,
            started=datetime.now(),
            generation=_GENERATION,
        )
        # Strong ref for the job's whole life — the coroutine is the only
        # other holder, and dropping it would let the GC collect the job.
        _JOBS[job.sid] = job
    _notify_start(job)
    job.future = asyncio.run_coroutine_threadsafe(_run_job(job, agent), _ensure_loop())
    # The spawn handshake — see the module docstring.
    job.spawned.wait(SPAWN_HANDSHAKE_TIMEOUT_SECONDS)
    return job


def set_completion_observer(cb: Callable[[Alarm], None] | None) -> None:
    """Install (or clear, with None) the completion listener."""

    global _COMPLETION_OBSERVER
    with _LOCK:
        _COMPLETION_OBSERVER = cb


def take_pending_alarms() -> list[Alarm]:
    """Drain every queued completion alarm, oldest first."""

    with _LOCK:
        drained = list(_PENDING_ALARMS)
        _PENDING_ALARMS.clear()
        return drained


def running_jobs() -> list[tuple[str, str]]:
    """`(sid, name)` of every still-running job, in submit order."""

    with _LOCK:
        return [(job.sid, job.name) for job in _JOBS.values() if job.status == "running"]


def is_running(sid: str) -> bool:
    """True while `sid` names a live job — the cheap liveness query.

    `repetition.cancel_background_loop` needs to tell "a looping background
    job" from "a supervised actor" without a status or type check of its
    own, so the registry answers that directly.
    """

    with _LOCK:
        job = _JOBS.get(sid)
        return job is not None and job.status == "running"


def job_status(sid: str) -> JobStatus | None:
    """Current status of `sid`, or None when the registry never saw it.

    The display layer needs to tell three cases apart for a row it is
    displaying: a job still running, a job that finished while nobody was
    looking (so the row can close itself), and an sid from a *replayed*
    session that this process never registered. None is the third case —
    the caller must leave such a row alone.
    """

    with _LOCK:
        job = _JOBS.get(sid)
        return None if job is None else job.status


def cancel_job(sid: str, reason: str = "cancelled") -> bool:
    """Cancel a running job; its completion path still writes file + alarm.

    False means there was nothing to cancel: unknown sid, already finished, or
    a coroutine that never started.
    """

    with _LOCK:
        job = _JOBS.get(sid)
        if job is None or job.status != "running" or job.future is None:
            return False
        job.stop_reason = reason
        if not job.future.cancel():
            return False
    if not job.spawned.is_set():
        # Cancelled before the coroutine's first step, so it will never
        # report itself. `_finish` is idempotent, so a job that did slip
        # past this check cannot double-write.
        _finish(job, "stopped", f"stopped: {reason}")
    return True


def reset() -> None:
    """Test hook: cancel every job and drop all registry state.

    Bumping the generation retires the current jobs, so a completion landing
    after this call writes no file and queues no alarm. The completion
    observer goes too: a stale one would fire into the next run's UI.
    """

    global _GENERATION, _COMPLETION_OBSERVER
    with _LOCK:
        _GENERATION += 1
        _COMPLETION_OBSERVER = None
        jobs = list(_JOBS.values())
        _JOBS.clear()
        _PENDING_ALARMS.clear()
    for job in jobs:
        if job.future is not None:
            job.future.cancel()


def format_completion_alarm(job: Job) -> str:
    """Render a finished job's alarm. Exact text — other milestones share it."""

    running = ", ".join(f"{name}-{sid}" for sid, name in running_jobs()) or "(none)"
    return (
        f"{ALARM_HEADER}\n"
        f"Background subagent '{job.name}' (id: {job.sid}) finished (status: {job.status}).\n"
        f"Result file: {job.result_path} — read this file (e.g. with `cat`) to see what it "
        f"produced.\n"
        f"Currently running background subagents: {running}"
    )


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """The daemon event loop, started on first use."""

    global _LOOP
    with _LOCK:
        if _LOOP is None:
            loop = asyncio.new_event_loop()
            _LOOP = loop
            threading.Thread(
                target=_run_loop, args=(loop,), name="miniouto-subagents", daemon=True
            ).start()
        return _LOOP


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


async def _run_job(job: Job, agent: BackgroundAgent) -> str:
    # Spawn handshake, step 1: before ANY use of agent.call, so `submit()`
    # learns that call() entry is imminent.
    job.spawned.set()
    tokens = _enter_context(job)
    try:
        text = (await agent.call(job.brief)).content
    except asyncio.CancelledError:
        _finish(job, "stopped", f"stopped: {job.stop_reason}")
        raise
    except Exception as exc:
        _finish(job, "error", f"error: {type(exc).__name__}: {exc}")
        return ""
    else:
        _finish(job, "done", text)
        return text
    finally:
        _exit_context(tokens)


def _finish(job: Job, status: JobStatus, body: str) -> None:
    """The single completion path: result file, job record, one alarm.

    Idempotent per job, so a job can only ever enqueue one alarm. The
    completion observer is notified only for an alarm this call actually
    queued — a retired job (older generation) is not even a completion any
    more.
    """

    with _LOCK:
        if job.status != "running" or job.generation != _GENERATION:
            return
        job.status = status
        job.finished = datetime.now()
        if status == "error":
            job.error = body
        job.result_path = str(_write_result_file(job, body))
        alarm = Alarm(
            text=format_completion_alarm(job), sid=job.sid, name=job.name
        )
        _PENDING_ALARMS.append(alarm)
        observer = _COMPLETION_OBSERVER
    if observer is not None:
        with contextlib.suppress(Exception):
            observer(alarm)


def _write_result_file(job: Job, body: str) -> Path:
    """Write the job's markdown result file; returns its absolute path."""

    SUBAGENT_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SUBAGENT_RESULTS_DIR / f"{job.sid}-{job.started.strftime('%Y-%m-%d-%H-%M')}.md"
    finished = job.finished.isoformat(timespec="seconds") if job.finished else ""
    path.write_text(
        f"# Subagent result — {job.name}-{job.sid}\n"
        "\n"
        f"- id: {job.sid}\n"
        f"- name: {job.name}\n"
        f"- status: {job.status}\n"
        f"- started: {job.started.isoformat(timespec='seconds')}\n"
        f"- finished: {finished}\n"
        "\n"
        "## Brief\n"
        "\n"
        f"{job.brief}\n"
        "\n"
        "## Result\n"
        "\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


def _notify_start(job: Job) -> None:
    """Emit the lifecycle "start" event, if a turn installed an observer.

    Completion is deliberately NOT emitted: it is signalled exclusively
    through the alarm, so a job finishing mid-turn and one finishing between
    turns behave identically. The import is deferred because core.runtime
    imports this module, and `_notify_subagent` no-ops without an observer
    and swallows its errors — a broken sink cannot break a spawn.
    """

    from . import runtime

    runtime._notify_subagent("start", job.sid, job.name, _brief_preview(job.brief))


def _brief_preview(brief: str) -> str:
    flat = brief.strip()
    if len(flat) <= BRIEF_PREVIEW_CHARS:
        return flat
    return flat[:BRIEF_PREVIEW_CHARS] + "…"


def _enter_context(job: Job) -> _ContextTokens:
    """Set runtime's subagent ContextVars inside this job's asyncio task."""

    from . import runtime

    return (
        runtime._SUBAGENT_DEPTH.set(runtime._SUBAGENT_DEPTH.get() + 1),
        runtime._SUBAGENT_ID.set(job.sid),
        runtime._SUBAGENT_NAME.set(job.name),
    )


def _exit_context(tokens: _ContextTokens) -> None:
    from . import runtime

    depth, ident, name = tokens
    runtime._SUBAGENT_DEPTH.reset(depth)
    runtime._SUBAGENT_ID.reset(ident)
    runtime._SUBAGENT_NAME.reset(name)
