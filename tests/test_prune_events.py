"""Removal tracking: display/file/perceived consistency after a repair.

When history repair (`_cut_poisoned_turn`) or stall recovery
(`sanitize_history`) removes a message from the live conversation, the
display record must lose the same message's events. Otherwise the TUI
still shows a tool call the model no longer has, and the session file's
`turn["events"]` describes a conversation that never happened.

The mechanism is `LoopEvent.msg_ref = str(id(message))` — an identity
that is only meaningful inside the process that produced it, which is
exactly the scope here: pruning is a LIVE-turn repair, never a
cross-process rehydration (ids from a previous process are meaningless
and are simply never matched, so a reloaded session just keeps the
events it already recorded).

The summarizer is deliberately EXCLUDED: it compacts history wholesale
because the context window demands it, while the display transcript is
the human record of what happened. Compaction must never erase rows.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import coreouto as co
import pytest
from textual.widgets import Static

from miniouto.cli.tui import ChatTUI, TUIEventSink, _RowWindow
from miniouto.core import chat as chat_mod
from miniouto.core import context as context_mod
from miniouto.core import runtime as rt
from miniouto.core.events import ConsoleEventSink, EventSink, LoopEvent, NullSink
from miniouto.storage import sessions as session_store

# ── fakes ────────────────────────────────────────────────────────────────────


class _Usage:
    total_tokens = 4321


class _FakeResponse:
    """Stands in for coreouto's Response on the ON_ITERATION hook."""

    usage = _Usage()
    content = ""


class _RawSink:
    """Minimal EventSink that keeps every loop event it is handed."""

    def __init__(self) -> None:
        self.events: list[LoopEvent] = []
        self.answers: list[str] = []
        self.pruned: list[list[str]] = []

    def begin_working(self) -> None:
        pass

    def update_activity(self, text: str) -> None:
        pass

    def end_working(self) -> None:
        pass

    def emit_loop_event(self, event: LoopEvent) -> None:
        self.events.append(event)

    def emit_final_answer(self, content: str, session_name: str) -> None:
        self.answers.append(content)

    def emit_stream_delta(self, kind: str, text: str) -> None:
        pass

    def remove_events(self, msg_refs: list[str]) -> None:
        self.pruned.append(list(msg_refs))


class _TurnDriver:
    """Fire the chat.py dispatchers in coreouto's real hook order.

    coreouto (agent.py): AFTER_LLM_CALL → ON_THINKING → append the
    assistant message → ON_ITERATION → BEFORE_TOOL_CALL → handler →
    AFTER_TOOL_CALL. The driver mirrors that exactly, so the ref each
    event gets is the one a real turn would stamp.
    """

    def __init__(self, sink: EventSink) -> None:
        self.sink = sink
        self.pending: dict[str, tuple[str, str, str | None]] = {}
        self.pending_thinking: dict[str, tuple[str, str, str | None]] = {}
        self.refs: dict[str, str] = {}
        self.on_response = chat_mod._make_response_dispatcher(sink, self.pending)
        self.on_thinking = chat_mod._make_thinking_dispatcher(self.pending_thinking)
        self.on_iteration = chat_mod._make_iteration_dispatcher(
            sink, self.pending, self.pending_thinking, self.refs
        )
        self.on_tool_call = chat_mod._make_tool_call_dispatcher(sink, self.refs)
        self.on_tool_result = chat_mod._make_tool_result_dispatcher(sink, self.refs)

    def iteration(
        self,
        messages: list[co.Message],
        *,
        iteration: int,
        call_id: str,
        command: str,
        content: str = "on it",
        thinking: str = "thinking about it",
    ) -> co.Message:
        self.on_response(content, True)
        if thinking:
            self.on_thinking(thinking)
        assistant = _tool_turn(call_id, command=command, content=content)
        messages.append(assistant)
        self.flush_iteration(iteration=iteration, messages=messages)
        self.on_tool_call("Bash", {"command": command})
        self.on_tool_result(
            "Bash", co.ToolResult(tool_call_id=call_id, content=f"out {call_id}")
        )
        return assistant

    def flush_iteration(self, *, iteration: int, messages: list[co.Message]) -> None:
        self.on_iteration(
            iteration=iteration, messages=messages, response=_FakeResponse()
        )


def _recording(inner: EventSink) -> chat_mod._RecordingSink:
    return chat_mod._RecordingSink(inner)


