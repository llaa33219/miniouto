"""Lock the path constants and `ensure_dirs()` skeleton in storage/paths.py."""

from __future__ import annotations

import shutil

from miniouto.storage import paths


def test_inbox_spawn_log_and_screen_dirs_live_under_root() -> None:
    # Given: the module's path constants
    # When / Then: the three new directories are children of ROOT
    assert paths.INBOX_DIR == paths.ROOT / "inbox"
    assert paths.SPAWN_LOG_DIR == paths.ROOT / "spawn-logs"
    assert paths.COMPUTER_SCREENS_DIR == paths.ROOT / "computer-screens"


def test_ensure_dirs_creates_the_three_new_directories() -> None:
    # Given: the skeleton built once, with the three new directories removed again
    paths.ensure_dirs()
    for directory in (paths.INBOX_DIR, paths.SPAWN_LOG_DIR, paths.COMPUTER_SCREENS_DIR):
        shutil.rmtree(directory)
        assert not directory.exists()

    # When: the skeleton is rebuilt
    paths.ensure_dirs()

    # Then: all three directories exist again
    assert paths.INBOX_DIR.is_dir()
    assert paths.SPAWN_LOG_DIR.is_dir()
    assert paths.COMPUTER_SCREENS_DIR.is_dir()
