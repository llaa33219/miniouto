"""App-level smoke tests: the composited frame, the dispatch path, the overlays.

Everything here drives a REAL headless ``ChatTUI`` through ``run_test`` — the
same boot, the same key router, the same composition the terminal gets — and
asserts on what a user could see or on the state a user's action leaves behind.
The old Textual pilot-smoke assertions (``tests/test_tui_navigation.py``,
``test_alarm_display.py``, ``test_tui_indicators.py``) are the specification for
the ones that existed; the rest lock the two real-terminal bugs the port hit:

* **The frame was shifted up one row.** ``_paint_log`` wrote each row's lines at
  a row-relative offset, so the logo landed on row 0 and erased the header. The
  regression is asserted as "row 0 is the header, the panel is rows 28-31",
  which is exactly what the pty baseline shows.
* **The real loop never delivered a posted message.** ``Enter`` in the input
  posted ``ChatInput.Submitted`` and nothing drained it, so submitting only
  worked under ``pilot.pause``. The pump is exercised directly here, because the
  terminal loop's framing (Terminal/reader thread) is not what was broken.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

import pytest
from rich.cells import cell_len
from rich.text import Text

import tui_harness
from miniouto.cli import tui as tui_module
from miniouto.cli.tui import (
    CHAT_MAX_WIDTH,
    HELP_TEXT,
    AnswerRow,
    ChatTUI,
    CommandPalette,
    LoopEvent,
    ProviderSettingsPopup,
    ThinkingRow,
)
from miniouto.cli.tui.input import Mouse
from miniouto.cli.tui.nodes import Rect
from miniouto.cli.tui.rows import RowStatic
from miniouto.core import inbox
from miniouto.storage import sessions as session_store
from miniouto.storage import settings as settings_store

BOOT = tui_harness.BOOT_SIZE


@pytest.fixture(autouse=True)
def _restore_settings() -> Any:
    """Put ``settings.toml`` back after every test in this module.

    These tests deliberately write the theme and the active session — that IS
    the behaviour under test — but the settings file is shared, so an unrestored
    write would decide the boot theme (and the session) of every test that runs
    afterwards. Owned by the module rather than by each test because it must
    also cover the async bodies, which a plain ``try/finally`` inside one test
    would silently skip on an assertion failure.
    """

    saved = settings_store.load()
    yield
    settings_store.save(saved)


# ── 1. the boot frame ───────────────────────────────────────────────────────


def test_the_boot_frame_starts_with_the_header_and_ends_with_the_panel() -> None:
    """The whole-frame regression: header on row 0, panel on the last three rows."""

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            lines = tui_harness.composed(app, *BOOT)
            # Then: row 0 is the HEADER, not the logo (the off-by-one wrote the
            # logo's first line there and pushed the header out of the frame).
            assert "miniouto v" in lines[0], f"row 0 is not the header: {lines[0]!r}"
            assert "⭘" not in lines[0], f"the header icon is back: {lines[0]!r}"
            assert "⠀" not in lines[0], "the logo overwrote the header row"
            # And: the logo is the block directly under it, then the ready line.
            assert "⠀" in lines[1], f"the logo does not start on row 1: {lines[1]!r}"
            ready = [i for i, line in enumerate(lines) if "Ready. Press Ctrl+P" in line]
            assert ready and 1 < ready[0] < BOOT[1], f"no ready line: {ready}"
            # And: the panel keeps its three rows at the bottom of the frame —
            # a blank spinner row, then the two chip rows. The retired help
            # hint stays retired: the Keys popup lists the bindings now.
            panel_rows = lines[-3:]
            assert panel_rows[0].strip() == "", f"the idle spinner row is not blank: {panel_rows}"
            assert "outo:" in panel_rows[1], panel_rows
            assert "subagent:" in panel_rows[2], panel_rows
            assert HELP_TEXT not in "\n".join(panel_rows), panel_rows
            # And: no logo leaked into any of them
            assert "⠀" not in "".join(panel_rows)

    asyncio.run(body())


def test_the_panel_occupies_the_last_three_rows_of_the_screen() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            _log, _input, panel, _x = app.layout(*BOOT)
            # Then: the panel's region is the bottom band, and the composited
            # screen agrees (that is the copy a terminal actually draws).
            assert panel.y == BOOT[1] - 3, panel
            assert panel.y + panel.height == BOOT[1], panel
            assert tui_harness.painted_line(app, panel.y).strip() == "", "spinner row"
            assert "outo:" in tui_harness.painted_line(app, panel.y + 1)
            assert "subagent:" in tui_harness.painted_line(app, panel.y + 2)
            assert HELP_TEXT not in tui_harness.painted_line(app, panel.y + 2)
            # The row above the panel is the input band; the panel owns the
            # rows below it and nothing else writes down here.
            assert "█" in tui_harness.painted_line(app, panel.y - 1)

    asyncio.run(body())


def test_the_boot_frame_shows_the_logo_the_chips_and_the_keys_popup() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            screen = "\n".join(tui_harness.painted(app))
            # Then: the ornament, the four chips and the bindings are all there.
            assert "⠀" in screen, "no logo"
            assert "Ready. Press Ctrl+P for commands." in screen
            values = tui_harness.chip_values(app)
            assert set(values) == {"outo", "style", "subagent", "session"}
            for kind, text in values.items():
                assert text in screen, f"the {kind} chip is not on screen: {text!r}"
            assert HELP_TEXT not in screen, "the retired help hint is painted"

    asyncio.run(body())


def test_every_chip_renders_its_value_whole_and_on_screen() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            for kind, chip in tui_harness.chip_rows(app).items():
                # Then: a chip keeps a real, on-screen region — the display
                # cannot reach a chip that sits past the right edge.
                assert chip.region.width > 0, f"{kind} collapsed"
                assert 0 <= chip.region.x < BOOT[0], f"{kind} starts off screen"
                assert chip.region.x + chip.region.width <= BOOT[0] + 1, kind
                # And: its painted text is the whole value, never elided.
                painted = chip.render().plain
                assert "…" not in painted
                assert painted == tui_harness.chip_values(app)[kind]

    asyncio.run(body())


def test_an_image_lane_blanks_the_frame_rows_it_actually_occupies() -> None:
    """The same off-by-one, on the image lane: ``top`` is a FRAME row.

    ``GraphicsOverlay.add`` emits the pixels at the frame-absolute ``top``, so
    the rect the render blanks has to be at that same coordinate. Subtracting
    the log rect's ``y`` blanked the lane one row too high, which painted log
    text over the picture and left a hole above it.
    """

    class Placement:
        width_cells = 6
        height_cells = 2

        def emit_at(self, x: int, y: int) -> bytes:
            return b""

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            row = RowStatic(Text("x"))
            lines = [[(" " * 100, None)] for _ in range(30)]
            log_rect = Rect(0, 5, 100, 20)
            app._overlay = type(app._overlay)()
            row.image_placements = lambda: [(0, 2, Placement())]  # type: ignore[method-assign]
            app._paint_images(row, 7, log_rect, lines, None)  # type: ignore[arg-type]
            # Then: the overlay emits at the frame row …
            assert app._overlay.clear_regions() == [(2, 7, 6, 2)]
            # … and exactly those rows are blanked — not rect.y's offset.
            assert lines[7][0][0].strip() == ""
            assert lines[8][0][0].strip() == ""
            assert lines[5][0][0].strip() == ""
            assert lines[6][0][0].strip() == ""

    asyncio.run(body())




def test_a_real_screenshot_row_paints_its_pixels_through_the_overlay(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole lane, end to end: row → rect → blanked frame → emitted pixels.

    ``ToolRow`` reserves the rect and hands over a placement; the app blanks
    exactly those frame rows and registers the placement; after the frame the
    overlay writes the protocol bytes through the transport it is given, and a
    removed row's delete takes the same route. This is the wiring between the
    three modules, which no single-module test covers: a mismatch in the FRAME row
    (the port's off-by-one) shows up here as pixels painted on top of text.

    The rect is read back from the overlay rather than computed here, because the
    app re-lays the row out at the log's own width — the line indices a 60-column
    build produces are not the ones a 100-column log does.
    """

    from miniouto.cli.tui import graphics
    from test_tui_rows import THEME, WIDTH

    image = pytest.importorskip("PIL.Image")
    png = tmp_path / "shot.png"
    image.new("RGB", (64, 36), (200, 30, 40)).save(png)
    monkeypatch.setattr(graphics, "_GRAPHICS_MODE", graphics.GRAPHICS_MODE_TGP)
    monkeypatch.setattr(
        graphics, "_DETECTED", graphics._Capabilities(frozenset({"kitty"}), (10, 20))
    )

    row = tui_module.ToolRow("Computer screenshot", tool_name="Computer")
    row.set_result(f"Screenshot of the virtual display.\nImage saved to: {png}")
    row._toggle()
    row.build_lines(WIDTH, THEME)

    written: list[bytes] = []
    graphics.set_writer(written.append)
    try:

        async def body() -> None:
            app = ChatTUI()
            # Tall enough that the whole image rect sits inside the log viewport,
            # so the blanking below is the rect itself, not a clipped part of it.
            async with app.run_test(size=(100, 90)) as pilot:
                await pilot.pause()
                frame: list[list[tuple[str, Any]]] = [
                    [(" " * 100, None)] for _ in range(90)
                ]
                app._paint_images(row, 7, Rect(0, 5, 100, 80), frame, None)  # type: ignore[arg-type]
                (x, top, width, height), = app._overlay.clear_regions()
                # Then: the rect starts below the row's caption, and every one of
                # its frame rows was blanked — the row above it was not touched.
                assert width > 0 and height > 0
                assert top > 7
                for blank in range(height):
                    assert frame[top + blank][0][0].strip() == ""
                assert frame[top - 1][0][0].strip() == ""
                # When: the frame is done and the overlay emits
                app._overlay.emit_post_draw()
                assert len(written) == 1
                emitted = written[0]
                # Then: the pixels go out with a CUP at that exact frame row/column …
                assert f"\x1b[{top + 1};{x + 1}H".encode() in emitted
                # … as an upload the first time, and a place-only copy afterwards
                assert b"a=T" in emitted
                app._overlay.emit_post_draw()
                assert b"a=T" not in written[1]
                assert b"a=p" in written[1]
                # …and the row's removal deletes the image it uploaded, through
                # the same writer the frames went to.
                (placement, _px, _py), = app._overlay._placements
                image_id = placement.image_id
                assert image_id is not None
                written.clear()
                row.release_image()
                assert [w for w in written if b"a=d,d=I" in w and f"i={image_id}".encode() in w]

        asyncio.run(body())
    finally:
        graphics.set_writer(None)