def _tool_turn(call_id: str, *, command: str = "ls", content: str = "") -> co.Message:
    return co.Message(
        role="assistant",
        content=content,
        tool_calls=[
            {"id": call_id, "name": "Bash", "arguments": {"command": command}}
        ],
    )


def _malformed_turn(call_id: str) -> co.Message:
    """An assistant tool call with `arguments: null` — every provider 400s.

    coreouto itself builds such messages through `model_construct` (it
    bypasses validation for provider-shaped payloads), so the poison has
    to be constructed the same way here.
    """

    return co.Message.model_construct(
        role="assistant",
        content="",
        tool_calls=[{"id": call_id, "name": "Bash", "arguments": None}],
        tool_call_id=None,
        name=None,
    )


def _result(call_id: str) -> co.Message:
    return co.Message(role="tool", content=f"out {call_id}", tool_call_id=call_id)


def _shape(events: list[dict[str, Any]]) -> list[tuple[str, str | None, str]]:
    return [(e["kind"], e.get("msg_ref"), e["text"]) for e in events]


@pytest.fixture
def session_name(request: pytest.FixtureRequest) -> str:
    return f"prune-{abs(hash(request.node.nodeid)) % 100000}"


def _persisted_events(session_name: str) -> list[dict[str, Any]]:
    blob = json.loads(session_store.path_for(session_name).read_text())
    return blob["turns"][-1]["events"]


# ── 1. msg_ref stamping ──────────────────────────────────────────────────────


def test_events_of_one_turn_share_the_assistant_message_ref() -> None:
    # Given: a sink driven through one assistant turn (thinking, text,
    # progress line, tool call, tool result)
    sink = _RawSink()
    driver = _TurnDriver(sink)
    messages = [co.Message(role="user", content="do it")]
    assistant = driver.iteration(messages, iteration=1, call_id="a1", command="ls")

    # When: the recorded events are inspected
    # Then: every event of that turn carries the assistant message's own
    # id() as msg_ref, so removing that message prunes the whole turn at
    # once — a `tool` row and its `tool_result` can never be orphaned
    assert [e.kind for e in sink.events] == [
        "thinking",
        "response",
        "context",
        "tool",
        "tool_result",
    ]
    assert {e.msg_ref for e in sink.events} == {str(id(assistant))}
    assert driver.refs[""] == str(id(assistant))


def test_a_second_turn_gets_its_own_ref() -> None:
    # Given: two turns in one conversation
    sink = _RawSink()
    driver = _TurnDriver(sink)
    messages = [co.Message(role="user", content="do it")]
    first = driver.iteration(messages, iteration=1, call_id="a1", command="ls")
    second = driver.iteration(messages, iteration=2, call_id="a2", command="pwd")

    # Then: each turn's five events carry that turn's own ref, so
    # pruning one turn leaves the other intact
    assert [e.msg_ref for e in sink.events[:5]] == [str(id(first))] * 5
    assert [e.msg_ref for e in sink.events[5:]] == [str(id(second))] * 5
    assert driver.refs[""] == str(id(second))


def test_events_without_a_message_never_carry_a_ref() -> None:
    # Given: events that are not produced from a live message
    sink = _RawSink()
    chat_mod._make_alarm_dispatcher(sink)(
        [rt.subagents.Alarm(sid="abc123", name="editor", text="job done")]
    )
    chat_mod._make_provider_error_dispatcher(sink)(
        status_code=400,
        error_message="bad",
        reaction="retry",
        reaction_message="retrying",
    )

    # Then: they stay unstamped, so no removal path can ever prune them —
    # an alarm is an ADDITION the model must keep seeing
    assert [e.kind for e in sink.events] == ["alarm", "error"]
    assert all(e.msg_ref is None for e in sink.events)


# ── 2. repair removal ────────────────────────────────────────────────────────


