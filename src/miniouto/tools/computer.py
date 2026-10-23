"""Computer-use tool: drive GUI apps inside virtual headless displays.

Backed by py-Vwayland (import name `vwayland`): a bundled Rust/Smithay
compositor, headless by default (pixman CPU rendering — no GPU, no display
server, works over SSH and in containers).

Screens: one compositor == one virtual screen == ONE app. This module keeps
a module-level registry of screens so the model can run several GUI apps by
spawning several screens explicitly (`spawn` / `kill` / `list` actions,
`screen=` targeting on every other action). A screen spawned with a text
label uses that label as its id (e.g. screen="browser"). When the model
omits `screen`: zero screens -> a default screen is spawned lazily; exactly
one -> that one; several -> an error listing the ids. Screens are killed at
process exit via vwayland's own `kill_on_exit` atexit hook; a screen whose
compositor dies mid-session is pruned from the registry and the next call
tells the model to spawn it again.

Layer rule: like media.py, this module must not import coreouto — only
tools/registry.py wraps results into ContentBlocks.

Availability: py-Vwayland is a default dependency whose wheel installs
anywhere but whose bundled compositor binary runs only on Linux x86_64
glibc >= 2.28. `computer_supported()` probes the platform once per process
(cheap, cached); `tools/registry.py` registers the Computer tool only when
it returns True, so unsupported platforms never advertise the tool to the
model. `MINIOUTO_COMPUTER=0` disables the tool explicitly; `=1` skips the
platform probe (for e.g. aarch64 source builds of py-Vwayland).

All vwayland calls are blocking and every action runs under one module
lock: coreouto runs sync handlers via asyncio.to_thread, so this lock is
what serializes concurrent tool calls (vwayland opens a fresh IPC socket
per call — without the lock even different screens could race the shared
`_spawned` bookkeeping upstream).

The `screen` action renders a capture as Unicode sub-cell art (2x4 source
pixels per terminal cell, block octants for crisp regions and braille dots for
blended ones) and writes it to a `.art` file under
`~/.miniouto/computer-screens/`; it returns the path plus a short preview as a
plain `str`, never an image, so it stays usable on providers whose format cannot
carry image tool results. The `screenshot` action writes that same `.art`
sibling for the same capture — best effort, and signalled to the caller through
`Screenshot.art_path` — so a TUI can render the screen from a screenshot's
details too; the line is appended by `tools/registry.py` when it wraps the
capture into blocks. The capture still comes from the same blocking
`comp.screenshot()` call and is still serialized by `_LOCK`. Decoding and
rendering live in `tools/ascii_art.py` (pure stdlib).
"""

from __future__ import annotations

import contextlib
import os
import platform
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from ..storage.paths import COMPUTER_SCREENS_DIR
from .ascii_art import (
    GLYPH_MIX,
    MODE_COLOR,
    MODE_COLOR_SUBCELL,
    MODE_PLAIN,
    build_art,
    decode_png_rgba,
    render_subcell,
    subcell_rows,
)

ComputerAction = Literal[
    "screenshot",
    "screen",
    "launch",
    "close_app",
    "mouse_move",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "left_click_drag",
    "scroll",
    "type",
    "key",
    "wait",
    "resize",
    "screen_info",
    "spawn",
    "kill",
    "list",
]

ScrollDirection = Literal["up", "down", "left", "right"]
GlyphStyle = Literal["mix", "octant", "braille"]

DEFAULT_SCREEN_WIDTH = 1280
DEFAULT_SCREEN_HEIGHT = 720

_MAX_WAIT_SECONDS = 30.0
_DOUBLE_CLICK_GAP_SECONDS = 0.08

# The `screen` action's art-width target is clamped: below 16 cells a 1280px
# source loses every glyph of GUI text, above 200 the art stops fitting any
# terminal (and the `.art` file stops being human-readable).
ART_MIN_CHARS = 16
ART_MAX_CHARS = 200
ART_DEFAULT_CHARS = 100

# Captures are display artifacts, not archives: a GUI-driving agent can take
# hundreds of screenshots a session, and each is a `.png` (~1 MB) plus a
# `.art` (~40 KB). Without a cap the directory grows without bound, so after
# every write the oldest pairs are pruned — the same newest-wins discipline
# as the session inbox (`core/inbox.py:MAX_INBOX_MESSAGES`).
MAX_CAPTURE_FILES = 100