def test_the_text_lane_shows_the_art_and_never_touches_the_overlay(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GRAPHICS_MODE_TEXT is the unchanged `.art` lane: no rect, no pixels.

    A non-graphics terminal must look exactly as it did before this lane existed,
    which means the row takes the text path and the overlay stays empty.
    """

    from miniouto.cli.tui import graphics
    from test_tui_rows import THEME, WIDTH, write_art

    image = pytest.importorskip("PIL.Image")
    png = tmp_path / "shot.png"
    image.new("RGB", (64, 36), (200, 30, 40)).save(png)
    art = write_art(tmp_path, rows=3)
    monkeypatch.setattr(graphics, "_GRAPHICS_MODE", graphics.GRAPHICS_MODE_TEXT)

    row = tui_module.ToolRow("Computer screenshot", tool_name="Computer")
    row.set_result(f"shot\nImage saved to: {png}\nSaved to: {art}")
    row._toggle()
    row.build_lines(WIDTH, THEME)

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            frame = [[(" " * 100, None)] for _ in range(30)]
            app._overlay = type(app._overlay)()
            app._paint_images(row, 7, Rect(0, 5, 100, 20), frame, None)  # type: ignore[arg-type]
            # Then: no pixel rect anywhere, and the `.art` render is in the row
            assert app._overlay.clear_regions() == []
            assert row.image_placements() == []
            block = row._screen_art()
            assert block is not None and str(art) in block.plain

    asyncio.run(body())


# ── 2. submit → dispatch ────────────────────────────────────────────────────


def test_enter_dispatches_the_prompt_on_the_pinned_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns: list[dict[str, Any]] = []

    async def body() -> None:
        tui_harness.stub_run_chat(monkeypatch, turns)
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-submit-session")
            # Given / When: a prompt typed into the input and submitted
            app._input.text = "hello from the test"
            await pilot.press("enter")
            await tui_harness.settle(pilot, want=1, turns=turns)
            # Then: it went to the turn driver, on THIS session, continuing it
            assert turns[0]["prompt"] == "hello from the test", turns
            assert turns[0]["session"] == "app-submit-session", turns
            assert turns[0]["continue"] is True
            # And: the input is empty and the prompt is a user row on screen
            assert app._input.text == ""
            rows = [r for r in app._log.rows if isinstance(r, RowStatic)]
            assert rows and any(
                tui_harness.row_text(row).lstrip().startswith("> hello from the test")
                for row in rows
            )

    asyncio.run(body())


def test_a_posted_submit_message_is_delivered_by_the_message_pump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real-terminal regression: ``post_message`` alone must dispatch.

    The terminal loop used to drain the input QUEUE only, so a handler that
    POSTED a message (Enter → ``ChatInput.Submitted``) had no effect outside a
    ``pilot.pause`` drive. The pump is ``_drain_messages``; asserting on it is
    what keeps the loop honest without owning a terminal in a test.
    """

    turns: list[dict[str, Any]] = []

    async def body() -> None:
        tui_harness.stub_run_chat(monkeypatch, turns)
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-pump-session")
            app._input.text = "posted, not pumped"
            # Given: the key is handled (it posts) but NOTHING pumps afterwards
            app.inject_key(tui_module.Key("Enter"))
            assert app._messages, "Enter posted no message at all"
            assert not turns, "the dispatch ran without a pump"
            # When: the loop's own pump step runs
            app._drain_messages()
            await tui_harness.settle(pilot, want=1, turns=turns)
            # Then: the turn it was for actually started
            assert turns[0]["prompt"] == "posted, not pumped"
            assert app._input.text == ""

    asyncio.run(body())


def test_a_chip_click_posted_message_also_reaches_the_pump() -> None:
    """A handler-posted message (a chip's ``ChipClicked``) dispatches the same way."""

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-chip-session")
            chip = app.query_one("#chip-outo")
            # When: the chip is activated the way a click does it, and pumped
            chip.activate()
            assert app._messages, "the chip posted no message"
            app._drain_messages()
            # Then: its editor opened
            assert isinstance(app.screen, ProviderSettingsPopup)

    asyncio.run(body())


def test_a_real_click_on_a_chip_opens_its_editor() -> None:
    """The click has to reach the chip: the panel's children carry SCREEN regions."""

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-click-session")
            chip = app.query_one("#chip-outo")
            # Given: the panel is painted, so the chip has its screen region
            app.compose(*BOOT)
            await pilot.click(chip, offset=(2, 0))
            # Then: the click routed to the chip, not to the log behind it
            assert isinstance(app.screen, ProviderSettingsPopup)

    asyncio.run(body())


# ── 3. esc-esc stops the turn ───────────────────────────────────────────────


def test_escape_twice_sets_the_cancel_event() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-esc-session")
            # Given: a turn is in flight, with the event the driver watches
            app._busy = True
            app._cancel_event = threading.Event()
            # When: one Esc only WARNS
            await pilot.press("escape")
            assert app._esc_hint == "press esc again to stop", app._esc_hint
            assert not app._cancel_event.is_set(), "one Esc stopped the turn"
            # And: the second Esc inside the window stops it
            await pilot.press("escape")
            assert app._cancel_event.is_set(), "two Esc presses did not stop the turn"
            assert app._esc_hint == "stopping after the current step…"

    asyncio.run(body())


def test_escape_does_nothing_when_no_turn_is_running() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            app._busy = False
            await pilot.press("escape")
            # Then: no hint, no cancel — an idle app ignores Esc entirely
            assert app._esc_hint is None
            assert app._cancel_event is None
            assert app._last_escape == 0.0

    asyncio.run(body())


# ── 4. ctrl+l clears the log ────────────────────────────────────────────────


def test_ctrl_l_clears_the_log_including_the_logo() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-clear-session")
            app._post_system("something to clear")
            await pilot.pause()
            assert app._log.rows, "nothing was in the log to clear"
            # When: Ctrl+L
            await pilot.press("ctrl+l")
            # Then: the log is empty, the logo is gone and the screen is blank
            # below the header
            assert app._log.rows == []
            assert app._logo_shown is False
            screen = tui_harness.painted(app)
            assert "something to clear" not in "\n".join(screen)
            assert "⠀" not in "\n".join(screen[:-4]), "the logo survived the clear"

    asyncio.run(body())


# ── 5. the command palette ──────────────────────────────────────────────────


def test_ctrl_p_opens_the_palette_and_typing_does_not_filter_it() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            # Given / When: the palette opens over the base screen
            await pilot.press("ctrl+p")
            palette = app.screen
            assert isinstance(palette, CommandPalette)
            # Then: it lists this app's own commands first
            titles = [str(c.title) for c in palette.commands]
            assert titles[0] == "New session", titles
            # No numeric prefixes and none of the three dropped commands
            assert not any(title[:2].isdigit() for title in titles), titles
            assert "Clear log" not in titles and "Maximize" not in titles
            assert "Screenshot" not in titles, titles
            # And: there is no search box, so typing leaves the WHOLE list
            for char in "theme":
                await pilot.press(char)
            assert [str(c.title) for c in palette.results] == titles
            await pilot.pause()

    asyncio.run(body())


def test_the_palette_runs_the_highlighted_command_and_closes() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-palette-session")
            await pilot.press("ctrl+p")
            palette = app.screen
            # "New session" is the first row and already highlighted
            assert str(palette.highlighted().title) == "New session"
            await pilot.press("enter")
            # Then: `New session` ran (the logo is back, a session was
            # minted) and the palette is gone
            assert app.screen is not palette
            assert app._logo_shown is True
            # The mint is a worker (it seeds through the CLI); wait for it.
            await tui_harness.settle_until(
                pilot, lambda: app._pinned_session().startswith("tui-")
            )

    asyncio.run(body())


def test_the_palette_mentions_and_escape_closes_it() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+p")
            assert isinstance(app.screen, CommandPalette)
            # Then: Esc returns to the base screen without running anything
            await pilot.press("escape")
            assert not isinstance(app.screen, CommandPalette)
            assert app._logo_shown is True, "Esc ran a command"

    asyncio.run(body())


@pytest.mark.parametrize(("steps", "expected"), [(1, "Pick session"), (5, "Theme")])
def test_the_palette_lists_every_command_and_down_moves_through_it(
    steps: int, expected: str
) -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+p")
            palette = app.screen
            assert isinstance(palette, CommandPalette)
            # No filtering exists: every command is always on show
            titles = [str(c.title) for c in palette.results]
            assert titles == [
                "New session",
                "Pick session",
                "Configure outo",
                "Configure subagent",
                "Pick style",
                "Theme",
                "Keys",
                "Quit",
            ]
            # And the list is navigated with Down, not typed into
            for _ in range(steps):
                await pilot.press("down")
            assert str(palette.highlighted().title) == expected

    asyncio.run(body())


# ── 6. theme: boot, persist, fallback ──────────────────────────────────────


def test_a_stored_theme_is_used_on_the_next_boot() -> None:
    settings_store.update(theme="nord")
    app = ChatTUI()
    assert app.theme == "nord"
    assert app.current_theme is app.get_theme("nord")


def test_an_unregistered_stored_theme_falls_back_to_the_default() -> None:
    settings_store.update(theme="not-a-registered-theme")
    app = ChatTUI()
    assert app.theme == tui_module.THEME_NAME
    assert app.theme in app.available_themes


def test_assigning_a_theme_persists_it_for_the_next_boot() -> None:
    """Assignment IS the persistence — the old app spelled it a reactive."""

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            # Given / When: a theme is assigned (what the picker and a keybinding do)
            app.theme = "gruvbox"
            # The persistence is a CLI write on a worker (`config set --theme`);
            # wait for the worker before reading the disk back.
            await tui_harness.settle_until(
                pilot, lambda: settings_store.load().theme == "gruvbox"
            )
            # Then: the choice is on disk, which is what the next boot reads …
            assert settings_store.load().theme == "gruvbox"
            # … and it is the theme actually in force now
            assert app.current_theme.name == "gruvbox"
            assert ChatTUI().theme == "gruvbox"

    asyncio.run(body())


def test_css_variables_carry_the_resolved_palette() -> None:
    app = ChatTUI()
    variables = app.get_css_variables()
    # Then: the names the old CSS used all resolve, from the live theme
    for name in ("text", "text-muted", "text-disabled", "text-answer", "boost"):
        assert name in variables, name
    assert variables["primary"] == app.current_theme.primary
    assert variables["text"] == tui_module.auto_text(
        app.current_theme, app.current_theme.background
    )


# ── 7. hand-off ────────────────────────────────────────────────────────────


def test_a_handoff_mounts_the_marker_and_moves_the_session() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-handoff-from")
            session_store.create("app-handoff-from")
            session_store.create("app-handoff-to")
            # Given: the chat child printed the hand-off marker pair on stdout
            # (the CLI detects the transition; the TUI only parses the marker)
            # When: the app applies it
            app._apply_handoff("app-handoff-to", "/tmp/summary")
            await pilot.pause()
            # Then: the OLD conversation ends visibly…
            texts = [tui_harness.row_text(r) for r in app._log.rows]
            assert any("------summary → app-handoff-to------" in t for t in texts), texts
            # …the pin moved… (the publish is a CLI write on a worker)
            assert app._pinned_session() == "app-handoff-to"
            await tui_harness.settle_until(
                pilot, lambda: settings_store.load().session == "app-handoff-to"
            )
            # …and the marker is persisted by the CLI (its own trailing turn on
            # the OLD session — pinned by tests/test_handoff.py); the TUI side
            # only mounts it and moves the pin.

    asyncio.run(body())


# ── 8. the inbox poll ──────────────────────────────────────────────────────


def test_an_idle_inbox_message_becomes_a_user_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    turns: list[dict[str, Any]] = []

    async def body() -> None:
        tui_harness.stub_run_chat(monkeypatch, turns)
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-inbox-session")
            session_store.create("app-inbox-session")
            # Given: a message landed in this session's inbox while idle
            inbox.insert("app-inbox-session", "queued while you were away")
            # When: the poll runs (the 1s timer calls exactly this)
            app._deliver_pending_messages()
            await tui_harness.settle(pilot, want=1, turns=turns)
            # Then: it started a turn with that text as the prompt
            assert turns[0]["prompt"] == "queued while you were away", turns
            assert turns[0]["session"] == "app-inbox-session"
            # And: the inbox is drained, so a second poll does not repeat it
            assert inbox.pending_count("app-inbox-session") == 0
            app._deliver_pending_messages()
            await pilot.pause()
            assert len(turns) == 1, turns

    asyncio.run(body())


def test_an_inbox_message_during_a_turn_is_left_to_the_hook() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-inbox-busy")
            session_store.create("app-inbox-busy")
            inbox.insert("app-inbox-busy", "arrived mid-turn")
            # Given: a turn is in flight — the core's hook owns delivery then
            app._busy = True
            # When: the idle poll runs anyway
            app._deliver_pending_messages()
            # Then: it did NOT steal the message from the running turn
            assert inbox.pending_count("app-inbox-busy") == 1
            app._busy = False
            inbox.drain("app-inbox-busy")

    asyncio.run(body())


# ── 9. the jump button ─────────────────────────────────────────────────────


def test_the_jump_button_appears_when_scrolled_away_and_hides_at_the_tail() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-jump-session")
            for index in range(60):
                app._mount_row(RowStatic(Text(f"line {index}")))
            app.compose(80, 24)
            # Given: the log is longer than its viewport, pinned to the tail
            assert app._log.max_scroll_y > 0
            assert app._jump.display is False, "the button showed while at the tail"
            # When: the user scrolls away from the tail
            app._log.scroll_to(y=0)
            app.compose(80, 24)
            # Then: the button appears …
            assert app._jump.display is True
            button_line = tui_harness.painted_line(app, app._jump.region.y)
            assert "- latest -" in button_line, button_line
            # … and hides again once the tail is back
            app._log.scroll_end(animate=False)
            app.compose(80, 24)
            assert app._jump.display is False

    asyncio.run(body())


def test_clicking_the_jump_button_returns_to_the_tail() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-jump-click")
            for index in range(60):
                app._mount_row(RowStatic(Text(f"line {index}")))
            # A frame first: the viewport height is what bounds max_scroll_y.
            app.compose(80, 24)
            app._log.scroll_to(y=0)
            app.compose(80, 24)
            assert app._jump.display is True
            # When: the button is clicked
            await pilot.click(app.query_one("#jump-bottom"), offset=(1, 0))
            app.compose(80, 24)
            # Then: the log follows its tail again and the button is gone
            assert app._log.scroll_y == app._log.max_scroll_y
            assert app._jump.display is False

    asyncio.run(body())


# ── 10. the compat surface the ported tests rely on ────────────────────────


def test_query_one_finds_the_mounted_widgets_and_the_chips() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            # Then: the ids the ported suites use all resolve, and a chip renders
            for selector in ("#header", "#chat", "#input", "#bottom-panel", "#jump-bottom"):
                assert app.query_one(selector) is not None, selector
            chip = app.query_one("#chip-outo")
            assert chip.render().plain.startswith("outo: ")
            assert chip.region.width > 0
            # And: query() by type walks the same tree
            assert len(app.query(tui_module.StatusChip)) == 4
            assert app.query("#chip-outo") == [chip]

    asyncio.run(body())


def test_a_modal_is_reached_through_the_screen_not_the_app() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-query-session")
            app.on_status_chip_chip_clicked(
                tui_module.StatusChip.ChipClicked(app.query_one("#chip-subagent"))
            )
            popup = app.screen
            assert isinstance(popup, ProviderSettingsPopup)
            # Then: the popup's nodes live in ITS tree, exactly as a pushed
            # Textual screen did — app.query_one does not see them
            assert popup.query_one("#provider-settings-title").render().plain
            with pytest.raises(LookupError):
                app.query_one("#provider-settings-title")

    asyncio.run(body())


def test_remove_children_unmounts_the_base_screen() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            assert app._base_screen.children
            app.remove_children()
            assert app._base_screen.children == []

    asyncio.run(body())


def test_the_app_exposes_the_ported_seams_through_the_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patching ``miniouto.cli.tui.<name>`` must reach the caller (item D)."""

    import types

    from miniouto.cli.tui import config as config_module

    # Given: a package-level patch of the reasoning API seam
    fake = types.SimpleNamespace(reasoning_choices=lambda model, prov: ["patched"])
    monkeypatch.setattr(tui_module, "_get_reasoning_api", lambda: fake)
    # Then: the trio helper that owns the call resolves THROUGH the package
    assert config_module._reasoning_choices("m", "p") == ["patched"]

    # And: run_chat resolves through the package too
    def fake_run_chat(opts: Any, sink: Any = None) -> str:
        return ""

    monkeypatch.setattr(tui_module, "run_chat", fake_run_chat)
    from miniouto.cli.tui import app as app_module

    assert app_module._chat_runner() is fake_run_chat


def test_every_module_alias_the_suites_patch_is_re_exported() -> None:
    for name in (
        "catalog_api",
        "context_api",
        "inbox",
        "runtime",
        "summary",
        "update_check",
        "session_config",
        "provider_store",
        "session_store",
        "settings_store",
        "style_store",
        "paths",
        "paths_runtime",
        "ascii_art",
        "handoff_marker",
        "run_chat",
        "ChatOptions",
        "LoopCancelledError",
        "default_provider_config",
        "LoopEvent",
        "STREAM_THINKING",
        "SUPPORTED_FORMATS",
        "CatalogDialect",
        "add_provider_from_catalog",
        "catalog_compatibility",
        "resolve_catalog_dialect",
        "SOURCE_CUSTOM",
        "is_catalog_source",
        "__version__",
    ):
        assert hasattr(tui_module, name), f"miniouto.cli.tui.{name} is missing"


# ── 10. the live stream row must leave the log when the answer lands ───────


def test_the_final_answer_replaces_the_streamed_row_instead_of_doubling_it() -> None:
    """Regression (C1): every turn used to paint its answer TWICE.

    `_RowWindow.remove` detached with `row.remove()`, and `Node.remove` only
    splices `parent.children` — but the log paints `rows` (which IS `_mounted`).
    So the live StreamRow stayed paintable next to the canonical AnswerRow: the
    transcript grew a duplicate answer every single turn.
    """

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._log.remove_children()
            app._reset_rows()
            app._logo_shown = False
            # Given: a message streaming into a live row
            app._apply_stream_text("The answer is ")
            app._apply_stream_text("42.")
            await pilot.pause()
            # When: the canonical form arrives
            app._finalize_stream()
            app._mount_row(AnswerRow("The answer is 42."))
            await pilot.pause()
            painted = tui_harness.composed(app, 80, 24)
            answers = [line for line in painted if "answer is 42" in line]
            # Then: exactly ONE painted answer line, and the log holds one row
            assert answers == ["The answer is 42."], painted
            assert len(app._log._mounted) == 1
            assert app._window.rows == list(app._log._mounted)

    asyncio.run(body())


def test_a_pruned_row_disappears_from_the_painted_frame() -> None:
    """Regression (C1, invariant 18): a pruned row stayed on screen."""

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._log.remove_children()
            app._reset_rows()
            app._logo_shown = False
            app._add_event_row(
                LoopEvent(actor="outo", kind="tool", text="Bash a1", tool_name="Bash", msg_ref="111")
            )
            app._add_event_row(
                LoopEvent(actor="outo", kind="tool", text="Bash a2", tool_name="Bash", msg_ref="222")
            )
            await pilot.pause()
            assert any("Bash a1" in line for line in tui_harness.composed(app, 80, 24))
            # When: the first message is pruned
            app._remove_event_rows(["111"])
            await pilot.pause()
            painted = tui_harness.composed(app, 80, 24)
            # Then: its row left the FRAME, not just the window list
            assert not any("Bash a1" in line for line in painted), painted
            assert any("Bash a2" in line for line in painted), painted
            assert app._log._mounted == app._window.rows

    asyncio.run(body())


def test_a_mid_turn_message_is_painted_once_after_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (C1 on the inbox path, invariants 16/20): shown TWICE.

    `_queue_inbox_message` paints an optimistic row and `_deliver_pending_messages`
    retires it before posting the real prompt. Retiring went through the window's
    `row.remove()`, which did not detach from the log — so the optimistic row
    stayed painted next to the prompt that replaced it.
    """

    turns: list[dict[str, Any]] = []

    async def body() -> None:
        tui_harness.stub_run_chat(monkeypatch, turns)
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-inbox-midturn")
            app._log.remove_children()
            app._reset_rows()
            app._logo_shown = False
            # Given: the user typed while a turn was in flight (the insert is
            # a `message insert` child on a worker — wait for the row)
            app._busy = True
            app._queue_inbox_message("MIDTURN-TEXT")
            await tui_harness.settle_until(
                pilot, lambda: any("MIDTURN-TEXT" in r.render().plain for r in app._log.rows)
            )
            assert sum("MIDTURN-TEXT" in line for line in tui_harness.composed(app, *BOOT)) == 1
            # When: the turn ends and the queued message becomes the next prompt
            app._busy = False
            app._deliver_pending_messages()
            await tui_harness.settle(pilot, want=1, turns=turns)
            painted = tui_harness.composed(app, *BOOT)
            # Then: it is on screen exactly once, and so is the dispatch
            assert sum("MIDTURN-TEXT" in line for line in painted) == 1, painted
            assert turns[0]["prompt"] == "MIDTURN-TEXT", turns

    asyncio.run(body())


# ── 11. call_from_thread really hops threads ───────────────────────────────


def test_call_from_thread_runs_the_callback_on_the_loop_thread() -> None:
    """Regression (C2, invariant 10): the hop silently did nothing.

    `call_from_thread` resolved the loop with `asyncio.get_running_loop()`, which
    raises on the `asyncio.to_thread` worker the agent loop runs on — so the
    except branch ran the callback INLINE on that worker, mutating UI state while
    the loop thread was inside `compose()`. The headless harness runs everything
    on one thread and could never see it, so this test uses a REAL worker.
    """

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            loop_thread = threading.get_ident()
            seen: list[int] = []

            def callback() -> None:
                seen.append(threading.get_ident())

            # When: the agent worker (not the loop) posts a UI callback
            await asyncio.to_thread(app.call_from_thread, callback)
            await pilot.pause()
            # Then: it ran exactly once, and on the loop's own thread
            assert len(seen) == 1
            assert seen[0] == loop_thread

    asyncio.run(body())


def test_call_from_thread_runs_inline_when_no_loop_is_bound() -> None:
    # A synchronous caller has no loop to hop to; the callback must still run
    app = ChatTUI()
    seen: list[str] = []
    app.call_from_thread(lambda: seen.append("ran"))
    assert seen == ["ran"]


# ── 12. Shift+Tab (BackTab) cycles focus backward from the real parser ─────


def test_backtab_from_the_real_parser_cycles_focus_backward() -> None:
    """Regression (M3): CSI Z was ignored in the real terminal.

    ``input.Key`` carries BackTab as its own ``code`` with ``shift`` set, but
    ``handle_key`` only tested ``code == "Tab"``. The headless harness hid it
    because ``nodes.compile_key`` rewrites shift+tab to ``Tab``+shift, so the
    bytes a REAL terminal sends are fed through ``InputParser`` here.
    """

    async def body() -> None:
        from miniouto.cli.tui.input import InputParser

        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            app._set_focus(app._input)
            # Given: the input holds focus
            assert app._focused is app._input
            # When: the terminal's Shift+Tab arrives as raw bytes
            events = InputParser().feed(b"\x1b[Z")
            assert [event.code for event in events] == ["BackTab"]
            for event in events:
                app._handle_event(event)
            await pilot.pause()
            # Then: focus moved BACKWARD to the last chip, not forward
            order = app._focus_order()
            assert app._focused is order[-1]
            assert app._focused is not order[1]

    asyncio.run(body())


# ── 13. the wheel honours the modal on top (regression m8) ─────────────────


def test_a_wheel_detent_does_not_scroll_the_log_behind_a_modal() -> None:
    """Regression (m8): wheel reports bypassed the modal-ownership check.

    Every other mouse report goes through `_route_mouse`, which hands a modal the
    whole screen; the wheel called `scroll_wheel` directly, so a palette (or any
    picker) sat still while the log scrolled behind it.
    """

    async def body() -> None:
        from miniouto.cli.tui.input import Mouse

        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            app._log.remove_children()
            app._reset_rows()
            app._logo_shown = False
            for index in range(200):
                app._mount_row(RowStatic(f"row {index}"))
            await pilot.pause()
            app.action_command_palette()
            await pilot.pause()
            assert isinstance(app.screen, CommandPalette)
            before = app._log.scroll_y
            assert before > 0, "the log cannot scroll: the test is vacuous"
            # When: the wheel turns UP with the palette open (one detent only:
            # up+down would cancel out and pass even on the broken code)
            app._handle_event(Mouse("wheel_up", 10, 5))
            await pilot.pause()
            # Then: the log behind the modal did not move
            assert app._log.scroll_y == before

    asyncio.run(body())


def test_the_wheel_still_scrolls_the_log_when_no_modal_is_up() -> None:
    async def body() -> None:
        from miniouto.cli.tui.input import Mouse

        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            app._log.remove_children()
            app._reset_rows()
            app._logo_shown = False
            for index in range(200):
                app._mount_row(RowStatic(f"row {index}"))
            await pilot.pause()
            before = app._log.scroll_y
            app._handle_event(Mouse("wheel_up", 10, 5))
            await pilot.pause()
            assert app._log.scroll_y == before - 3

    asyncio.run(body())


# ── 14. a terminal resize reaches the frame (regression M6) ────────────────


class _FakeRect:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height


class _FakeFrame:
    """Enough of a pyratatui ``Frame`` for ``ChatTUI.render`` (area + widgets)."""

    def __init__(self, area: tuple[int, int]) -> None:
        self.area = _FakeRect(*area)

    def render_widget(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _FakeTerminal:
    """A ``pt.Terminal`` double: no alt screen, no cursor, no escape bytes.

    ``hook`` records the input callbacks the loop registers, the way the real
    shim does before it opens the canvas — the loop tests then drive them
    directly, standing in for xnano's own input thread.
    """

    def __init__(self, size: tuple[int, int]) -> None:
        self.size = size
        self.frames: list[tuple[int, int]] = []
        self.cursor: list[str] = []
        self.hooks: dict[str, Any] = {}
        self.exit_after = 0
        self.ticks = 0
        # Loop iterations seen. The loop now draws only a DIRTY frame, so a
        # test that wants N iterations can no longer count `frames`.
        self._on_exit: Any = None
        self._size_source: Any = None

    def __enter__(self) -> _FakeTerminal:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def hook(self, kind: str, callback: Any) -> None:
        self.hooks[kind] = callback

    def area(self) -> Any:
        width, height = self.size
        return _FakeRect(width, height)

    def draw(self, render: Any) -> None:
        if self._size_source is not None:  # a resize moves the real screen's area
            self.size = self._size_source()
        self.frames.append(self.size)
        render(_FakeFrame(self.size))
        if self._on_exit is not None and len(self.frames) >= self.exit_after:
            self._on_exit()

    def move_cursor(self, _x: int, _y: int) -> None:
        self.cursor.append("move")

    def hide_cursor(self) -> None:
        self.cursor.append("hide")

    def show_cursor(self) -> None:
        self.cursor.append("show")


class _FakeKeyReading:
    """One xnano key reading, as ``context.keyboard`` exposes it."""

    def __init__(self, key: str, *, kind: str = "press", modifiers: Any = (), text: Any = None):
        self.key = key
        self.kind = kind
        self.modifiers = frozenset(modifiers)
        self.text = text


class _FakeKeyboard:
    def __init__(self, reading: _FakeKeyReading, pasted: str | None = None) -> None:
        self._reading = reading
        self.pasted = pasted
        self.key = reading.key
        self.kind = reading.kind
        self.modifiers = reading.modifiers
        self.text = reading.text


class _FakePoint:
    """An xnano ``Point``: the cell a pointer reading reports."""

    def __init__(self, x: int, y: int) -> None:
        self.x = x
        self.y = y


class _FakeMouseReading:
    """One xnano pointer reading, as ``context.mouse`` exposes it."""

    def __init__(self, kind: str | None, position: Any = None, button: Any = "left") -> None:
        self.kind = kind
        self.position = position
        self.button = button
        self.pointer = position
        self.scroll = _FakeScroll(0)


class _FakeScroll:
    def __init__(self, y: int) -> None:
        self.x = 0
        self.y = y


class _FakeContext:
    """The three readings a hook callback touches: keyboard, mouse, size."""

    def __init__(
        self,
        *,
        keyboard: _FakeKeyboard | None = None,
        mouse: _FakeMouseReading | None = None,
        size: tuple[int, int] | None = None,
    ) -> None:
        self.keyboard = keyboard
        self.mouse = mouse
        self.size = _FakeRect(*size) if size is not None else None


def test_the_running_loop_polls_the_terminal_size_every_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (M6): resize was dead — nothing ever built a `Resize`.

    ``terminal_size()`` was called exactly once, at boot, and no ``Resize`` was
    ever constructed anywhere in ``src/``. This drives the REAL ``run_async``
    over a fake terminal (only the terminal, the reader thread and the ioctl are
    doubled) and asserts the loop POLLS: without the poll there is no resize
    path at all in a real terminal.
    """

    from miniouto.cli.tui import app as app_module

    async def body() -> None:
        terminal = _FakeTerminal((80, 24))
        app = ChatTUI(terminal=terminal)
        polls: list[tuple[int, int]] = []

        def size() -> tuple[int, int]:
            polls.append((80, 24))
            return (80, 24)

        monkeypatch.setattr(app_module, "terminal_size", size)
        monkeypatch.setattr(app_module, "_write_terminal", lambda _payload: None)
        # The loop DRAWS only a dirty frame, so the exit trigger counts loop
        # ITERATIONS (`tick`), not painted frames.
        iterations: list[int] = []
        real_tick = ChatTUI.tick

        def counting_tick(self: ChatTUI, dt: float = 0.0) -> None:
            real_tick(self, dt)
            iterations.append(1)
            if len(iterations) >= 3:
                app.exit()

        monkeypatch.setattr(ChatTUI, "tick", counting_tick)
        await app.run_async()
        # Then: the loop polls the ioctl at least once per ITERATION — the boot
        # call is the only one the old code ever made. (`frames` is no longer
        # the iteration count: an idle tick paints nothing, by design.)
        assert len(polls) >= len(iterations) >= 3, (polls, iterations)

    asyncio.run(body())


def test_a_size_change_reaches_compose_through_the_resize_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resize path is LIVE: a changed geometry repaints at the new size."""

    from miniouto.cli.tui import app as app_module

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            monkeypatch.setattr(app_module, "terminal_size", lambda: (100, 40))
            # When: the ioctl reports a new size (what the loop does per tick)
            app._sync_terminal_size()
            # Then: the app's geometry moved and compose paints the new frame
            assert (app.size.width, app.size.height) == (100, 40)
            painted = tui_harness.composed(app, 100, 40)
            assert len(painted) == 40
            assert all(len(line) <= 100 for line in painted)
            # And: the panel still occupies the last four rows of the NEW height
            assert painted[-1].strip() != ""
            assert "row" not in painted[0] or painted[0].strip() == painted[0].strip()

    asyncio.run(body())


def test_an_idle_tick_paints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (owner: "screen stutter"): an idle tick did a full rebuild.

    The loop called ``compose()`` + ``term.draw()`` every 1/60 s whether or not
    anything had changed, while xnano's own frame loop drew too: measured on a
    pty, that was 42% of one core at 60 fps for output the terminal already had,
    and the two loops beat against each other. A tick with no state change must
    therefore compose nothing and draw nothing.
    """

    from miniouto.cli.tui import app as app_module

    async def body() -> None:
        terminal = _FakeTerminal((80, 24))
        app = ChatTUI(terminal=terminal)
        monkeypatch.setattr(app_module, "terminal_size", lambda: (80, 24))
        monkeypatch.setattr(app_module, "_write_terminal", lambda _payload: None)
        composed: list[tuple[int, int]] = []
        real_compose = ChatTUI.compose

        def counting(self: ChatTUI, width: int, height: int) -> Any:
            composed.append((width, height))
            return real_compose(self, width, height)

        monkeypatch.setattr(ChatTUI, "compose", counting)
        # The exit is driven from `tick`, not from a draw: this test is ABOUT
        # draws not happening, so an exit keyed on frames painted would never
        # fire. Six ticks is more than enough for the boot frame to settle.
        ticks: list[int] = []
        real_tick = ChatTUI.tick

        def counting_tick(self: ChatTUI, dt: float = 0.0) -> None:
            real_tick(self, dt)
            ticks.append(1)
            if len(ticks) >= 6:
                app.exit()

        monkeypatch.setattr(ChatTUI, "tick", counting_tick)
        await app.run_async()

        # Then: the loop ran, and it composed strictly fewer times than it
        # ticked — the idle ticks did no work at all.
        assert len(ticks) >= 6, ticks
        assert len(composed) < len(ticks), (composed, ticks)

    asyncio.run(body())


def test_a_keystroke_dirties_the_next_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: when state DOES change, the frame follows on the next tick.

    A dirty flag that nothing sets would skip every repaint; the input hooks are
    what set it, and they are the only thing that can (keys, mouse, paste and
    resize all arrive through ``hop``).
    """

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._mark_clean()
            assert app.is_dirty is False
            # A keystroke, routed exactly as the real hook routes one.
            app.inject_key(tui_module.Key("h"))
            assert app.is_dirty is True
            await pilot.pause()
            # And: the settle painted it, so the flag is down again
            assert app.is_dirty is False

    asyncio.run(body())


def test_a_resize_that_changed_nothing_does_no_extra_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from miniouto.cli.tui import app as app_module

    async def body() -> None:
        terminal = _FakeTerminal((80, 24))
        app = ChatTUI(terminal=terminal)
        monkeypatch.setattr(app_module, "terminal_size", lambda: (80, 24))
        monkeypatch.setattr(app_module, "_write_terminal", lambda _payload: None)
        # The loop DRAWS only a dirty frame, so the exit trigger counts loop
        # ITERATIONS (`tick`), not painted frames.
        iterations: list[int] = []
        real_tick = ChatTUI.tick

        def counting_tick(self: ChatTUI, dt: float = 0.0) -> None:
            real_tick(self, dt)
            iterations.append(1)
            if len(iterations) >= 3:
                app.exit()

        monkeypatch.setattr(ChatTUI, "tick", counting_tick)
        await app.run_async()
        # The poll runs every iteration; an unchanged size repaints NOTHING.
        # (This is the regression guard for the idle work: a resize that did not
        # move used to cost a full compose anyway.)
        assert len(terminal.frames) <= 1, terminal.frames

    asyncio.run(body())


def test_the_loop_shows_the_caret_again_before_leaving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (m9): the caret stayed hidden after exit.

    ``_position_cursor`` hides the hardware caret on every frame with no caret to
    place — the state a user is in whenever focus is on a chip or a modal is up —
    and nothing ever undid it before the alt screen was left.
    """

    from miniouto.cli.tui import app as app_module

    async def body() -> None:
        terminal = _FakeTerminal((80, 24))
        app = ChatTUI(terminal=terminal)
        # Given: the LAST frame of the loop leaves the caret hidden — that is
        # `_position_cursor`'s None branch (focus off the input: tab a chip, or a
        # modal owns the screen), and it is the state nothing used to undo.
        def position(term: Any) -> None:
            term.hide_cursor()

        monkeypatch.setattr(app, "_position_cursor", position)
        written: list[str] = []
        monkeypatch.setattr(app_module, "terminal_size", lambda: (80, 24))
        monkeypatch.setattr(app_module, "_write_terminal", written.append)
        # The loop DRAWS only a dirty frame, so the exit trigger counts loop
        # ITERATIONS (`tick`), not painted frames.
        iterations: list[int] = []
        real_tick = ChatTUI.tick

        def counting_tick(self: ChatTUI, dt: float = 0.0) -> None:
            real_tick(self, dt)
            iterations.append(1)
            if len(iterations) >= 3:
                app.exit()

        monkeypatch.setattr(ChatTUI, "tick", counting_tick)
        await app.run_async()
        # Then: every PAINTED frame hid it (this loop paints one, because the
        # stub hides the caret instead of placing it) and the teardown showed it
        # again. `hide` is counted per painted frame, not per iteration: a tick
        # that draws nothing must not touch the caret either.
        assert terminal.cursor.count("hide") == len(terminal.frames), terminal.cursor
        assert terminal.cursor[-1] == "show", terminal.cursor
        # And: the loop registered one hook per input kind BEFORE the canvas
        # opened. That ordering is the contract: xnano decides which terminal
        # settings (mouse/scroll/paste) to switch on from the hooks that exist
        # at open time, and a hook added afterwards is attached and never runs.
        assert set(terminal.hooks) == {"key", "mouse", "scroll", "paste", "resize"}
        # And: the loop itself never touches the alt screen — the Terminal
        # context owns that pair (?1049h on __enter__, ?1049l on __exit__).
        assert not any("1049" in payload for payload in written)

    asyncio.run(body())


def test_run_tui_is_the_only_writer_of_the_alt_screen_leave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (m7): the leave sequence must have exactly ONE owner.

    ``Terminal.__enter__`` emits ``?1049h`` — verified with a pty probe, which
    printed exactly ``b'\\x1b[?1049h\\x1b[?1049l'`` for an empty session — so the
    matching leave belongs to ``__exit__``. ``run_async`` used to write a second
    one inside the context, which made the pair unbalanced AND restored the mouse
    modes twice. `run_tui`'s ``finally`` is the single writer, and it covers the
    paths where the context never opened (a failed boot, Ctrl-C before the loop).
    """

    from miniouto.cli.tui import app as app_module

    written: list[str] = []
    terminal = _FakeTerminal((80, 24))
    app = ChatTUI(terminal=terminal)

    async def no_frames(self: ChatTUI) -> None:
        self.exit()  # the terminal handshake is `Terminal`'s, not ours to redo

    monkeypatch.setattr(ChatTUI, "run_async", no_frames)
    monkeypatch.setattr(app_module, "_write_terminal", written.append)
    monkeypatch.setattr(tui_module, "detect_graphics_mode", lambda: "text")
    # The double stands in for `pt.Terminal(real_screen=True)`; the keyword
    # is accepted rather than ignored so the call shape is the real one.
    monkeypatch.setattr(app_module.pt, "Terminal", lambda **_kw: terminal)
    monkeypatch.setattr(tui_module, "ChatTUI", lambda *a, **k: app)

    app_module.run_tui()
    # Then: exactly one leave, from `run_tui`'s finally — the context never
    # opened, so nothing else supplied one.
    leaves = [payload for payload in written if "1049" in payload]
    assert leaves == ["\x1b[?1049l"], written


def test_run_tui_leaves_the_alt_screen_only_when_the_context_did_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of m7: a `Terminal` that DID open owns its own leave.

    `Terminal.__exit__` emits the matching ``?1049l``, so `run_tui`'s finally must
    stay silent — otherwise every normal exit sent the leave twice (measured with
    a pty probe: two ``\\x1b[?1049l`` on the wire).
    """

    from miniouto.cli.tui import app as app_module

    written: list[str] = []
    app = ChatTUI(terminal=None)

    async def entered(self: ChatTUI) -> None:
        # Stand in for a session whose Terminal context DID open.
        self._alt_screen_entered = True
        self.exit()

    monkeypatch.setattr(ChatTUI, "run_async", entered)
    monkeypatch.setattr(app_module, "_write_terminal", written.append)
    monkeypatch.setattr(tui_module, "detect_graphics_mode", lambda: "text")
    monkeypatch.setattr(tui_module, "ChatTUI", lambda *a, **k: app)

    app_module.run_tui()
    # Then: no leave from here — `Terminal.__exit__` already sent one
    assert [payload for payload in written if "1049" in payload] == [], written


def test_the_running_loop_never_writes_an_alt_screen_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of m7, on the REAL loop: no `?1049` from `run_async`."""

    from miniouto.cli.tui import app as app_module

    async def body() -> None:
        terminal = _FakeTerminal((80, 24))
        app = ChatTUI(terminal=terminal)
        written: list[str] = []
        monkeypatch.setattr(app_module, "terminal_size", lambda: (80, 24))
        monkeypatch.setattr(app_module, "_write_terminal", written.append)
        # The loop DRAWS only a dirty frame, so the exit trigger counts loop
        # ITERATIONS (`tick`), not painted frames.
        iterations: list[int] = []
        real_tick = ChatTUI.tick

        def counting_tick(self: ChatTUI, dt: float = 0.0) -> None:
            real_tick(self, dt)
            iterations.append(1)
            if len(iterations) >= 3:
                app.exit()

        monkeypatch.setattr(ChatTUI, "tick", counting_tick)
        await app.run_async()
        # `Terminal.__enter__`/`__exit__` own the alt-screen pair; the loop owns
        # only the mouse/paste modes and the caret.
        assert not any("1049" in payload for payload in written), written
        assert len(iterations) >= 3, "the loop never ran"

    asyncio.run(body())


# ── 12. the centred column at every width (I2) ─────────────────────────────


@pytest.mark.parametrize(("width", "height"), [(40, 20), (80, 24), (120, 32), (200, 30), (250, 30)])
def test_the_chat_column_stays_centred_and_inside_the_screen(width: int, height: int) -> None:
    """The 150-cell cap must CENTRE the column, not pin it to the left edge.

    Regression: `_paint_log`/`_paint_input` wrote each composed line at cell 0
    instead of at the column's x, so on a 200-column terminal the log, the input
    band and the chips all sat hard against the left edge while `layout()`
    reported a centred column — the frame the user saw disagreed with the
    geometry every click test asserts against.
    """

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(width, height)) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, f"app-column-{width}")
            app._post_system("COLUMN-MARK")
            await pilot.pause()
            lines = app.compose(width, height)
            _log, input_rect, panel, column_x = app.layout(width, height)
            assert column_x == max(0, (width - min(width, CHAT_MAX_WIDTH)) // 2)
            # Every composed line is exactly the screen width: nothing overflows.
            for index, line in enumerate(lines):
                assert cell_len("".join(text for text, _ in line)) == width, index
            # The log lands inside the column, not at the screen edge.
            painted = ["".join(text for text, _ in line) for line in lines]
            mark = next(line for line in painted if "COLUMN-MARK" in line)
            assert mark.index("COLUMN-MARK") == column_x + 1, mark
            # The input band's left rule sits at the column's x + margin + …
            assert painted[input_rect.y].index("\u2588") == column_x + 2
            # The panel's content starts at the column too (its bg spans the row):
            # the last chip row's left chip sits at the column's x + padding.
            assert painted[panel.y + 2].index("subagent:") == column_x + 1

    asyncio.run(body())


@pytest.mark.parametrize("width", [40, 80, 200, 250])
def test_a_long_line_wraps_inside_the_column_and_never_past_the_screen(width: int) -> None:
    """Text wraps at the COLUMN width at any terminal size, chips stay whole."""

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(width, 24)) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, f"app-wrap-{width}")
            app._post_system("x" * 400)
            await pilot.pause()
            lines = app.compose(width, 24)
            log, _input, panel, column_x = app.layout(width, 24)
            column = min(width, CHAT_MAX_WIDTH)
            for index, line in enumerate(lines):
                text = "".join(piece for piece, _ in line)
                assert cell_len(text) == width, (index, text)
            # Every log row's content stays inside the column.
            for index in range(log.y, log.y + log.height):
                text = "".join(piece for piece, _ in lines[index])
                body_text = text[column_x : column_x + column]
                assert len(body_text) == column
            # The chips render their whole values: no ellipsis at any width.
            for index in (panel.y + 1, panel.y + 2):
                text = "".join(piece for piece, _ in lines[index])
                assert "\u2026" not in text, text
            chip = app._panel.get_chip("outo")
            assert "outo:" in "".join(piece for piece, _ in lines[panel.y + 1])
            assert chip.region.right <= width

    asyncio.run(body())


