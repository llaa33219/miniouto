"""TUI navigation + rendering: column cap, chip reachability, paging, screen art.

The TUI overhaul split into five user-visible changes, and this file locks the
parts of each that are pure logic or cheaply observable, so a later refactor
cannot quietly undo them:

1. **Column cap** (`logo_columns`) — a wide terminal must not stretch the chat
   column or the logo past it.
2. **Click targets** — a collapsible row is its whole surface: a plain click
   anywhere on it (glyph, header text, expanded body, attached result) toggles
   it, while a drag that selects text never does.
3. **Paging** (`at_bottom`) — the jump button appears exactly when the log is
   away from its tail, with enough tolerance that following the stream never
   makes it flicker.
4. **Screen art** (`art_path_in_result`, `ToolRow._screen_art`) — a Computer
   `screen` result names the `.art` file it saved; the row renders THAT file in
   truecolour, lazily, capped, and with the result's own escape bytes stripped.
5. **Inbox de-duplication** (`ChatTUI._claim_inbox_event`) — a message inserted
   mid-turn is already on screen, so the event the core emits for it must not
   add a second row.

Two regressions are locked the same way, because both were invisible until the
user hit them: the chat column is 150 cells (`CHAT_MAX_WIDTH`, which feeds
`$chat-max-width`), and the four chips stay reachable at ANY terminal width —
a chip pushed past the right edge still renders but is clipped to nothing, which
is how the style and session pickers went missing once the column was capped.
The chips now live in TWO rows, one chip left and one right per row, so the
values stay short enough to render WHOLE: an earlier attempt elided them with
"…" to fit four into one row, and the owner rejected that. So the chip group
locks that all four chips keep a non-zero on-screen region at every width, that
nothing is ever elided, and that a real click on each of the four reaches its
own picker. The same group locks session routing: the pinned
`ChatTUI._session_name`, so a `miniouto chat` run that rewrites
`settings.session` mid-conversation cannot redirect the live TUI to another
session's files — and lose the alarms queued in its inbox.

Several pilot tests boot the real app to prove the pieces are wired, not just
correct in isolation.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from rich.cells import cell_len
from rich.markdown import Markdown
from textual.events import Click, Leave, MouseDown, MouseMove, MouseUp
from textual.selection import SELECT_ALL
from textual.widgets import Button, Label, Static

from miniouto.cli import tui as tui_module
from miniouto.cli.tui import (
    _ART_MAX_ROWS,
    CHAT_MAX_WIDTH,
    COLLAPSED_GLYPH,
    EXPANDED_GLYPH,
    GLYPH_CELLS,
    ChatInput,
    ChatTUI,
    ProviderSettingsPopup,
    SelectionModal,
    SettingRow,
    TextInputModal,
    ThinkingRow,
    ToolRow,
    art_path_in_result,
    at_bottom,
    logo_columns,
)
from miniouto.core import context as context_api
from miniouto.core import inbox
from miniouto.core.events import LoopEvent
from miniouto.core.summary import Handoff
from miniouto.storage import providers as provider_store
from miniouto.storage import session_config
from miniouto.storage import sessions as session_store
from miniouto.storage import settings as settings_store
from miniouto.tools import ascii_art

ART_PATH = "/home/u/.miniouto/computer-screens/screen1-20260101-120000.art"

# Terminal widths the two chip rows must survive: a cramped phone-width window,
# the common 80/100, the 150 cap, and terminals far wider than the cap.
CHIP_WIDTHS = (40, 80, 100, 120, 150, 200, 250)
# From 80 cells up both chips of a row fit whole, so nothing may be cut there.
WIDE_WIDTHS = (80, 100, 120, 150, 200, 250)

# Chip values as a live session produces them: a model plus its reasoning
# effort on the two left chips, a style name and a pinned session name on the
# two right ones.
CHIP_VALUES = {
    "outo": "gpt-5.5 high",
    "subagent": "claude-opus-4-5 low",
    "style": "coding-work",
    "session": "tui-20260101-120000-abc123",
}


def _run(body: Any) -> None:
    asyncio.run(body())


def _in_app(body: Any) -> None:
    """Run `body(app, pilot)` inside a booted app.

    Collapsible rows read `app.current_theme` while rendering, so a row that
    is exercised outside a running app cannot paint itself. Booting the real
    app keeps these tests on the same path production uses. `body` may be a
    coroutine function when it has to mount a row before inspecting it.
    """

    async def main() -> None:
        app = ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            result = body(app, pilot)
            if inspect.isawaitable(result):
                await result

    _run(main)


def _art_body(rows: int = 60) -> str:
    return "\n".join(
        f"\x1b[38;2;{i % 255};{(i * 3) % 255};{(i * 7) % 255}m\x1b[48;2;10;20;30m{'▀' * 20}"
        for i in range(rows)
    )


def _write_art(directory: Path, rows: int = 60) -> Path:
    path = directory / "screen1.art"
    meta = {
        "screen": "screen1",
        "captured": "2026-01-01T12:00:00",
        "source": "screen1",
        "grid": f"20x{rows}",
        "cell": "8x8",
        "mode": ascii_art.MODE_COLOR,
    }
    path.write_text(ascii_art.build_art(meta, _art_body(rows)), encoding="utf-8")
    return path


def _computer_result(path: Path | str, *, rows_shown: int = 24) -> str:
    """The result string `tools/computer.py` actually builds."""

    return (
        "Screen art captured (screen1 -> 20x60 chars, halfblock truecolor).\n"
        f"Saved to: {path}\n"
        "Click coordinates still come from the screenshot action.\n"
        f"Full art: read that file with `cat`. Preview (first {rows_shown} rows):\n"
        + "\n".join(_art_body().split("\n")[:rows_shown])
    )


def _screenshot_result(path: Path | str, *, size: str = "1280x720") -> str:
    """The result a `screenshot` produces: a summary text block ending in the
    same `Saved to:` line, plus an image block the text flattening drops."""

    return (
        f"Screenshot of the virtual display ({size} pixels). "
        "Click coordinates for the next action refer to this image; "
        "(0, 0) is the top-left corner.\n"
        f"Saved to: {path}"
    )


def _click(row: Any, y: int) -> None:
    row.on_click(Click(row, 2, y, 0, 0, 1, False, False, False))


def _post_mouse(app: Any, event_cls: Any, row: Any, dx: int, dy: int) -> None:
    """Post one mouse event at a row-relative offset, the way a driver does.

    `x`/`y` must be SCREEN coordinates: `App.on_event` reads them to resolve
    the widget under the pointer, and the screen then re-derives the
    widget-relative `offset` the rows act on. `pilot` only ships
    down/up/click, so the drag half of a gesture is spelled out here.
    """
    screen_x = row.region.x + dx
    screen_y = row.region.y + dy
    app.post_message(
        event_cls(
            row, screen_x, screen_y, 0, 0, 1, False, False, False,
            screen_x=screen_x, screen_y=screen_y,
        )
    )


async def _press(app: Any, pilot: Any, row: Any, dx: int, dy: int) -> None:
    _post_mouse(app, MouseMove, row, dx, dy)
    _post_mouse(app, MouseDown, row, dx, dy)
    await pilot.pause()


async def _drag_to(app: Any, pilot: Any, row: Any, dx: int, dy: int) -> None:
    _post_mouse(app, MouseMove, row, dx, dy)
    await pilot.pause()


async def _release(app: Any, pilot: Any, row: Any, dx: int, dy: int) -> None:
    _post_mouse(app, MouseUp, row, dx, dy)
    await pilot.pause()


async def _plain_click(app: Any, pilot: Any, row: Any, dx: int, dy: int) -> None:
    """Down and up on the same cell with no movement between: the plain click."""
    await _press(app, pilot, row, dx, dy)
    await _release(app, pilot, row, dx, dy)
    await pilot.pause()


def _selected_text(app: Any, row: Any) -> str | None:
    """What the screen's selection currently holds for `row`, via `get_selection`."""
    selection = app.screen.selections.get(row)
    if selection is None:
        return None
    selected = row.get_selection(selection)
    return selected[0] if selected else None


def _stub_run_chat(monkeypatch: pytest.MonkeyPatch, sessions: list[str]) -> None:
    """Stand in for the turn driver, recording the session each turn routed on."""

    def fake_run_chat(opts: Any, sink: Any = None) -> str:
        sessions.append(opts.session)
        return ""

    monkeypatch.setattr(tui_module, "run_chat", fake_run_chat)


def _fill_chips(app: ChatTUI) -> None:
    """Give the four chips the values a live session produces."""

    for kind, value in CHIP_VALUES.items():
        app._panel.set_value(kind, value)


def _pin_test_session(app: ChatTUI, name: str = "tui-chip-fixture") -> str:
    """Pin a session of this test's own, so the chips read a config no other
    test can have frozen.

    The publish mirrors what a real session event does: the "active session"
    is the provider-config seed a mint copies from, so a pin that stayed
    unpublished would seed its continuations from a stale shared name.
    """

    app._session_name = name
    settings_store.update(session=name)
    session_config.clear(name)
    app._chat_started = True
    return name


def _painted_line(app: ChatTUI, y: int) -> str:
    """The composited screen line `y`, i.e. what the terminal actually gets.

    Reading the compositor rather than the widget is what makes this an
    assertion about the DISPLAY: it accounts for the crop to the screen edge
    and for any ellipsis the widget style would add.
    """

    return app.screen._compositor.render_strips()[y].text


@pytest.fixture
def chip_source() -> Any:
    """Point the chips at two known providers, then undo every change.

    `chip-source` carries a `reasoning_effort` and `chip-plain` does not, and
    the subagent overrides start cleared — so one fixture covers the outo
    chip's configured/unset reasoning split and the "same as outo" wording.
    """

    for name, effort in (("chip-source", "high"), ("chip-plain", None)):
        provider_store.upsert(
            provider_store.Provider(
                name=name,
                api_format="openai",
                default_model="gpt-5.5",
                reasoning_effort=effort,
            )
        )
    saved = settings_store.load()
    settings_store.save(
        replace(
            saved,
            provider="chip-source",
            subagent_provider="",
            subagent_model="",
            subagent_reasoning="",
        )
    )
    yield "chip-source"
    provider_store.remove("chip-source")
    provider_store.remove("chip-plain")
    settings_store.save(saved)


@pytest.fixture
def one_provider() -> str:
    """A provider for the outo/subagent pickers to offer (both list them)."""

    name = "chip-click-target"
    provider_store.upsert(
        provider_store.Provider(name=name, api_format="openai", default_model="m")
    )
    yield name
    provider_store.remove(name)


# ── 12b: the chat column cap ─────────────────────────────────────────────────


def test_the_chat_column_is_150_cells_wide() -> None:
    # Given / When / Then: the reported ask was 1.5x the previous 100-cell cap.
    # This one number feeds $chat-max-width, so it caps the log, the input AND
    # the chips row — it is the single source of the column width.
    assert CHAT_MAX_WIDTH == 150


def test_logo_columns_caps_a_terminal_wider_than_the_column() -> None:
    # Given: a log wider than the column on a very wide terminal
    # When: the logo width is resolved
    # Then: it stops at the column cap, so the logo cannot overflow the log
    assert logo_columns(CHAT_MAX_WIDTH + 40, 400) == CHAT_MAX_WIDTH


def test_logo_columns_falls_back_to_the_app_width_before_layout() -> None:
    # Given: the log has not been laid out yet (width 0) on a wide terminal
    # When: the logo width is resolved
    # Then: the app width stands in and is still capped
    assert logo_columns(0, 400) == CHAT_MAX_WIDTH


def test_logo_columns_uses_the_column_on_a_narrow_terminal() -> None:
    # Given / When: a log narrower than the cap on a wider terminal
    # Then: the column wins, so the logo follows the log it is drawn inside
    assert logo_columns(64, 200) == 64


# ── 12f: the four chips stay reachable, and are never elided ─────────────────


def test_the_chips_sit_in_two_rows_anchored_left_and_right() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _fill_chips(app)
            await pilot.pause()
            # Given: the panel's two chip rows, each with a `1fr` spacer
            row_a = app.query_one("#chip-row-a").region
            row_b = app.query_one("#chip-row-b").region
            # Then: the rows are separate lines, so no row asks for four chips
            assert row_a.y != row_b.y, f"both chip rows on line {row_a.y}"
            for row, (left_kind, right_kind) in (
                (row_a, ("outo", "style")),
                (row_b, ("subagent", "session")),
            ):
                left = app._panel._chips[left_kind].region
                right = app._panel._chips[right_kind].region
                # And each row hugs its left edge with one chip and its right
                # edge with the other (the 1-cell StatusChip margin-right)
                assert left.x == row.x, f"{left_kind} is not left-anchored: {left} in {row}"
                assert right.x + right.width == row.x + row.width - 1, (
                    f"{right_kind} is not right-anchored: {right} in {row}"
                )

    asyncio.run(body())