# The tool-result cap is 4000 chars; the `screen` result reserves the rest for
# the path, the header lines and the closing line.
_ART_RESULT_LIMIT = 3800
_ART_PREVIEW_ROWS = 24

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# xdotool-style aliases models commonly emit -> vwayland key names.
_KEY_ALIASES = {
    "control": "ctrl",
    "cmd": "super",
    "command": "super",
    "option": "alt",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "del": "delete",
    "ins": "insert",
}

_CLICK_BUTTONS = {
    "left_click": "left",
    "right_click": "right",
    "middle_click": "middle",
    "double_click": "left",
}

_SCROLL_VECTORS = {
    "up": (0, 1),
    "down": (0, -1),
    "left": (-1, 0),
    "right": (1, 0),
}


@dataclass
class Screenshot:
    """A PNG frame of a virtual screen; the registry wraps this into blocks.

    `art_path` is the sibling `.art` dump of the SAME capture, written
    best-effort by the `screenshot` action (None when the art could not be
    produced). The registry appends a `Saved to: <path>` line for it, which is
    the hook the TUI keys on to render the screen inline — the exact same line
    shape the `screen` action emits.

    `png_path` is the raw PNG written next to the `.art` (also best effort):
    the TUI's graphics-capable path renders THAT inline (Kitty TGP / Sixel
    via textual-image) where `.art` is only the fallback.
    """

    data: bytes
    width: int
    height: int
    mime_type: str = field(default="image/png")
    art_path: Path | None = None
    png_path: Path | None = None


class ComputerUseError(Exception):
    pass


try:
    import vwayland
except ImportError:
    vwayland = None

_LOCK = threading.Lock()
_SCREENS = {}  # id (str) -> vwayland.Compositor; insertion order = spawn order
_SUPPORTED: bool | None = None  # cached computer_supported() verdict

# Per-turn cancel slot, set by core.runtime.build_runtime. Computer handlers
# are SYNC (coreouto runs them via asyncio.to_thread), so the loop's task
# cancellation cannot interrupt an in-flight action — the cooperative check
# below is the only way ESC ESC shortens a long `wait` or blocks a queued
# action. It mirrors the same slot in `tools/bash.py`.
_CANCEL_EVENT: threading.Event | None = None


def set_cancel_event(event: threading.Event | None) -> None:
    global _CANCEL_EVENT
    _CANCEL_EVENT = event


def _cancelled() -> bool:
    return _CANCEL_EVENT is not None and _CANCEL_EVENT.is_set()

_MIN_GLIBC = (2, 28)
_ENV_DISABLE = {"0", "off", "no", "false"}
_ENV_ENABLE = {"1", "on", "yes", "true"}


def computer_supported() -> bool:
    """One-time verdict on whether the Computer tool should be registered.

    `MINIOUTO_COMPUTER=0/off/no/false` forces off; `=1/on/yes/true` forces on
    (skipping the platform probe, for non-standard py-Vwayland builds);
    unset means auto-detect. Cached for the process lifetime — registration
    runs on every chat turn, so this must stay cheap.
    """
    global _SUPPORTED
    if _SUPPORTED is None:
        override = os.environ.get("MINIOUTO_COMPUTER", "").strip().lower()
        if override in _ENV_DISABLE:
            _SUPPORTED = False
        elif override in _ENV_ENABLE:
            _SUPPORTED = vwayland is not None
        else:
            _SUPPORTED = vwayland is not None and _platform_supported()
    return _SUPPORTED


def _platform_supported() -> bool:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        return False
    libc, version = platform.libc_ver()
    if libc and libc != "glibc":
        return False  # musl etc. — the bundled binary needs glibc
    if libc == "glibc" and version:
        try:
            major, minor = (int(part) for part in version.split(".")[:2])
        except ValueError:
            return True  # unparseable — let the spawn attempt decide later
        if (major, minor) < _MIN_GLIBC:
            return False
    return True


