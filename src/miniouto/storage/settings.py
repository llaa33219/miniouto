"""User-level settings: active provider, style, session."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from . import toml_io
from .paths import SETTINGS_FILE, ensure_dirs


@dataclass
class Settings:
    provider: str = ""
    model: str = ""
    style: str = "default"
    session: str = "default"
    theme: str = ""
    # Subagent provider/model/reasoning overrides. Empty string = inherit
    # outo's provider/model and fall back to the subagent provider's
    # `reasoning_effort`. NOTE: `merge()` and `update()` skip empty-string
    # values, so these cannot be cleared through `update()` — callers must
    # clear them via `save(replace(load(), subagent_provider="", ...))`
    # (see cli/subagent.py's `clear` command and `set --reasoning ""`).
    subagent_provider: str = ""
    subagent_model: str = ""
    subagent_reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}

    def merge(self, overrides: dict[str, Any]) -> Settings:
        merged = asdict(self)
        for k, v in overrides.items():
            if v not in (None, ""):
                merged[k] = v
        return Settings(**merged)


def load() -> Settings:
    ensure_dirs()
    raw = toml_io.load(SETTINGS_FILE)
    return Settings(**{k: raw.get(k, getattr(Settings(), k)) for k in asdict(Settings())})


def save(settings: Settings) -> None:
    ensure_dirs()
    toml_io.save(SETTINGS_FILE, settings.to_dict())


def update(**kwargs: Any) -> Settings:
    current = load()
    merged = current.merge(kwargs)
    save(merged)
    return merged
