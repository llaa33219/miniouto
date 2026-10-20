"""Lock the TUI's graphics-capable Computer screenshot lane.

The `.art` text render is the fallback; when the terminal speaks real
graphics (Kitty TGP / Sixel) the row must instead surface the raw PNG the
`screenshot` action wrote next to the `.art`. These tests pin the contract
at the seams: the PNG path travels through the tool result, the row gates
on it, and every failure mode (no graphics, no png, import/decode error)
drops back to the `.art` text without raising.
"""

from __future__ import annotations

import asyncio

from textual.widget import Widget

from miniouto.cli import tui as tui_module
from miniouto.cli.tui import ChatTUI, ToolRow
from miniouto.tools import computer as computer_mod
from miniouto.tools.registry import _computer_handler
from test_ascii_art import make_png


def _png_bytes(width: int = 8, height: int = 4) -> bytes:
    return make_png([[(10, 20, 30, 255)] * width for _ in range(height)], 6)


class _FakeImage:
    def __init__(self, png: bytes, width: int, height: int) -> None:
        self.png_bytes = png
        self.width = width
        self.height = height


class _FakeCompositor:
    def __init__(self, png: bytes, width: int, height: int) -> None:
        self._image = _FakeImage(png, width, height)

    def screenshot(self) -> _FakeImage:
        return self._image


class _FakeImageWidget(Widget):
    """Stand-in for a `textual_image` widget.

    The real one needs a live terminal (it sends pixels to it); this proves
    the ROW's wiring — mount, display, fallback — without one.
    """

    def __init__(self, path) -> None:
        super().__init__()
        self.path = path
        self.styles.width = 100
        self.styles.height = 28


def _fake_screen(monkeypatch, tmp_path):
    png = _png_bytes()
    comp = _FakeCompositor(png, 8, 4)
    monkeypatch.setattr(
        computer_mod, "_resolve_screen", lambda screen, *, autospawn: ("wl-1", comp)
    )
    monkeypatch.setattr(computer_mod, "COMPUTER_SCREENS_DIR", tmp_path)
    return png


def test_screenshot_writes_png_sibling_and_result_names_it(monkeypatch, tmp_path) -> None:
    _fake_screen(monkeypatch, tmp_path)
    result = computer_mod.computer("screenshot")
    assert isinstance(result, computer_mod.Screenshot)
    assert result.png_path is not None
    assert result.png_path.suffix == ".png"
    assert result.png_path.is_file()
    assert result.png_path.parent == tmp_path
    # the .art sibling lands in the same directory; the two are written a
    # moment apart, so they share the timestamp SECOND except when the clock
    # crosses a second between the two writes (then the stems differ by one)
    assert result.art_path is not None
    assert result.art_path.parent == result.png_path.parent
    assert result.art_path.suffix == ".art"
    assert result.png_path.stem.split("-")[:-1] == result.art_path.stem.split("-")[:-1]


def test_registry_text_block_carries_the_png_line(monkeypatch, tmp_path) -> None:
    _fake_screen(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "miniouto.tools.registry._SCREENSHOTS_SUPPORTED", True, raising=False
    )
    blocks = _computer_handler("screenshot")
    text = blocks[0].text
    assert "Image saved to: " in text
    # the png line rides above the art line, which stays last (TUI hook)
    lines = text.splitlines()
    assert lines[-2].startswith("Image saved to: ") and lines[-2].endswith(".png")
    assert lines[-1].startswith("Saved to: ") and lines[-1].endswith(".art")


def test_png_path_in_result_parses_the_registry_line() -> None:
    result = (
        "Screenshot of the virtual display (1280x720 pixels). "
        "Click coordinates for the next action refer to this image; "
        "(0, 0) is the top-left corner.\n"
        "Saved to: /x/wl-1-2026-10-20-18-00-00.art\n"
        "Image saved to: /x/wl-1-2026-10-20-18-00-00.png"
    )
    assert tui_module.png_path_in_result(result) == "/x/wl-1-2026-10-20-18-00-00.png"
    assert tui_module.png_path_in_result("no image line here") is None
    assert tui_module.png_path_in_result("Saved to: /x/a.art") is None