@pytest.mark.parametrize("width", CHIP_WIDTHS)
def test_no_chip_is_pushed_off_screen_at_any_width(width: int) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            _fill_chips(app)
            await pilot.pause()
            # Given / When: the four chips carry real session values
            for kind, chip in app._panel._chips.items():
                region = chip.region
                # Then: the chip still occupies screen cells, so a click can
                # reach it — a chip whose whole region sat past the right edge
                # was how the style and session pickers went missing
                assert region.width > 0, f"{kind} collapsed to nothing at {width} cols"
                on_screen = min(region.x + region.width, app.size.width) - max(region.x, 0)
                assert on_screen > 0, f"{kind} pushed off screen at {width} cols: {region}"

    asyncio.run(body())


@pytest.mark.parametrize("width", CHIP_WIDTHS)
def test_no_chip_line_carries_an_ellipsis_at_any_width(width: int) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            _fill_chips(app)
            await pilot.pause()
            # Given / When: the two chip rows are composited to the screen
            for row_id in ("chip-row-a", "chip-row-b"):
                line = _painted_line(app, app.query_one(f"#{row_id}").region.y)
                # Then: nothing on the row is elided with "…" at any width —
                # the owner's explicit rejection of the one-row ellipsis fit
                assert "…" not in line, f"{row_id} elides at {width} cols: {line!r}"

    asyncio.run(body())


@pytest.mark.parametrize("width", WIDE_WIDTHS)
def test_chip_values_render_whole_from_80_cells_up(width: int) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            _fill_chips(app)
            await pilot.pause()
            # Given / When: the chips carry values wider than a 40-cell row
            for kind, chip in app._panel._chips.items():
                plain = chip.render().plain
                line = _painted_line(app, chip.region.y)
                # Then: the chip's box is inside the screen and at least as wide
                # as its value, and the value is on screen in full
                assert chip.region.x >= 0, f"{kind} starts off screen: {chip.region}"
                assert chip.region.x + chip.region.width <= app.size.width, (
                    f"{kind} runs off screen at {width} cols: {chip.region}"
                )
                assert chip.region.width >= len(plain), (
                    f"{kind} is {chip.region.width} cells for a {len(plain)}-cell value"
                )
                assert plain in line, f"{kind} is not fully painted: {line!r}"

    asyncio.run(body())


