"""TOML config IO."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomli_w
import tomllib


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        tomli_w.dump(data, f)

