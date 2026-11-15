"""Modal-screen tests: the pickers, the settings popup and the command palette.

These port the modal assertions of the old Textual suites (they ARE the
specification) onto the PyRatatui modal objects:

* ``test_tui_navigation.py`` — the settings popup's row order and values, the
  provider change that resets Model and Reasoning, the ``Caps`` row's whole
  lifecycle, the catalog ``Compatibility`` picker, and the subagent trio's
  "same as outo" semantics (AGENTS.md invariant 12);
* ``test_session_cwd_grouping.py`` — the selection modal's section headers and
  ``●`` markers;
* ``test_issue_fixes.py`` — the provider picker opens with an empty registry and
  offers both "add" entries.

Two rules shape the file:

1. **A real storage round-trip.** Every popup assertion reads the writes back
   through ``session_config`` / ``settings`` / ``provider_store``, so a flow that
   repaints but forgets to persist fails here. Only the provider registry's
   REASONING ladder and modelslist's HTTP calls are stubbed — the write paths are
   production code.
2. **No app layer needed.** ``modals.py`` talks to the app through the framework
   contract (``push_screen`` / ``pop_screen`` / ``push_screen_wait`` /
   ``run_worker`` / ``size`` / ``get_system_commands``); :class:`FakeApp` below
   implements exactly that surface, including the screen stack and the awaited
   futures, so the same code path the real app will drive runs here.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from miniouto.cli.tui import config as config_module
from miniouto.cli.tui import modals as modals_module
from miniouto.cli.tui.input import Key, Mouse
from miniouto.cli.tui.modals import (
    LIST_MAX_ROWS,
    PROVIDER_HINT,
    SELECTION_HINT,
    TEXT_INPUT_HINT,
    CommandPalette,
    ListItem,
    ProviderSettingsPopup,
    Rect,
    SelectionModal,
    SettingRow,
    SystemCommand,
    TextInputModal,
    default_system_commands,
)
from miniouto.cli.tui.theme import MINIOUTO_THEME, color
from miniouto.storage import providers as provider_store
from miniouto.storage import session_config
from miniouto.storage import settings as settings_store

SCREEN = Rect(0, 0, 120, 40)


# ── the app double ──────────────────────────────────────────────────────────


class FakeApp:
    """The framework contract's app surface, minus rendering and the pilot.

    ``push_screen`` pushes and mounts; ``pop_screen`` pops; ``push_screen_wait``
    is exactly ``push_screen`` + ``screen.wait_result()`` — the semantic the
    contract specifies. ``run_worker`` is a real task, so an async flow runs on
    the loop the test already drives with ``await asyncio.sleep(0)``.
    """

    def __init__(self, width: int = 120, height: int = 40) -> None:
        self.size = type("Size", (), {"width": width, "height": height})()
        self.screens: list[Any] = []
        self.tasks: list[asyncio.Task[Any]] = []
        self.commands: list[SystemCommand] = []

    # screens ────────────────────────────────────────────────────────────────
    def push_screen(self, screen: Any, on_close: Any = None) -> Any:
        screen.attach(self, on_close)
        self.screens.append(screen)
        return screen

    async def push_screen_wait(self, screen: Any) -> Any:
        self.push_screen(screen)
        return await screen.wait_result()

    def pop_screen(self, result: Any = None) -> None:
        if self.screens:
            self.screens.pop()

    @property
    def screen(self) -> Any:
        return self.screens[-1] if self.screens else None

    # workers / commands ─────────────────────────────────────────────────────
    def run_worker(self, coro: Any, exclusive: bool = False) -> asyncio.Task[Any]:
        """Schedule ``coro`` on the running loop; discard it when there is none.

        ``ensure_future`` needs a CURRENT event loop. A synchronous test — an
        entry point called with no ``asyncio.run`` in scope, possibly after
        another test closed the default loop — has none, and running the coroutine
        there would BLOCK forever: a flow that awaits ``push_screen_wait`` waits on
        a dialog only the test could dismiss. Such a caller is asserting the
        SYNCHRONOUS part of the entry point (what it reports before starting any
        flow), so the coroutine is closed unstarted, which is what Textual's
        ``run_worker`` did for a message that never reached the pump.
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return None
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    def get_system_commands(self, _screen: Any = None) -> list[SystemCommand]:
        return list(self.commands)


async def settle(rounds: int = 12) -> None:
    """Let queued callbacks and worker tasks run to completion."""

    for _ in range(rounds):
        await asyncio.sleep(0)


def run(body: Any) -> None:
    asyncio.run(body())


# ── fixtures ────────────────────────────────────────────────────────────────


class FakeReasoning:
    """The modelslist reasoning boundary, stubbed per model."""

    def __init__(self, ladder: dict[str, list[str]], defaults: dict[str, str]) -> None:
        self._ladder = ladder
        self._defaults = defaults

    def reasoning_choices(self, model: str, _provider: str | None) -> list[str] | None:
        return self._ladder.get(model)

    def default_reasoning_choice(self, model: str, _provider: str | None) -> str | None:
        return self._defaults.get(model)


@pytest.fixture
def settings_home(monkeypatch: pytest.MonkeyPatch) -> str:
    """Two CATALOG providers with different defaults, and no reasoning except on one.

    ``pop-alpha`` offers an effort ladder and records ``default_model`` plus
    ``reasoning_effort``; ``pop-beta`` has no reasoning options at all. That split
    is what proves a provider change actually MOVES the other two rows.

    Both are ``source="modelslist"`` on purpose: a hand-added provider carries the
    ``Caps`` row, and these tests are about the trio alone.
    """

    provider_store.upsert(
        provider_store.Provider(
            name="pop-alpha",
            api_format="openai",
            default_model="alpha-default",
            source=provider_store.SOURCE_MODELSLIST,
            reasoning_effort="high",
        )
    )
    provider_store.upsert(
        provider_store.Provider(
            name="pop-beta",
            api_format="openai",
            default_model="beta-default",
            source=provider_store.SOURCE_MODELSLIST,
        )
    )
    monkeypatch.setattr(
        config_module,
        "_get_reasoning_api",
        lambda: FakeReasoning(
            ladder={"alpha-default": ["off", "low", "high"], "alpha-fast": ["off", "low"]},
            defaults={"alpha-default": "low"},
        ),
    )
    saved = settings_store.load()
    yield "pop-alpha"
    settings_store.save(saved)
    provider_store.remove("pop-alpha")
    provider_store.remove("pop-beta")


@pytest.fixture
def custom_provider() -> str:
    """A hand-added provider — the only kind whose caps the popup will edit."""

    name = "pop-custom"
    provider_store.upsert(
        provider_store.Provider(
            name=name,
            api_format="openai",
            default_model="custom-default",
            source=provider_store.SOURCE_CUSTOM,
        )
    )
    yield name
    provider_store.remove(name)


@pytest.fixture
def session() -> str:
    """A fresh pinned session name per test, cleared before and after."""

    name = "modals-fixture-session"
    session_config.clear(name)
    yield name
    session_config.clear(name)


