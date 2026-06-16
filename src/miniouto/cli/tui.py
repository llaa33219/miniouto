"""Minimal TUI built on Textual.

Layout:
  ┌─────────────────────────────┐
  │ ChatLog (scrollable)        │
  │ ...                         │
  ├─────────────────────────────┤
  │ Input                       │
  ├─────────────────────────────┤
  │ Status: provider / model /  │
  │ style / session | [Settings]│
  └─────────────────────────────┘
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Header, Input, RichLog, Static

from ..core.chat import ChatOptions, run_chat
from ..storage import providers as provider_store
from ..storage import settings as settings_store
from ..storage import styles as style_store


class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $boost;
        color: $text;
        padding: 0 1;
    }
    """

    summary: reactive[str] = reactive("")

    def render(self) -> Text:
        return Text(self.summary, style="bold")


class ChatTUI(App):
    CSS = """
    Screen { layout: vertical; }
    #chat { height: 1fr; border: solid $primary; }
    #input { height: 3; border: solid $secondary; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+l", "clear_log", "Clear"),
        Binding("ctrl+s", "settings", "Settings"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._status: StatusBar | None = None
        self._log: RichLog | None = None
        self._input: Input | None = None
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            self._log = RichLog(highlight=False, id="chat", wrap=True, markup=False)
            yield self._log
            self._input = Input(placeholder="Type a message and press Enter…", id="input")
            yield self._input
        self._status = StatusBar()
        yield self._status
        yield Footer()

    def on_mount(self) -> None:
        self.title = "miniouto"
        self.sub_title = "outo x subagent"
        self._refresh_status()
        self._log.write(Text("miniouto TUI ready. Type to chat, Ctrl+C to quit.", style="dim"))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._busy:
            return
        text = event.value.strip()
        if not text:
            return
        self._input.value = ""
        self._post_user(text)
        self.run_worker(self._dispatch(text), exclusive=True)

    def action_clear_log(self) -> None:
        if self._log is not None:
            self._log.clear()

    def action_settings(self) -> None:
        self._refresh_status()
        if self._log is not None:
            self._log.write(Text(self._status_text(), style="dim"))

    def _post_user(self, text: str) -> None:
        if self._log is None:
            return
        self._log.write(Text("> ", style="bold cyan") + Text(text))

    def _post_assistant(self, text: str) -> None:
        if self._log is None:
            return
        self._log.write(Text(text))

    def _post_system(self, text: str) -> None:
        if self._log is None:
            return
        self._log.write(Text(f"[{text}]", style="yellow"))

    def _refresh_status(self) -> None:
        if self._status is None:
            return
        self._status.summary = self._status_text()

    def _status_text(self) -> str:
        s = settings_store.load()
        provider = provider_store.get(s.provider)
        model = provider.default_model if provider else ""
        base = provider.base_url if provider else "-"
        return (
            f"provider: {s.provider or '-'}  model: {model or '- (use --model)'}  "
            f"style: {s.style or '-'}  session: {s.session or '-'}  "
            f"base: {base}"
        )

    async def _dispatch(self, prompt: str) -> None:
        assert self._log is not None
        s = settings_store.load()
        self._busy = True
        self._post_system("thinking…")
        try:
            opts = ChatOptions(prompt=prompt, session=s.session or "default")
            reply = await asyncio.to_thread(run_chat, opts)
        except Exception as exc:
            self._post_system(f"error: {exc}")
            self._busy = False
            return
        if not reply:
            self._post_system("(empty response)")
        else:
            self._log.write(Text(""))
            self._post_assistant(reply)
            self._log.write(Text(""))
        self._busy = False
        self._refresh_status()


def run_tui() -> None:
    ChatTUI().run()


def tui_summary() -> dict[str, Any]:
    s = settings_store.load()
    provider = provider_store.get(s.provider)
    return {
        "provider": s.provider,
        "model": provider.default_model if provider else "",
        "style": s.style,
        "session": s.session,
        "styles_available": style_store.list_styles(),
    }