def test_cut_poisoned_turn_reports_removed_refs_and_prunes_the_record(
    session_name: str,
) -> None:
    # Given: a turn with two tool calls where the FIRST one is malformed
    sink = _recording(_RawSink())
    persister = chat_mod._TurnPersister(session_name)
    persister.track(sink.events)
    persister.begin("do it")
    sink.on_event = persister.on_event
    sink.on_prune = persister.on_prune

    driver = _TurnDriver(sink)
    messages = [co.Message(role="user", content="do it")]
    poisoned = _malformed_turn("a1")
    poisoned_result = _result("a1")
    healthy = _tool_turn("a2", command="pwd")
    messages.append(poisoned)
    driver.flush_iteration(iteration=1, messages=messages)
    driver.on_tool_call("Bash", {"command": "boom"})
    driver.on_tool_result("Bash", co.ToolResult(tool_call_id="a1", content="nope"))
    messages.append(poisoned_result)
    messages.append(healthy)
    driver.flush_iteration(iteration=2, messages=messages)
    driver.on_tool_call("Bash", {"command": "pwd"})
    driver.on_tool_result("Bash", co.ToolResult(tool_call_id="a2", content="out a2"))
    messages.append(_result("a2"))
    # A repetition alarm — an ADDITION, never prunable
    sink.emit_loop_event(
        LoopEvent(actor="watchdog", kind="alarm", text="change pattern")
    )
    session_store.update_turn_events(session_name, sink.events)

    removed: list[list[str]] = []

    def on_removed(refs: list[str]) -> None:
        removed.append(list(refs))
        sink.remove_events(refs)

    hook = rt._make_history_repair_hook(
        on_repair=lambda msgs: None, on_removed=on_removed
    )

    # When: the provider rejects the payload with a retryable 400
    hook(status_code=400, reaction="retry", messages=messages)

    # Then: the poisoned turn AND its own result are reported removed —
    # an orphaned result is a 400 of its own, so the cut takes both
    assert removed == [[str(id(poisoned)), str(id(poisoned_result))]]
    # And its events are gone from the in-memory record (the source of
    # truth `finish_turn` writes) and from the persisted turn
    survivor = str(id(healthy))
    expected = [
        ("context", survivor, "iter 2 · 4321 tokens"),
        ("tool", survivor, "Bash pwd"),
        ("tool_result", survivor, "out a2"),
        ("alarm", None, "change pattern"),
    ]
    assert _shape(sink.events) == expected
    assert _shape(_persisted_events(session_name)) == expected


def test_repair_reports_removed_refs_for_duplicate_tool_results(
    session_name: str,
) -> None:
    # Given: a history carrying the same tool result twice (the old
    # `tool_result` retry reaction used to pile them up)
    sink = _recording(_RawSink())
    persister = chat_mod._TurnPersister(session_name)
    persister.track(sink.events)
    persister.begin("do it")
    sink.on_event = persister.on_event
    sink.on_prune = persister.on_prune
    driver = _TurnDriver(sink)
    messages = [co.Message(role="user", content="do it")]
    turn = _tool_turn("a1")
    messages.append(turn)
    driver.flush_iteration(iteration=1, messages=messages)
    first = _result("a1")
    second = _result("a1")
    messages.extend([first, second])
    driver.on_tool_call("Bash", {"command": "ls"})
    driver.on_tool_result("Bash", co.ToolResult(tool_call_id="a1", content="out a1"))
    driver.on_tool_result("Bash", co.ToolResult(tool_call_id="a1", content="out a1"))

    removed: list[list[str]] = []
    hook = rt._make_history_repair_hook(
        on_repair=lambda msgs: None,
        on_removed=lambda refs: removed.append(list(refs)),
    )

    # When: the repair hook dedupes the pair
    before = list(sink.events)
    hook(status_code=400, reaction="retry", messages=messages)

    # Then: only the DUPLICATE is reported removed, and the kept copy of
    # its message (same ref as the whole turn) is still in the record.
    # Identity, not equality: co.Message compares by value, and the two
    # results are the same message as far as `==` is concerned.
    assert removed == [[str(id(second))]]
    assert any(m is first for m in messages)
    assert not any(m is second for m in messages)
    # And nothing was pruned: a `tool_result` carries its ASSISTANT
    # message's ref, never its own, so the duplicate's ref matches no
    # event. The record keeps showing the call and the surviving result —
    # exactly what the model now has.
    assert sink.events == before
    assert any(e.get("msg_ref") == str(id(turn)) for e in sink.events)


# ── 3. sanitize_history truncation ───────────────────────────────────────────


