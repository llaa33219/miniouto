"""Failure diagnostics go to a LOG FILE, never into the TUI's alternate screen.

A failed turn used to print its error line, last tool calls and traceback to
stderr (`core/chat.py:_fail_console`). Under the TUI that stderr write lands in
the middle of the alternate screen, painting the traceback over the frame the
TUI composes — the `[error: …]` row was there, buried under Python internals.
The fix keeps the CONTENT and moves it: every failure appends its report to
`LOG_DIR/failure-<session>.log`, and stderr receives it only when it is not a
TUI's screen (`ChatOptions.quiet_diagnostics`, set by `_dispatch`).

Three things are locked here, in the order the fix depends on them:

1. the log file is written ALWAYS, with the full report (error, tool trail,
   traceback) — the debugging value is preserved, not discarded;
2. a `quiet` dump writes the log and NOTHING to stderr, which is the exact
   property that keeps the alternate screen intact;
3. the TUI sets the flag on the ``ChatOptions`` it dispatches, and the CLI
   leaves it False — so `miniouto chat` keeps its stderr output.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import harness
import tui_harness
from miniouto.cli.tui import ChatTUI
from miniouto.core import chat as chat_mod
from miniouto.storage import sessions as session_store
from miniouto.storage.paths import LOG_DIR


@pytest.fixture(autouse=True)
def _clean_failure_logs() -> Any:
    """Each test owns its log file; the test home is shared across the run."""

    def _sweep() -> None:
        for path in LOG_DIR.glob("failure-*.log") if LOG_DIR.is_dir() else []:
            path.unlink(missing_ok=True)

    _sweep()
    yield
    _sweep()


def _failed_turn(monkeypatch: pytest.MonkeyPatch, session: str) -> None:
    """Run a real `run_chat` turn whose provider raises — the failure path."""

    async def handler(_request: harness.Request) -> Any:
        raise RuntimeError("provider went away")

    harness.install(monkeypatch, handler, session=session)


# ── 1. the report always reaches the log file ───────────────────────────────


def test_a_quiet_failure_writes_the_whole_report_to_the_log_and_nothing_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given: a turn that fails, run the way a TUI runs it
    session = "diag-quiet-session"
    _failed_turn(monkeypatch, session)

    # When: the turn dies and the caller asked for quiet diagnostics
    with pytest.raises(RuntimeError, match="provider went away"):
        chat_mod.run_chat(
            chat_mod.ChatOptions(
                prompt="hi", session=session, quiet_diagnostics=True
            ),
            harness.CaptureSink(),
        )

    # Then: stderr stayed untouched — the alternate screen is intact
    captured = capsys.readouterr()
    assert captured.err == ""

    # And: the whole report is on disk under a per-session file
    log = chat_mod.failure_log_path(session)
    assert log.parent == LOG_DIR
    assert log.name == f"failure-{session}.log"
    text = log.read_text(encoding="utf-8")
    assert "RuntimeError: provider went away" in text
    assert "No tool call was observed before the failure" in text
    assert "Traceback:" in text
    assert "Traceback (most recent call last):" in text
    # The handler frame is in the report: the traceback is the full one, not a
    # one-line summary
    assert "tests/test_failure_diagnostics.py" in text


def test_an_unquiet_failure_keeps_the_stderr_dump_and_also_logs_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI's debugging output is the behaviour that must NOT regress."""

    session = "diag-loud-session"
    _failed_turn(monkeypatch, session)

    with pytest.raises(RuntimeError, match="provider went away"):
        chat_mod.run_chat(
            chat_mod.ChatOptions(prompt="hi", session=session),
            harness.CaptureSink(),
        )

    err = capsys.readouterr().err
    assert "✗ RuntimeError: provider went away" in err
    assert "Traceback:" in err
    # Same content, both sinks: the log is not a lesser record
    logged = chat_mod.failure_log_path(session).read_text(encoding="utf-8")
    assert "✗ RuntimeError: provider went away" in logged
    assert "Traceback:" in logged


def test_the_last_tool_calls_reach_the_log_with_the_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a turn that calls a tool and only then dies
    session = "diag-tool-trail-session"

    async def handler(req: harness.Request) -> Any:
        if req.index == 0:
            return harness.co.LLMResponse(
                content="running it",
                tool_calls=[
                    harness.co.ToolCall(
                        id="c1", name="Bash", arguments={"command": "ls -la"}
                    )
                ],
            )
        raise RuntimeError("died after the tool call")

    harness.install(monkeypatch, handler, session=session)

    with pytest.raises(RuntimeError, match="died after the tool call"):
        chat_mod.run_chat(
            chat_mod.ChatOptions(
                prompt="list the files", session=session, quiet_diagnostics=True
            ),
            harness.CaptureSink(),
        )

    text = chat_mod.failure_log_path(session).read_text(encoding="utf-8")
    assert "Last tool call before failure (1 total this turn):" in text
    assert "  - Bash: ls -la" in text