def open_popup(
    app: FakeApp,
    session: str,
    target: Any = config_module.OUTO_TARGET,
    changes: list[int] | None = None,
) -> ProviderSettingsPopup:
    """Open the popup the way the chip's click handler does, and record changes."""

    popup = ProviderSettingsPopup(
        target,
        session=session,
        on_change=(lambda: changes.append(1)) if changes is not None else (lambda: None),
    )
    app.push_screen(popup)
    return popup


def fields(popup: ProviderSettingsPopup) -> list[str]:
    return [row.field for row in popup.rows]


def texts(popup: ProviderSettingsPopup) -> list[str]:
    return [row.render().plain for row in popup.rows]


def press(screen: Any, *codes: str) -> None:
    for code in codes:
        screen.handle_key(Key(code))


def click(node: Any, dx: int = 0) -> Mouse:
    """A left press in the middle of ``node``'s region (offset by ``dx``)."""

    return Mouse("down", node.region.x + dx, node.region.y, button=0)


# ── SelectionModal: rows, markers, sections ─────────────────────────────────
#
# Ported from `test_session_cwd_grouping.py`. A section header is a
# DISPLAY-ONLY row (no mapped value), options carry the `●` marker when they are
# the current value, and the pre-selected row index is the real row — not the
# option's position in the options list, which stops matching the moment a
# header row is inserted above it.


def _rows(modal: SelectionModal) -> list[tuple[str, str | None, bool]]:
    return [
        (item.label.plain, modal._row_values.get(item), "-header" in item.classes)
        for item in modal.list.children
    ]


def test_selection_modal_renders_section_headers_and_preselects_correctly() -> None:
    modal = SelectionModal(
        "Select session",
        [],
        current="sess-b",
        allow_none=False,
        extra_options=[("__new__", "+ new session…")],
        sections=[
            ("This directory", [("sess-a", "sess-a")]),
            ("Other directories", [("sess-b", "[proj] sess-b")]),
        ],
    )

    assert _rows(modal) == [
        ("This directory", None, True),
        ("  sess-a", "sess-a", False),
        ("Other directories", None, True),
        ("● [proj] sess-b", "sess-b", False),
        ("  + new session…", "__new__", False),
    ]
    # The pre-selected row index is the real option row, not the option's index.
    assert modal._row_index_by_value == {"sess-a": 1, "sess-b": 3, "__new__": 4}
    assert modal.index == 3
    assert modal.highlighted_value() == "sess-b"


def test_selection_modal_flat_rendering_is_unchanged_without_sections() -> None:
    modal = SelectionModal(
        "Select style",
        ["a", "b"],
        current="b",
        allow_none=False,
        extra_options=[("__new__", "+ new")],
    )

    assert _rows(modal) == [
        ("  a", "a", False),
        ("● b", "b", False),
        ("  + new", "__new__", False),
    ]
    assert modal._row_index_by_value == {"a": 0, "b": 1, "__new__": 2}


def test_the_none_entry_returns_none_and_sits_above_everything_else() -> None:
    modal = SelectionModal("Pick", ["a"], allow_none=True, none_label="(none — clear)")

    assert _rows(modal) == [("(none — clear)", None, False), ("  a", "a", False)]
    assert modal._row_index_by_value == {"a": 1}
    assert modal.index == 0  # ListView's own default: the first row
    press(modal, "Enter")
    assert modal.result is None
    assert modal.dismissed


def test_section_headers_are_skipped_by_arrow_keys() -> None:
    modal = SelectionModal(
        "Select session",
        [],
        sections=[
            ("This directory", [("a", "a")]),
            ("Other directories", [("b", "b")]),
        ],
    )
    # ListView mounts on its first row, so the picker opens ON a selection.
    assert modal.index == 1
    assert modal.highlighted_value() == "a"

    assert modal.highlighted_value() == "a"
    press(modal, "Down")
    # The header between the two groups is skipped, not stopped on.
    assert modal.index == 3
    assert modal.highlighted_value() == "b"
    press(modal, "Down")
    assert modal.highlighted_value() == "b"  # clamped at the last selectable row
    press(modal, "Up")
    assert modal.highlighted_value() == "a"


def test_enter_selects_the_highlighted_value_and_esc_cancels() -> None:
    modal = SelectionModal("Pick", ["a", "b"], current="a")
    press(modal, "Down", "Enter")
    assert modal.result == "b"

    cancelled = SelectionModal("Pick", ["a", "b"], current="a")
    press(cancelled, "Esc")
    assert cancelled.result is None


def test_a_click_on_a_row_selects_it() -> None:
    modal = SelectionModal("Pick", ["a", "b", "c"])
    modal.build_lines(SCREEN, MINIOUTO_THEME)
    target = modal.rows[2]

    assert modal.handle_mouse(click(target)) is True
    assert modal.result == "c"


def test_the_list_stops_at_twenty_rows_and_scrolls_instead() -> None:
    options = [f"opt{index:02d}" for index in range(30)]
    modal = SelectionModal("Many", options, current="opt00")
    modal.build_lines(SCREEN, MINIOUTO_THEME)

    assert modal.list_rows(modal.row_budget(SCREEN)) == LIST_MAX_ROWS
    for _ in range(25):
        modal.handle_key(Key("Down"))
    visible = [row.label.plain for _, row in modal.list.window(LIST_MAX_ROWS)]
    assert len(visible) == LIST_MAX_ROWS
    assert modal.list.index == 25
    # The window followed the highlight: it scrolled exactly far enough that
    # the selected row is the LAST visible one, so the oldest rows are gone.
    assert "opt06" in visible[0]
    assert "opt25" in visible[-1]
    assert modal.list.rows[25].highlighted
    assert not any("opt29" in label for label in visible)


# ── SelectionModal: the painted dialog ──────────────────────────────────────


def test_the_dialog_is_centered_with_the_thick_primary_border() -> None:
    modal = SelectionModal("Select provider", ["pop-alpha"], current="pop-alpha")
    lines = modal.build_lines(SCREEN, MINIOUTO_THEME)
    painted = ["".join(text for text, _ in line) for line in lines]

    assert modal.region.width == 60
    assert modal.region.x == (SCREEN.width - 60) // 2
    # The backdrop is padded left/right, so the box starts at the region's x.
    left = modal.region.x
    top = painted[modal.region.y]
    assert top[left : left + 60] == "█" + "▀" * 58 + "█"
    assert painted[modal.region.bottom - 1][left : left + 60] == "█" + "▄" * 58 + "█"
    assert painted[modal.region.y + 1][left : left + 60].strip("█ ") == ""
    # `padding: 1 2` + the centered bold title.
    assert painted[modal.region.y + 2][left : left + 60].strip("█ ") == "Select provider"
    # The hint is centred inside the box, above the bottom padding row.
    assert SELECTION_HINT in painted[modal.region.bottom - 3]