def test_terminal_graphics_supported_degrades_safely(monkeypatch) -> None:
    # import failure -> False, never raises
    import builtins

    real_import = builtins.__import__

    def boom(name, *args, **kwargs):
        if name.startswith("textual_image"):
            raise ImportError("no module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert tui_module.terminal_graphics_supported() is False


def test_screen_art_stays_text_when_no_graphics(monkeypatch, tmp_path) -> None:
    # terminal has no TGP/Sixel -> the .art text render is what survives.
    # `_screen_art` is exercised on an unattached row: the image lane bails at
    # `is_attached` before any probing, so the text path runs with no app.
    row = tui_module.ToolRow.__new__(tui_module.ToolRow)
    row._tool_name = "Computer"
    row._result = None
    row._image_widget = None
    row._image_failed = False
    row._image_visible = False
    from miniouto.tools.ascii_art import build_art

    art_path = tmp_path / "wl-1-2026-10-20-18-00-00.art"
    art_path.write_text(
        build_art(
            {"source": "8x4", "grid": "4x2", "cell": "2x2", "mode": "subcell-truecolor"},
            "\x1b[38;2;1;2;3m▀\x1b[0m",
        ),
        encoding="utf-8",
    )
    row._result = (
        f"Screenshot of the virtual display (8x4 pixels).\nSaved to: {art_path}\n"
        f"Image saved to: {tmp_path / 'wl-1-2026-10-20-18-00-00.png'}"
    )
    block = row._screen_art()
    assert block is not None
    assert "screen art" in block.plain
    # graphics lane never engaged: no widget was mounted
    assert row._image_widget is None


def test_screen_action_result_never_engages_the_image_lane(tmp_path) -> None:
    # A `screen` result names only the `.art` — the image lane must stay off
    # even where graphics exist, so the model's text-art view is stable.
    from miniouto.tools.ascii_art import build_art

    art_path = tmp_path / "wl-1-2026-10-20-18-00-00.art"
    art_path.write_text(
        build_art(
            {"source": "8x4", "grid": "4x2", "cell": "2x2", "mode": "subcell-truecolor"},
            "\x1b[38;2;1;2;3m▀\x1b[0m",
        ),
        encoding="utf-8",
    )
    row = tui_module.ToolRow.__new__(tui_module.ToolRow)
    row._tool_name = "Computer"
    row._image_widget = None
    row._image_failed = False
    row._image_visible = False
    row._result = (
        "Screen art captured (8x4 -> 4x2 chars, subcell truecolor).\n"
        f"Saved to: {art_path}\n"
        "Click coordinates still come from the screenshot action."
    )
    block = row._screen_art()
    assert block is not None
    assert "screen art" in block.plain
    assert row._image_widget is None


def test_expanded_screenshot_row_mounts_the_image_widget(monkeypatch, tmp_path) -> None:
    """Graphics-capable terminal + attached row: the PNG child is mounted."""

    from miniouto.cli.tui import ChatTUI, ToolRow

    png_path = tmp_path / "wl-1-2026-10-20-18-00-00.png"
    png_path.write_bytes(_png_bytes())
    art_path = tmp_path / "wl-1-2026-10-20-18-00-00.art"
    from miniouto.tools.ascii_art import build_art

    art_path.write_text(
        build_art(
            {"source": "8x4", "grid": "4x2", "cell": "2x2", "mode": "subcell-truecolor"},
            "\x1b[38;2;1;2;3m▀\x1b[0m",
        ),
        encoding="utf-8",
    )
    # Detection is what enables the lane: the mode AND the widget class must
    # be in place (the render path never imports or probes on its own).
    monkeypatch.setattr(tui_module, "graphics_mode", lambda: tui_module.GRAPHICS_MODE_TGP)
    monkeypatch.setitem(
        tui_module._IMAGE_WIDGETS, tui_module.GRAPHICS_MODE_TGP, _FakeImageWidget
    )

    async def main() -> None:
        app = ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            row = ToolRow("Computer screenshot", tool_name="Computer")
            await app._log.mount(row)
            row.set_result(
                "Screenshot of the virtual display (8x4 pixels).\n"
                f"Image saved to: {png_path}\nSaved to: {art_path}"
            )
            row._expanded = True
            row._refresh()
            await pilot.pause()
            assert row._image_widget is not None
            assert row._image_widget.display is True
            assert "screen image" in row._screen_art().plain
            # collapse hides the child again
            row._expanded = False
            row._refresh()
            await pilot.pause()
            assert row._image_widget.display is False

    import asyncio

    asyncio.run(main())


# ── the detection contract: one probe, before Textual starts, never again ──


def test_detection_picks_tgp_when_the_terminal_answers_tgp(monkeypatch) -> None:
    from textual_image._terminal import CellSize, TerminalCapabilities

    caps = TerminalCapabilities(CellSize(10, 20), sixel=False, tgp=True)
    monkeypatch.setattr(
        "textual_image._terminal.probe_terminal", lambda: caps, raising=True
    )
    monkeypatch.setattr(tui_module, "_GRAPHICS_MODE", None, raising=False)
    monkeypatch.setattr(tui_module, "_IMAGE_WIDGETS", {}, raising=False)
    assert tui_module.detect_graphics_mode() == tui_module.GRAPHICS_MODE_TGP


def test_detection_falls_back_to_sixel_when_tgp_is_absent(monkeypatch) -> None:
    from textual_image._terminal import CellSize, TerminalCapabilities

    caps = TerminalCapabilities(CellSize(10, 20), sixel=True, tgp=False)
    monkeypatch.setattr(
        "textual_image._terminal.probe_terminal", lambda: caps, raising=True
    )
    monkeypatch.setattr(tui_module, "_GRAPHICS_MODE", None, raising=False)
    monkeypatch.setattr(tui_module, "_IMAGE_WIDGETS", {}, raising=False)
    assert tui_module.detect_graphics_mode() == tui_module.GRAPHICS_MODE_SIXEL


def test_detection_falls_back_to_text_when_nothing_is_supported(monkeypatch) -> None:
    from textual_image._terminal import CellSize, TerminalCapabilities

    caps = TerminalCapabilities(CellSize(10, 20), sixel=False, tgp=False)
    monkeypatch.setattr(
        "textual_image._terminal.probe_terminal", lambda: caps, raising=True
    )
    monkeypatch.setattr(tui_module, "_GRAPHICS_MODE", None, raising=False)
    monkeypatch.setattr(tui_module, "_IMAGE_WIDGETS", {}, raising=False)
    assert tui_module.detect_graphics_mode() == tui_module.GRAPHICS_MODE_TEXT
    # and the text lane is what the render path then uses
    assert tui_module.graphics_mode() == tui_module.GRAPHICS_MODE_TEXT
    assert tui_module.terminal_graphics_supported() is False


def test_a_failed_probe_is_the_text_lane_not_an_error(monkeypatch) -> None:
    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr("textual_image._terminal.probe_terminal", boom, raising=True)
    monkeypatch.setattr(tui_module, "_GRAPHICS_MODE", None, raising=False)
    monkeypatch.setattr(tui_module, "_IMAGE_WIDGETS", {}, raising=False)
    assert tui_module.detect_graphics_mode() == tui_module.GRAPHICS_MODE_TEXT


def test_the_render_path_never_probes(monkeypatch) -> None:
    """The render path must read the detected mode, never probe.

    A probe is a terminal round-trip; running it while Textual owns stdin
    is the stall this design removes. `graphics_mode()` therefore reads the
    cached value and reports TEXT when detection never ran.
    """

    def boom():
        raise AssertionError("the render path must not probe the terminal")

    monkeypatch.setattr("textual_image._terminal.probe_terminal", boom, raising=True)
    monkeypatch.setattr(tui_module, "_GRAPHICS_MODE", None, raising=False)
    assert tui_module.graphics_mode() == tui_module.GRAPHICS_MODE_TEXT
    assert tui_module.terminal_graphics_supported() is False


def test_detection_is_cached_so_the_probe_runs_once(monkeypatch) -> None:
    calls = []

    from textual_image._terminal import CellSize, TerminalCapabilities

    caps = TerminalCapabilities(CellSize(10, 20), sixel=False, tgp=True)

    def probe():
        calls.append(1)
        return caps

    monkeypatch.setattr("textual_image._terminal.probe_terminal", probe, raising=True)
    monkeypatch.setattr(tui_module, "_GRAPHICS_MODE", None, raising=False)
    monkeypatch.setattr(tui_module, "_IMAGE_WIDGETS", {}, raising=False)
    assert tui_module.detect_graphics_mode() == tui_module.GRAPHICS_MODE_TGP
    assert tui_module.detect_graphics_mode() == tui_module.GRAPHICS_MODE_TGP
    assert len(calls) == 1


# ── the render path: PNG-only results and the sixel/tgp split ──


def test_a_png_without_an_art_sibling_still_renders_the_image(monkeypatch, tmp_path) -> None:
    """A capture whose `.art` write failed still has its pixels.

    The `.art` path used to be checked FIRST, so a result naming only the
    PNG bailed out before the image lane was ever reached — the image
    existed on disk and was never shown.
    """

    png_path = tmp_path / "shot.png"
    png_path.write_bytes(_png_bytes())
    monkeypatch.setattr(
        tui_module, "graphics_mode", lambda: tui_module.GRAPHICS_MODE_TGP
    )
    monkeypatch.setitem(
        tui_module._IMAGE_WIDGETS, tui_module.GRAPHICS_MODE_TGP, _FakeImageWidget
    )

    async def main() -> None:
        app = ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            row = ToolRow("Computer screenshot", tool_name="Computer")
            await app._log.mount(row)
            row.set_result(f"Screenshot (8x4).\nImage saved to: {png_path}")
            row._expanded = True
            row._refresh()
            await pilot.pause()
            assert row._image_widget is not None
            assert "screen image" in row._screen_art().plain

    asyncio.run(main())


def test_sixel_is_preferred_when_the_terminal_offers_both(monkeypatch) -> None:
    """Konsole/wezterm REPORT TGP support but do not implement it.

    Their placeholder scheme is broken, so TGP there renders nothing, while
    Sixel works. Sixel therefore wins whenever it is available; TGP is for
    the terminals that have Sixel absent (in practice Kitty).
    """

    from textual_image._terminal import CellSize, TerminalCapabilities

    caps = TerminalCapabilities(CellSize(10, 20), sixel=True, tgp=True)
    monkeypatch.setattr("textual_image._terminal.probe_terminal", lambda: caps)
    monkeypatch.setattr(tui_module, "_GRAPHICS_MODE", None, raising=False)
    monkeypatch.setattr(tui_module, "_IMAGE_WIDGETS", {}, raising=False)
    assert tui_module.detect_graphics_mode() == tui_module.GRAPHICS_MODE_SIXEL


def test_tgp_is_used_when_sixel_is_absent(monkeypatch) -> None:
    from textual_image._terminal import CellSize, TerminalCapabilities

    caps = TerminalCapabilities(CellSize(10, 20), sixel=False, tgp=True)
    monkeypatch.setattr("textual_image._terminal.probe_terminal", lambda: caps)
    monkeypatch.setattr(tui_module, "_GRAPHICS_MODE", None, raising=False)
    monkeypatch.setattr(tui_module, "_IMAGE_WIDGETS", {}, raising=False)
    assert tui_module.detect_graphics_mode() == tui_module.GRAPHICS_MODE_TGP


# ── the image row's placement: a sibling, never a child of the text row ──


def test_the_image_is_a_sibling_row_not_a_child_of_the_text_row(
    monkeypatch, tmp_path
) -> None:
    """The image must not share lines with the row's own text.

    A child widget is laid out at the TOP of its parent's content area —
    exactly where the parent's own caption is — so the two were drawn on
    top of each other. The image therefore lives in the log, right after
    the row.
    """

    png_path = tmp_path / "shot.png"
    png_path.write_bytes(_png_bytes())
    monkeypatch.setattr(
        tui_module, "graphics_mode", lambda: tui_module.GRAPHICS_MODE_TGP
    )
    monkeypatch.setitem(
        tui_module._IMAGE_WIDGETS, tui_module.GRAPHICS_MODE_TGP, _FakeImageWidget
    )

    async def main() -> None:
        app = ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            row = ToolRow("Computer screenshot", tool_name="Computer")
            await app._log.mount(row)
            row.set_result(f"Screenshot (8x4).\nImage saved to: {png_path}")
            row._expanded = True
            row._refresh()
            await pilot.pause()
            await pilot.pause()
            widget = row._image_widget
            assert widget is not None
            # the image is NOT a child of the text row
            assert widget not in list(row.children)
            # and it is in the log, right after the row
            children = list(app._log.children)
            assert children.index(widget) == children.index(row) + 1
            # the two do not overlap on screen
            assert widget.region.y >= row.region.y + row.region.height

    asyncio.run(main())


def test_collapsing_the_row_keeps_the_row_and_hides_only_the_image(
    monkeypatch, tmp_path
) -> None:
    """Collapsing must not take the row with it.

    A `Static` with ANY child becomes a container, and a container whose
    only child is hidden computes its auto height as ZERO — the row
    vanished from the log when the image was hidden. The image is a
    sibling row now, so the text row keeps its height.
    """

    png_path = tmp_path / "shot.png"
    png_path.write_bytes(_png_bytes())
    monkeypatch.setattr(
        tui_module, "graphics_mode", lambda: tui_module.GRAPHICS_MODE_TGP
    )
    monkeypatch.setitem(
        tui_module._IMAGE_WIDGETS, tui_module.GRAPHICS_MODE_TGP, _FakeImageWidget
    )

    async def main() -> None:
        app = ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            row = ToolRow("Computer screenshot", tool_name="Computer")
            await app._log.mount(row)
            row.set_result(f"Screenshot (8x4).\nImage saved to: {png_path}")
            row._expanded = True
            row._refresh()
            await pilot.pause()
            await pilot.pause()
            assert row._image_widget is not None
            expanded_height = row.content_size.height
            assert expanded_height > 0

            # collapse
            row._expanded = False
            row._refresh()
            await pilot.pause()
            await pilot.pause()

            # the row is still there, with its text
            assert row.is_attached
            assert row.content_size.height > 0
            assert row.region.height > 0
            # and the image is hidden, not the row
            assert row._image_widget.display is False

    asyncio.run(main())