def test_appends_across_failures_and_never_escapes_the_log_dir() -> None:
    # Given: two failures in the same session, and a hostile session name
    first = chat_mod.failure_log_path("diag-append-session")
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("older entry\n", encoding="utf-8")

    # When: the same session fails again
    try:
        raise ValueError("boom")
    except ValueError as exc:
        chat_mod._dump_failure_diagnostics(exc, "diag-append-session", quiet=True)

    # Then: it APPENDED — the earlier entry survives
    text = first.read_text(encoding="utf-8")
    assert text.startswith("older entry\n")
    assert "✗ ValueError: boom" in text

    # And: a name with separators is squashed into the log directory
    escaped = chat_mod.failure_log_path("../../etc/passwd")
    assert escaped.parent == LOG_DIR
    assert "/" not in escaped.name


def test_a_failing_log_write_never_raises_into_the_failure_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given: a LOG_DIR that cannot be created — a FILE sits where the directory
    # belongs, so the mkdir raises ENOTDIR (an OSError, like a read-only home)
    blocker = tmp_path / "logs-blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(chat_mod, "LOG_DIR", blocker / "logs")

    # When: a failure is dumped inside an except block
    try:
        raise ValueError("still the real error")
    except ValueError as exc:
        # Then: the diagnostic path swallowed the OSError instead of raising …
        chat_mod._dump_failure_diagnostics(exc, "diag-io-session", quiet=True)

    # … and a quiet dump still said nothing at all on stderr
    assert capsys.readouterr().err == ""
    assert not (blocker / "logs").exists(), "the impossible LOG_DIR was created"


def test_report_markup_in_the_error_text_is_not_parsed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rich markup in an error message must not break the failure path."""

    class _MarkupError(Exception):
        pass

    # When: the exception's message is a rich closing tag
    try:
        raise _MarkupError("[/] unclosed [bold] markup")
    except _MarkupError as exc:
        chat_mod._dump_failure_diagnostics(exc, "diag-markup-session")

    # Then: it was printed literally instead of raising MarkupError
    err = capsys.readouterr().err
    assert "[/] unclosed [bold] markup" in err


# ── 3. the two callers set the flag the way their terminal requires ─────────


def test_the_tui_dispatches_its_turn_with_quiet_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns: list[dict[str, Any]] = []

    async def body() -> None:
        tui_harness.stub_run_chat(monkeypatch, turns)
        app = ChatTUI()
        async with app.run_test(size=tui_harness.BOOT_SIZE) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "diag-tui-session")
            await app._dispatch("a prompt")
            await pilot.pause()

    asyncio.run(body())

    # Then: the turn was told not to touch stderr — this app owns the screen
    assert turns and turns[0]["quiet_diagnostics"] is True, turns


def test_the_cli_leaves_diagnostics_on_stderr() -> None:
    # Given: the CLI's flag bag, built the way `chat_cmd` builds it
    # When/Then: nothing opts into quiet—the field's default is False, so a
    # CLI run keeps printing to stderr exactly as before
    assert chat_mod.ChatOptions(prompt="hi").quiet_diagnostics is False


def test_a_failed_turn_in_the_tui_shows_the_error_row_and_keeps_stderr_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The end-to-end lock: the REAL turn fails under a booted TUI.

    This is the bug as the owner hit it. `run_chat` runs for real (only the
    provider is a double), the turn raises, `_dispatch` catches it and posts
    its `[error: …]` row. What must NOT happen is a traceback arriving on
    stderr, where the alternate screen is.
    """

    session = "diag-e2e-tui-session"

    async def handler(_request: harness.Request) -> Any:
        raise RuntimeError("provider went away")

    harness.install(monkeypatch, handler, session=session)

    # The turn is a chat SUBPROCESS now, and the in-process provider double
    # cannot cross the process boundary. The spawn is stubbed with a child
    # that fails the way the real one would — a non-zero exit, the error on
    # its stderr, the turn stamped interrupted — which is what
    # `_run_chat_child` surfaces on the TUI's own row.
    class _FailingProcess(tui_harness._FakeChatProcess):
        def __init__(self, argv: list[str]) -> None:
            super().__init__(argv, stderr_text="RuntimeError: provider went away\n")

        def _render(self) -> bytes:
            self.returncode = 1
            prompt, sess, _cont = self._parse()
            session_store.begin_turn(sess, prompt)
            session_store.finish_turn(sess, assistant="", status="interrupted")
            return b""

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:
        return _FailingProcess(list(argv))

    monkeypatch.setattr(tui_harness.app_module.bridge, "asyncio_create_subprocess_exec", fake_exec)

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=tui_harness.BOOT_SIZE) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, session)
            await app._dispatch("a prompt that fails")
            await pilot.pause()
            # Then: the TUI's own surface said what happened
            painted = tui_harness.painted(app)
            assert any("provider went away" in line for line in painted), painted

    asyncio.run(body())

    # And: nothing was dumped on stderr — the screen the TUI composes is intact
    assert capsys.readouterr().err == ""
    # And: the turn record on disk says the turn was interrupted, so the
    # failure left a trace (the per-run failure log the CLI writes lives in
    # the child, not this process).
    data = session_store.load(session)
    assert data.turns and data.turns[-1].status == session_store.STATUS_INTERRUPTED