@pytest.mark.parametrize("width", CHIP_WIDTHS)
@pytest.mark.parametrize("kind", sorted(CHIP_VALUES))
def test_a_click_on_each_chip_opens_its_picker_at_any_width(
    width: int,
    kind: str,
    one_provider: str,
) -> None:
    # The two provider chips open the settings POPUP (one screen, three rows);
    # style and session open a flat SelectionModal.
    expected = (
        (ProviderSettingsPopup, "#provider-settings-dialog")
        if kind in ("outo", "subagent")
        else (SelectionModal, "#selection-dialog")
    )

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            _fill_chips(app)
            await pilot.pause()
            # Given: the chip the click handler routes to a picker. The click
            # lands on the centre of the part of it that is on screen: at 40
            # cells a row cannot hold both chips whole, and a click past the
            # right edge reaches nothing.
            chip = app.query_one(f"#chip-{kind}")
            region = chip.region
            left = max(region.x, 0)
            right = min(region.x + region.width, app.size.width)
            # When: it is really clicked
            await pilot.click(offset=(left + (right - left) // 2, region.y))
            await pilot.pause()
            # Then: its picker/modal is on screen with a non-zero dialog
            screen_cls, dialog_id = expected
            assert isinstance(app.screen, screen_cls), f"{kind} opened no picker"
            dialog = app.screen.query_one(dialog_id).region
            assert dialog.width > 0 and dialog.height > 0, kind

    asyncio.run(body())


def test_the_outo_chip_names_the_active_model_and_its_reasoning_level(
    chip_source: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            # Given / When: the chip is rebuilt from the active provider
            app._refresh_chips()
            await pilot.pause()
            plain = app._panel.get_chip("outo").render().plain
            # Then: it names the model AND its configured reasoning effort,
            # with no "reasoning:" prefix bloating the value
            assert plain.startswith("outo: ")
            assert "gpt-5.5" in plain
            assert plain.endswith(" high")

    asyncio.run(body())


def test_the_outo_chip_marks_an_unset_reasoning_level_with_a_dash(
    chip_source: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            # Given: the active provider carries no reasoning_effort
            settings_store.update(provider="chip-plain")
            # When: the chip is rebuilt
            app._refresh_chips()
            await pilot.pause()
            plain = app._panel.get_chip("outo").render().plain
            # Then: the model is still named and the reasoning slot reads "-"
            assert "gpt-5.5" in plain
            assert plain.endswith(" -"), plain

    asyncio.run(body())


def test_the_subagent_chip_says_same_as_outo_when_nothing_is_overridden(
    chip_source: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            # Given: no subagent_provider / _model / _reasoning is set
            # When: the chip is rebuilt
            app._refresh_chips()
            await pilot.pause()
            # Then: it mirrors `miniouto status`'s wording, not a model guess
            plain = app._panel.get_chip("subagent").render().plain
            assert plain == "subagent: same as outo"

    asyncio.run(body())


def test_the_subagent_chip_names_its_overridden_model_and_reasoning(
    chip_source: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            # Given: an explicit subagent model + reasoning override
            settings_store.update(subagent_model="claude-opus-4-5", subagent_reasoning="low")
            # When: the chip is rebuilt
            app._refresh_chips()
            await pilot.pause()
            plain = app._panel.get_chip("subagent").render().plain
            # Then: it names the override, in the same "<model> <reasoning>" shape
            assert plain == "subagent: claude-opus-4-5 low"

    asyncio.run(body())


def test_the_chips_read_through_the_pinned_sessions_own_config(
    chip_source: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            # Given: this session is pinned to its own provider, while the
            # GLOBAL default says something else
            session_config.set_fields(
                app._session_name, provider="chip-plain", model="pinned-model"
            )
            # When: the chip is rebuilt
            app._refresh_chips()
            await pilot.pause()
            # Then: it names the PINNED values — a TUI bound to a session must
            # show that session's config, not whatever the default is now
            assert app._panel.get_chip("outo").render().plain == "outo: pinned-model -"
            # And the global default it diverges from is untouched
            assert settings_store.load().provider == "chip-source"

    asyncio.run(body())


def test_the_session_chip_shows_the_pinned_session_name() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(200, 30)) as pilot:
            await pilot.pause()
            app._session_name = "tui-20260101-120000-abc123"
            app._chat_started = True
            app._refresh_chips()
            await pilot.pause()
            # Given / When: the pinned name is rendered on the session chip
            # Then: the chip names the live session in full — not a stale
            # settings read, and not a truncated one
            assert (
                app.query_one("#chip-session").render().plain
                == "session: tui-20260101-120000-abc123"
            )

    asyncio.run(body())


# ── the provider settings popup: one screen, three rows, two stores ──────────
#
# The chip pair opens ONE popup whose three rows are the trio it edits. Two
# rules make it correct and both fail silently otherwise:
#
# 1. A pick writes BOTH the pinned session's config and the global default, so
#    two processes editing different sessions never redirect each other while a
#    fresh session still picks up the newest choice.
# 2. Picking a PROVIDER resets Model and Reasoning immediately, because those
#    two belong to the provider being left behind — a stale model id would fail
#    the next turn with a provider that has never heard of it.
#
# The Reasoning row is disabled when the model offers no levels, so the row
# says so instead of opening a picker with nothing in it.


class _FakeReasoning:
    """The lma reasoning boundary, stubbed per model."""

    def __init__(self, ladder: dict[str, list[str]], defaults: dict[str, str]) -> None:
        self._ladder = ladder
        self._defaults = defaults

    def reasoning_choices(self, model: str, _provider: str | None) -> list[str] | None:
        return self._ladder.get(model)

    def default_reasoning_choice(self, model: str, _provider: str | None) -> str | None:
        return self._defaults.get(model)


@pytest.fixture
def settings_home(monkeypatch: pytest.MonkeyPatch) -> str:
    """Two CATALOG providers with different defaults, and no lma reasoning except on one.

    `pop-alpha` offers an effort ladder and records `default_model` +
    `reasoning_effort`; `pop-beta` has no reasoning options at all. That split
    is what proves a provider change actually MOVES the other two rows.

    Both are `source="lma"` on purpose: a hand-added provider carries the
    `Caps` row, and these tests are about the trio alone.
    """
    provider_store.upsert(
        provider_store.Provider(
            name="pop-alpha",
            api_format="openai",
            default_model="alpha-default",
            source=provider_store.SOURCE_LMA,
            reasoning_effort="high",
        )
    )
    provider_store.upsert(
        provider_store.Provider(
            name="pop-beta",
            api_format="openai",
            default_model="beta-default",
            source=provider_store.SOURCE_LMA,
        )
    )
    monkeypatch.setattr(
        tui_module,
        "_get_reasoning_api",
        lambda: _FakeReasoning(
            ladder={"alpha-default": ["off", "low", "high"], "alpha-fast": ["off", "low"]},
            defaults={"alpha-default": "low"},
        ),
    )
    yield "pop-alpha"
    provider_store.remove("pop-alpha")
    provider_store.remove("pop-beta")


def _open_popup(app: ChatTUI, chip: str = "outo") -> ProviderSettingsPopup:
    """Open the popup the way the chip's click handler does.

    Dispatches directly because the real click path is what
    `test_both_provider_chips_open_one_settings_popup` drives through the
    pilot; these tests are about what the popup does once open.
    """

    app.on_status_chip_chip_clicked(
        tui_module.StatusChip.ChipClicked(app.query_one(f"#chip-{chip}"))
    )
    screen = app.screen
    assert isinstance(screen, ProviderSettingsPopup), f"{chip} opened no popup"
    return screen


def _row(popup: ProviderSettingsPopup, field: str) -> SettingRow:
    for row in popup.query(SettingRow):
        if row.field == field:
            return row
    raise AssertionError(f"no {field} row")


def _row_text(popup: ProviderSettingsPopup, field: str) -> str:
    return _row(popup, field).render().plain


def _fields(popup: ProviderSettingsPopup) -> list[str]:
    return [row.field for row in popup.query(SettingRow)]


@pytest.mark.parametrize(
    ("chip", "title"),
    [("outo", "outo settings"), ("subagent", "subagent settings")],
)
def test_both_provider_chips_open_one_settings_popup(
    chip: str, title: str, settings_home: str
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            # Given / When: the chip is clicked for real
            target = app.query_one(f"#chip-{chip}")
            await pilot.click(target, offset=(2, 0))
            await pilot.pause()
            # Then: one popup, titled for the trio that chip edits
            assert isinstance(app.screen, ProviderSettingsPopup), f"{chip} opened no popup"
            assert app.screen.query_one("#provider-settings-title").render().plain == title

    asyncio.run(body())


def test_the_popup_lists_the_trio_then_catalog_compatibility_in_order(
    settings_home: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            settings_store.update(provider="pop-alpha")
            # Given / When: the popup is open
            _open_popup(app, chip="outo")
            await pilot.pause()
            popup = app.screen
            assert isinstance(popup, ProviderSettingsPopup)
            # Then: the trio stays first, followed by the catalog endpoint row
            fields = [row.field for row in popup.query(SettingRow)]
            assert fields == ["provider", "model", "reasoning", "compatibility"]
            assert [_row_text(popup, f) for f in fields] == [
                "Provider: pop-alpha",
                "Model: alpha-default",
                "Reasoning: high",
                "Compatibility: native SDK",
            ]

    asyncio.run(body())


def test_each_row_opens_its_own_picker(settings_home: str) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            settings_store.update(provider="pop-alpha")
            for field, expect in (
                ("provider", "#selection-list"),
                ("reasoning", "#selection-list"),
            ):
                # Given: the popup is open and a row is clicked for real
                _open_popup(app, chip="outo")
                await pilot.pause()
                assert isinstance(app.screen, ProviderSettingsPopup)
                row = _row(app.screen, field)
                await pilot.click(row, offset=(2, 0))
                await pilot.pause()
                # Then: that row's own picker is on top of the popup
                assert isinstance(app.screen, SelectionModal), field
                app.screen.query_one(expect)
                app.screen.dismiss(None)
                await pilot.pause()

    asyncio.run(body())


def test_the_model_row_opens_the_model_list(settings_home: str, monkeypatch: Any) -> None:
    async def body() -> None:
        monkeypatch.setattr(
            tui_module.catalog_api,
            "list_models",
            lambda _p: [{"id": "alpha-default"}, {"id": "alpha-fast", "name": "Fast"}],
        )
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            settings_store.update(provider="pop-alpha")
            _open_popup(app, chip="outo")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            # Given / When: the Model row is clicked and its list arrives
            await pilot.click(_row(app.screen, "model"), offset=(2, 0))
            for _ in range(6):
                await pilot.pause()
            # Then: the catalog model list is offered, newest first, with the
            # model in use marked
            assert isinstance(app.screen, SelectionModal)
            options = [
                str(item.query_one(Label).render().plain).strip()
                for item in app.screen.query_one("#selection-list").children
            ]
            assert options[0].endswith("alpha-fast — Fast")
            assert options[1] == "● alpha-default —"

    asyncio.run(body())


def test_picking_a_model_updates_its_row_and_writes_both_stores(
    settings_home: str, monkeypatch: Any
) -> None:
    async def body() -> None:
        monkeypatch.setattr(
            tui_module.catalog_api,
            "list_models",
            lambda _p: [{"id": "alpha-default"}, {"id": "alpha-fast", "name": "Fast"}],
        )
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            session = _pin_test_session(app)
            settings_store.update(provider="pop-alpha")
            _open_popup(app, chip="outo")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            # Given / When: the Model row picks a different model
            await pilot.click(_row(app.screen, "model"), offset=(2, 0))
            for _ in range(6):
                await pilot.pause()
            assert isinstance(app.screen, SelectionModal)
            app.screen.dismiss("alpha-fast — Fast")
            await _settle(pilot)

            # Then: the row says so, and the popup is still standing
            assert _row_text(app.screen, "model") == "Model: alpha-fast"
            assert isinstance(app.screen, ProviderSettingsPopup)
            # And the PIN carries it: this session keeps the model it picked
            pinned = session_config.effective(session)
            assert (pinned.provider, pinned.model) == ("pop-alpha", "alpha-fast")
            # And the GLOBAL default followed, so a fresh session starts here
            assert settings_store.load().provider == "pop-alpha"
            assert provider_store.get("pop-alpha").default_model == "alpha-fast"

    asyncio.run(body())


def test_the_chips_follow_a_pick_without_a_reload(settings_home: str) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            settings_store.update(provider="pop-alpha")
            _open_popup(app, chip="outo")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            # Given / When: a reasoning level is picked in the popup
            await pilot.click(_row(app.screen, "reasoning"), offset=(2, 0))
            await pilot.pause()
            assert isinstance(app.screen, SelectionModal)
            app.screen.dismiss("low")
            await _settle(pilot)
            # Then: the popup row AND the chip behind it both show it
            assert _row_text(app.screen, "reasoning") == "Reasoning: low"
            assert app._panel.get_chip("outo").render().plain == "outo: alpha-default low"

    asyncio.run(body())


def test_changing_the_provider_resets_model_and_reasoning_immediately(
    settings_home: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            session = _pin_test_session(app)
            settings_store.update(provider="pop-alpha")
            _open_popup(app, chip="outo")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            # Given: model and reasoning are alpha's
            assert _row_text(app.screen, "model") == "Model: alpha-default"
            # When: the Provider row picks a provider with its OWN defaults
            await pilot.click(_row(app.screen, "provider"), offset=(2, 0))
            await pilot.pause()
            assert isinstance(app.screen, SelectionModal)
            app.screen.dismiss("pop-beta")
            await pilot.pause()

            # Then: both rows moved to the new provider's defaults at once —
            # a model id from the provider being left behind would fail the
            # next turn with a provider that has never heard of it
            assert _row_text(app.screen, "provider") == "Provider: pop-beta"
            assert _row_text(app.screen, "model") == "Model: beta-default"
            assert _row_text(app.screen, "reasoning") == "Reasoning: -"
            # And the pin agrees, key by key
            pinned = session_config.effective(session)
            assert (pinned.provider, pinned.model, pinned.reasoning) == (
                "pop-beta",
                "beta-default",
                "",
            )

    asyncio.run(body())


def test_a_provider_change_takes_the_lma_default_when_the_record_has_none(
    settings_home: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            settings_store.update(provider="pop-alpha")
            # Given: a provider whose record stores no reasoning preference
            provider_store.upsert(
                provider_store.Provider(
                    name="pop-auto", api_format="openai", default_model="alpha-default"
                )
            )
            _open_popup(app, chip="outo")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            # When: it is picked
            await pilot.click(_row(app.screen, "provider"), offset=(2, 0))
            await pilot.pause()
            assert isinstance(app.screen, SelectionModal)
            app.screen.dismiss("pop-auto")
            await pilot.pause()
            # Then: the model's lma default fills the gap the record left
            assert _row_text(app.screen, "reasoning") == "Reasoning: low"
            provider_store.remove("pop-auto")

    asyncio.run(body())


def test_the_reasoning_row_is_disabled_for_a_model_without_levels(
    settings_home: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            settings_store.update(provider="pop-beta")
            _open_popup(app, chip="outo")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            # Given / When / Then: the row is visibly unavailable and inert
            row = _row(app.screen, "reasoning")
            assert row.enabled is False
            assert row.render().plain == "Reasoning: -"
            await pilot.click(row, offset=(2, 0))
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup), "opened a picker anyway"

    asyncio.run(body())


# ── the Caps row: a fourth row, only for a custom provider ───────────────────
#
# Caps (`max_context_window` / `max_output_tokens`) are an ENDPOINT property of
# a hand-added provider record — a catalog provider's caps come from lma, so
# there is nothing to override and the row must not exist at all. Unlike the
# trio rows a caps save writes the record ALONE (never a session pin), and it
# has to drop `core.context`'s per-process cache or this long-lived TUI would
# keep serving the caps lma answered before the edit.


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
def lma_caps(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """The caps lma reports for any (model, provider), as a dict the test edits.

    `core.context` caches that answer for the life of the process, so the cache
    is cleared around the test — the point of the last test below is that the
    POPUP is what clears it.
    """

    answers: dict[str, int] = {}
    monkeypatch.setattr(context_api.lma_api, "get_model", lambda *_a, **_k: dict(answers))
    context_api.clear_model_cache()
    yield answers
    context_api.clear_model_cache()


async def _edit_caps(app: ChatTUI, pilot: Any, context_window: str, output_tokens: str) -> None:
    """Click the Caps row and walk its two prompts, as a user would."""

    assert isinstance(app.screen, ProviderSettingsPopup)
    await pilot.click(_row(app.screen, "caps"), offset=(2, 0))
    await pilot.pause()
    assert isinstance(app.screen, TextInputModal), "caps row opened no editor"
    app.screen.dismiss(context_window)
    await pilot.pause()
    assert isinstance(app.screen, TextInputModal), "no second caps prompt"
    app.screen.dismiss(output_tokens)
    await _settle(pilot)
    assert isinstance(app.screen, ProviderSettingsPopup), "the popup did not survive the edit"


def test_the_caps_row_is_absent_for_a_catalog_provider(
    settings_home: str, custom_provider: str
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            settings_store.update(provider="pop-alpha")
            # Given / When: the popup is open on a `source="lma"` provider
            _open_popup(app, chip="outo")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            # Then: its caps come from lma, so there is nothing to override —
            # the caps row is absent; catalog compatibility is a separate row
            assert _fields(app.screen) == [
                "provider", "model", "reasoning", "compatibility"
            ]

    asyncio.run(body())


def test_the_caps_row_reads_unset_as_a_dash_pair_for_a_custom_provider(
    settings_home: str, custom_provider: str
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            settings_store.update(provider="pop-custom")
            # Given / When: the popup is open on a hand-added provider
            _open_popup(app, chip="outo")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            # Then: the fourth row is there, below Reasoning, and both unset
            # caps read "-" — that state means "follow lma", not "broken"
            assert _fields(app.screen) == ["provider", "model", "reasoning", "caps"]
            assert _row_text(app.screen, "caps") == "Caps: - / -"

    asyncio.run(body())


def test_changing_the_provider_adds_and_removes_the_caps_row(
    settings_home: str, custom_provider: str
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            settings_store.update(provider="pop-custom")
            _open_popup(app, chip="outo")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            assert "caps" in _fields(app.screen)

            # When: the Provider row moves to the catalog provider
            await pilot.click(_row(app.screen, "provider"), offset=(2, 0))
            await pilot.pause()
            assert isinstance(app.screen, SelectionModal)
            app.screen.dismiss("pop-alpha")
            await pilot.pause()

            # Then: Caps is gone with that provider; compatibility takes over
            assert isinstance(app.screen, ProviderSettingsPopup)
            assert _fields(app.screen) == [
                "provider", "model", "reasoning", "compatibility"
            ]

            # And moving back brings it, labelled with THAT record's caps
            provider_store.upsert(
                replace(
                    provider_store.get("pop-custom"),
                    max_context_window=200000,
                    max_output_tokens=16384,
                )
            )
            await pilot.click(_row(app.screen, "provider"), offset=(2, 0))
            await pilot.pause()
            assert isinstance(app.screen, SelectionModal)
            app.screen.dismiss("pop-custom")
            await pilot.pause()

            assert isinstance(app.screen, ProviderSettingsPopup)
            assert _row_text(app.screen, "caps") == "Caps: 200000 / 16384"

    asyncio.run(body())


def test_the_caps_editor_writes_the_provider_record_and_leaves_the_pin_alone(
    settings_home: str, custom_provider: str
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            session = _pin_test_session(app)
            settings_store.update(provider="pop-custom")
            _open_popup(app, chip="outo")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            pin_before = session_config.read(session)

            # Given / When: both caps are typed into the editor
            await _edit_caps(app, pilot, "200000", "16384")

            # Then: the RECORD carries them…
            record = provider_store.get("pop-custom")
            assert (record.max_context_window, record.max_output_tokens) == (200000, 16384)
            # …the row says so…
            assert _row_text(app.screen, "caps") == "Caps: 200000 / 16384"
            # …and the PIN is byte-for-byte what it was: caps are global per
            # provider, so a second session must not inherit one session's edit
            assert session_config.read(session) == pin_before

    asyncio.run(body())


def test_empty_caps_input_clears_the_override_back_to_unset(
    settings_home: str, custom_provider: str
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            provider_store.upsert(
                replace(
                    provider_store.get("pop-custom"),
                    max_context_window=200000,
                    max_output_tokens=16384,
                )
            )
            settings_store.update(provider="pop-custom")
            _open_popup(app, chip="outo")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            assert _row_text(app.screen, "caps") == "Caps: 200000 / 16384"

            # Given / When: both prompts are submitted empty
            await _edit_caps(app, pilot, "", "")

            # Then: the overrides are GONE from the record (not zero, not ""),
            # so the next turn falls back to lma's caps again
            record = provider_store.get("pop-custom")
            assert (record.max_context_window, record.max_output_tokens) == (None, None)
            assert _row_text(app.screen, "caps") == "Caps: - / -"

    asyncio.run(body())


def test_a_caps_save_takes_effect_without_a_restart(
    settings_home: str, custom_provider: str, lma_caps: dict[str, int]
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            settings_store.update(provider="pop-custom")
            lma_caps.update({"context_window": 128000, "max_output_tokens": 4096})
            # Given: lma answered for this provider once, and that answer is
            # now cached for the life of this process
            assert context_api.get_context_window("custom-default", "pop-custom") == 128000

            _open_popup(app, chip="outo")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            # When: the caps editor saves an override
            await _edit_caps(app, pilot, "200000", "16384")
            # Then: the override wins at once, with no restart
            assert context_api.get_context_window("custom-default", "pop-custom") == 200000
            assert context_api.get_max_output_tokens("custom-default", "pop-custom") == 16384

            # When: lma's answer changes and the overrides are cleared again
            lma_caps["context_window"] = 64000
            await _edit_caps(app, pilot, "", "")
            # Then: lma is asked again — a stale `_MODEL_CACHE` entry would
            # still be serving the 128000 this process cached before the edit
            assert context_api.get_context_window("custom-default", "pop-custom") == 64000

    asyncio.run(body())


def test_the_subagent_trio_says_same_as_outo_and_persists_it_through_its_own_keys(
    settings_home: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            session = _pin_test_session(app)
            settings_store.update(provider="pop-alpha")
            # Given: the subagent trio is unset, so it inherits outo
            _open_popup(app, chip="subagent")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            assert _row_text(app.screen, "provider") == "Provider: same as outo"
            # When: its provider is picked
            await pilot.click(_row(app.screen, "provider"), offset=(2, 0))
            await pilot.pause()
            assert isinstance(app.screen, SelectionModal)
            app.screen.dismiss("pop-alpha")
            await pilot.pause()
            # Then: the rows resolve concretely and the write went to the
            # SUBAGENT keys — outo's own trio is untouched
            assert _row_text(app.screen, "model") == "Model: alpha-default"
            saved = settings_store.load()
            assert saved.subagent_provider == "pop-alpha"
            assert saved.subagent_model == "alpha-default"
            pinned = session_config.effective(session)
            assert pinned.subagent_provider == "pop-alpha"
            assert pinned.provider == "" and pinned.model == ""

    asyncio.run(body())


def test_clearing_the_subagent_reasoning_writes_empty_but_keeps_the_rest(
    settings_home: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            session = _pin_test_session(app)
            settings_store.update(
                subagent_provider="pop-alpha", subagent_reasoning="high"
            )
            _open_popup(app, chip="subagent")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            # Given / When: its reasoning picker offers "same as outo"…
            await pilot.click(_row(app.screen, "reasoning"), offset=(2, 0))
            await pilot.pause()
            assert isinstance(app.screen, SelectionModal)
            app.screen.dismiss(tui_module.SENTINEL_SAME_AS_OUTO)
            await _settle(pilot)
            # Then: only that key is cleared — empty means "follow the
            # default", which for the subagent is outo's own reasoning
            pinned = session_config.effective(session)
            assert pinned.subagent_reasoning == ""
            assert pinned.subagent_provider == "pop-alpha"
            assert settings_store.load().subagent_reasoning == ""

    asyncio.run(body())


def test_a_minted_session_inherits_the_config_it_continues_from(
    settings_home: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            previous = _pin_test_session(app, "seed-source")
            session_config.set_fields(
                previous, provider="pop-alpha", model="alpha-default", reasoning="high"
            )
            # Given / When: "new session" mints a fresh name over it
            app._new_session()
            await pilot.pause()
            # Then: the new session starts on the config it continues from,
            # instead of freezing whatever the default is at its first turn
            minted = app._session_name
            assert minted != previous
            inherited = session_config.effective(minted)
            assert (inherited.provider, inherited.model, inherited.reasoning) == (
                "pop-alpha",
                "alpha-default",
                "high",
            )

    asyncio.run(body())


def test_the_first_dispatched_session_inherits_the_config_it_continues_from(
    monkeypatch: pytest.MonkeyPatch, settings_home: str
) -> None:
    async def body() -> None:
        routed: list[str] = []
        _stub_run_chat(monkeypatch, routed)
        # Given: a session the user was last in, with its own pinned config —
        # published BEFORE this TUI mounts, which is when it adopts the name
        settings_store.update(provider="pop-alpha", session="dispatch-source")
        session_config.set_fields(
            "dispatch-source", provider="pop-alpha", model="alpha-fast", reasoning="low"
        )
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # When: the first turn mints this TUI's own session
            await app._dispatch("first prompt")
            # Then: it is seeded from the session whose conversation it continues
            assert routed == [app._session_name]
            assert app._session_name != "dispatch-source"
            seeded = session_config.effective(app._session_name)
            assert (seeded.provider, seeded.model, seeded.reasoning) == (
                "pop-alpha",
                "alpha-fast",
                "low",
            )

    asyncio.run(body())


# ── 12g: session routing is pinned, never re-read from settings ──────────────


def test_an_external_settings_session_write_cannot_redirect_the_live_tui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def body() -> None:
        routed: list[str] = []
        _stub_run_chat(monkeypatch, routed)
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Given: a live TUI whose first turn has pinned its own session
            await app._dispatch("first prompt")
            pinned = app._session_name
            assert pinned is not None and routed == [pinned]
            # When: another process (a delegated `miniouto chat` run) hijacks
            # the shared settings.session mid-conversation
            settings_store.update(session="hijacked-by-another-process")
            # Then: neither the drained inbox nor the next turn follows it
            assert app._inbox_session() == pinned
            await app._dispatch("second prompt")
            assert routed == [pinned, pinned]
            assert app._session_name == pinned

    asyncio.run(body())


def test_the_minted_session_keeps_the_tui_name_format_and_is_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def body() -> None:
        routed: list[str] = []
        _stub_run_chat(monkeypatch, routed)
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Given / When: the very first turn of a fresh TUI mints a session
            await app._dispatch("first prompt")
            # Then: the pin keeps the `tui-<ts>-<hex>` mint format, and the
            # "active session" convention still publishes it to settings
            assert re.fullmatch(r"tui-\d{8}-\d{6}-[0-9a-f]{6}", app._session_name)
            assert settings_store.load().session == app._session_name

    asyncio.run(body())


def test_the_session_picker_pins_and_publishes_the_chosen_session() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            session_store.create("picked-session")
            # Given: the user picks a different session from the picker
            app._open_session_picker()
            await pilot.pause()
            assert isinstance(app.screen, SelectionModal)
            app.screen.dismiss("picked-session")
            await pilot.pause()
            # Then: the pin and the published active session both follow it
            assert app._session_name == "picked-session"
            assert settings_store.load().session == "picked-session"
            assert app._inbox_session() == "picked-session"

    asyncio.run(body())


def test_new_session_pins_and_publishes_the_new_name() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Given / When: "new session" mints a fresh one
            app._new_session()
            await pilot.pause()
            # Then: it is pinned, published, and in the mint format
            assert re.fullmatch(r"tui-\d{8}-\d{6}-[0-9a-f]{6}", app._session_name)
            assert settings_store.load().session == app._session_name
            assert app._inbox_session() == app._session_name

    asyncio.run(body())


def test_a_session_less_tui_never_adopts_the_published_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def body() -> None:
        routed: list[str] = []
        _stub_run_chat(monkeypatch, routed)
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            # Given: another live TUI published its own session as the active one
            settings_store.update(session="other-live-session")
            await pilot.pause()
            # Then: the session-less TUI uses its private pending inbox, not
            # the published active session or the shared `default` inbox.
            assert app._inbox_session().startswith("tui-pending-")
            assert app._inbox_session() != "other-live-session"
            # And: the first turn still mints this TUI's own session, seeded
            # from the published provider config as before
            await app._dispatch("first prompt")
            assert re.fullmatch(r"tui-\d{8}-\d{6}-[0-9a-f]{6}", app._session_name)
            assert routed == [app._session_name]

    asyncio.run(body())


def test_an_idle_session_less_tui_never_steals_another_sessions_alarms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def body() -> None:
        prompts: list[str] = []
        _stub_run_chat(monkeypatch, prompts)
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            # Given: TUI A published its session and has an alarm waiting in it,
            # while this TUI (TUI B) is mounted with no session of its own
            settings_store.update(session="session-a")
            inbox.insert(
                "session-a",
                f"{inbox.ALARM_HEADER}\nresult ready",
                kind=inbox.ALARM_KIND,
                source="spawn",
            )
            await pilot.pause()
            # When: B's idle poll fires
            app._deliver_pending_messages()
            await pilot.pause()
            # Then: A's alarm is still in A's inbox and B started no turn
            assert inbox.pending_count("session-a") == 1
            assert prompts == []
            assert not app._chat_started

    asyncio.run(body())


# ── 12c: a plain click toggles a collapsible row anywhere; a drag never does ───


def test_a_plain_click_on_the_glyph_toggles_a_tool_row() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: a mounted, collapsed tool row (so its gutter is the real one)
        row = ToolRow("Bash ls -la")
        await app._log.mount(row)
        await pilot.pause()
        # When: down + up on the glyph cell, with no movement between
        await _plain_click(app, pilot, row, row._glyph_column, 0)
        # Then: the row expands
        assert row._expanded is True

    _in_app(body)


def test_a_plain_click_on_the_header_text_toggles_a_tool_row() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: a mounted, collapsed tool row
        row = ToolRow("Bash ls -la")
        await app._log.mount(row)
        await pilot.pause()
        # When: the click lands on the header text, past the glyph
        await _plain_click(app, pilot, row, row._glyph_column + GLYPH_CELLS + 3, 0)
        # Then: it expands
        assert row._expanded is True

    _in_app(body)


def test_a_second_plain_click_re_collapses_a_tool_row() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: an expanded tool row
        row = ToolRow("Bash ls -la")
        row._toggle()
        await app._log.mount(row)
        await pilot.pause()
        # When: another plain click lands, on a DIFFERENT cell — two clicks on
        # the same cell inside the chain window are a double-click (one toggle)
        await _plain_click(app, pilot, row, row._glyph_column + GLYPH_CELLS + 5, 0)
        # Then: it collapses
        assert row._expanded is False

    _in_app(body)


def test_a_plain_click_on_the_expanded_body_toggles_a_tool_row() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: an EXPANDED tool row holding a command and its output
        row = ToolRow("Bash ls -la", detail="ls -la /tmp")
        row.set_result("hello world")
        row._toggle()
        await app._log.mount(row)
        await pilot.pause()
        # When: a click lands on the command line of the body (line 1)
        await _plain_click(app, pilot, row, row._glyph_column + 2, 1)
        # Then: the row collapses — the body is part of the button
        assert row._expanded is False
        row._toggle()
        await pilot.pause()
        # And: a click on the attached result line does the same
        await _plain_click(app, pilot, row, row._glyph_column + 2, 3)
        assert row._expanded is False

    _in_app(body)


def test_a_plain_click_toggles_a_thinking_row_on_the_header_and_the_body() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: a thinking row holding streamed reasoning
        row = ThinkingRow("some deep reasoning")
        await app._log.mount(row)
        await pilot.pause()
        # When: a plain click lands on the header
        await _plain_click(app, pilot, row, row._glyph_column + GLYPH_CELLS + 2, 0)
        # Then: it expands
        assert row._expanded is True
        await pilot.pause()
        # And: a plain click on the reasoning text itself collapses it again
        await _plain_click(app, pilot, row, row._glyph_column + 2, 1)
        assert row._expanded is False

    _in_app(body)


def test_a_drag_that_selects_text_never_toggles_the_row() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: a mounted, collapsed tool row
        row = ToolRow("Bash ls -la")
        await app._log.mount(row)
        await pilot.pause()
        # When: the pointer presses on the header, drags across it, releases
        await _press(app, pilot, row, row._glyph_column + 1, 0)
        await _drag_to(app, pilot, row, row._glyph_column + 11, 0)
        await _release(app, pilot, row, row._glyph_column + 11, 0)
        await pilot.pause()
        # Then: the row stayed collapsed, and the dragged text is selected
        assert row._expanded is False
        assert _selected_text(app, row) == " Bash ls -l"
        assert app.screen.selections.get(row) is not SELECT_ALL

    _in_app(body)


def test_a_drag_over_the_expanded_body_keeps_the_body_text_selected() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: an EXPANDED tool row, so a command line exists
        row = ToolRow("Bash ls -la", detail="ls -la /tmp")
        row._toggle()
        await app._log.mount(row)
        await pilot.pause()
        # When: the pointer drags across the command line
        await _press(app, pilot, row, row._glyph_column + 1, 1)
        await _drag_to(app, pilot, row, row._glyph_column + 4, 1)
        await _release(app, pilot, row, row._glyph_column + 4, 1)
        await pilot.pause()
        # Then: the row did not toggle, and the drag took exactly that text
        assert row._expanded is True
        assert _selected_text(app, row) == "s -l"
        assert app.screen.selections.get(row) is not SELECT_ALL

    _in_app(body)


def test_a_drag_that_selects_nothing_still_toggles_the_row() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: an EXPANDED thinking row whose middle line is blank, so a drag
        # across it moves the pointer without taking a single character
        row = ThinkingRow("alpha\n\nbeta")
        row._toggle()
        await app._log.mount(row)
        await pilot.pause()
        # When: the pointer drags across the blank line
        await _press(app, pilot, row, row._glyph_column + 1, 2)
        await _drag_to(app, pilot, row, row._glyph_column + 5, 2)
        await _release(app, pilot, row, row._glyph_column + 5, 2)
        await pilot.pause()
        # Then: nothing was selected, so the gesture counts as a plain click
        assert row._expanded is False
        assert not _selected_text(app, row)

    _in_app(body)


def test_a_double_click_selects_no_whole_row_and_toggles_once() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: a mounted tool row
        row = ToolRow("Bash ls -la")
        await app._log.mount(row)
        await pilot.pause()
        whole_row = row.get_selection(SELECT_ALL)[0]
        # When: it is double-clicked
        await pilot.click(row, offset=(row._glyph_column + 3, 0), times=2)
        await pilot.pause()
        # Then: no whole-row selection survives (select-all is off at the source)
        assert app.screen.selections.get(row) is None
        assert _selected_text(app, row) != whole_row
        # And: the second click of the pair is consumed, so the row opened once
        # and did not flicker shut again
        assert row._expanded is True

    _in_app(body)


def test_a_triple_click_never_toggles_the_row() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: a mounted tool row, counting every toggle it receives
        row = ToolRow("Bash ls -la")
        await app._log.mount(row)
        await pilot.pause()
        toggles: list[bool] = []
        real_toggle = ToolRow._toggle
        ToolRow._toggle = lambda self: (toggles.append(True), real_toggle(self))[1]
        try:
            # When: it is triple-clicked
            await pilot.click(row, offset=(row._glyph_column + 3, 0), times=3)
            await pilot.pause()
        finally:
            ToolRow._toggle = real_toggle
        # Then: only the first click of the chain toggles it
        assert len(toggles) == 1

    _in_app(body)


def test_a_thinking_row_double_click_selects_no_whole_row() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: a mounted thinking row
        row = ThinkingRow("deep reasoning text here")
        await app._log.mount(row)
        await pilot.pause()
        whole_row = row.get_selection(SELECT_ALL)[0]
        # When: it is double-clicked
        await pilot.click(row, offset=(row._glyph_column + 3, 0), times=2)
        await pilot.pause()
        # Then: the whole-row selection is gone there too
        assert app.screen.selections.get(row) is not SELECT_ALL
        assert _selected_text(app, row) != whole_row

    _in_app(body)


def test_a_row_refuses_text_select_all() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: a mounted row, which the framework asks to select everything
        # when a click chain reaches 2 (`Widget._on_click` → `text_select_all`)
        row = ToolRow("Bash ls -la")
        await app._log.mount(row)
        await pilot.pause()
        # When: that call is made
        row.text_select_all()
        # Then: nothing is selected — a drag is the only selection a row has
        assert app.screen.selections == {}

    _in_app(body)


def test_a_click_without_a_tracked_press_still_toggles() -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: a row whose gesture tracker never saw a MouseDown (a click
        # delivered straight to the handler, as the framework brokers it)
        row = ToolRow("Bash ls -la")
        # When: it is clicked on the header, and again on the body
        _click(row, 0)
        # Then: both count as plain clicks
        assert row._expanded is True
        _click(row, 3)
        assert row._expanded is False

    _in_app(body)


def test_a_click_on_the_header_toggles_a_thinking_row() -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: a collapsed thinking row
        row = ThinkingRow("some deep reasoning")
        # When: the header is clicked
        _click(row, 0)
        # Then: it expands
        assert row._expanded is True

    _in_app(body)


def test_enter_still_toggles_a_tool_row() -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: a collapsed tool row
        row = ToolRow("Bash ls -la")
        # When: Enter is pressed (keyboard path is unchanged by the click rule)
        row.key_enter()
        # Then: it expands
        assert row._expanded is True

    _in_app(body)


def test_a_collapsed_header_carries_only_the_toggle_glyph() -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: collapsed rows
        tool = ToolRow("Bash ls -la")
        thinking = ThinkingRow("reasoning")
        failed = ToolRow("Bash rm -rf /")
        failed.set_result("error: nope", is_error=True)
        # When: they render their header
        tool._refresh()
        thinking._refresh()
        failed._refresh()
        # Then: the ▶ glyph is the whole affordance — no key hint is printed
        assert tool.render().plain == "▶ Bash ls -la"
        assert thinking.render().plain == "▶ thinking"
        assert failed.render().plain == "▶ Bash rm -rf / ✗"
        for row in (tool, thinking, failed):
            assert "[enter]" not in row.render().plain

    _in_app(body)


# ── 12h: the toggle glyph is big, and hover highlights ONLY the glyph ────────


def _move(row: Any, x: int, y: int) -> None:
    row.on_mouse_move(MouseMove(row, x, y, 0, 0, 0, False, False, False))


def _glyph_segment_style(row: Any) -> str:
    """The style of the toggle glyph's own rendered segment (the header prefix)."""
    glyph_segment = next(iter(row._render_cache.lines[0]))
    assert glyph_segment.text.strip() in (COLLAPSED_GLYPH, EXPANDED_GLYPH)
    return str(glyph_segment.style)


def test_the_toggle_glyph_is_the_big_triangle_pair() -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given / When: a row in each state
        collapsed = ToolRow("Bash ls -la")
        expanded = ToolRow("Bash ls -la")
        expanded._toggle()
        # Then: U+25B6 collapsed / U+25BC expanded, not the old small triangles
        assert collapsed.render().plain.startswith(COLLAPSED_GLYPH)
        assert expanded.render().plain.startswith(EXPANDED_GLYPH)

    _in_app(body)


def test_the_hit_math_uses_the_measured_glyph_width() -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: the chosen glyphs, both East-Asian-Ambiguous
        # Then: the width the hit math uses is the width they really spend
        assert GLYPH_CELLS == cell_len(COLLAPSED_GLYPH) == cell_len(EXPANDED_GLYPH)

    _in_app(body)


def test_hovering_the_glyph_highlights_only_the_glyph() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: a collapsed tool row, mounted so its gutter is the real one
        row = ToolRow("Bash ls -la")
        await app._log.mount(row)
        await pilot.pause()
        resting = _glyph_segment_style(row)
        # When: the pointer sits on the glyph cell
        _move(row, row._glyph_column, 0)
        await pilot.pause()
        # Then: the glyph itself goes reverse-video
        assert row.hover_glyph is True
        highlighted = _glyph_segment_style(row)
        assert "reverse" in highlighted
        assert highlighted != resting
        # And: the header text next to it keeps its resting style
        tail = list(row._render_cache.lines[0])[1]
        assert tail.text.startswith("Bash ls -la")
        assert "reverse" not in str(tail.style)

    _in_app(body)


def test_hovering_the_header_text_leaves_the_row_unchanged() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: a collapsed tool row with the glyph at rest
        row = ToolRow("Bash ls -la")
        await app._log.mount(row)
        await pilot.pause()
        resting = _glyph_segment_style(row)
        # When: the pointer moves onto the header TEXT, one cell past the glyph
        _move(row, row._glyph_column + GLYPH_CELLS, 0)
        await pilot.pause()
        # Then: no highlight — the hit math stops at the measured glyph width
        assert row.hover_glyph is False
        assert _glyph_segment_style(row) == resting
        # And: further along the header it is still untouched
        _move(row, row._glyph_column + GLYPH_CELLS + 2, 0)
        await pilot.pause()
        assert row.hover_glyph is False
        assert _glyph_segment_style(row) == resting

    _in_app(body)


def test_hovering_the_body_leaves_the_row_unchanged() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: an EXPANDED tool row, so a body line exists
        row = ToolRow("Bash ls -la", detail="ls -la /tmp")
        await app._log.mount(row)
        await pilot.pause()
        row._toggle()
        await pilot.pause()
        resting = _glyph_segment_style(row)
        # When: the pointer sits on the body line, at the first column
        _move(row, 0, 1)
        await pilot.pause()
        # Then: nothing highlights
        assert row.hover_glyph is False
        assert _glyph_segment_style(row) == resting

    _in_app(body)


def test_leaving_the_row_reverts_the_glyph_highlight() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: a row whose glyph is highlighted
        row = ToolRow("Bash ls -la")
        await app._log.mount(row)
        await pilot.pause()
        _move(row, row._glyph_column, 0)
        # When: the pointer leaves the row
        row.on_leave(Leave(row))
        await pilot.pause()
        # Then: the highlight is gone
        assert row.hover_glyph is False
        assert "reverse" not in _glyph_segment_style(row)

    _in_app(body)


def test_a_thinking_row_highlights_its_glyph_on_hover() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: a thinking row
        row = ThinkingRow("reasoning")
        await app._log.mount(row)
        await pilot.pause()
        # When: the pointer sits on the glyph cell
        _move(row, row._glyph_column, 0)
        await pilot.pause()
        # Then: it highlights the way a tool row does
        assert row.hover_glyph is True
        assert "reverse" in _glyph_segment_style(row)
        # And: the `thinking` label beside it is untouched
        assert "reverse" not in str(list(row._render_cache.lines[0])[1].style)

    _in_app(body)


def test_neither_collapsible_row_declares_a_hover_background() -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: the compiled CSS of the two collapsible row classes
        # Then: neither has a `:hover` rule — a CSS tint would repaint the
        # header text, the body and the results too, which is the bug itself
        for css in (ToolRow.DEFAULT_CSS, ThinkingRow.DEFAULT_CSS):
            assert ":hover" not in css
        assert ToolRow("Bash ls -la")._has_hover_style is False
        assert ThinkingRow("reasoning")._has_hover_style is False

    _in_app(body)


def test_the_click_scope_is_the_whole_row_not_just_the_header_line() -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: a collapsed row whose glyph is highlighted
        row = ToolRow("Bash ls -la")
        await app._log.mount(row)
        await pilot.pause()
        _move(row, row._glyph_column, 0)
        # When: a click lands on the header TEXT, past the glyph
        _click(row, 0)
        # Then: it toggles — the hover scope never narrowed the click scope
        assert row._expanded is True
        # And: a click on the body toggles too, which is the whole change
        _click(row, 1)
        assert row._expanded is False
        # And: Enter still toggles back
        row.key_enter()
        assert row._expanded is True

    _in_app(body)


# ── 12d: the at-bottom predicate behind the jump button ─────────────────────


class _FakeLog:
    def __init__(self, scroll_y: float, max_scroll_y: float) -> None:
        self.scroll_y = scroll_y
        self.max_scroll_y = max_scroll_y


def test_at_bottom_is_true_within_the_tolerance_band() -> None:
    # Given: a log parked two lines short of its tail
    # Then: it still counts as at the bottom, so following never flickers
    assert at_bottom(_FakeLog(98.0, 100.0)) is True


def test_at_bottom_is_true_exactly_on_the_tail() -> None:
    # Given / When: a log scrolled fully to its end
    # Then: it is at the bottom
    assert at_bottom(_FakeLog(100.0, 100.0)) is True


def test_at_bottom_is_false_above_the_tolerance_band() -> None:
    # Given: a log scrolled well above its tail
    # Then: the jump button must appear
    assert at_bottom(_FakeLog(40.0, 100.0)) is False


def test_at_bottom_is_true_when_the_log_cannot_scroll() -> None:
    # Given: a log with no overflow (max_scroll_y == 0)
    # Then: it is trivially at the bottom and the button stays hidden
    assert at_bottom(_FakeLog(0.0, 0.0)) is True


def test_at_bottom_is_true_for_a_log_that_does_not_exist_yet() -> None:
    # Given: no log mounted yet
    # Then: the safe default is "at the bottom" — the button only ever shows
    # in response to scrolling AWAY
    assert at_bottom(None) is True


# ── 12e: screen art ─────────────────────────────────────────────────────────


def test_the_art_path_is_found_mid_result_like_a_real_computer_result() -> None:
    # Given: the exact shape tools/computer.py returns — "Saved to:" is a
    # middle line, followed by an instruction line and a preview
    result = _computer_result(ART_PATH)
    # When: the path is extracted
    # Then: it is found despite not being the last line
    assert art_path_in_result(result) == ART_PATH


def test_the_art_path_is_found_on_the_last_line() -> None:
    # Given: a result ending with the saved path
    # When: the path is extracted
    # Then: it is found
    assert art_path_in_result(f"done\nSaved to: {ART_PATH}") == ART_PATH


def test_a_path_mentioned_mid_sentence_is_not_an_art_path() -> None:
    # Given: a path echoed inside prose, not on its own "Saved to:" line
    # When: the path is extracted
    # Then: nothing matches, so arbitrary tool output cannot render as art
    assert art_path_in_result(f"the log said Saved to: {ART_PATH} yesterday") is None


def test_no_art_path_is_found_in_a_result_that_saves_nothing() -> None:
    # Given / When / Then: an ordinary result names no art file
    assert art_path_in_result("error: something went wrong") is None


def test_the_saved_line_must_name_an_art_file() -> None:
    # Given: a "Saved to:" line naming some other kind of file
    # When: the path is extracted
    # Then: it does not match — only `.art` is a screen capture
    assert art_path_in_result("Saved to: /tmp/report.txt") is None


def test_the_result_text_carries_no_escape_bytes() -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: a Computer screen result embedding a colourised preview
        row = ToolRow("Computer screen", tool_name="Computer")
        row.set_result(_computer_result(ART_PATH))
        # When: the displayable result is rendered
        # Then: the SGR bytes are gone — raw escapes are unreadable as text
        assert "\x1b" not in row._result_text()

    _in_app(body)


def test_the_result_text_still_carries_the_preview_and_the_path() -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: the same result
        row = ToolRow("Computer screen", tool_name="Computer")
        row.set_result(_computer_result(ART_PATH))
        # When: the displayable result is rendered
        # Then: the human-readable parts survive the strip
        text = row._result_text()
        assert "Click coordinates" in text
        assert ART_PATH in text

    _in_app(body)


def test_a_long_result_is_truncated_at_the_display_cap() -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: a result far past the row's display cap
        row = ToolRow("Bash", tool_name="Bash")
        row.set_result("x" * (ToolRow._MAX_RESULT_CHARS + 500))
        # When: it is rendered
        # Then: it is cut and marked, rather than bloating the layout
        rendered = row._result_text()
        assert len(rendered) < len("x" * (ToolRow._MAX_RESULT_CHARS + 500))
        assert "truncated" in rendered

    _in_app(body)


def test_an_unbounded_result_is_never_truncated() -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: a result the row was told to show in full
        row = ToolRow("answer", unbounded_result=True)
        row.set_result("y" * (ToolRow._MAX_RESULT_CHARS + 500))
        # When: it is rendered
        # Then: nothing is dropped
        assert row._result_text() == "y" * (ToolRow._MAX_RESULT_CHARS + 500)

    _in_app(body)


def test_the_screen_art_is_capped_with_an_overflow_note(tmp_path: Path) -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: a saved art file taller than the inline cap
        path = _write_art(tmp_path, rows=60)
        row = ToolRow("Computer screen", tool_name="Computer")
        row.set_result(_computer_result(path))
        # When: the row expands
        block = row._screen_art()
        assert block is not None
        # Then: only the cap is rendered, and the rest is named in a note that
        # tells the user how to see all of it
        rendered_lines = block.plain.split("\n")
        assert len(rendered_lines) >= _ART_MAX_ROWS
        assert "… 20 more rows" in block.plain
        assert f"miniouto computer show {path}" in block.plain

    _in_app(body)


def test_the_screen_art_keeps_its_truecolour_spans(tmp_path: Path) -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given / When: an art file in half-block truecolour
        path = _write_art(tmp_path, rows=3)
        row = ToolRow("Computer screen", tool_name="Computer")
        row.set_result(_computer_result(path))
        block = row._screen_art()
        # Then: it carries real colour, not grey ramp characters
        assert block is not None
        assert any(getattr(span.style, "color", None) is not None for span in block.spans)

    _in_app(body)


def test_no_screen_art_is_rendered_for_a_tool_that_is_not_computer(
    tmp_path: Path,
) -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: a Bash result that happens to end with the same "Saved to:" line
        path = _write_art(tmp_path, rows=3)
        row = ToolRow("Bash cat", tool_name="Bash")
        row.set_result(f"cat {path}\nSaved to: {path}\n")
        # When: the row expands
        # Then: no art block is rendered — the tool name gates it
        assert row._screen_art() is None

    _in_app(body)


def test_a_screenshot_result_renders_its_art_just_like_a_screen_call(
    tmp_path: Path,
) -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: a `screenshot` result, whose summary ends with the same
        # "Saved to:" line `screen` uses — and a capture on disk
        path = _write_art(tmp_path, rows=60)
        row = ToolRow("Computer screenshot", tool_name="Computer")
        row.set_result(_screenshot_result(path))
        # When: the row expands
        block = row._screen_art()
        # Then: the art renders. The gate is the saved path, never the action:
        # a screenshot the provider could not carry as pixels is exactly the
        # one the user most needs to see.
        assert block is not None
        assert str(path) in block.plain
        assert "… 20 more rows" in block.plain

    _in_app(body)


def test_a_screenshot_result_is_art_lazy_and_capped_like_the_screen_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(app: Any, pilot: Any) -> None:
        # Given: a screenshot row holding a capture taller than the inline cap
        path = _write_art(tmp_path, rows=60)
        row = ToolRow("Computer screenshot", tool_name="Computer")
        row.set_result(_screenshot_result(path))
        await app._log.mount(row)
        await pilot.pause()
        reads: list[str] = []
        original = Path.read_text

        def _tracking_read_text(self: Path, *a: Any, **kw: Any) -> str:
            reads.append(str(self))
            return original(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", _tracking_read_text)
        # When: the collapsed row renders
        row._refresh()
        # Then: nothing was read from disk — a session of screenshots must not
        # touch the filesystem on load
        assert reads == []
        # And once expanded, it reads once and shows the cap plus a note
        row._toggle()
        await pilot.pause()
        assert str(path) in reads
        assert "… 20 more rows" in row.render().plain

    _in_app(body)


def test_a_missing_art_file_renders_nothing_rather_than_raising(
    tmp_path: Path,
) -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: a result pointing at a file that is not there
        row = ToolRow("Computer screen", tool_name="Computer")
        row.set_result(f"Saved to: {tmp_path}/gone.art\n")
        # When: the row expands
        # Then: the row still renders; the art is simply skipped
        assert row._screen_art() is None

    _in_app(body)


def test_a_corrupt_art_file_renders_nothing_rather_than_raising(
    tmp_path: Path,
) -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: a file that exists but is not an art file
        bad = tmp_path / "bad.art"
        bad.write_text("not a screen-art file at all", encoding="utf-8")
        row = ToolRow("Computer screen", tool_name="Computer")
        row.set_result(f"Saved to: {bad}\n")
        # When: the row expands
        # Then: parse_art's refusal is contained and the row still renders
        assert row._screen_art() is None

    _in_app(body)


def test_the_art_file_is_not_read_while_the_row_is_collapsed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    def body(app: Any, pilot: Any) -> None:
        # Given: a row whose result names an art file, still collapsed
        path = _write_art(tmp_path, rows=3)
        row = ToolRow("Computer screen", tool_name="Computer")
        row.set_result(_computer_result(path))
        reads: list[str] = []
        original = Path.read_text

        def _tracking_read_text(self: Path, *a: Any, **kw: Any) -> str:
            reads.append(str(self))
            return original(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", _tracking_read_text)
        # When: the collapsed row renders
        row._refresh()
        # Then: no art file was opened — a long session of screenshots must
        # not hit the filesystem on load
        assert reads == []

    _in_app(body)


# ── 12a: inbox de-duplication ───────────────────────────────────────────────


def test_a_drained_message_already_on_screen_suppresses_its_row() -> None:
    # Given: a message inserted mid-turn, whose row is already displayed
    app = ChatTUI()
    app._optimistic_rows["msg-1"] = Static("> typed while working")
    event = LoopEvent(actor="outo", kind="user", text="typed while working", detail="inbox:msg-1")
    # When: the core's event for it arrives
    claimed = app._claim_inbox_event(event)
    # Then: the row is suppressed and the id is retired
    assert claimed is False
    assert app._optimistic_rows == {}


def test_a_drained_message_that_was_never_shown_is_claimed() -> None:
    # Given: a message inserted by another process, never rendered here
    app = ChatTUI()
    event = LoopEvent(actor="outo", kind="alarm", text="job finished", detail="inbox:msg-2")
    # When: the event arrives
    claimed = app._claim_inbox_event(event)
    # Then: its row renders
    assert claimed is True


def test_an_event_without_an_inbox_detail_is_always_claimed() -> None:
    # Given: a legacy event carrying no inbox detail at all
    app = ChatTUI()
    # When / Then: it renders like any other event
    assert app._claim_inbox_event(LoopEvent(actor="outo", kind="alarm", text="x")) is True


# ── pilot smoke tests ───────────────────────────────────────────────────────


def test_the_app_boots_with_the_jump_button_hidden_at_the_bottom() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            button = app.query_one("#jump-bottom", Button)
            # Given: a fresh app with a short log that cannot overflow
            # When: the button state is evaluated
            app._sync_jump_button()
            await pilot.pause()
            # Then: the button exists and is hidden
            assert button.display is False

    _run(body)


def test_the_jump_button_appears_once_the_log_is_scrolled_away() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            button = app.query_one("#jump-bottom", Button)
            for i in range(80):
                app._mount_row(Static(f"row {i}"))
            await pilot.pause()
            await pilot.pause()
            # Given: a log long enough to scroll, followed to the bottom
            app._input.focus()
            assert button.display is False
            # When: the user pages up from the focused input
            await pilot.press("pageup")
            await pilot.pause()
            # Then: the log moved and the jump button appeared
            assert app._log.scroll_y < app._log.max_scroll_y
            assert button.display is True
            # And when the button is pressed: back at the tail, hidden again,
            # with typing focus restored to the input
            await pilot.click("#jump-bottom")
            await pilot.pause()
            await pilot.pause()
            assert app._log.scroll_y == app._log.max_scroll_y
            assert button.display is False
            assert app.focused is not None and app.focused.id == "input"

    _run(body)


def test_page_down_returns_to_the_tail_without_animating() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for i in range(80):
                app._mount_row(Static(f"row {i}"))
            await pilot.pause()
            await pilot.pause()
            app._input.focus()
            # Given: a long log at the bottom
            bottom = app._log.scroll_y
            # When: the user pages up and back down with the input focused
            await pilot.press("pageup")
            await pilot.pause()
            assert app._log.scroll_y < bottom
            await pilot.press("pagedown")
            await pilot.pause()
            # Then: paging lands instantly on the tail — no smooth scroll,
            # which would leave scroll_y short of max for several frames
            assert app._log.scroll_y == app._log.max_scroll_y

    _run(body)


def test_home_and_end_page_the_log_from_the_focused_input() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for i in range(80):
                app._mount_row(Static(f"row {i}"))
            await pilot.pause()
            await pilot.pause()
            app._input.focus()
            # Given: the input owns home/end for caret movement
            # When: they are pressed
            await pilot.press("end")
            await pilot.pause()
            assert app._log.scroll_y == app._log.max_scroll_y
            await pilot.press("home")
            await pilot.pause()
            # Then: the LOG paged, not the caret
            assert app._log.scroll_y == 0

    _run(body)


def test_a_legacy_subagent_event_replays_as_a_plain_row() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._log.remove_children()
            app._reset_rows()
            # Given: a session recorded before the subagent UI was removed
            app._replay_event(
                LoopEvent(
                    actor="editor-abc123",
                    kind="subagent_start",
                    text="edit the file",
                    subagent_id="abc123",
                    subagent_name="editor",
                )
            )
            app._replay_event(
                LoopEvent(
                    actor="editor-abc123",
                    kind="subagent_end",
                    text="edited 3 files",
                    subagent_id="abc123",
                    subagent_name="editor",
                )
            )
            await pilot.pause()
            # When: the session is replayed
            # Then: both events render as rows — a legacy session reloads
            # without crashing and without the special subagent rendering
            rendered = [w.render().plain for w in app._window.rows]
            assert any("edit the file" in t for t in rendered)
            assert any("edited 3 files" in t for t in rendered)

    _run(body)


# ── 12h: a summary hand-off moves the conversation to a new session ───────────
#
# `core/summary.py` holds the hand-off; the TUI performs it. Two things must
# hold for it to work at all, and both fail silently otherwise: the OLD
# conversation has to visibly END with the marker (in the live log AND on
# disk, or a reload shows a transcript that just stops), and the session pin
# has to move BEFORE `_deliver_pending_messages` runs — that call drains
# `self._inbox_session()`, so a stale pin would drain the old inbox and drop
# the continuation message on the floor.


HANDOFF_SUMMARY_DIR = "/tmp/miniouto-summary/tui-fixture"


class _FakeSummary:
    """The hand-off seam the TUI calls: the pending triple plus the contract slot."""

    def __init__(self) -> None:
        self._pending: Handoff | None = None
        self.contracts: list[dict[str, str]] = []

    def request_handoff(self, new_session: str, *, from_session: str, summary_dir: str) -> None:
        self._pending = Handoff(
            new_session=new_session, from_session=from_session, summary_dir=summary_dir
        )

    def pending_handoff(self) -> Handoff | None:
        return self._pending

    def consume_handoff(self) -> Handoff | None:
        handoff, self._pending = self._pending, None
        return handoff

    def set_run_contract(self, **values: str) -> None:
        self.contracts.append(values)


@pytest.fixture
def fake_summary(monkeypatch: pytest.MonkeyPatch) -> _FakeSummary:
    stub = _FakeSummary()
    monkeypatch.setattr(tui_module, "summary", stub)
    return stub


async def _settle(pilot: Any) -> None:
    """Let the dispatch worker (and the continuation turn it starts) finish."""

    for _ in range(40):
        await pilot.pause()


def _stub_summarizing_turn(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeSummary, routed: list[tuple[str, str]]
) -> str:
    """A turn driver whose FIRST turn summarizes; returns the session it opens."""

    new_session = "cont-20260101-120000-abcd"

    def fake_run_chat(opts: Any, sink: Any = None) -> str:
        routed.append((opts.session, opts.prompt))
        if len(routed) == 1:
            fake.request_handoff(
                new_session, from_session=opts.session, summary_dir=HANDOFF_SUMMARY_DIR
            )
            inbox.insert(new_session, "요약을 보고 이어서 진행", kind="user", source="tool")
        return ""

    monkeypatch.setattr(tui_module, "run_chat", fake_run_chat)
    return new_session


def test_a_summary_handoff_switches_the_session_and_runs_the_continuation(
    fake_summary: _FakeSummary, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body() -> None:
        routed: list[tuple[str, str]] = []
        new_session = _stub_summarizing_turn(monkeypatch, fake_summary, routed)
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Given / When: one turn that summarizes and calls continue_session
            app.on_chat_input_submitted(ChatInput.Submitted("do the work"))
            await _settle(pilot)

            # Then: the SAME conversation continued in the new session, on the
            # continuation message the tool left in its inbox — no user action
            assert [prompt for _, prompt in routed] == ["do the work", "요약을 보고 이어서 진행"]
            assert routed[1][0] == new_session
            assert app._session_name == new_session
            assert app._inbox_session() == new_session
            assert settings_store.load().session == new_session

    _run(body)


def test_the_old_conversation_shows_the_handoff_marker_and_keeps_it(
    fake_summary: _FakeSummary, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body() -> None:
        routed: list[tuple[str, str]] = []
        new_session = _stub_summarizing_turn(monkeypatch, fake_summary, routed)
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.on_chat_input_submitted(ChatInput.Submitted("do the work"))
            await _settle(pilot)
            old_session = routed[0][0]
            marker = f"------summary → {new_session}------\nsummary: {HANDOFF_SUMMARY_DIR}"

            # Then: the live log shows it
            rendered = [w.render().plain for w in app._window.rows]
            assert any(f"summary → {new_session}" in text for text in rendered)
            assert any(HANDOFF_SUMMARY_DIR in text for text in rendered)

            # Then: and it is ON DISK, so reopening the old session replays it
            data = session_store.load(old_session)
            assert data.turns[-1].events == [
                {"actor": "outo", "kind": "handoff", "text": marker}
            ]
            app._load_session_history(old_session)
            await pilot.pause()
            replayed = [w.render().plain for w in app._window.rows]
            assert any(f"summary → {new_session}" in text for text in replayed)

    _run(body)


def test_a_dispatch_registers_the_pinned_session_as_the_run_contract(
    fake_summary: _FakeSummary, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body() -> None:
        routed: list[tuple[str, str]] = []
        _stub_run_chat(monkeypatch, routed)
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Given / When: a TUI turn starts
            await app._dispatch("do the work")
            await pilot.pause()

            # Then: the summarization lane is told this session — and a TUI run
            # is plain, so identity only, no delegation contract to carry
            assert fake_summary.contracts == [
                {"session": app._session_name, "style": settings_store.load().style or ""}
            ]

    _run(body)


def _channel_sum(hex_color: str) -> int:
    """Sum of a color's three channels — the "how dark is it" measure here."""

    raw = hex_color.lstrip("#")
    return sum(int(raw[index : index + 2], 16) for index in (0, 2, 4))


def _relative_luminance(hex_color: str) -> float:
    raw = hex_color.lstrip("#")
    channels = []
    for index in (0, 2, 4):
        value = int(raw[index : index + 2], 16) / 255
        channels.append(
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        )
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 2.1 contrast ratio between two hex colors."""

    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def _css_variable(app: ChatTUI, name: str) -> str:
    """What a `$name` CSS reference resolves to on this booted app."""

    return app.get_css_variables()[name]


def test_the_miniouto_theme_is_registered_and_active_at_start() -> None:
    def body(app: ChatTUI, pilot: Any) -> None:
        # Given / When: a freshly constructed app, before any user action
        # Then: the theme is registered and is the one in force
        assert tui_module.THEME_NAME in app.available_themes
        assert app.theme == tui_module.THEME_NAME
        assert app.current_theme.name == tui_module.THEME_NAME
        assert app.current_theme.dark is True

    _in_app(body)


def test_the_miniouto_accent_is_the_owner_pinned_cyan() -> None:
    def body(app: ChatTUI, pilot: Any) -> None:
        # Given / When: the theme's own fields and the CSS variables the app
        # renders through both name the same accent
        # Then: primary (and accent, which IS the accent color here) is the pin
        assert tui_module.MINIOUTO_THEME.primary == "#2fc2ff"
        assert tui_module.MINIOUTO_THEME.accent == "#2fc2ff"
        assert _css_variable(app, "primary").lower() == "#2fc2ff"
        assert _css_variable(app, "accent").lower() == "#2fc2ff"

    _in_app(body)


def test_the_miniouto_background_is_strictly_darker_than_the_stock_dark_one() -> None:
    def body(app: ChatTUI, pilot: Any) -> None:
        # Given / When: the colors each theme actually RESOLVES to (a stock
        # theme leaves `background` unset and Textual supplies its own default,
        # so the declared fields are not what gets painted)
        stock = app.get_theme("textual-dark")
        assert stock is not None
        stock_colors = stock.to_color_system().generate()
        mine_colors = tui_module.MINIOUTO_THEME.to_color_system().generate()

        # Then: this theme's dark surfaces are darker on channels, so the
        # owner constraint "noticeably darker" holds objectively, not just
        # subjectively — and the panel/boost steps still climb above them, so
        # the surfaces stay distinguishable
        for name in ("background", "surface"):
            assert _channel_sum(mine_colors[name]) < _channel_sum(stock_colors[name])
        assert _channel_sum(mine_colors["panel"]) > _channel_sum(mine_colors["surface"])
        assert _channel_sum(mine_colors["boost"]) > _channel_sum(mine_colors["panel"])

        # And the floor is lifted OFF near-black: the previous #05070a (channel
        # sum 22) measured "great" by WCAG but glared under halation, so a
        # regression back down to it must fail here. 40 is ~#0a0d13.
        assert _channel_sum(mine_colors["background"]) >= 40

    _in_app(body)


def test_the_body_text_contrast_sits_in_the_comfort_band_on_every_surface() -> None:
    def body(app: ChatTUI, pilot: Any) -> None:
        # Given: the resolved body-text color and the surfaces the transcript
        # body is drawn on ($background rows, $surface input, $panel chips)
        variables = app.get_css_variables()
        foreground = variables["foreground"]
        # One band per surface, stepped by the surface's own luminance: the
        # contract is 9.5-10.5:1 on `$background`, and the two lighter
        # surfaces legitimately sit lower without any separate decision.
        bands = {
            "background": (9.5, 10.5),
            "surface": (9.0, 9.6),
            "panel": (8.3, 9.0),
        }

        # Then: each surface holds its own band. Asserted per surface (not
        # just on the darkest one) because the three differ enough that a
        # value could sit in band on two and out on one.
        for name, (low, high) in bands.items():
            surface = variables[name]
            ratio = _contrast_ratio(foreground, surface)
            assert low <= ratio <= high, f"{foreground} on ${name} {surface} is {ratio:.2f}"

    _in_app(body)


def test_the_final_answer_is_the_brightest_text_layer() -> None:
    def body(app: ChatTUI, pilot: Any) -> None:
        # Given / When: the answer color against the transcript background
        variables = app.get_css_variables()
        ratio = _contrast_ratio(variables["text-answer"], variables["background"])

        # Then: 11.5-12.5:1 — a near-white that is unmistakably the focal
        # layer, and still short of the ~13.3:1 glare the comfort pass removed
        assert 11.5 <= ratio <= 12.5, f"answer on background is {ratio:.2f}"

    _in_app(body)


def test_the_text_layers_form_a_strict_answer_body_muted_ladder() -> None:
    def body(app: ChatTUI, pilot: Any) -> None:
        # Given / When: the four text layers measured on the transcript
        # background — the hierarchy only reads if they are ORDERED, which is
        # what the owner asked for after the comfort pass flattened the
        # transcript into one undifferentiated gray.
        variables = app.get_css_variables()
        background = variables["background"]
        answer = _contrast_ratio(variables["text-answer"], background)
        foreground = _contrast_ratio(variables["foreground"], background)
        muted = _contrast_ratio(variables["text-muted"], background)
        disabled = _contrast_ratio(variables["text-disabled"], background)

        # Then: answer > body > muted > disabled, with a real gap at each step
        assert answer > foreground, f"answer {answer:.2f} vs body {foreground:.2f}"
        assert foreground > muted, f"body {foreground:.2f} vs muted {muted:.2f}"
        assert muted > disabled, f"muted {muted:.2f} vs disabled {disabled:.2f}"
        assert answer - foreground >= 1.5, f"answer is only {answer - foreground:.2f} above body"

    _in_app(body)


def test_the_answer_row_paints_the_answer_layer_and_not_the_body_one() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(78, 24)) as pilot:
            await pilot.pause()
            # Given: a mounted Markdown answer and a plain body row beside it
            # (the two layers the eye is meant to tell apart)
            app._mount_row(tui_module.AnswerRow(Markdown("**bold** plain answer text")))
            app._mount_row(Static("plain static body"))
            await pilot.pause()
            await pilot.pause()

            # When / Then: the compositor is what the terminal receives, so
            # reading it proves the Markdown renderable really takes the
            # row's color — `$text-answer`, not the inherited `$foreground`.
            # Matched per SEGMENT (a dict keyed by line would collapse the
            # line's spans and keep only whichever color came last).
            variables = app.get_css_variables()
            segments = [
                (segment.text, segment.style.color.name)
                for strip in app.screen._compositor.render_strips()
                for segment in strip._segments
                if segment.text.strip() and segment.style is not None and segment.style.color
            ]
            answer_color = variables["text-answer"].lower()
            body_color = variables["foreground"].lower()
            answer_spans = {text: color for text, color in segments if text in ("bold", "answer")}
            body_spans = {text: color for text, color in segments if text == "plain static body"}

            assert answer_spans, "the answer row painted no visible spans"
            assert set(answer_spans.values()) == {answer_color}, (
                f"answer row painted {answer_spans}, want {answer_color}"
            )
            assert set(body_spans.values()) == {body_color}, (
                f"body row painted {body_spans}, want {body_color}"
            )

    _run(body)


def test_the_muted_text_ramp_sits_in_its_comfort_band_on_the_background() -> None:
    def body(app: ChatTUI, pilot: Any) -> None:
        # Given / When: the hint color against the transcript background
        variables = app.get_css_variables()
        ratio = _contrast_ratio(variables["text-muted"], variables["background"])

        # Then: 4.5-5.2:1 — readable, but not another thing to squint at
        assert 4.5 <= ratio <= 5.2, f"muted on background is {ratio:.2f}"

    _in_app(body)


def test_every_semantic_color_clears_the_aa_floor_on_every_surface() -> None:
    def body(app: ChatTUI, pilot: Any) -> None:
        # Given / When: the four loud colors against every surface, `$boost`
        # included (the status chips and picker rows sit on it)
        variables = app.get_css_variables()
        surfaces = [variables[name] for name in ("background", "surface", "panel", "boost")]

        # Then: 4.5:1 minimum everywhere, so desaturating them for comfort
        # never cost a color its legibility
        for name in ("secondary", "success", "warning", "error"):
            for surface in surfaces:
                ratio = _contrast_ratio(variables[name], surface)
                assert ratio >= 4.5, f"${name} {variables[name]} on {surface} is {ratio:.2f}"

    _in_app(body)


def test_the_disabled_text_ramp_stays_below_the_muted_one() -> None:
    def body(app: ChatTUI, pilot: Any) -> None:
        # Given / When: the two dim ends of the text ramp on $background
        variables = app.get_css_variables()
        disabled = _contrast_ratio(variables["text-disabled"], variables["background"])
        muted = _contrast_ratio(variables["text-muted"], variables["background"])

        # Then: disabled still reads as OFF. WCAG exempts disabled controls
        # from the 4.5 floor, so this is a "not brighter than muted" guard
        # rather than a contrast floor.
        assert disabled < muted, f"disabled {disabled:.2f} vs muted {muted:.2f}"

    _in_app(body)


def test_every_colored_css_variable_the_app_uses_resolves() -> None:
    def body(app: ChatTUI, pilot: Any) -> None:
        # Given / When: the variables the app's CSS references
        # Then: none is left transparent/undefined by this theme — in
        # particular `$boost`, which `Theme.boost` alone would NOT provide
        # (Textual drops the boost field whenever `panel` is set explicitly)
        variables = app.get_css_variables()
        for name in (
            "accent",
            "background",
            "boost",
            "error",
            "panel",
            "primary",
            "screen-selection-background",
            "surface",
            "text",
            "text-answer",
            "text-muted",
            "warning",
        ):
            value = variables[name].lower()
            assert not value.startswith("transparent"), f"${name} is {value}"
        assert variables["boost"].lower() == "#1d2530"
        assert variables["text-muted"].lower() == "#7b8490"
        assert variables["text-answer"].lower() == "#c5d0d8"
        assert variables["foreground"].lower() == "#a9bac5"

    _in_app(body)


def test_the_muted_text_ramp_clears_the_aa_floor_on_every_surface() -> None:
    def body(app: ChatTUI, pilot: Any) -> None:
        # Given / When: the hint text color and the surfaces it is actually
        # drawn on ($background rows, $surface input, $panel chips/modals)
        variables = app.get_css_variables()
        muted = variables["text-muted"]
        surfaces = [variables[name] for name in ("background", "surface", "panel")]

        # Then: AA for normal-size text on the darkest and the lightest of them
        for surface in surfaces:
            assert _contrast_ratio(muted, surface) >= 4.5, f"{muted} on {surface}"

        # And the accent reads as an accent, not as body text: comfortably
        # above the muted ramp on every one of those surfaces
        for surface in surfaces:
            assert _contrast_ratio(variables["primary"], surface) > 7.0

    _in_app(body)


def test_settings_theme_overrides_the_default_on_the_next_boot() -> None:
    # Given: an operator who picked a different theme last session. The value
    # must NOT be Textual's own default (`textual-dark`), or the assertion
    # would pass whether or not settings were read at all.
    toml_io = tui_module.settings_store.toml_io
    saved = toml_io.load(tui_module.settings_store.SETTINGS_FILE)
    toml_io.save(tui_module.settings_store.SETTINGS_FILE, {**saved, "theme": "gruvbox"})
    try:

        def body(app: ChatTUI, pilot: Any) -> None:
            # When / Then: the stored value wins over the `miniouto` default
            assert app.theme == "gruvbox"
            assert app.current_theme.name == "gruvbox"

        _in_app(body)
    finally:
        toml_io.save(tui_module.settings_store.SETTINGS_FILE, saved)


def test_an_unregistered_settings_theme_falls_back_to_the_default() -> None:
    # Given: a settings.toml naming a theme no build registers
    toml_io = tui_module.settings_store.toml_io
    saved = toml_io.load(tui_module.settings_store.SETTINGS_FILE)
    toml_io.save(
        tui_module.settings_store.SETTINGS_FILE, {**saved, "theme": "no-such-theme"}
    )
    try:

        def body(app: ChatTUI, pilot: Any) -> None:
            # When / Then: booting still works (an invalid name must not kill
            # the TUI through `App.theme`'s validator) on the pinned default
            assert app.theme == tui_module.THEME_NAME

        _in_app(body)
    finally:
        toml_io.save(tui_module.settings_store.SETTINGS_FILE, saved)


def test_the_theme_a_user_picks_is_persisted_for_the_next_boot() -> None:
    async def body() -> None:
        # Given: an app already running on the `miniouto` default
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # When: the theme is changed (what `Ctrl+P` → Theme does)
            app.theme = "gruvbox"
            await pilot.pause()
            # Then: the choice is on disk, which is what the next boot reads
            assert settings_store.load().theme == "gruvbox"

    _run(body)


# ── catalog compatibility: provider-global format + dialect base URL ──────────


@pytest.fixture
def dialect_provider(settings_home: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """A catalog provider with both confirmed and tentative hostable dialects."""

    provider_store.upsert(
        replace(
            provider_store.get("pop-alpha"),
            api_format="openai",
            base_url="https://gateway.example/v1",
            catalog_dialect="openai-chat",
        )
    )
    monkeypatch.setattr(
        tui_module.catalog_api,
        "find_provider_by_slug",
        lambda name: (
            {
                "name": "Pop Alpha",
                "sdk": "openai-compatible",
                "api": "https://gateway.example/v1",
                "dialects": {
                    "supported": {
                        "openai-chat": "https://gateway.example/v1",
                        "anthropic-messages": "https://gateway.example/anthropic",
                    },
                    "likely": {
                        "openai-responses": "https://gateway.example/v1",
                        "typesafe-systemone": "https://ignored.example",
                    },
                },
            }
            if name == "pop-alpha"
            else None
        ),
    )
    return "pop-alpha"


def test_catalog_provider_has_a_compatibility_row_with_the_saved_dialect(
    dialect_provider: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            settings_store.update(provider="pop-alpha")
            _open_popup(app, chip="outo")
            await pilot.pause()

            assert isinstance(app.screen, ProviderSettingsPopup)
            assert _fields(app.screen) == [
                "provider", "model", "reasoning", "compatibility"
            ]
            assert _row_text(app.screen, "compatibility") == (
                "Compatibility: openai-chat"
            )

    asyncio.run(body())


def test_compatibility_picker_changes_format_and_url_without_touching_the_trio(
    dialect_provider: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            session = _pin_test_session(app)
            settings_store.update(provider="pop-alpha")
            _open_popup(app, chip="outo")
            await pilot.pause()
            pin_before = session_config.read(session)

            await pilot.click(_row(app.screen, "compatibility"), offset=(2, 0))
            for _ in range(6):
                await pilot.pause()
            assert isinstance(app.screen, SelectionModal)
            labels = [
                str(item.query_one(Label).render().plain).strip()
                for item in app.screen.query_one("#selection-list").children
            ]
            assert any("anthropic-messages (supported)" in label for label in labels)
            assert any("openai-responses (likely)" in label for label in labels)
            assert not any("typesafe-systemone" in label for label in labels)

            selected = next(
                label for label in labels if "anthropic-messages (supported)" in label
            )
            app.screen.dismiss(selected.removeprefix("● "))
            await _settle(pilot)

            assert isinstance(app.screen, ProviderSettingsPopup)
            record = provider_store.get("pop-alpha")
            assert record.catalog_dialect == "anthropic-messages"
            assert record.api_format == "anthropic"
            assert record.base_url == "https://gateway.example/anthropic"
            assert _row_text(app.screen, "compatibility") == (
                "Compatibility: anthropic-messages"
            )
            assert _row_text(app.screen, "model") == "Model: alpha-default"
            assert _row_text(app.screen, "reasoning") == "Reasoning: high"
            # Like Caps, compatibility belongs to the provider record, not to
            # an individual session's provider/model/reasoning pin.
            assert session_config.read(session) == pin_before

    asyncio.run(body())


def test_inherited_subagent_popup_edits_the_effective_outo_compatibility(
    dialect_provider: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            settings_store.save(
                replace(
                    settings_store.load(),
                    provider="pop-alpha",
                    subagent_provider="",
                    subagent_model="",
                    subagent_reasoning="",
                )
            )
            _open_popup(app, chip="subagent")
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)
            assert "compatibility" in _fields(app.screen)

            await pilot.click(_row(app.screen, "compatibility"), offset=(2, 0))
            for _ in range(6):
                await pilot.pause()
            assert isinstance(app.screen, SelectionModal)
            selected = next(
                str(item.query_one(Label).render().plain).strip().removeprefix("● ")
                for item in app.screen.query_one("#selection-list").children
                if "openai-responses (likely)"
                in str(item.query_one(Label).render().plain)
            )
            app.screen.dismiss(selected)
            await _settle(pilot)

            record = provider_store.get("pop-alpha")
            assert record.catalog_dialect == "openai-responses"
            assert record.api_format == "openai-response"
            assert settings_store.load().subagent_provider == ""

    asyncio.run(body())


def test_catalog_add_persists_the_dialect_format_and_exact_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TUI add wizard must not silently fall back to the generic api URL."""

    name = "Dialect Gateway"
    slug = "dialect-gateway"
    monkeypatch.setattr(
        tui_module.catalog_api,
        "list_providers",
        lambda: [
            {
                "name": name,
                "sdk": "openai-compatible",
                "api": "https://gateway.example/v1",
                "dialects": {
                    "supported": {
                        "anthropic-messages": "https://gateway.example/anthropic"
                    },
                    "likely": {"openai-chat": "https://gateway.example/v1"},
                },
            }
        ],
    )
    monkeypatch.setattr(
        tui_module.catalog_api,
        "list_models",
        lambda _provider: [{"id": "gateway-model"}],
    )
    monkeypatch.setattr(tui_module, "_get_reasoning_api", lambda: None)
    screens = iter([name, "test-api-key"])

    async def fake_push_screen_wait(screen: Any) -> str | None:
        assert isinstance(screen, (SelectionModal, TextInputModal))
        return next(screens)

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            monkeypatch.setattr(app, "push_screen_wait", fake_push_screen_wait)

            await app._catalog_add_flow()

            record = provider_store.get(slug)
            assert record is not None
            assert record.catalog_dialect == "anthropic-messages"
            assert record.api_format == "anthropic"
            assert record.base_url == "https://gateway.example/anthropic"
            assert record.default_model == "gateway-model"
            assert settings_store.load().provider == slug
            provider_store.remove(slug)

    asyncio.run(body())



def test_compatibility_can_restore_the_native_sdk_mapping(
    dialect_provider: str,
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            settings_store.update(provider="pop-alpha")
            _open_popup(app, chip="outo")
            await pilot.pause()

            await pilot.click(_row(app.screen, "compatibility"), offset=(2, 0))
            for _ in range(6):
                await pilot.pause()
            assert isinstance(app.screen, SelectionModal)
            selected = next(
                str(item.query_one(Label).render().plain).strip().removeprefix("● ")
                for item in app.screen.query_one("#selection-list").children
                if "native SDK" in str(item.query_one(Label).render().plain)
            )
            app.screen.dismiss(selected)
            await _settle(pilot)

            record = provider_store.get("pop-alpha")
            assert record.catalog_dialect == ""
            assert record.api_format == "openai"
            assert record.base_url == "https://gateway.example/v1"
            assert _row_text(app.screen, "compatibility") == "Compatibility: native SDK"

    asyncio.run(body())


def test_compatibility_preserves_a_saved_dialect_missing_from_current_lma_results(
    dialect_provider: str,
) -> None:
    async def body() -> None:
        provider_store.upsert(
            replace(
                provider_store.get("pop-alpha"),
                api_format="openai-response",
                base_url="https://old.example/v1",
                catalog_dialect="openai-responses",
            )
        )
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _pin_test_session(app)
            settings_store.update(provider="pop-alpha")
            _open_popup(app, chip="outo")
            await pilot.pause()
            await pilot.click(_row(app.screen, "compatibility"), offset=(2, 0))
            for _ in range(6):
                await pilot.pause()

            assert isinstance(app.screen, SelectionModal)
            labels = [
                str(item.query_one(Label).render().plain).strip()
                for item in app.screen.query_one("#selection-list").children
            ]
            saved = next(label for label in labels if "(saved)" in label)
            assert "https://old.example/v1" in saved
            assert saved.startswith("● ")
            # Confirming the highlighted saved row is a no-op, not a silent
            # migration to the current catalog's preferred dialect.
            app.screen.dismiss(saved.removeprefix("● "))
            await _settle(pilot)
            record = provider_store.get("pop-alpha")
            assert record.catalog_dialect == "openai-responses"
            assert record.api_format == "openai-response"
            assert record.base_url == "https://old.example/v1"

    asyncio.run(body())