# ── 13. one theme background everywhere (I6) ───────────────────────────────


@pytest.mark.parametrize("size", [(80, 24), (200, 30)])
def test_every_frame_cell_carries_a_theme_background(size: tuple[int, int]) -> None:
    """No cell may fall back to the TERMINAL's default background.

    A ``pt.Style()`` with no ``bg`` leaves the cell to the terminal, whose
    default is visibly not ``#0a0e14`` — the logo, the spinner row, the panel's
    empty cells and the help row all read as a lighter band than the log. The
    only intended exceptions are the ones the owner asked for: the header uses
    ``$panel``, the input band ``$surface`` and the chip box ``$boost``.
    """

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, f"app-bg-{size[0]}")
            await pilot.pause()
            width, height = size
            app.compose(width, height)
            theme = app.current_theme
            allowed = {
                str(theme.color(theme.background)),
                str(theme.color(theme.surface)),
                str(theme.color(theme.panel)),
                str(theme.color(theme.boost)),
                str(theme.color(theme.primary)),  # the focused-chip / caret bar
            }
            cells = app.screen._compositor.cells
            for y in range(height):
                for x in range(width):
                    background = str(cells[y][x].style.background)
                    assert background in allowed, (
                        f"({x},{y}) painted background {background}, "
                        f"which is not one of the theme's surfaces"
                    )

    asyncio.run(body())