def computer(
    action: ComputerAction,
    coordinate: list[int] | None = None,
    end_coordinate: list[int] | None = None,
    text: str | None = None,
    scroll_direction: ScrollDirection | None = None,
    scroll_amount: int = 3,
    duration: float = 1.0,
    screen: str | None = None,
    chars: int = ART_DEFAULT_CHARS,
    color: bool = True,
    glyph: GlyphStyle = GLYPH_MIX,
) -> str | Screenshot:
    """Run one computer-use action against the target virtual screen.

    `chars`, `color` and `glyph` are the art inputs: the width target in
    terminal cells (clamped to 16..200), whether the art is truecolour sub-cells
    or a plain luminance ramp, and which glyph family truecolour art picks per
    cell (`mix` = crisp block octants where a cell is a clean two-tone, braille
    dots where colours should blend; `braille` forces dots for fonts with no
    Unicode-16 octant coverage, `octant` forces blocks). They shape the `.art`
    dump written by `screen` and by `screenshot` (best effort); every other
    action ignores all three.
    """
    sid = None
    if _cancelled():
        raise ComputerUseError("Action cancelled by user (esc esc).")
    with _LOCK:
        try:
            if action == "spawn":
                return _do_spawn(text, coordinate)
            if action == "list":
                return _do_list()
            sid, comp = _resolve_screen(screen, autospawn=(action != "kill"))
            if action == "kill":
                comp.kill()
                del _SCREENS[sid]
                return f"Screen {sid!r} killed."
            return _dispatch(
                comp,
                sid,
                action,
                coordinate,
                end_coordinate,
                text,
                scroll_direction,
                scroll_amount,
                duration,
                chars,
                color,
                glyph,
            )
        except vwayland.CompositorNotFoundError as exc:
            if sid is not None:
                _SCREENS.pop(sid, None)
                raise ComputerUseError(
                    f"Screen {sid!r} died and was removed from the registry. "
                    "spawn it again, then re-issue the action."
                ) from exc
            raise ComputerUseError(str(exc)) from exc
        except vwayland.VwaylandError as exc:
            raise ComputerUseError(str(exc)) from exc


def _resolve_screen(screen: str | None, *, autospawn: bool) -> tuple[str, object]:
    if vwayland is None:
        raise ComputerUseError(
            "py-Vwayland failed to import even though it is a default "
            "dependency — reinstall miniouto and retry."
        )
    if screen is not None:
        comp = _SCREENS.get(screen)
        if comp is None:
            raise ComputerUseError(f"No screen {screen!r}. {_screens_summary()}")
        return screen, comp
    if not _SCREENS:
        if not autospawn:
            raise ComputerUseError("No screens are up.")
        return _spawn_screen(None, None)
    if len(_SCREENS) == 1:
        return next(iter(_SCREENS.items()))
    raise ComputerUseError(
        "Multiple screens are up; pass screen=\"<id>\" to pick one. "
        + _screens_summary()
    )


def _spawn_screen(label: str | None, size: tuple[int, int] | None) -> tuple[str, object]:
    width, height = size or (DEFAULT_SCREEN_WIDTH, DEFAULT_SCREEN_HEIGHT)
    kwargs = {"width": width, "height": height, "headless": True}
    if label:
        kwargs["id"] = label
    comp = vwayland.spawn(**kwargs)
    _SCREENS[comp.id] = comp
    return comp.id, comp


def _screens_summary() -> str:
    if not _SCREENS:
        return "No screens are up — spawn one first."
    return "Screens up: " + ", ".join(sorted(_SCREENS)) + "."


def _kill_all() -> None:
    for _sid, comp in list(_SCREENS.items()):
        comp.kill()  # documented idempotent; no-op when the process is gone
    _SCREENS.clear()


def _do_spawn(text: str | None, coordinate: list[int] | None) -> str:
    if vwayland is None:
        raise ComputerUseError(
            "py-Vwayland failed to import even though it is a default "
            "dependency — reinstall miniouto and retry."
        )
    label = text.strip() if text and text.strip() else None
    if label is not None and label in _SCREENS:
        raise ComputerUseError(
            f"Screen {label!r} already exists. kill it first or pick another label."
        )
    size = _coerce_coordinate(coordinate, "coordinate") if coordinate else None
    sid, comp = _spawn_screen(label, size)
    info = comp.info()
    return (
        f"Screen {sid!r} up ({info['width']}x{info['height']}). "
        "Launch an app on it, then screenshot. "
        f"Pass screen=\"{sid}\" on every action that should target it."
    )