def test_the_focused_row_is_a_solid_primary_bar() -> None:
    modal = SelectionModal("Pick", ["a", "b"], current="b")
    lines = modal.build_lines(SCREEN, MINIOUTO_THEME)
    row = modal.rows[1]

    row_line = next(
        line for line in lines if any("● b" in text for text, _ in line)
    )
    body = [stripe for stripe in row_line if "● b" in stripe[0]]
    # The whole row is one run: solid primary background, bold near-black ink.
    assert body[0][0].startswith("● b")
    assert str(body[0][1].background) == str(color(MINIOUTO_THEME.primary))
    assert "BOLD" in str(body[0][1])
    assert row.region.height == 1


def test_the_dialog_never_exceeds_the_screen_but_the_list_caps_at_twenty() -> None:
    options = [f"opt{index:02d}" for index in range(30)]
    for height in (40, 24, 12):
        modal = SelectionModal("Many", options, current="opt00")
        modal.build_lines(Rect(0, 0, 80, height), MINIOUTO_THEME)
        assert modal.region.y >= 0
        assert modal.region.bottom <= height
        assert modal.list.region.bottom <= modal.region.bottom
        assert modal.hint.region.bottom <= modal.region.bottom


# ── TextInputModal: submit and cancel ───────────────────────────────────────


def test_text_input_submits_the_typed_value() -> None:
    modal = TextInputModal("Max context window — pop-custom", placeholder="e.g. 128000")

    for char in "128000":
        modal.handle_key(Key(char))
    assert modal.value == "128000"
    modal.handle_key(Key("Enter"))
    assert modal.result == "128000"
    assert modal.dismissed


def test_text_input_submits_empty_for_a_cleared_value() -> None:
    modal = TextInputModal("Title", initial="200000")

    modal.handle_key(Key("u", ctrl=True))
    modal.handle_key(Key("Enter"))
    assert modal.result == ""


def test_text_input_cancel_returns_none() -> None:
    modal = TextInputModal("Title", initial="abc")

    modal.handle_key(Key("Esc"))
    assert modal.result is None
    assert modal.dismissed


def test_text_input_starts_with_the_initial_value_and_can_edit_it() -> None:
    modal = TextInputModal("Title", initial="2000")

    assert modal.value == "2000"
    modal.handle_key(Key("0"))
    assert modal.value == "20000"
    modal.handle_key(Key("Backspace"))
    assert modal.value == "2000"


def test_text_input_uses_the_tall_border_and_shows_the_placeholder() -> None:
    modal = TextInputModal("Title", placeholder="model id")
    painted = ["".join(text for text, _ in line) for line in modal.build_lines(SCREEN, MINIOUTO_THEME)]
    input_top = modal.input.region.y

    inner = slice(modal.input.region.x, modal.input.region.right)
    assert painted[input_top][inner].startswith("▊▔")
    assert painted[input_top][inner].endswith("▎")
    assert painted[input_top + 1][inner].startswith("▊")
    assert painted[input_top + 2][inner].startswith("▊▁")
    assert "model id" in painted[input_top + 1]
    assert TEXT_INPUT_HINT in painted[modal.region.bottom - 3]


def test_the_cursor_blinks_between_the_block_and_the_bare_cell() -> None:
    modal = TextInputModal("Title", initial="ab")
    first = "".join(text for text, _ in modal.build_lines(SCREEN, MINIOUTO_THEME)[modal.input.region.y + 1])
    modal.tick(0.5)
    second = "".join(
        text for text, _ in modal.build_lines(SCREEN, MINIOUTO_THEME)[modal.input.region.y + 1]
    )

    assert first != second or modal.input.blink_on is False
    assert "ab" in first and "ab" in second


# ── ProviderSettingsPopup: rows, values, order ──────────────────────────────
#
# Ported from `test_tui_navigation.py`. The chip pair opens ONE popup whose three
# rows are the trio it edits; a CATALOG provider then gets a Compatibility row.
# Two rules make it correct and both fail silently otherwise:
#
# 1. A pick writes BOTH the pinned session's config and the global default, so two
#    processes editing different sessions never redirect each other while a fresh
#    session still picks up the newest choice.
# 2. Picking a PROVIDER resets Model and Reasoning immediately, because those two
#    belong to the provider being left behind.


@pytest.mark.parametrize(
    ("target", "title"),
    [
        (config_module.OUTO_TARGET, "outo settings"),
        (config_module.SUBAGENT_TARGET, "subagent settings"),
    ],
)
def test_one_popup_serves_both_chips_with_its_own_title(
    target: Any, title: str, settings_home: str, session: str
) -> None:
    app = FakeApp()
    popup = open_popup(app, session, target)

    assert popup.title_text == title
    assert popup.query_one("#provider-settings-title").render().plain == title
    assert app.screen is popup


def test_the_popup_lists_the_trio_then_catalog_compatibility_in_order(
    settings_home: str, session: str
) -> None:
    settings_store.update(provider="pop-alpha")
    popup = open_popup(FakeApp(), session)

    assert fields(popup) == ["provider", "model", "reasoning", "compatibility"]
    assert texts(popup) == [
        "Provider: pop-alpha",
        "Model: alpha-default",
        "Reasoning: high",
        "Compatibility: native SDK",
    ]
    assert popup.query_one("#provider-settings-hint").render().plain == PROVIDER_HINT


def test_each_trio_row_opens_its_own_picker(settings_home: str, session: str) -> None:
    settings_store.update(provider="pop-alpha")
    app = FakeApp()
    popup = open_popup(app, session)

    for field in ("provider", "reasoning"):
        popup.row(field).on_click()
        picker = app.screen
        assert isinstance(picker, SelectionModal), field
        picker.query_one("#selection-list")
        picker.dismiss(None)
        assert app.screen is popup


def test_changing_the_provider_resets_model_and_reasoning_immediately(
    settings_home: str, session: str
) -> None:
    settings_store.update(provider="pop-alpha")
    app = FakeApp()
    popup = open_popup(app, session)
    assert popup.row_text("model") == "Model: alpha-default"

    popup.row("provider").on_click()
    assert isinstance(app.screen, SelectionModal)
    app.screen.dismiss("pop-beta")

    assert popup.row_text("provider") == "Provider: pop-beta"
    assert popup.row_text("model") == "Model: beta-default"
    assert popup.row_text("reasoning") == "Reasoning: -"
    pinned = session_config.effective(session)
    assert (pinned.provider, pinned.model, pinned.reasoning) == ("pop-beta", "beta-default", "")


def test_a_provider_change_takes_the_modelslist_default_when_the_record_has_none(
    settings_home: str, session: str
) -> None:
    provider_store.upsert(
        provider_store.Provider(
            name="pop-auto", api_format="openai", default_model="alpha-default"
        )
    )
    settings_store.update(provider="pop-beta")
    app = FakeApp()
    popup = open_popup(app, session)

    popup.row("provider").on_click()
    app.screen.dismiss("pop-auto")

    # The record stores no reasoning, so the model's modelslist default fills it.
    assert popup.row_text("reasoning") == "Reasoning: low"
    provider_store.remove("pop-auto")