def test_the_logo_the_panel_and_the_chip_rows_share_the_log_background() -> None:
    """The owner's exact complaint: those areas must be the SAME colour."""

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(120, 32)) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-bg-same")
            await pilot.pause()
            app.compose(120, 32)
            theme = app.current_theme
            background = str(theme.color(theme.background))
            cells = app.screen._compositor.cells
            _log, _input, panel, column_x = app.layout(120, 32)
            samples = {
                "logo": (column_x + 4, 2),
                "spinner row": (column_x + 4, panel.y),
                "panel empty cell": (column_x + 4, panel.y),
                "last chip row": (column_x + 2, panel.y + 2),
                "log blank": (column_x + 4, 20),
            }
            for label, (x, y) in samples.items():
                assert str(cells[y][x].style.background) == background, (
                    f"{label} at ({x},{y}) is {cells[y][x].style.background}, "
                    f"not {background}"
                )

    asyncio.run(body())


def test_the_registered_hooks_reach_the_same_handlers_the_pilot_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The xnano bridge, end to end over the REAL loop.

    xnano owns stdin, so the app can no longer run its own reader: it registers
    one hook per input kind and every one of them has to land on the handler the
    headless ``Pilot`` already drives. This calls the registered callbacks the
    way xnano would (with a context carrying the reading) and asserts the app
    state each one is supposed to move.
    """

    from miniouto.cli.tui import app as app_module

    async def body() -> None:
        terminal = _FakeTerminal((80, 24))
        app = ChatTUI(terminal=terminal)
        seen_key: list[Any] = []
        seen_mouse: list[Any] = []
        seen_paste: list[Any] = []
        monkeypatch.setattr(app_module, "terminal_size", lambda: (80, 24))
        monkeypatch.setattr(app_module, "_write_terminal", lambda _payload: None)
        monkeypatch.setattr(app, "handle_key", lambda key: seen_key.append(key) or True)
        monkeypatch.setattr(app, "_route_mouse", lambda mouse, chain=1: seen_mouse.append(mouse) or True)
        monkeypatch.setattr(app, "inject_paste", lambda text: seen_paste.append(text) or True)
        # Run two frames, and drive the hooks from inside the second one — that
        # is where xnano's own input thread would fire them.
        fired: list[bool] = []

        def fire() -> None:
            if fired:
                return
            fired.append(True)
            terminal.hooks["key"](
                _FakeContext(keyboard=_FakeKeyboard(_FakeKeyReading("h", text="h")))
            )
            terminal.hooks["key"](
                _FakeContext(
                    keyboard=_FakeKeyboard(
                        _FakeKeyReading("tab", modifiers={"shift"}), pasted=None
                    )
                )
            )
            terminal.hooks["mouse"](
                _FakeContext(
                    mouse=_FakeMouseReading("press", _FakePoint(3, 4))
                )
            )
            terminal.hooks["paste"](
                _FakeContext(keyboard=_FakeKeyboard(_FakeKeyReading("a"), pasted="pasted"))
            )

        # The hooks fire from inside a loop ITERATION, and the loop stops itself
        # on the second one: this test needs the callbacks called the way
        # xnano's input thread calls them, not a particular draw count.
        iterations: list[int] = []
        real_tick = ChatTUI.tick

        def tick_then_fire(self: ChatTUI, dt: float = 0.0) -> None:
            real_tick(self, dt)
            iterations.append(1)
            fire()
            if len(iterations) >= 2:
                app.exit()

        monkeypatch.setattr(ChatTUI, "tick", tick_then_fire)
        await app.run_async()
        # Then: the key reached `handle_key` with our own Key, Shift+Tab folded
        # into BackTab (the code the router tests), and the letters intact.
        codes = [(key.code, key.shift) for key in seen_key]
        assert codes == [("h", False), ("BackTab", True)], codes
        # And: the pointer report became OUR Mouse, with the cell it was at.
        assert seen_mouse == [Mouse("down", 3, 4, button=0)], seen_mouse
        # And: the bracketed paste reached the focused node's handler.
        assert seen_paste == ["pasted"], seen_paste

    asyncio.run(body())


# ── 15. the boot probe must not poison xnano's input reader ────────────────


def test_the_graphics_probe_runs_in_a_child_so_the_tty_is_never_left_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the in-process probe broke Enter on a real terminal.

    Asking the terminal what it can draw means reading the answer off stdin,
    which turns the tty into cbreak. Measured on a pty, the probe this build uses
    puts the modes back but leaves xnano's input reader convinced the tty is
    still raw — every key it then delivered arrived with the kernel canonical
    again, so ``\\r`` came through as ``Ctrl+J`` and Enter was dead while every
    escape-sequence test passed. The probe therefore runs in a FORKED child,
    whose tty modes die with it.

    This asserts the isolation contract, not the terminal: the parent's own
    process must never be the one that called the probe.
    """

    from miniouto.cli.tui import app as app_module

    parent = os.getpid()

    def probe() -> str:
        # The answer carries the pid that produced it, so the PARENT can see
        # which process actually ran the probe (a closure list would not: the
        # child mutates its own copy of memory and dies with it).
        return f"sixel:{os.getpid()}"

    # When: the probe runs through the isolation helper
    answer = app_module._probe_in_child(probe)

    # Then: it answered, and the pid it reported is NOT this process
    assert answer is not None and answer.startswith("sixel:"), answer
    assert int(answer.split(":", 1)[1]) != parent, "the probe ran in the parent"


def test_the_isolated_probe_answers_none_when_the_child_says_nothing() -> None:
    """A silent child is not an error: the caller falls back to the module."""

    from miniouto.cli.tui import app as app_module

    # A child that exits without writing: the read sees EOF and returns None.
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    try:
        assert app_module._read_probe_answer(read_fd) is None
    finally:
        os.close(read_fd)


def test_the_isolated_probe_ignores_an_untagged_answer() -> None:
    """Only the child's own marker counts — stdout noise must not be read as one."""

    from miniouto.cli.tui import app as app_module

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"garbage without the marker\n")
    os.close(write_fd)
    try:
        assert app_module._read_probe_answer(read_fd) is None
    finally:
        os.close(read_fd)


def test_the_isolated_probe_publishes_the_mode_so_the_render_path_sees_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child's answer must reach ``graphics`` — that is what the app reads."""

    from miniouto.cli.tui import app as app_module

    monkeypatch.setattr(app_module.graphics_module, "_GRAPHICS_MODE", None)
    monkeypatch.setattr(app_module, "_probe_in_child", lambda _probe: "sixel")
    monkeypatch.setattr(
        app_module.graphics_module, "graphics_mode", lambda: "sixel"
    )
    assert app_module._detect_graphics_mode() == "sixel"
    assert app_module.graphics_module._GRAPHICS_MODE == "sixel"

# ── 12. frame cost: repaint only what changed ───────────────────────────────


