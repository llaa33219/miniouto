"""Lock the inbox delivery hook in src/miniouto/core/runtime.py.

A detached `chat --bg` process that finishes drops one file per message into the
session's inbox directory, and that message must reach outo's next LLM call as
ONE synthetic user message. The rules under test: coalescing (N drained
messages -> 1 message), folding (never two consecutive user messages — the
hazard `_cut_poisoned_turn` documents), the LoopEvent each drained message
produces for the session record and the TUI, and that the delivery path cannot
be broken by the display path.

The messages here are written through the REAL `inbox.insert` / drained by the
REAL hook rather than stubbed: the file-per-message shape is the whole reason a
cross-process writer cannot lose a message, and a stubbed queue would pass even
if the hook drained the wrong directory.
"""

from __future__ import annotations

from collections.abc import Iterator

import coreouto as co
import pytest

from miniouto.core import chat as chat_mod
from miniouto.core import inbox
from miniouto.core import runtime as rt


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


@pytest.fixture(autouse=True)
def _empty_inboxes() -> Iterator[None]:
    """Every test posts into its own session directory; clear them all."""

    yield
    for directory in inbox.INBOX_DIR.glob("coalescing-*"):
        for path in directory.iterdir():
            path.unlink(missing_ok=True)
        directory.rmdir()


def _post(text: str, session: str, **meta: object) -> str:
    """Write one inbox message, the way a spawned process would."""

    return inbox.insert(session, text, kind=inbox.ALARM_KIND, meta=dict(meta))


def test_two_pending_messages_land_in_one_user_message() -> None:
    # Given: two jobs finished between iterations, mid-loop
    session = "coalescing-two"
    _post("alarm one", session, id="aaa111", role="editor")
    _post("alarm two", session, id="bbb222", role="reviewer")
    messages = [
        co.Message(role="user", content="do the work"),
        co.Message(role="assistant", content="on it"),
    ]

    # When: the hook fires
    rt._make_alarm_hook(session)(messages=messages)

    # Then: exactly one new message carries both, in drain order
    assert len(messages) == 3
    assert messages[-1].role == "user"
    assert messages[-1].content == "alarm one\n\nalarm two"


def test_a_message_folds_into_a_trailing_user_message() -> None:
    # Given: the turn's own prompt is still the last message (a job finished
    # before the model answered), where appending would make two in a row
    session = "coalescing-fold"
    _post("alarm one", session)
    _post("alarm two", session)
    messages = [
        co.Message(role="user", content="do the work"),
        co.Message(role="assistant", content="on it"),
        co.Message(role="user", content="any update?"),
    ]

    # When: the hook fires
    rt._make_alarm_hook(session)(messages=messages)

    # Then: nothing is appended; both are folded into the last message
    assert len(messages) == 3
    assert messages[-1].content == "any update?\n\nalarm one\n\nalarm two"


def test_a_message_appends_after_an_assistant_message() -> None:
    # Given: a normal mid-loop position (last message is the model's)
    session = "coalescing-append"
    _post("alarm one", session)
    messages = [
        co.Message(role="user", content="do the work"),
        co.Message(role="assistant", content="on it"),
    ]

    # When: the hook fires
    rt._make_alarm_hook(session)(messages=messages)

    # Then: one new user message is appended
    assert len(messages) == 3
    assert messages[-1].role == "user"
    assert messages[-1].content == "alarm one"


def test_a_plain_user_message_is_delivered_verbatim() -> None:
    # Given: a `kind="user"` message — no alarm banner of its own
    session = "coalescing-user"
    inbox.insert(session, "the editor says: file X is empty", kind=inbox.USER_KIND)
    messages = [co.Message(role="assistant", content="working")]

    # When: the hook fires
    rt._make_alarm_hook(session)(messages=messages)

    # Then: the text lands unchanged — the hook adds no banner of its own
    assert messages[-1].content == "the editor says: file X is empty"