def test_picking_a_model_updates_its_row_and_writes_both_stores(
    settings_home: str, session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        modals_module.catalog_api,
        "list_models",
        lambda _provider: [{"id": "alpha-default"}, {"id": "alpha-fast", "name": "Fast"}],
    )
    settings_store.update(provider="pop-alpha")
    app = FakeApp()
    popup = open_popup(app, session)

    async def body() -> None:
        task = app.run_worker(popup._model_flow("pop-alpha", "alpha-default"))
        await settle()
        assert isinstance(app.screen, SelectionModal)
        options = [item.label.plain for item in app.screen.list.children]
        # modelslist's own order, the entry in use marked.
        assert options[0].strip() == "● alpha-default —"
        assert options[1].strip() == "alpha-fast — Fast"
        app.screen.dismiss("alpha-fast — Fast")
        await settle()
        assert task.done()

    run(body)

    assert popup.row_text("model") == "Model: alpha-fast"
    assert isinstance(app.screen, ProviderSettingsPopup)
    pinned = session_config.effective(session)
    assert (pinned.provider, pinned.model) == ("pop-alpha", "alpha-fast")
    # The GLOBAL default followed, so a fresh session starts here.
    assert settings_store.load().provider == "pop-alpha"
    assert provider_store.get("pop-alpha").default_model == "alpha-fast"


