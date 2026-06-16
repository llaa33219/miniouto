"""Filesystem layout and paths for miniouto state."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("MINIOUTO_HOME") or Path.home() / ".miniouto").expanduser()
PROVIDERS_FILE = ROOT / "providers.toml"
SETTINGS_FILE = ROOT / "settings.toml"
STYLE_DIR = ROOT / "style"
SESSION_DIR = ROOT / "sessions"
LOG_DIR = ROOT / "logs"


def ensure_dirs() -> None:
    """Create the on-disk skeleton if missing, and seed bundled defaults."""

    for p in (ROOT, STYLE_DIR, SESSION_DIR, LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)
    from . import styles as _styles

    bundled_dir = Path(__file__).parent.parent / "default_style"
    if bundled_dir.is_dir():
        for src in bundled_dir.glob("*.md"):
            target = STYLE_DIR / src.name
            if not target.exists():
                target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        _ = _styles

