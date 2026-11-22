"""`LoopEvent.detail` for tool calls: the primary argument, every loggable tool.

`_make_tool_call_dispatcher` stamps the tool call's PRIMARY argument into
`detail` — Bash's verbatim multi-line command, the media tools' file path,
Computer's action — while `text` keeps its `<name> <preview>` shape, because
the CLI output and the session records read `text` (the TUI's expanded boxes
read `detail`). `continue_session` gets NO detail: its header already names
the summary dir and its badge-less row would print it twice.
"""
from __future__ import annotations

from typing import Any

from miniouto.core import chat as chat_mod
from miniouto.core.events import LoopEvent


class _RawSink:
    """Minimal EventSink that keeps every loop event it is handed."""

    def __init__(self) -> None:
        self.events: list[LoopEvent] = []

    def begin_working(self) -> None: ...
    def update_activity(self, text: str) -> None: ...
    def end_working(self) -> None: ...
    def emit_loop_event(self, event: LoopEvent) -> None:
        self.events.append(event)

    def emit_final_answer(self, content: str, session_name: str) -> None: ...
    def emit_stream_delta(self, kind: str, text: str) -> None: ...
    def remove_events(self, msg_refs: list[str]) -> None: ...


def _dispatch(name: str, arguments: dict[str, Any]) -> LoopEvent:
    """Run one tool call through the dispatcher and return its event."""

    sink = _RawSink()
    on_tool_call = chat_mod._make_tool_call_dispatcher(sink, {})
    on_tool_call(name, arguments)
    (event,) = sink.events
    return event


def test_bash_detail_carries_the_verbatim_multiline_command() -> None:
    # Given: a Bash call whose command spans lines (a heredoc write)
    command = "cat > /tmp/f <<'EOF'\nline one\nline two\nEOF"
    # When: the dispatcher fires
    event = _dispatch("Bash", {"command": command})
    # Then: detail is the verbatim command — newlines intact, nothing flattened
    assert event.detail == command
    # And: text keeps its `<name> <preview>` shape for the CLI and the record
    assert event.text == "Bash " + command.replace("\n", " ")
    assert event.tool_name == "Bash"


def test_media_details_carry_the_file_path() -> None:
    # Given: one call per media tool, each naming a file
    for name in ("Image", "Video", "Audio"):
        # When: the dispatcher fires
        event = _dispatch(name, {"file_path": f"/tmp/shot.{name.lower()}"})
        # Then: detail is the path, and text keeps its shape
        assert event.detail == f"/tmp/shot.{name.lower()}"
        assert event.text == f"{name} /tmp/shot.{name.lower()}"


def test_computer_detail_carries_the_action() -> None:
    event = _dispatch("Computer", {"action": "screenshot"})
    assert event.detail == "screenshot"
    assert event.text == "Computer screenshot"


def test_continue_session_carries_no_detail_its_header_names_the_dir() -> None:
    # The header already prints the summary dir and the TUI never boxes this
    # row (it is badge-less, so its legacy expanded layout prints `detail`
    # verbatim under that header) — stamping detail would print the dir twice.
    event = _dispatch("continue_session", {"summary_dir": "/proj/.miniouto/summary/s1"})
    assert event.detail is None
    assert event.text == "continue_session /proj/.miniouto/summary/s1"


def test_a_missing_primary_argument_yields_no_detail() -> None:
    for name in ("Bash", "Image", "Computer", "continue_session"):
        assert _dispatch(name, {}).detail is None
    assert _dispatch("Image", {"file_path": None}).detail is None
    assert _dispatch("Computer", {"action": 3}).detail is None


def test_a_non_loggable_tool_emits_no_event_at_all() -> None:
    # Given: a tool outside _LOGGABLE_TOOL_NAMES
    recording = _RawSink()
    on_tool_call = chat_mod._make_tool_call_dispatcher(recording, {})
    # When: the dispatcher fires for it
    on_tool_call("NotATool", {"command": "x"})
    # Then: nothing was emitted — the detail change never widened the set of
    # tools that log
    assert recording.events == []
