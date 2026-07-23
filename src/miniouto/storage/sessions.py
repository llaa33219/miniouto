"""Persistent chat sessions: restorable history + display turns.

Schema v2 splits a session into two sections:

- `history`: coreouto `Message` dumps (`model_dump(mode="json")`), system
  messages excluded. This is the exact transcript fed back to
  `agent.call_sync(history=...)` on resume, so it contains the full loop —
  intermediate assistant messages, tool calls, and tool results — matching
  coreouto `examples/21_loop_history.py`. Rewritten in full after every
  turn from `Response.messages` (minus system), which keeps it consistent
  with whatever the summarize hook compacted mid-loop.

- `turns`: display-only records (user prompt, loop events, final answer).
  Thinking/reasoning lives here as `kind="thinking"` events — coreouto's
  providers never put thinking into `Message` objects, so it cannot be
  part of the restorable history.

v1 files (no `version` key, flat `messages` list) are migrated on load.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import SESSION_DIR, ensure_dirs

SCHEMA_VERSION = 2


def _utcnow() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


@dataclass
class TurnRecord:
    """One user→assistant exchange plus the loop events produced during it."""

    user: str
    assistant: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    ts: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ts": self.ts, "user": self.user, "assistant": self.assistant}
        if self.events:
            d["events"] = self.events
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TurnRecord:
        events = d.get("events")
        return cls(
            user=str(d.get("user") or ""),
            assistant=str(d.get("assistant") or ""),
            events=[e for e in events if isinstance(e, dict)] if isinstance(events, list) else [],
            ts=str(d.get("ts") or _utcnow()),
        )


@dataclass
class SessionData:
    name: str
    history: list[dict[str, Any]] = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)


def path_for(name: str) -> Path:
    return SESSION_DIR / f"{name}.json"


def load(name: str) -> SessionData:
    """Load a session, tolerating corrupt files and migrating v1 envelopes.

    Never raises on bad content — a broken session file yields an empty
    SessionData rather than crashing the TUI/CLI.
    """

    ensure_dirs()
    p = path_for(name)
    if not p.exists():
        return SessionData(name=name)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SessionData(name=name)
    if not isinstance(raw, dict):
        return SessionData(name=name)
    if raw.get("version") == SCHEMA_VERSION:
        history = [d for d in raw.get("history", []) if isinstance(d, dict)]
        turns = [TurnRecord.from_dict(t) for t in raw.get("turns", []) if isinstance(t, dict)]
        return SessionData(name=name, history=history, turns=turns)
    return _migrate_v1(name, raw)


def save(name: str, data: SessionData) -> None:
    ensure_dirs()
    path_for(name).write_text(
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "session": name,
                "updated": _utcnow(),
                "history": data.history,
                "turns": [t.to_dict() for t in data.turns],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def record_turn(name: str, *, history: list[dict[str, Any]], turn: TurnRecord) -> None:
    """Rewrite `history` in full and append one display turn.

    The history rewrite is deliberate: the in-loop summarize hook mutates
    the message list in place, so only `Response.messages` (minus system)
    reflects what the model actually saw this turn.
    """

    data = load(name)
    data.history = list(history)
    data.turns.append(turn)
    save(name, data)


def create(name: str) -> None:
    """Touch an empty session so it shows up in `list_sessions`."""

    if not path_for(name).exists():
        save(name, SessionData(name=name))


def clear(name: str) -> None:
    p = path_for(name)
    if p.exists():
        p.unlink()


def list_sessions() -> list[str]:
    ensure_dirs()
    return sorted(p.stem for p in SESSION_DIR.glob("*.json"))


def _migrate_v1(name: str, raw: dict[str, Any]) -> SessionData:
    """Convert a v1 flat `messages` list into history + synthesized turns.

    v1 only ever stored user prompts and the final assistant message (plus
    a `(session created)` system marker, dropped here), so the migrated
    history is exactly those records re-shaped as coreouto Message dicts.
    """

    history: list[dict[str, Any]] = []
    turns: list[TurnRecord] = []
    current: TurnRecord | None = None
    messages = raw.get("messages")
    if isinstance(messages, list):
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role not in ("user", "assistant", "tool"):
                continue
            entry: dict[str, Any] = {"role": role, "content": m.get("content") or ""}
            if m.get("tool_calls"):
                entry["tool_calls"] = m["tool_calls"]
            if m.get("tool_call_id"):
                entry["tool_call_id"] = m["tool_call_id"]
            if m.get("name"):
                entry["name"] = m["name"]
            history.append(entry)
            if role == "user":
                current = TurnRecord(user=str(m.get("content") or ""))
                turns.append(current)
            elif role == "assistant" and current is not None:
                current.assistant = str(m.get("content") or "")
    return SessionData(name=name, history=history, turns=turns)
