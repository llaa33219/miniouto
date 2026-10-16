"""Lock the spawn-marker contract from src/miniouto/core/spawn_state.py.

Markers are the ONLY liveness signal a separate process can read, so the three
properties that matter are: a written marker reads back intact, nothing about a
missing or corrupt file ever raises to the caller, and a marker whose `pid` is
gone is not pending — that last one is what lets a waiter stop waiting for a
crashed run instead of burning its whole time bound.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid

import pytest

from miniouto.core import spawn_state
from miniouto.core.spawn_state import SpawnMarker

SESSION = "spawn-state-test"


@pytest.fixture(autouse=True)
def _clean_markers():
    """Markers outlive a test on purpose, so drop this module's own leftovers."""

    for path in spawn_state.SPAWN_LOG_DIR.glob("*.json"):
        marker = spawn_state.read_marker(path)
        if marker is None or marker.alarm_session == SESSION:
            path.unlink(missing_ok=True)


def _dead_pid() -> int:
    """A pid that is provably gone: a finished, reaped child."""

    proc = subprocess.Popen(["true"], stdin=subprocess.DEVNULL)
    proc.wait()
    return proc.pid


def _marker(
    session: str = SESSION, status: str = "running", *, pid: int | None = None
) -> SpawnMarker:
    return SpawnMarker(
        id=uuid.uuid4().hex[:6],
        alarm_session=session,
        session="spawn-20260101-000000-abcdef",
        started="2026-01-01T00:00:00",
        role="editor",
        style="coding",
        return_path="/tmp/result.md",
        status=status,
        log="/tmp/spawn.log",
        pid=os.getpid() if pid is None else pid,
    )


def test_write_then_read_round_trips_every_field() -> None:
    # Given: a marker with every field populated
    marker = _marker()

    # When: it is written to disk
    path = spawn_state.write_marker(marker)

    # Then: it is named <job-id>.json under the spawn-log dir and reads back whole
    assert path.name == f"{marker.id}.json"
    assert path.parent == spawn_state.SPAWN_LOG_DIR
    read_back = spawn_state.read_marker(path)
    assert read_back == marker
    # And the run's own session survives, distinct from the alarm target
    assert read_back.session == "spawn-20260101-000000-abcdef"
    assert read_back.session != read_back.alarm_session


