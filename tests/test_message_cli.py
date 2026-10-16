"""`miniouto message` is the only door into the cross-process inbox from outside.

Every test names its own session, so the shared test home's inbox directory
stays a shared-but-uncontended fixture: no fixture juggling, and the real
`core.inbox` layout is exercised end to end.
"""

from __future__ import annotations

import json
import uuid

from typer.testing import CliRunner

from miniouto.cli import message as message_mod
from miniouto.core import inbox
from miniouto.storage import settings as settings_store

runner = CliRunner()


def _session() -> str:
    return f"msg-cli-{uuid.uuid4().hex[:8]}"


def _messages(session: str) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(inbox.inbox_dir(session).glob("*.json"))]


def test_insert_from_positional_text_joins_words_and_prints_the_id() -> None:
    # Given: a session with an empty inbox
    session = _session()

    # When: text is passed as separate words
    result = runner.invoke(message_mod.app, ["insert", "hello", "there", "--session", session])

    # Then: the id is printed and the joined text landed in the inbox
    message_id = result.output.strip()
    assert result.exit_code == 0
    assert (inbox.inbox_dir(session) / f"{message_id}.json").is_file()
    assert _messages(session)[0]["text"] == "hello there"


def test_insert_reads_the_body_from_stdin() -> None:
    # Given: a session with an empty inbox
    session = _session()

    # When: the body arrives on stdin
    result = runner.invoke(
        message_mod.app, ["insert", "--session", session, "--stdin"], input="piped body\n"
    )

    # Then: stdin text is what was stored
    assert result.exit_code == 0
    assert _messages(session)[0]["text"] == "piped body\n"


def test_kind_and_source_are_passed_through() -> None:
    # Given: a session with an empty inbox
    session = _session()

    # When: an alarm and a user interjection are inserted
    alarm = runner.invoke(
        message_mod.app, ["insert", "job done", "--session", session, "--kind", "alarm", "--source", "spawn"]
    )
    interjection = runner.invoke(message_mod.app, ["insert", "stop", "--session", session])

    # Then: the alarm keeps its explicit kind, the default one is "user"
    assert (alarm.exit_code, interjection.exit_code) == (0, 0)
    stored = _messages(session)
    assert {(m["kind"], m["source"], m["text"]) for m in stored} == {
        ("alarm", "spawn", "job done"),
        ("user", "cli", "stop"),
    }


def test_insert_without_a_session_is_rejected() -> None:
    # When: --session is omitted
    result = runner.invoke(message_mod.app, ["insert", "orphan"])

    # Then: typer itself refuses the call
    assert result.exit_code != 0
    assert "--session" in result.output


def test_insert_rejects_empty_text_and_two_bodies() -> None:
    # Given: a session with an empty inbox
    session = _session()

    # When: whitespace-only text, no text at all, and text plus --stdin are tried
    blank = runner.invoke(message_mod.app, ["insert", "   ", "--session", session])
    neither = runner.invoke(message_mod.app, ["insert", "--session", session])
    both = runner.invoke(
        message_mod.app, ["insert", "words", "--session", session, "--stdin"], input="more"
    )

    # Then: each is a clean exit-1 error, not a traceback
    assert (blank.exit_code, neither.exit_code, both.exit_code) == (1, 1, 1)
    assert "Traceback" not in blank.output
    assert _messages(session) == []


def test_list_renders_pending_messages_and_the_empty_case() -> None:
    # Given: a session holding two messages
    session = _session()
    inbox.insert(session, "first body", kind="alarm", source="spawn")
    inbox.insert(session, "second\nbody", kind="user", source="cli")

    # When: the session is listed
    listed = runner.invoke(message_mod.app, ["list", "--session", session])

    # Then: one line per message, with the timestamp/kind/source and a flat preview
    lines = [line for line in listed.output.splitlines() if line.strip()]
    assert len(lines) == 2
    assert "alarm" in lines[0] and "spawn" in lines[0] and "first body" in lines[0]
    assert "user" in lines[1] and "second body" in lines[1]

    # And listing is non-destructive
    assert inbox.pending_count(session) == 2


def test_list_of_an_empty_inbox_is_silent() -> None:
    # Given: a session that never received anything
    session = _session()

    # When: it is listed
    result = runner.invoke(message_mod.app, ["list", "--session", session])

    # Then: nothing at all on stdout, exit 0 — so a monitor loop can poll it
    assert result.exit_code == 0
    assert result.output == ""


def test_list_and_clear_default_to_the_settings_session() -> None:
    # Given: a settings session holding one message
    session = _session()
    settings_store.save(settings_store.Settings(session=session))
    inbox.insert(session, "waiting")

    # When: neither subcommand is given --session
    listed = runner.invoke(message_mod.app, ["list"])
    cleared = runner.invoke(message_mod.app, ["clear", "--yes"])

    # Then: both acted on the settings session
    assert "waiting" in listed.output
    assert cleared.exit_code == 0
    assert inbox.pending_count(session) == 0


def test_clear_aborts_on_no_and_deletes_on_yes() -> None:
    # Given: a session holding two messages
    session = _session()
    inbox.insert(session, "one")
    inbox.insert(session, "two")

    # When: the prompt is answered "n"
    aborted = runner.invoke(message_mod.app, ["clear", "--session", session], input="n\n")

    # Then: the abort kept everything
    assert aborted.exit_code != 0
    assert inbox.pending_count(session) == 2

    # When: it is answered "y"
    accepted = runner.invoke(message_mod.app, ["clear", "--session", session], input="y\n")

    # Then: the confirmation discarded it
    assert accepted.exit_code == 0
    assert inbox.pending_count(session) == 0


def test_clear_of_an_empty_inbox_says_so() -> None:
    # Given: a session with nothing pending
    session = _session()

    # When: it is cleared
    result = runner.invoke(message_mod.app, ["clear", "--session", session, "--yes"])

    # Then: nothing is discarded and the empty case is stated
    assert result.exit_code == 0
    assert "no pending messages" in result.output


def test_a_bad_kind_fails_cleanly_instead_of_tracing_back() -> None:
    # Given: a session with an empty inbox
    session = _session()

    # When: an unknown kind is requested
    result = runner.invoke(message_mod.app, ["insert", "x", "--session", session, "--kind", "gossip"])

    # Then: the inbox's ValueError surfaces as a clean exit-1 error
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "gossip" in result.output
