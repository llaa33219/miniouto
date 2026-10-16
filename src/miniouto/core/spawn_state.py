"""File-based liveness markers for detached `chat` runs.

`miniouto chat --bg` detaches a real OS process, so an in-memory job registry
living inside the launching process cannot tell another process whether that
run is still going to alarm it. The marker file is that signal: one JSON file
per job under `storage.paths.SPAWN_LOG_DIR`, next to the job's stdio logs.

A run is *pending* for a session while its marker says so:

    write_marker(SpawnMarker(id=..., alarm_session=..., started=..., pid=...))
    ... the detached process works ...
    finish_marker(job_id, "done")

The waiter's rule is therefore just `pending_spawns(session)` — non-empty
means "something may still write an alarm into this session's inbox".

Liveness is a promise, not a proof. A process that is SIGKILLed (or crashes)
never calls `finish_marker`, so its marker keeps saying `"running"` forever;
but a marker written today also carries the detached process's `pid`, so the
waiter no longer has to take the promise at face value — `pending_spawns`
keeps only markers whose process is still there (`is_pid_alive`). A crashed
run therefore stops being pending at once instead of burning the caller's
whole wait bound, while a long run keeps its waiter for as long as it lives.

Known limitation: a PID is not a handle. If the run dies and the OS recycles
its PID to an unrelated process, that marker stays pending for as long as the
unrelated process lives — harmless, because the wait is bounded in turns and
(`cli/chat.py`) the wall bound is only a safety valve. Markers are otherwise
write-once per transition: `write_marker` at each status change, and never a
mutation from the reader side (`pending_spawns` is a pure filter).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from ..storage.paths import SPAWN_LOG_DIR

RUNNING = "running"
VALID_STATUSES = ("running", "done", "error", "stopped")

_SUFFIX = ".json"


@dataclass(frozen=True, slots=True)
class SpawnMarker:
    """One detached run. `alarm_session` receives the alarm; `session` is the run's own."""

    id: str
    alarm_session: str
    # The run's OWN session — where its transcript is written — which is a
    # different name from `alarm_session` (the session that receives the
    # completion message; for `--bg` the two never coincide, because a detached
    # run generates its own session instead of sharing the caller's writer).
    session: str = ""
    started: str = ""
    role: str | None = None
    style: str | None = None
    return_path: str | None = None
    status: str = RUNNING
    finished: str | None = None
    log: str | None = None
    # The detached process's OS pid — what lets a waiter tell a live run from a
    # crashed one. `None` means UNKNOWN (a marker written before this field
    # existed), and unknown must read as alive: never drop a waiter silently.
    pid: int | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "role": self.role,
            "style": self.style,
            "alarm_session": self.alarm_session,
            "session": self.session,
            "return_path": self.return_path,
            "status": self.status,
            "started": self.started,
            "finished": self.finished,
            "log": self.log,
            "pid": self.pid,
        }


def marker_path(job_id: str) -> Path:
    return SPAWN_LOG_DIR / f"{job_id}{_SUFFIX}"


def write_marker(marker: SpawnMarker) -> Path:
    """Persist `marker` atomically (tmp + `os.replace`); return its path."""

    path = marker_path(marker.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(marker.to_payload(), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def finish_marker(job_id: str, status: str, *, finished: str | None = None) -> None:
    """Stamp a finished status onto an existing marker. Idempotent; no-op if absent."""

    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    path = marker_path(job_id)
    marker = read_marker(path)
    if marker is None:
        return
    write_marker(
        replace(
            marker,
            status=status,
            finished=finished if finished is not None else datetime.now().isoformat(),
        )
    )


def is_pid_alive(pid: int | None) -> bool:
    """Is `pid` still a process? `None` (unknown) counts as alive.

    `os.kill(pid, 0)` is the cheapest cross-process probe there is, and its
    three outcomes map exactly onto the three answers: it succeeds (alive),
    raises `PermissionError` (alive, owned by another user), or raises
    `ProcessLookupError`/any other `OSError` (gone). Unknown is deliberately
    alive — an unanswerable question must not silently release a waiter.
    """

    if pid is None:
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def pending_spawns(session: str) -> list[SpawnMarker]:
    """Every marker of `session` still running AND still backed by a live process.

    A pure filter: a marker whose process is gone is simply not pending, and
    nothing on disk is touched. Unreadable files are skipped.
    """

    if not SPAWN_LOG_DIR.is_dir():
        return []
    pending: list[SpawnMarker] = []
    for path in SPAWN_LOG_DIR.glob(f"*{_SUFFIX}"):
        marker = read_marker(path)
        if (
            marker is not None
            and marker.alarm_session == session
            and marker.status == RUNNING
            and is_pid_alive(marker.pid)
        ):
            pending.append(marker)
    pending.sort(key=lambda m: m.id)
    return pending


def read_marker(path: Path) -> SpawnMarker | None:
    """Parse one marker file. Missing or corrupt content yields None, never a raise."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_pid = payload.get("pid")
        return SpawnMarker(
            id=str(payload["id"]),
            alarm_session=str(payload["alarm_session"]),
            session=str(payload.get("session") or ""),
            started=str(payload.get("started") or ""),
            role=payload.get("role"),
            style=payload.get("style"),
            return_path=payload.get("return_path"),
            status=str(payload.get("status") or RUNNING),
            finished=payload.get("finished"),
            log=payload.get("log"),
            pid=raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