def test_a_marker_written_before_the_session_existed_reads_back_empty() -> None:
    # Given: a payload from a run whose marker predates the `session` field
    marker = _marker()
    spawn_state.write_marker(marker)
    path = spawn_state.marker_path(marker.id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("session")
    path.write_text(json.dumps(payload), encoding="utf-8")

    # When: it is read
    read_back = spawn_state.read_marker(path)

    # Then: it still parses, with no session — never a KeyError
    assert read_back is not None
    assert read_back.session == ""


def test_finish_marker_stamps_a_finish_time() -> None:
    # Given: a running marker
    path = spawn_state.write_marker(_marker())

    # When: the detached run finishes
    spawn_state.finish_marker(path.stem, "done")

    # Then: it no longer claims to be running and carries a finish timestamp
    finished = spawn_state.read_marker(path)
    assert finished.status == "done"
    assert finished.finished is not None


def test_finish_marker_is_idempotent_and_ignores_missing_job_ids() -> None:
    # Given: a marker finished once with an explicit timestamp
    path = spawn_state.write_marker(_marker())
    spawn_state.finish_marker(path.stem, "error", finished="2026-01-01T00:00:01")

    # When: it is finished again, and an unknown job id is finished too
    spawn_state.finish_marker(path.stem, "error", finished="2026-01-01T00:00:01")
    spawn_state.finish_marker("nosuch", "error")

    # Then: nothing changed and nothing raised
    finished = spawn_state.read_marker(path)
    assert (finished.status, finished.finished) == ("error", "2026-01-01T00:00:01")


def test_pending_spawns_filters_by_session_and_running_status() -> None:
    # Given: markers of this session and another, running and finished
    running_a, running_b = _marker(), _marker()
    spawn_state.write_marker(running_a)
    spawn_state.write_marker(running_b)
    spawn_state.write_marker(_marker(session="another-session"))
    spawn_state.write_marker(_marker(status="done"))

    # When: this session's pending runs are asked for
    pending = {m.id for m in spawn_state.pending_spawns(SESSION)}

    # Then: only its own still-running markers come back
    assert pending == {running_a.id, running_b.id}


def test_a_finished_marker_stops_being_pending() -> None:
    # Given: a marker that is running, then finished
    marker = _marker()
    spawn_state.write_marker(marker)

    # When: the run finishes
    spawn_state.finish_marker(marker.id, "done")

    # Then: it is no longer owed to the session
    assert marker.id not in {m.id for m in spawn_state.pending_spawns(SESSION)}


def test_corrupt_and_missing_markers_read_as_none() -> None:
    # Given: a marker file that is not JSON at all
    path = spawn_state.write_marker(_marker())
    path.write_text("}{ not json", encoding="utf-8")

    # When / Then: unreadable content and a missing file both yield None
    assert spawn_state.read_marker(path) is None
    assert spawn_state.read_marker(path.with_name("nosuch.json")) is None


def test_pending_spawns_skips_corrupt_marker_files() -> None:
    # Given: a corrupt marker file in the spawn-log dir
    corrupt = spawn_state.SPAWN_LOG_DIR / "corrupt.json"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("not json", encoding="utf-8")

    # When / Then: it never surfaces and never raises
    assert all(m.id != "corrupt" for m in spawn_state.pending_spawns(SESSION))


# ── liveness: the pid is the proof behind "running" ──────────────────────────


def test_the_pid_round_trips_through_the_payload() -> None:
    # Given: a marker stamped with the detached process's pid
    marker = _marker(pid=os.getpid())

    # When: it is written and read back
    path = spawn_state.write_marker(marker)
    payload = json.loads(path.read_text(encoding="utf-8"))

    # Then: the pid survives the round trip as an int, not a string
    assert payload["pid"] == os.getpid()
    assert spawn_state.read_marker(path) == marker


def test_a_payload_written_before_the_pid_existed_reads_back_unknown() -> None:
    # Given: a marker file from a run whose format predates `pid`
    marker = _marker()
    path = spawn_state.write_marker(marker)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("pid")
    path.write_text(json.dumps(payload), encoding="utf-8")

    # When: it is read
    read_back = spawn_state.read_marker(path)

    # Then: it parses with no pid — never a KeyError
    assert read_back is not None
    assert read_back.pid is None

    # And unknown reads as ALIVE: a legacy marker still holds its waiter
    assert spawn_state.is_pid_alive(read_back.pid) is True
    assert marker.id in {m.id for m in spawn_state.pending_spawns(SESSION)}


def test_is_pid_alive_distinguishes_a_live_process_from_a_reaped_one() -> None:
    # Given: this very process, and a child that has finished and been reaped
    dead = _dead_pid()

    # When / Then: the probe agrees with the process table in both directions
    assert spawn_state.is_pid_alive(os.getpid()) is True
    assert spawn_state.is_pid_alive(dead) is False


def test_is_pid_alive_treats_an_unknown_pid_as_alive() -> None:
    # When / Then: no pid is a question we cannot answer, never a dropped waiter
    assert spawn_state.is_pid_alive(None) is True


def test_a_running_marker_whose_process_died_is_not_pending() -> None:
    # Given: a crashed run — still "running" on disk, but its process is gone
    crashed = _marker(pid=_dead_pid())
    alive = _marker()
    spawn_state.write_marker(crashed)
    spawn_state.write_marker(alive)

    # When: the session's pending runs are asked for
    pending = {m.id for m in spawn_state.pending_spawns(SESSION)}

    # Then: only the run still backed by a process is owed to it
    assert crashed.id not in pending
    assert pending == {alive.id}

    # And the filter is pure: the dead marker is untouched on disk
    assert spawn_state.read_marker(spawn_state.marker_path(crashed.id)).status == "running"