def test_sanitize_history_reports_the_truncated_tail() -> None:
    # Given: a cancelled loop whose last assistant tool call never got
    # its result (a dangling call is an HTTP 400 for every provider)
    system = co.Message(role="system", content="prompt")
    user = co.Message(role="user", content="do it")
    dangling = _tool_turn("a1")
    live = [system, user, dangling]
    removed: list[list[str]] = []

    # When: the watchdog sanitizes the live list for the restart
    clean = rt.sanitize_history(
        live, on_removed=lambda refs: removed.append(list(refs))
    )

    # Then: the dangling turn is reported removed so its display events
    # can be pruned, and the clean transcript keeps the answered prefix
    assert clean == [user]
    assert removed == [[str(id(dangling))]]


def test_sanitize_history_reports_nothing_when_the_transcript_is_intact() -> None:
    # Given: a fully answered transcript
    messages = [
        co.Message(role="system", content="prompt"),
        co.Message(role="user", content="do it"),
        _tool_turn("a1"),
        _result("a1"),
    ]
    removed: list[list[str]] = []

    # When/Then: the system prompt is dropped (call() prepends its own and
    # it never produced a display event), so nothing is reported removed
    clean = rt.sanitize_history(
        messages, on_removed=lambda refs: removed.append(list(refs))
    )
    assert len(clean) == 3
    assert removed == []


def test_watchdog_restart_prunes_the_sink_for_the_truncated_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a live transcript with a dangling tool call, under a stall
    # watchdog that fires once
    monkeypatch.setattr(rt, "WATCHDOG_CHECK_SECONDS", 0.01)
    sink = _recording(_RawSink())
    driver = _TurnDriver(sink)
    live = [
        co.Message(role="system", content="prompt"),
        co.Message(role="user", content="go"),
    ]
    assistant = driver.iteration(live, iteration=1, call_id="a1", command="sleep 1")
    # The tool result never lands in `live` — the loop is cancelled
    # mid-tool, which is exactly how a dangling call arises.

    class _Activity:
        def seconds_since_last_activity(self) -> float:
            return rt.API_STALL_TIMEOUT_SECONDS * 10

    class _Progress:
        phase: str | None = "llm_call"

    attempts = 0

    async def start(p: str | None, h: list[co.Message] | None) -> co.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await asyncio.sleep(3600)  # wedged; only the watchdog ends it
        return co.Response(content="recovered", messages=[], iterations=1)

    # When: the stall path restarts the turn from the sanitized list
    response = asyncio.run(
        rt.supervised_run(
            start,
            "go",
            None,
            live_key="outo",
            level=0,
            on_wakeup=lambda *args: None,
            on_removed=sink.remove_events,
            states=(_Activity(), _Progress(), {"outo": live}),
        )
    )

    # Then: the resumed attempt ran, and the discarded tail's events were
    # pruned from the record so it matches what the model still has
    assert response.content == "recovered"
    assert sink.events == []
    assert sink._inner.pruned == [[str(id(assistant))]]


# ── 4. the fold-into-user-message path is not a removal ──────────────────────


def test_folding_the_explanation_into_the_user_message_prunes_nothing() -> None:
    # Given: a poisoned turn whose predecessor is a user message — the
    # repair folds its explanation into that message instead of appending
    # a second user message (providers reject two in a row)
    user = co.Message(role="user", content="do it")
    poisoned = _malformed_turn("a1")
    result_msg = _result("a1")
    messages = [user, poisoned, result_msg]
    removed: list[list[str]] = []

    # When: the turn is cut
    assert rt._cut_poisoned_turn(
        messages, on_removed=lambda refs: removed.append(list(refs))
    )

    # Then: only the cut turn and its result are removals — the fold is
    # an in-place edit of a surviving message, so it prunes nothing
    assert str(id(user)) not in removed[0]
    assert set(removed[0]) == {str(id(poisoned)), str(id(result_msg))}
    assert user.content.startswith("do it")
    assert "removed from the history" in user.content


# ── 5. summarizer compaction is explicitly NOT a removal ─────────────────────


