"""Minimal TUI built on Textual.

Layout:
  ┌─────────────────────────────────────────────┐
  │ Header                                      │
  ├─────────────────────────────────────────────┤
  │ ChatLog (scrollable)                        │
  │ ...                                         │
  ├─────────────────────────────────────────────┤
  │ Input                                       │
  ├─────────────────────────────────────────────┤
  │ [provider] [model] [style] [session]   <- clickable chips
  │ Tab/click to switch · Enter to open · Esc   │   <- help hint
  └─────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, ClassVar

from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, RichLog, Static

from ..core import lma as lma_api
from ..core.chat import ChatOptions, run_chat
from ..core.events import LoopEvent
from ..core.providers import SUPPORTED_FORMATS, add_provider_from_lma, sdk_to_format
from ..storage import paths
from ..storage import providers as provider_store
from ..storage import sessions as session_store
from ..storage import settings as settings_store
from ..storage import styles as style_store
from ..storage.providers import SOURCE_CUSTOM, SOURCE_LMA
from ..storage.sessions import MessageRecord

SENTINEL_LMA_ADD = "__lma_add__"
SENTINEL_CUSTOM_ADD = "__custom_add__"

# Braille spinner frames. The status line reads e.g. "⠧ Write…" and the
# glyph rotates through this set at ~12.5fps so the bottom of the screen
# shows the agent is alive even between tool calls.
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_INTERVAL = 0.08
_SPINNER_DEFAULT_TEXT = "Working…"

# ─── Clickable chip ──────────────────────────────────────────────────────────


class StatusChip(Static):
    """A focusable, clickable status chip that emits `ChipClicked`."""

    can_focus = True

    class ChipClicked(Message):
        """Posted when the chip is activated (click or Enter)."""

        def __init__(self, chip: StatusChip) -> None:
            super().__init__()
            self.chip = chip

    DEFAULT_CSS = """
    StatusChip {
        height: 1;
        width: 1fr;
        padding: 0 1;
        margin: 0 1 0 0;
        background: $boost;
        color: $text;
        text-style: bold;
        text-overflow: ellipsis;
    }
    StatusChip:hover {
        background: $primary 30%;
        text-style: bold underline;
    }
    StatusChip:focus {
        background: $primary 50%;
        color: $text;
        text-style: bold reverse;
    }
    """

    def __init__(
        self,
        label: str,
        value: str,
        *,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._label = label
        self._value = value

    @property
    def label(self) -> str:
        return self._label

    @property
    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value
        self.refresh()

    def render(self) -> Text:
        text = self._value or "-"
        display = f"{self._label}: {text}"
        return Text(display, style="bold")

    def on_click(self) -> None:
        self.post_message(self.ChipClicked(self))

    def _on_focus(self, _event) -> None:  # type: ignore[no-untyped-def]
        # Surface a hover-style on focus so keyboard navigation is visible.
        self.refresh()

    def key_enter(self) -> None:
        self.post_message(self.ChipClicked(self))


# ─── Modal screens ───────────────────────────────────────────────────────────


class SelectionModal(ModalScreen[str | None]):
    """A modal ListView picker. Returns the selected string, or None on cancel.

    Pass `allow_none=True` to add a "(none)" entry that returns None.
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss_cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    SelectionModal {
        align: center middle;
    }
    #selection-dialog {
        width: 60;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    #selection-title {
        width: 100%;
        content-align: center middle;
        text-style: bold;
        margin-bottom: 1;
    }
    #selection-list {
        height: auto;
        max-height: 20;
        background: $boost;
    }
    #selection-hint {
        width: 100%;
        content-align: center middle;
        color: $text-muted;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        title: str,
        options: list[str],
        *,
        current: str = "",
        allow_none: bool = False,
        none_label: str = "(none — clear)",
        extra_options: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        self._current = current
        self._allow_none = allow_none
        self._none_label = none_label
        self._extra_options = list(extra_options or [])
        self._row_values: dict[ListItem, str | None] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="selection-dialog"):
            yield Label(self._title, id="selection-title")
            items: list[ListItem] = []
            if self._allow_none:
                none_item = ListItem(Label(self._none_label), id="row-none")
                self._row_values[none_item] = None
                items.append(none_item)
            for idx, opt in enumerate(self._options):
                marker = "● " if opt == self._current else "  "
                row = ListItem(Label(marker + opt), id=f"row-opt-{idx}")
                self._row_values[row] = opt
                items.append(row)
            for idx, (sentinel, label) in enumerate(self._extra_options):
                row = ListItem(Label(label), id=f"row-extra-{idx}")
                self._row_values[row] = sentinel
                items.append(row)
            yield ListView(*items, id="selection-list")
            yield Label("Enter to select · Esc to cancel", id="selection-hint")

    def on_mount(self) -> None:
        # Pre-select the current value so Enter picks it without further input.
        if self._current and self._current in self._options:
            lv = self.query_one("#selection-list", ListView)
            for idx, opt in enumerate(self._options):
                if opt == self._current:
                    lv.index = idx + (1 if self._allow_none else 0)
                    break
        lv = self.query_one("#selection-list", ListView)
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        value = self._row_values.get(event.item)
        if value is None and event.item not in self._row_values:
            return
        self.dismiss(value)

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)


