"""Provider registry persisted as TOML."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import toml_io
from .paths import PROVIDERS_FILE, ensure_dirs

SOURCE_CUSTOM = "custom"
SOURCE_LMA = "lma"
VALID_SOURCES = (SOURCE_CUSTOM, SOURCE_LMA)


@dataclass
class Provider:
    name: str
    api_format: str = "openai"
    base_url: str = ""
    api_key: str = ""
    default_model: str = ""
    source: str = SOURCE_CUSTOM
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, {}, "")}

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> Provider:
        known = {"name", "api_format", "base_url", "api_key", "default_model", "source"}
        extra = {k: v for k, v in data.items() if k not in known}
        source = data.get("source") or SOURCE_CUSTOM
        if source not in VALID_SOURCES:
            source = SOURCE_CUSTOM
        return cls(
            name=name,
            api_format=data.get("api_format", "openai"),
            base_url=data.get("base_url", ""),
            api_key=data.get("api_key", ""),
            default_model=data.get("default_model", ""),
            source=source,
            extra=extra,
        )


def load_all() -> dict[str, Provider]:
    ensure_dirs()
    raw = toml_io.load(PROVIDERS_FILE)
    return {name: Provider.from_dict(name, body) for name, body in raw.items()}


def get(name: str) -> Provider | None:
    return load_all().get(name)


def upsert(provider: Provider) -> None:
    data = toml_io.load(PROVIDERS_FILE)
    data[provider.name] = provider.to_dict()
    toml_io.save(PROVIDERS_FILE, data)


def remove(name: str) -> bool:
    data = toml_io.load(PROVIDERS_FILE)
    if name in data:
        del data[name]
        toml_io.save(PROVIDERS_FILE, data)
        return True
    return False