def test_the_inbox_is_drained_so_the_same_message_is_never_twice() -> None:
    # Given: one pending message and a live message list
    session = "coalescing-drain-once"
    _post("alarm one", session)
    messages = [co.Message(role="assistant", content="working")]

    # When: the hook fires twice (two loop iterations)
    rt._make_alarm_hook(session)(messages=messages)
    rt._make_alarm_hook(session)(messages=messages)

    # Then: the drain consumed the message file, so the second pass is a
    # no-op instead of a duplicate delivery
    assert len(messages) == 2
    assert inbox.pending_count(session) == 0


def test_each_drained_message_emits_one_annotated_loop_event() -> None:
    # Given: two drained messages and a sink fed by the chat dispatcher
    session = "coalescing-events"
    _post("alarm one", session, id="aaa111", role="editor")
    _post("alarm two", session, id="bbb222", role="reviewer")
    sink = RecordingSink()
    messages = [co.Message(role="assistant", content="working")]

    # When: the hook fires and forwards them
    rt._make_alarm_hook(session, chat_mod._make_alarm_dispatcher(sink))(messages=messages)

    # Then: one event per message, carrying its spawn identity and the
    # `inbox:<id>` de-duplication handle the display layer keys on
    assert [(e.kind, e.subagent_id, e.subagent_name) for e in sink.events] == [
        ("alarm", "aaa111", "editor"),
        ("alarm", "bbb222", "reviewer"),
    ]
    assert [e.text for e in sink.events] == ["alarm one", "alarm two"]
    assert all(e.actor == "outo" for e in sink.events)
    assert [e.detail.split(":")[0] for e in sink.events] == ["inbox", "inbox"]
    assert len({e.detail for e in sink.events}) == 2


def test_a_user_kind_message_emits_a_user_kind_event() -> None:
    # Given: a `kind="user"` message drained through the dispatcher
    session = "coalescing-user-event"
    inbox.insert(session, "proceed with plan B", kind=inbox.USER_KIND)
    sink = RecordingSink()
    messages = [co.Message(role="assistant", content="working")]

    # When: the hook fires
    rt._make_alarm_hook(session, chat_mod._make_alarm_dispatcher(sink))(messages=messages)

    # Then: the event kind mirrors the inbox kind, so a display can tell a
    # plain user turn from a spawned process reporting back
    assert [e.kind for e in sink.events] == ["user"]
    assert [e.text for e in sink.events] == ["proceed with plan B"]


def test_an_alarm_banner_survives_the_round_trip_verbatim() -> None:
    # Given: a message written with the shared alarm header
    session = "coalescing-banner"
    _post(f"{inbox.ALARM_HEADER}\nthe validator finished", session)
    messages = [co.Message(role="assistant", content="working")]

    # When: the hook delivers it
    rt._make_alarm_hook(session)(messages=messages)

    # Then: the banner is the first line of the delivered text, which is what
    # the model and the display both key on
    assert messages[-1].content.splitlines()[0] == inbox.ALARM_HEADER


def test_a_broken_sink_cannot_break_the_loop() -> None:
    # Given: an event callback that raises, as a failing sink would
    session = "coalescing-broken-sink"
    _post("alarm one", session)

    def boom(_messages: list[inbox.InboxMessage]) -> None:
        raise RuntimeError("sink is gone")

    messages = [co.Message(role="assistant", content="working")]

    # When/Then: the message still reaches the model and nothing propagates
    rt._make_alarm_hook(session, boom)(messages=messages)
    assert messages[-1].content == "alarm one"


def test_no_pending_message_leaves_the_history_untouched() -> None:
    # Given: an empty inbox (the common case, every other iteration)
    session = "coalescing-empty"
    messages = [co.Message(role="user", content="do the work")]

    # When: the hook fires
    rt._make_alarm_hook(session)(messages=messages)

    # Then: no message is added or rewritten
    assert len(messages) == 1
    assert messages[0].content == "do the work"


def test_the_drain_targets_the_session_it_was_built_for() -> None:
    # Given: a message pending for a DIFFERENT session
    other = "coalescing-other-session"
    _post("not yours", other)
    session = "coalescing-mine"
    messages = [co.Message(role="assistant", content="working")]

    # When: a hook bound to this session fires
    rt._make_alarm_hook(session)(messages=messages)

    # Then: the other session's message is neither delivered nor consumed
    assert len(messages) == 1
    assert inbox.pending_count(other) == 1