def _do_list() -> str:
    if not _SCREENS:
        return "No screens. Use action=spawn to create one."
    lines = []
    for sid, comp in _SCREENS.items():
        try:
            info = comp.info()
        except vwayland.VwaylandError:
            lines.append(f"{sid}: <dead — pruned on next use>")
            continue
        app = info.get("app_pid") or "none"
        lines.append(
            f"{sid}: {info['width']}x{info['height']}, app_pid={app}, "
            f"WAYLAND_DISPLAY={info['display']}, "
            f"XDG_RUNTIME_DIR={comp.runtime_dir}"
        )
    return "\n".join(lines)


def _dispatch(
    comp,
    sid: str,
    action: ComputerAction,
    coordinate: list[int] | None,
    end_coordinate: list[int] | None,
    text: str | None,
    scroll_direction: ScrollDirection | None,
    scroll_amount: int,
    duration: float,
    chars: int = ART_DEFAULT_CHARS,
    color: bool = True,
    glyph: GlyphStyle = GLYPH_MIX,
) -> str | Screenshot:
    if action == "screenshot":
        img = comp.screenshot()
        return Screenshot(
            data=img.png_bytes,
            width=img.width,
            height=img.height,
            png_path=_write_png_best_effort(img.png_bytes, sid),
            art_path=_write_art_best_effort(img.png_bytes, sid, chars, color, glyph),
        )

    if action == "screen":
        result, _path = _write_art(comp.screenshot().png_bytes, sid, chars, color, glyph)
        return result

    if action == "launch":
        argv = shlex.split(text or "")
        if not argv:
            raise ComputerUseError(
                "action 'launch' requires text = command line, e.g. \"firefox\"."
            )
        pid = comp.launch(argv)
        return (
            f"Launched {argv[0]!r} (pid {pid}) on screen {sid!r}. "
            f"Its stdout/stderr goes to {comp.runtime_dir}/app.log. "
            "GUI apps take a moment to map their window — wait 1-2 s, "
            "then screenshot."
        )

    if action == "close_app":
        gone = comp.close_app()
        if gone:
            return f"App on screen {sid!r} closed."
        return "The app did not exit within the timeout; it may be wedged."

    if action == "mouse_move":
        x, y = _require_coordinate(coordinate, action)
        comp.move_to(x, y)
        return f"Pointer at ({x}, {y})."

    if action in _CLICK_BUTTONS:
        button = _CLICK_BUTTONS[action]
        if coordinate is None:
            comp.click(button=button)
            where = "the current pointer position"
        else:
            x, y = _coerce_coordinate(coordinate, "coordinate")
            comp.click(x, y, button=button)
            where = f"({x}, {y})"
        if action == "double_click":
            deadline = time.monotonic() + _DOUBLE_CLICK_GAP_SECONDS
            while time.monotonic() < deadline:
                if _cancelled():
                    raise ComputerUseError("Action cancelled by user (esc esc).")
                time.sleep(0.02)
            comp.click(button=button)
            return f"Double-clicked at {where}."
        return f"Clicked {button} at {where}."

    if action == "left_click_drag":
        x1, y1 = _require_coordinate(coordinate, action)
        if end_coordinate is None:
            raise ComputerUseError(
                "action 'left_click_drag' requires end_coordinate = [x, y]."
            )
        x2, y2 = _coerce_coordinate(end_coordinate, "end_coordinate")
        comp.drag(x1, y1, x2, y2)
        return f"Dragged from ({x1}, {y1}) to ({x2}, {y2})."

    if action == "scroll":
        if scroll_direction not in _SCROLL_VECTORS:
            raise ComputerUseError(
                "action 'scroll' requires scroll_direction: up/down/left/right."
            )
        if scroll_amount < 1:
            raise ComputerUseError("scroll_amount must be >= 1 (wheel detents).")
        if coordinate is not None:
            x, y = _coerce_coordinate(coordinate, "coordinate")
            comp.move_to(x, y)
        dx, dy = _SCROLL_VECTORS[scroll_direction]
        comp.scroll(dx=dx * scroll_amount, dy=dy * scroll_amount)
        return f"Scrolled {scroll_direction} by {scroll_amount} detents."

    if action == "type":
        if not text:
            raise ComputerUseError("action 'type' requires text to type.")
        comp.type_text(text)
        return f"Typed {len(text)} characters."

    if action == "key":
        if not text or not text.strip():
            raise ComputerUseError(
                "action 'key' requires text = key spec, e.g. \"enter\", "
                "\"f5\", \"ctrl+s\", \"alt+f4\"."
            )
        keys = [
            _KEY_ALIASES.get(part, part)
            for part in (p.strip().lower() for p in text.split("+"))
            if part
        ]
        if not keys:
            raise ComputerUseError(
                f"Could not parse key spec {text!r}. To type a literal "
                "'+' character use the 'type' action."
            )
        if len(keys) == 1:
            comp.key(keys[0])
        else:
            comp.combo(*keys)
        return f"Pressed {'+'.join(keys)}."

    if action == "wait":
        try:
            seconds = float(duration)
        except (TypeError, ValueError) as exc:
            raise ComputerUseError(
                f"duration must be a number of seconds, got {duration!r}."
            ) from exc
        if not 0 < seconds <= _MAX_WAIT_SECONDS:
            raise ComputerUseError(
                f"duration must be in (0, {_MAX_WAIT_SECONDS:g}] seconds."
            )
        deadline = time.monotonic() + seconds
        while True:
            if _cancelled():
                raise ComputerUseError("Action cancelled by user (esc esc).")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.05))
        return f"Waited {seconds:g} s."

    if action == "resize":
        w, h = _require_coordinate(coordinate, action)
        applied_w, applied_h = comp.resize(w, h)
        return f"Screen {sid!r} is now {applied_w}x{applied_h}."

    if action == "screen_info":
        info = comp.info()
        app = info.get("app_pid") or "none"
        return (
            f"Screen {sid}: {info['width']}x{info['height']}, "
            f"headless={info['headless']}, app_pid={app}.\n"
            "To attach extra GUI apps to this screen from Bash: "
            f"WAYLAND_DISPLAY={info['display']} "
            f"XDG_RUNTIME_DIR={comp.runtime_dir} <command> &"
        )

    raise ComputerUseError(f"Unknown action {action!r}.")