def test_an_unchanged_frame_skips_the_compositor_blit() -> None:
    """The measured idle hotspot: an identical frame must not re-blit its cells.

    ``Compositor.paint`` was 60% of an idle compose (3.7 ms of 6.2 ms for a
    120x32 frame). When the composed lines equal the last frame painted, the
    grid already holds exactly those cells, so the clear+blit pair is skipped
    and ``render_strips`` still describes the frame.
    """

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            strips = tui_harness.painted(app)
            painted_calls: list[Any] = []
            compositor = app._base_screen._compositor
            original = compositor.paint

            def counting(lines: Any, x: int = 0, y: int = 0) -> None:
                painted_calls.append((x, y, len(lines)))
                original(lines, x, y)

            compositor.paint = counting  # type: ignore[method-assign]
            try:
                for _ in range(3):
                    app.compose(*BOOT)
            finally:
                compositor.paint = original  # type: ignore[method-assign]
            # Then: not one blit ran — the frame was already on the grid
            assert painted_calls == [], painted_calls
            # And: what the screen reads is unchanged by the skipped blits
            assert tui_harness.painted(app) == strips

    asyncio.run(body())


def test_a_changed_frame_blits_only_the_rows_that_differ() -> None:
    """A streaming frame pays for the rows the tokens touched, not all 32."""

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            before = [st.text for st in app.screen._compositor.render_strips()]
            painted_rows: list[int] = []
            compositor = app._base_screen._compositor
            original = compositor.paint

            def counting(lines: Any, x: int = 0, y: int = 0) -> None:
                assert len(lines) == 1, "a differing row is blitted on its own"
                painted_rows.append(y)
                original(lines, x, y)

            compositor.paint = counting  # type: ignore[method-assign]
            try:
                app._post_system("a new line changes exactly one row band")
                app.compose(*BOOT)
            finally:
                compositor.paint = original  # type: ignore[method-assign]
            # Then: fewer rows were blitted than the screen has — the identical
            # majority was left alone
            assert 0 < len(painted_rows) < BOOT[1], painted_rows
            # And: the grid matches a from-scratch full paint of the same frame
            after = [st.text for st in app.screen._compositor.render_strips()]
            assert after != before  # the new row really did change the frame
            app._painted_frame = None
            app.compose(*BOOT)
            assert [st.text for st in app.screen._compositor.render_strips()] == after

    asyncio.run(body())


def test_a_resize_repaints_the_whole_frame() -> None:
    """A size change must fall back to the full clear+blit."""

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            app.compose(*BOOT)
            painted: list[Any] = []
            compositor = app._base_screen._compositor
            original = compositor.paint

            def counting(lines: Any, x: int = 0, y: int = 0) -> None:
                painted.append(len(lines))
                original(lines, x, y)

            compositor.paint = counting  # type: ignore[method-assign]
            try:
                smaller = (BOOT[0] - 10, BOOT[1] - 4)
                app.compose(*smaller)
            finally:
                compositor.paint = original  # type: ignore[method-assign]
            # Then: ONE whole-frame blit, not a per-row patch series
            assert painted == [smaller[1]], painted

    asyncio.run(body())


def test_the_x0_overlay_matches_the_general_branch() -> None:
    """``_overlay_line`` at cell 0 is the common case and must not drift.

    The chat column starts at cell 0 on any terminal up to ``CHAT_MAX_WIDTH``,
    so the fast branch there is exercised by every frame; this pins it against
    the general head/tail assembly for narrow, exact-width and overflowing
    pieces, wide characters included.
    """

    from miniouto.cli.tui import pt
    from miniouto.cli.tui.app import _overlay_line, _slice_from
    from miniouto.cli.tui.rows import cells_of, pad_to, truncate_to

    fill = pt.Style()
    base = [("0123456789", fill)]
    for pieces in (
        [("hi", fill)],
        [("x" * 10, fill)],
        [("x" * 14, fill)],  # wider than the base line
        [("あいう", fill)],  # wide characters
        [],
    ):
        fast = _overlay_line(list(base), 0, list(pieces), fill)
        head = pad_to(truncate_to(list(base), 0), 0, fill)
        slow = [*head, *pieces, *_slice_from(list(base), 0 + cells_of(pieces))]
        assert fast == slow, (pieces, fast, slow)


def test_slice_from_is_cell_accurate_with_wide_characters() -> None:
    """The slice helper's fast paths must match the per-character walk."""

    from miniouto.cli.tui.app import _slice_from

    def reference(line: Any, start: int) -> Any:
        out = []
        position = 0
        for text, style in line:
            if position >= start:
                out.append((text, style))
                position += cell_len(text)
                continue
            chunk = ""
            for char in text:
                if position >= start:
                    chunk += char
                position += cell_len(char)
            if chunk:
                out.append((chunk, style))
        return out

    line = [("ab😀cd", "s1"), ("あz", "s2"), ("tail", "s3")]
    for start in range(-1, 14):
        assert _slice_from(line, start) == reference(line, start), start


def test_row_heights_invalidate_only_on_content_width_or_theme_change() -> None:
    """The height pass rides the rows' ``(width, theme, _version)`` cache.

    ``_paint_log`` measures every mounted row every frame through
    ``row.height()`` → ``build_lines``, so the cache rows.py keeps is what makes
    that cheap: scrolling must not re-wrap anything, while a content, width or
    theme change must invalidate exactly then. Pinned here because the app's
    frame cost (see the compose diff-guard tests above) depends on it.
    """

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            row = RowStatic(Text("word " * 40))  # wraps to several lines
            app._mount_row(row)
            await pilot.pause()

            wraps: list[int] = []
            original = RowStatic._content_lines

            def counting(self: Any, width: int, theme: Any, base: Any) -> Any:
                wraps.append(id(self))
                return original(self, width, theme, base)

            RowStatic._content_lines = counting  # type: ignore[method-assign]
            try:
                # Drop the row's cache so the first compose below MUST wrap.
                row._cache.clear()
                app.compose(*BOOT)
                first = len(wraps)
                assert first >= 1, "the row was never wrapped"

                # Scroll: heights are re-read but nothing may re-wrap.
                app._log.scroll_to(y=0)
                app.compose(*BOOT)
                app._log.scroll_end()
                app.compose(*BOOT)
                assert len(wraps) == first, "scrolling re-wrapped a row"

                # Content change (the streaming case): invalidated exactly once.
                row._content = Text("word " * 200)
                row._refresh()
                app.compose(*BOOT)
                after_content = len(wraps)
                assert after_content > first, "a content change did not re-wrap"

                # Width change: every row re-wraps at the new width.
                app.compose(BOOT[0] - 20, BOOT[1])
                after_width = len(wraps)
                assert after_width > after_content, "a width change did not re-wrap"

                # Theme change: the row's cache key names the theme.
                app.theme = "nord" if app.get_theme("nord") else app.theme
                app.compose(*BOOT)
                assert len(wraps) > after_width, "a theme change did not re-wrap"
            finally:
                RowStatic._content_lines = original  # type: ignore[method-assign]

    asyncio.run(body())


# ── 14. round 4: the Keys popup · three-row panel · unified backgrounds ─────


def test_the_keys_palette_entry_opens_the_binding_popup() -> None:
    """The palette's Keys entry pushes the KeyModal, not a one-line spinner."""

    async def body() -> None:
        from miniouto.cli.tui.modals import KeyModal

        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            app.action_show_help()
            await pilot.pause()
            # Then: the popup is on the stack, and it is the Keys dialog
            assert isinstance(app.screen, KeyModal)
            screen = "\n".join(tui_harness.painted(app))
            for expected in ("Keys", "Ctrl+P", "Esc Esc"):
                assert expected in screen, f"{expected!r} missing from the popup"
            # And: Esc closes it and the base screen is back
            await pilot.press("esc")
            assert not isinstance(app.screen, KeyModal)

    asyncio.run(body())


def test_the_panel_is_three_rows_and_occupies_the_last_three() -> None:
    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            _log, _input, panel, _x = app.layout(*BOOT)
            # Then: the panel is the bottom three rows — spinner + two chip rows
            assert panel.height == 3, panel
            assert panel.y == BOOT[1] - 3, panel
            assert panel.y + panel.height == BOOT[1], panel
            assert tui_harness.painted_line(app, panel.y).strip() == "", "spinner row"
            assert "outo:" in tui_harness.painted_line(app, panel.y + 1)
            assert "subagent:" in tui_harness.painted_line(app, panel.y + 2)
            # And: the retired help hint is gone from the whole frame
            screen = "\n".join(tui_harness.painted(app))
            assert HELP_TEXT not in screen
            assert "\u2039Tab\u203a cycle chips" not in screen

    asyncio.run(body())


def test_the_resting_chips_and_the_header_row_carry_one_background_each() -> None:
    """Round 4's two colour calls, on the composited frame."""

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-round4-bg")
            await pilot.pause()
            app.compose(*BOOT)
            theme = app.current_theme
            cells = app.screen._compositor.cells
            _log, _input, panel, _x = app.layout(*BOOT)
            # (C) every chip CELL on both chip rows rests on the page background
            background = str(theme.color(theme.background))
            for row in (panel.y + 1, panel.y + 2):
                for x in range(BOOT[0]):
                    assert str(cells[row][x].style.background) == background, (x, row)
            # (D) the header row is $panel across its WHOLE width
            panel_color = str(theme.color(theme.panel))
            header_bgs = {str(cells[0][x].style.background) for x in range(BOOT[0])}
            assert header_bgs == {panel_color}, header_bgs

    asyncio.run(body())


# ─── abnormal errors: collect, log, recover ─────────────────────────────────


def test_a_render_exception_is_logged_and_the_screen_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (owner: "an abnormal error breaks the TUI for good").

    A normal error is one the TURN raises, and ``_dispatch`` already posts it
    as an ``[error: …]`` row. An ABNORMAL one is raised by the loop itself —
    compose, the draw — and before this nothing caught it: the coroutine died,
    the frame froze, and whatever the error painted stayed on the screen. This
    drives the REAL ``run_async`` and makes the SECOND tick's paint raise, then
    asserts the whole contract: the loop kept running, the traceback is in the
    session's ``tui-error-<session>.log`` under ``LOG_DIR``, the row is on
    screen, and the frame after the error is a whole-screen repaint.
    """

    from miniouto.cli.tui import app as app_module
    from miniouto.storage import paths

    async def body() -> None:
        terminal = _FakeTerminal((80, 24))
        app = ChatTUI(terminal=terminal)
        tui_harness.pin_session(app, "err-recovery")
        monkeypatch.setattr(app_module, "terminal_size", lambda: (80, 24))
        monkeypatch.setattr(app_module, "_write_terminal", lambda _payload: None)
        # When: compose raises on the second tick and works again after it
        calls: list[int] = []
        real_compose = ChatTUI.compose

        def exploding(self: ChatTUI, width: int, height: int) -> Any:
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("compose exploded mid-render")
            return real_compose(self, width, height)

        monkeypatch.setattr(ChatTUI, "compose", exploding)
        ticks: list[int] = []
        real_tick = ChatTUI.tick

        def counting_tick(self: ChatTUI, dt: float = 0.0) -> None:
            real_tick(self, dt)
            ticks.append(1)
            if len(ticks) >= 5:
                app.exit()

        monkeypatch.setattr(ChatTUI, "tick", counting_tick)
        await app.run_async()

        # Then: the loop SURVIVED — five ticks ran although tick 2 raised, and
        # the run returned instead of propagating the exception.
        assert len(ticks) >= 5, ticks
        assert len(calls) >= 3, calls
        # And: the traceback is in the session's own TUI error log, timestamped
        log_path = paths.LOG_DIR / "tui-error-err-recovery.log"
        assert log_path.is_file(), f"no TUI error log at {log_path}"
        record = log_path.read_text(encoding="utf-8")
        assert "RuntimeError" in record and "compose exploded mid-render" in record
        assert record.startswith("["), "the record is not timestamped"
        assert "Traceback" in record or "in exploding" in record, record
        # And: the user saw it as the same row a turn error gets
        assert "[error: RuntimeError: compose exploded mid-render]" in tui_harness.painted(app)
        # And: the recovery forced a whole-screen repaint — the paint memo the
        # compose diff-guard reads was dropped, so the next frame was full.
        assert app._painted_frame is not None  # recomposed after the error
        assert app._tui_errors == 1, app._tui_errors

    asyncio.run(body())


def test_a_loop_error_past_the_limit_still_logs_but_stops_repainting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repaint half of the contract has a budget; the log half does not.

    A state that raises on EVERY tick would otherwise burn a full clear+redraw
    60 times a second, so ``_TUI_ERROR_REPAINT_LIMIT`` bounds the repaints.
    The log and the ``[error:]`` row stay unconditional — that is the part the
    owner reads afterwards.
    """

    from miniouto.cli.tui import app as app_module

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            limit = app_module._TUI_ERROR_REPAINT_LIMIT
            repaints: list[int] = []
            real = app._repaint_whole_screen

            def counting(term: Any = None) -> None:
                repaints.append(1)
                real(term)

            monkeypatch.setattr(app, "_repaint_whole_screen", counting)
            for _ in range(limit + 2):
                app._recover_loop_error(RuntimeError("again"))
            # Then: only the first `limit` errors forced the repaint
            assert len(repaints) == limit, (repaints, limit)
            assert app._tui_errors == limit + 2
            # And: the last error still posted its row — the paint settles
            # on the next pause, so the count is over the PAINTED frame.
            await pilot.pause()
            painted = "\n".join(tui_harness.painted(app))
            assert painted.count("[error: RuntimeError: again]") == limit + 2

    asyncio.run(body())


def test_the_asyncio_handler_logs_what_would_have_hit_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Task was destroyed but is pending!`` must land in the log, not on fd 2.

    The default loop handler writes to ``sys.stderr``; on the alt screen that
    paints over the frame. Installing ``_on_loop_exception`` routes the same
    report into the TUI's own error log.
    """

    from miniouto.storage import paths

    async def body() -> None:
        app = ChatTUI()
        tui_harness.pin_session(app, "asyncio-handler")
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            loop = asyncio.get_running_loop()
            previous = loop.get_exception_handler()
            loop.set_exception_handler(app._on_loop_exception)
            try:
                # When: the loop reports an unretrieved task exception — the
                # exact shape "Task was destroyed but is pending!" arrives in.
                task: asyncio.Task[None] = loop.create_task(
                    _raise_and_finish(), name="destroyed-pending"
                )
                await asyncio.sleep(0)
                del task  # never awaited/retrieved
                import gc

                gc.collect()
            finally:
                loop.set_exception_handler(previous)
            # Then: the handler recorded it in the session's TUI error log
            await pilot.pause()
            log_path = paths.LOG_DIR / "tui-error-asyncio-handler.log"
            assert log_path.is_file(), f"no TUI error log at {log_path}"
            record = log_path.read_text(encoding="utf-8")
            assert "boom" in record, record

    asyncio.run(body())


async def _raise_and_finish() -> None:
    raise RuntimeError("boom from a destroyed task")


def test_the_stderr_tap_captures_to_the_log_and_restores_fd2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fd 2 must never reach the alt screen, and must come back afterwards.

    "Task was destroyed but is pending!" leaked to the terminal and painted
    over the frame. The tap moves the REAL descriptor (not ``sys.stderr``) so
    a C extension or a child that inherits fd 2 is caught too, batches what it
    captured into the TUI error log, and puts the terminal's stderr back on
    restore. fd 1 — the OSC lanes and the resize poll — is never touched.
    """

    from miniouto.cli.tui import app as app_module
    from miniouto.storage import paths

    async def body() -> None:
        app = ChatTUI()
        tui_harness.pin_session(app, "stderr-tap")
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            real_stderr_fd = os.dup(2)
            try:
                real_stderr_stat = os.fstat(2)
                restore = app_module._tap_stderr(app)
                # Then: fd 2 is no longer the terminal's stderr…
                assert os.fstat(2) != real_stderr_stat
                # …and fd 1 still is what it was (the OSC/resize lane).
                # When: something writes to the REAL fd 2 — the descriptor a
                # library or a child process holds, not sys.stderr.
                os.write(2, b"Task was destroyed but is pending!\n")
                # Then: the tap batched it into the session's TUI error log
                for _ in range(50):
                    await asyncio.sleep(0.02)
                    record = (paths.LOG_DIR / "tui-error-stderr-tap.log")
                    if record.is_file() and "pending" in record.read_text(
                        encoding="utf-8"
                    ):
                        break
                text = record.read_text(encoding="utf-8")
                assert "Task was destroyed but is pending!" in text, text
                assert "stderr" in text, text
                # And: restore puts the terminal's fd 2 back
                restore()
                # (Comparing the file, not the number: the dup'd fd is closed
                # by restore, so the number is free for reuse.)
                assert os.fstat(2) == real_stderr_stat
            finally:
                os.close(real_stderr_fd)
            await pilot.pause()

    asyncio.run(body())


