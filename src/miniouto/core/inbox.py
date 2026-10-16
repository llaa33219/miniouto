"""Cross-process message inbox: one file per pending message, per session.

A session's inbox is a directory under ``storage.paths.INBOX_DIR`` holding one
JSON file per pending message. It is the durable hand-off from a detached
``miniouto chat --bg`` run back to the session that launched it: writer and drainer
are separate OS processes, never two halves of one address space, so the channel
has to survive the writer exiting.

Concurrency, and the two rules that follow from this file-per-message shape:

1. **Insert-vs-drain is safe by construction.** A writer never appends to a
   file somebody else is reading: it creates its own file and renames it into
   place with ``os.replace``, which is atomic on every POSIX and Windows
   filesystem. A drainer therefore sees a message either not at all or
   complete — never half-written. A message inserted while a drain is in
   progress is simply picked up by the next drain, so a writer racing a
   drainer is never lost, only deferred. (An append-only ``.jsonl`` cannot
   offer this: consuming it needs a lock, and the lock would be held across
   the read while the writer waits on it.)
2. **One consumer per session, deliberately.** Nothing serializes two
   *drainers*: whichever process lists the directory first unlinks the file and
   the other never sees it. Two live processes bound to one session would steal
   each other's messages. Isolate them with a distinct ``MINIOUTO_HOME`` (or a
   distinct session name) instead of adding a locking protocol here.

Delivery is at-least-once: a message file is unlinked only after it has been
read and parsed, so a crash in between re-delivers rather than drops. As with
session files (invariant 7), corrupt content never raises — an unparseable file
is renamed ``<stem>.bad`` and the drain continues.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..storage.paths import INBOX_DIR

# The single source of truth for the alarm banner: the bg-run side
# (cli/chat.py) stamps every completion message with it, the repetition
# restart path builds its own alarm from it (core/repetition.py:alarm_text),
# and the TUI matches on it when rendering.
ALARM_HEADER = "[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]"

ALARM_KIND = "alarm"
USER_KIND = "user"
VALID_KINDS = (ALARM_KIND, USER_KIND)

MAX_INBOX_MESSAGES = 200
MAX_TEXT_CHARS = 100_000

_STAMP_FORMAT = "%Y%m%d-%H%M%S%f"
_SUFFIX = ".json"


@dataclass(frozen=True)
class InboxMessage:
    id: str
    kind: str
    text: str
    ts: str
    source: str
    meta: dict[str, Any]


def inbox_dir(session: str) -> Path:
    if not session or not session.strip():
        raise ValueError("session must be a non-empty name")
    return INBOX_DIR / session


def insert(
    session: str,
    text: str,
    *,
    kind: str = ALARM_KIND,
    source: str = "cli",
    meta: dict[str, Any] | None = None,
) -> str:
    """Write one message file atomically; return its id (the filename stem)."""

    directory = inbox_dir(session)
    _validate(kind=kind, text=text, meta=meta)

    now = datetime.now()
    name = f"{now.strftime(_STAMP_FORMAT)}-{secrets.token_hex(3)}"
    payload = {
        "kind": kind,
        "text": text,
        "ts": now.isoformat(timespec="seconds"),
        "source": source,
        "meta": meta if meta is not None else {},
    }

    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"{name}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, directory / f"{name}{_SUFFIX}")

    prune(session)
    return name


def drain(session: str) -> list[InboxMessage]:
    """Read and unlink every message file, oldest first. Never raises on content."""

    directory = inbox_dir(session)
    if not directory.is_dir():
        return []

    messages: list[InboxMessage] = []
    for path in directory.glob(f"*{_SUFFIX}"):
        message = _read_one(path)
        if message is not None:
            messages.append(message)
    # Ids are the filenames, whose zero-padded microsecond stamps sort in
    # insertion order — so the result is ordered regardless of listdir order.
    messages.sort(key=lambda m: m.id)
    return messages


def pending_count(session: str) -> int:
    directory = inbox_dir(session)
    if not directory.is_dir():
        return 0
    return sum(1 for _ in directory.glob(f"*{_SUFFIX}"))


def prune(session: str, *, keep: int = MAX_INBOX_MESSAGES) -> int:
    """Delete the oldest message files beyond `keep` (newest kept). Returns count."""

    directory = inbox_dir(session)
    if not directory.is_dir():
        return 0

    paths = sorted(directory.glob(f"*{_SUFFIX}"))
    doomed = paths[: max(len(paths) - keep, 0)]
    deleted = 0
    for path in doomed:
        try:
            path.unlink()
        except OSError:
            continue
        deleted += 1
    return deleted


def _read_one(path: Path) -> InboxMessage | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        message = InboxMessage(
            id=path.stem,
            kind=payload["kind"],
            text=payload["text"],
            ts=payload["ts"],
            source=payload["source"],
            meta=payload["meta"] if isinstance(payload["meta"], dict) else {},
        )
    except (OSError, ValueError, KeyError, TypeError):
        _quarantine(path)
        return None
    # Unlink last: a crash before this re-delivers instead of dropping.
    path.unlink(missing_ok=True)
    return message


def _quarantine(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.replace(path, path.with_suffix(".bad"))


def _validate(*, kind: str, text: str, meta: dict[str, Any] | None) -> None:
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
    if not text or not text.strip():
        raise ValueError("text must not be empty or whitespace-only")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"text exceeds MAX_TEXT_CHARS ({MAX_TEXT_CHARS})")
    if meta is not None and not isinstance(meta, dict):
        raise ValueError("meta must be a dict when provided")
