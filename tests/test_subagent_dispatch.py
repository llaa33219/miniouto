"""Lock the call_subagent dispatch contract in src/miniouto/core/runtime.py.

The tool is fire-and-forget: it hands one brief to the background job engine
(`core/subagents.py`) and returns an acknowledgement, never the subagent's
answer. So these tests pin three things: the exact acknowledgement text, the
absence of the retired fan-out parameters, and the unchanged name-resolution
rules (explicit name / "subagent" spec / sole spec / ambiguity error).

A stub Agent stands in for `coreouto.Agent` (same pattern as
test_subagent_jobs.py): the wrapper only hands the agent object to
`subagents.submit`, so a full agent would drag in provider registration the
dispatch contract does not care about.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
import time
from collections.abc import Iterator
from datetime import datetime

import coreouto as co
import pytest

from miniouto.core import runtime as rt
from miniouto.core import subagents
from miniouto.storage.paths import SUBAGENT_RESULTS_DIR
from miniouto.storage.styles import SubagentSpec

SPECS = [
    SubagentSpec("editor", "editor prompt"),
    SubagentSpec("reviewer", "reviewer prompt"),
    SubagentSpec("validator", "validator prompt"),
]

SID_PATTERN = re.compile(r"[0-9a-f]{6}")

# The acknowledgement template, byte for byte. `result_path_preview` is the
# results DIRECTORY: the file's own name is only known once the job finishes.
ACK_TEMPLATE = (
    "Started background subagent '{label}' (id: {sid}). It runs in the "
    "background and this returns immediately — this text is NOT the result. "
    "When it finishes you will receive a user message starting with "
    "[SYSTEM ALARM - THIS IS NOT A USER MESSAGE] naming the result file at "
    "{dir}. Do not wait or poll; continue with other work."
)


class StubAgent:
    """Async `call()` gated by a threading.Event, with a scripted answer."""

    def __init__(self, *, gate: threading.Event | None = None, result: str = "answer") -> None:
        self.gate = gate or threading.Event()
        self.result = result
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
        return co.Response(content=self.result, messages=[], iterations=1)


def _agents(**results: str) -> dict[str, StubAgent]:
    return {spec.name: StubAgent(result=results.get(spec.name, f"{spec.name}-answer")) for spec in SPECS}


def _ack(name: str, sid: str) -> str:
    return ACK_TEMPLATE.format(label=f"{name}-{sid}", sid=sid, dir=SUBAGENT_RESULTS_DIR)


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    subagents.reset()
    yield
    subagents.reset()


def _await_alarm(timeout: float = 5.0) -> subagents.Alarm:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        drained = subagents.take_pending_alarms()
        if drained:
            return drained[0]
        time.sleep(0.005)
    raise AssertionError("no alarm arrived")


def test_ack_is_the_exact_acknowledgement_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a submit that mints a known id, so the whole string is pinnable
    calls: list[tuple[str, str]] = []
    fake_job = subagents.Job(
        sid="abc123", name="editor", brief="do it", started=datetime.now(), generation=0
    )

    def fake_submit(agent: object, name: str, brief: str) -> subagents.Job:
        calls.append((name, brief))
        return fake_job

    monkeypatch.setattr(subagents, "submit", fake_submit)
    wrapped = rt._wrap_subagent_handler(_agents(), SPECS)

    # When: one brief is delegated
    out = asyncio.run(wrapped(name="editor", task="do it"))

    # Then: the ack is the template verbatim, and submit saw name + brief once
    assert out == _ack("editor", "abc123")
    assert calls == [("editor", "do it")]


def test_one_brief_starts_a_background_job_and_never_returns_its_answer() -> None:
    # Given: a gated stub agent, so the job is still running when the ack lands
    agents = _agents()
    agents["editor"].result = "the editor rewrote three functions"
    wrapped = rt._wrap_subagent_handler(agents, SPECS)

    # When: the brief is delegated
    out = asyncio.run(wrapped(name="editor", task="do it"))

    # Then: an ack carrying a fresh 6-hex id, and the stub already got the brief
    assert out.startswith("Started background subagent 'editor-")
    assert SID_PATTERN.fullmatch(out.split("'editor-")[1][:6])
    assert agents["editor"].briefs == ["do it"]

    # And the answer arrives only as an alarm + result file, never in the ack
    agents["editor"].gate.set()
    alarm = _await_alarm()
    assert "the editor rewrote three functions" not in out
    assert (alarm.sid, alarm.name) == (out.split("'editor-")[1][:6], "editor")
    assert alarm.text.startswith(subagents.ALARM_HEADER)


def test_fan_out_parameters_are_gone_from_the_handler_signature() -> None:
    # Given: the wrapped handler the model-facing schema is derived from
    wrapped = rt._wrap_subagent_handler(_agents(), SPECS)

    # When/Then: only `name` and `task` remain — the array parameters are deleted
    assert set(inspect.signature(wrapped).parameters) == {"name", "task"}


def test_unknown_name_fails_fast_and_starts_nothing() -> None:
    # Given: a typo'd persona
    wrapped = rt._wrap_subagent_handler(_agents(), SPECS)

    # When/Then: the call fails, listing the available names, and no job exists
    with pytest.raises(ValueError, match="Unknown subagent name 'edtor'"):
        asyncio.run(wrapped(name="edtor", task="do it"))
    assert subagents.running_jobs() == []


def test_omitted_name_on_a_multi_spec_style_is_an_ambiguity_error() -> None:
    # Given: several personas declared and a brief with no name
    wrapped = rt._wrap_subagent_handler(_agents(), SPECS)

    # When/Then: ambiguity is an error listing the available names
    with pytest.raises(ValueError, match="required when multiple subagents"):
        asyncio.run(wrapped(task="do it"))
    assert subagents.running_jobs() == []


def test_sole_spec_fills_an_omitted_name() -> None:
    # Given: exactly one persona declared
    agent = StubAgent()
    wrapped = rt._wrap_subagent_handler({"editor": agent}, [SPECS[0]])

    # When: the brief omits `name`
    out = asyncio.run(wrapped(task="do it"))

    # Then: the sole persona ran it
    assert out.startswith("Started background subagent 'editor-")
    assert agent.briefs == ["do it"]


def test_legacy_subagent_spec_wins_the_omitted_name_fallback() -> None:
    # Given: a legacy `<subagent>` block alongside a modern named spec
    agent = StubAgent()
    specs = [SubagentSpec("subagent", "legacy body"), SubagentSpec("editor", "editor body")]
    wrapped = rt._wrap_subagent_handler({"subagent": agent, "editor": StubAgent()}, specs)

    # When: the brief omits `name`
    out = asyncio.run(wrapped(task="do it"))

    # Then: the spec literally named `subagent` ran it
    assert out.startswith("Started background subagent 'subagent-")
    assert agent.briefs == ["do it"]


def test_blank_task_is_rejected() -> None:
    # Given: a name that resolves but no usable brief
    wrapped = rt._wrap_subagent_handler(_agents(), SPECS)

    # When/Then: whitespace-only or missing `task` raises, and nothing is spawned
    with pytest.raises(ValueError, match="requires `task`"):
        asyncio.run(wrapped(name="editor", task="   "))
    with pytest.raises(ValueError, match="requires `task`"):
        asyncio.run(wrapped(name="editor"))
    assert subagents.running_jobs() == []
