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
"""

from __future__ import annotations

import os
import platform
import shlex
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

ComputerAction = Literal[
    "screenshot",
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

DEFAULT_SCREEN_WIDTH = 1280
DEFAULT_SCREEN_HEIGHT = 720

_MAX_WAIT_SECONDS = 30.0
_DOUBLE_CLICK_GAP_SECONDS = 0.08

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
    """A PNG frame of a virtual screen; the registry wraps this into blocks."""

    data: bytes
    width: int
    height: int
    mime_type: str = field(default="image/png")


class ComputerUseError(Exception):
    pass


try:
    import vwayland
except ImportError:
    vwayland = None

_LOCK = threading.Lock()
_SCREENS = {}  # id (str) -> vwayland.Compositor; insertion order = spawn order
_SUPPORTED: bool | None = None  # cached computer_supported() verdict

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
) -> str | Screenshot:
    """Run one computer-use action against the target virtual screen."""
    sid = None
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
) -> str | Screenshot:
    if action == "screenshot":
        img = comp.screenshot()
        return Screenshot(data=img.png_bytes, width=img.width, height=img.height)

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
            time.sleep(_DOUBLE_CLICK_GAP_SECONDS)
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
        time.sleep(seconds)
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
