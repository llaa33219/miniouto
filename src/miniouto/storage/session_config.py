"""Per-session pinned provider configuration.

Provider/model/reasoning used to live ONLY in global state
(`settings.toml` plus `Provider.default_model` / `Provider.reasoning_effort`),
so a model switch in one process silently redirected every other process
bound to the same installation. This module gives each session its own pin
at ``session-config/<session>.toml``, so processes running different
settings side by side never interfere.

RESOLUTION ORDER (per key — the outo trio and the subagent trio are read
through the same two steps):

1. the explicit per-call override (``ChatOverrides``, or the ``--pm``
   value a CLI lane fills it from);
2. THIS SESSION'S PIN;
3. the global default — ``settings.provider`` / ``settings.subagent_*``
   plus the resolved provider record's ``default_model`` /
   ``reasoning_effort``. `core.runtime.default_provider_config` computes
   it (storage must not import core, so the caller injects it).

SEMANTICS OF A STORED VALUE: values are CONCRETE, and the empty string
means "follow the global default at read time". A pin is therefore a set
of overrides on the default, not a replacement for it — clearing a key
(`set_fields(session, model="")`) hands that key back to the global
default without touching the others.

FREEZE-AT-FIRST-USE: `effective()` materializes the pin. A session with
no pin file gets the current global default written as its pin and that
value returned, so a later global change (the TUI switching models in
another process) cannot redirect a session that is already running.
Writes are atomic (tmp file + ``os.replace``) and a corrupt pin file is
treated as absent rather than raised over — the same tolerance
`storage.sessions.load` applies to a session file.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import tomli_w

from . import toml_io
from .paths import SESSION_CONFIG_DIR

FIELDS = (
    "provider",
    "model",
    "reasoning",
    "subagent_provider",
    "subagent_model",
    "subagent_reasoning",
)


@dataclass(frozen=True)
class ProviderConfig:
    """One session's six provider keys; empty string = follow the default."""

    provider: str = ""
    model: str = ""
    reasoning: str = ""
    subagent_provider: str = ""
    subagent_model: str = ""
    subagent_reasoning: str = ""

    def as_dict(self) -> dict[str, str]:
        return {k: v for k, v in asdict(self).items() if v}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProviderConfig:
        return cls(**{key: str(data.get(key, "") or "") for key in FIELDS})


EMPTY = ProviderConfig()


def path_for(session: str) -> Path:
    return SESSION_CONFIG_DIR / f"{session}.toml"


def read(session: str) -> ProviderConfig | None:
    """The stored pin, or None when there is none (or it is unusable)."""

    path = path_for(session)
    if not path.is_file():
        return None
    try:
        raw = toml_io.load(path)
    except Exception:
        # A corrupt pin is not an error: the session simply follows the
        # default again (same spirit as `sessions.load`, invariant 7).
        return None
    return ProviderConfig.from_dict(raw)


def effective(
    session: str, *, default: Callable[[], ProviderConfig] | None = None
) -> ProviderConfig:
    """The session's effective config, freezing the default on first use.

    A stored pin is returned as-is. With no pin, the current default is
    computed, WRITTEN as the pin (so later global changes cannot move
    this session) and returned.
    """

    pinned = read(session)
    if pinned is not None:
        return pinned
    pinned = (default or (lambda: EMPTY))()
    _save(session, pinned)
    return pinned


def set_fields(session: str, **fields: str) -> ProviderConfig:
    """Merge `fields` into one session's pin; returns the new pin.

    An empty value stores nothing for that key, i.e. it clears the pin
    entry and the key falls back to the global default. Keys outside
    `FIELDS` are ignored — the pin is a fixed shape.
    """

    current = read(session) or EMPTY
    updates = {k: v for k, v in fields.items() if k in FIELDS}
    merged = replace(current, **{k: str(v or "") for k, v in updates.items()})
    _save(session, merged)
    return merged


def seed_from(new_session: str, source_session: str) -> None:
    """Copy the source session's pin into a freshly minted session.

    Copy-if-absent: a session that already carries a pin keeps it, so a
    hand-off (or any other mint) never silently re-points a configured
    session. A source with no pin seeds nothing — the new session freezes
    the default itself on first use.
    """

    if new_session == source_session:
        return
    source = read(source_session)
    if source is None or read(new_session) is not None:
        return
    _save(new_session, source)


def clear(session: str) -> None:
    """Drop the pin; the session follows the global default again."""

    with contextlib.suppress(FileNotFoundError):
        path_for(session).unlink()


def _save(session: str, config: ProviderConfig) -> None:
    """Write the pin atomically: a reader sees the old or the new file."""

    target = path_for(session)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as f:
            tomli_w.dump(config.as_dict(), f)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