def test_summarizer_compaction_leaves_the_display_record_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a real summarize hook with a tiny context window, so the
    # next BEFORE_LLM_CALL compacts the conversation
    monkeypatch.setattr(context_mod, "get_context_window", lambda *a, **k: 100)

    class _SummaryAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def call(self, prompt: str) -> Any:
            return co.Response(content="handoff", messages=[], iterations=1)

    monkeypatch.setattr(co, "Agent", _SummaryAgent)
    sink = _recording(_RawSink())
    driver = _TurnDriver(sink)
    messages = [
        co.Message(role="system", content="prompt"),
        co.Message(role="user", content="go"),
    ]
    assistant = driver.iteration(messages, iteration=1, call_id="a1", command="ls")
    messages.append(_result("a1"))
    before = list(sink.events)

    on_iteration, before_llm_call = context_mod.make_summarize_hook("m", "s", "p")
    on_iteration(iteration=1, messages=messages, response=_FakeResponse())

    # When: the compaction runs on the next iteration
    asyncio.run(before_llm_call(messages=messages))

    # Then: history really was compacted...
    assert len(messages) == 2
    assert messages[0].role == "system"
    # ...and NOT ONE recorded event was dropped: the transcript is the
    # human record of what happened, history is the model-context layer
    assert sink.events == before
    assert sink._inner.pruned == []
    assert any(e.get("msg_ref") == str(id(assistant)) for e in sink.events)


# ── 6. cross-turn pruning on disk ────────────────────────────────────────────


def test_prune_turn_events_reaches_earlier_turns_in_the_file(
    session_name: str,
) -> None:
    # Given: two recorded turns, one of them carrying a msg_ref
    session_store.begin_turn(session_name, "first")
    session_store.update_turn_events(
        session_name,
        [
            {"actor": "outo", "kind": "tool", "text": "Bash a1", "msg_ref": "111"},
            {
                "actor": "outo",
                "kind": "tool_result",
                "text": "out a1",
                "msg_ref": "111",
            },
        ],
    )
    session_store.finish_turn(session_name, assistant="one")
    session_store.begin_turn(session_name, "second")
    session_store.update_turn_events(
        session_name,
        [
            {"actor": "outo", "kind": "tool", "text": "Bash a2", "msg_ref": "222"},
            {"actor": "watchdog", "kind": "alarm", "text": "change pattern"},
        ],
    )
    session_store.finish_turn(session_name, assistant="two")

    # When: a repair in the SECOND turn reaches back and cuts a message
    # from the FIRST turn's history (a resumed session carries both)
    removed = session_store.prune_turn_events(session_name, ["111"])

    # Then: the earlier turn's events are gone from the FILE too
    data = session_store.load(session_name)
    assert removed == 2
    assert [e["text"] for e in data.turns[0].events] == []
    assert [e["text"] for e in data.turns[1].events] == [
        "Bash a2",
        "change pattern",
    ]


def test_prune_turn_events_with_unknown_refs_is_a_no_op(
    session_name: str,
) -> None:
    # Given: a finished turn whose events carry a ref nothing will match
    session_store.begin_turn(session_name, "go")
    session_store.update_turn_events(
        session_name,
        [{"actor": "outo", "kind": "tool", "text": "Bash", "msg_ref": "999"}],
    )
    session_store.finish_turn(session_name, assistant="done")
    before = session_store.path_for(session_name).read_text()

    # When: an unrelated removal is reported
    removed = session_store.prune_turn_events(session_name, ["12345"])

    # Then: nothing is reported removed and the file is byte-identical
    assert removed == 0
    assert session_store.path_for(session_name).read_text() == before


def test_a_removal_that_matches_nothing_here_still_reaches_the_persister(
    session_name: str,
) -> None:
    # Given: a resumed session whose EARLIER turn holds the events of a
    # message a repair is about to cut, and a live turn that holds none
    session_store.begin_turn(session_name, "earlier")
    session_store.update_turn_events(
        session_name, [{"actor": "outo", "kind": "tool", "text": "Bash", "msg_ref": "111"}]
    )
    session_store.finish_turn(session_name, assistant="one")

    sink = _recording(_RawSink())
    persister = chat_mod._TurnPersister(session_name)
    persister.track(sink.events)
    persister.begin("current")
    sink.on_event = persister.on_event
    sink.on_prune = persister.on_prune
    sink.emit_loop_event(LoopEvent(actor="outo", kind="context", text="iter 1"))

    # When: the ref of a message from the earlier turn is removed — the
    # in-memory list has nothing matching
    sink.remove_events(["111"])

    # Then: the file was swept anyway, so the earlier turn stops
    # advertising a call the model no longer has
    data = session_store.load(session_name)
    assert data.turns[0].events == []
    assert [e["text"] for e in data.turns[1].events] == ["iter 1"]
    assert [e["text"] for e in sink.events] == ["iter 1"]


# ── 7. TUI row removal keeps the window consistent ──────────────────────────


