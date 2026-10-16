"""Lock the cross-process inbox contract from src/miniouto/core/inbox.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from miniouto.core import inbox
from miniouto.core.inbox import (
    ALARM_HEADER,
    ALARM_KIND,
    USER_KIND,
    InboxMessage,
    drain,
    inbox_dir,
    insert,
    pending_count,
    prune,
)

SESSION = "s1"


def _raw(directory: Path, stem: str, **overrides: Any) -> Path:
    """Write one message file by hand, bypassing `insert` and its pruning."""

    payload: dict[str, Any] = {
        "kind": ALARM_KIND,
        "text": f"text of {stem}",
        "ts": "2026-01-01T00:00:00",
        "source": "test",
        "meta": {},
    }
    payload.update(overrides)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_inbox_alarm_header_is_the_literal_the_model_contract_names() -> None:
    # Given: the header core/inbox.py owns (it used to be owned by the deleted
    # in-process engine, and nothing in src/ writes its own copy any more)
    # When / Then: it is byte-identical to the literal, and the repetition
    # alarm — the one OTHER src/ source of an alarm — leads with it
    from miniouto.core import repetition

    verdict = repetition.LoopVerdict(kind="tool", detail="30x: Bash")
    assert ALARM_HEADER == "[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]"
    assert repetition.alarm_text(verdict).splitlines()[0] == ALARM_HEADER


def test_inbox_insert_drain_round_trip_preserves_every_field() -> None:
    # Given: an alarm inserted with source and meta
    meta = {"sid": "abc123", "name": "editor"}
    message_id = insert(SESSION, "hello", kind=ALARM_KIND, source="spawn", meta=meta)

    # When: the session's inbox is drained
    messages = drain(SESSION)

    # Then: exactly one message, every field preserved, id == the filename stem
    assert messages == [
        InboxMessage(
            id=message_id,
            kind=ALARM_KIND,
            text="hello",
            ts=messages[0].ts,
            source="spawn",
            meta=meta,
        )
    ]
    assert messages[0].id == (inbox_dir(SESSION) / f"{message_id}.json").stem


def test_inbox_insert_defaults_kind_source_and_meta() -> None:
    # Given: insert with no optional arguments
    insert(SESSION, "wake up")

    # When: drained
    (message,) = drain(SESSION)

    # Then: defaults are the alarm kind, the cli source and an empty meta
    assert (message.kind, message.source, message.meta) == (ALARM_KIND, "cli", {})


def test_inbox_drain_returns_insertion_order_despite_shuffled_listdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: three messages whose filenames sort oldest-first
    directory = inbox_dir(SESSION)
    for stem in (
        "20260101-000000000001-aaaaaa",
        "20260101-000000000002-bbbbbb",
        "20260101-000000000003-cccccc",
    ):
        _raw(directory, stem)

    # When: the filesystem hands them back in reverse order
    real_glob = Path.glob

    def reversed_glob(self: Path, pattern: str) -> Any:
        return list(reversed(list(real_glob(self, pattern))))

    monkeypatch.setattr(Path, "glob", reversed_glob)
    messages = drain(SESSION)

    # Then: insertion order is preserved by the explicit sort on id
    assert [m.id for m in messages] == [
        "20260101-000000000001-aaaaaa",
        "20260101-000000000002-bbbbbb",
        "20260101-000000000003-cccccc",
    ]


def test_inbox_drain_empties_the_directory() -> None:
    # Given: two pending messages
    insert(SESSION, "one")
    insert(SESSION, "two")
    assert pending_count(SESSION) == 2

    # When: drained
    drain(SESSION)

    # Then: nothing is left on disk and a second drain is empty
    assert pending_count(SESSION) == 0
    assert list(inbox_dir(SESSION).glob("*.json")) == []
    assert drain(SESSION) == []


def test_inbox_drain_of_an_untouched_session_is_empty() -> None:
    # Given: a session that has never been written to
    # When: drained
    # Then: the missing directory is an empty result, not an error
    assert drain("never-used") == []
    assert pending_count("never-used") == 0
    assert prune("never-used") == 0


def test_inbox_insert_during_an_in_progress_drain_is_deferred_not_lost() -> None:
    # Given: one message already pending
    insert(SESSION, "first")
    # When: it is drained, then a second message arrives and is drained
    assert [m.text for m in drain(SESSION)] == ["first"]
    insert(SESSION, "second")
    # Then: the late arrival is picked up whole by the next drain
    assert [m.text for m in drain(SESSION)] == ["second"]


def test_inbox_a_message_mid_rename_is_either_seen_whole_or_not_seen() -> None:
    # Given: a writer that has written its tmp file but not yet renamed it
    directory = inbox_dir(SESSION)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "20260101-000000000009-ffffff.tmp").write_text("{not json", encoding="utf-8")

    # When: drained
    messages = drain(SESSION)

    # Then: the unrenamed file is invisible — not read, not quarantined
    assert messages == []
    assert (directory / "20260101-000000000009-ffffff.tmp").exists()

    # And: once renamed into place, the same content IS delivered
    _raw(directory, "20260101-000000000009-ffffff")
    assert [m.id for m in drain(SESSION)] == ["20260101-000000000009-ffffff"]


def test_inbox_corrupt_file_is_quarantined_and_the_drain_continues() -> None:
    # Given: a good message, a JSON-syntax error, and a well-formed wrong shape
    directory = inbox_dir(SESSION)
    _raw(directory, "20260101-000000000001-aaaaaa")
    (directory / "20260101-000000000002-bbbbbb.json").write_text("{not json", encoding="utf-8")
    (directory / "20260101-000000000003-cccccc.json").write_text(
        '{"kind": "alarm"}', encoding="utf-8"
    )

    # When: drained
    messages = drain(SESSION)

    # Then: only the good one is returned, and neither bad file raises
    assert [m.id for m in messages] == ["20260101-000000000001-aaaaaa"]
    assert (directory / "20260101-000000000002-bbbbbb.bad").exists()
    assert (directory / "20260101-000000000003-cccccc.bad").exists()
    assert list(directory.glob("*.json")) == []


def test_inbox_prune_keeps_the_newest_and_deletes_the_rest() -> None:
    # Given: five messages, with keep overridden to two
    directory = inbox_dir(SESSION)
    stems = [f"20260101-00000000000{i}-aaaaaa" for i in range(1, 6)]
    for stem in stems:
        _raw(directory, stem)

    # When: pruned
    deleted = prune(SESSION, keep=2)

    # Then: the two newest survive and the count is the number deleted
    assert deleted == 3
    assert [p.stem for p in sorted(directory.glob("*.json"))] == stems[3:]


def test_inbox_prune_at_the_real_boundary_keeps_200() -> None:
    # Given: more messages than the real cap (insert prunes as it goes)
    for i in range(inbox.MAX_INBOX_MESSAGES + 5):
        insert(SESSION, f"m{i}")

    # When / Then: the inbox holds exactly the cap and is drained whole
    assert pending_count(SESSION) == inbox.MAX_INBOX_MESSAGES
    assert len(drain(SESSION)) == inbox.MAX_INBOX_MESSAGES


def test_inbox_two_sessions_are_fully_isolated() -> None:
    # Given: a message in one session only
    insert("alpha", "for alpha")
    insert("beta", "for beta")

    # When: each is drained
    alpha = [m.text for m in drain("alpha")]
    beta = [m.text for m in drain("beta")]

    # Then: neither sees the other's message
    assert alpha == ["for alpha"]
    assert beta == ["for beta"]
    assert inbox_dir("alpha") != inbox_dir("beta")


def test_inbox_user_kind_is_accepted() -> None:
    # Given: an explicitly user-kind message
    insert(SESSION, "a real instruction", kind=USER_KIND)

    # When: drained
    (message,) = drain(SESSION)

    # Then: the kind round-trips
    assert message.kind == USER_KIND


def test_inbox_rejects_empty_text() -> None:
    # Given / When / Then: whitespace-only text is not a message
    with pytest.raises(ValueError, match="text must not be empty"):
        insert(SESSION, "   \n ")


def test_inbox_rejects_oversized_text() -> None:
    # Given / When / Then: text past MAX_TEXT_CHARS is refused before any write
    with pytest.raises(ValueError, match="MAX_TEXT_CHARS"):
        insert(SESSION, "x" * (inbox.MAX_TEXT_CHARS + 1))
    assert pending_count(SESSION) == 0


def test_inbox_rejects_an_unknown_kind() -> None:
    # Given / When / Then: only alarm and user are valid kinds
    with pytest.raises(ValueError, match="kind must be one of"):
        insert(SESSION, "hi", kind="system")


def test_inbox_rejects_a_non_dict_meta() -> None:
    # Given / When / Then: meta must be an object
    with pytest.raises(ValueError, match="meta must be a dict"):
        insert(SESSION, "hi", meta=["not", "a", "dict"])


def test_inbox_rejects_an_empty_session() -> None:
    # Given / When / Then: a session name is required
    with pytest.raises(ValueError, match="session must be a non-empty name"):
        insert("   ", "hi")
    with pytest.raises(ValueError, match="session must be a non-empty name"):
        drain("")
