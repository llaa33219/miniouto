"""Persistent chat history keyed by session name."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import SESSION_DIR, ensure_dirs


@dataclass
class MessageRecord:
    role: str
    content: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    ts: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds") + "Z")

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], "")}


def path_for(name: str) -> Path:
    return SESSION_DIR / f"{name}.json"


def load(name: str) -> list[MessageRecord]:
    ensure_dirs()
    p = path_for(name)
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    return [MessageRecord(**m) for m in raw.get("messages", [])]


def save(name: str, messages: list[MessageRecord]) -> None:
    ensure_dirs()
    p = path_for(name)
    p.write_text(
        json.dumps(
            {
                "session": name,
                "updated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "messages": [m.to_dict() for m in messages],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def append(name: str, message: MessageRecord) -> list[MessageRecord]:
    current = load(name)
    current.append(message)
    save(name, current)
    return current


def clear(name: str) -> None:
    p = path_for(name)
    if p.exists():
        p.unlink()


def list_sessions() -> list[str]:
    ensure_dirs()
    return sorted(p.stem for p in SESSION_DIR.glob("*.json"))


def to_coreouto_messages(messages: list[MessageRecord]) -> list[dict[str, Any]]:
    """Convert to plain dicts that coreouto's Message model can ingest."""

    out: list[dict[str, Any]] = []
    for m in messages:
        d: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            d["tool_calls"] = m.tool_calls
        if m.tool_call_id is not None:
            d["tool_call_id"] = m.tool_call_id
        if m.name is not None:
            d["name"] = m.name
        out.append(d)
    return out
