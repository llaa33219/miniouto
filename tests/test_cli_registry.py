"""The CLI root must expose every command the harness ships, and `computer show`
must render the art the Computer tool saved.

The root app is the one surface an automation script can rely on, so this is
the only place that asserts the registry as a whole: a command group silently
dropped from `cli/__init__.py` is otherwise invisible until a user hits it.

Each `computer show` test redirects `COMPUTER_SCREENS_DIR` at its own tmp
directory, so a capture another test wrote can never change which file is the
"newest" one.
"""

from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner

from miniouto.cli import app as root_app
from miniouto.cli import computer as computer_mod
from miniouto.storage import settings as settings_store
from miniouto.tools.ascii_art import HALF_BLOCK, build_art

runner = CliRunner()

#: A two-row capture: two cells per row, distinct fg/bg per cell so the body
#: is real truecolour half-block art and `art_to_plain` has colour to fold.
_META = {
    "screen": "scr-1",
    "source": "4x4",
    "grid": "2x2",
    "cell": "2x2",
    "mode": "halfblock-truecolor",
}
_BODY = (
    "\x1b[38;2;255;0;0m\x1b[48;2;0;0;255m▀▀\x1b[0m\n"
    "\x1b[38;2;0;255;0m\x1b[48;2;255;255;0m▀▀\x1b[0m"
)


def _write_art(directory: Path, name: str = "scr-1-2026-01-01-00-00-00.art") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(build_art(_META, _BODY), encoding="utf-8")
    return path


def _rows(output: str) -> list[str]:
    """The art rows only — line 0 is the dim header."""
    return output.splitlines()[1:]


# ── the registry ──────────────────────────────────────────────────────────────


def test_root_help_lists_every_top_level_command() -> None:
    # When: the root app is asked for its help
    result = runner.invoke(root_app, ["--help"])

    # Then: chat's groups and the new commands are all reachable
    assert result.exit_code == 0
    for command in ("message", "computer", "chat", "provider", "style"):
        assert command in result.output


def test_the_spawn_command_is_gone_and_delegation_is_chat_flags() -> None:
    # When: the root help and the chat command's own help are asked for
    root_help = runner.invoke(root_app, ["--help"])
    spawn_help = runner.invoke(root_app, ["spawn", "--help"])
    chat_help = runner.invoke(root_app, ["chat", "--help"])

    # Then: there is no `spawn` command, and every delegation flag moved to chat
    assert "spawn" not in root_help.output
    assert spawn_help.exit_code == 2
    assert chat_help.exit_code == 0
    for flag in ("--role", "--use-subagent-provider", "--alarm-session", "--return", "--bg"):
        assert flag in chat_help.output


def test_the_provider_trio_has_exactly_one_spelling() -> None:
    # When: the chat command's help is asked for its provider surface
    chat_help = runner.invoke(root_app, ["chat", "--help"])

    # Then: `--pm` documents its format with an example, and the flags it
    # replaced are gone from the command entirely
    assert chat_help.exit_code == 0
    assert "--pm" in chat_help.output
    assert "PROVIDER/MODEL[:REASONING]" in chat_help.output
    assert "minimax/MiniMax-M3.1:high" in chat_help.output
    for removed in (
        "--provider", "--model", "--reasoning",
        "--subagent-provider", "--subagent-model",
    ):
        assert removed not in chat_help.output


def test_status_reports_no_spawn_defaults() -> None:
    # Given: settings with the subagent trio only
    settings_store.save(settings_store.Settings(subagent_provider="p", subagent_model="m"))

    # When: status is printed
    result = runner.invoke(root_app, ["status"])

    # Then: the retired spawn lines are gone and the rest still reports
    assert result.exit_code == 0
    assert "Spawn return:" not in result.output
    assert "Alarm session:" not in result.output
    assert "Subagent:" in result.output and "p/m" in result.output


# ── computer show ─────────────────────────────────────────────────────────────


def test_show_defaults_to_the_newest_capture_and_keeps_the_colour_body(
    tmp_path: Path, monkeypatch
) -> None:
    # Given: two captures, the newer one written last
    screens = tmp_path / "screens"
    _write_art(screens, "older.art")
    newer = _write_art(screens, "newer.art")
    older = screens / "older.art"
    older.touch()
    newer_mtime = newer.stat().st_mtime
    os.utime(older, (newer_mtime - 10, newer_mtime - 10))
    monkeypatch.setattr(computer_mod, "COMPUTER_SCREENS_DIR", screens)

    # When: show is called with no path
    result = runner.invoke(computer_mod.app, ["show"])

    # Then: the newest file's header and its truecolour half-blocks are printed
    assert result.exit_code == 0
    assert "grid=2x2" in result.output and "screen=scr-1" in result.output
    assert HALF_BLOCK in result.output
    assert "\x1b[38;2;255;0;0m" in result.output


def test_plain_render_carries_no_colour_escapes(tmp_path: Path) -> None:
    # Given: one saved capture
    path = _write_art(tmp_path / "screens")

    # When: it is rendered as plain
    result = runner.invoke(computer_mod.app, ["show", str(path), "--plain"])

    # Then: the ramp replaces both the glyphs and the colour
    assert result.exit_code == 0
    assert "\x1b" not in result.output
    assert HALF_BLOCK not in result.output
    assert len(_rows(result.output)) == 2


def test_cols_requantises_and_says_so_in_the_header(tmp_path: Path) -> None:
    # Given: one saved 2-column capture
    path = _write_art(tmp_path / "screens")

    # When: it is rendered at 10 columns without --plain
    result = runner.invoke(computer_mod.app, ["show", str(path), "--cols", "10"])

    # Then: every art row is 10 ramp cells wide, and the header says why
    assert result.exit_code == 0
    assert "re-quantised to 10 cols" in result.output
    rows = _rows(result.output)
    assert [len(row) for row in rows] == [10, 10]


def test_head_truncates_after_rendering(tmp_path: Path) -> None:
    # Given: one saved two-row capture
    path = _write_art(tmp_path / "screens")

    # When: only the first row is asked for
    result = runner.invoke(computer_mod.app, ["show", str(path), "--head", "1"])

    # Then: one art row follows the header
    assert result.exit_code == 0
    assert len(_rows(result.output)) == 1


def test_show_of_a_missing_file_exits_one(tmp_path: Path) -> None:
    # When: a path that does not exist is rendered
    result = runner.invoke(computer_mod.app, ["show", str(tmp_path / "nope.art")])

    # Then: a clean exit-1 message, not a traceback
    assert result.exit_code == 1
    assert "no such screen art" in result.output
    assert "Traceback" not in result.output


def test_show_of_an_unparseable_file_exits_one(tmp_path: Path) -> None:
    # Given: a file that is not screen art
    path = tmp_path / "screens" / "junk.art"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("hello\n", encoding="utf-8")

    # When: it is rendered
    result = runner.invoke(computer_mod.app, ["show", str(path)])

    # Then: the parse error is the message
    assert result.exit_code == 1
    assert "not a screen-art file" in result.output


def test_show_without_any_capture_says_so(tmp_path: Path, monkeypatch) -> None:
    # Given: a screenshots directory that was never written to
    monkeypatch.setattr(computer_mod, "COMPUTER_SCREENS_DIR", tmp_path / "empty")

    # When: show is called with no path
    result = runner.invoke(computer_mod.app, ["show"])

    # Then: the empty case is stated as a clean exit-1 error
    assert result.exit_code == 1
    assert "no screen art yet" in result.output
    assert "Traceback" not in result.output