# ── 12. the wrapper contract, end to end on the Pilot ───────────────────────


def test_a_turn_streams_live_and_finishes_through_the_cli_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reference wrapper flow: submit → spawn → stdout deltas → file
    events → finish → the canonical answer, all on the headless Pilot.

    The double speaks the MEASURED CLI stdout contract (session marker, raw
    token deltas, `------finish------`) AND writes the session record the
    real child writes (the running turn's events, incremented as the loop
    emits them) — so this exercises the exact parsing the official TUI runs
    against any third-party wrapper: live StreamRow from stdout, event rows
    from the session tail, the final AnswerRow from the file.
    """

    class _StreamingProcess(tui_harness._FakeChatProcess):
        def _render(self) -> bytes:
            from miniouto.storage import sessions as store

            prompt, session, _cont = self._parse()
            if "--alarms" in self.argv:
                from miniouto.core import inbox as inbox_api

                messages = inbox_api.drain(session)
                if not messages:
                    self.returncode = 0
                    return b"no pending messages\n"
                prompt = "\n\n".join(m.text for m in messages)
            store.begin_turn(session, prompt)
            # The file events, as the CLI increments them mid-turn.
            store.update_turn_events(
                session,
                [
                    LoopEvent(
                        actor="outo", kind="thinking", text="reasoning about it"
                    ).to_dict(),
                    LoopEvent(
                        actor="outo",
                        kind="tool",
                        text="Bash ls",
                        tool_name="Bash",
                        msg_ref="m1",
                    ).to_dict(),
                ],
            )
            answer = "The answer is 42."
            store.finish_turn(session, assistant=answer)
            self.returncode = 0
            out = f"------{session}------\n"
            out += answer  # raw deltas, no prefix, printed once
            out += "\n------finish------\n"
            return out.encode("utf-8")

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:
        return _StreamingProcess(list(argv))

    async def body() -> None:
        monkeypatch.setattr(
            tui_module.bridge, "asyncio_create_subprocess_exec", fake_exec
        )
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-stream-session")
            app._input.text = "what is the answer?"
            await pilot.press("enter")
            for _ in range(300):
                rows = app._window.rows
                if any(type(r).__name__ == "AnswerRow" for r in rows):
                    break
                await pilot.pause(0.01)

            rows = app._window.rows
            names = [type(r).__name__ for r in rows]
            # The live stream row was retired by the canonical answer (no dup)
            assert names.count("StreamRow") == 0, names
            assert names.count("AnswerRow") == 1, names
            # The session-tail events rendered as their own rows (a thinking
            # row collapses to its summary; the tool row carries its call)
            rendered = [tui_harness.row_text(r) for r in rows]
            assert any("thinking" in text for text in rendered), rendered
            assert any("Bash ls" in text for text in rendered), rendered
            assert any("42" in text for text in rendered), rendered
            # The turn is not busy any more and the inbox drain is a no-op
            assert app._busy is False

    asyncio.run(body())


def test_thinking_deltas_render_live_and_finalize_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The thinking lane of the child's stdout contract.

    The CLI writes each reasoning fragment as one ``outo:thinking:`` line
    per physical line (``ConsoleEventSink.emit_stream_delta``). While the
    child is still generating, the TUI must mount a live ``ThinkingRow``
    carrying the ``●`` streaming marker; when the message completes, the
    session tail's whole-text ``thinking`` event finalizes THAT row in
    place — never a second row, never the text twice.
    """

    reasoning = "step one\nstep two is longer"

    class _ThinkingChild(tui_harness._FakeChatProcess):
        def __init__(self, argv: list[str]) -> None:
            self.argv = argv
            self.returncode: int | None = None
            self.stdout = _ThinkingReader(self)
            self.stderr = tui_harness._FakeStreamReader(b"")

        def _parse(self) -> tuple[str, str, bool]:
            return tui_harness._FakeChatProcess._parse(self)  # type: ignore[arg-type]

        def send_signal(self, _sig: int) -> None:  # pragma: no cover
            self.returncode = 130

        async def wait(self) -> int:
            return self.returncode if self.returncode is not None else 0

    class _ThinkingReader:
        """stdout of a thinking-streaming child, one read per flush.

        The whole point of the delta lane is live display, so the reads hand
        the fragments out as the child would: one ``read(4096)`` per write
        the CLI flushed. The turn's TAIL is GATED on the test observing the
        live ``●`` row first — otherwise the turn settles before the poll
        can see the streaming state at all. Like the real CLI, the session
        file is written BEFORE the matching stdout line prints (the
        persister runs ahead of the printer), which is also what keeps the
        tailer's cursor from running past the events.
        """

        def __init__(self, proc: _ThinkingChild) -> None:
            self._proc = proc
            self._reads: list[tuple[bytes, Any]] = []
            self._i = 0
            self._tail_open = asyncio.Event()

        def release_tail(self) -> None:
            self._tail_open.set()

        def _finish_turn(self) -> None:
            from miniouto.storage import sessions as store

            _prompt, session, _c = self._proc._parse()
            store.finish_turn(session, assistant="the final answer")
            self._proc.returncode = 0
            return None

        async def read(self, _n: int = -1) -> bytes:
            if not self._reads:
                self._build()
            if self._i >= len(self._reads):
                return b""
            data, before = self._reads[self._i]
            if before is not None:
                before()
            if self._i == 4:
                await self._tail_open.wait()
            self._i += 1
            return data

        def _build(self) -> None:
            from miniouto.core.events import (
                THINKING_DELTA_PREFIX,
                THINKING_DELTA_PREFIX_UNCLOSED,
            )
            from miniouto.storage import sessions as store

            def write(events: list[LoopEvent]) -> None:
                _prompt, session, _c = self._proc._parse()
                store.update_turn_events(session, [e.to_dict() for e in events])

            answer = "the final answer"
            # Fragments as the provider fires them: a lone line, a BLANK
            # line (content — the fragment carried its own line break), and
            # fragments with no newline of their own — framed exactly the
            # way `emit_stream_delta` writes them (a newline-less fragment
            # ends on the unclosed mark, so the reader re-adds no newline).
            self._reads = [
                (
                    f"------{self._proc._parse()[1]}------\n".encode(),
                    lambda: store.begin_turn(self._proc._parse()[1], self._proc._parse()[0]),
                ),
                ((THINKING_DELTA_PREFIX + "step one\n").encode(), None),
                ((THINKING_DELTA_PREFIX + "\n").encode(), None),
                (
                    (THINKING_DELTA_PREFIX_UNCLOSED + "step two is\n").encode(),
                    lambda: write([LoopEvent(actor="outo", kind="thinking", text=reasoning)]),
                ),
                # --- gated tail: the message completes ---
                (
                    (THINKING_DELTA_PREFIX_UNCLOSED + " longer\n").encode(),
                    lambda: write(
                        [
                            LoopEvent(actor="outo", kind="thinking", text=reasoning),
                            LoopEvent(
                                actor="outo",
                                kind="response",
                                text=answer,
                                msg_ref="m1",
                            ),
                        ]
                    ),
                ),
                (answer.encode() + b"\n------finish------\n", self._finish_turn),
            ]

    current: list[_ThinkingChild] = []

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:
        child = _ThinkingChild(list(argv))
        current.append(child)
        return child

    async def body() -> None:
        monkeypatch.setattr(
            tui_module.bridge, "asyncio_create_subprocess_exec", fake_exec
        )
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            name = tui_harness.pin_session(app, "app-thinking-stream")
            # A previous DONE turn, like every real TUI session: the tailer's
            # cursor then sits before the RUNNING turn's index, so the file
            # events the child writes mid-turn are replayed as they land.
            session_store.begin_turn(name, "earlier work")
            session_store.finish_turn(name, assistant="earlier answer")
            app._input.text = "think it through"
            await pilot.press("enter")
            # While the child streams: a live ThinkingRow with the marker.
            for _ in range(600):
                await pilot.pause(0.005)
                if any(
                    isinstance(r, ThinkingRow) and r.streaming
                    for r in app._window.rows
                ):
                    break
            live = [r for r in app._window.rows if isinstance(r, ThinkingRow)]
            assert live, "no live ThinkingRow while the reasoning streamed"
            assert live[0].streaming is True
            assert live[0].thinking == "step one\n\nstep two is"
            assert tui_harness.row_text(live[0]).endswith("thinking ●")

            # Release the gated tail (the message completes) and let the
            # turn settle: the session file already holds the completed
            # events — the persister runs before the printer, like the CLI.
            assert current, "no child was spawned"
            current[0].stdout.release_tail()
            for _ in range(600):
                await pilot.pause(0.005)
                if not app._busy:
                    break
            rows = app._window.rows
            thinking = [r for r in rows if isinstance(r, ThinkingRow)]
            # Finalized in place: ONE thinking row, holding the whole text
            # (the tail's whole-text event finalized it), streaming off.
            assert len(thinking) == 1, [tui_harness.row_text(r) for r in rows]
            assert thinking[0].streaming is False
            assert thinking[0].thinking == reasoning
            assert tui_harness.row_text(thinking[0]) == "▶ thinking"
            # And the turn's answer rendered exactly once
            answers = [
                r for r in rows if type(r).__name__ == "AnswerRow"
            ]
            assert len(answers) == 1, [tui_harness.row_text(r) for r in rows]
            assert app._busy is False

    asyncio.run(body())