class TextInputModal(ModalScreen[str | None]):
    """A modal Input dialog. Returns entered string, or None if cancelled.

    Empty submission returns "" (which the caller treats as "clear").
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss_cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    TextInputModal {
        align: center middle;
    }
    #text-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    #text-title {
        width: 100%;
        content-align: center middle;
        text-style: bold;
        margin-bottom: 1;
    }
    #text-input {
        margin-bottom: 1;
    }
    #text-hint {
        width: 100%;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        title: str,
        *,
        initial: str = "",
        placeholder: str = "",
        hint: str = "Enter to confirm · Esc to cancel",
    ) -> None:
        super().__init__()
        self._title = title
        self._initial = initial
        self._placeholder = placeholder
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="text-dialog"):
            yield Label(self._title, id="text-title")
            yield Input(value=self._initial, placeholder=self._placeholder, id="text-input")
            yield Label(self._hint, id="text-hint")

    def on_mount(self) -> None:
        self.query_one("#text-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)


# ─── Bottom panel ────────────────────────────────────────────────────────────


HELP_TEXT = (
    "[dim]\u2039Tab\u203a cycle chips  \u00b7  "
    "\u2039Enter\u203a open  \u00b7  "
    "\u2039Esc\u203a cancel  \u00b7  "
    "\u2039Ctrl+L\u203a clear  \u00b7  "
    "\u2039Ctrl+C\u203a quit[/dim]"
)


class BottomPanel(Static):
    """The 3-row panel under the input: spinner on top, chips, help hint."""

    DEFAULT_CSS = """
    BottomPanel {
        height: 3;
        background: $boost;
    }
    #spinner-row {
        height: 1;
        padding: 0 1;
    }
    #chip-row {
        height: 1;
    }
    #help-row {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._chips: dict[str, StatusChip] = {}
        self._spinner_row: Static | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="spinner-row")
        with Horizontal(id="chip-row"):
            self._chips["provider"] = StatusChip("provider", "-", id="chip-provider")
            self._chips["model"] = StatusChip("model", "-", id="chip-model")
            self._chips["style"] = StatusChip("style", "-", id="chip-style")
            self._chips["session"] = StatusChip("session", "-", id="chip-session")
            yield from self._chips.values()
        yield Static(HELP_TEXT, id="help-row", markup=True)

    def set_value(self, kind: str, value: str) -> None:
        chip = self._chips.get(kind)
        if chip is not None:
            chip.set_value(value)

    def get_chip(self, kind: str) -> StatusChip | None:
        return self._chips.get(kind)

    def render_spinner(self, frame: str, text: str) -> None:
        if self._spinner_row is None:
            try:
                self._spinner_row = self.query_one("#spinner-row", Static)
            except Exception:
                return
        if not frame:
            self._spinner_row.update("")
            return
        self._spinner_row.update(
            Text.assemble((frame, "bold cyan"), (f" {text}", "white"))
        )


