"""Cross-milestone integration: the seams only exist in the wiring.

Each milestone pinned its own unit (the inbox, the alarm hook, the repetition
watchdog, the removal bridge, the session file). None of those tests can catch
a seam BETWEEN them, so these run the real `core.chat.run_chat` — real
`resolve_runtime_from_settings`, real `build_runtime`, real coreouto loop,
real hooks, real session persistence — with exactly one thing faked: the
provider (see `tests/harness.py`).

The promises locked here, one seam per test:

1. A delegated `miniouto chat` run that finishes writes its result file, retires its
   marker and leaves exactly ONE `[SYSTEM ALARM …]` message in the session
   inbox, which reaches the model at the next turn as a synthetic user
   message — recorded as an `alarm` loop event, in the turn record and in the
   restorable history.
2. A repetition stop and a delegated run's completion share one alarm header,
   and the repetition restart delivers its alarm as the resumed attempt's
   user message — including when the repetition IS the final answer, which
   is discarded rather than returned.
3. A history repair that removes a message leaves the model's transcript,
   the record and the session file describing the same conversation.
4. A run that finishes AFTER its turn is waited for by no one: the one-shot chat
   returns with the alarm still queued, and an explicit `--alarms -c` wakeup is
   what turns it into the next prompt.
5. A second send resolves the model the session is pinned to at that moment.
6. A finished turn is returned on completion, not one watchdog poll later.
7. The retired fan-out language is gone from `src/` — the grep as a test.
8. Every delegated run persists its session — `-a` silences stdout only.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import coreouto as co
import pytest
from typer.testing import CliRunner

import harness
from miniouto.cli import app as cli_app
from miniouto.cli import chat as chat_cli
from miniouto.core import chat as chat_mod
from miniouto.core import inbox, repetition, spawn_state
from miniouto.core import runtime as rt
from miniouto.core.spawn_state import SpawnMarker
from miniouto.storage import session_config
from miniouto.storage import sessions as session_store
from miniouto.storage.paths import SUBAGENT_RESULTS_DIR

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "miniouto"

# Never-registered tool name: coreouto answers the call with a "tool not
# found" result instead of executing anything, and BEFORE_TOOL_CALL still
# fires — all the repetition detector needs, with no side effect.
UNREGISTERED_TOOL = "NoSuchTool"
REPEAT_ARGS = {"question": "same"}

RESULT_FILE_RE = re.compile(r"Result file: (?P<path>\S+)")
ALARM_BETWEEN_TURNS = (
    f"{inbox.ALARM_HEADER}\nDelegated run 'editor' (id: aaa111) finished (status: done).\n"
    "Result file: /tmp/e2e-between-turns.md"
)

runner = CliRunner()


@dataclass
class Result:
    exit_code: int
    output: str


def _chat(*args: str) -> Result:
    """Invoke the real `miniouto chat` entrypoint, argv normalisation included."""

    with runner.isolation() as streams:
        code = 0
        try:
            cli_app(["chat", *args])
        except SystemExit as exit_signal:
            code = exit_signal.code if isinstance(exit_signal.code, int) else 1
        output = streams[2].getvalue().decode("utf-8", "replace")
    return Result(code, output)


@pytest.fixture(autouse=True)
def _clean_cross_turn_state() -> Iterator[None]:
    """Every cross-turn module gets a clean slate around each test.

    Result files are part of it: this module's tests count them, and a
    delegated run writes one per invocation.
    """

    def _sweep() -> None:
        for directory in inbox.INBOX_DIR.glob("e2e-*"):
            for message in directory.iterdir() if directory.is_dir() else []:
                message.unlink(missing_ok=True)
        for result in SUBAGENT_RESULTS_DIR.glob("*.md"):
            result.unlink(missing_ok=True)

    repetition.reset()
    _sweep()
    yield
    repetition.reset()
    _sweep()


def _answer(text: str) -> co.LLMResponse:
    return co.LLMResponse(content=text)


# ── 1. the delegation round trip, end to end ──────────────────────────────────


def test_a_finished_delegated_run_is_answered_by_exactly_one_alarm_in_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a session with a working outo, and a delegated `chat` run that
    # completes — the real `cli/chat.py` delegation path, with only its own
    # turn stubbed (no model call, no network)
    session = "e2e-spawn-round-trip"
    brief = "rewrite the argument parser"
    work = "the editor rewrote three functions"
    final = "read the result file and finished the rewrite."
    harness.install(monkeypatch, lambda _req: _answer("unused"), session=session)

    def delegated_turn(opts: object, sink: object = None) -> str:
        return work

    monkeypatch.setattr(chat_cli, "run_chat", delegated_turn)
    result = _chat(
        brief, "--style", harness.STYLE_NAME, "--role", "editor",
        "--return", "--alarm-session", session,
    )

    # Then: the run acknowledged itself the two ways it can — a finished
    # marker and a result file — and armed exactly one inbox message
    assert result.exit_code == 0
    files = sorted(SUBAGENT_RESULTS_DIR.glob("*.md"))
    assert len(files) == 1
    result_path = files[0]
    assert result_path.read_text(encoding="utf-8") == work
    markers = spawn_state.pending_spawns(session)
    assert markers == [], "the marker never reached a finished status"
    assert inbox.pending_count(session) == 1
    (delivered,) = inbox.drain(session)
    assert delivered.kind == inbox.ALARM_KIND
    assert delivered.text.startswith(inbox.ALARM_HEADER)
    assert str(result_path) in delivered.text
    inbox.insert(session, delivered.text, kind=delivered.kind, source=delivered.source)

    # When: outo's next real turn runs against the scripted provider
    async def handler(req: harness.Request) -> co.LLMResponse:
        for fragment in ("read the result ", "file and finished ", "the rewrite."):
            await req.stream(fragment)
        return _answer(final)

    provider = harness.install(monkeypatch, handler, session=session)
    sink = harness.CaptureSink()
    answer = chat_mod.run_chat(
        chat_mod.ChatOptions(prompt="any news?", session=session, continue_session=True), sink
    )

    # Then: the model received exactly one synthetic user message naming it,
    # folded into this turn's own prompt rather than appended after it —
    # providers reject two consecutive user messages
    assert answer == final
    asked = provider.for_actor("outo")[0].user_texts()
    alarms = [t for t in asked if inbox.ALARM_HEADER in t]
    assert len(alarms) == 1
    assert alarms[0].startswith("any news?")
    assert str(result_path) in alarms[0]
    assert inbox.pending_count(session) == 0

    # And the record shows the same thing: one alarm event carrying the
    # inbox de-duplication handle, plus the live deltas and nothing partial
    alarm_events = sink.of_kind("alarm")
    assert len(alarm_events) == 1
    assert alarm_events[0].detail is not None
    assert alarm_events[0].detail.startswith("inbox:")
    assert [kind for kind, _text in sink.deltas] == ["text"] * 3
    assert "".join(text for _kind, text in sink.deltas) == final
    assert harness.turn_events(session) == [e.to_dict() for e in sink.events]

    # And the restorable history carries the alarm as a user message
    history_alarms = [t for t in harness.user_history(session) if inbox.ALARM_HEADER in t]
    assert len(history_alarms) == 1
    assert str(result_path) in history_alarms[0]


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
        if any(t.startswith(inbox.ALARM_HEADER) for t in req.user_texts()):
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
    assert resumed.user_texts()[-1].splitlines()[0] == inbox.ALARM_HEADER

    # And the display side shows that one alarm, attributed to the watchdog
    assert [e.to_dict() for e in sink.of_kind("alarm")] == [
        {"actor": "watchdog", "kind": "alarm", "text": expected}
    ]

    # And a delegated run's completion alarm leads with the same first line, so
    # the model cannot tell the two apart structurally: a system alarm is a
    # system alarm
    completion_alarm = chat_cli._alarm_text(
        role="editor",
        job_id="000000",
        session="e2e-completion-session",
        status="done",
        path=Path("/tmp/does-not-matter.md"),
        answer="",
    )
    assert expected.splitlines()[0] == completion_alarm.splitlines()[0]

    # And the alarm landed in the restorable history as a user message, so a
    # later resume still shows why the turn restarted
    assert expected in harness.user_history(session)


def test_a_repeating_final_answer_is_discarded_and_the_turn_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a model whose FIRST attempt ends naturally on a garbage answer —
    # the same sentence ten times across the closing reply, no tool call, so
    # the turn completes instead of looping. coreouto fires ON_THINKING /
    # ON_ITERATION (where the text is noted) microseconds before the attempt
    # task finishes, so the verdict exists with nobody left to enforce it.
    session = "e2e-repetition-final-answer"
    sentence = "I checked the parser again"
    garbage = " ".join(f"{sentence}." for _ in range(repetition.REPEAT_PHRASE_THRESHOLD))
    calls = 0

    async def handler(req: harness.Request) -> co.LLMResponse:
        nonlocal calls
        if req.actor != "outo":
            return _answer("unused")
        if any(t.startswith(inbox.ALARM_HEADER) for t in req.user_texts()):
            return _answer("read the AST instead of grepping")
        calls += 1
        return co.LLMResponse(content=garbage, thinking=garbage)

    provider = harness.install(monkeypatch, handler, session=session)
    sink = harness.CaptureSink()

    # When: the turn runs
    answer = chat_mod.run_chat(chat_mod.ChatOptions(prompt="find the bug", session=session), sink)

    # Then: the garbage answer was DISCARDED and the resumed attempt's answer
    # is what the user got — the loop never reached run_chat
    assert calls == 1
    assert answer == "read the AST instead of grepping"
    assert garbage not in answer
    assert provider.for_actor("outo")[-1].messages[-1].content.startswith(
        inbox.ALARM_HEADER
    )

    # And the model was told why, exactly once, with the shared header
    delivered = [
        t
        for t in provider.for_actor("outo")[-1].user_texts()
        if t.startswith(inbox.ALARM_HEADER)
    ]
    assert len(delivered) == 1
    assert delivered[0] == repetition.alarm_text(repetition.LoopVerdict(
        kind="phrase", detail=f"{repetition.REPEAT_PHRASE_THRESHOLD}x: {sentence.lower()}"
    ))

    # And the display side carries that one alarm, attributed to the watchdog
    assert [e.actor for e in sink.of_kind("alarm")] == ["watchdog"]
    assert len(sink.of_kind("alarm")) == 1


def test_a_repeated_final_answer_without_thinking_is_also_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a model that never emits thinking at all — the repetition is
    # in the CONTENT only, which must reach the phrase detector on its own
    session = "e2e-repetition-content-only"
    sentence = "I checked the parser again"
    garbage = " ".join(f"{sentence}." for _ in range(repetition.REPEAT_PHRASE_THRESHOLD))

    async def handler(req: harness.Request) -> co.LLMResponse:
        if req.actor != "outo":
            return _answer("unused")
        if any(t.startswith(inbox.ALARM_HEADER) for t in req.user_texts()):
            return _answer("read the AST instead of grepping")
        return co.LLMResponse(content=garbage)

    harness.install(monkeypatch, handler, session=session)
    sink = harness.CaptureSink()

    # When: the turn runs
    answer = chat_mod.run_chat(chat_mod.ChatOptions(prompt="find the bug", session=session), sink)

    # Then: the content-only repeat trips the detector and the turn resumes
    assert answer == "read the AST instead of grepping"
    assert len(sink.of_kind("alarm")) == 1


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


# ── 4. a run that outlives its turn ─────────────────────────────────────────


def test_a_run_finishing_after_the_turn_is_waited_for_by_no_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: outo delegated a brief to a detached run and moved on, so a
    # marker in this session still promises an alarm that has not arrived
    session = "e2e-between-turns"
    marker = SpawnMarker(id="aaa111", alarm_session=session, started="2026-01-01T00:00:00")
    spawn_state.write_marker(marker)
    assert spawn_state.pending_spawns(session) == [marker]

    prompts: list[chat_mod.ChatOptions] = []

    def fake_run_chat(opts: chat_mod.ChatOptions, sink: object = None) -> str:
        prompts.append(opts)
        if len(prompts) == 1:
            # The other process finishes after the turn ended: marker done,
            # alarm queued — and nobody awake to read it.
            spawn_state.finish_marker(marker.id, "done")
            inbox.insert(session, ALARM_BETWEEN_TURNS, kind=inbox.ALARM_KIND, source="chat")
        return "ok"

    monkeypatch.setattr(chat_cli, "run_chat", fake_run_chat)

    # When: the one-shot chain runs
    chat_cli.run_turn_chain(
        chat_mod.ChatOptions(prompt="review the diff", session=session), quiet=True
    )

    # Then: exactly one turn ran, and the alarm is still queued — a wakeup is
    # somebody else's business, and the marker no longer claims to owe it
    assert [o.prompt for o in prompts] == ["review the diff"]
    assert inbox.pending_count(session) == 1
    assert spawn_state.pending_spawns(session) == []


def test_the_queued_alarm_is_answered_by_a_later_wakeup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the same queued alarm, waiting for an explicit `--alarms` run
    session = "e2e-woken-between-turns"
    session_store.begin_turn(session, "review the diff")
    session_store.finish_turn(session, assistant="ok")
    inbox.insert(session, ALARM_BETWEEN_TURNS, kind=inbox.ALARM_KIND, source="chat")

    asked: list[chat_mod.ChatOptions] = []

    def fake_run_chat(opts: chat_mod.ChatOptions, sink: object = None) -> str:
        asked.append(opts)
        return "read it"

    monkeypatch.setattr(chat_cli, "run_chat", fake_run_chat)

    # When: the session is woken, continuing its history
    result = _chat("--alarms", "--continue", "--name", session)

    # Then: the queued alarm became this turn's user message, in the same
    # session, and the run finished normally
    assert [o.prompt for o in asked] == [ALARM_BETWEEN_TURNS]
    assert asked[0].session == session
    assert asked[0].continue_session is True
    assert result.exit_code == 0
    assert result.output.endswith("------finish------\nread it\n")
    assert inbox.pending_count(session) == 0


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
    # model editor does. A session's provider pin freezes the first
    # resolution, so the edit only reaches the wire once the session is
    # re-pinned — a global edit alone can no longer move a running session.
    harness.set_default_model(harness.MODEL_B)
    session_config.set_fields(session, model=harness.MODEL_B)
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


# ── 8. persistence is unconditional ──────────────────────────────────────────


def test_every_delegated_run_writes_its_session_file_even_with_answer_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a real runtime (only the provider is faked), and `-a` — which
    # silences stdout and must never silence persistence
    async def handler(req: harness.Request) -> co.LLMResponse:
        return _answer(f"worked as {req.actor}")

    harness.install(monkeypatch, handler, session="e2e-persist-boss")

    # When: a foreground delegated run and a `--bg-child` one both execute
    foreground = _chat(
        "rewrite the parser", "--style", harness.STYLE_NAME, "--role", "editor",
        "--name", "e2e-persist-fg", "--return", "--alarm-session", "e2e-persist-boss", "-a",
    )
    detached = _chat(
        "review the diff", "--style", harness.STYLE_NAME, "--role", "editor",
        "--name", "e2e-persist-child", "--return", "--alarm-session", "e2e-persist-boss",
        "--bg-child", "--job-id", "persist01", "-a",
    )

    # Then: both wrote their own session file, holding the finished turn
    assert (foreground.exit_code, detached.exit_code) == (0, 0)
    for name in ("e2e-persist-fg", "e2e-persist-child"):
        turns = session_store.load(name).turns
        assert [t.user for t in turns] == [
            "rewrite the parser" if name.endswith("fg") else "review the diff"
        ]
        assert turns[0].assistant == "worked as subagent"

    # And the alarm names the session each run actually used
    texts = [m.text for m in inbox.drain("e2e-persist-boss")]
    assert any(f"Session: e2e-persist-fg — {session_store.path_for('e2e-persist-fg')}" in t
               for t in texts)
    assert any(f"Session: e2e-persist-child — {session_store.path_for('e2e-persist-child')}" in t
               for t in texts)


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
    # a delegated chat run takes ONE prompt per invocation
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
