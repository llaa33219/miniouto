"""Lock the call_subagent dispatch contract in src/miniouto/core/runtime.py.

Covers name resolution, mixed-name parallel fan-out via `briefs`, same-name
fan-out via `tasks`, and per-brief error degradation. The mixed-name case is
a regression lock: eager call-level name resolution once made
`briefs=[{name: ...}, ...]` with an omitted call-level `name` fail with
"``name`` is required when multiple subagents are declared" before any
brief ran.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from miniouto.core import runtime as rt
from miniouto.storage.styles import SubagentSpec

SPECS = [
    SubagentSpec("editor", "editor prompt"),
    SubagentSpec("reviewer", "reviewer prompt"),
    SubagentSpec("validator", "validator prompt"),
]


class Recorder:
    def __init__(self) -> None:
        self.starts: list[tuple[str, float]] = []
        self.ends: list[tuple[str, float]] = []

    async def dispatch(self, name: str, brief: str, history: list | None = None) -> str:
        self.starts.append((name, time.monotonic()))
        await asyncio.sleep(0.05)
        self.ends.append((name, time.monotonic()))
        return f"{name}-result"

    @property
    def started_concurrently(self) -> bool:
        if len(self.starts) < 2:
            return False
        first_end = min(t for _, t in self.ends)
        return all(t < first_end for _, t in self.starts)


def test_mixed_briefs_fan_out_with_omitted_call_level_name() -> None:
    # Given: three briefs, each with its own persona name, no call-level `name`
    rec = Recorder()
    wrapped = rt._wrap_subagent_handler(rec.dispatch, SPECS)

    # When: one call_subagent invocation carries the whole mixed-role layer
    out = asyncio.run(
        wrapped(
            briefs=[
                {"name": "editor", "task": "brief A"},
                {"name": "reviewer", "task": "brief B"},
                {"name": "validator", "task": "brief C"},
            ]
        )
    )

    # Then: all three run concurrently and the combined result names each
    assert sorted(n for n, _ in rec.starts) == ["editor", "reviewer", "validator"]
    assert rec.started_concurrently
    assert "editor-result" in out and "reviewer-result" in out and "validator-result" in out


def test_tasks_fan_out_resolves_call_level_name_for_all_briefs() -> None:
    # Given: a call-level name and three same-shape briefs in `tasks`
    rec = Recorder()
    wrapped = rt._wrap_subagent_handler(rec.dispatch, SPECS)

    # When: one invocation fans out to the same persona
    out = asyncio.run(wrapped(name="editor", tasks=["a", "b", "c"]))

    # Then: three concurrent runs, all under "editor"
    assert [n for n, _ in rec.starts] == ["editor", "editor", "editor"]
    assert rec.started_concurrently
    assert out.count("editor-result") == 3


def test_unknown_name_degrades_per_brief_in_parallel_call() -> None:
    # Given: one typo'd persona among valid ones in a parallel fan-out
    rec = Recorder()
    wrapped = rt._wrap_subagent_handler(rec.dispatch, SPECS)

    # When: the whole layer goes out in one call
    out = asyncio.run(
        wrapped(
            briefs=[
                {"name": "editor", "task": "ok"},
                {"name": "edtor", "task": "typo"},
                {"name": "reviewer", "task": "ok 2"},
            ]
        )
    )

    # Then: the typo becomes an error section and siblings still complete
    assert "error: ValueError" in out
    assert "Unknown subagent name 'edtor'" in out
    assert "editor-result" in out and "reviewer-result" in out


def test_single_brief_unknown_name_fails_the_whole_call() -> None:
    # Given: a single brief naming a persona that does not exist
    rec = Recorder()
    wrapped = rt._wrap_subagent_handler(rec.dispatch, SPECS)

    # When/Then: the call fails and nothing is spawned
    with pytest.raises(ValueError, match="Unknown subagent name"):
        asyncio.run(wrapped(task="do it", name="edtor"))
    assert rec.starts == []


def test_omitted_name_on_single_brief_is_ambiguous_error() -> None:
    # Given: multiple personas declared and a single brief with no name
    rec = Recorder()
    wrapped = rt._wrap_subagent_handler(rec.dispatch, SPECS)

    # When/Then: ambiguity is an error listing the available names
    with pytest.raises(ValueError, match="required when multiple subagents"):
        asyncio.run(wrapped(task="do it"))
    assert rec.starts == []


def test_sole_spec_fills_an_omitted_name() -> None:
    # Given: exactly one persona declared
    rec = Recorder()
    wrapped = rt._wrap_subagent_handler(rec.dispatch, [SPECS[0]])

    # When: the brief omits `name`
    out = asyncio.run(wrapped(task="do it"))

    # Then: the sole persona fills it
    assert out == "editor-result"
    assert rec.starts == [("editor", rec.starts[0][1])]


def test_explicit_call_level_name_typo_fails_fast() -> None:
    # Given: a typo'd call-level name even though briefs name themselves
    rec = Recorder()
    wrapped = rt._wrap_subagent_handler(rec.dispatch, SPECS)

    # When/Then: an explicit call-level name is always validated
    with pytest.raises(ValueError, match="Unknown subagent name"):
        asyncio.run(
            wrapped(name="edtor", briefs=[{"name": "editor", "task": "ok"}])
        )
    assert rec.starts == []
