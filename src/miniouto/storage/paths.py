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

# Bundled styles renamed at 0.8.1: the old seeded copy under ~/.miniouto/style/
# is removed only while it still matches the renamed bundle byte-for-byte (a
# user-customized file survives as a regular user style), and settings.style
# is repointed to the new name so the active style survives the rename.
_RENAMED_BUNDLED_STYLES: dict[str, str] = {
    "pro": "coding-pro",
    "ultra": "coding-ultra",
}


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

    _remove_renamed_seeds()


def _remove_renamed_seeds() -> None:
    for old, new in _RENAMED_BUNDLED_STYLES.items():
        old_path = STYLE_DIR / f"{old}.md"
        new_src = BUNDLED_STYLE_DIR / f"{new}.md"
        if not old_path.exists() or not new_src.is_file():
            continue
        if old_path.read_text(encoding="utf-8") != new_src.read_text(encoding="utf-8"):
            continue  # user-customized copy — keep it as a user style
        old_path.unlink()
        _repoint_settings_style(old, new)


def _repoint_settings_style(old: str, new: str) -> None:
    # Function-local import: settings.py imports this module, so a module-level
    # import would be circular.
    from . import toml_io

    raw = toml_io.load(SETTINGS_FILE)
    if raw.get("style") == old:
        raw["style"] = new
        toml_io.save(SETTINGS_FILE, raw)