class _FakeLog:
    """Records the mount calls `_RowWindow` makes; mounts nothing."""

    def __init__(self) -> None:
        self.mounted: list[Any] = []
        self.scroll_y = 0.0
        self.max_scroll_y = 0.0

    def mount(self, widget: Any, before: Any = None) -> None:
        self.mounted.append(widget)

    def scroll_to(self, y: float, animate: bool = False) -> None:
        self.scroll_y = y

    def scroll_end(self, animate: bool = False) -> None:
        self.scroll_y = 0.0

    def call_after_refresh(self, fn: Any, *args: Any) -> None:
        fn(*args)


def test_removing_rows_keeps_the_window_first_in_range() -> None:
    # Given: a window of five rows with two already evicted
    window = _RowWindow()
    rows = [Static(f"row {i}") for i in range(5)]
    for row in rows:
        window.rows.append(row)
    window.first = 2

    # When: a pruned row below `first` and one at the tail are dropped
    window.remove(rows[3])
    window.remove(rows[4])

    # Then: the window still describes a contiguous suffix of `rows`
    assert window.first == 2
    assert len(window.rows) == 3
    assert 0 <= window.first <= len(window.rows)


def test_removing_the_tail_row_never_desynchronizes_chunk_loading() -> None:
    # Given: a window whose `first` pointed past the last row — the shape
    # a live "cut the last turn" prune leaves behind
    window = _RowWindow()
    rows = [Static(f"row {i}") for i in range(3)]
    for row in rows:
        window.rows.append(row)
    window.first = 3
    log = _FakeLog()

    window.remove(rows[2])

    # Then: `first` is clamped into range, so scrolling up mounts the
    # missing chunk instead of indexing off the end of the row list
    assert window.first == len(window.rows)
    window.maybe_load_older(log, 0.0)  # type: ignore[arg-type]
    assert window.first == 0
    assert log.mounted == window.rows


def test_the_tui_sink_hops_the_removal_onto_the_ui_thread() -> None:
    # Given: a sink over a stand-in app (row work must not run on the
    # agent's worker thread)
    calls: list[list[str]] = []

    class _FakeApp:
        def call_from_thread(self, callback: Any, *args: Any) -> None:
            callback(*args)

        def _remove_event_rows(self, msg_refs: list[str]) -> None:
            calls.append(list(msg_refs))

    sink = TUIEventSink(_FakeApp())  # type: ignore[arg-type]
    sink.remove_events(["111", "222"])

    # Then: the app was asked to drop exactly those rows
    assert calls == [["111", "222"]]


def test_the_tui_removes_only_the_pruned_rows() -> None:
    # Given: rows built from loop events, two of them sharing a ref
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            app._log.remove_children()
            app._reset_rows()
            await pilot.pause()
            app._add_event_row(
                LoopEvent(
                    actor="outo",
                    kind="tool",
                    text="Bash a1",
                    tool_name="Bash",
                    msg_ref="111",
                )
            )
            app._add_event_row(
                LoopEvent(
                    actor="outo",
                    kind="tool_result",
                    text="out a1",
                    tool_name="Bash",
                    msg_ref="111",
                )
            )
            app._add_event_row(
                LoopEvent(
                    actor="outo",
                    kind="tool",
                    text="Bash a2",
                    tool_name="Bash",
                    msg_ref="222",
                )
            )
            app._add_event_row(
                LoopEvent(actor="watchdog", kind="alarm", text="change pattern")
            )
            # A blank spacer between turns: a plain `Static`, so it has no
            # msg_ref attribute at all and must not break the sweep.
            app._mount_row(Static(""))
            await pilot.pause()
            before = list(app._window.rows)
            # A tool call and its result are ONE row (the result attaches
            # to the pending call row), so three events make two tool
            # boxes, the alarm line and the spacer.
            assert len(before) == 4

            # When: the first turn's message is removed from history
            app._remove_event_rows(["111"])
            await pilot.pause()

            # Then: its tool box went with the result it already held
            # (both carried that ref), the survivors are the same
            # widgets, and the pending-pair list no longer references the
            # dead row
            assert app._window.rows == [before[1], before[2], before[3]]
            assert app._pending_tool_rows == [before[1]]
            assert all(row.is_attached for row in app._window.rows)
            assert 0 <= app._window.first <= len(app._window.rows)

    asyncio.run(body())


# ── 8. end-to-end consistency ────────────────────────────────────────────────


