"""LoopEvent.msg_ref serialization contract.

`msg_ref` carries `str(id(message))` of the live coreouto Message a
display event was produced from, so later milestones can prune events
whose message was removed from the restorable history. The field is
additive: nothing stamps it yet, so every assertion here is about the
serialization surface alone, not about emitter behavior.
"""

from __future__ import annotations

from typing import Any

from miniouto.core.events import LoopEvent


def test_msg_ref_round_trip_preserves_value() -> None:
    # Given: an event carrying a msg_ref
    event = LoopEvent(actor="outo", kind="tool", text="Bash", msg_ref="140234")

    # When: it is serialized and reloaded
    d = event.to_dict()
    restored = LoopEvent.from_dict(d)

    # Then: the msg_ref survives the round trip
    assert d["msg_ref"] == "140234"
    assert restored.msg_ref == "140234"


def test_to_dict_omits_msg_ref_when_none() -> None:
    # Given: an event with no msg_ref
    event = LoopEvent(actor="outo", kind="tool", text="Bash")

    # When: it is serialized
    d = event.to_dict()

    # Then: the key is absent entirely (or-omit, like the sibling fields)
    assert event.msg_ref is None
    assert "msg_ref" not in d


def test_from_dict_without_msg_ref_key_yields_none() -> None:
    # Given: a dict that never went through the new field
    d: dict[str, Any] = {"actor": "editor-abc123", "kind": "tool", "text": "Edit"}

    # When: it is loaded
    event = LoopEvent.from_dict(d)

    # Then: msg_ref defaults to None instead of raising
    assert event.msg_ref is None


def test_old_style_event_dict_still_loads() -> None:
    # Given: a pre-msg_ref event dict as persisted by an older build
    d: dict[str, Any] = {
        "actor": "subagent-def456",
        "kind": "thinking",
        "text": "reasoning",
        "tool_name": "Bash",
        "subagent_id": "def456",
        "detail": "ls -la",
        "subagent_name": "reviewer",
    }

    # When: it is loaded
    event = LoopEvent.from_dict(d)

    # Then: every legacy field is intact and msg_ref is None (retro-compat)
    assert event.actor == "subagent-def456"
    assert event.kind == "thinking"
    assert event.text == "reasoning"
    assert event.tool_name == "Bash"
    assert event.subagent_id == "def456"
    assert event.detail == "ls -la"
    assert event.subagent_name == "reviewer"
    assert event.msg_ref is None


def test_the_new_user_kind_round_trips_with_its_inbox_detail() -> None:
    # Given: a drained inbox message recorded as a `user` event
    event = LoopEvent(
        actor="outo",
        kind="user",
        text="proceed with plan B",
        subagent_id="abc123",
        subagent_name="editor",
        detail="inbox:20260101-000000-000000-abcdef",
    )

    # When: it is serialized and reloaded
    restored = LoopEvent.from_dict(event.to_dict())

    # Then: the kind, the spawn identity and the de-duplication handle all
    # survive — the display layer keys off every one of them
    assert restored.kind == "user"
    assert restored.text == "proceed with plan B"
    assert restored.subagent_id == "abc123"
    assert restored.subagent_name == "editor"
    assert restored.detail == "inbox:20260101-000000-000000-abcdef"
    assert restored.msg_ref is None


def test_retired_subagent_kinds_still_load_from_older_sessions() -> None:
    # Given: `subagent_start` / `subagent_end` events persisted before the
    # in-process subagent engine was erased
    start = LoopEvent.from_dict(
        {"actor": "editor-abc123", "kind": "subagent_start", "text": "fix the bug"}
    )
    end = LoopEvent.from_dict(
        {"actor": "editor-abc123", "kind": "subagent_end", "text": "done"}
    )

    # Then: they deserialize untouched instead of raising or being coerced —
    # nothing is emitted with these kinds any more, but a session file
    # written by an older build must still open
    assert start.kind == "subagent_start"
    assert end.kind == "subagent_end"
    assert start.subagent_id is None
    assert end.text == "done"
