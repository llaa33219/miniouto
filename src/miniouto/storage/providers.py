"""Provider registry persisted as TOML."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import toml_io
from .paths import PROVIDERS_FILE, ensure_dirs


@dataclass
class Provider:
    name: str
    api_format: str = "openai"
    base_url: str = ""
    api_key: str = ""
    default_model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, {}, "")}

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> Provider:
        known = {"name", "api_format", "base_url", "api_key", "default_model"}
        extra = {k: v for k, v in data.items() if k not in known}
        clean = {k: data.get(k, "") for k in ("api_format", "base_url", "api_key", "default_model")}
        return cls(name=name, extra=extra, **clean)


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
