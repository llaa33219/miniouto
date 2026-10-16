"""`continue_session`: hand the work to a NEW session that reads the summary folder.

The model calls this after writing its own summary documents (the summary
request pinned both the folder and the layout, in ``core.summary``). Three
things happen, in this order, and all three are cheap and reversible enough to
retry: the folder is validated, a continuation message is dropped into a
freshly minted ``cont-*`` session's inbox, and the hand-off is recorded in the
in-process slot the entry layer consumes to move the conversation over.

Nothing here decides whether the folder is *good* — only that it is a
directory with at least one markdown document, because the only thing that
can judge the summary is the session that reads it.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path

from ..core import inbox, summary
from ..paths_runtime import INVOCATION_CWD
from ..storage import session_config

_SESSION_STAMP = "%Y%m%d-%H%M%S"
_MARKDOWN = "*.md"

DESCRIPTION = (
    "Continue this work in a NEW session, reading the summary documents you "
    "wrote. Call this ONLY as the last action of a turn that was asked to "
    "summarize: write the summary documents into summary_dir first "
    "(00-overview.md required, NN-<topic>.md optional), then pass that same "
    "folder here. This tool ends the CURRENT conversation — the new session "
    "opens with a message pointing at the summary folder and continues from "
    "there, so after calling it you must not do any more work in this "
    "session: stop and end your turn. The history is not carried over; the "
    "summary documents are the only hand-off, so they must be complete. "
    "Delegation contracts (alarm session, result file, role, style) are "
    "carried over to the new session automatically and must be completed "
    "there. summary_dir must be an existing folder containing at least one "
    "*.md document; relative paths resolve against the directory miniouto "
    "was invoked from."
)

_ERROR = "continue_session failed — no handoff was made."

_RETRY_HINT = (
    "(write the summary documents first, then call continue_session)."
)


def continue_session(summary_dir: str) -> str:
    """Validate the summary folder, open a ``cont-*`` session, record the hand-off."""

    directory, error = _resolve(summary_dir)
    if error is not None:
        return error

    contract = summary.get_run_contract()
    from_session = (contract or {}).get("session", "").strip() or directory.name
    new_session = _mint_session()
    # The continuation keeps the provider settings it was working under:
    # the new session inherits the source session's pin, so a hand-off
    # cannot silently move the work to another model/provider.
    session_config.seed_from(new_session, from_session)
    message = summary.build_continuation_message(
        new_session=new_session,
        from_session=from_session,
        summary_dir=directory,
        contract=contract,
    )
    inbox.insert(
        new_session,
        message,
        kind=inbox.ALARM_KIND,
        source="summary",
        meta={"from": from_session},
    )
    summary.request_handoff(
        new_session, from_session=from_session, summary_dir=str(directory)
    )
    return _ack(new_session, directory, contract)


def _resolve(summary_dir: str) -> tuple[Path, str | None]:
    """The absolute summary folder, or the error string to hand back to the model."""

    raw = (summary_dir or "").strip()
    if not raw:
        return Path(), f"{_ERROR}\nReason: summary_dir is empty. {_RETRY_HINT}"
    directory = Path(raw).expanduser()
    if not directory.is_absolute():
        directory = INVOCATION_CWD / directory
    directory = directory.resolve()

    if not directory.exists():
        reason = "the folder does not exist."
    elif not directory.is_dir():
        reason = "the path is not a folder."
    elif not next(directory.glob(_MARKDOWN), None):
        reason = f"the folder holds no {_MARKDOWN} summary document."
    else:
        return directory, None
    return Path(), (
        f"{_ERROR}\nReason: {reason} (path: {directory})\n{_RETRY_HINT}"
    )


def _mint_session() -> str:
    stamp = datetime.now().strftime(_SESSION_STAMP)
    return f"cont-{stamp}-{secrets.token_hex(3)}"


def _ack(new_session: str, directory: Path, contract: dict[str, str] | None) -> str:
    lines = [
        "Handoff complete — this conversation is now closed. "
        f"New session '{new_session}' continues the work from {directory}."
    ]
    if contract and any(contract.get(key) for key in ("role", "style", "alarm_session", "return_path")):
        lines.append(
            "Delegation contract carried over (alarm session "
            f"'{contract.get('alarm_session', '')}', result file "
            f"'{contract.get('return_path', '')}')."
        )
    lines.append("Stop working in this session now.")
    return "\n".join(lines)