def _require_coordinate(value, action: str) -> tuple[int, int]:
    if value is None:
        raise ComputerUseError(f"action {action!r} requires coordinate = [x, y].")
    return _coerce_coordinate(value, "coordinate")


def _coerce_coordinate(value, name: str) -> tuple[int, int]:
    ok = isinstance(value, (list, tuple)) and len(value) == 2
    x = y = 0
    if ok:
        try:
            x = round(float(value[0]))
            y = round(float(value[1]))
        except (TypeError, ValueError):
            ok = False
    if not ok:
        raise ComputerUseError(
            f"{name} must be [x, y] (two numbers), got {value!r}. "
            "Coordinates are logical screen pixels; (0, 0) is top-left."
        )
    return x, y


# ── the `screen` action: screenshot -> Unicode sub-cell art -> .art file ─────

_ART_MODE_LABELS = {
    MODE_COLOR: "halfblock truecolor",
    MODE_COLOR_SUBCELL: "subcell truecolor",
    MODE_PLAIN: "ramp plain",
}


def _render_screen_art(
    png_bytes: bytes, sid: str, chars: int, color: bool, glyph: GlyphStyle = GLYPH_MIX
) -> tuple[str, str, Path]:
    """Build the tool-result string, the `.art` file text and its path.

    Pure with respect to the compositor and the filesystem: everything the
    caller needs to persist the capture is returned, so this is testable
    without a screen.
    """
    cols = max(ART_MIN_CHARS, min(ART_MAX_CHARS, int(chars)))
    try:
        width, height, rgba = decode_png_rgba(png_bytes)
    except ValueError as exc:
        raise ComputerUseError(f"Could not decode the screen capture: {exc}") from exc

    body = render_subcell(rgba, width, height, cols=cols, color=color, glyph=glyph)
    rows = subcell_rows(width, height, cols)
    now = datetime.now()
    path = COMPUTER_SCREENS_DIR / f"{_safe_file_part(sid)}-{now:%Y-%m-%d-%H-%M-%S}.art"

    meta = {
        "screen": sid,
        "captured": now.replace(microsecond=0).isoformat(),
        "source": f"{width}x{height}",
        "grid": f"{cols}x{rows}",
        "cell": f"{round(width / (2 * cols))}x{round(height / (4 * rows))}",
        "mode": MODE_COLOR_SUBCELL if color else MODE_PLAIN,
    }
    return _art_result(meta, body, path), build_art(meta, body), path