def test_the_turn_record_describes_exactly_the_surviving_messages(
    session_name: str,
) -> None:
    # Given: a real turn — two tool-call iterations, then a repair that
    # cuts the FIRST (malformed) one — recorded through the real
    # `_RecordingSink` + `_TurnPersister` chain
    sink = _recording(_RawSink())
    persister = chat_mod._TurnPersister(session_name)
    persister.track(sink.events)
    persister.begin("do it")
    sink.on_event = persister.on_event
    sink.on_prune = persister.on_prune

    driver = _TurnDriver(sink)
    user = co.Message(role="user", content="do it")
    poisoned = _malformed_turn("a1")
    poisoned_result = _result("a1")
    healthy = _tool_turn("a2", command="pwd")
    healthy_result = _result("a2")
    messages = [user]
    messages.append(poisoned)
    driver.flush_iteration(iteration=1, messages=messages)
    driver.on_tool_call("Bash", {"command": "boom"})
    driver.on_tool_result("Bash", co.ToolResult(tool_call_id="a1", content="nope"))
    messages.append(poisoned_result)
    messages.append(healthy)
    driver.flush_iteration(iteration=2, messages=messages)
    driver.on_tool_call("Bash", {"command": "pwd"})
    driver.on_tool_result("Bash", co.ToolResult(tool_call_id="a2", content="out a2"))
    messages.append(healthy_result)
    sink.emit_loop_event(LoopEvent(actor="watchdog", kind="alarm", text="change pattern"))

    repaired: list[list[co.Message]] = []
    hook = rt._make_history_repair_hook(
        on_repair=lambda msgs: repaired.append(list(msgs)),
        on_removed=sink.remove_events,
    )

    # When: the provider 400s and the repair cuts the poisoned turn
    hook(status_code=400, reaction="retry", messages=messages)

    # Then: the record matches the surviving messages event for event.
    # The expectation is written out by hand: the cut turn's three events
    # (progress, tool, result) are gone, the healthy turn's three remain
    # stamped with its own message, and the alarm addition — which never
    # carries a ref — was never prunable.
    survivor = str(id(healthy))
    expected = [
        ("context", survivor, "iter 2 · 4321 tokens"),
        ("tool", survivor, "Bash pwd"),
        ("tool_result", survivor, "out a2"),
        ("alarm", None, "change pattern"),
    ]
    assert _shape(sink.events) == expected
    # And what the model now perceives is the repaired transcript
    assert repaired[0] == [user, healthy, healthy_result]    # And the same events are what the session file holds
    session_store.finish_turn(
        session_name,
        prompt="do it",
        assistant="done",
        status=session_store.STATUS_DONE,
        events=sink.events,
    )
    assert _shape(_persisted_events(session_name)) == expected


# ── the protocol is closed ───────────────────────────────────────────────────


def test_every_event_sink_implementer_grew_remove_events() -> None:
    # Given: the four implementers of the closed EventSink protocol
    sinks: list[Any] = [
        NullSink(),
        ConsoleEventSink(quiet=True),
        _RawSink(),
        _recording(_RawSink()),
    ]

    # Then: each one defines the method — hooks are global and there is
    # no __getattr__ delegation, so a missing method would raise on the
    # first removal instead of degrading
    for sink in sinks:
        assert callable(sink.remove_events)
        sink.remove_events([])


def test_console_sink_notes_the_removal_instead_of_unprinting() -> None:
    # Given: a console sink whose Console output is captured
    printed: list[str] = []

    class _Console:
        def print(self, text: Any = "", **kwargs: Any) -> None:
            printed.append(getattr(text, "plain", str(text)))

    sink = ConsoleEventSink()
    sink._console = _Console()  # type: ignore[assignment]

    # When: messages are removed from the record
    sink.remove_events(["1", "2"])

    # Then: a dim note line is printed — the console is append-only and
    # cannot unprint what it already showed, so a note is the honest
    # v1 behavior
    assert printed == ["[removed 2 corrupted messages from the record]"]


def test_quiet_console_sink_prints_no_note() -> None:
    # Given: the byte-identical output modes
    printed: list[str] = []

    class _Console:
        def print(self, text: Any = "", **kwargs: Any) -> None:
            printed.append(str(text))

    sink = ConsoleEventSink(quiet=True)
    sink._console = _Console()  # type: ignore[assignment]
    sink.remove_events(["1"])

    # Then: --answer-only / --with-session output stays untouched
    assert printed == []
