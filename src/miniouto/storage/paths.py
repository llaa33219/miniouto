"""Filesystem layout and paths for miniouto state."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("MINIOUTO_HOME") or Path.home() / ".miniouto").expanduser()
PROVIDERS_FILE = ROOT / "providers.toml"
SETTINGS_FILE = ROOT / "settings.toml"
STYLE_DIR = ROOT / "style"
STYLE_REPOS_FILE = ROOT / "style_repos.toml"
SESSION_DIR = ROOT / "sessions"
LOG_DIR = ROOT / "logs"
BUNDLED_STYLE_DIR = Path(__file__).parent.parent / "default_style"


def ensure_dirs() -> None:
    """Create the on-disk skeleton if missing, and refresh bundled defaults.

    Bundled styles are force-refreshed: any installed file whose name matches
    a bundled template is overwritten with the current bundled content (only
    written when the content actually differs, to avoid needless disk churn).
    User-created styles with names that do not match a bundled template are
    left untouched.
    """

    for p in (ROOT, STYLE_DIR, SESSION_DIR, LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)

    if BUNDLED_STYLE_DIR.is_dir():
        for src in BUNDLED_STYLE_DIR.glob("*.md"):
            target = STYLE_DIR / src.name
            bundled_text = src.read_text(encoding="utf-8")
            if not target.exists() or target.read_text(encoding="utf-8") != bundled_text:
                target.write_text(bundled_text, encoding="utf-8")