def _safe_file_part(sid: str) -> str:
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", sid).strip("_")
    return cleaned or "screen"


def _write_art(
    png_bytes: bytes, sid: str, chars: int, color: bool, glyph: GlyphStyle = GLYPH_MIX
) -> tuple[str, Path]:
    result, art_text, path = _render_screen_art(png_bytes, sid, chars, color, glyph)
    COMPUTER_SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(art_text, encoding="utf-8")
    # Pruning is housekeeping: it must never cost the capture.
    with contextlib.suppress(Exception):
        _prune_captures()
    return result, path


def _write_art_best_effort(
    png_bytes: bytes, sid: str, chars: int, color: bool, glyph: GlyphStyle = GLYPH_MIX
) -> Path | None:
    """The `.art` sibling for a `screenshot`, or None when it could not be made.

    Best effort on purpose: the screenshot's pixels are the deliverable, so any
    decode / render / write problem (an undecodable PNG, a full disk, a
    read-only home) costs the art file and nothing else — never the capture.
    """
    try:
        _result, path = _write_art(png_bytes, sid, chars, color, glyph)
    except Exception:
        return None
    return path


def _prune_captures() -> None:
    """Keep only the newest `MAX_CAPTURE_FILES` capture files in the screens dir.

    `.art` and `.png` are one pool: they share a stem, so pruning oldest-first
    by name (the timestamp is in the name) retires a capture's two siblings
    together. Best effort like the writes themselves — an unlink failure is
    skipped, never raised into a tool call.
    """

    try:
        files = sorted(COMPUTER_SCREENS_DIR.glob("*.*"))
        doomed = files[: max(len(files) - MAX_CAPTURE_FILES, 0)]
        for path in doomed:
            try:
                path.unlink()
            except OSError:
                continue
    except OSError:
        return


def _write_png_best_effort(png_bytes: bytes, sid: str) -> Path | None:
    """The raw-PNG sibling for a `screenshot`, or None when it could not be made.

    Same best-effort contract as the `.art` write: the pixels are already the
    tool result, so a filesystem problem costs the sibling file, never the
    capture. The TUI's graphics lane reads this file; the fallback lane reads
    the `.art`. The name mirrors the `.art` name (same screen id + timestamp
    second) so a directory listing pairs them; the art write immediately
    follows and shares the second, so the pair only splits at a same-second
    boundary failure, which costs nothing but an unmatched file.
    """
    try:
        COMPUTER_SCREENS_DIR.mkdir(parents=True, exist_ok=True)
        path = (
            COMPUTER_SCREENS_DIR
            / f"{_safe_file_part(sid)}-{datetime.now():%Y-%m-%d-%H-%M-%S}.png"
        )
        # No prune here: a `screenshot` is always followed by its `.art`
        # write in `_dispatch`, which prunes the pair.
        path.write_bytes(png_bytes)
    except Exception:
        return None
    return path


def _art_result(meta: dict[str, str], body: str, path: Path) -> str:
    header = (
        f"Screen art captured ({meta['source']} -> {meta['grid']} chars, "
        f"{_ART_MODE_LABELS[meta['mode']]}).\n"
        f"Saved to: {path}\n"
        "Click coordinates still come from the screenshot action.\n"
    )
    lines = body.split("\n")
    preview, shown = _fit_preview(lines, len(header) + _RESULT_OVERHEAD)
    result = (
        header
        + f"Full art: read that file with `cat`. Preview (first {shown} rows):\n"
        + "\n".join(preview)
    )
    if len(lines) > shown:
        result += f"\n… {len(lines) - shown} more rows are in the file above."
    return result


#: Worst case for the closing "… N more rows" line and the preview caption.
_RESULT_OVERHEAD = 120


def _fit_preview(lines: list[str], used: int) -> tuple[list[str], int]:
    budget = _ART_RESULT_LIMIT - used
    chosen: list[str] = []
    total = 0
    for line in lines[:_ART_PREVIEW_ROWS]:
        cost = len(line) + 1
        if total + cost > budget:
            break
        chosen.append(line)
        total += cost
    return chosen, len(chosen)
