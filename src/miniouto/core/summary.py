"""Explicit summarization / session-handoff contract.

The harness does **no** implicit summarization or compaction. When a turn has
consumed most of its context window, the caller (the inbox lane) delivers
:func:`build_summary_request` — a synthetic user message telling the model to
write its own summary documents and then call the ``continue_session`` tool. The
model never loses context silently: either it summarized, or it was cut off
with the warning quoted in the request.

The two halves of the hand-off live here because they must agree byte for byte
with what the model reads:

* :func:`build_summary_request` goes to the session that is running out of
  context. It names the directory (:func:`summary_dir_for`) and pins the FORMAT
  block, so the documents the model writes are the ones the next session reads.
* :func:`build_continuation_message` goes to the *new* session. It points at the
  same directory and, for a delegated run, re-states the alarm-session /
  result-file contract that must be finished after the hand-off.

Both texts are English and share the harness-wide alarm banner
(:data:`~miniouto.core.inbox.ALARM_HEADER`) with every other machine-inserted
message, so the model reads them as part of one convention. They are part of the
model-facing contract, so they are pinned as literal text with only the ``{...}``
values substituted. :data:`SUMMARY_FORMAT_SPEC` is the single source of truth
for the document layout and is embedded in the request verbatim.

State is deliberately in-process and minimal: :class:`Handoff` plus the
request/peek/consume triple is the seam the ``continue_session`` tool sets and
the chat loop observes, while the run-contract triple (set/get/clear) carries
the current run's identity and outstanding outcomes into that hand-off.
Nothing here touches disk, the provider, or a tool registry — this module is
the shared vocabulary other lanes wire together.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from ..paths_runtime import INVOCATION_CWD
from .inbox import ALARM_HEADER

# The project-local summary root: <invocation cwd>/.miniouto/summary/<session>/.
# Project-local (not ~/.miniouto/) because a summary is a project artifact — it
# is meant to be read, reviewed and committed with the work it describes.
_SUMMARY_ROOT = Path(".miniouto") / "summary"

# The document layout, embedded verbatim in the summary request. The seven
# sections are the handoff contract: 00-overview.md alone must be enough for the
# next session to resume without the conversation.
SUMMARY_FORMAT_SPEC = "\n".join(
    [
        "./.miniouto/summary/<session>/",
        "|-- 00-overview.md      (required)",
        "`-- NN-<topic>.md       (optional, as many as needed)",
        "",
        "[00-overview.md format]",
        "# <title> — session <session> summary",
        "(timestamp, status: in-progress/done)",
        "",
        "## 1. Background & Goal (Why)",
        "What was being done and why — original requirements, constraints, success criteria.",
        "",
        "## 2. Current State (What)",
        "- Done: changed file paths + key changes",
        "- In progress / unfinished: exactly where work stopped",
        "- Blockers",
        "",
        "## 3. Key Decisions & Rationale",
        "Decisions + reasons + why they must not be reverted.",
        "",
        "## 4. Next Steps",
        "Actionable ordered steps; name the first action.",
        "",
        "## 5. Gotchas & Verification",
        "Fragile areas, traps, how to verify (name the tests/commands).",
        "",
        "## 6. File Map",
        "Path -> one-line role.",
        "",
        "## 7. Original Brief & Contract",
        "The original request/delegation brief verbatim, plus alarm-session / "
        "result-return contract.",
    ]
)

# role/style share one line (identity), the two outcome contracts get their own.
_CONTRACT_FIELDS = (
    ("Role", "role"),
    ("Style", "style"),
    ("Alarm session", "alarm_session"),
    ("Result file", "return_path"),
)
_IDENTITY_FIELD_COUNT = 2


@dataclass(frozen=True)
class Handoff:
    """One pending hand-off: which session replaces which, and where to read from."""

    new_session: str
    from_session: str
    summary_dir: str


_HANDOFF_LOCK = threading.Lock()
_pending: Handoff | None = None

# The run contract: WHO is running (session) and WHAT it owes its caller
# (role/style identity plus the alarm-session / result-file outcomes). The
# entry layer registers it before the turn; the `continue_session` tool
# reads it so the hand-off can restate the contract in the new session.
# In-process by design, like the hand-off itself — a delegated run is its
# own process, so nothing here has to survive the turn.
_RUN_CONTRACT_LOCK = threading.Lock()
_run_contract: dict[str, str] | None = None

# Only non-empty values are stored, so `get_run_contract()` never hands the
# continuation message a half-blank contract block.
_RUN_CONTRACT_FIELDS = ("session", "role", "style", "alarm_session", "return_path")


def summary_dir_for(session: str) -> Path:
    """Where the summary documents of ``session`` live."""

    if not session or not session.strip():
        raise ValueError("session must be a non-empty name")
    return INVOCATION_CWD / _SUMMARY_ROOT / session


def build_summary_request(
    *,
    session: str,
    pct: int,
    window_tokens: int | None,
    used_tokens: int | None,
    summary_dir: Path,
) -> str:
    """The message asking the model to summarize before its context runs out.

    ``window_tokens`` / ``used_tokens`` are informational — pass ``None`` when
    the caller has no figures, and the parenthetical is dropped rather than
    rendered with a hole.
    """

    figures = ""
    if window_tokens is not None and used_tokens is not None:
        figures = f" ({used_tokens}/{window_tokens} tokens)"

    lines = [
        ALARM_HEADER,
        f"Context budget: {pct}% of this session's window is consumed{figures}. "
        "Explicit summarization is required now.",
        "",
        "This harness never summarizes or compacts implicitly — a summary "
        "exists only as files you write.",
        "",
        "HOW TO SUMMARIZE",
        f"1. Create the folder with Bash: mkdir -p {summary_dir}",
        "2. Write documents in this format (00-overview.md is required; add "
        "NN-<topic>.md as needed):",
        "",
        SUMMARY_FORMAT_SPEC,
        "",
        "3. Standard: reading ONLY this folder must be enough to continue the "
        "work. Record exact file paths, decision rationale, and verification "
        "commands.",
        "",
        "AFTER SUMMARIZING",
        f'Call continue_session(summary_dir="{summary_dir}"). This conversation '
        "ends there; a new session continues the work from the summary. If this "
        "run is delegated, the alarm session and result-return contract carry "
        "over automatically.",
        "",
        "IF YOU SKIP THIS",
        "- Requests fail at the context limit and earlier content is lost.",
        "- Finished work gets redone, settled decisions get overturned.",
        "- Always: write the summary documents -> call continue_session.",
    ]
    return "\n".join(lines)


def build_continuation_message(
    *,
    new_session: str,
    from_session: str,
    summary_dir: Path,
    contract: dict[str, str] | None = None,
) -> str:
    """The message that opens the new session, pointing it at the summary folder.

    ``new_session`` names the session being opened (callers stamp it on the
    alarm marker); the body itself only has to point at the summary and say which
    session it continues. ``contract`` is the delegation contract that must be
    finished after the hand-off — absent for a plain outo run.
    """

    lines = [
        ALARM_HEADER,
        "New session opened from a summary — continue the previous work here.",
        "",
        f"Summary folder: {summary_dir}",
        "",
        "WHAT TO DO",
        "1. Read every document in the folder (00-overview.md first).",
        f"2. Continue the work of session '{from_session}'.",
        "",
        "CAUTIONS",
        "- The summary is a record of past state — verify current code/state "
        "before trusting it.",
        "- Conversation history did not carry over; find needed context in the "
        "summary documents.",
        '- If the summary lists "Next Steps", follow them in order.',
        "",
        "RECOMMENDED",
        "- Check key files and test status before starting.",
        "- Keep the summary documents updated in the same folder as you work.",
    ]
    if contract is not None:
        lines += [
            "",
            "DELEGATION CONTRACT (carried over)",
            *_contract_lines(contract),
            "Fulfill this contract in THIS session — it was carried over from "
            "the previous one.",
        ]
    return "\n".join(lines)


def request_handoff(new_session: str, *, from_session: str, summary_dir: str) -> None:
    """Record a pending hand-off, replacing any previous one."""

    global _pending
    with _HANDOFF_LOCK:
        _pending = Handoff(
            new_session=new_session,
            from_session=from_session,
            summary_dir=summary_dir,
        )


def pending_handoff() -> Handoff | None:
    """Peek at the pending hand-off without consuming it."""

    with _HANDOFF_LOCK:
        return _pending


def consume_handoff() -> Handoff | None:
    """Read the pending hand-off and clear it; ``None`` when there was none."""

    global _pending
    with _HANDOFF_LOCK:
        handoff, _pending = _pending, None
        return handoff


def clear_handoff() -> None:
    """Drop any pending hand-off (tests, and a cancelled hand-off)."""

    global _pending
    with _HANDOFF_LOCK:
        _pending = None


def set_run_contract(
    *,
    session: str,
    role: str | None = None,
    style: str | None = None,
    alarm_session: str | None = None,
    return_path: str | None = None,
) -> None:
    """Register the running turn's identity and what it owes its caller.

    Overwrites any previous contract. Empty values are dropped, so a plain
    outo run with no delegation flags clears the slot instead of recording a
    blank contract the continuation message would have to filter out.
    """

    global _run_contract
    values = {
        "session": session,
        "role": role,
        "style": style,
        "alarm_session": alarm_session,
        "return_path": return_path,
    }
    contract = {
        key: value.strip()
        for key, value in values.items()
        if key in _RUN_CONTRACT_FIELDS and value and value.strip()
    }
    with _RUN_CONTRACT_LOCK:
        _run_contract = {key: contract[key] for key in _RUN_CONTRACT_FIELDS if key in contract} or None


def get_run_contract() -> dict[str, str] | None:
    """The registered run contract, or None when no run registered one.

    A copy, so a caller cannot mutate the slot in place.
    """

    with _RUN_CONTRACT_LOCK:
        return dict(_run_contract) if _run_contract is not None else None


def clear_run_contract() -> None:
    """Drop the run contract (tests, and the end of a run)."""

    global _run_contract
    with _RUN_CONTRACT_LOCK:
        _run_contract = None


def _contract_lines(contract: dict[str, str]) -> list[str]:
    """The carried-over contract, one line per present key (identity pairs up)."""

    values = [
        (label, contract[key].strip())
        for label, key in _CONTRACT_FIELDS
        if contract.get(key, "").strip()
    ]
    if not values:
        return []
    identity = " / ".join(f"{label}: {value}" for label, value in values[:_IDENTITY_FIELD_COUNT])
    return [f"- {identity}"] + [f"- {label}: {value}" for label, value in values[_IDENTITY_FIELD_COUNT:]]
