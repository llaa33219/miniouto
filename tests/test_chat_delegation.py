"""Delegation is `miniouto chat` with four extra flags — there is no second command.

The contract under test is the lifecycle: a delegated run is an ordinary chat
run, and on completion it owes (a) a markdown file containing ONLY the final
answer, (b) exactly one alarm whose first line is `inbox.ALARM_HEADER` and whose
meta names the job, and (c) a `core/spawn_state` marker that always reaches a
terminal status — including when the turn raises. `--bg` detaches the whole
invocation into a NEW process with every optional flag value already expanded
and file stdio only, because `tools/bash.py` kills process groups and pipe
holders.

`run_chat` is stubbed everywhere: these tests are about the CLI's wiring, never
about a model call.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from miniouto.cli import app
from miniouto.cli import chat as chat_mod
from miniouto.core import inbox, spawn_state
from miniouto.core.chat import ChatOptions
from miniouto.core.inbox import ALARM_HEADER
from miniouto.storage import providers as provider_store
from miniouto.storage import session_config, toml_io
from miniouto.storage import sessions as session_store
from miniouto.storage import styles as style_store
from miniouto.storage.paths import (
    INBOX_DIR,
    SETTINGS_FILE,
    SPAWN_LOG_DIR,
    SUBAGENT_RESULTS_DIR,
)

runner = CliRunner()


@dataclass
class Result:
    exit_code: int
    output: str


def _chat(*args: str) -> Any:
    """Invoke the REAL entrypoint, `app(argv)` — argv normalisation included.

    `typer.testing.CliRunner.invoke` rebuilds the click command from the Typer
    object and would skip that normalisation, so the bare `--return` /
    `--alarm-session` forms would never be exercised.
    """

    with runner.isolation() as streams:
        code = 0
        try:
            app(["chat", *args])
        except SystemExit as exit_signal:
            code = exit_signal.code if isinstance(exit_signal.code, int) else 1
        output = streams[2].getvalue().decode("utf-8", "replace")
    return Result(code, output)

SESSION = "delegation-test-session"


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"




def _messages(session: str) -> list[dict]:
    directory = inbox.inbox_dir(session)
    if not directory.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]


def _result_files() -> list[Path]:
    return sorted(SUBAGENT_RESULTS_DIR.glob("*.md"))


def _marker(job_id: str) -> spawn_state.SpawnMarker | None:
    return spawn_state.read_marker(spawn_state.marker_path(job_id))


def _write_settings(**values: str) -> None:
    """Write settings verbatim.

    `settings_store.save()` drops empty values, so the "no session at all"
    case has to be expressed on disk directly.
    """

    toml_io.save(SETTINGS_FILE, {"style": "default", **values})


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """The test home is shared; give each test its own settings, inbox, results, markers."""

    _write_settings(session=SESSION)
    for path in SUBAGENT_RESULTS_DIR.glob("*.md"):
        path.unlink()
    for path in SPAWN_LOG_DIR.glob("*") if SPAWN_LOG_DIR.is_dir() else []:
        path.unlink()
    for directory in INBOX_DIR.iterdir() if INBOX_DIR.is_dir() else []:
        if directory.is_dir():
            for message in directory.iterdir():
                message.unlink()


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[ChatOptions]:
    """Stub the turn runner and record the options each run received."""

    recorded: list[ChatOptions] = []

    def fake_run_chat(opts: ChatOptions, sink: object = None) -> str:
        recorded.append(opts)
        return "ANSWER"

    monkeypatch.setattr(chat_mod, "run_chat", fake_run_chat)
    return recorded


# ── the flag-absent baseline ──────────────────────────────────────────────────


def test_a_plain_chat_owes_nothing(calls: list[ChatOptions]) -> None:
    # Given/When: an ordinary `chat` with none of the delegation flags
    result = _chat("hello")

    # Then: it ran outo and produced no file, no alarm, no marker
    assert result.exit_code == 0
    assert calls[0].role is None
    assert _result_files() == []
    assert _messages(SESSION) == []
    assert list(SPAWN_LOG_DIR.glob("*.json")) == []


def test_no_role_runs_the_outo_prompt(calls: list[ChatOptions]) -> None:
    # Given/When: a delegated run that names no role
    result = _chat("brief", "--alarm-session", SESSION)

    # Then: the turn ran as outo, and the delegation channels still fired
    assert result.exit_code == 0
    assert calls[0].role is None
    assert len(_messages(SESSION)) == 1


# ── --role ────────────────────────────────────────────────────────────────────


def test_role_is_plumbed_into_chat_options(calls: list[ChatOptions]) -> None:
    # Given: an installed style declaring a role
    style_store.write("delegation-fixture", "<outo>OUTO</outo>\n<editor>EDIT</editor>", overwrite=True)

    # When: a run names both the style and its role
    result = _chat("brief", "--style", "delegation-fixture", "--role", "editor", "--alarm-session", SESSION)

    # Then: the turn ran AS that role of that style, and answered normally
    assert result.exit_code == 0
    assert calls[0].style == "delegation-fixture"
    assert calls[0].role == "editor"


def test_an_unknown_role_fails_before_the_turn_runs(calls: list[ChatOptions]) -> None:
    # Given: a style with a known roster
    style_store.write("delegation-fixture", "<outo>OUTO</outo>\n<editor>EDIT</editor>", overwrite=True)

    # When: a typo'd role is requested
    result = _chat("brief", "--style", "delegation-fixture", "--role", "edtior")

    # Then: it is a clean exit-1 naming the available roles, with no turn
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "edtior" in result.output and "editor" in result.output
    assert calls == []


def test_role_without_style_fails_before_the_turn(calls: list[ChatOptions]) -> None:
    # Given: a style with a known roster, active through settings
    style_store.write("delegation-fixture", "<outo>OUTO</outo>\n<editor>EDIT</editor>", overwrite=True)
    _write_settings(session=SESSION, style="delegation-fixture")

    # When: a role is named without the style it belongs to
    result = _chat("brief", "--role", "editor")

    # Then: a clean exit-1 naming both flags, with no turn, no file, no marker
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "--role requires --style" in result.output
    assert calls == []
    assert _result_files() == []
    assert list(SPAWN_LOG_DIR.glob("*.json")) == []


def test_role_and_style_together_still_run(calls: list[ChatOptions]) -> None:
    # Given: the same style, named explicitly
    style_store.write("delegation-fixture", "<outo>OUTO</outo>\n<editor>EDIT</editor>", overwrite=True)

    # When: `--style` alone is given, with no role
    plain = _chat("brief", "--style", "delegation-fixture")

    # Then: an ordinary outo turn of that style ran, unchanged
    assert plain.exit_code == 0
    assert calls[0].style == "delegation-fixture"
    assert calls[0].role is None


# ── --use-subagent-provider ───────────────────────────────────────────────────


def _install_providers() -> None:
    provider_store.upsert(provider_store.Provider(name="outo-prov", default_model="outo-model"))
    provider_store.upsert(
        provider_store.Provider(name="sub-prov", default_model="sub-model", reasoning_effort="high")
    )


def test_use_subagent_provider_resolves_the_settings_trio(
    calls: list[ChatOptions], tmp_path: Path
) -> None:
    # Given: a subagent provider/model/reasoning stored in settings
    _install_providers()
    _write_settings(
        session=SESSION,
        provider="outo-prov",
        subagent_provider="sub-prov",
        subagent_model="settings-model",
        subagent_reasoning="low",
    )

    # When: the run asks to use it
    result = _chat("brief", "--use-subagent-provider")

    # Then: the turn ran on the subagent provider, model and reasoning
    assert result.exit_code == 0
    assert calls[0].provider == "sub-prov"
    assert calls[0].model == "settings-model"
    assert calls[0].reasoning == "low"


def test_explicit_pm_beats_the_subagent_settings(calls: list[ChatOptions]) -> None:
    # Given: a subagent trio in settings
    _install_providers()
    _write_settings(
        session=SESSION,
        provider="outo-prov",
        subagent_provider="sub-prov",
        subagent_model="settings-model",
        subagent_reasoning="low",
    )

    # When: provider, model and reasoning are all named on the command line
    result = _chat(
        "brief", "--use-subagent-provider", "--pm", "outo-prov/flag-model:medium",
    )

    # Then: every segment of the one flag won over the settings it would
    # otherwise come from
    assert result.exit_code == 0
    assert calls[0].provider == "outo-prov"
    assert calls[0].model == "flag-model"
    assert calls[0].reasoning == "medium"


def test_without_the_flag_outo_defaults_are_used(calls: list[ChatOptions]) -> None:
    # Given: a subagent trio in settings that was NOT asked for
    _install_providers()
    _write_settings(session=SESSION, provider="outo-prov", subagent_provider="sub-prov")

    # When: an ordinary chat runs
    result = _chat("hello")

    # Then: outo's provider is what the turn gets — the trio is inert
    assert result.exit_code == 0
    assert calls[0].provider is None


def test_an_unconfigured_subagent_provider_exits_one(calls: list[ChatOptions]) -> None:
    # Given: settings naming a provider nobody configured. The shared session
    # carries no pin of its own — an earlier test in this file froze one for
    # the very same name, and a frozen pin would outrank these settings.
    _write_settings(session=SESSION, provider="outo-prov", subagent_provider="ghost")
    session_config.clear(SESSION)

    # When: the run asks to use it
    result = _chat("brief", "--use-subagent-provider")

    # Then: a clean exit-1 explaining it, with no turn
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "ghost" in result.output
    assert calls == []


# ── --pm ──────────────────────────────────────────────────────────────────────


def test_pm_fills_the_provider_trio(calls: list[ChatOptions]) -> None:
    # Given/When: one compact provider/model/reasoning value
    result = _chat("brief", "--pm", "minimax/MiniMax-M3.1:high")

    # Then: all three override fields came from it
    assert result.exit_code == 0
    assert calls[0].provider == "minimax"
    assert calls[0].model == "MiniMax-M3.1"
    assert calls[0].reasoning == "high"


def test_pm_without_reasoning_leaves_it_unset(calls: list[ChatOptions]) -> None:
    # Given/When: the reasoning suffix is absent
    plain = _chat("brief", "--pm", "openai/gpt-5.5")

    # Then: provider and model were split and reasoning was not invented
    assert plain.exit_code == 0
    assert calls[0].provider == "openai"
    assert calls[0].model == "gpt-5.5"
    assert calls[0].reasoning is None


def test_pm_model_may_contain_a_slash(calls: list[ChatOptions]) -> None:
    # Given/When: a model name with `/` in it, plus a reasoning suffix
    result = _chat("brief", "--pm", "openrouter/meta/llama-3:low")

    # Then: only the FIRST `/` split the provider off; the rest is the model
    assert result.exit_code == 0
    assert calls[0].provider == "openrouter"
    assert calls[0].model == "meta/llama-3"
    assert calls[0].reasoning == "low"


def test_pm_with_an_empty_reasoning_suffix_is_no_reasoning(calls: list[ChatOptions]) -> None:
    # Given/When: the value ends in the separator with nothing after it
    result = _chat("brief", "--pm", "openai/gpt-5.5:")

    # Then: the trailing `:` is a separator with an empty tail, not a model
    assert result.exit_code == 0
    assert calls[0].model == "gpt-5.5"
    assert calls[0].reasoning is None


def test_pm_beats_the_session_pin(calls: list[ChatOptions]) -> None:
    # Given: a session pinned to a different provider and model
    _install_providers()
    _write_settings(session=SESSION, provider="outo-prov")
    session_config.set_fields(SESSION, provider="outo-prov", model="pinned-model")

    # When: this call names its own trio
    result = _chat("brief", "--pm", "sub-prov/flag-model:high")

    # Then: the pin did not answer — the per-call override did
    assert result.exit_code == 0
    assert calls[0].provider == "sub-prov"
    assert calls[0].model == "flag-model"
    assert calls[0].reasoning == "high"


@pytest.mark.parametrize("value", ["minimax", "/model", "provider/", "   /   "])
def test_a_malformed_pm_is_a_usage_error(calls: list[ChatOptions], value: str) -> None:
    # Given/When: a value with no `/`, or with an empty provider or model
    result = _chat("brief", "--pm", value)

    # Then: a clean exit-1 naming the format, with no turn run
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "PROVIDER/MODEL[:REASONING]" in result.output
    assert calls == []


def test_pm_overrides_the_global_default_without_touching_it(calls: list[ChatOptions]) -> None:
    # Given: a session with NO pin at all, and a global default provider
    _install_providers()
    _write_settings(session=SESSION, provider="outo-prov")
    session_config.clear(SESSION)

    # When: the call pins itself to another provider
    result = _chat("brief", "--name", "pm-override", "--pm", "sub-prov/flag-model")

    # Then: the turn ran on the override...
    assert result.exit_code == 0
    assert calls[0].provider == "sub-prov"
    assert calls[0].model == "flag-model"

    # And the override left no pin behind: it was a per-call value, so the
    # session's next run still follows whatever it was configured with
    assert session_config.read("pm-override") is None


@pytest.mark.parametrize(
    "flag", ["--provider", "--model", "--reasoning", "--subagent-provider", "--subagent-model"]
)
def test_the_replaced_flags_are_a_usage_error(calls: list[ChatOptions], flag: str) -> None:
    # Given/When: one of the flags `--pm` replaced is passed
    result = _chat("brief", flag, "some-value")

    # Then: click rejects it outright — the trio has exactly one spelling
    assert result.exit_code == 2
    assert "No such option" in result.output
    assert calls == []


# ── --alarm-session ───────────────────────────────────────────────────────────


def test_alarm_session_with_a_value_delivers_exactly_one_alarm(calls: list[ChatOptions]) -> None:
    # Given/When: a run naming the session that receives its completion
    result = _chat("brief", "--return", "--alarm-session", "boss-session")

    # Then: one alarm, stamped with the shared header and this run's meta
    assert result.exit_code == 0
    delivered = _messages("boss-session")
    assert len(delivered) == 1
    assert (delivered[0]["kind"], delivered[0]["source"]) == ("alarm", "chat")
    assert delivered[0]["text"].splitlines()[0] == ALARM_HEADER
    path = _result_files()[0]
    assert str(path) in delivered[0]["text"]
    assert delivered[0]["meta"] == {
        "id": path.name[:6],
        "role": None,
        "style": "default",
        "return_path": str(path),
    }


def test_the_alarm_names_the_session_the_run_wrote_to(calls: list[ChatOptions]) -> None:
    # Given: a delegated run that owns a named session
    _chat("brief", "--name", "worker-session", "--return", "--alarm-session", SESSION)

    # When: its completion arrives
    lines = _messages(SESSION)[0]["text"].splitlines()

    # Then: the receiver can tell which session was used and where it lives —
    # the header, the status, and that line, in that order
    assert lines[0] == ALARM_HEADER
    assert "finished (status: done)" in lines[1]
    assert lines[2] == (
        f"Session: worker-session — {session_store.path_for('worker-session')}"
    )


def test_a_bare_alarm_session_is_a_usage_error(
    calls: list[ChatOptions],
) -> None:
    # Given: a stored session the bare form used to fall back on
    _write_settings(session="stored-session")

    # When: the flag is given without a value
    result = _chat("brief", "--alarm-session")

    # Then: click rejects it outright — the run must never consult ambient
    # state to decide where its alarm goes
    assert result.exit_code == 2
    assert "requires an argument" in result.output
    assert calls == []
    assert _messages("stored-session") == []


def test_no_alarm_session_flag_delivers_no_alarm(calls: list[ChatOptions]) -> None:
    # Given/When: a run with `--return` but no alarm flag at all
    result = _chat("brief", "--return")

    # Then: the file was written and no message was armed anywhere
    assert result.exit_code == 0
    assert len(_result_files()) == 1
    assert _messages(SESSION) == []


# ── --return ──────────────────────────────────────────────────────────────────


def test_the_answer_file_contains_only_the_answer(calls: list[ChatOptions]) -> None:
    # Given/When: a run whose answer is written to an explicit file
    result = _chat("rewrite the parser", "--return", "--alarm-session", SESSION)

    # Then: the file is the answer alone — no meta header, no brief section
    assert result.exit_code == 0
    (path,) = _result_files()
    body = path.read_text(encoding="utf-8")
    assert body == "ANSWER"
    assert "## Brief" not in body and "## Result" not in body and not body.startswith("#")


def test_a_bare_return_lands_in_the_results_dir(calls: list[ChatOptions]) -> None:
    # Given/When: `--return` with no value at all
    result = _chat("brief", "--return")

    # Then: the generated filename landed under the default results directory
    assert result.exit_code == 0
    (path,) = _result_files()
    assert path.parent == SUBAGENT_RESULTS_DIR
    assert path.name.startswith(path.name[:6])
    assert path.name.endswith(".md")


def test_a_directory_return_generates_a_file_inside_it(
    calls: list[ChatOptions], tmp_path: Path
) -> None:
    # Given: an existing directory and a trailing-slash path
    existing = tmp_path / "existing"
    existing.mkdir()
    trailing = tmp_path / "trailing"

    # When: either form is named
    assert _chat("brief", "--return", str(existing)).exit_code == 0
    assert _chat("brief", "--return", f"{trailing}/").exit_code == 0

    # Then: both received one auto-named markdown file, verbatim inside
    assert len(list(existing.glob("*.md"))) == 1
    assert len(list(trailing.glob("*.md"))) == 1
    assert _result_files() == []


def test_no_return_flag_writes_no_file(calls: list[ChatOptions]) -> None:
    # Given/When: a run with no `--return` at all
    result = _chat("brief", "--alarm-session", SESSION)

    # Then: nothing was written and the completion line says so
    assert result.exit_code == 0
    assert _result_files() == []
    assert "result: (not saved)" in result.output

    # And: the alarm carries the answer instead of a path
    text = _messages(SESSION)[0]["text"]
    assert text.startswith(ALARM_HEADER)
    assert "Result file:" not in text
    assert text.endswith("\n\nANSWER")


def test_an_answer_too_long_for_the_alarm_is_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a run producing more text than the alarm carries
    monkeypatch.setattr(chat_mod, "run_chat", lambda opts, sink=None: "x" * 5000)

    # When: it finishes with no result file
    result = _chat("brief", "--alarm-session", SESSION)

    # Then: the alarm keeps the cap and says it was truncated
    assert result.exit_code == 0
    text = _messages(SESSION)[0]["text"]
    assert "x" * chat_mod.MAX_ALARM_ANSWER_CHARS in text
    assert text.endswith("\n…(truncated)")


# ── failure paths ─────────────────────────────────────────────────────────────


def test_a_raising_turn_reports_the_error_through_every_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a delegated run whose turn blows up
    def boom(opts: ChatOptions, sink: object = None) -> str:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(chat_mod, "run_chat", boom)

    # When: it runs
    result = _chat("brief", "--return", "--alarm-session", SESSION, "--bg-child", "--job-id", "abc123")

    # Then: it is a clean exit-1, not a traceback
    assert result.exit_code == 1
    assert "Traceback" not in result.output

    # And: the answer file and the alarm both carry the error
    (path,) = _result_files()
    assert path.read_text(encoding="utf-8") == "error: RuntimeError: kaboom"
    assert "status: error" in _messages(SESSION)[0]["text"]

    # And: the marker reached the terminal error status
    marker = _marker("abc123")
    assert marker is not None and marker.status == "error"


def test_a_cancelled_run_is_reported_as_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a delegated run the user interrupts
    def cancel(opts: ChatOptions, sink: object = None) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(chat_mod, "run_chat", cancel)

    # When: it runs
    result = _chat("brief", "--alarm-session", SESSION, "--bg-child", "--job-id", "stop001")

    # Then: a clean exit-1 whose alarm and marker both say stopped
    assert result.exit_code == 1
    assert "status: stopped" in _messages(SESSION)[0]["text"]
    assert _marker("stop001").status == "stopped"  # type: ignore[union-attr]


# ── --bg detachment ───────────────────────────────────────────────────────────


@pytest.fixture
def captured_popen(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> object:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return seen


def test_bg_detaches_with_file_stdio_and_a_running_marker(
    captured_popen: dict[str, Any], calls: list[ChatOptions]
) -> None:
    # Given: `--bg`, which never runs a turn in this process
    style_store.write("delegation-fixture", "<outo>OUTO</outo>\n<editor>EDIT</editor>", overwrite=True)

    # When: the run is detached
    result = _chat(
        "the brief", "--style", "delegation-fixture", "--role", "editor",
        "--bg", "--alarm-session", "bg-session",
    )

    # Then: the ack names the job, its session, its log and its status file
    assert result.exit_code == 0
    job_id = result.output.split("'")[1]
    session = captured_popen["argv"][captured_popen["argv"].index("--name") + 1]
    assert result.output == (
        f"Started run '{job_id}' (bg).\n"
        f"Session: {session} — {session_store.path_for(session)}\n"
        f"Log: {SPAWN_LOG_DIR / (job_id + '.log')}\n"
        f"Status: {SPAWN_LOG_DIR / (job_id + '.json')} (running → done|error|stopped)\n"
        f"Alarm → session 'bg-session' when finished.\n"
    )
    assert calls == []

    # And: the child is a `-m miniouto chat` invocation carrying the resolved flags
    argv = captured_popen["argv"]
    assert argv[1:5] == ["-m", "miniouto", "chat", "the brief"]
    assert "--bg-child" in argv and "--bg" not in argv
    assert argv[argv.index("--job-id") + 1] == job_id
    assert argv[argv.index("--role") + 1] == "editor"
    assert argv[argv.index("--style") + 1] == "delegation-fixture"
    assert argv[argv.index("--alarm-session") + 1] == "bg-session"

    # And: it is a detached session whose stdio is FILES, never pipes or inherited fds
    kwargs = captured_popen["kwargs"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.STDOUT
    assert Path(kwargs["stdout"].name) == SPAWN_LOG_DIR / f"{job_id}.log"

    # And: the parent already promised the alarm, with the log path recorded
    marker = _marker(job_id)
    assert marker is not None
    assert marker.status == "running"
    assert marker.alarm_session == "bg-session"
    assert marker.session == session
    assert marker.log == str(SPAWN_LOG_DIR / f"{job_id}.log")
    assert marker.started
    # And it carries the detached child's pid, so a waiter can tell this run
    # from one that crashed without ever writing a finished status
    assert marker.pid == 4242


def test_a_bg_ack_without_an_alarm_says_so(captured_popen: dict[str, Any]) -> None:
    # Given/When: a detached run that armed no alarm
    result = _chat("brief", "--bg")

    # Then: the last line points at the status file instead of a session
    assert result.exit_code == 0
    assert result.output.splitlines()[-1] == "No alarm configured — check the status file."


def test_bg_forwards_the_expanded_pm(captured_popen: dict[str, Any]) -> None:
    # Given: a detached run that named its trio compactly
    result = _chat("brief", "--bg", "--pm", "minimax/MiniMax-M3.1:high")

    # Then: the child carries the same three values as ONE concrete --pm,
    # never a bare or partial form it could resolve differently
    assert result.exit_code == 0
    argv = captured_popen["argv"]
    assert argv.count("--pm") == 1
    assert argv[argv.index("--pm") + 1] == "minimax/MiniMax-M3.1:high"


def test_a_bg_child_re_entry_carries_the_pm(calls: list[ChatOptions]) -> None:
    # Given: a child process re-entering with the line its parent built
    child = _chat(
        "brief", "--bg-child", "--job-id", "pmchild", "--name", "spawn-pm",
        "--pm", "minimax/MiniMax-M3.1:high", "--alarm-session", SESSION,
    )

    # Then: it ran with exactly those values
    assert child.exit_code == 0
    assert calls[0].provider == "minimax"
    assert calls[0].model == "MiniMax-M3.1"
    assert calls[0].reasoning == "high"


def test_bg_expands_the_bare_return_into_a_concrete_path(
    captured_popen: dict[str, Any], tmp_path: Path
) -> None:
    # Given: a bare `--return` and a named alarm target
    _write_settings(session="stored-session")

    # When: the run is detached
    result = _chat("brief", "--bg", "--alarm-session", "boss-session", "--return")

    # Then: the bare flag did not survive the hand-off — it carries its path
    argv = captured_popen["argv"]
    assert argv[argv.index("--alarm-session") + 1] == "boss-session"
    assert argv[argv.index("--return") + 1].startswith(str(SUBAGENT_RESULTS_DIR))
    assert argv.count("--alarm-session") == 1 and argv.count("--return") == 1

    # And: the marker already knows both destinations
    marker = _marker(result.output.split("'")[1])
    assert marker is not None and marker.return_path is not None


def test_bg_without_a_session_generates_its_own(
    captured_popen: dict[str, Any], calls: list[ChatOptions]
) -> None:
    # Given: a detached run with no `--name`
    _write_settings(session="boss-session")

    # When: it is detached
    result = _chat("brief", "--bg", "--alarm-session", "boss-session")

    # Then: its own session is generated, never the shared one — two writers on
    # one session file lose turns
    assert result.exit_code == 0
    argv = captured_popen["argv"]
    session = argv[argv.index("--name") + 1]
    assert session.startswith("spawn-") and session != "boss-session"

    # And: detaching never overwrote the user's stored session
    assert toml_io.load(SETTINGS_FILE)["session"] == "boss-session"


def test_bg_child_adopts_the_marker_its_parent_wrote(
    captured_popen: dict[str, Any], calls: list[ChatOptions]
) -> None:
    # Given: a parent that detached a run and promised an alarm
    parent = _chat("brief", "--bg", "--alarm-session", "bg-session")
    parent_marker = _marker(parent.output.split("'")[1])
    assert parent_marker is not None

    # When: the child re-enters under the same job id
    child = _chat(
        "brief", "--bg-child", "--job-id", parent_marker.id, "--alarm-session", "bg-session"
    )

    # Then: it adopted the parent's bookkeeping instead of restarting it
    assert child.exit_code == 0
    adopted = _marker(parent_marker.id)
    assert adopted is not None
    assert adopted.started == parent_marker.started
    assert adopted.log == parent_marker.log
    assert adopted.status == "done"


def test_bg_and_bg_child_together_are_rejected(captured_popen: dict[str, Any]) -> None:
    # Given: a child invocation that also asks to detach again
    # When: it runs
    result = _chat("brief", "--bg", "--bg-child")

    # Then: a clean exit-1, and nothing was detached
    assert result.exit_code == 1
    assert "--bg cannot be combined" in result.output
    assert captured_popen == {}


# ── settings.session ownership ────────────────────────────────────────────────


def _stored_session() -> str:
    return toml_io.load(SETTINGS_FILE).get("session", "")


def test_a_foreground_delegated_run_leaves_the_stored_session_alone(
    calls: list[ChatOptions],
) -> None:
    # Given: an installed style declaring a role, and a session the user owns
    style_store.write(
        "delegation-fixture", "<outo>OUTO</outo>\n<editor>EDIT</editor>", overwrite=True
    )
    _write_settings(session="boss-session")

    # When: a delegated run mints its own `chat-…` session in the foreground
    result = _chat(
        "brief", "--style", "delegation-fixture", "--role", "editor", "--return"
    )

    # Then: it ran, wrote its result file, and still owns no say over the
    # user's active session
    assert result.exit_code == 0
    assert calls[0].session.startswith("chat-") and calls[0].session != "boss-session"
    assert _result_files()
    assert _stored_session() == "boss-session"


def test_a_bg_child_re_entry_leaves_the_stored_session_alone(
    captured_popen: dict[str, Any], calls: list[ChatOptions]
) -> None:
    # Given: a detached run the user launched — the parent already left
    # `settings.session` alone, and re-entering with `bg=False` used to undo that
    _write_settings(session="boss-session")
    assert _chat("brief", "--bg", "--alarm-session", "boss-session").exit_code == 0

    # When: the child re-enters under its job id with its generated name
    child_session = "spawn-20260101-000000-abcdef"
    result = _chat(
        "brief", "--bg-child", "--job-id", "job002",
        "--name", child_session, "--alarm-session", "boss-session",
    )

    # Then: the child's `spawn-…` name never became the user's active session
    assert result.exit_code == 0
    assert calls[0].session == child_session
    assert _stored_session() == "boss-session"


def test_a_role_only_run_leaves_the_stored_session_alone(
    calls: list[ChatOptions],
) -> None:
    # Given: a `--role` run with no result file and no alarm — delegation by
    # role alone, which `--return`-shaped predicates alone would miss
    style_store.write(
        "delegation-fixture", "<outo>OUTO</outo>\n<editor>EDIT</editor>", overwrite=True
    )
    _write_settings(session="boss-session")

    # When: it runs
    result = _chat("brief", "--style", "delegation-fixture", "--role", "editor")

    # Then: no channel fired, and the user's session is untouched
    assert result.exit_code == 0
    assert _result_files() == []
    assert _stored_session() == "boss-session"


def test_a_delegated_run_resolves_its_own_session_during_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a delegated run whose caller owns the stored session
    _write_settings(session="boss-session")
    resolved: list[str] = []

    def record_session(opts: ChatOptions, sink: object = None) -> str:
        resolved.append(toml_io.load(SETTINGS_FILE).get("session", ""))
        return "ANSWER"

    monkeypatch.setattr(chat_mod, "run_chat", record_session)

    # When: it runs
    result = _chat("brief", "--name", "guest-session", "--return", "--alarm-session", "boss-session")

    # Then: the turn resolved the GUEST name — the inbox hook drains that
    # session, and on the caller's name it would steal the caller's messages —
    # and the caller's name is what survives on disk
    assert result.exit_code == 0
    assert resolved == ["guest-session"]
    assert _stored_session() == "boss-session"


# ── mint seeding ──────────────────────────────────────────────────────────────


def test_a_minted_session_inherits_the_callers_pins(calls: list[ChatOptions]) -> None:
    # Given: the caller's session carries a provider pin
    _write_settings(session="boss-session")
    session_config.set_fields(
        "boss-session", provider="sub-prov", model="boss-model", reasoning="high"
    )

    # When: a delegated run mints its own `chat-…` session
    result = _chat("brief", "--style", "delegation-fixture", "--role", "editor", "--return")

    # Then: the fresh session starts from the caller's pins, so the child
    # cannot land on a different provider than the caller resolved
    assert result.exit_code == 0
    minted = calls[0].session
    assert minted.startswith("chat-")
    seeded = session_config.read(minted)
    assert seeded is not None
    assert (seeded.provider, seeded.model, seeded.reasoning) == (
        "sub-prov", "boss-model", "high",
    )


def test_a_minted_bg_session_inherits_the_callers_pins(
    captured_popen: dict[str, Any], calls: list[ChatOptions]
) -> None:
    # Given: a caller session pinned to one provider
    _write_settings(session="boss-session")
    session_config.set_fields("boss-session", provider="sub-prov", model="boss-model")

    # When: a detached run mints its `spawn-…` session
    result = _chat("brief", "--bg", "--alarm-session", "boss-session")

    # Then: the detached session carries the pin across the process boundary
    assert result.exit_code == 0
    argv = captured_popen["argv"]
    spawned = argv[argv.index("--name") + 1]
    assert spawned.startswith("spawn-")
    assert session_config.read(spawned) == session_config.read("boss-session")


def test_seeding_never_overwrites_a_pin_the_new_session_already_has(
    calls: list[ChatOptions],
) -> None:
    # Given: a named session that is already pinned, and a caller with
    # a different pin
    _write_settings(session="boss-session")
    session_config.set_fields("boss-session", provider="outo-prov", model="boss-model")
    session_config.set_fields("mine", provider="sub-prov", model="mine-model")

    # When: a run is told to use that session by name
    result = _chat("brief", "--name", "mine")

    # Then: its own pin survived — seeding is copy-if-absent
    assert result.exit_code == 0
    assert session_config.read("mine") == session_config.ProviderConfig(
        provider="sub-prov", model="mine-model"
    )


def test_a_plain_chat_still_becomes_the_active_session(
    calls: list[ChatOptions],
) -> None:
    # Given: a session someone else owns, and a plain run with an explicit name
    _write_settings(session="boss-session")

    # When: an ordinary chat runs
    result = _chat("hi", "--name", "my-session")

    # Then: the "active session" convention still holds for plain runs
    assert result.exit_code == 0
    assert _stored_session() == "my-session"


# ── persistence ───────────────────────────────────────────────────────────────


def test_every_delegated_run_turns_with_its_own_session(calls: list[ChatOptions]) -> None:
    # Given: `-a`, which silences stdout and nothing else, on both foreground
    # and `--bg-child` delegation
    assert _chat("fg", "--name", "fg-session", "--return", "--alarm-session", SESSION, "-a").exit_code == 0
    assert _chat(
        "child", "--name", "child-session", "--return", "--alarm-session", SESSION,
        "--bg-child", "--job-id", "job001", "-a",
    ).exit_code == 0

    # Then: each run handed its own session to the turn runner — which owns
    # writing the session file — so no path skips persistence
    assert [(o.session, o.prompt) for o in calls] == [
        ("fg-session", "fg"),
        ("child-session", "child"),
    ]