def test_the_model_row_falls_back_to_free_text_when_the_catalog_is_empty(
    settings_home: str, session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(modals_module.catalog_api, "list_models", lambda _provider: [])
    settings_store.update(provider="pop-alpha")
    app = FakeApp()
    popup = open_popup(app, session)

    async def body() -> None:
        task = app.run_worker(popup._model_flow("pop-alpha", "alpha-default"))
        await settle()
        assert isinstance(app.screen, TextInputModal)
        assert app.screen.value == "alpha-default"
        app.screen.dismiss("alpha-hand-written")
        await settle()
        assert task.done()

    run(body)

    assert popup.row_text("model") == "Model: alpha-hand-written"


def test_the_model_row_reports_when_no_provider_is_configured(
    settings_home: str, session: str
) -> None:
    # `update` treats "" as "no change" (a provider edit is a PAIR edit), so a
    # test that needs NO provider has to save the record it wants explicitly.
    settings_store.save(replace(settings_store.load(), provider="", model=""))
    app = FakeApp()
    popup = open_popup(app, session)

    popup.row("model").on_click()

    assert popup.status_text == "Pick a provider first."
    assert app.screen is popup


def test_the_reasoning_row_is_disabled_for_a_model_without_levels(
    settings_home: str, session: str
) -> None:
    settings_store.update(provider="pop-beta")
    app = FakeApp()
    popup = open_popup(app, session)

    row = popup.row("reasoning")
    assert row is not None
    assert row.enabled is False
    assert row.render().plain == "Reasoning: -"
    popup.build_lines(SCREEN, MINIOUTO_THEME)
    popup.handle_mouse(click(row))
    assert app.screen is popup, "the disabled row opened a picker anyway"


def test_the_reasoning_picker_offers_the_models_ladder(
    settings_home: str, session: str
) -> None:
    settings_store.update(provider="pop-alpha")
    app = FakeApp()
    popup = open_popup(app, session)

    popup.row("reasoning").on_click()
    picker = app.screen
    assert isinstance(picker, SelectionModal)
    assert [row.label.plain for row in picker.list.children] == ["  off", "  low", "● high"]

    picker.dismiss("low")
    assert popup.row_text("reasoning") == "Reasoning: low"


def test_a_pick_calls_on_change_so_the_chips_follow_without_a_reload(
    settings_home: str, session: str
) -> None:
    settings_store.update(provider="pop-alpha")
    changes: list[int] = []
    app = FakeApp()
    popup = open_popup(app, session, changes=changes)

    popup.row("reasoning").on_click()
    app.screen.dismiss("high")

    assert changes == [1]


# ── the Caps row: a fourth row, only for a custom provider ──────────────────
#
# Caps are an ENDPOINT property of a hand-added provider record — a catalog
# provider's caps come from modelslist, so there is nothing to override and the
# row must not exist at all. Unlike the trio rows a caps save writes the record
# ALONE (never a session pin), and it has to drop `core.context`'s per-process
# cache or a long-lived TUI keeps serving the caps modelslist answered before.


def edit_caps(app: FakeApp, popup: ProviderSettingsPopup, context_window: str, tokens: str) -> None:
    """Click the Caps row and walk its two prompts, as a user would."""

    popup.row("caps").on_click()
    first = app.screen
    assert isinstance(first, TextInputModal), "the caps row opened no editor"
    first.dismiss(context_window)
    second = app.screen
    assert isinstance(second, TextInputModal), "no second caps prompt"
    second.dismiss(tokens)
    assert app.screen is popup, "the popup did not survive the edit"


def test_the_caps_row_is_absent_for_a_catalog_provider(
    settings_home: str, custom_provider: str, session: str
) -> None:
    settings_store.update(provider="pop-alpha")
    popup = open_popup(FakeApp(), session)

    assert fields(popup) == ["provider", "model", "reasoning", "compatibility"]


def test_the_caps_row_reads_unset_as_a_dash_pair_for_a_custom_provider(
    settings_home: str, custom_provider: str, session: str
) -> None:
    settings_store.update(provider="pop-custom")
    popup = open_popup(FakeApp(), session)

    assert fields(popup) == ["provider", "model", "reasoning", "caps"]
    assert popup.row_text("caps") == "Caps: - / -"


def test_changing_the_provider_adds_and_removes_the_caps_row(
    settings_home: str, custom_provider: str, session: str
) -> None:
    settings_store.update(provider="pop-custom")
    app = FakeApp()
    popup = open_popup(app, session)
    assert "caps" in fields(popup)

    popup.row("provider").on_click()
    app.screen.dismiss("pop-alpha")
    assert fields(popup) == ["provider", "model", "reasoning", "compatibility"]

    # Moving back brings it, labelled with THAT record's caps.
    provider_store.upsert(
        replace(
            provider_store.get("pop-custom"),
            max_context_window=200000,
            max_output_tokens=16384,
        )
    )
    popup.row("provider").on_click()
    app.screen.dismiss("pop-custom")

    assert fields(popup) == ["provider", "model", "reasoning", "caps"]
    assert popup.row_text("caps") == "Caps: 200000 / 16384"


def test_the_caps_editor_writes_the_record_and_leaves_the_pin_alone(
    settings_home: str, custom_provider: str, session: str
) -> None:
    settings_store.update(provider="pop-custom")
    app = FakeApp()
    popup = open_popup(app, session)
    pin_before = session_config.read(session)

    edit_caps(app, popup, "200000", "16384")

    record = provider_store.get("pop-custom")
    assert (record.max_context_window, record.max_output_tokens) == (200000, 16384)
    assert popup.row_text("caps") == "Caps: 200000 / 16384"
    # Caps are global per provider: a second session must not inherit the edit.
    assert session_config.read(session) == pin_before


def test_empty_caps_input_clears_the_override_back_to_unset(
    settings_home: str, custom_provider: str, session: str
) -> None:
    provider_store.upsert(
        replace(
            provider_store.get("pop-custom"),
            max_context_window=200000,
            max_output_tokens=16384,
        )
    )
    settings_store.update(provider="pop-custom")
    app = FakeApp()
    popup = open_popup(app, session)
    assert popup.row_text("caps") == "Caps: 200000 / 16384"

    edit_caps(app, popup, "", "")

    record = provider_store.get("pop-custom")
    assert (record.max_context_window, record.max_output_tokens) == (None, None)
    assert popup.row_text("caps") == "Caps: - / -"


def test_a_non_numeric_cap_is_refused_and_the_stored_value_survives(
    settings_home: str, custom_provider: str, session: str
) -> None:
    provider_store.upsert(replace(provider_store.get("pop-custom"), max_context_window=1000))
    settings_store.update(provider="pop-custom")
    app = FakeApp()
    popup = open_popup(app, session)

    popup.row("caps").on_click()
    app.screen.dismiss("lots")

    assert popup.status_text == "max context window must be a positive number."
    assert provider_store.get("pop-custom").max_context_window == 1000
    assert app.screen is popup


def test_a_caps_save_drops_the_context_model_cache(
    settings_home: str, custom_provider: str, session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_calls: list[int] = []
    monkeypatch.setattr(modals_module.context_api, "clear_model_cache", lambda: clear_calls.append(1))
    settings_store.update(provider="pop-custom")
    app = FakeApp()
    popup = open_popup(app, session)

    edit_caps(app, popup, "200000", "16384")

    # Without this the process would serve the pre-edit caps until exit.
    assert clear_calls == [1]


# ── the catalog Compatibility row ───────────────────────────────────────────

CATALOG_PROVIDER = {
    "name": "Catalog Gateway",
    "slug": "catalog-gateway",
    "formats": [
        {"format": "openai", "baseUrl": "https://gateway.example/v1"},
        {"format": "anthropic", "baseUrl": "https://gateway.example/anthropic"},
        {"format": "openai-responses", "baseUrl": "https://gateway.example/v1/responses"},
        {"format": "typesafe", "baseUrl": "https://ignored.example"},
    ],
}


@pytest.fixture
def dialect_provider(settings_home: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """A catalog provider whose advertised formats include one unsupported one."""

    provider_store.upsert(
        provider_store.Provider(
            name="catalog-gateway",
            api_format="openai",
            base_url="https://gateway.example/v1",
            catalog_dialect="openai",
            source=provider_store.SOURCE_MODELSLIST,
        )
    )
    monkeypatch.setattr(
        modals_module.catalog_api,
        "find_provider_by_slug",
        lambda name, base_url="": CATALOG_PROVIDER if name == "catalog-gateway" else None,
    )
    return "catalog-gateway"


def compatibility_labels(picker: SelectionModal) -> list[str]:
    return [row.label.plain.strip().removeprefix("● ") for row in picker.list.children]


def test_the_compatibility_row_shows_the_saved_dialect(dialect_provider: str, session: str) -> None:
    settings_store.update(provider=dialect_provider)
    popup = open_popup(FakeApp(), session)

    assert "compatibility" in fields(popup)
    assert popup.row_text("compatibility") == "Compatibility: openai"


def test_the_compatibility_picker_changes_format_and_url_without_touching_the_trio(
    dialect_provider: str, session: str
) -> None:
    settings_store.update(provider=dialect_provider)
    app = FakeApp()
    popup = open_popup(app, session)
    pin_before = session_config.read(session)

    async def body() -> None:
        popup.row("compatibility").on_click()
        await settle()
        assert isinstance(app.screen, SelectionModal)
        # modelslist's advertised order, the unsupported format dropped.
        assert compatibility_labels(app.screen) == [
            "openai — https://gateway.example/v1",
            "anthropic — https://gateway.example/anthropic",
            "openai-responses — https://gateway.example/v1/responses",
        ]
        selected = next(
            label for label in compatibility_labels(app.screen) if label.startswith("anthropic ")
        )
        app.screen.dismiss(selected)
        await settle()

    run(body)

    record = provider_store.get(dialect_provider)
    assert record.catalog_dialect == "anthropic"
    assert record.api_format == "anthropic"
    assert record.base_url == "https://gateway.example/anthropic"
    assert popup.row_text("compatibility") == "Compatibility: anthropic"
    assert session_config.read(session) == pin_before


def test_an_inheriting_subagent_popup_edits_the_effective_outo_compatibility(
    dialect_provider: str, session: str
) -> None:
    settings_store.save(
        replace(
            settings_store.load(),
            provider=dialect_provider,
            subagent_provider="",
            subagent_model="",
            subagent_reasoning="",
        )
    )
    app = FakeApp()
    popup = open_popup(app, session, config_module.SUBAGENT_TARGET)
    assert popup.row_text("provider") == "Provider: same as outo"

    async def body() -> None:
        popup.row("compatibility").on_click()
        await settle()
        assert isinstance(app.screen, SelectionModal)
        selected = next(
            label
            for label in compatibility_labels(app.screen)
            if label.startswith("openai-responses ")
        )
        app.screen.dismiss(selected)
        await settle()

    run(body)

    record = provider_store.get(dialect_provider)
    assert record.catalog_dialect == "openai-responses"
    assert record.api_format == "openai-response"
    assert settings_store.load().subagent_provider == ""


def test_an_unsaved_dialect_stays_selectable_as_the_saved_entry(
    dialect_provider: str, session: str
) -> None:
    provider_store.upsert(
        replace(
            provider_store.get(dialect_provider),
            catalog_dialect="legacy-dialect",
            base_url="https://hand-edited.example/v1",
        )
    )
    settings_store.update(provider=dialect_provider)
    app = FakeApp()
    popup = open_popup(app, session)

    async def body() -> None:
        popup.row("compatibility").on_click()
        await settle()
        labels = compatibility_labels(app.screen)
        # The hand-edited endpoint is the pre-selected row and is NOT rewritten
        # merely because the popup was opened.
        assert labels[0] == "legacy-dialect (saved) — https://hand-edited.example/v1"
        assert app.screen.index == 0
        app.screen.dismiss(None)
        await settle()

    run(body)

    record = provider_store.get(dialect_provider)
    assert record.catalog_dialect == "legacy-dialect"
    assert record.base_url == "https://hand-edited.example/v1"


# ── the subagent trio: "same as outo" is a stored EMPTY value ───────────────


def test_the_subagent_trio_persists_through_its_own_keys(settings_home: str, session: str) -> None:
    settings_store.update(provider="pop-beta")
    app = FakeApp()
    popup = open_popup(app, session, config_module.SUBAGENT_TARGET)
    assert popup.row_text("provider") == "Provider: same as outo"

    popup.row("provider").on_click()
    assert isinstance(app.screen, SelectionModal)
    app.screen.dismiss("pop-alpha")

    assert popup.row_text("model") == "Model: alpha-default"
    saved = settings_store.load()
    assert saved.subagent_provider == "pop-alpha"
    assert saved.subagent_model == "alpha-default"
    pinned = session_config.effective(session)
    assert pinned.subagent_provider == "pop-alpha"
    # outo's own trio is untouched by a subagent edit.
    assert (pinned.provider, pinned.model) == ("", "")


def test_clearing_the_subagent_reasoning_writes_empty_but_keeps_the_rest(
    settings_home: str, session: str
) -> None:
    settings_store.update(subagent_provider="pop-alpha", subagent_reasoning="high")
    app = FakeApp()
    popup = open_popup(app, session, config_module.SUBAGENT_TARGET)

    popup.row("reasoning").on_click()
    picker = app.screen
    assert isinstance(picker, SelectionModal)
    labels = [row.label.plain for row in picker.list.children]
    assert "  same as outo" in labels
    picker.dismiss(config_module.SENTINEL_SAME_AS_OUTO)

    pinned = session_config.effective(session)
    assert pinned.subagent_reasoning == ""
    assert pinned.subagent_provider == "pop-alpha"
    assert settings_store.load().subagent_reasoning == ""


def test_the_outo_reasoning_picker_has_no_same_as_outo_entry(
    settings_home: str, session: str
) -> None:
    settings_store.update(provider="pop-alpha")
    app = FakeApp()
    popup = open_popup(app, session)

    popup.row("reasoning").on_click()

    labels = [row.label.plain for row in app.screen.list.children]
    assert not any("same as outo" in label for label in labels)


def test_an_outo_edit_never_disturbs_a_configured_subagent(
    settings_home: str, session: str
) -> None:
    settings_store.save(
        replace(
            settings_store.load(),
            provider="pop-beta",
            subagent_provider="pop-alpha",
            subagent_model="alpha-fast",
            subagent_reasoning="low",
        )
    )
    app = FakeApp()
    popup = open_popup(app, session)

    popup.row("provider").on_click()
    app.screen.dismiss("pop-alpha")

    saved = settings_store.load()
    assert saved.provider == "pop-alpha"
    assert (saved.subagent_provider, saved.subagent_model, saved.subagent_reasoning) == (
        "pop-alpha",
        "alpha-fast",
        "low",
    )


# ── the provider picker with an empty registry (issue 1) ────────────────────


def test_the_provider_picker_opens_with_no_provider_configured(session: str) -> None:
    for name in provider_store.load_all():
        provider_store.remove(name)
    # `update` treats "" as "no change" (a provider edit is a PAIR edit), so a
    # test that needs NO provider has to save the record it wants explicitly.
    settings_store.save(replace(settings_store.load(), provider="", model=""))
    app = FakeApp()
    popup = open_popup(app, session)

    popup.row("provider").on_click()

    picker = app.screen
    assert isinstance(picker, SelectionModal), "the picker did not open"
    assert picker.title_text == "No providers yet — add one"
    labels = [item.label.plain for item in picker.query(ListItem)]
    assert any("add from catalog" in label for label in labels), labels
    assert any("add custom" in label for label in labels), labels


def test_the_add_provider_entries_return_their_sentinels(
    settings_home: str, session: str
) -> None:
    calls: list[str] = []

    class AddFlowApp(FakeApp):
        async def _catalog_add_flow(self) -> None:
            calls.append("catalog")

        def _open_custom_add_wizard(self) -> None:
            calls.append("custom")

    settings_store.update(provider="pop-alpha")
    app = AddFlowApp()
    popup = open_popup(app, session)

    popup.row("provider").on_click()
    picker = app.screen
    assert isinstance(picker, SelectionModal)
    picker.select(config_module.SENTINEL_CATALOG_ADD)
    assert picker.highlighted_value() == config_module.SENTINEL_CATALOG_ADD

    # A sentinel is NOT a provider: the popup routes it to the app's add flow
    # and leaves the trio exactly as it was.
    async def body() -> None:
        picker.handle_key(Key("Enter"))
        await settle()

    run(body)

    assert calls == ["catalog"]
    assert popup.row_text("provider") == "Provider: pop-alpha"

    custom_app = AddFlowApp()
    custom_popup = open_popup(custom_app, session)
    custom_popup.row("provider").on_click()
    custom_popup_flow = custom_app.screens[-1]
    custom_popup_flow.select(config_module.SENTINEL_CUSTOM_ADD)
    custom_popup_flow.handle_key(Key("Enter"))

    assert calls == ["catalog", "custom"]


# ── SettingRow ──────────────────────────────────────────────────────────────


def test_a_setting_row_renders_field_capitalized_and_value_or_dash() -> None:
    assert SettingRow("provider", "pop-alpha").render().plain == "Provider: pop-alpha"
    assert SettingRow("caps", "- / -").render().plain == "Caps: - / -"
    assert SettingRow("model", "").render().plain == "Model: -"


def test_a_setting_row_activates_only_while_enabled() -> None:
    fired: list[str] = []
    row = SettingRow("provider", "pop-alpha")
    row.on_activate = lambda activated: fired.append(activated.field)

    row.on_click()
    row.key_enter()
    assert fired == ["provider", "provider"]

    disabled = SettingRow("reasoning", "-", enabled=False)
    disabled.on_activate = lambda activated: fired.append(activated.field)
    disabled.on_click()
    disabled.handle_key(Key("Enter"))
    disabled.build_lines(40, MINIOUTO_THEME)
    disabled.handle_mouse(click(disabled))
    assert fired == ["provider", "provider"]
    assert disabled.enabled is False
    assert disabled.has_class("-disabled")


def test_set_value_only_touches_enabled_when_it_is_passed() -> None:
    row = SettingRow("reasoning", "high")

    row.set_value("low")
    assert (row.value, row.enabled) == ("low", True)
    row.set_value("-", enabled=False)
    assert (row.value, row.enabled) == ("-", False)
    assert row.has_class("-disabled")
    row.set_value("low", enabled=True)
    assert not row.has_class("-disabled")


def test_a_setting_row_paints_its_focus_and_hover_tints() -> None:
    row = SettingRow("provider", "pop-alpha")
    plain = row.build_lines(40, MINIOUTO_THEME)[0]
    row.hovered = True
    hovered = row.build_lines(40, MINIOUTO_THEME)[0]
    row.focused = True
    focused = row.build_lines(40, MINIOUTO_THEME)[0]

    assert str(plain[0][1].background) != str(hovered[0][1].background)
    assert str(hovered[0][1].background) != str(focused[0][1].background)
    assert " Provider: pop-alpha" in "".join(text for text, _ in focused)


# ── CommandPalette ──────────────────────────────────────────────────────────


def palette_commands() -> list[SystemCommand]:
    return [
        SystemCommand("New session", "Create a new session", lambda: None),
        SystemCommand("Pick session", "Switch the active session", lambda: None),
        SystemCommand("Configure outo", "Provider → model → reasoning", lambda: None),
        SystemCommand("Configure subagent", "Subagent trio", lambda: None),
        SystemCommand("Pick style", "Switch the active style document", lambda: None),
        SystemCommand("Theme", "Change the current theme", lambda: None),
        SystemCommand("Keys", "Show help for the focused widget", lambda: None),
        SystemCommand("Quit", "Quit the application as soon as possible", lambda: None),
    ]


def test_the_palette_lists_every_command_in_order() -> None:
    palette = CommandPalette(palette_commands())

    assert [command.title for command in palette.results] == [
        "New session",
        "Pick session",
        "Configure outo",
        "Configure subagent",
        "Pick style",
        "Theme",
        "Keys",
        "Quit",
    ]
    assert palette.highlighted().title == "New session"


def test_typing_does_not_filter_the_palette_keeps_the_whole_list() -> None:
    palette = CommandPalette(palette_commands())

    for char in "theme":
        palette.handle_key(Key(char))

    # There is no search box, so typing changes nothing: every command stays.
    assert [command.title for command in palette.results] == [
        "New session",
        "Pick session",
        "Configure outo",
        "Configure subagent",
        "Pick style",
        "Theme",
        "Keys",
        "Quit",
    ]
    assert palette.highlighted().title == "New session"


def test_an_empty_command_list_leaves_no_highlight() -> None:
    """No query exists any more, so only an empty list deselects."""

    palette = CommandPalette([])

    assert palette.results == []
    assert palette.highlighted() is None
    palette.handle_key(Key("Enter"))
    assert palette.dismissed is False  # Enter with nothing selected is inert


def test_up_and_down_move_the_highlight_and_clamp_at_the_ends() -> None:
    palette = CommandPalette(palette_commands())

    press(palette, "Down", "Down")
    assert palette.highlighted().title == "Configure outo"
    press(palette, "Up")
    assert palette.highlighted().title == "Pick session"
    press(palette, "Up", "Up")
    assert palette.highlighted().title == "New session"


def test_enter_runs_the_highlighted_command_and_closes() -> None:
    ran: list[str] = []
    commands = [
        SystemCommand("New session", "Create", lambda: ran.append("new")),
        SystemCommand("Pick session", "Switch", lambda: ran.append("pick")),
    ]
    app = FakeApp()
    palette = CommandPalette(commands)
    app.push_screen(palette)

    press(palette, "Down", "Enter")

    assert ran == ["pick"]
    assert palette.result is not None
    assert palette.result.title == "Pick session"


def test_esc_closes_the_palette_without_running_anything() -> None:
    ran: list[str] = []
    app = FakeApp()
    palette = CommandPalette(
        [SystemCommand("New session", "Create", lambda: ran.append("new"))]
    )
    app.push_screen(palette)

    press(palette, "Esc")

    assert palette.dismissed is True
    assert palette.result is None
    assert ran == []


def test_the_palette_takes_its_items_from_the_apps_system_commands() -> None:
    app = FakeApp()
    app.commands = palette_commands()
    palette = CommandPalette()
    app.push_screen(palette)

    assert [command.title for command in palette.results][:2] == [
        "New session",
        "Pick session",
    ]


def test_the_palette_is_a_centred_dialog_in_the_modal_house_style() -> None:
    """NOT Textual's full-screen overlay: the same card the pickers use."""

    palette = CommandPalette(palette_commands())
    lines = palette.build_lines(SCREEN, MINIOUTO_THEME)
    painted = ["".join(text for text, _ in line) for line in lines]
    top = palette.region.y
    left = palette.region.x
    # The thick `█▀▄` primary border, centred, with a `$surface` body.
    assert painted[top][left] == "█"
    assert painted[top][left + 1] == "▀"
    assert painted[top + palette.region.height - 1][left] == "█"
    assert painted[top + palette.region.height - 1][left + 1] == "▄"
    assert left > 0 and top > 0, "the dialog is not centred"
    # The title is centred inside the border, and there is NO search box.
    title_row = next(line for line in painted if "Menu" in line)
    assert title_row.strip("█ ").strip() == "Menu"
    assert "Search for commands…" not in "".join(painted)
    # The Textual hkey frame (a full-width ▔/▁ rule) is gone, and so is 🔎.
    # The input's tall frame is gone with the search box: no ▔/▁ glyphs at all.
    assert not any(line.startswith("▔") or line.startswith("▁") for line in painted)
    assert "🔎" not in "".join(painted)
    # No numeric prefixes in front of the commands.
    assert not any(line.strip().startswith(("00 ", "01 ")) for line in painted)


def test_the_palette_rows_carry_the_list_surface_and_a_full_highlight() -> None:
    palette = CommandPalette(palette_commands())
    lines = palette.build_lines(SCREEN, MINIOUTO_THEME)
    boost = str(color(MINIOUTO_THEME.boost))
    # The unselected command rows are painted on `$boost` — the list surface the
    # pickers use — so the list reads as a list, not as body text.
    for region in palette._row_regions[1:]:
        row = lines[region.y]
        assert any(boost in str(style.background) for _, style in row), row
    # The highlighted row is solid primary, edge to edge (the pickers' bar).
    first = lines[palette._row_regions[0].y]
    primary = str(color(MINIOUTO_THEME.primary))
    # The dialog's own border/padding runs sit around it; the row's body — the
    # name, its help and the padding out to the border — is one primary bar.
    bar = [style for text, style in first if text.strip() and "█" not in text]
    assert bar and all(primary in str(style.background) for style in bar), first
    # The dialog's █ border cells frame the row, so the name sits after them.
    assert "New session" in "".join(text for text, _ in first)


def test_an_empty_command_list_paints_a_no_commands_row() -> None:
    palette = CommandPalette([])
    painted = [
        "".join(text for text, _ in line)
        for line in palette.build_lines(SCREEN, MINIOUTO_THEME)
    ]

    assert any("no commands" in line for line in painted)


def test_a_click_outside_the_panel_closes_and_a_click_on_a_row_runs_it() -> None:
    ran: list[str] = []
    commands = [
        SystemCommand("New session", "Create", lambda: ran.append("new")),
        SystemCommand("Pick session", "Switch", lambda: ran.append("pick")),
    ]
    app = FakeApp()
    palette = CommandPalette(commands)
    app.push_screen(palette)
    palette.build_lines(SCREEN, MINIOUTO_THEME)

    # Outside the panel (below the frame) closes the palette.
    assert palette.handle_mouse(Mouse("down", 2, SCREEN.height - 1, button=0)) is True
    assert palette.dismissed is True
    assert ran == []

    app2 = FakeApp()
    second = CommandPalette(commands)
    app2.push_screen(second)
    second.build_lines(SCREEN, MINIOUTO_THEME)
    target = second._row_regions[1]
    assert second.handle_mouse(Mouse("down", target.x + 3, target.y, button=0)) is True

    assert ran == ["pick"]
    assert second.result is not None


def test_no_search_remains_so_nothing_is_underlined_in_the_painted_name() -> None:
    """The fuzzy matcher is gone: the whole name is one plain run."""

    palette = CommandPalette([SystemCommand("Theme", "Change the current theme")])
    lines = palette.build_lines(SCREEN, MINIOUTO_THEME)

    underlined = [
        text
        for line in lines
        for text, style in line
        if "UNDERLINED" in str(style)
    ]
    assert "".join(underlined) == ""
    assert "Theme" in "".join(text for line in lines for text, _ in line)


def test_the_palette_scrolls_when_there_are_more_commands_than_rows() -> None:
    commands = [
        SystemCommand(f"Command {index}", f"Help {index}") for index in range(40)
    ]
    palette = CommandPalette(commands)
    area = Rect(0, 0, 100, 20)
    palette.build_lines(area, MINIOUTO_THEME)

    rows = palette.max_result_rows(area)
    assert rows < len(commands)
    presses = rows + 5
    for _ in range(presses):
        palette.handle_key(Key("Down"))
    assert palette.index == presses
    assert palette.scroll == presses - rows + 1
    # The highlight is the LAST visible row of the window.
    visible = palette.commands[palette.scroll : palette.scroll + rows]
    assert palette.highlighted() is visible[-1]


# ── default_system_commands ─────────────────────────────────────────────────


def test_default_system_commands_lists_the_app_actions_without_numbers() -> None:
    class App:
        def _new_session(self) -> None: ...
        def _open_session_picker(self) -> None: ...
        def _configure_outo(self) -> None: ...
        def _open_subagent_editor(self) -> None: ...
        def _open_style_picker(self) -> None: ...
        def action_change_theme(self) -> None: ...
        def action_clear_log(self) -> None: ...
        def action_show_help(self) -> None: ...
        def action_quit(self) -> None: ...

    commands = default_system_commands(App())

    assert [command.title for command in commands] == [
        "New session",
        "Pick session",
        "Configure outo",
        "Configure subagent",
        "Pick style",
        "Theme",
        "Keys",
        "Quit",
    ]
    # No numeric prefixes at all — that was Textual's alphabetical-sort hack.
    assert not any(command.title[:2].isdigit() for command in commands)
    # The three the owner never used are GONE (Clear log stays on Ctrl+L).
    titles = [command.title for command in commands]
    assert "Clear log" not in titles
    assert "Maximize" not in titles
    assert "Screenshot" not in titles
    assert all(callable(command.callback) for command in commands)


def test_a_missing_app_method_becomes_a_none_callback() -> None:
    """The palette is a convenience: an absent action must not crash it."""

    class App:
        def _new_session(self) -> None: ...

    commands = default_system_commands(App())
    assert commands[0].callback is not None
    assert commands[1].callback is None  # `_open_session_picker` is absent
    assert commands[-1].callback is None  # `action_quit` is absent too


# ── modal mouse ownership and scrolling ─────────────────────────────────────


def test_a_click_beside_the_dialog_is_swallowed_not_dispatched() -> None:
    """A Textual ModalScreen covers the terminal, so nothing escapes to the app."""

    modal = SelectionModal("Pick", ["a"], current="a")
    modal.build_lines(SCREEN, MINIOUTO_THEME)

    assert modal.handle_mouse(Mouse("down", 0, 0, button=0)) is True
    assert modal.dismissed is False
    assert modal.result is None


def test_the_wheel_scrolls_the_list_without_moving_the_highlight() -> None:
    options = [f"opt{index:02d}" for index in range(30)]
    modal = SelectionModal("Many", options, current="opt00")
    modal.build_lines(SCREEN, MINIOUTO_THEME)
    rows = modal.list_rows(modal.row_budget(SCREEN))
    target = modal.list.region
    before = modal.list.index

    for _ in range(3):
        modal.handle_mouse(Mouse("wheel_down", target.x + 2, target.y, button=0))
        # A repaint must NOT undo the wheel scroll by re-snapping to the cursor.
        modal.build_lines(SCREEN, MINIOUTO_THEME)

    assert modal.list.scroll == 3
    assert modal.list.index == before
    assert "opt03" in modal.list.window(rows)[0][1].label.plain


def test_the_wheel_stops_at_both_ends_of_the_list() -> None:
    options = [f"opt{index:02d}" for index in range(5)]
    modal = SelectionModal("Few", options, current="opt00")
    modal.build_lines(SCREEN, MINIOUTO_THEME)

    for _ in range(10):
        modal.handle_mouse(Mouse("wheel_down", modal.list.region.x + 1, modal.list.region.y, 0))
    assert modal.list.scroll == 0  # five rows fit: nothing to scroll

    for _ in range(10):
        modal.handle_mouse(Mouse("wheel_up", modal.list.region.x + 1, modal.list.region.y, 0))
    assert modal.list.scroll == 0


def test_escape_is_the_close_key_on_every_modal() -> None:
    for modal in (
        SelectionModal("Pick", ["a"]),
        TextInputModal("Title"),
        CommandPalette(palette_commands()),
    ):
        modal.handle_key(Key("Esc"))
        assert modal.dismissed is True, type(modal).__name__
        assert modal.result is None


def test_a_modal_ignores_keys_it_does_not_own_but_stays_open() -> None:
    modal = SelectionModal("Pick", ["a", "b"], current="a")

    assert modal.handle_key(Key("F5")) is True
    assert modal.dismissed is False


# ─── KeyModal (the palette's Keys entry) ────────────────────────────────────


def test_the_key_modal_lists_the_apps_real_bindings() -> None:
    from miniouto.cli.tui.modals import DEFAULT_KEY_BINDINGS, KeyModal

    modal = KeyModal()
    # Then: every row is a (key, action) pair taken from the real router
    assert modal.title_text == "Keys"
    assert modal.bindings == list(DEFAULT_KEY_BINDINGS)
    keys = [key for key, _ in modal.bindings]
    # And: the bindings the app's handle_key actually answers are all listed
    for expected in ("Ctrl+P", "Ctrl+L", "Esc Esc", "Ctrl+C", "Tab / Shift+Tab",
                     "Enter", "Esc", "PageUp / PageDown", "Home / End"):
        assert expected in keys, expected


def test_the_key_modal_paints_the_house_style_and_closes_on_esc() -> None:
    from miniouto.cli.tui.modals import KeyModal

    app = FakeApp()
    modal = KeyModal()
    app.push_screen(modal)
    modal.build_lines(SCREEN, MINIOUTO_THEME)
    rendered = modal.render().plain
    # Then: the title, every key and the hint are in the dialog
    assert "Keys" in rendered
    for key, _ in modal.bindings:
        assert key in rendered, key
    assert "Esc to close" in rendered
    # And: the dialog is the house box — █ corners on the primary border
    assert modal.region.width == 60
    # And: Esc dismisses (the modal base owns that key)
    assert modal.handle_key(Key("Esc")) is True
    assert modal.dismissed is True


def test_the_key_modal_closes_on_a_click_outside_the_dialog() -> None:
    from miniouto.cli.tui.modals import KeyModal

    app = FakeApp()
    modal = KeyModal()
    app.push_screen(modal)
    modal.build_lines(SCREEN, MINIOUTO_THEME)
    corner = Mouse("down", 2, SCREEN.height - 1, button=0)
    assert modal.handle_mouse(corner) is True
    assert modal.dismissed is True
    # And: a click INSIDE the dialog leaves it open
    second = KeyModal()
    app.push_screen(second)
    second.build_lines(SCREEN, MINIOUTO_THEME)
    inside = Mouse("down", second.region.x + 3, second.region.y + 2, button=0)
    assert second.handle_mouse(inside) is True
    assert second.dismissed is False
