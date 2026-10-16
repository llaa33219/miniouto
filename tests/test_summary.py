"""Lock the explicit summarization / handoff contract from core/summary.py."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from miniouto.core import inbox, summary
from miniouto.core.summary import (
    SUMMARY_FORMAT_SPEC,
    Handoff,
    build_continuation_message,
    build_summary_request,
    clear_handoff,
    clear_run_contract,
    consume_handoff,
    get_run_contract,
    pending_handoff,
    request_handoff,
    set_run_contract,
    summary_dir_for,
)
from miniouto.paths_runtime import INVOCATION_CWD

SESSION = "s1"
SUMMARY_DIR = summary_dir_for(SESSION)


@pytest.fixture(autouse=True)
def _no_leftover_handoff() -> Iterator[None]:
    """The handoff state is process-global; never let it leak between tests."""

    clear_handoff()
    clear_run_contract()
    yield
    clear_handoff()
    clear_run_contract()


def _request(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "session": SESSION,
        "pct": 80,
        "window_tokens": 200_000,
        "used_tokens": 160_000,
        "summary_dir": SUMMARY_DIR,
    }
    kwargs.update(overrides)
    return build_summary_request(**kwargs)  # type: ignore[arg-type]


def test_summary_dir_for_is_project_local_under_the_invocation_cwd() -> None:
    # Given: a session name
    # When: its summary directory is resolved
    directory = summary_dir_for("my-session")
    # Then: it is <invocation cwd>/.miniouto/summary/<session>, not under ~/.miniouto
    assert directory == INVOCATION_CWD / ".miniouto" / "summary" / "my-session"
    assert directory.parts[-3:] == (".miniouto", "summary", "my-session")


def test_summary_dir_for_rejects_an_empty_session() -> None:
    # Given / When / Then: a session name is required
    with pytest.raises(ValueError, match="session must be a non-empty name"):
        summary_dir_for("   ")


def test_summary_request_header_is_the_shared_alarm_banner() -> None:
    # Given: a request built for 93% context usage
    message = build_summary_request(
        session=SESSION,
        pct=93,
        window_tokens=None,
        used_tokens=None,
        summary_dir=SUMMARY_DIR,
    )
    # When / Then: the first line is the harness-wide alarm banner, verbatim
    assert message.splitlines()[0] == inbox.ALARM_HEADER
    assert summary.ALARM_HEADER == "[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]"
    # And the percentage is reported on the second line, not in the banner
    assert message.splitlines()[1].startswith("Context budget: 93% of this session's window")


def test_summary_request_names_every_section_in_order() -> None:
    # Given: a summary request
    message = _request()
    # When / Then: all five sections are present, in the pinned order
    positions = [
        message.index(section)
        for section in (
            "This harness never summarizes or compacts implicitly",
            "HOW TO SUMMARIZE",
            "AFTER SUMMARIZING",
            "IF YOU SKIP THIS",
        )
    ]
    assert positions == sorted(positions)


def test_summary_request_embeds_the_format_spec_verbatim() -> None:
    # Given: a summary request
    message = _request()
    # When / Then: the FORMAT block is the module constant, unmodified
    assert SUMMARY_FORMAT_SPEC in message
    assert "|-- 00-overview.md      (required)" in message
    for heading in (
        "## 1. Background & Goal (Why)",
        "## 2. Current State (What)",
        "## 3. Key Decisions & Rationale",
        "## 4. Next Steps",
        "## 5. Gotchas & Verification",
        "## 6. File Map",
        "## 7. Original Brief & Contract",
    ):
        assert heading in message


def test_summary_request_tells_the_model_the_exact_continue_session_call() -> None:
    # Given: a summary request for a session
    message = _request()
    # When / Then: the next action is spelled out with the real directory
    assert f'continue_session(summary_dir="{SUMMARY_DIR}")' in message
    assert f"mkdir -p {SUMMARY_DIR}" in message


def test_summary_request_reports_both_token_figures_when_known() -> None:
    # Given: known context-window and used-token counts
    message = _request(pct=80, window_tokens=200_000, used_tokens=160_000)
    # When / Then: the second line names used/window, in that order
    assert (
        "Context budget: 80% of this session's window is consumed "
        "(160000/200000 tokens). Explicit summarization is required now." in message
    )


@pytest.mark.parametrize(
    ("window_tokens", "used_tokens"),
    [(None, None), (200_000, None), (None, 160_000)],
)
def test_summary_request_omits_unknown_token_figures(
    window_tokens: int | None, used_tokens: int | None
) -> None:
    # Given: at least one token figure missing
    # When: the request is built
    message = _request(window_tokens=window_tokens, used_tokens=used_tokens)
    # Then: the parenthetical is gone entirely and no placeholder hole is left
    assert (
        "Context budget: 80% of this session's window is consumed. "
        "Explicit summarization is required now." in message
    )
    assert "tokens)" not in message
    assert "None" not in message


def test_continuation_message_header_and_folder_are_exact() -> None:
    # Given: a continuation for a new session taking over from SESSION
    message = build_continuation_message(
        new_session="s2", from_session=SESSION, summary_dir=SUMMARY_DIR
    )
    # When / Then: banner, folder and the previous session name are all named
    assert message.splitlines()[0] == inbox.ALARM_HEADER
    assert summary.ALARM_HEADER == "[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]"
    assert f"Summary folder: {SUMMARY_DIR}" in message
    assert f"Continue the work of session '{SESSION}'." in message
    assert "WHAT TO DO" in message and "CAUTIONS" in message


def test_continuation_message_carries_the_delegation_contract_when_present() -> None:
    # Given: a delegated run's contract being carried over
    contract = {
        "role": "editor",
        "style": "coding",
        "alarm_session": "boss",
        "return_path": "/tmp/answer.md",
    }
    # When: the continuation for the new session is built
    message = build_continuation_message(
        new_session="s2",
        from_session=SESSION,
        summary_dir=SUMMARY_DIR,
        contract=contract,
    )
    # Then: every key is restated, and the obligation to finish is explicit
    assert "DELEGATION CONTRACT (carried over)" in message
    assert "- Role: editor / Style: coding" in message
    assert "- Alarm session: boss" in message
    assert "- Result file: /tmp/answer.md" in message
    assert (
        "Fulfill this contract in THIS session — it was carried over from the "
        "previous one." in message
    )


def test_continuation_message_omits_the_contract_block_for_a_plain_run() -> None:
    # Given: a plain outo run, no contract
    # When: the continuation is built
    message = build_continuation_message(
        new_session="s2", from_session=SESSION, summary_dir=SUMMARY_DIR
    )
    # Then: no contract block, and no empty version of it
    assert "DELEGATION CONTRACT" not in message
    assert "carried over" not in message


def test_continuation_message_renders_only_the_contract_keys_it_has() -> None:
    # Given: a partial contract (alarm session only)
    # When: the continuation is built
    message = build_continuation_message(
        new_session="s2",
        from_session=SESSION,
        summary_dir=SUMMARY_DIR,
        contract={"alarm_session": "boss"},
    )
    # Then: only that key is rendered — no empty Role/Style lines
    assert "- Alarm session: boss" in message
    assert "Role:" not in message
    assert "Style:" not in message
    assert "Result file:" not in message


def test_handoff_request_is_pending_and_carries_both_sessions() -> None:
    # Given: a continue_session tool call recorded a hand-off
    request_handoff("s2", from_session=SESSION, summary_dir=str(SUMMARY_DIR))
    # When / Then: it is visible, unconsumed, with every field set
    assert pending_handoff() == Handoff(
        new_session="s2", from_session=SESSION, summary_dir=str(SUMMARY_DIR)
    )


def test_handoff_consume_returns_the_handoff_once_and_clears_it() -> None:
    # Given: a pending hand-off
    request_handoff("s2", from_session=SESSION, summary_dir=str(SUMMARY_DIR))
    # When: the chat loop consumes it
    first = consume_handoff()
    # Then: it is delivered once, and a second consume finds nothing
    assert first == Handoff(new_session="s2", from_session=SESSION, summary_dir=str(SUMMARY_DIR))
    assert pending_handoff() is None
    assert consume_handoff() is None


def test_handoff_request_overwrites_a_pending_one() -> None:
    # Given: a hand-off that was never consumed
    request_handoff("s2", from_session=SESSION, summary_dir="/old")
    # When: a second hand-off is requested
    request_handoff("s3", from_session="s2", summary_dir="/new")
    # Then: only the newer one is pending
    assert consume_handoff() == Handoff(new_session="s3", from_session="s2", summary_dir="/new")


def test_clear_handoff_drops_a_pending_handoff() -> None:
    # Given: a pending hand-off
    request_handoff("s2", from_session=SESSION, summary_dir=str(SUMMARY_DIR))
    # When: it is cleared
    clear_handoff()
    # Then: nothing is pending and nothing can be consumed
    assert pending_handoff() is None
    assert consume_handoff() is None


def test_handoff_is_frozen() -> None:
    # Given: a hand-off record
    handoff = Handoff(new_session="s2", from_session=SESSION, summary_dir="/d")
    # When / Then: it is immutable, like the other on-disk contracts
    assert isinstance(handoff, summary.Handoff)
    with pytest.raises(FrozenInstanceError):
        handoff.new_session = "s3"  # type: ignore[misc]


def test_summary_dir_is_a_path_the_mkdir_command_can_use() -> None:
    # Given: the directory named in the request
    # When / Then: it renders as an absolute path with no shell-hostile quoting
    directory: Path = summary_dir_for(SESSION)
    assert directory.is_absolute()
    assert " " not in str(directory)


def test_run_contract_is_absent_until_a_run_registers_one() -> None:
    # Given: a process where no run registered its identity
    # When / Then: the tool has no contract to carry over
    assert get_run_contract() is None


def test_run_contract_keeps_only_the_fields_the_run_actually_owes() -> None:
    # Given: a plain outo run with no delegation flags
    set_run_contract(session=SESSION)
    # When: the tool reads the contract
    contract = get_run_contract()
    # Then: only the session is there — a blank contract is not a contract
    assert contract == {"session": SESSION}


def test_run_contract_carries_a_delegated_runs_full_contract() -> None:
    # Given: a delegated run registered before its turn
    set_run_contract(
        session="spawn-1",
        role="validator",
        style="coding-work",
        alarm_session="boss",
        return_path="/tmp/answer.md",
    )
    # When: the tool reads the contract
    # Then: every key the continuation message renders is present
    assert get_run_contract() == {
        "session": "spawn-1",
        "role": "validator",
        "style": "coding-work",
        "alarm_session": "boss",
        "return_path": "/tmp/answer.md",
    }


def test_run_contract_get_returns_a_copy() -> None:
    # Given: a registered contract
    set_run_contract(session=SESSION, role="editor")
    # When: a caller mutates what it was handed
    contract = get_run_contract()
    contract["role"] = "tampered"  # type: ignore[index]
    # Then: the registered slot is untouched
    assert get_run_contract() == {"session": SESSION, "role": "editor"}


def test_run_contract_registration_overwrites_the_previous_one() -> None:
    # Given: a delegated run that already registered
    set_run_contract(session=SESSION, alarm_session="boss")
    # When: the next run registers a plain one
    set_run_contract(session="s2")
    # Then: only the newer contract stands
    assert get_run_contract() == {"session": "s2"}


def test_clear_run_contract_drops_the_slot() -> None:
    # Given: a registered contract
    set_run_contract(session=SESSION, alarm_session="boss")
    # When: the run ends
    clear_run_contract()
    # Then: nothing is left to carry over
    assert get_run_contract() is None