class TUIEventSink:
    """Sink that posts chat events to a running `ChatTUI` app.

    The agent loop runs on a worker thread (via `asyncio.to_thread`), so
    every callback must hop to the main thread with `call_from_thread`
    before touching widgets. The spinner is driven by a Textual `Timer`
    that ticks on the main thread — `start_spin` / `stop_spin` only need
    to enable/disable it; `tick_spin` runs in the main loop already.
    """

    def __init__(self, app: ChatTUI) -> None:
        self._app = app
        self._frame_idx = 0
        self._activity = _SPINNER_DEFAULT_TEXT
        self._timer: Timer | None = None

    def begin_working(self) -> None:
        self._app.call_from_thread(self._start_spin)

    def _start_spin(self) -> None:
        if self._timer is not None:
            return
        self._frame_idx = 0
        self._timer = self._app.set_interval(_SPINNER_INTERVAL, self._tick_spin)

    def _tick_spin(self) -> None:
        frame = _SPINNER_FRAMES[self._frame_idx % len(_SPINNER_FRAMES)]
        self._frame_idx += 1
        self._app._render_spinner(frame, self._activity)

    def update_activity(self, text: str) -> None:
        self._activity = text or _SPINNER_DEFAULT_TEXT

    def end_working(self) -> None:
        self._app.call_from_thread(self._stop_spin)

    def _stop_spin(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._app._render_spinner("", "")

    def emit_loop_event(self, event: LoopEvent) -> None:
        self._app.call_from_thread(self._post_loop_event, event)

    def _post_loop_event(self, event: LoopEvent) -> None:
        log = self._app._log
        if log is None:
            return
        line = Text.assemble(
            (f"{event.actor}:", "orange3"),
            (f" {event.text}", "white"),
        )
        log.write(line)

    def emit_final_answer(self, content: str, session_name: str) -> None:
        self._app.call_from_thread(self._post_final_answer, content, session_name)

    def _post_final_answer(self, content: str, session_name: str) -> None:
        log = self._app._log
        if log is None:
            return
        if content:
            log.write(Markdown(content))
        else:
            log.write(Text("(empty response)", style="dim"))
        log.write(Text(""))


# ─── Main app ────────────────────────────────────────────────────────────────


class ChatTUI(App):
    CSS = """
    Screen { layout: vertical; }
    #chat { height: 1fr; border: solid $primary; }
    #input { height: 3; border: solid $secondary; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+l", "clear_log", "Clear"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._log: RichLog | None = None
        self._input: Input | None = None
        self._panel: BottomPanel | None = None
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            self._log = RichLog(highlight=False, id="chat", wrap=True, markup=False)
            yield self._log
            self._input = Input(placeholder="Type a message and press Enter…", id="input")
            yield self._input
        self._panel = BottomPanel()
        yield self._panel
        yield Footer()

    def on_mount(self) -> None:
        self.title = "miniouto"
        self.sub_title = "outo x subagent"
        self._refresh_chips()
        self._log = self.query_one("#chat", RichLog)
        self._log.write(Text("miniouto TUI ready. Click a chip below to switch settings.", style="dim"))

    # ── chip click routing ──────────────────────────────────────────────────

    def on_status_chip_chip_clicked(self, event: StatusChip.ChipClicked) -> None:
        chip = event.chip
        if chip.id == "chip-provider":
            self._open_provider_picker()
        elif chip.id == "chip-model":
            self._open_model_editor()
        elif chip.id == "chip-style":
            self._open_style_picker()
        elif chip.id == "chip-session":
            self._open_session_picker()

    # ── modal actions ───────────────────────────────────────────────────────

    def _open_provider_picker(self) -> None:
        providers = sorted(provider_store.load_all().keys())
        s = settings_store.load()

        def _on_close(result: str | None) -> None:
            if not result:
                return
            if result == SENTINEL_LMA_ADD:
                self.run_worker(self._lma_add_flow(), exclusive=False)
                return
            if result == SENTINEL_CUSTOM_ADD:
                self._open_custom_add_wizard()
                return
            self._switch_provider(result)

        self.push_screen(
            SelectionModal(
                "Select provider",
                providers,
                current=s.provider,
                allow_none=False,
                extra_options=[
                    (SENTINEL_LMA_ADD, "+ add from lma…"),
                    (SENTINEL_CUSTOM_ADD, "+ add custom…"),
                ],
            ),
            _on_close,
        )

    def _switch_provider(self, name: str) -> None:
        settings_store.update(provider=name)
        self._refresh_chips()
        self._post_system(f"provider → {name}")

    async def _lma_add_flow(self) -> None:
        self._post_system("fetching lma provider list…")
        try:
            all_providers = await asyncio.to_thread(lma_api.list_providers)
        except Exception as exc:
            self._post_system(f"lma error: {exc}")
            return

        name_to_provider: dict[str, dict[str, Any]] = {}
        for p in all_providers:
            fmt, _ = sdk_to_format(p.get("sdk"), p.get("api"))
            if fmt:
                name_to_provider[p["name"]] = p
        if not name_to_provider:
            self._post_system("No lma providers have a supported api_format.")
            return

        picked_name = await self.push_screen_wait(
            SelectionModal(
                f"Add from lma ({len(name_to_provider)} addable)",
                sorted(name_to_provider.keys()),
                allow_none=False,
            )
        )
        if not picked_name:
            return

        lma_p = name_to_provider[picked_name]
        our_name = lma_api.slugify(picked_name)
        if provider_store.get(our_name) is not None:
            choice = await self.push_screen_wait(
                SelectionModal(
                    f"Provider {our_name!r} already exists",
                    ["overwrite", "cancel"],
                    allow_none=False,
                )
            )
            if choice != "overwrite":
                return

        api_key = await self.push_screen_wait(
            TextInputModal(
                f"API key for {picked_name}",
                placeholder="sk-…",
                hint="Enter to confirm · Esc to cancel",
            )
        )
        if api_key is None:
            return

        self._post_system("fetching lma model list…")
        default_model = ""
        try:
            models = await asyncio.to_thread(lma_api.list_models, picked_name)
            if models:
                default_model = models[0].get("id", "")
        except Exception as exc:
            self._post_system(f"lma models error: {exc}")

        try:
            provider = add_provider_from_lma(
                name=our_name,
                api_key=api_key.strip(),
                sdk=lma_p.get("sdk"),
                api=lma_p.get("api"),
                default_model=default_model,
            )
        except ValueError as exc:
            self._post_system(f"add failed: {exc}")
            return

        paths.ensure_dirs()
        provider_store.upsert(provider)
        settings_store.update(provider=our_name)
        self._refresh_chips()
        self._post_system(
            f"provider → {our_name} (added from lma, "
            f"default-model={default_model or '-'})"
        )

    def _open_custom_add_wizard(self) -> None:
        state: dict[str, Any] = {}

        def ask_name() -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                name = lma_api.slugify(result)
                if not name:
                    self._post_system("name required; wizard cancelled.")
                    return
                if provider_store.get(name) is not None:
                    self._post_system(f"Provider {name!r} already exists.")
                    return
                state["name"] = name
                ask_format()

            self.push_screen(
                TextInputModal(
                    "Custom provider: name (lowercase id)",
                    placeholder="my-ollama",
                    hint="Enter to continue · Esc to cancel",
                ),
                on_close,
            )

        def ask_format() -> None:
            def on_close(result: str | None) -> None:
                if result is None or result not in SUPPORTED_FORMATS:
                    return
                state["api_format"] = result
                ask_url()

            self.push_screen(
                SelectionModal(
                    "Custom provider: api_format",
                    list(SUPPORTED_FORMATS),
                    current=state.get("api_format", "openai"),
                    allow_none=False,
                ),
                on_close,
            )

        def ask_url() -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                state["base_url"] = result.strip()
                ask_key()

            self.push_screen(
                TextInputModal(
                    "Custom provider: base URL (optional)",
                    placeholder="https://api.example.com/v1",
                    hint="Empty to skip · Enter to continue · Esc to cancel",
                ),
                on_close,
            )

        def ask_key() -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                state["api_key"] = result.strip()
                ask_model()

            self.push_screen(
                TextInputModal(
                    "Custom provider: api key (optional)",
                    placeholder="sk-…",
                    hint="Empty to skip · Enter to continue · Esc to cancel",
                ),
                on_close,
            )

        def ask_model() -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                model = result.strip()
                paths.ensure_dirs()
                provider_store.upsert(
                    provider_store.Provider(
                        name=state["name"],
                        api_format=state["api_format"],
                        base_url=state["base_url"],
                        api_key=state["api_key"],
                        default_model=model,
                        source=SOURCE_CUSTOM,
                    )
                )
                settings_store.update(provider=state["name"])
                self._refresh_chips()
                self._post_system(f"provider → {state['name']} (custom)")

            self.push_screen(
                TextInputModal(
                    "Custom provider: default model (optional)",
                    placeholder="model-id",
                    hint="Empty to set later · Enter to save · Esc to cancel",
                ),
                on_close,
            )

        ask_name()

    def _open_model_editor(self) -> None:
        s = settings_store.load()
        provider = provider_store.get(s.provider) if s.provider else None
        if not provider:
            self._post_system("No active provider. Pick a provider first.")
            return
        if provider.source == SOURCE_LMA:
            self.run_worker(self._lma_model_picker_flow(provider), exclusive=False)
        else:
            self._open_custom_model_editor(provider)

    def _open_custom_model_editor(self, provider) -> None:
        current = provider.default_model
        placeholder = (
            f"current default: {current}" if current else "model id"
        )

        def _on_close(result: str | None) -> None:
            if result is None:
                return  # cancelled
            self._save_model_change(provider.name, result.strip())

        self.push_screen(
            TextInputModal(
                f"Edit model (custom provider: {provider.name})",
                initial=current,
                placeholder=placeholder,
                hint="Empty to clear · Enter to confirm · Esc to cancel",
            ),
            _on_close,
        )

    async def _lma_model_picker_flow(self, provider) -> None:
        self._post_system("fetching lma model list…")
        try:
            models = await asyncio.to_thread(lma_api.list_models, provider.name)
        except Exception as exc:
            self._post_system(f"lma error: {exc}; falling back to text input")
            self._open_custom_model_editor(provider)
            return
        if not models:
            self._post_system(f"No lma models for {provider.name!r}.")
            return

        options = [f"{m.get('id', '?')} — {m.get('name', '')}" for m in models]
        current = provider.default_model
        current_disp = next(
            (opt for opt in options if opt.split(" — ", 1)[0] == current),
            "",
        )

        picked = await self.push_screen_wait(
            SelectionModal(
                f"Model ({provider.name}, {len(options)})",
                options,
                current=current_disp,
                allow_none=False,
            )
        )
        if not picked:
            return
        new_id = picked.split(" — ", 1)[0].strip()
        self._save_model_change(provider.name, new_id)

    def _save_model_change(self, provider_name: str, new_model: str) -> None:
        p = provider_store.get(provider_name)
        if p is None:
            return
        provider_store.upsert(replace(p, default_model=new_model))
        settings_store.update(model="")
        self._refresh_chips()
        if new_model:
            self._post_system(f"model → {new_model} (provider default)")
        else:
            self._post_system("model → cleared (provider default empty)")

    def _open_style_picker(self) -> None:
        styles = style_store.list_styles()
        if not styles:
            self._post_system("No styles installed. Run `miniouto style add <repo>`.")
            return
        s = settings_store.load()

        def _on_close(result: str | None) -> None:
            if not result:
                return
            settings_store.update(style=result)
            self._refresh_chips()
            self._post_system(f"style → {result}")

        self.push_screen(
            SelectionModal("Select style", styles, current=s.style, allow_none=False),
            _on_close,
        )

    def _open_session_picker(self) -> None:
        sessions = session_store.list_sessions()
        s = settings_store.load()

        def _on_close(result: str | None) -> None:
            if result is None:
                # None means "cancel" — don't touch the allow_none option.
                return
            # Special sentinel for "new session" handled below.
            if result == "__new__":
                self._open_new_session_dialog()
                return
            settings_store.update(session=result)
            self._refresh_chips()
            self._post_system(f"session → {result}")

        options = [*sessions, "__new__"]
        self.push_screen(
            SelectionModal(
                "Select session",
                options,
                current=s.session,
                allow_none=False,
                extra_options=[("__new__", "+ new session…")],
            ),
            _on_close,
        )

    def _open_new_session_dialog(self) -> None:
        def _on_close(result: str | None) -> None:
            if not result or not result.strip():
                return
            name = result.strip()
            settings_store.update(session=name)
            # Touch the session file so it shows up in `list_sessions()`.
            session_store.append(name, MessageRecord(role="system", content="(session created)"))
            self._refresh_chips()
            self._post_system(f"session → {name} (new)")

        self.push_screen(
            TextInputModal(
                "New session name",
                initial="",
                placeholder="e.g. my-project",
                hint="Enter to create · Esc to cancel",
            ),
            _on_close,
        )

    # ── status / refresh ────────────────────────────────────────────────────

    def _refresh_chips(self) -> None:
        if self._panel is None:
            return
        s = settings_store.load()
        provider = provider_store.get(s.provider) if s.provider else None
        active_model = provider.default_model if provider else ""
        self._panel.set_value("provider", s.provider or "-")
        self._panel.set_value("model", active_model or "-")
        self._panel.set_value("style", s.style or "-")
        self._panel.set_value("session", s.session or "-")

    # ── chat flow ───────────────────────────────────────────────────────────

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

    def _post_user(self, text: str) -> None:
        if self._log is None:
            return
        self._log.write(Text("> ", style="bold cyan") + Text(text))

    def _post_assistant(self, text: str) -> None:
        if self._log is None:
            return
        self._log.write(Text(text, style="dark_orange3"))

    def _post_system(self, text: str) -> None:
        if self._log is None:
            return
        self._log.write(Text(f"[{text}]", style="yellow"))

    def _render_spinner(self, frame: str, text: str) -> None:
        if self._panel is not None:
            self._panel.render_spinner(frame, text)

    async def _dispatch(self, prompt: str) -> None:
        assert self._log is not None
        s = settings_store.load()
        self._busy = True
        sink = TUIEventSink(self)
        try:
            opts = ChatOptions(
                prompt=prompt,
                session=s.session or "default",
                model=s.model or None,
            )
            await asyncio.to_thread(run_chat, opts, sink)
        except Exception as exc:
            self._post_system(f"error: {exc}")
            self._busy = False
            return
        self._busy = False
        self._refresh_chips()


def run_tui() -> None:
    ChatTUI().run()


def tui_summary() -> dict[str, Any]:
    s = settings_store.load()
    provider = provider_store.get(s.provider)
    return {
        "provider": s.provider,
        "model": s.model or (provider.default_model if provider else ""),
        "style": s.style,
        "session": s.session,
        "styles_available": style_store.list_styles(),
    }