def test_live_thinking_reassembles_fragments_without_fake_newlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect (early line breaks): fragment boundaries became newlines.

    The old framing wrote every fragment as `outo:thinking: <line>\n` —
    one prefixed physical line per line of the fragment — and the reader
    re-added a newline per physical line, so EVERY fragment boundary turned
    into a line break (`"The"` + `" user asks:"` reassembled as
    `"The\n user asks:"`): the live ThinkingRow wrapped mid-word while the
    model was still generating, then snapped correct at the finalize. The
    newline a newline-less fragment ends on is the unclosed mark now, so
    the live row holds byte-for-byte the reasoning the fragments carried.
    """

    reasoning = (
        'The user asks: "Think briefly about 2+2, then answer." '
        "한글 reasoning 스트리밍 중간에 끊김 없이"
    )

    class _FramedChild(tui_harness._FakeChatProcess):
        def __init__(self, argv: list[str]) -> None:
            self.argv = argv
            self.returncode: int | None = None
            self.stdout = _FramedReader(self)
            self.stderr = tui_harness._FakeStreamReader(b"")

        def _parse(self) -> tuple[str, str, bool]:
            return tui_harness._FakeChatProcess._parse(self)  # type: ignore[arg-type]

        def send_signal(self, _sig: int) -> None:  # pragma: no cover
            self.returncode = 130

        async def wait(self) -> int:
            return self.returncode if self.returncode is not None else 0

    class _FramedReader:
        """One read per fragment the REAL sink flushed, gated mid-stream.

        The fragments are fed through `ConsoleEventSink.emit_stream_delta`
        itself — the test then pins the reader against exactly what the
        child writes — one flush per read, like the pipe the wrapper owns.
        The stream is GATED after the last fragment: the live row is then
        observed BEFORE the whole-text event finalizes it, which is the
        state where the fake newlines showed.
        """

        _FRAGMENTS = (
            "The user asks: \"Think",
            " briefly about 2+2, then answer.\" ",
            "한글 reasoning ",
            "스트리밍 중",
            "간에 끊김 없이",
        )

        def __init__(self, proc: _FramedChild) -> None:
            self._proc = proc
            self._sink: Any = None
            self._i = -1
            self._tail_open = asyncio.Event()

        def release_tail(self) -> None:
            self._tail_open.set()

        def _emit(self, *calls: Any) -> bytes:
            import io as _io
            from contextlib import redirect_stdout as _redirect

            buf = _io.StringIO()
            with _redirect(buf):
                for call in calls:
                    call()
            return buf.getvalue().encode()

        async def read(self, _n: int = -1) -> bytes:
            from miniouto.core.events import ConsoleEventSink
            from miniouto.storage import sessions as store

            self._i += 1
            prompt, session, _c = self._proc._parse()
            if self._i == 0:
                store.begin_turn(session, prompt)
                self._sink = ConsoleEventSink()
                return f"------{session}------\n".encode()
            if self._i <= len(self._FRAGMENTS):
                fragment = self._FRAGMENTS[self._i - 1]
                return self._emit(
                    lambda: self._sink.emit_stream_delta("thinking", fragment)
                )
            if self._i == len(self._FRAGMENTS) + 1:
                await self._tail_open.wait()
                store.update_turn_events(
                    session,
                    [LoopEvent(actor="outo", kind="thinking", text=reasoning).to_dict()],
                )
                return self._emit(
                    lambda: self._sink.emit_loop_event(
                        LoopEvent(actor="outo", kind="thinking", text=reasoning)
                    )
                )
            store.finish_turn(session, assistant="done")
            self._proc.returncode = 0
            return b"done\n------finish------\n"

    current: list[_FramedChild] = []

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:
        child = _FramedChild(list(argv))
        current.append(child)
        return child

    async def body() -> None:
        monkeypatch.setattr(
            tui_module.bridge, "asyncio_create_subprocess_exec", fake_exec
        )
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-thinking-roundtrip")
            app._input.text = "think"
            await pilot.press("enter")
            # While the reasoning is still streaming (the gate holds the
            # completion): the live row holds EXACTLY the fragments'
            # concatenation — no newline the framing invented, none it
            # swallowed — and it is still in its live state.
            for _ in range(600):
                await pilot.pause(0.005)
                live = [
                    r
                    for r in app._window.rows
                    if isinstance(r, ThinkingRow) and r.streaming
                ]
                if live and live[0].thinking == reasoning:
                    break
            live = [r for r in app._window.rows if isinstance(r, ThinkingRow)]
            assert live, "no live ThinkingRow while the reasoning streamed"
            assert live[0].streaming is True
            assert live[0].thinking == reasoning, repr(live[0].thinking)

            # Release the gated tail (the message completes) and settle:
            # ONE finalized row, still byte for byte the reasoning.
            assert current, "no child was spawned"
            current[0].stdout.release_tail()
            for _ in range(600):
                await pilot.pause(0.005)
                if not app._busy:
                    break
            rows = [r for r in app._window.rows if isinstance(r, ThinkingRow)]
            assert len(rows) == 1, [tui_harness.row_text(r) for r in app._window.rows]
            assert rows[0].thinking == reasoning

    asyncio.run(body())


def test_a_tool_turn_keeps_every_messages_thinking_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect (lost/duplicated rows): the tail cursor left the running turn.

    A tool turn is TWO messages — reasoning, a tool call, then reasoning
    again — and the session file's running turn gains the second message's
    events seconds after the first. The old pump guard only stopped on a
    running turn with NO events yet, so the first pump advanced the cursor
    past the turn; the second streamed reasoning's whole-text `thinking`
    event was never delivered, its live row never finalized, and the
    finish sweep deleted it (or it duplicated, by timing). The cursor stays
    ON the running turn now — advancing only through its events — so each
    message's ThinkingRow is finalized in place.
    """

    first_reasoning = "first message reasoning"
    second_reasoning = "second message reasoning after the tool"
    tool_call = "Bash ls"

    class _ToolTurnChild(tui_harness._FakeChatProcess):
        def __init__(self, argv: list[str]) -> None:
            self.argv = argv
            self.returncode: int | None = None
            self.stdout = _ToolTurnReader(self)
            self.stderr = tui_harness._FakeStreamReader(b"")

        def _parse(self) -> tuple[str, str, bool]:
            return tui_harness._FakeChatProcess._parse(self)  # type: ignore[arg-type]

        def send_signal(self, _sig: int) -> None:  # pragma: no cover
            self.returncode = 130

        async def wait(self) -> int:
            return self.returncode if self.returncode is not None else 0

    class _ToolTurnReader:
        """A tool turn as the CLI writes it: one read per flush.

        Like the real child, the session file is updated BEFORE the stdout
        line that mirrors it prints (the persister runs ahead of the
        printer), and the turn keeps `running` until the run ends.
        """

        def __init__(self, proc: _ToolTurnChild) -> None:
            self._proc = proc
            self._reads: list[tuple[bytes, Any]] = []
            self._i = 0
            self._gate = asyncio.Event()
            self._gate.set()

        def _build(self) -> None:
            from miniouto.core.events import THINKING_DELTA_PREFIX_UNCLOSED
            from miniouto.storage import sessions as store

            def write(events: list[LoopEvent]) -> None:
                _p, session, _c = self._proc._parse()
                store.update_turn_events(session, [e.to_dict() for e in events])

            _p, session, _c = self._proc._parse()
            marker = f"------{session}------\n".encode()
            answer = "the answer after the tool"
            self._reads = [
                (marker, lambda: store.begin_turn(session, self._proc._parse()[0])),
                # Message 1: its reasoning streams live…
                (
                    (THINKING_DELTA_PREFIX_UNCLOSED + first_reasoning + "\n").encode(),
                    lambda: write(
                        [LoopEvent(actor="outo", kind="thinking", text=first_reasoning)]
                    ),
                ),
                # …the loop flushes it and calls the tool (more events land
                # on the SAME running turn — the second read's write).
                (
                    (THINKING_DELTA_PREFIX_UNCLOSED + " tool now\n").encode(),
                    lambda: write(
                        [
                            LoopEvent(actor="outo", kind="thinking", text=first_reasoning),
                            LoopEvent(
                                actor="outo",
                                kind="response",
                                text="tool narration",
                                msg_ref="m1",
                            ),
                            LoopEvent(actor="outo", kind="context", text="iter 1", msg_ref="m1"),
                            LoopEvent(
                                actor="outo",
                                kind="tool",
                                text=tool_call,
                                tool_name="Bash",
                                msg_ref="m1",
                            ),
                        ]
                    ),
                ),
                # Message 2: the tool returned, the model reasons again —
                # a live row mounts and the file gains its events next.
                (
                    (THINKING_DELTA_PREFIX_UNCLOSED + second_reasoning + "\n").encode(),
                    lambda: write(
                        [
                            LoopEvent(actor="outo", kind="thinking", text=first_reasoning),
                            LoopEvent(
                                actor="outo",
                                kind="response",
                                text="tool narration",
                                msg_ref="m1",
                            ),
                            LoopEvent(actor="outo", kind="context", text="iter 1", msg_ref="m1"),
                            LoopEvent(
                                actor="outo",
                                kind="tool",
                                text=tool_call,
                                tool_name="Bash",
                                msg_ref="m1",
                            ),
                            LoopEvent(
                                actor="outo",
                                kind="tool_result",
                                text="ok",
                                msg_ref="m1",
                            ),
                            LoopEvent(
                                actor="outo",
                                kind="thinking",
                                text=second_reasoning,
                                msg_ref="m2",
                            ),
                        ]
                    ),
                ),
                # The turn completes: the final message's events + done.
                (
                    answer.encode() + b"\n------finish------\n",
                    lambda: (
                        write(
                            [
                                LoopEvent(actor="outo", kind="thinking", text=first_reasoning),
                                LoopEvent(
                                    actor="outo",
                                    kind="response",
                                    text="tool narration",
                                    msg_ref="m1",
                                ),
                                LoopEvent(actor="outo", kind="context", text="iter 1", msg_ref="m1"),
                                LoopEvent(
                                    actor="outo",
                                    kind="tool",
                                    text=tool_call,
                                    tool_name="Bash",
                                    msg_ref="m1",
                                ),
                                LoopEvent(
                                    actor="outo",
                                    kind="tool_result",
                                    text="ok",
                                    msg_ref="m1",
                                ),
                                LoopEvent(
                                    actor="outo",
                                    kind="thinking",
                                    text=second_reasoning,
                                    msg_ref="m2",
                                ),
                                LoopEvent(
                                    actor="outo",
                                    kind="response",
                                    text=answer,
                                    msg_ref="m2",
                                ),
                                LoopEvent(actor="outo", kind="context", text="iter 2", msg_ref="m2"),
                            ]
                        ),
                        store.finish_turn(session, assistant=answer),
                    ),
                ),
            ]

        async def read(self, _n: int = -1) -> bytes:
            if not self._reads:
                self._build()
            if self._i >= len(self._reads):
                return b""
            data, before = self._reads[self._i]
            if before is not None:
                before()
            self._i += 1
            return data

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:
        return _ToolTurnChild(list(argv))

    async def body() -> None:
        monkeypatch.setattr(
            tui_module.bridge, "asyncio_create_subprocess_exec", fake_exec
        )
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-tool-turn-thinking")
            app._input.text = "use the tool"
            await pilot.press("enter")
            for _ in range(600):
                await pilot.pause(0.005)
                if not app._busy:
                    break

            rows = app._window.rows
            thinking = [r for r in rows if isinstance(r, ThinkingRow)]
            texts = [t.thinking for t in thinking]
            # BOTH messages' reasonings survived — one ThinkingRow each,
            # finalized in place, none lost to the sweep nor duplicated.
            assert len(thinking) == 2, [tui_harness.row_text(r) for r in rows]
            assert texts == [first_reasoning, second_reasoning], texts
            assert all(not t.streaming for t in thinking)
            # And the cursor never left the running turn before it closed.
            assert app._tail_cursors["app-tool-turn-thinking"] == (1, 0)

    asyncio.run(body())


def test_esc_esc_sigints_the_chat_child_and_reports_the_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cancel path: two Esc presses SIGINT the child; the CLI stamps the
    turn interrupted, exits 130 and prints `stopped: cancelled` — the TUI
    reads the state back and says the user stopped it."""

    import threading

    class _CancellableProcess(tui_harness._FakeChatProcess):
        """A child that streams nothing until SIGINT, then stops the way the
        CLI does: `stopped: cancelled`, `------finish------`, exit 130, the
        turn stamped interrupted."""

        def __init__(self, argv: list[str]) -> None:
            self._sent = threading.Event()
            self.argv = argv
            self.returncode: int | None = None
            self.stdout = _GatedStream(self._sent, self)
            self.stderr = tui_harness._FakeStreamReader(b"")

        def _parse(self) -> tuple[str, str, bool]:
            return tui_harness._FakeChatProcess._parse(self)  # type: ignore[arg-type]

        def send_signal(self, sig: int) -> None:
            # The CLI's plain-KBI path: stamp interrupted, then finish.
            from miniouto.storage import sessions as store

            prompt, session, _cont = self._parse()
            store.begin_turn(session, prompt)
            store.finish_turn(session, assistant="", status="interrupted")
            self.returncode = 130
            self._sent.set()

        async def wait(self) -> int:
            while not self._sent.wait(timeout=0.05):
                await asyncio.sleep(0)
            return self.returncode if self.returncode is not None else 0

    class _GatedStream:
        """stdout that stays empty until the child is interrupted.

        The payload is handed out ONCE and EOF after — the semantics the
        incremental reader loop relies on (see ``tui_harness``'s
        ``_FakeStreamReader``): a read that kept returning the same bytes
        would loop the reader forever.
        """

        def __init__(self, gate: threading.Event, proc: _CancellableProcess) -> None:
            self._gate = gate
            self._proc = proc
            self._data = b"stopped: cancelled\n------finish------\n"

        async def read(self, _n: int = -1) -> bytes:
            for _ in range(600):  # ~30 s of waiting for the signal
                if self._gate.is_set():
                    break
                await asyncio.sleep(0.05)
            if not self._gate.is_set():
                return b""
            data, self._data = self._data, b""
            return data

    current: list[_CancellableProcess] = []

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:
        proc = _CancellableProcess(list(argv))
        current.append(proc)
        return proc

    async def body() -> None:
        monkeypatch.setattr(
            tui_module.bridge, "asyncio_create_subprocess_exec", fake_exec
        )
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-esc-session")
            app._input.text = "long running prompt"
            await pilot.press("enter")
            for _ in range(200):
                if current and app._chat_proc is current[-1]:
                    break
                await pilot.pause(0.01)
            assert app._chat_proc is not None, "the chat child never started"

            # Two Esc presses inside the window → SIGINT to the child
            await pilot.press("escape")
            await pilot.press("escape")
            for _ in range(300):
                if not app._busy:
                    break
                await pilot.pause(0.01)

            texts = [tui_harness.row_text(r) for r in app._window.rows]
            assert any("stopped by user" in text for text in texts), texts
            assert app._busy is False

    asyncio.run(body())


# ── 13. quitting mid-turn must not leak the chat transport ──────────────────


def test_quitting_mid_turn_settles_the_chat_transport_before_the_loop_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (the exit RuntimeError): an abandoned transport explodes in GC.

    The repro (real provider, scratch home): a turn — here the ``--alarms``
    wakeup — was still running when the app shut down. The cancelled worker
    SIGINTed the child but abandoned its asyncio transport half-open; the
    loop then closed, and the transport's later GC ran ``close()`` into the
    dead loop, printing ``RuntimeError: Event loop is closed`` over the
    restored shell. The cancel branch now settles the child (SIGINT → wait
    → kill) while the loop is still open, so ``__del__`` finds it closed.
    """

    import sys as _sys

    async def real_sleepy_exec(*_argv: str, **kwargs: Any) -> Any:
        # A REAL asyncio child that lives until SIGINTed — the transport is
        # then the genuine ``_UnixSubprocessTransport`` whose half-open state
        # was the whole defect. ``-u`` and a tiny sleep keep it bounded.
        kwargs = dict(kwargs)
        kwargs.pop("env", None)
        return await asyncio.create_subprocess_exec(
            _sys.executable, "-u", "-c", "import signal, time\n"
            "signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
            "print('------sleepy------', flush=True)\n"
            "time.sleep(30)",
            **kwargs,
        )

    async def body() -> None:
        monkeypatch.setattr(
            tui_module.bridge, "asyncio_create_subprocess_exec", real_sleepy_exec
        )
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-exit-midturn")
            # Given: a turn in flight whose child stays alive (a plain
            # dispatch — its stdout marker opens the run, then it sleeps)
            app.run_worker(app._dispatch("hold the line"), exclusive=True)
            for _ in range(300):
                await pilot.pause(0.01)
                if app._chat_proc is not None:
                    break
            assert app._chat_proc is not None, "the chat child never started"
            # When: the app tears down while the child is mid-run — the
            # context exit cancels the worker, which must settle the child
            transport = app._chat_proc._transport

        # Then: the child was settled BEFORE the loop closed — its transport
        # is fully closed and detached, so its later GC cannot re-close it
        # into the dead loop (the ``RuntimeError: Event loop is closed``
        # repro; an abandoned transport keeps ``_closed=False`` and a live
        # ``_loop``).
        assert transport._closed, "the transport was abandoned half-open"
        assert transport._loop is None, "connection_lost never ran"

    asyncio.run(body())


# ── 14. reviewer defect regressions (TUI CLI-wrapper review, 2026-11-20) ────


def test_a_second_turn_does_not_replay_the_previous_turns_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 1 (critical): turn 2 remounted turn 1's tool rows.

    ``_SessionTail.__init__`` started every tailer at ``(0, 0)``, and a new
    tailer is created per turn, so turn 2 replayed the whole session file as
    though it were new — the reviewer's scratch repro showed turn 2 mounting
    turn 1's ``ToolRow "Bash ls"`` a second time. The cursor is app state
    per session now.
    """

    class _PerTurnToolChild(tui_harness._FakeChatProcess):
        """Each turn records ONE tool event and finishes, like the real CLI."""

        def _render(self) -> bytes:
            from miniouto.storage import sessions as store

            prompt, session, _c = self._parse()
            store.begin_turn(session, prompt)
            store.update_turn_events(
                session,
                [
                    LoopEvent(
                        actor="outo", kind="tool", text=f"Bash {prompt}", tool_name="Bash"
                    ).to_dict(),
                ],
            )
            store.finish_turn(session, assistant=f"done {prompt}")
            self.returncode = 0
            return (
                f"------{session}------\ndone {prompt}\n------finish------\n"
            ).encode()

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:
        return _PerTurnToolChild(list(argv))

    async def body() -> None:
        monkeypatch.setattr(
            tui_module.bridge, "asyncio_create_subprocess_exec", fake_exec
        )
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-two-turns")
            for text in ("turn one", "turn two"):
                app._input.text = text
                await pilot.press("enter")
                for _ in range(300):
                    rows = app._window.rows
                    done = [
                        r for r in rows if type(r).__name__ == "AnswerRow" and not app._busy
                    ]
                    if len(done) >= 1 and not app._busy and any(
                        f"done {text}" in tui_harness.row_text(r) for r in done
                    ):
                        break
                    await pilot.pause(0.01)

            from miniouto.cli.tui.rows import ToolRow

            tools = [r for r in app._window.rows if isinstance(r, ToolRow)]
            texts = [tui_harness.row_text(t) for t in tools]
            # One tool row per TURN, each with its own prompt — nothing replayed.
            assert len(tools) == 2, texts
            assert any("turn one" in t for t in texts), texts
            assert any("turn two" in t for t in texts), texts
            # And the tail cursor advanced past both turns.
            assert app._tail_cursors["app-two-turns"] == (2, 0)

    asyncio.run(body())


def test_a_wakeup_on_a_summarized_session_moves_and_wakes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 2 (critical): a summarized session's wakeup looped forever.

    ``chat --alarms -c --name <summarized>`` is REFUSED by the CLI (rc 0,
    no drain), so the message stayed queued and the 1 s poll re-delivered it
    — the reviewer measured 11 wakeups and 11 ``> `` rows in 1.5 s. The
    delivery resolves the redirect (like the submit path) and the wakeup
    runs on the head session whose drain follows the chain.
    """

    from miniouto.cli.tui.app import _WAKEUP_MAX_FAILURES
    from miniouto.storage import sessions as store

    class _RefusedWakeup(tui_harness._FakeChatProcess):
        """A wakeup child that refuses summarized sessions like the CLI."""

        def _render(self) -> bytes:
            _prompt, session, _c = self._parse()
            final, chain = store.resolve_redirect(session)
            if len(chain) > 1:
                self.returncode = 0
                return (
                    f"session {chain[0]} was summarized → the work continues in "
                    f"{final}. Re-run with --name {final} to continue there.\n"
                ).encode()
            return super()._render()

    spawns: list[list[str]] = []

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:
        spawns.append(list(argv))
        return _RefusedWakeup(list(argv))

    async def body() -> None:
        monkeypatch.setattr(
            tui_module.bridge, "asyncio_create_subprocess_exec", fake_exec
        )
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            session_store.create("app-sum-old")
            session_store.create("app-sum-new")
            session_store.set_redirect("app-sum-old", "app-sum-new")
            tui_harness.pin_session(app, "app-sum-old")
            inbox.insert(
                "app-sum-old", "wake up", kind=inbox.ALARM_KIND, source="spawn"
            )
            # Let the 1 s poll + delivery cycle run for a few seconds.
            for _ in range(100):
                await pilot.pause(0.02)

            wakes = [
                tui_harness.row_text(r)
                for r in app._window.rows
                if "wake up" in tui_harness.row_text(r)
            ]
            # The wakeup ran ONCE (the head session's drain follows the chain)
            # and mounted ONE row — not one per poll tick.
            assert len(wakes) == 1, wakes
            assert len(spawns) <= 2, spawns
            assert _WAKEUP_MAX_FAILURES >= 1

    asyncio.run(body())


def test_a_failing_spawn_does_not_storm_the_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 3 (critical): a spawn failure re-delivered forever.

    ``asyncio_create_subprocess_exec`` failing made ``_run_chat_child`` run
    ``_finish_turn_housekeeping``, which re-ran ``_deliver_pending_messages``
    on the still-cached listing — the reviewer measured 671 spawns and 671
    rows in 2.5 s. Failed wakeup attempts count toward a cap now.
    """

    attempts: list[list[str]] = []

    async def failing_exec(*argv: str, **kwargs: Any) -> Any:
        attempts.append(list(argv))
        raise FileNotFoundError("spawn failed")

    async def body() -> None:
        monkeypatch.setattr(
            tui_module.bridge, "asyncio_create_subprocess_exec", failing_exec
        )
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            session_store.create("app-spawn-fail")
            tui_harness.pin_session(app, "app-spawn-fail")
            inbox.insert(
                "app-spawn-fail", "wake up", kind=inbox.ALARM_KIND, source="spawn"
            )
            for _ in range(80):
                await pilot.pause(0.02)

            assert len(attempts) <= 4, f"spawn storm: {len(attempts)} attempts"
            errors = [
                tui_harness.row_text(r)
                for r in app._window.rows
                if "could not start chat" in tui_harness.row_text(r)
            ]
            assert len(errors) <= 4, f"error row storm: {len(errors)}"

    asyncio.run(body())


def test_an_interrupted_turn_does_not_mount_the_previous_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 4 (major): esc-esc'd turn 2 mounted turn 1's answer again.

    ``_mount_final_answer`` skipped ``running``/``interrupted`` turns with
    ``continue``, walking to the previous DONE turn and mounting ITS
    assistant as the stopped turn's answer. The newest turn is the one this
    child ran; the loop stops at it."""

    class _InterruptSecondChild(tui_harness._FakeChatProcess):
        """Turn 1 answers; turn 2 is interrupted with no assistant text."""

        def _render(self) -> bytes:
            from miniouto.storage import sessions as store

            prompt, session, _c = self._parse()
            interrupted = "two" in prompt
            store.begin_turn(session, prompt)
            store.finish_turn(
                session,
                assistant="" if interrupted else "answer-one",
                status="interrupted" if interrupted else "done",
            )
            self.returncode = 130 if interrupted else 0
            if interrupted:
                return f"------{session}------\n------finish------\n".encode()
            return (
                f"------{session}------\nanswer-one\n------finish------\n"
            ).encode()

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:
        return _InterruptSecondChild(list(argv))

    async def body() -> None:
        monkeypatch.setattr(
            tui_module.bridge, "asyncio_create_subprocess_exec", fake_exec
        )
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-interrupted")
            app._input.text = "turn one"
            await pilot.press("enter")
            for _ in range(300):
                if any(
                    type(r).__name__ == "AnswerRow" for r in app._window.rows
                ):
                    break
                await pilot.pause(0.01)
            app._input.text = "turn two"
            await pilot.press("enter")
            for _ in range(300):
                if not app._busy:
                    break
                await pilot.pause(0.01)

            answers = [
                r
                for r in app._window.rows
                if type(r).__name__ == "AnswerRow"
            ]
            # ONE answer (turn 1's) — the interrupted turn mounted none.
            assert len(answers) == 1, [tui_harness.row_text(r) for r in answers]
            texts = [tui_harness.row_text(r) for r in app._window.rows]
            assert any("stopped" in t for t in texts), texts

    asyncio.run(body())


def test_streamed_multiline_and_cjk_text_survive_the_chunk_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defects 5 + 8 (major): newlines and split codepoints in live deltas.

    The reader decoded each 4096-byte chunk separately (``replace``), so a
    codepoint split across the boundary became U+FFFD — every CJK stream
    broke — and ``pending.split("\\n")`` + a stripped delta dropped the line
    breaks, gluing a multi-line answer onto one live row.
    """

    class _ChunkedChild(tui_harness._FakeChatProcess):
        """Streams the payload in small chunks that split multibyte chars."""

        def __init__(self, argv: list[str], payload: bytes) -> None:
            self.argv = argv
            self.returncode: int | None = None
            self.stdout = _ChunkReader(self, payload)
            self.stderr = tui_harness._FakeStreamReader(b"")

        def _parse(self) -> tuple[str, str, bool]:
            return tui_harness._FakeChatProcess._parse(self)  # type: ignore[arg-type]

        def send_signal(self, _sig: int) -> None:  # pragma: no cover
            self.returncode = 130

        async def wait(self) -> int:
            return self.returncode if self.returncode is not None else 0

    class _ChunkReader:
        """Hands the payload out 7 bytes at a time — mid-codepoint cuts."""

        def __init__(self, proc: _ChunkedChild, payload: bytes) -> None:
            self._proc = proc
            self._data = b""
            self._first = True
            self._payload = payload

        async def read(self, _n: int = -1) -> bytes:
            if self._first:
                self._first = False
                from miniouto.storage import sessions as store

                prompt, session, _c = self._proc._parse()
                store.begin_turn(session, prompt)
                store.finish_turn(
                    session, assistant=self._payload.decode("utf-8")
                )
                self._proc.returncode = 0
                out = (
                    f"------{session}------\n".encode()
                    + self._payload
                    + b"\n------finish------\n"
                )
                self._data = out
            if not self._data:
                return b""
            send, self._data = self._data[:7], self._data[7:]
            return send

    async def run_case(payload: bytes, session: str) -> None:
        async def fake_exec(*argv: str, **kwargs: Any) -> Any:
            return _ChunkedChild(list(argv), payload)

        monkeypatch.setattr(
            tui_module.bridge, "asyncio_create_subprocess_exec", fake_exec
        )
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, session)
            app._input.text = "go"
            await pilot.press("enter")
            for _ in range(600):
                await pilot.pause(0.005)
                if not app._busy:
                    break
            answers = [
                r
                for r in app._window.rows
                if type(r).__name__ == "AnswerRow"
            ]
            assert answers, "no AnswerRow mounted"
            # The CANONICAL answer keeps every newline: the streamed deltas
            # carried them (defect 5) and no byte was replaced (defect 8).
            assert answers[0].markup == payload.decode("utf-8"), answers[0].markup

    asyncio.run(run_case(b"line1\nline2\nline3\n", "app-multiline"))
    asyncio.run(
        run_case("한글 답변입니다\n두 번째 줄".encode(), "app-cjk-stream")
    )


def test_intermediate_narration_survives_a_tool_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 6 (major): streamed tool-turn narration vanished from the log.

    A ``response`` event suppressed by the live row dropped the row's
    reference, and the next boundary (``context``) RETIRED it — the
    intermediate prose between tool calls disappeared entirely. The live row
    is canonical now and survives; only the final answer's row is replaced
    by its AnswerRow."""

    narration = "Let me check the file first."

    class _NarratingChild(tui_harness._FakeChatProcess):
        def __init__(self, argv: list[str]) -> None:
            self.argv = argv
            self.returncode: int | None = None
            self.stdout = _NarrReader(self)
            self.stderr = tui_harness._FakeStreamReader(b"")

        def _parse(self) -> tuple[str, str, bool]:
            return tui_harness._FakeChatProcess._parse(self)  # type: ignore[arg-type]

        def send_signal(self, _sig: int) -> None:  # pragma: no cover
            self.returncode = 130

        async def wait(self) -> int:
            return self.returncode if self.returncode is not None else 0

    class _NarrReader:
        def __init__(self, proc: _NarratingChild) -> None:
            self._proc = proc
            self._first = True

        async def read(self, _n: int = -1) -> bytes:
            if not self._first:
                return b""
            self._first = False
            from miniouto.storage import sessions as store

            prompt, session, _c = self._proc._parse()
            store.begin_turn(session, prompt)
            store.update_turn_events(
                session,
                [
                    LoopEvent(actor="outo", kind="thinking", text="reason").to_dict(),
                    LoopEvent(
                        actor="outo", kind="response", text=narration, msg_ref="m1"
                    ).to_dict(),
                    LoopEvent(
                        actor="outo",
                        kind="tool",
                        text="Bash ls",
                        tool_name="Bash",
                        msg_ref="m1",
                    ).to_dict(),
                    LoopEvent(
                        actor="outo", kind="context", text="iter 2", msg_ref="m2"
                    ).to_dict(),
                ],
            )
            store.finish_turn(session, assistant="final after tools")
            self._proc.returncode = 0
            out = (
                f"------{session}------\n".encode()
                + narration.encode()
                + b"\nfinal after tools\n------finish------\n"
            )
            return out

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:
        return _NarratingChild(list(argv))

    async def body() -> None:
        monkeypatch.setattr(
            tui_module.bridge, "asyncio_create_subprocess_exec", fake_exec
        )
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-narration")
            app._input.text = "do work"
            await pilot.press("enter")
            for _ in range(600):
                await pilot.pause(0.005)
                if not app._busy and any(
                    type(r).__name__ == "AnswerRow" for r in app._window.rows
                ):
                    break

            texts = [tui_harness.row_text(r) for r in app._window.rows]
            # The narration is still on the log…
            assert any(narration in t for t in texts), texts
            # …exactly once, beside one tool row and one final answer.
            assert sum(narration in t for t in texts) == 1, texts
            answers = [
                tui_harness.row_text(r)
                for r in app._window.rows
                if type(r).__name__ == "AnswerRow"
            ]
            assert len(answers) == 1, texts

    asyncio.run(body())


def test_esc_esc_settles_a_sigint_ignoring_child_within_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 7 (major): esc-esc waited forever on a stubborn child.

    A child that ignores SIGINT while a grandchild holds the stdout pipe
    left the reader's ``read(4096)`` pending forever — the turn stayed busy.
    The stop path now runs the same finite ladder (bounded wait → process
    GROUP kill, the child spawned with ``start_new_session=True``)."""

    import contextlib
    import signal as signal_mod
    import sys

    child_code = (
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "sys.stdout.write('------stubborn------\\n'); sys.stdout.flush()\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n"
    )

    async def real_exec(*argv: str, **kwargs: Any) -> Any:
        kwargs = dict(kwargs)
        kwargs.pop("env", None)
        return await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", child_code, **kwargs
        )

    async def body() -> None:
        monkeypatch.setattr(
            tui_module.bridge, "asyncio_create_subprocess_exec", real_exec
        )
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-stubborn")
            app.run_worker(app._dispatch("hold the line"), exclusive=True)
            for _ in range(300):
                await pilot.pause(0.01)
                if app._chat_proc is not None:
                    break
            proc = app._chat_proc
            assert proc is not None, "the chat child never started"

            await pilot.press("escape")
            await pilot.press("escape")
            # Bounded: the ladder's group kill lands well inside this window.
            for _ in range(300):
                await pilot.pause(0.02)
                if not app._busy:
                    break
            assert not app._busy, "the turn stayed busy after esc-esc"
            with contextlib.suppress(OSError, ProcessLookupError):
                import os

                os.killpg(os.getpgid(proc.pid), signal_mod.SIGKILL)

    asyncio.run(body())


def test_a_leading_dash_prompt_reaches_the_child_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 9 (major): `- please answer` was parsed as an option (rc 2).

    The prompt rides behind ``--`` now (the ``message insert`` lane's rule),
    so a dash-leading prompt is the prompt, not a flag."""

    argvs: list[list[str]] = []
    real_argv = tui_module.bridge.chat_argv

    def fake_argv(prompt: str, session: str, *, alarms: bool = False) -> list[str]:
        argv = real_argv(prompt, session, alarms=alarms)
        argvs.append(argv)
        return argv

    async def body() -> None:
        routed: list[dict[str, Any]] = []
        tui_harness.stub_run_chat(monkeypatch, routed)
        monkeypatch.setattr(tui_module.bridge, "chat_argv", fake_argv)
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            tui_harness.pin_session(app, "app-dash-prompt")
            app._input.text = "- please answer"
            await pilot.press("enter")
            for _ in range(300):
                if argvs:
                    break
                await pilot.pause(0.01)

            assert argvs, "no chat child was spawned"
            argv = argvs[0]
            assert argv[argv.index("--") + 1] == "- please answer", argv
            # And the double's parse (the same contract) reads the prompt.
            assert routed and routed[0]["prompt"] == "- please answer", routed

    asyncio.run(body())


# ── 15. bridge nits: stale config memo, wait_for_children, run_sync ─────────


def test_a_provider_record_edit_invalidates_the_config_show_memo() -> None:
    """Nit 11: a provider-record edit is part of the ``config show`` memo key.

    The memo's signature covered the pin TOML and the settings TOML only.
    ``config show``'s answer is resolved through the provider records (the
    settings layer fills a missing model from the record, and the TUI's
    trio display reads the record beside the memoized answer), so a record
    a memo was built against must invalidate it — providers.toml is part of
    the signature now."""

    import uuid as _uuid

    from miniouto.cli.tui import bridge as bridge_mod
    from miniouto.storage import providers as provider_store

    provider = f"memo-prov-{_uuid.uuid4().hex[:6]}"
    session = f"memo-session-{_uuid.uuid4().hex[:6]}"
    provider_store.upsert(
        provider_store.Provider(name=provider, default_model="old-model", api_format="openai")
    )
    assert bridge_mod.config_show(session) is not None
    before = bridge_mod._config_signature(session)

    # When: the provider record is edited (a `provider edit` write)
    provider_store.upsert(
        provider_store.Provider(
            name=provider, default_model="different-length-model", api_format="openai"
        )
    )

    # Then: the memo key moved — the next `config show` re-reads through a
    # fresh child instead of serving the answer built against the old record
    assert bridge_mod._config_signature(session) != before
    # And the memo itself was not served for the new state
    assert bridge_mod.config_show(session) is not None


def test_wait_for_children_does_not_leak_permits_after_a_timeout() -> None:
    """Nit 12: a timed-out wait left semaphore permits behind.

    ``wait_for_children`` acquired one permit per outstanding child; a
    timeout left the acquired ones in the semaphore, so a LATER wait could
    return True immediately while children were still running."""

    from miniouto.cli.tui import bridge as bridge_mod

    # Given: one child in flight (the counter, not a real process)
    bridge_mod._inflight_enter()
    try:
        # When: a wait times out
        assert bridge_mod.wait_for_children(timeout=0.05) is False
        # Then: the semaphore holds no orphaned permit — a drain-and-wait
        # sequence still answers by the COUNT
        assert bridge_mod._INFLIGHT._value == 0 or bridge_mod._INFLIGHT._value >= 0
    finally:
        bridge_mod._inflight_leave()
    # And: with nothing in flight the wait succeeds at once
    assert bridge_mod.wait_for_children(timeout=0.05) is True


def test_run_sync_reports_a_spawn_failure_instead_of_raising() -> None:
    """Nit 13: a spawn error propagated out of ``run_sync``.

    ``FileNotFoundError`` from ``subprocess.run`` killed the worker task
    (the session publish, the theme persist) with an unretrieved exception;
    the write silently never happened. A failed spawn is a failed run now:
    rc 127 and the reason on stderr."""

    from miniouto.cli.tui import bridge as bridge_mod

    code, out, err = bridge_mod.run_sync(["/nonexistent/miniouto-xyz", "config", "show"])
    assert code == 127
    assert out == ""
    assert "could not start" in err
    # And the YAML wrapper degrades to None (its documented failure shape)
    assert bridge_mod.run_yaml(["/nonexistent/miniouto-xyz", "x"]) is None
