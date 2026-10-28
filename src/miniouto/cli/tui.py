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
   │ Working spinner (1 row)                     │
   │ outo: <model> <reasoning>            <style>│   <- clickable chips, row A
   │ subagent: <model> <reasoning>   session: <n>│   <- clickable chips, row B
   │ Tab/click chips · Enter open · Esc cancel   │   <- help hint
   └─────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar

from rich.cells import cell_len
from rich.markdown import Markdown
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click, Leave, MouseDown, MouseMove
from textual.geometry import Offset
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.strip import Strip
from textual.theme import Theme
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TextArea,
)

from .. import __version__, paths_runtime
from ..core import context as context_api
from ..core import inbox, runtime, summary
from ..core import lma as catalog_api
from ..core.chat import ChatOptions, run_chat
from ..core.events import STREAM_THINKING, LoopEvent
from ..core.providers import SUPPORTED_FORMATS, add_provider_from_lma, sdk_to_format
from ..core.runtime import LoopCancelledError, default_provider_config
from ..storage import paths, session_config
from ..storage import providers as provider_store
from ..storage import sessions as session_store
from ..storage import settings as settings_store
from ..storage import styles as style_store
from ..storage.providers import SOURCE_CUSTOM
from ..tools import ascii_art
from .chat import handoff_marker

SENTINEL_CATALOG_ADD = "__catalog_add__"
SENTINEL_CUSTOM_ADD = "__custom_add__"
SENTINEL_SAME_AS_OUTO = "__same_as_outo__"

# Field name of the settings-popup row that edits a custom provider's caps.
# `SettingRow` capitalizes the field, so this value IS the visible label.
CAPS_FIELD = "caps"

# Sentinel for `_construct_provider_with_reasoning(reasoning_effort=...)`:
# don't touch the provider's existing value (vs. None which would clear it).
_SENTINEL_NO_REASONING = object()

# Two escape presses within this window (seconds) while busy force-stop the loop.
_ESC_DOUBLE_PRESS_WINDOW = 2.0

# Config-only fallback before a TUI has chosen a session. It is deliberately
# NOT an inbox destination: every unassigned TUI needs its own pending queue.
_DEFAULT_SESSION = "default"


def _parse_optional_int(result: str | None) -> int | None:
    """Parse a TextInputModal int result.

    Returns int for valid positive input, None for empty/cancelled (the
    caller treats None as "clear the override"). Raises ValueError for
    non-numeric, zero, or negative input so the caller can keep the
    existing value and post a system message.
    """
    if result is None:
        return None
    stripped = result.strip()
    if not stripped:
        return None
    v = int(stripped)
    if v <= 0:
        raise ValueError("must be > 0")
    return v


def _get_reasoning_api():
    """Lazy import of `miniouto.core.reasoning`; returns None if missing.

    The parallel work that defines `reasoning_choices` /
    `default_reasoning_choice` may not have landed yet, so we defer the
    import to call time. Callers treat None as "no reasoning options
    available" and skip the picker.
    """
    try:
        from ..core import reasoning as reasoning_api
    except ImportError:
        return None
    return reasoning_api


def _reasoning_value(provider: provider_store.Provider) -> str | None:
    """Defensive read of the (parallel-work) `reasoning_effort` field."""
    return getattr(provider, "reasoning_effort", None)

def _reasoning_chip_value(provider: provider_store.Provider | None) -> str:
    """Render a chip's reasoning part: the configured level, or `-` for unset.

    An explicit "off"/"none" is a real setting and stays visible as "off";
    only a provider with no `reasoning_effort` collapses to the `-` the rest
    of the chips use for an unknown value.
    """

    if provider is None:
        return "-"
    re_val = _reasoning_value(provider)
    if re_val is None:
        return "-"
    if re_val.lower() in ("off", "none"):
        return "off"
    return re_val


def _with_reasoning_effort(
    provider: provider_store.Provider,
    value: str | None,
) -> provider_store.Provider:
    """Return a copy of `provider` with `reasoning_effort` set to `value`.

    Falls back to stashing in `extra` if the field hasn't landed on
    `Provider` yet (parallel work in flight). `None` clears the override.
    """
    try:
        return replace(provider, reasoning_effort=value)
    except TypeError:
        extra = {k: v for k, v in provider.extra.items() if k != "reasoning_effort"}
        if value is not None:
            extra["reasoning_effort"] = value
        return replace(provider, extra=extra)


def _construct_provider_with_reasoning(**kwargs: Any) -> provider_store.Provider:
    """Construct a `Provider`, surfacing `reasoning_effort` if supported.

    If the field hasn't landed yet, the `reasoning_effort` kwarg is moved
    into `extra` so the value survives a round-trip.
    """
    re_kw = kwargs.pop("reasoning_effort", _SENTINEL_NO_REASONING)
    if re_kw is _SENTINEL_NO_REASONING:
        return provider_store.Provider(**kwargs)
    try:
        return provider_store.Provider(**kwargs, reasoning_effort=re_kw)
    except TypeError:
        extra = {k: v for k, v in kwargs.get("extra", {}).items() if k != "reasoning_effort"}
        if re_kw is not None:
            extra["reasoning_effort"] = re_kw
        kwargs["extra"] = extra
        return provider_store.Provider(**kwargs)


def _record(name: str) -> provider_store.Provider | None:
    """The provider record `name`, or None for an unset (inheriting) key."""

    return provider_store.get(name) if name else None


def _default_reasoning(model: str, provider_name: str) -> str | None:
    """lma's auto reasoning default for a model, or None (no options / lookup failed)."""

    reasoning_api = _get_reasoning_api()
    if reasoning_api is None or not model:
        return None
    try:
        return reasoning_api.default_reasoning_choice(model, provider_name)
    except Exception:
        return None


def _reasoning_choices(model: str, provider_name: str) -> list[str] | None:
    """The effort levels a model offers, or None when it has no reasoning.

    A lma transport failure is indistinguishable from "no reasoning options"
    here, exactly as it is in `resolve_reasoning_passthrough`: refusing to
    send reasoning is never the safe default, so the caller degrades to
    "leave whatever is configured alone".
    """

    reasoning_api = _get_reasoning_api()
    if reasoning_api is None or not model:
        return None
    try:
        return reasoning_api.reasoning_choices(model, provider_name)
    except Exception:
        return None


def _model_defaults(provider_name: str, model: str = "") -> tuple[str, str]:
    """The (model, reasoning) a provider hands a session that just picked it.

    The record's own `default_model` / `reasoning_effort` win — they ARE that
    provider's configured defaults, and rewriting them here would clobber a
    preference another process set for the same provider. lma's auto default
    fills the reasoning gap; both empty means "this model offers no reasoning".
    """

    record = _record(provider_name)
    if record is None:
        return model, ""
    resolved = model or record.default_model
    return resolved, (_reasoning_value(record) or _default_reasoning(resolved, provider_name) or "")


def _effective_config(session: str) -> session_config.ProviderConfig:
    """The provider config `session` resolves to: its pin, else the default.

    Deliberately `session_config.read`, NOT `effective`: merely LOOKING at the
    chips or opening the settings popup must not freeze a pin for a session
    nobody configured. Freezing belongs to the turn (`runtime._pinned_for`),
    so a session with no pin keeps following the global default live and an
    edit made in another process shows up here immediately.
    """

    return session_config.read(session) or default_provider_config()


@dataclass(frozen=True)
class TrioTarget:
    """Which provider trio a settings popup edits.

    `fields` are the three field names the pin (`session_config.ProviderConfig`)
    and the global default (`storage.Settings`) share for that trio, in popup
    row order. `inherits` marks the subagent trio, whose empty keys mean
    "same as outo" rather than "unset".
    """

    title: str
    fields: tuple[str, str, str]
    inherits: bool


OUTO_TARGET = TrioTarget("outo settings", ("provider", "model", "reasoning"), False)
SUBAGENT_TARGET = TrioTarget(
    "subagent settings", ("subagent_provider", "subagent_model", "subagent_reasoning"), True
)


def _trio(
    config: session_config.ProviderConfig, fields: tuple[str, str, str]
) -> tuple[str, str, str]:
    """One trio out of a config, in row order."""

    provider, model, reasoning = fields
    return (getattr(config, provider), getattr(config, model), getattr(config, reasoning))


def _trio_display(
    target: TrioTarget, config: session_config.ProviderConfig
) -> tuple[str, str, str]:
    """The three values a trio's rows read: the configured one, else what it
    resolves to (the provider record's own default), else `-`.

    An inheriting trio (the subagent's) reads `same as outo` instead of `-`,
    so a row states what actually runs rather than looking unset.
    """

    provider, model, reasoning = _trio(config, target.fields)
    record = _record(provider)
    empty = "same as outo" if target.inherits else ""
    return (
        (provider or empty) or "-",
        (model or (record.default_model if record else "") or empty) or "-",
        (reasoning or _reasoning_chip_value(record) or empty) or "-",
    )


def _reasoning_target(
    target: TrioTarget, config: session_config.ProviderConfig
) -> tuple[str, str]:
    """The (provider, model) a trio's reasoning level belongs to.

    A subagent trio with no provider of its own runs on outo's, so its
    reasoning choices come from there — the same inheritance
    `runtime._resolve_subagent` applies when resolving a spawned run.
    """

    provider, model, _ = _trio(config, target.fields)
    if not provider:
        provider, model, _ = _trio(config, OUTO_TARGET.fields)
    if not model:
        model, _ = _model_defaults(provider)
    return provider, model


def _caps_value(record: provider_store.Provider) -> str:
    """A provider record's caps as one cell: `context / output`, `-` when unset.

    Unset is the interesting state, not an error: it means "follow lma", and
    for the output side that lands on the 16K floor.
    """

    ctx = record.max_context_window if record.max_context_window else "-"
    tokens = record.max_output_tokens if record.max_output_tokens else "-"
    return f"{ctx} / {tokens}"


def _persist_trio(
    session: str,
    target: TrioTarget,
    values: tuple[str, str, str],
) -> None:
    """Write one settings edit through BOTH the session pin and the global default.

    The pin is what THIS session resolves (`runtime._pinned_for`), so an edit
    here survives a later default change; the default is what every other
    process — and the next session — resolves, and the owner's rule is that the
    most recently changed default wins. Every edit therefore writes both, so
    two processes editing different sessions never silently redirect each
    other while a fresh session still picks up the newest choice.

    outo keeps invariant 12's shape (the active model lives on the provider
    record, `settings.model` is never written); the subagent trio writes the
    three `subagent_*` settings, where empty means "same as outo". An empty
    value CLEARS the pin key, handing it back to the global default — the
    documented `session_config` semantics, not a reset to nothing.
    """

    provider_key, model_key, reasoning_key = target.fields
    provider, model, reasoning = values
    session_config.set_fields(
        session,
        **{provider_key: provider, model_key: model, reasoning_key: reasoning},
    )
    if target.inherits:
        settings_store.save(
            replace(
                settings_store.load(),
                subagent_provider=provider,
                subagent_model=model,
                subagent_reasoning=reasoning,
            )
        )
        return
    settings_store.update(provider=provider)
    record = _record(provider)
    if record is not None:
        provider_store.upsert(
            _with_reasoning_effort(replace(record, default_model=model), reasoning or None)
        )


# Braille spinner frames. The status line reads e.g. "⠧ Bash…" and the
# glyph rotates through this set at ~12.5fps so the bottom of the screen
# shows the agent is alive even between tool calls.
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_INTERVAL = 0.08
_SPINNER_DEFAULT_TEXT = "Working…"

# How often the idle TUI drains `core.inbox`. No registry callback pushes
# messages any more (a detached `miniouto chat --bg` run and
# `miniouto message insert` write into a session's inbox directory and may
# finish at any moment), so the poll is
# what turns a queued message into a turn.
_INBOX_POLL_INTERVAL = 1.0

# `LoopEvent.detail` prefix the core stamps on the event of a drained inbox
# message — the de-duplication handle against the optimistic row.
_INBOX_DETAIL_PREFIX = "inbox:"

# Live token streaming. coreouto fires one hook per token, so a burst of
# deltas is folded into a single UI-thread update roughly every 33 ms
# (~30 Hz) — fast enough to look like typing, cheap enough that a 400-token
# burst repaints a dozen times instead of four hundred.
_STREAM_FLUSH_INTERVAL = 0.033

# Chat-log windowing. Mounting every row of a long session makes Textual
# lay out thousands of widgets on each reflow (the reported slowdown), so
# only a contiguous suffix of `ChatTUI._rows` is mounted at a time; older
# rows mount in chunks as the user scrolls toward the top. Row objects
# are kept alive off-DOM, so collapse state, tool results, and subagent
# status survive eviction.
_WINDOW_INITIAL_ROWS = 100  # tail rows mounted on session load
_WINDOW_CHUNK_ROWS = 50  # rows prepended per top-edge chunk load
_WINDOW_MAX_ROWS = 300  # soft cap; excess evicted from the top
_TOP_LOAD_LINES = 10  # scroll_y at/below which a chunk load triggers
_EVICT_MARGIN_LINES = 60  # rows this close above the viewport are kept

# Rows of screen art rendered inline in an expanded Computer tool row. A
# full screenshot is hundreds of rows; the point is to SEE the screen, not
# to scroll it, and the rest is one `miniouto computer show` away.
_ART_MAX_ROWS = 40

# ANY Computer result that names a `.art` file: the `screen` action ends with
# "Saved to: …", and a `screenshot` appends the same line to its own summary —
# so the gate is the line, never the action, and a screenshot renders too. The
# `.art` extension is what makes this unambiguous; a stray ".art" inside
# command output cannot match it.
_ART_SAVED_RE = re.compile(r"Saved to:\s*(?P<path>\S+\.art)\s*$", re.MULTILINE)


def art_path_in_result(result: str) -> str | None:
    """The `.art` path a Computer tool result names, or None.

    Matched on its own line so a path echoed mid-sentence does not count.
    """

    match = _ART_SAVED_RE.search(result)
    return match.group("path") if match else None


# A `screenshot` result ALSO names the raw PNG written next to the `.art`
# (tools/registry.py appends `Image saved to: …`); on a graphics-capable
# terminal that file is what the row renders and the `.art` is the fallback.
_PNG_SAVED_RE = re.compile(r"Image saved to:\s*(?P<path>\S+\.png)\s*$", re.MULTILINE)


def png_path_in_result(result: str) -> str | None:
    """The raw-PNG path a Computer `screenshot` result names, or None."""

    match = _PNG_SAVED_RE.search(result)
    return match.group("path") if match else None


# ── terminal graphics mode ──────────────────────────────────────────────────
#
# The mode is decided ONCE, BEFORE Textual starts: the probe reads the
# terminal's reply to an escape query from stdin, and Textual's own reader
# thread takes stdin over the moment the app runs — after that the reply is
# never seen and the probe just burns its timeout. So the check belongs here,
# not in the render path.
#
# `TEXT`     — the `.art` truecolour text render (always available)
# `TGP`      — Kitty's Terminal Graphics Protocol (pixel-exact images)
# `SIXEL`    — the Sixel graphics protocol
#
# The mode is a module-level value, set by `detect_graphics_mode()` and
# read by the render path. A failed probe (no package, no tty, a dead
# stdout, a timeout) is TEXT — the unchanged fallback, never an error.
GRAPHICS_MODE_TEXT = "text"
GRAPHICS_MODE_TGP = "tgp"
GRAPHICS_MODE_SIXEL = "sixel"

_GRAPHICS_MODE: str | None = None

# The image widget classes, imported during detection (not at render time):
# they pull in Pillow, and the render path must not pay for an import while
# the user waits for a screenshot to appear.
_IMAGE_WIDGETS: dict[str, Any] = {}


def detect_graphics_mode() -> str:
    """Probe the terminal ONCE and pick the graphics mode.

    Call this BEFORE Textual starts (see `run_tui`). The probe asks the
    terminal whether it speaks TGP or Sixel and reads the answer off stdin;
    Textual's own reader thread takes stdin over the moment the app runs, and
    after that the answer is never seen — the probe then only burns its
    2-second timeout. So the check belongs here, not in the render path.

    Returns `GRAPHICS_MODE_TGP` for Kitty-style graphics, `GRAPHICS_MODE_SIXEL`
    for Sixel, and `GRAPHICS_MODE_TEXT` when neither is available (also the
    result of ANY failure — a missing textual-image, a piped stdout, a
    timeout, an unparsable reply). The result is cached for the process.

    **Sixel wins when the terminal offers both.** TGP is the better protocol,
    but its "image placement" scheme needs a terminal that implements the
    placeholder protocol properly, and several terminals only *report* TGP
    support (the query is answered) without implementing it — Konsole and
    wezterm are the known cases, and there the TGP image never appears.
    Sixel is a plain byte stream that any Sixel terminal renders, so it is
    the safe choice whenever it is available. Only a terminal with Sixel
    *absent* gets TGP — in practice Kitty, which is exactly the terminal
    that implements the placeholder scheme properly. (textual-image's own
    auto-renderable picks Sixel first for the same reason.)

    When a graphics mode is found, the image widget is imported HERE too:
    it pulls in Pillow (~0.3 s), and paying that during boot is better than
    paying it on the first screenshot the user is waiting for.
    """

    global _GRAPHICS_MODE
    if _GRAPHICS_MODE is not None:
        return _GRAPHICS_MODE
    mode = GRAPHICS_MODE_TEXT
    try:
        from textual_image._terminal import probe_terminal

        caps = probe_terminal()
        if caps.sixel:
            mode = GRAPHICS_MODE_SIXEL
        elif caps.tgp:
            mode = GRAPHICS_MODE_TGP
    except Exception:
        # No textual-image, no tty, a closed stdout, a timeout: the text
        # lane is the answer, and it is always available.
        _GRAPHICS_MODE = GRAPHICS_MODE_TEXT
        return _GRAPHICS_MODE
    if mode != GRAPHICS_MODE_TEXT:
        try:
            # Warm the import now (PIL + the widget classes) so the first
            # screenshot renders without an import stall.
            from textual_image.widget import SixelImage

            from .graphics import CachedTGPImage

            # TGP gets the cached widget: the stock one re-uploads the whole
            # image on every repaint (see cli/graphics.py). Sixel keeps the
            # stock widget — it caches its encoded sixels itself.
            _IMAGE_WIDGETS[GRAPHICS_MODE_TGP] = CachedTGPImage
            _IMAGE_WIDGETS[GRAPHICS_MODE_SIXEL] = SixelImage
        except Exception:
            mode = GRAPHICS_MODE_TEXT
            _IMAGE_WIDGETS.clear()
    _GRAPHICS_MODE = mode
    return mode


def graphics_mode() -> str:
    """The graphics mode for the render path — NEVER probes.

    The probe is a terminal round-trip, and doing it lazily here (i.e.
    after Textual has started) is exactly the stall this design avoids. So
    this reads the detected mode and reports TEXT when detection never ran:
    an undetected terminal is treated as a non-graphics one, which is the
    safe, always-available lane.
    """

    return _GRAPHICS_MODE or GRAPHICS_MODE_TEXT


def terminal_graphics_supported() -> bool:
    """True when the terminal can show REAL graphics (Kitty TGP or Sixel)."""

    return graphics_mode() != GRAPHICS_MODE_TEXT


# Tolerance (lines) for "the log is at the bottom". Sub-pixel scroll
# rounding otherwise leaves the view parked one or two lines short of
# `max_scroll_y`, which would make the jump button flicker while the user
# is simply following the tail.
_BOTTOM_TOLERANCE_LINES = 2


def at_bottom(log: Any | None) -> bool:
    """True when `log` is scrolled to (or within tolerance of) its tail.

    An unlaid-out or absent log counts as "at bottom": the jump button only
    ever appears in response to scrolling AWAY, so the safe default is to
    stay hidden.
    """

    if log is None:
        return True
    max_scroll = getattr(log, "max_scroll_y", 0.0) or 0.0
    if max_scroll <= 0:
        return True
    return (log.scroll_y or 0.0) >= max_scroll - _BOTTOM_TOLERANCE_LINES


# Chat column width cap (cells). Past this the log, the input and the chips
# row stop growing and stay centered — a 300-column tool output otherwise
# stretches a sentence across half the screen. Every width-capped element
# below reads this one number.
CHAT_MAX_WIDTH = 150


def logo_columns(log_width: int, app_width: int) -> int:
    """Cells the start-up logo may occupy: the chat column, never the terminal.

    `log_width` is 0 on the very first `on_mount`, before the log has been
    laid out, so the app width stands in until it is known — either way the
    result is capped, because the logo is drawn INSIDE the capped column.
    """

    width = log_width or app_width
    return min(width, CHAT_MAX_WIDTH) if width > 0 else CHAT_MAX_WIDTH

# Logo: 180x36 packed bitmap from logo.png. _render_logo() scales to fit.
_LOGO_PIX_W = 180
_LOGO_PIX_H = 36
_LOGO_BITS = bytes.fromhex(
    "0000000000000000000000c00000000000000000000000"
    "000000000000000000001ffe0000000000000003ff0000"
    "000000000000000000007fff800000000000000fffe000"
    "000000000f0000007800ffffe00000000000003ffff000"
    "000000000f0000007803fffff0000000000000fffffc00"
    "000000000f0000007807f803f8000000000001ff01fe00"
    "000000000f000000780fe000fc000000000003f8007f00"
    "0000000000000000001f80007e000000000003f0001f80"
    "0000000000000000001f00e03f000000000007c0780fc0"
    "0000000000000000003e01e01f00000000000f80f807c0"
    "0000000000000000007c03f00f80000000000f80fc03e0"
    "000000000f000000787c07f80780000001e01f01fe03e0"
    "000000000f00000078787fff87c0000001e01e07ff81f0"
    "000000000f00000078f8ffffc3c0000001e03e3ffff1f0"
    "000000000f00000078f8ffffc3c0000001e03e7ffff0f0"
    "fffffff80f1ffff878f07fffc3c3800e1ffc3c7ffff8f0"
    "fffffff80f1ffff878f07fff83c3800e1ffc3c7ffff0f0"
    "fffffff80f1ffff878f03fff83e3800e1ffc3c3ffff0f0"
    "f001c0070f1e007878f07fff83e3800e01e03c1fffe0f0"
    "f001c0070f1e007878f07fff83c3800e01e03c1fffc0f0"
    "f001c0070f1e007878f07fffc3c3800e01e03c0fffc0f0"
    "f001c0070f1e007878f8ffffc3c3800e01e03c0fffc0f0"
    "f001c0070f1e007878787fffc7c3800e01e03e0fffc0f0"
    "f001c0070f1e007878787fff87c3800e01e01e0fffc1f0"
    "f001c0070f1e0078787c07f80f83800e01e01f0fffc1e0"
    "f001c0070f1e0078783e03f00f83800e01e01f0fcf83e0"
    "f001c0070f1e0078783e01e01f03800e01e00f820107c0"
    "f001c0070f1e0078781f00c03f03800e01e007c0000fc0"
    "f001c0070f1e0078780fc0007e03800e01e007e0001f80"
    "f001c0070f1e0078780fe001fc03800e01e003f8003f00"
    "f001c0070f1e00787807fc07f803800e01e001fe00fe00"
    "f001c0070f1e00787801fffff003fffe01fc00fffffc00"
    "f001c0070f1e00787800ffffc003fffe01fc003ffff800"
    "f001c0070f1e007878003fff8003fffe01fc001fffe000"
    "f001c0070f1e007878000ffc0003fffe01fc0007ff8000"
    "0000000000000000000000000000000000000000000000"
)


def _unpack_logo_pixels() -> list[list[int]]:
    row_bytes = (_LOGO_PIX_W + 7) // 8
    return [
        [
            (_LOGO_BITS[y * row_bytes + x // 8] >> (7 - x % 8)) & 1
            for x in range(_LOGO_PIX_W)
        ]
        for y in range(_LOGO_PIX_H)
    ]


def _pixels_to_braille(grid: list[list[int]], w: int, h: int) -> str:
    rows = []
    for gy in range(0, h, 4):
        line = []
        for gx in range(0, w, 2):
            v = 0
            if grid[gy][gx]:
                v |= 0x01
            if gy + 1 < h and grid[gy + 1][gx]:
                v |= 0x02
            if gy + 2 < h and grid[gy + 2][gx]:
                v |= 0x04
            if grid[gy][gx + 1]:
                v |= 0x08
            if gy + 1 < h and grid[gy + 1][gx + 1]:
                v |= 0x10
            if gy + 2 < h and grid[gy + 2][gx + 1]:
                v |= 0x20
            if gy + 3 < h and grid[gy + 3][gx]:
                v |= 0x40
            if gy + 3 < h and grid[gy + 3][gx + 1]:
                v |= 0x80
            line.append(chr(0x2800 + v))
        rows.append("".join(line).rstrip("⠀"))
    while rows and not rows[0]:
        rows.pop(0)
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows)


def _render_logo(max_chars: int) -> str:
    if max_chars < 10:
        return "miniouto"
    src = _unpack_logo_pixels()
    target_w = min(max_chars * 2, _LOGO_PIX_W)
    target_w -= target_w % 2
    if target_w >= _LOGO_PIX_W:
        return _pixels_to_braille(src, _LOGO_PIX_W, _LOGO_PIX_H)
    scale = target_w / _LOGO_PIX_W
    target_h = int(_LOGO_PIX_H * scale)
    target_h -= target_h % 4
    if target_h < 4:
        target_h = 4
    scaled = [
        [
            src[min(int(py / scale), _LOGO_PIX_H - 1)]
            [min(int(px / scale), _LOGO_PIX_W - 1)]
            for px in range(target_w)
        ]
        for py in range(target_h)
    ]
    return _pixels_to_braille(scaled, target_w, target_h)

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
        width: auto;
        padding: 0 1;
        margin: 0 1 0 0;
        background: $boost;
        color: $text;
        text-style: bold;
        text-wrap: nowrap;
    }
    StatusChip.-muted {
        color: $text-muted;
    }
    StatusChip.-accent {
        color: $accent;
    }
    StatusChip:hover {
        background: $primary 12%;
        text-style: bold underline;
    }
    StatusChip:focus {
        background: $primary 22%;
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
        variant: str = "default",
    ) -> None:
        super().__init__(id=id)
        if variant != "default":
            self.add_class(f"-{variant}")
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
        self.refresh(layout=True)

    def render(self) -> Text:
        # Rendered whole, never elided: `width: auto` + `nowrap` grows the chip
        # to fit its value, so do not re-add a `text-overflow: ellipsis`.
        text = self._value or "-"
        display = f"{self._label}: {text}" if self._label else text
        return Text(display)

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
    Pass `sections` to render non-selectable header rows above grouped
    `(value, label)` options; the default (no sections) renders a flat
    list, unchanged.
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
    #selection-list > ListItem.-header {
        background: $boost;
        color: $text-muted;
        text-style: bold;
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
        sections: list[tuple[str, list[tuple[str, str]]]] | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        self._current = current
        self._allow_none = allow_none
        self._none_label = none_label
        self._extra_options = list(extra_options or [])
        self._sections = list(sections or [])
        self._row_values: dict[ListItem, str | None] = {}
        # Option label -> its row in the ListView, which stops matching the
        # option index as soon as header rows are inserted above it.
        self._row_index_by_value: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="selection-dialog"):
            yield Label(self._title, id="selection-title")
            items: list[ListItem] = []
            row = 0

            def add_option(label: str, value: str | None, *, mark: bool = False) -> None:
                nonlocal row
                marker = "● " if mark else "  "
                item = ListItem(Label(marker + label), id=f"row-{row}")
                self._row_values[item] = value
                if value is not None:
                    self._row_index_by_value[value] = row
                items.append(item)
                row += 1

            if self._allow_none:
                none_item = ListItem(Label(self._none_label), id="row-none")
                self._row_values[none_item] = None
                items.append(none_item)
                row = 1
            for header, opts in self._sections:
                if not opts:
                    continue
                items.append(ListItem(Label(header), id=f"row-{row}", classes="-header"))
                row += 1
                for value, label in opts:
                    add_option(label, value, mark=value == self._current)
            for opt in self._options:
                add_option(opt, opt, mark=opt == self._current)
            for sentinel, label in self._extra_options:
                add_option(label, sentinel)
            yield ListView(*items, id="selection-list")
            yield Label("Enter to select · Esc to cancel", id="selection-hint")

    def on_mount(self) -> None:
        lv = self.query_one("#selection-list", ListView)
        # Pre-select the current value so Enter picks it without further input.
        if self._current:
            idx = self._row_index_by_value.get(self._current)
            if idx is not None:
                lv.index = idx
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item not in self._row_values:
            return
        self.dismiss(self._row_values[event.item])
        event.stop()

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
        # Input.Submitted bubbles by default; the parent ChatTUI has its own
        # on_input_submitted that would otherwise dispatch the entered value
        # (e.g. an API key) as a chat prompt. Stop propagation here so modal
        # submits stay scoped to the modal.
        event.stop()

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)


class SettingRow(Static):
    """One `Field: value` line of the provider settings popup.

    Clicking it (or pressing Enter) posts `Activated` naming the field it
    edits; the popup owns the picker. A row with nothing to pick from — a
    model that offers no reasoning levels — takes no clicks and renders
    muted, so it reads as unavailable instead of silently doing nothing.
    """

    can_focus = True

    class Activated(Message):
        """Posted when the row is activated (click or Enter)."""

        def __init__(self, row: SettingRow) -> None:
            super().__init__()
            self.row = row

    DEFAULT_CSS = """
    SettingRow {
        height: 1;
        width: 1fr;
        padding: 0 1;
        color: $text;
    }
    SettingRow.-disabled {
        color: $text-muted;
    }
    SettingRow:hover {
        background: $primary 12%;
    }
    SettingRow:focus {
        background: $primary 22%;
    }
    """

    def __init__(self, field: str, value: str, *, enabled: bool = True) -> None:
        super().__init__()
        self._field = field
        self._value = value
        self._enabled = enabled
        if not enabled:
            self.add_class("-disabled")

    @property
    def field(self) -> str:
        return self._field

    @property
    def value(self) -> str:
        return self._value

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_value(self, value: str, *, enabled: bool | None = None) -> None:
        """Repaint the row; `enabled` is only touched when it is passed."""
        self._value = value
        if enabled is not None and enabled != self._enabled:
            self._enabled = enabled
            self.set_class(enabled, "-disabled")
        self.refresh()

    def render(self) -> Text:
        return Text.assemble(f"{self._field.capitalize()}: ", "", self._value or "-")

    def on_click(self) -> None:
        if self._enabled:
            self.post_message(self.Activated(self))

    def key_enter(self) -> None:
        self.on_click()


class ProviderSettingsPopup(ModalScreen[None]):
    """Provider / Model / Reasoning for one actor, as three clickable rows.

    ONE popup serves both chips: `target` is the trio it edits (outo's or the
    `subagent_*` trio) and `session` is the pinned session every pick is
    written through, so the edit reaches this conversation even while another
    process moves the global default.

    Provider and Model both reset Reasoning: an effort level belongs to a
    model, so picking either lands on that model's default (the provider
    record's own preference, else lma's auto default). Reasoning is one more
    click away whenever that default is not what the user wants.

    A fourth row, `Caps`, exists only while the trio runs on a CUSTOM
    provider: caps are an endpoint property of a hand-added record, and a
    catalog provider's caps come from lma, so there is nothing to edit. Unlike
    the trio rows it writes the provider RECORD alone — caps are global per
    provider, never part of a session pin.
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss_cancel", "Close"),
    ]

    DEFAULT_CSS = """
    ProviderSettingsPopup {
        align: center middle;
    }
    #provider-settings-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    #provider-settings-title {
        width: 100%;
        content-align: center middle;
        text-style: bold;
        margin-bottom: 1;
    }
    #provider-settings-status {
        width: 100%;
        color: $error;
    }
    #provider-settings-rows {
        height: auto;
    }
    #provider-settings-hint {
        width: 100%;
        content-align: center middle;
        color: $text-muted;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        target: TrioTarget,
        *,
        session: str,
        on_change: Callable[[], None],
    ) -> None:
        super().__init__()
        self._target = target
        self._session = session
        self._on_change = on_change

    # ── composition / painting ──────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        config = _effective_config(self._session)
        provider, model, reasoning = _trio_display(self._target, config)
        reasoning_provider, reasoning_model = _reasoning_target(self._target, config)
        with Vertical(id="provider-settings-dialog"):
            yield Label(self._target.title, id="provider-settings-title")
            # The rows live in their own container so `Caps` can be mounted and
            # removed from the bottom of the list as the provider changes.
            with Vertical(id="provider-settings-rows"):
                yield SettingRow("provider", provider)
                yield SettingRow("model", model)
                yield SettingRow(
                    "reasoning",
                    reasoning,
                    enabled=self._reasoning_enabled(reasoning_provider, reasoning_model),
                )
            yield Label("", id="provider-settings-status")
            yield Label("Click a row to change it · Esc to close", id="provider-settings-hint")

    def on_mount(self) -> None:
        self.query_one("SettingRow", SettingRow).focus()
        self.refresh_rows()

    def on_screen_resume(self) -> None:
        # Coming back from a nested picker — including one the provider-ADD
        # flows pushed on this screen — the rows must show what is configured
        # now, not what it was when the popup opened.
        self.refresh_rows()

    def refresh_rows(self) -> None:
        """Re-read the trio and repaint the rows in place, adding or removing
        the `Caps` row so it tracks the selected provider's kind."""

        config = _effective_config(self._session)
        provider, model, reasoning = _trio_display(self._target, config)
        reasoning_provider, reasoning_model = _reasoning_target(self._target, config)
        enabled = self._reasoning_enabled(reasoning_provider, reasoning_model)
        rows = {row.field: row for row in self.query(SettingRow)}
        rows["provider"].set_value(provider)
        rows["model"].set_value(model)
        rows["reasoning"].set_value(reasoning, enabled=enabled)
        self._sync_caps_row()

    def _sync_caps_row(self) -> None:
        """Keep the `Caps` row present exactly while a custom provider is selected."""

        record = self._caps_record()
        row = next((r for r in self.query(SettingRow) if r.field == CAPS_FIELD), None)
        if record is None:
            if row is not None:
                row.remove()
            return
        if row is None:
            self.query_one("#provider-settings-rows", Vertical).mount(
                SettingRow(CAPS_FIELD, _caps_value(record))
            )
            return
        row.set_value(_caps_value(record))

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)

    # ── picking ─────────────────────────────────────────────────────────────

    def on_setting_row_activated(self, event: SettingRow.Activated) -> None:
        event.stop()
        if event.row.field == "provider":
            self._pick_provider()
        elif event.row.field == "model":
            self._pick_model()
        elif event.row.field == CAPS_FIELD:
            self._pick_caps()
        else:
            self._pick_reasoning()

    def _values(self) -> tuple[str, str, str]:
        return _trio(_effective_config(self._session), self._target.fields)

    def _apply(self, values: tuple[str, str, str]) -> None:
        _persist_trio(self._session, self._target, values)
        self.query_one("#provider-settings-status", Label).update("")
        self.refresh_rows()
        self._on_change()

    def _reasoning_enabled(self, provider: str, model: str) -> bool:
        return bool(model) and _reasoning_choices(model, provider) is not None

    def _report(self, message: str) -> None:
        """Surface why a row could not open its picker, inside the popup."""

        self.query_one("#provider-settings-status", Label).update(message)

    def _pick_provider(self) -> None:
        options = sorted(provider_store.load_all())
        if not options:
            self._report("No providers configured. Add one first.")
            return

        def on_close(result: str | None) -> None:
            if not result:
                return
            if result == SENTINEL_CATALOG_ADD:
                self.app.run_worker(self.app._catalog_add_flow(), exclusive=False)
                return
            if result == SENTINEL_CUSTOM_ADD:
                self.app._open_custom_add_wizard()
                return
            # A new provider invalidates the other two rows: model and
            # reasoning belong to it, not to the one being left behind.
            self._apply((result, *_model_defaults(result)))

        self.app.push_screen(
            SelectionModal(
                "Select provider",
                options,
                current=self._values()[0],
                allow_none=False,
                extra_options=[
                    (SENTINEL_CATALOG_ADD, "+ add from catalog…"),
                    (SENTINEL_CUSTOM_ADD, "+ add custom…"),
                ],
            ),
            on_close,
        )

    def _pick_model(self) -> None:
        provider, model, _ = self._values()
        if not provider:
            self._report("Pick a provider first.")
            return
        self.app.run_worker(self._model_flow(provider, model), exclusive=False)

    async def _model_flow(self, provider: str, current: str) -> None:
        """The model picker, with a free-text fallback for an unknown provider.

        A hand-added endpoint need not exist in the lma catalog at all, so
        "no models" is answered by asking for the id rather than by a dead end.
        """

        options: list[str] = []
        try:
            models = await asyncio.to_thread(catalog_api.list_models, provider)
            # lma returns oldest-first; reverse so the newest models sit on top.
            options = [f"{m.get('id', '?')} — {m.get('name', '')}" for m in reversed(models)]
        except Exception as exc:
            self._report(f"model list unavailable ({exc}); type the model id instead")
        if options:
            current_display = next(
                (opt for opt in options if opt.split(" — ", 1)[0] == current), ""
            )
            picked = await self.app.push_screen_wait(
                SelectionModal(
                    f"Model ({provider}, {len(options)})",
                    options,
                    current=current_display,
                    allow_none=False,
                )
            )
        else:
            picked = await self.app.push_screen_wait(
                TextInputModal(
                    f"Model id — {provider}",
                    initial=current,
                    placeholder="model id",
                    hint="Enter to save · Esc to cancel",
                )
            )
        model = (picked or "").split(" — ", 1)[0].strip()
        if not model:
            return
        resolved, reasoning = _model_defaults(provider, model)
        self._apply((provider, resolved, reasoning))

    def _pick_reasoning(self) -> None:
        config = _effective_config(self._session)
        provider, model = _reasoning_target(self._target, config)
        choices = _reasoning_choices(model, provider)
        if not choices:
            self._report(f"{model or 'the current model'} offers no reasoning levels.")
            return
        extra = (
            [(SENTINEL_SAME_AS_OUTO, "same as outo")] if self._target.inherits else []
        )

        def on_close(result: str | None) -> None:
            if result is None:
                return
            provider_value, model_value, _ = self._values()
            # "same as outo" is stored as an empty value, which is exactly what
            # both stores read as "follow the default".
            reasoning = "" if result == SENTINEL_SAME_AS_OUTO else result
            self._apply((provider_value, model_value, reasoning))

        self.app.push_screen(
            SelectionModal(
                f"Reasoning ({model})",
                choices,
                current=self._values()[2] or "off",
                allow_none=False,
                extra_options=extra,
            ),
            on_close,
        )

    def _caps_record(self) -> provider_store.Provider | None:
        """The record the `Caps` row edits, or None when there is nothing to edit.

        Caps belong to the endpoint, so the record is whichever provider the
        trio runs on — an inheriting subagent trio runs on outo's — and only a
        hand-added one carries them; a catalog provider's caps come from lma.
        """

        provider, _ = _reasoning_target(self._target, _effective_config(self._session))
        record = _record(provider)
        if record is None or record.source != SOURCE_CUSTOM:
            return None
        return record

    def _pick_caps(self) -> None:
        """Ask for the two caps in turn, then write them to the provider record.

        Two prompts, like the custom-add wizard. Empty clears that cap back to
        "follow lma"; a non-numeric answer is refused, stored value untouched.
        """

        record = self._caps_record()
        if record is None:
            self._report("Caps are editable for custom providers only.")
            return

        def ask_context() -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                try:
                    context_window = _parse_optional_int(result)
                except ValueError:
                    self._report("max context window must be a positive number.")
                    return
                ask_tokens(context_window)

            self.app.push_screen(
                TextInputModal(
                    f"Max context window — {record.name}",
                    initial=str(record.max_context_window or ""),
                    placeholder="e.g. 128000 · empty to follow lma",
                    hint="Enter to continue · Esc to cancel",
                ),
                on_close,
            )

        def ask_tokens(context_window: int | None) -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                try:
                    output_tokens = _parse_optional_int(result)
                except ValueError:
                    self._report("max output tokens must be a positive number.")
                    return
                provider_store.upsert(
                    replace(
                        record,
                        max_context_window=context_window,
                        max_output_tokens=output_tokens,
                    )
                )
                # The override itself re-reads the record every call, but the
                # lma FALLBACK it hands back to is cached for the life of the
                # process — drop it or this TUI serves pre-edit caps until exit.
                context_api.clear_model_cache()
                self.query_one("#provider-settings-status", Label).update("")
                self.refresh_rows()

            self.app.push_screen(
                TextInputModal(
                    f"Max output tokens — {record.name}",
                    initial=str(record.max_output_tokens or ""),
                    placeholder="e.g. 16384 · empty to follow lma",
                    hint="Enter to save · Esc to cancel",
                ),
                on_close,
            )

        ask_context()


# ─── Bottom panel ────────────────────────────────────────────────────────────


HELP_TEXT = (
    "[dim]\u2039Tab\u203a cycle chips  \u00b7  "
    "\u2039Enter\u203a open  \u00b7  "
    "\u2039Esc\u203a cancel  \u00b7  "
    "\u2039Ctrl+L\u203a clear  \u00b7  "
    "\u2039Ctrl+C\u203a quit[/dim]"
)


class BottomPanel(Static):
    """The 4-row panel under the input: spinner, two chip rows, help hint."""

    DEFAULT_CSS = """
    BottomPanel {
        height: 4;
        width: 100%;
        align-horizontal: center;
        background: $background;
    }
    /* The panel's background spans the terminal, but its CONTENT stops at
       the chat column so the chips line up with the log and the input. */
    #panel-inner {
        height: 4;
        width: 1fr;
        max-width: $chat-max-width;
    }
    #spinner-row {
        height: 1;
        padding: 0 1;
    }
    /* The chips sit in TWO rows, each anchored around a `1fr` spacer: the
       model+reasoning values on the left, the pickers' other targets on the
       right. Two chips a row is what keeps every value short enough to render
       whole — the earlier single row had to elide them with "…". */
    #chip-row-a, #chip-row-b {
        height: 1;
    }
    #chip-spacer-a, #chip-spacer-b {
        width: 1fr;
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
        with Vertical(id="panel-inner"):
            yield Static("", id="spinner-row")
            with Horizontal(id="chip-row-a"):
                self._chips["outo"] = StatusChip(
                    "outo", "-", id="chip-outo", variant="accent"
                )
                self._chips["style"] = StatusChip("", "-", id="chip-style")
                yield self._chips["outo"]
                yield Static("", id="chip-spacer-a")
                yield self._chips["style"]
            with Horizontal(id="chip-row-b"):
                self._chips["subagent"] = StatusChip(
                    "subagent", "-", id="chip-subagent", variant="muted"
                )
                self._chips["session"] = StatusChip(
                    "session", "-", id="chip-session", variant="muted"
                )
                yield self._chips["subagent"]
                yield Static("", id="chip-spacer-b")
                yield self._chips["session"]
            yield Static(HELP_TEXT, id="help-row", markup=True)

    def set_value(self, kind: str, value: str) -> None:
        """Write a chip's value."""

        chip = self._chips.get(kind)
        if chip is not None:
            chip.set_value(value)

    def set_session(self, name: str) -> None:
        """Publish the live session name to its chip (row B, right)."""

        self.set_value("session", name or "-")

    def get_chip(self, kind: str) -> StatusChip | None:
        return self._chips.get(kind)

    def render_spinner(self, frame: str, text: str) -> None:
        """Paint the bottom status line.

        `frame` empty with `text` set is a NOTICE — plain text, no glyph
        (a frozen spinner next to a finished action reads as "still
        working"). Both empty blanks the row: when nothing needs showing
        the line shows nothing.
        """

        if self._spinner_row is None:
            try:
                self._spinner_row = self.query_one("#spinner-row", Static)
            except Exception:
                return
        if not frame and not text:
            self._spinner_row.update("")
            return
        theme = self.app.current_theme
        parts: list[tuple[str, str]] = []
        if frame:
            parts.append((frame, f"bold {theme.accent}"))
        if text:
            parts.append((f" {text}" if frame else text, theme.foreground))
        self._spinner_row.update(Text.assemble(*parts))


# ─── Chat log rows ───────────────────────────────────────────────────────────


class RowStatic(Static):
    """Chat-log row with uniform reverse-video drag selection.

    Textual's native selection paints with the theme's
    `$screen-selection-background` color — and Markdown rows (RichVisual)
    get no native painting at all. This base class suppresses the native
    path (`text_selection` → None, so `Visual.to_strips` skips its
    painting) and paints the selected span itself with plain
    `Style(reverse=True)` — a true fg/bg inversion that looks identical
    on every row type (prompts, loop output, thinking, alarms, and
    Markdown answers).

    `msg_ref` is the `str(id(message))` of the live coreouto Message this
    row was built from (set from the LoopEvent), or None for rows that
    have no message behind them (user prompts, system notices, answers)
    and for events that are never prunable (alarms, wakeups, provider
    errors). `ChatTUI._remove_event_rows` drops every row whose ref is
    reported removed, which is how a repaired-away turn stops being
    displayed without a separate bookkeeping list.
    """

    msg_ref: str | None = None

    @property
    def text_selection(self) -> Selection | None:
        return None  # selection is painted in render_line instead

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        try:
            selection = self.screen.selections.get(self)
        except Exception:
            selection = None
        if selection is not None:
            span = selection.get_span(y)
            if span is not None:
                # Rebuild as Rich Text and stylize the span in *characters*
                # (Strip.crop is cell-based and misaligns on CJK glyphs);
                # end == -1 stops at the text end, not the padded width.
                text = Text.assemble(*((seg.text, seg.style) for seg in strip))
                text_length = len(text.plain.rstrip())
                start_x, end_x = span
                start_x = max(0, start_x)
                end_x = text_length if end_x == -1 else min(end_x, text_length)
                if start_x < end_x:
                    text.stylize(Style(reverse=True), start_x, end_x)
                    strip = Strip(
                        list(text.render(self.app.console)), strip.cell_length
                    )
        return strip.apply_offsets(0, y)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        strips = self._render_cache.lines
        if not strips:
            return None
        # rstrip: rendered lines can be padded to full width (e.g. Rich
        # Markdown), and the padding must not leak into the clipboard.
        text = "\n".join(strip.text.rstrip() for strip in strips)
        return selection.extract(text), "\n"

    def text_select_all(self) -> None:
        """Select-all is deliberately off: a double-click must not take a whole row.

        `Widget._on_click` calls `text_select_all()` when `event.chain == 2`,
        and it runs BEFORE the click is brokered to `on_click` — so no click
        handler can undo it, the only lever is not to do it. Whole-row
        select-all is wrong twice over on a chat log: it grabs far more than
        the gesture aimed at, and `render_line` then paints every line
        reverse (`SELECT_ALL.get_span` is `(0, -1)`).

        Drag selection is untouched by this: the screen builds it from the
        select state as the button moves, never through this call.
        """


class EventRow(RowStatic):
    """One intermediate loop-output line (tool call, thinking, progress).

    Rendered with a translucent left border and muted gray text instead of
    the old `actor:` prefix — everything that is not the final answer is
    visually grouped and subordinated. The `$primary 40%` tint follows the
    active theme automatically.
    """

    DEFAULT_CSS = """
    EventRow {
        height: auto;
        width: 1fr;
        border-left: heavy $primary 40%;
        padding-left: 1;
        color: $text-muted;
    }
    EventRow.-error {
        color: $error;
        border-left: heavy $error 60%;
    }
    """


COLLAPSED_GLYPH = "▶"
"""Toggle glyph of a collapsed row header."""

EXPANDED_GLYPH = "▼"
"""Toggle glyph of an expanded row header."""

GLYPH_CELLS = max(cell_len(COLLAPSED_GLYPH), cell_len(EXPANDED_GLYPH))
"""Terminal cells the toggle glyph occupies, measured rather than assumed.

Both U+25B6 and U+25BC are East-Asian-Ambiguous, so a hard-coded 1 would be a
width assumption the terminal may not honour; the hover hit math and every
test that reasons about the header read this instead.
"""


class ToggleGlyphHover:
    """Hover feedback for the toggle glyph of a collapsible row header.

    Hover is scoped to the glyph ALONE: a `:hover` CSS rule tints the whole
    widget — header text, body and attached result included — which is not
    what the affordance is about. These rows are `Static`s that paint their
    own selection, so the highlight is rendered into the row's own text
    instead of being handed to CSS, and it only flips for a pointer on the
    header line inside the measured glyph width (which starts after the row's
    border + padding gutter, since mouse offsets are relative to the outer
    region).
    """

    hover_glyph: reactive[bool] = reactive(False)

    def watch_hover_glyph(self, hover_glyph: bool) -> None:
        self._refresh()

    def on_mouse_move(self, event: MouseMove) -> None:
        self.hover_glyph = self._pointer_on_glyph(event.offset)

    def on_leave(self, event: Leave) -> None:
        self.hover_glyph = False

    @property
    def _glyph_column(self) -> int:
        """First cell of the glyph, i.e. the row's border + padding gutter."""
        return self.styles.gutter.left

    def _pointer_on_glyph(self, offset: Offset) -> bool:
        return (
            offset.y == 0
            and self._glyph_column <= offset.x < self._glyph_column + GLYPH_CELLS
        )

    def _glyph_piece(self, glyph: str, base_style: str = "") -> tuple[str, str]:
        """The `▶ ` header prefix, reverse-video while the pointer is on it.

        `base_style` is the glyph's resting style (ToolRow colors it with the
        theme accent; ThinkingRow leaves the glyph to the row's own CSS).
        """
        if not self.hover_glyph:
            return f"{glyph} ", base_style
        tokens = ["bold", "reverse", str(self.app.current_theme.accent)]
        if base_style:
            tokens.insert(0, base_style)
        return f"{glyph} ", " ".join(tokens)


LEFT_MOUSE_BUTTON = 1
"""The button that drags a text selection (`MouseMove.button` while held)."""


class ClickToToggle:
    """A plain click anywhere on a collapsible row toggles it; a drag never does.

    The whole row is the button — glyph, header text, expanded body, attached
    result — because the click is now told apart from a text selection by the
    GESTURE, not by where it landed: `MouseDown` records where the press
    started, a held-button `MouseMove` of ≥1 cell marks the gesture a drag,
    and `on_click` toggles only when that drag took no text. So dragging a
    command out of a collapsed row still copies it, and clicking the same cell
    expands it.

    The deciding signal is the SELECTION (the owner's rule: a drag that got at
    least one character is a selection; a gesture that got none is a plain
    click). Textual builds that selection on the screen while the button moves
    and keeps it when the release lands back on this row, so it is already in
    `screen.selections` by the time `on_click` runs; a plain click clears it on
    MouseUp, so an absent selection means nothing was dragged over.

    A click with `chain > 1` — the second click of a double/triple-click — is
    consumed, so one double-click gesture toggles at most once (no
    open-then-close flicker). Its row select-all is removed at the source in
    `RowStatic.text_select_all`; a triple-click keeps Textual's own
    container-level select-all (the whole visible log), which is what it does
    everywhere else and never toggles a row.

    Both this mixin and `ToggleGlyphHover` define `on_mouse_move` on purpose:
    Textual dispatches a naming-convention handler from EVERY class in the MRO
    (they are not deduplicated, unlike decorated handlers), so the press/drag
    tracker and the glyph hover both run.
    """

    _expanded: bool
    _press_offset: Offset | None = None
    _dragged: bool = False

    def on_mouse_down(self, event: MouseDown) -> None:
        self._press_offset = event.offset
        self._dragged = False

    def on_mouse_move(self, event: MouseMove) -> None:
        if (
            event.button == LEFT_MOUSE_BUTTON
            and self._press_offset is not None
            and event.offset != self._press_offset
        ):
            self._dragged = True

    def on_click(self, event: Click) -> None:
        if event.chain > 1 or self._drag_took_text():
            return
        self._toggle()

    def key_enter(self) -> None:
        self._toggle()

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._refresh()

    def _drag_took_text(self) -> bool:
        """Whether the press behind this click dragged at least one character out."""
        if not self._dragged:
            return False
        selection = self.screen.selections.get(self)
        if selection is None:
            return False
        selected = self.get_selection(selection)
        return bool(selected and selected[0])


class ThinkingRow(ToggleGlyphHover, ClickToToggle, EventRow):
    """Collapsible reasoning row: `▶ thinking` collapsed (default), a plain
    click anywhere on the row or `Enter` expands the full reasoning text, a
    plain click again re-collapses it.

    A drag is never a toggle: `ClickToToggle` reads the gesture, so the
    reasoning text can be selected and copied straight out of the collapsed
    row without the row opening under the pointer.

    Inherits EventRow's translucent left border + muted styling. While the
    model is still streaming its reasoning the collapsed header carries a
    `●` marker, so a live row is distinguishable from a settled one.
    """

    can_focus = True

    DEFAULT_CSS = """
    ThinkingRow:focus {
        background: $primary 22%;
    }
    """

    def __init__(self, thinking: str = "") -> None:
        super().__init__(Text(f"{COLLAPSED_GLYPH} thinking"))
        self._thinking = thinking
        self._expanded = False
        self._streaming = False

    def begin_stream(self) -> None:
        self._streaming = True
        self._refresh()

    def append(self, delta: str) -> None:
        if not delta:
            return
        self._thinking += delta
        self._refresh()

    def finalize(self, thinking: str) -> None:
        """Replace the streamed text with the message's complete reasoning."""

        self._streaming = False
        self._thinking = thinking
        self._refresh()

    def _refresh(self) -> None:
        marker = " ●" if self._streaming else ""
        glyph = EXPANDED_GLYPH if self._expanded else COLLAPSED_GLYPH
        pieces: list[tuple[str, str]] = [
            self._glyph_piece(glyph),
            (f"thinking{marker}", ""),
        ]
        if self._expanded:
            pieces.append(("\n", ""))
            pieces.append((self._thinking, ""))
        self.update(Text.assemble(*pieces))


class StreamRow(EventRow):
    """Live assistant text: one row, updated in place as tokens arrive.

    Mounted on the first text delta of a message and appended to (never
    replaced) for each subsequent one, so a streamed message costs one
    widget instead of one per token. The final answer replaces this row
    with the canonical Markdown `AnswerRow` (see `ChatTUI._finalize_stream`).
    """

    def __init__(self, text: str) -> None:
        super().__init__(Text(text))
        self._text = text

    def append(self, delta: str) -> None:
        if not delta:
            return
        self._text += delta
        self.update(Text(self._text))


class ToolRow(ToggleGlyphHover, ClickToToggle, EventRow):
    """Collapsible tool-call row: `▶ <header>` collapsed (default), a plain
    click anywhere on the row or `Enter` expands the full command + result.
    The result is attached later via `set_result`; until then the box is purely
    the call preview.

    A drag is never a toggle: `ClickToToggle` reads the gesture, so the
    command and its output can be dragged out and copied without the row
    opening under the pointer, and a double-click selects nothing.

    Inherits EventRow's translucent left border + muted styling. When the
    result is an error (prefixed `"error: "`), the row gains the `-error`
    class (muted → `$error` text and `$error` border-left) and the header
    shows a subtle `✗` marker.

    Detail (multi-line command) and result are each capped at ~4000 chars
    on display as a safety net even though the core layer already truncates.
    `unbounded_result=True` opts out — a result the user cannot afford to
    read cut at the cap must read in full.
    """

    can_focus = True

    DEFAULT_CSS = """
    ToolRow:focus {
        background: $primary 22%;
    }
    """

    # Display cap (chars). Core already truncates to ~4000; we re-cap here
    # so old sessions that somehow escaped that pass still don't blow up the
    # layout.
    _MAX_DETAIL_CHARS = 4000
    _MAX_RESULT_CHARS = 4000

    def __init__(
        self,
        header: str,
        *,
        detail: str | None = None,
        tool_name: str | None = None,
        unbounded_result: bool = False,
    ) -> None:
        super().__init__(Text(f"{COLLAPSED_GLYPH} {header}"))
        self._header = header
        self._detail = detail
        self._result: str | None = None
        self._is_error = False
        self._expanded = False
        self._tool_name = tool_name
        self._unbounded_result = unbounded_result
        self._image_widget: Widget | None = None
        self._image_failed = False
        self._image_visible = False

    @property
    def tool_name(self) -> str | None:
        return self._tool_name

    @property
    def has_result(self) -> bool:
        return self._result is not None

    def set_result(self, text: str, *, is_error: bool = False) -> None:
        """Attach a result to this row. Idempotent — second call is a no-op.

        Used for the live path (a paired Bash/Image/... result) and for the
        single result attached at mount time.
        """
        if self._result is not None:
            return
        self._result = text
        self._is_error = is_error
        if is_error:
            self.add_class("-error")
        self._refresh()

    def _refresh(self) -> None:
        theme = self.app.current_theme
        glyph_style = f"bold {theme.accent}"
        if not self._expanded:
            header = Text.assemble(
                self._glyph_piece(COLLAPSED_GLYPH, glyph_style),
                (self._header, ""),
                (" ✗", f"bold {theme.error}") if self._is_error else ("", ""),
            )
            self._image_visible = False
            self.update(header)
            self._sync_image_display()
            return
        pieces: list[tuple[str, str]] = [
            self._glyph_piece(EXPANDED_GLYPH, glyph_style),
            (self._header, ""),
        ]
        if self._is_error:
            pieces.append((" ✗", f"bold {theme.error}"))
        if self._detail:
            detail = self._detail
            if len(detail) > self._MAX_DETAIL_CHARS:
                detail = detail[: self._MAX_DETAIL_CHARS] + "\n…(truncated)"
            pieces.append(("\n", ""))
            pieces.append((detail, ""))
        if self._result is not None:
            pieces.append(("\n── output ──\n", "dim"))
            pieces.append((self._result_text(), ""))
            body = Text.assemble(*pieces)
            art = self._screen_art()
            if art is not None:
                body.append("\n")
                body.append_text(art)
            self._image_visible = art is not None and self._image_widget is not None
            self.update(body)
            self._sync_image_display()
            return
        self._image_visible = False
        self.update(Text.assemble(*pieces))
        self._sync_image_display()

    def _result_text(self) -> str:
        """The result with any SGR stripped.

        A Computer result carries an inline preview of the art it also saved to
        a `.art` file; rendering those raw escape bytes as plain text is
        unreadable noise, and the file render below it is the one worth
        looking at.
        """

        result = self._result or ""
        if not self._unbounded_result and len(result) > self._MAX_RESULT_CHARS:
            result = result[: self._MAX_RESULT_CHARS] + "\n…(truncated)"
        return ascii_art.strip_sgr(result)

    def _screen_art(self) -> Text | None:
        """Truecolour render of the `.art` file a Computer result names.

        Any Computer result, whatever action produced it: `screen` ends with
        the saved path and a `screenshot` summary ends with the same line, so
        both render — the gate is the path, not the action, because a
        screenshot the model could not receive as pixels is precisely the one
        the user most wants to see. Read lazily — only while the row is
        expanded — so a session holding hundreds of screenshots never touches
        the filesystem on load.
        """

        if self._tool_name != "Computer":
            return None
        # A graphics-capable terminal renders the raw PNG inline instead of
        # the unicode art; the widget carries the pixels, this Text keeps the
        # caption. Any failure (no png sibling, import error, decode error)
        # falls through to the `.art` below — the current behavior. The gate
        # is the PNG line: a `screen` action writes only the `.art`, so it
        # always renders as text art, image-capable terminal or not.
        #
        # The PNG is checked BEFORE the `.art`: a capture whose art file
        # could not be written (a full disk, a read-only home) still has its
        # pixels, and those are exactly what the graphics lane shows.
        png = png_path_in_result(self._result or "")
        if png is not None and self._mount_screen_image():
            return Text(f"── screen image ({png}) ──", style="dim")
        path = art_path_in_result(self._result or "")
        if path is None:
            return None
        try:
            art = ascii_art.parse_art(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        lines = art.body.split("\n")
        hidden = len(lines) - _ART_MAX_ROWS
        block = Text()
        block.append(f"── screen art ({path}) ──\n", style="dim")
        block.append_text(Text.from_ansi("\n".join(lines[:_ART_MAX_ROWS])))
        if hidden > 0:
            block.append(
                f"\n… {hidden} more rows (miniouto computer show {path})", style="dim"
            )
        return block

    def _mount_screen_image(self) -> bool:
        """Mount the screenshot's raw PNG as real graphics; False = caller falls back.

        The image is mounted as a SIBLING row in the log, right after this
        row — NOT as a child of it. Two reasons, both found by rendering the
        real app:

        * A child widget is laid out at the TOP of the parent's content
          area, which is exactly where the parent's own text is — the
          caption and the image would be drawn ON TOP OF EACH OTHER.
        * A `Static` with ANY child becomes a container, and a container
          whose only child is `display: none` computes its auto height as
          ZERO — so hiding the image collapsed the whole row out of
          existence instead of just hiding the picture.

        As a sibling row, the image has its own line, and hiding it leaves
        the text row's height alone.

        Gate order, cheapest first: the row must be a Computer row whose
        result names a PNG sibling, the terminal must actually do TGP/Sixel
        (the DETECTED mode — the render path never probes), and the image
        widget must build (PIL decode, size maths). ANY failure is a no-op
        False: the `.art` text render is the unchanged fallback.
        """

        if self._tool_name != "Computer":
            return False
        try:
            attached = self.is_attached
        except Exception:
            # No app context (a unit test, a half-torn-down app): mounting is
            # impossible, which is exactly the fallback signal.
            return False
        if not attached:
            return False
        png = png_path_in_result(self._result or "")
        if png is None:
            return False
        if self._image_widget is not None:
            return True
        if self._image_failed:
            return False
        mode = graphics_mode()
        if mode == GRAPHICS_MODE_TEXT:
            # No graphics terminal: the `.art` text lane, unchanged.
            return False
        widget_class = _IMAGE_WIDGETS.get(mode)
        if widget_class is None:
            # Detection never ran, or it found no graphics: the text lane.
            return False
        try:
            # The widget class follows the DETECTED mode, so the renderer and
            # the probe can never disagree about the protocol.
            widget = widget_class(Path(png))
            # The image fills the LOG's content width (`100%`), never a fixed
            # cell count: a fixed width overflows a narrow terminal and paints
            # over the log beside it. The height follows the capture's aspect
            # at that width.
            widget.styles.width = "100%"
            widget.styles.height = "auto"
        except Exception:
            self._image_failed = True
            return False
        try:
            log = self._image_log()
            if log is None:
                return False
            log.mount(widget, after=self)
        except Exception:
            self._image_failed = True
            return False
        self._image_widget = widget
        return True

    def _image_log(self) -> Any | None:
        """The scrollable log the image row is mounted into, or None."""

        try:
            parent = self.parent
        except Exception:
            return None
        return parent if parent is not None else None

    def _sync_image_display(self) -> None:
        """Show the image row only while this row is expanded in image mode.

        The widget stays mounted (re-mounting would re-send the pixels to
        the terminal); `display: none` removes it from layout so a collapsed
        or art-fallback row carries no dead space.
        """

        if self._image_widget is not None:
            self._image_widget.display = self._image_visible


class AnswerRow(RowStatic):
    """Final-answer row (Markdown) — the TOP of the text hierarchy.

    `color` paints the Rich `Markdown` renderable whole: it is one
    `RichVisual` with no DOM children, so this is the single rule that puts
    the agent's final answer on `$text-answer` (12.33:1) against a 9.69:1
    body and a 5.11:1 dim layer. Selection behavior comes from RowStatic —
    needed because Markdown is wrapped in `RichVisual`, whose
    `render_strips` ignores `RenderOptions.selection` entirely (Text-backed
    widgets are promoted to `Content`, which honors it)."""

    DEFAULT_CSS = """
    AnswerRow {
        height: auto;
        width: 1fr;
        color: $text-answer;
    }
    """


class _RowWindow:
    """Windowed row mounting for one scrollable log.

    All rows ever added live in `rows` (widget objects, so collapse /
    result / spinner state survives), but only the contiguous suffix
    starting at `first` is mounted — Textual lays out every mounted child
    on each reflow, so an unbounded log gets slower the longer the
    session runs. Older rows mount in chunks as the user scrolls toward
    the top (`maybe_load_older`), and rows far above the viewport are
    evicted again once the window overflows (`evict`).

    Scroll compensation: adding rows ABOVE the viewport shifts the viewed
    content down, removing them shifts it up — both are countered by
    adjusting `scroll_y` by the exact pixel height of the rows involved,
    measured after the reflow that lays them out/tears them down.
    """

    def __init__(self) -> None:
        self.rows: list[Static] = []
        self.first = 0
        self._loading_chunk = False

    def reset(self) -> None:
        self.rows.clear()
        self.first = 0

    def remove(self, widget: Static) -> None:
        """Drop a row from the window and the DOM, keeping `first` aligned.

        Two callers: a live stream row replaced by the canonical answer
        row (re-mounting the answer at the tail would otherwise leave
        the streamed prefix above it as a duplicate), and a row whose
        message was removed from history by a repair (`_remove_event_rows`).

        `first` is clamped, not just decremented: removing the last row of
        a fully-evicted window would otherwise leave it pointing past the
        end of `rows`, and the next top-edge chunk load would index off it.
        """

        for idx, row in enumerate(self.rows):
            if row is widget:
                del self.rows[idx]
                if idx < self.first:
                    self.first -= 1
                break
        self.first = min(self.first, len(self.rows))
        if widget.is_attached:
            widget.remove()

    def append(self, log: VerticalScroll, widget: Static, *, defer_mount: bool) -> None:
        self.rows.append(widget)
        if defer_mount:
            return
        log.mount(widget)
        # No scroll_end: the log's anchor() follows the tail while the
        # user is at the bottom and stays put when they've scrolled up.
        self.evict(log)

    def mount_tail(self, log: VerticalScroll) -> None:
        """After a bulk fill, mount only the trailing window."""

        self.first = max(0, len(self.rows) - _WINDOW_INITIAL_ROWS)
        for row in self.rows[self.first:]:
            if not row.is_attached:
                log.mount(row)
        log.scroll_end(animate=False)

    def maybe_load_older(self, log: VerticalScroll, scroll_y: float) -> None:
        if scroll_y <= _TOP_LOAD_LINES and self.first > 0:
            self._load_older(log)

    def _load_older(self, log: VerticalScroll) -> None:
        if self._loading_chunk or self.first <= 0:
            return
        self._loading_chunk = True
        start = max(0, self.first - _WINDOW_CHUNK_ROWS)
        new_rows = self.rows[start:self.first]
        self.first = start
        # The new chunk goes in front of the first currently mounted row.
        # A pruned window can have nothing mounted (`first == len(rows)`),
        # and there is then no anchor to insert before.
        anchor_idx = start + len(new_rows)
        anchor = self.rows[anchor_idx] if anchor_idx < len(self.rows) else None
        for row in new_rows:
            if row.is_attached:
                continue
            if anchor is None:
                log.mount(row)
            else:
                log.mount(row, before=anchor)
        log.call_after_refresh(self._finish_older, log, new_rows)

    def _finish_older(self, log: VerticalScroll, new_rows: list[Static]) -> None:
        self._loading_chunk = False
        added = sum(row.outer_size.height for row in new_rows if row.is_attached)
        if added:
            log.scroll_to(y=log.scroll_y + added, animate=False)

    def evict(self, log: VerticalScroll) -> None:
        excess = (len(self.rows) - self.first) - _WINDOW_MAX_ROWS
        if excess <= 0:
            return
        viewport_top = log.scroll_y
        removed_height = 0.0
        victims: list[Static] = []
        for row in self.rows[self.first:]:
            if len(victims) >= excess:
                break
            height = row.outer_size.height
            if removed_height + height > viewport_top - _EVICT_MARGIN_LINES:
                break
            removed_height += height
            victims.append(row)
        if not victims:
            return
        self.first += len(victims)
        for row in victims:
            row.remove()
        # At the bottom the anchor re-pins to the tail on the next reflow,
        # so only an up-scrolled view needs the shift compensated.
        if removed_height and log.scroll_y < log.max_scroll_y - 1:
            log.call_after_refresh(self._fix_scroll_after_evict, log, removed_height)

    def _fix_scroll_after_evict(self, log: VerticalScroll, removed_height: float) -> None:
        log.scroll_to(y=max(0.0, log.scroll_y - removed_height), animate=False)


class ChatScroll(VerticalScroll):
    """Scrollable log with anchored follow, top-edge reporting, no animation.

    Textual's built-in `anchor()` pins the scroll position to the bottom
    on every reflow until the user scrolls away (and re-engages when they
    scroll back to the bottom) — this replaces the old unconditional
    `scroll_end` on every row mount, so reading history while a turn is
    streaming no longer yanks the view back down. `watch_scroll_y` feeds
    the scroll position to the owning screen (a modal detail screen) or,
    for the main log, the app, so it can mount older row chunks as the
    user approaches the top of the loaded window.

    Every scroll action is overridden to `animate=False`: Textual defaults
    to `animate=True`, and paging through a log that is still streaming
    should land instantly rather than slide.
    """

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        callback = None
        try:
            callback = getattr(self.screen, "_on_log_scroll", None)
            if callback is None:
                callback = getattr(self.app, "_on_log_scroll", None)
        except Exception:
            callback = None
        if callback is not None:
            callback(new_value)

    def action_page_up(self) -> None:
        self.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        self.scroll_page_down(animate=False)

    def action_scroll_home(self) -> None:
        self.scroll_home(animate=False, x_axis=self.scroll_x == 0)

    def action_scroll_end(self) -> None:
        self.scroll_end(animate=False, x_axis=self.scroll_x == self.max_scroll_x)


# Loop event kinds that close the current streamed message: the live row
# is retired for them, so any buffered tail must flush first (see
# `TUIEventSink.emit_loop_event`).
_STREAM_BOUNDARY_KINDS = frozenset({"thinking", "response", "tool", "context"})


class _StreamCoalescer:
    """Fold per-token deltas into one UI update per flush window.

    coreouto fires one ON_STREAM_* hook per token, often 100+/s, and every
    sink call hops to the UI thread — hopping and repainting per token
    would starve the Textual event loop. The sink appends into this
    buffer (only the single worker thread calls it, so no lock is needed)
    and drains it on a time gate; the turn's last window is drained
    explicitly when working ends, so no tail is ever lost.
    """

    def __init__(
        self,
        flush_interval: float = _STREAM_FLUSH_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._flush_interval = flush_interval
        self._clock = clock
        self._text: list[str] = []
        self._thinking: list[str] = []
        self._last_flush = 0.0

    def push(self, kind: str, text: str) -> bool:
        """Buffer one delta; return True when the caller should flush now."""

        target = self._thinking if kind == STREAM_THINKING else self._text
        target.append(text)
        now = self._clock()
        if now - self._last_flush < self._flush_interval:
            return False
        self._last_flush = now
        return True

    def drain(self) -> tuple[str, str]:
        text, thinking = "".join(self._text), "".join(self._thinking)
        self._text.clear()
        self._thinking.clear()
        return text, thinking


class TUIEventSink:
    """Sink that posts chat events to a running `ChatTUI` app.

    The agent loop runs on a worker thread (via `asyncio.to_thread`), so
    every callback must hop to the main thread with `call_from_thread`
    before touching widgets. The sink owns NO animation state: the spinner
    ticker lives on the app (`ChatTUI._sync_ticker`), because a sink is
    created per dispatch and dies at `end_working`. `begin_working` /
    `end_working` / `update_activity` are therefore thin drivers of app
    state.
    """

    def __init__(self, app: ChatTUI) -> None:
        self._app = app
        self._stream = _StreamCoalescer()

    def begin_working(self) -> None:
        self._app.call_from_thread(self._app._set_chat_working, True)

    def update_activity(self, text: str) -> None:
        # Plain attribute on the app: the ticker repaints it on the next
        # frame, so there is nothing to hop to the UI thread for.
        self._app._set_activity(text)

    def end_working(self) -> None:
        self._app.call_from_thread(self._end_working_on_ui)

    def _end_working_on_ui(self) -> None:
        self._app._set_chat_working(False)
        # Flush the last (< interval) window on the UI thread we are already
        # on — call_from_thread would raise from this thread.
        self._drain_stream()

    def emit_stream_delta(self, kind: str, text: str) -> None:
        if not text:
            return
        if self._stream.push(kind, text):
            self._flush_stream()

    def _flush_stream(self) -> None:
        text, thinking = self._stream.drain()
        if not text and not thinking:
            return
        self._app.call_from_thread(self._post_stream, text, thinking)

    def _drain_stream(self) -> None:
        text, thinking = self._stream.drain()
        if text or thinking:
            self._post_stream(text, thinking)

    def _post_stream(self, text: str, thinking: str) -> None:
        if thinking:
            self._app._apply_stream_thinking(thinking)
        if text:
            self._app._apply_stream_text(text)

    def emit_loop_event(self, event: LoopEvent) -> None:
        # A message-boundary event (thinking / response / tool / context)
        # retires the current live stream row. Flush the buffered tail first
        # so it renders on the row it belongs to — otherwise the coalescer's
        # last (< interval) window survives the boundary and lands on the
        # NEXT message's live row, which reads as the previous message's
        # ending reappearing at the top of the new one.
        if event.kind in _STREAM_BOUNDARY_KINDS:
            self._flush_stream()
        self._app.call_from_thread(self._post_loop_event, event)

    def _post_loop_event(self, event: LoopEvent) -> None:
        # Every event renders in the main chat: the subagent-scoped kinds
        # (`subagent_start` / `subagent_end`) only survive in sessions
        # recorded before that UI was torn down, and `_add_event_row` falls
        # through to a plain row for them.
        self._app._add_event_row(event)

    def emit_final_answer(self, content: str, session_name: str) -> None:
        self._app.call_from_thread(self._post_final_answer, content, session_name)

    def _post_final_answer(self, content: str, session_name: str) -> None:
        self._app._finalize_stream()
        if content:
            self._app._mount_row(AnswerRow(Markdown(content)))
        else:
            self._app._mount_row(Static(Text("(empty response)", style="dim")))
        self._app._mount_row(Static(""))

    def remove_events(self, msg_refs: list[str]) -> None:
        if not msg_refs:
            return
        self._app.call_from_thread(self._app._remove_event_rows, msg_refs)


# ─── Chat input ──────────────────────────────────────────────────────────────


class ChatInput(TextArea):
    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "submit", "Send", show=False, priority=True),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(text="", soft_wrap=True, id=id)

    def on_mount(self) -> None:
        super().on_mount()
        self.show_line_numbers = False

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.action_submit()
            return
        await super()._on_key(event)

    def watch_virtual_size(self, virtual_size) -> None:
        content_lines = virtual_size.height
        target = max(5, min(content_lines + 2, 12))
        self.styles.height = target

    def action_submit(self) -> None:
        if self.text.strip():
            self.post_message(self.Submitted(self.text))


# ─── Theme ───────────────────────────────────────────────────────────────────

THEME_NAME = "miniouto"
"""Name of the theme the TUI boots on (`settings.theme` may name another one)."""

MINIOUTO_THEME = Theme(
    name=THEME_NAME,
    dark=True,
    # A midnight blue-black family, comfort-tuned, with a THREE-LAYER text
    # hierarchy: `$text-answer` (12.33:1) > `$foreground` (9.69:1) >
    # `$text-muted` (5.11:1) > `$text-disabled` (2.16:1), all on `$background`.
    # The floor is lifted off near-black (#05070a → #0a0e14, ~2.1x the relative
    # luminance) and the ceiling comes DOWN from the old 13.28:1 glare — but the
    # comfort pass had flattened the WHOLE transcript into one gray, so the
    # answer lost its emphasis. The ladder is the fix: the agent's final answer
    # is the only near-white thing on screen and reads as the focal layer.
    # `#a9bac5` measures 9.69 / 9.26 / 8.66 on `$background` / `$surface` /
    # `$panel` — comfortably readable everywhere, and 3.4x the CHROMA of the
    # `#a8b0b1` it replaces (S≈0.19 vs S≈0.06), which is what "too gray" was:
    # it now carries the theme's blue family without going clinical.
    # Still strictly darker than Textual's stock `#121212` / `#1e1e1e`, which
    # the theme test enforces; the surfaces step up in even increments (channel
    # sums 44 → 62 → 84 → 114).
    background="#0a0e14",
    surface="#0f141b",
    panel="#151b24",
    # Accent. `primary` drives focus borders, the input's left rule, block
    # cursors and the scrollbar; `accent` is the same color, because the logo
    # and the tool-row toggle glyph render through `$accent` / the Rich style
    # string and the owner's single accent color is this one. OWNER-PINNED, so
    # it is NOT desaturated with the rest: the lifted background alone drops its
    # measured contrast from 9.87:1 to 9.47 / 9.05 / 8.47 / 7.56 on background /
    # surface / panel / boost, so it reads calmer for free.
    primary="#2fc2ff",
    accent="#2fc2ff",
    # The loud colors, one notch down: same hue families, trimmed chroma and
    # luminance so they stop shouting on a dark ground (secondary 6.41→6.05,
    # success 11.41→8.29, warning 11.01→7.54, error 7.34→5.76 on `$background`,
    # as each color RESOLVES — Textual quantizes the declared hex by a hair).
    secondary="#8f86d8",
    success="#52bd8a",
    warning="#c99a4e",
    # One step above the suggested #d16b7a, which resolved to 4.46:1 on
    # `$boost` — short of the 4.5 floor every other color clears. Same family.
    error="#d16f7e",
    foreground="#a9bac5",
    # `boost` is passed through `variables`, not through the `boost=` field:
    # Textual's ColorSystem DROPS the boost field whenever `panel` is set
    # explicitly (`_generate` only reads it on the panel-is-None branch), which
    # would leave `$boost` transparent and take the status chips, the selection
    # lists and Textual's own TextArea/Markdown backgrounds with it. The two
    # text ramp entries are here for the same reason: the derived defaults
    # (`auto 60%` / `auto 38%`) are not this palette's muted/disabled grays.
    # `text-muted` is tuned into the 4.5-5.2:1 comfort band on `$background`
    # while still clearing 4.5 on `$panel`, which the hint text also sits on:
    # #7b8490 measures 5.11 / 4.88 / 4.57 on background / surface / panel.
    # `text-disabled` is deliberately left low-contrast (2.16:1), as WCAG
    # exempts disabled controls.
    # `text-answer` is the TOP of the text ladder and rides in `variables` for
    # the same reason: `AnswerRow` paints with it, and a `Theme` field would
    # have to be some *other* color's name. #c5d0d8 measures 12.33 / 11.78 /
    # 11.03 / 9.85 on background / surface / panel / boost — the only
    # near-white in the palette, so the agent's final answer is unambiguously
    # the brightest layer against a 9.69:1 body and a 5.11:1 dim layer.
    variables={
        "boost": "#1d2530",
        "text-muted": "#7b8490",
        "text-disabled": "#434a52",
        "text-answer": "#c5d0d8",
    },
)
"""The owner's theme: dark surfaces, `#2fc2ff` accent.

`$text` is deliberately NOT overridden here. Textual derives it as `auto 87%`,
which resolves per-widget against its actual background: on our dark surfaces
it does not paint the transcript at all (the declared `foreground` above does,
which is what carries the comfort band), while on the picker's selected row —
`$screen-selection-background`, a solid `#2fc2ff` bar — it is what supplies the
near-black ink that keeps that row at 8.80:1. Pinning `$text` to the solid
foreground was measured and drops that row to 1.08:1, so the auto ramp stays.
"""


# ─── Main app ────────────────────────────────────────────────────────────────


def _persist_handoff_marker(session: str, handoff: summary.Handoff) -> None:
    """Close the OLD conversation with the hand-off marker, on disk.

    A trailing turn holding one `handoff` event, so reopening the old session
    replays the marker through `_add_event_row` instead of showing a
    transcript that just stops mid-work. It is appended as its own turn rather
    than merged into the last one, because replay renders a turn's events
    BEFORE its answer — merged, the marker would sit above the final message.
    """

    event = LoopEvent(actor="outo", kind="handoff", text=handoff_marker(handoff))
    data = session_store.load(session)
    data.turns.append(
        session_store.TurnRecord(
            user="", events=[event.to_dict()], status=session_store.STATUS_DONE
        )
    )
    session_store.save(session, data)


class ChatTUI(App):
    CSS = """
    Screen { layout: vertical; }
    Screen {
        layers: base jump;
    }
    #main-area {
        width: 1fr;
        height: 1fr;
        align-horizontal: center;
        background: $background;
    }
    #chat {
        height: 1fr;
        width: 1fr;
        max-width: $chat-max-width;
        background: $background;
        overflow-y: auto;
        scrollbar-size: 0 0;
    }
    #chat:focus {
        background: $background;
        background-tint: transparent;
    }
    #input {
        height: 5;
        width: 1fr;
        max-width: $chat-max-width;
        padding: 1 1;
        margin: 0 2;
        background: $surface;
        border: none;
        border-left: thick $primary;
        scrollbar-size: 1 1;
    }
    #input:focus {
        border: none;
        border-left: thick $primary;
    }
    RowStatic.-alarm {
        height: auto;
        width: 1fr;
        border-left: heavy $warning 60%;
        padding-left: 1;
        color: $warning;
    }
    #jump-bottom {
        layer: jump;
        dock: bottom;
        display: none;
        width: auto;
        height: 1;
        margin: 0 0 1 0;
        padding: 0 2;
        border: none;
        background: $panel;
        color: $text-muted;
        text-style: none;
    }
    #jump-bottom:hover {
        background: $primary 12%;
        color: $text;
    }
    #jump-bottom:focus {
        background: $primary 22%;
        color: $text;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+shift+c", "copy_text", "Copy"),
        Binding("ctrl+l", "clear_log", "Clear"),
        Binding("escape", "escape_pressed", "Stop", show=False),
        # Priority because the input is a TextArea that owns these keys for
        # cursor movement: without it PgUp scrolls the caret, not the log.
        Binding("pageup", "page_up", "", show=False, priority=True),
        Binding("pagedown", "page_down", "", show=False, priority=True),
        Binding("home", "scroll_home", "", show=False, priority=True),
        Binding("end", "scroll_end", "", show=False, priority=True),
    ]

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return {"chat-max-width": str(CHAT_MAX_WIDTH)}

    def __init__(self) -> None:
        super().__init__()
        self.register_theme(MINIOUTO_THEME)
        self._log: VerticalScroll | None = None
        self._input: Input | None = None
        self._panel: BottomPanel | None = None
        self._busy = False
        self._logo_shown = False
        self._chat_started = False
        self._session_assigned = False
        # The session name THIS conversation routes on: what `ChatOptions.session`
        # gets and which inbox the idle poll drains. `_pin_session` sets it at
        # the three user-visible session events (mint / picker / new session),
        # which also publish it to `settings.session` — the cross-surface
        # "active session" convention. It is never read back for routing.
        self._session_name: str | None = None
        # A distinct, private queue until this instance chooses its real
        # session. Two freshly opened TUIs must never both drain `default`.
        self._unassigned_inbox = f"tui-pending-{uuid.uuid4().hex[:12]}"
        # Inbox message id -> the row rendered when it was inserted mid-turn.
        # Its keys ARE the optimistic set: a drained event whose id is here is
        # already on screen, so the real row is suppressed, and an idle drain
        # retires the row before rendering the turn prompt in its place.
        self._optimistic_rows: dict[str, Static] = {}
        self._pending_tool_rows: list[ToolRow] = []
        self._window = _RowWindow()
        self._bulk_loading = False
        self._stream_text_row: StreamRow | None = None
        self._stream_thinking_row: ThinkingRow | None = None
        self._cancel_event: threading.Event | None = None
        self._last_escape = 0.0
        self._esc_hint: str | None = None
        self._esc_hint_until = 0.0
        self._spin_timer: Timer | None = None
        self._spin_frame_idx = 0
        self._chat_working = False
        self._activity = _SPINNER_DEFAULT_TEXT
        self._wizard_status: str | None = None
        self._wizard_working = False
        # Last: assigning the reactive runs `watch_theme`, which reads
        # `_logo_shown` and republishes the name to settings.
        self.theme = self._boot_theme()

    def _boot_theme(self) -> str:
        """The theme to start on: `settings.theme`, else `THEME_NAME`.

        A stored name that is not registered (a typo, or a theme from a build
        that never registered it) falls back instead of raising: `App.theme`'s
        validator rejects an unknown name outright, which would make the TUI
        unbootable over a stale key in `settings.toml`.
        """

        configured = settings_store.load().theme.strip()
        if not configured:
            return THEME_NAME
        return configured if self.get_theme(configured) else THEME_NAME

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="main-area"):
            self._log = ChatScroll(id="chat")
            yield self._log
            self._input = ChatInput(id="input")
            yield self._input
        self._panel = BottomPanel()
        yield self._panel
        yield Footer()
        yield Button("- latest -", id="jump-bottom")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "jump-bottom":
            # `anchor()` is the follow idiom: it re-engages the bottom pin so
            # new rows keep arriving, where a bare scroll_end would snap
            # back once and then detach again.
            if self._log is not None:
                self._log.anchor()
            self._sync_jump_button()
            # Hiding the button blurs it, which would leave the app with no
            # focused widget at all — typing would die until the user
            # clicked back into the input.
            if self._input is not None:
                self._input.focus()

    def _sync_jump_button(self) -> None:
        """Show the jump button exactly when the log is away from the tail."""

        try:
            button = self.query_one("#jump-bottom", Button)
        except Exception:
            return
        button.display = not at_bottom(self._log)

    def on_mount(self) -> None:
        cwd = Path.cwd()
        home = Path.home()
        try:
            cwd_display = "~/" + str(cwd.relative_to(home))
        except ValueError:
            cwd_display = str(cwd)
        self.title = cwd_display
        self.sub_title = f"miniouto v{__version__}"
        self._refresh_chips()
        self._log = self.query_one("#chat", VerticalScroll)
        self._log.anchor()
        saved = settings_store.load()
        if saved.theme:
            self.theme = saved.theme
        self._show_logo()
        if self._input is not None:
            self._input.focus()
        self.set_interval(_INBOX_POLL_INTERVAL, self._deliver_pending_messages)

    def _pin_session(self, name: str) -> None:
        """Adopt `name` as this conversation's session and publish it.

        The pin is what routing reads from here on; the
        `settings_store.update` is the "active session" convention other
        surfaces (and a later `miniouto status`) read.
        """

        self._session_name = name
        self._session_assigned = True
        self._chat_started = True
        settings_store.update(session=name)

    def _mint_session_name(self) -> str:
        import datetime
        import uuid

        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"tui-{ts}-{uuid.uuid4().hex[:6]}"

    def _adopt_minted_session(self) -> None:
        """Mint this TUI's own session name, then pin it.

        A fresh name would otherwise start with NO provider config and freeze
        whatever the global default happened to be at its first turn, silently
        dropping the model the conversation the user just continued was
        configured with. Seeding is copy-if-absent (`session_config`), so a
        source without a pin seeds nothing and the new session freezes the
        default itself — and a hand-picked name can never be overwritten here
        because this only ever runs on a name this call just minted.
        """

        # The seed source is the published active session, read once here —
        # it must NOT go through `_pinned_session`, or this TUI's routing
        # (and its inbox drain) would adopt another process's session.
        source = settings_store.load().session or _DEFAULT_SESSION
        name = self._mint_session_name()
        session_config.seed_from(name, source)
        self._pin_session(name)

    def _pinned_session(self) -> str:
        """The session this conversation routes on (see `_session_name`).

        Before the first session event there is no pin, so this falls back
        to a unique per-instance queue WITHOUT adopting the published active
        session. The provider-config seed for the mint is read separately in
        `_adopt_minted_session`, which is the only place that global value is
        wanted. The mint in `_dispatch` still replaces the fallback with this
        TUI's own `tui-…` name, which is why a CLI run's `--name` is never
        continued here.
        """

        if self._session_name is None:
            self._session_name = self._session_name or self._unassigned_inbox
        return self._session_name

    def _inbox_session(self) -> str:
        """The session whose inbox this TUI drains — the exact value
        `ChatOptions.session` gets in `_dispatch`, which is also the inbox
        directory the core's BEFORE_LLM_CALL hook drains."""

        return self._pinned_session()

    def _config_session(self) -> str:
        """Whose pinned config the chips should read, WITHOUT adopting it.

        `_pinned_session` is the one-shot adoption that decides what this
        conversation routes on, and it belongs to the first routing call — a
        repaint must not make that decision as a side effect, or the chips
        would freeze the name they read at mount and never follow a session
        published a moment later.
        """

        return self._session_name or settings_store.load().session or _DEFAULT_SESSION

    def _show_logo(self) -> None:
        if self._log is None:
            return
        self._log.remove_children()
        self._reset_rows()
        # The logo belongs to the chat COLUMN, not to the app: on a wide
        # terminal the column is capped, so sizing by the app width alone
        # would draw it past the right edge of the log.
        columns = logo_columns(self._log.size.width, self.size.width)
        self._log.mount(
            Static(Text(_render_logo(columns - 2), style=self.current_theme.accent))
        )
        self._log.mount(Static(""))
        self._log.mount(Static(Text(f"miniouto v{__version__} — Ready. Press Ctrl+P for commands.", style="dim")))
        self._logo_shown = True

    def watch_theme(self) -> None:
        settings_store.update(theme=self.theme)
        if self._logo_shown:
            self._show_logo()

    def get_system_commands(self, screen) -> Iterable[SystemCommand]:
        yield SystemCommand(
            "00 New session",
            "Create a new session",
            self._new_session,
        )
        yield SystemCommand(
            "01 Pick session",
            "Switch the active session",
            self._open_session_picker,
        )
        yield SystemCommand(
            "02 Configure outo",
            "Provider → model → reasoning for the main agent",
            self._configure_outo,
        )
        yield SystemCommand(
            "03 Configure subagent",
            "Provider → model → reasoning for the subagent",
            self._open_subagent_editor,
        )
        yield SystemCommand(
            "04 Pick style",
            "Switch the active style document",
            self._open_style_picker,
        )
        yield SystemCommand(
            "05 Theme",
            "Change the current theme",
            self.action_change_theme,
        )
        yield SystemCommand(
            "06 Clear log",
            "Clear the chat log (also bound to Ctrl+L)",
            self.action_clear_log,
        )
        remaining = [
            cmd for cmd in super().get_system_commands(screen)
            if cmd.title != "Theme"
        ]
        for i, cmd in enumerate(remaining):
            yield SystemCommand(
                f"{i + 7:02d} {cmd.title}",
                cmd.help,
                cmd.callback,
                cmd.discover,
            )

    # ── chip click routing ──────────────────────────────────────────────────

    def on_status_chip_chip_clicked(self, event: StatusChip.ChipClicked) -> None:
        chip = event.chip
        if chip.id == "chip-outo":
            self._configure_outo()
        elif chip.id == "chip-subagent":
            self._open_subagent_editor()
        elif chip.id == "chip-style":
            self._open_style_picker()
        elif chip.id == "chip-session":
            self._open_session_picker()

    # ── modal actions ───────────────────────────────────────────────────────

    def _configure_outo(self) -> None:
        self._open_provider_settings(OUTO_TARGET)

    def _open_subagent_editor(self) -> None:
        self._open_provider_settings(SUBAGENT_TARGET)

    def _open_provider_settings(self, target: TrioTarget) -> None:
        """Open the provider settings popup for one trio (see the class docstring)."""

        self.push_screen(
            ProviderSettingsPopup(
                target,
                session=self._pinned_session(),
                on_change=self._refresh_chips,
            )
        )

    def _apply_provider(self, name: str) -> None:
        """Adopt `name` as the active provider, through the pin and the default.

        The pin write is what keeps a provider added mid-conversation from
        being shadowed by the config this session already froze.
        """

        _persist_trio(self._pinned_session(), OUTO_TARGET, (name, *_model_defaults(name)))
        self._refresh_chips()

    async def _catalog_add_flow(self) -> None:
        self._spinner_status("fetching catalog…", working=True)
        try:
            all_providers = await asyncio.to_thread(catalog_api.list_providers)
        except Exception as exc:
            self._spinner_status(f"catalog error: {exc}")
            return
        self._clear_spinner_status()

        name_to_provider: dict[str, dict[str, Any]] = {}
        for p in all_providers:
            fmt, _ = sdk_to_format(p.get("sdk"), p.get("api"))
            if fmt:
                name_to_provider[p["name"]] = p
        if not name_to_provider:
            self._spinner_status("No catalog providers have a supported api_format.")
            return

        picked_name = await self.push_screen_wait(
            SelectionModal(
                f"Add from catalog ({len(name_to_provider)} addable)",
                sorted(name_to_provider.keys()),
                allow_none=False,
            )
        )
        if not picked_name:
            return

        catalog_p = name_to_provider[picked_name]
        our_name = catalog_api.slugify(picked_name)
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

        self._spinner_status("fetching model list…", working=True)
        default_model = ""
        try:
            models = await asyncio.to_thread(catalog_api.list_models, picked_name)
            if models:
                default_model = models[0].get("id", "")
        except Exception as exc:
            self._spinner_status(f"catalog models error: {exc}")

        reasoning_effort: str | None = None
        reasoning_api = _get_reasoning_api()
        if default_model and reasoning_api is not None:
            choices: list[str] | None = None
            try:
                choices = await asyncio.to_thread(
                    reasoning_api.reasoning_choices, default_model, our_name
                )
            except Exception:
                choices = None
            if choices:
                default_choice: str | None = None
                try:
                    default_choice = await asyncio.to_thread(
                        reasoning_api.default_reasoning_choice,
                        default_model,
                        our_name,
                    )
                except Exception:
                    default_choice = None
                picked = await self.push_screen_wait(
                    SelectionModal(
                        f"Reasoning ({default_model})",
                        choices,
                        current=default_choice or "off",
                        allow_none=False,
                    )
                )
                if picked:
                    reasoning_effort = picked

        try:
            provider = add_provider_from_lma(
                name=our_name,
                api_key=api_key.strip(),
                sdk=catalog_p.get("sdk"),
                api=catalog_p.get("api"),
                default_model=default_model,
                reasoning_effort=reasoning_effort,
            )
        except TypeError:
            # Parallel `reasoning_effort` kwarg hasn't landed yet; build
            # without it and persist the value via upsert afterward.
            provider = add_provider_from_lma(
                name=our_name,
                api_key=api_key.strip(),
                sdk=catalog_p.get("sdk"),
                api=catalog_p.get("api"),
                default_model=default_model,
            )
        except ValueError as exc:
            self._spinner_status(f"add failed: {exc}")
            return

        paths.ensure_dirs()
        provider_store.upsert(provider)
        if reasoning_effort is not None:
            stored = provider_store.get(our_name)
            if stored is not None:
                try:
                    provider_store.upsert(replace(stored, reasoning_effort=reasoning_effort))
                except TypeError:
                    provider_store.upsert(_with_reasoning_effort(stored, reasoning_effort))
        self._apply_provider(our_name)
        status_parts = [
            f"provider → {our_name} (added from catalog, default-model={default_model or '-'}"
        ]
        if reasoning_effort:
            status_parts.append(f"reasoning={reasoning_effort}")
        status_parts.append(")")
        self._spinner_status(" · ".join(status_parts))

    def _open_custom_add_wizard(self) -> None:
        state: dict[str, Any] = {}

        def ask_name() -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                name = catalog_api.slugify(result)
                if not name:
                    self._spinner_status("name required; wizard cancelled.")
                    return
                if provider_store.get(name) is not None:
                    self._spinner_status(f"Provider {name!r} already exists.")
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
                ask_max_context(result.strip())

            self.push_screen(
                TextInputModal(
                    "Custom provider: default model (optional)",
                    placeholder="model-id",
                    hint="Empty to set later · Enter to continue · Esc to cancel",
                ),
                on_close,
            )

        def ask_max_context(model: str) -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                try:
                    parsed = _parse_optional_int(result)
                except ValueError:
                    self._spinner_status("invalid max context window; skipping")
                    parsed = None
                ask_max_tokens(model, parsed)

            self.push_screen(
                TextInputModal(
                    "Custom provider: max context window (optional)",
                    placeholder="e.g. 128000 · empty to skip",
                    hint="Enter to continue · Esc to cancel",
                ),
                on_close,
            )

        def ask_max_tokens(model: str, ctx: int | None) -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                try:
                    parsed = _parse_optional_int(result)
                except ValueError:
                    self._spinner_status("invalid max output tokens; skipping")
                    parsed = None
                ask_reasoning(model, ctx, parsed)

            self.push_screen(
                TextInputModal(
                    "Custom provider: max output tokens (optional)",
                    placeholder="e.g. 16384 · empty to skip",
                    hint="Enter to save · Esc to cancel",
                ),
                on_close,
            )

        def ask_reasoning(model: str, ctx: int | None, tokens: int | None) -> None:
            def on_close(result: str | None) -> None:
                if result is None:
                    return
                stored = result.strip() or None
                state["reasoning"] = stored
                paths.ensure_dirs()
                provider_store.upsert(
                    _construct_provider_with_reasoning(
                        name=state["name"],
                        api_format=state["api_format"],
                        base_url=state["base_url"],
                        api_key=state["api_key"],
                        default_model=model,
                        source=SOURCE_CUSTOM,
                        max_context_window=ctx,
                        max_output_tokens=tokens,
                        reasoning_effort=stored,
                    )
                )
                self._apply_provider(state["name"])
                status = f"provider → {state['name']} (custom)"
                if stored:
                    status = f"{status} · reasoning={stored}"
                self._spinner_status(status)

            self.push_screen(
                TextInputModal(
                    "Custom provider: reasoning effort (optional)",
                    placeholder=(
                        "optional · e.g. medium / on · empty to skip"
                    ),
                    hint="Enter to save · Esc to cancel",
                ),
                on_close,
            )

        ask_name()

    def _open_style_picker(self) -> None:
        styles = style_store.list_styles()
        if not styles:
            self._spinner_status("No styles installed. Run `miniouto style add <repo>`.")
            return
        # The style shown is the one THIS session resolves: its own pin, else
        # the global default. `_config_session` (not `_pinned_session`): the
        # pin belongs to the session the CONVERSATION runs on, and adopting
        # a session as a side effect of opening a picker would be wrong.
        session = self._config_session()
        current = (session_config.read(session) or session_config.EMPTY).style
        current = current or settings_store.load().style

        def _on_close(result: str | None) -> None:
            if not result:
                return
            # The choice becomes the DEFAULT (like a provider/model change)
            # and every session that already exists freezes the style it is
            # running under — so this conversation and the ones already
            # running keep theirs, and a session started NEXT picks the new
            # one up. The provider trio is untouched.
            runtime.set_active_style(result, session=session)
            self._refresh_chips()
            self._spinner_status(f"style → {result}")

        self.push_screen(
            SelectionModal("Select style", styles, current=current, allow_none=False),
            _on_close,
        )

    def _open_session_picker(self) -> None:
        here = str(paths_runtime.INVOCATION_CWD)
        this_dir: list[tuple[str, str]] = []
        other_dir: list[tuple[str, str]] = []
        for name, cwd, _mtime in session_store.list_sessions_with_meta():
            if cwd == here:
                this_dir.append((name, name))
            else:
                label = f"[{Path(cwd).name or cwd}] {name}" if cwd else f"[?] {name}"
                other_dir.append((name, label))
        current = self._session_name if self._chat_started else ""

        def _on_close(result: str | None) -> None:
            if result is None:
                return
            if result == "__new__":
                self._new_session()
                return
            self._pin_session(result)
            self._load_session_history(result)
            self._refresh_chips()

        sections: list[tuple[str, list[tuple[str, str]]]] = []
        if this_dir:
            sections.append(("This directory", this_dir))
        if other_dir:
            sections.append(("Other directories", other_dir))
        self.push_screen(
            SelectionModal(
                "Select session",
                [],
                current=current,
                allow_none=False,
                extra_options=[("__new__", "+ new session…")],
                sections=sections,
            ),
            _on_close,
        )

    def _new_session(self) -> None:
        self._adopt_minted_session()
        session_store.create(self._pinned_session())
        self._refresh_chips()
        self._show_logo()

    def _load_session_history(self, session_name: str) -> None:
        if self._log is None:
            return
        self._log.remove_children()
        self._reset_rows()
        self._logo_shown = False
        self._sync_ticker()
        self._pending_tool_rows.clear()
        data = session_store.load(session_name)
        # Bulk: rows are only collected here; `_mount_tail_window` mounts
        # just the trailing window instead of the whole history.
        self._bulk_loading = True
        try:
            if not data.turns:
                self._mount_row(Static(Text("(empty session)", style="dim")))
            for turn in data.turns:
                if turn.user:
                    self._post_user(turn.user)
                for ev_dict in turn.events:
                    self._replay_event(LoopEvent.from_dict(ev_dict))
                if turn.assistant:
                    self._mount_row(AnswerRow(Markdown(turn.assistant)))
                elif turn.status in ("running", "interrupted"):
                    # A "running" turn on disk means its writer died mid-turn
                    # (SIGKILL/crash); either way there is no final answer to
                    # render — mark the gap explicitly instead of leaving the
                    # previous turn's answer looking like the response.
                    self._post_system(
                        "turn interrupted — progress above was saved; "
                        "send a message to continue"
                    )
                self._mount_row(Static(""))
        finally:
            self._bulk_loading = False
        self._mount_tail_window()

    def _replay_event(self, event: LoopEvent) -> None:
        """Re-render one recorded turn event (session reload path).

        Everything routes through `_add_event_row`, so a reloaded turn
        renders exactly like the live one. Events recorded by an older
        miniouto — `subagent_start` / `subagent_end` in particular — still
        deserialize (the event schema is unchanged), and now replay as
        plain rows: only the special rendering is gone.
        """

        self._add_event_row(event)

    # ── status / refresh ────────────────────────────────────────────────────

    def _refresh_chips(self) -> None:
        if self._panel is None:
            return
        s = settings_store.load()
        # Read-through: the pinned session's own config wins, so a TUI bound to
        # a session shows THAT session's provider/model/reasoning even while the
        # global default moves underneath it in another process.
        config = _effective_config(self._config_session())
        outo_model, outo_reasoning = _trio_display(OUTO_TARGET, config)[1:]
        self._panel.set_value("outo", f"{outo_model} {outo_reasoning}")

        sub_provider, sub_model, sub_reasoning = _trio(config, SUBAGENT_TARGET.fields)
        if sub_provider or sub_model or sub_reasoning:
            record = _record(sub_provider)
            model_part = sub_model or (record.default_model if record else "") or "same"
            reasoning_part = sub_reasoning or _reasoning_chip_value(record)
            self._panel.set_value("subagent", f"{model_part} {reasoning_part}")
        else:
            self._panel.set_value("subagent", "same as outo")

        # The style chip reads the SESSION's pinned style, the same
        # read-through the provider/model chips do: a style chosen for one
        # session must not show up (or apply) in another one.
        pinned_style = config.style or s.style
        self._panel.set_value("style", pinned_style or "-")
        name = self._session_name if self._chat_started and self._session_name else ""
        self._panel.set_session(name)

    # ── chat flow ───────────────────────────────────────────────────────────

    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        self._input.text = ""
        if self._busy:
            self._queue_inbox_message(text)
            return
        if self._logo_shown and self._log is not None:
            self._log.remove_children()
            self._reset_rows()
            self._logo_shown = False
        self._post_user(text)
        if self._log is not None:
            # Re-engage the follow anchor: submitting a new prompt always
            # jumps to the tail, even if the user was reading history.
            self._log.anchor()
        self.run_worker(self._dispatch(text), exclusive=True)

    def _queue_inbox_message(self, text: str) -> None:
        """Hand typed text to the running turn (mid-turn interjection).

        Writing it to the inbox makes it an ordinary user message for the
        model: the core's BEFORE_LLM_CALL hook drains it into the live
        history at the next iteration, exactly like an alarm, so the user
        can steer a run already in flight instead of waiting for it to end.
        The row is rendered here for immediacy and remembered by id — when
        the real event arrives `_add_event_row` suppresses its row, so the
        text is shown once even though the model receives it once.
        """

        try:
            message_id = inbox.insert(
                self._inbox_session(), text, kind=inbox.USER_KIND, source="tui"
            )
        except (OSError, ValueError) as exc:
            self._post_system(f"could not queue message: {exc}")
            return
        self._optimistic_rows[message_id] = self._user_row(text)
        self._mount_row(self._optimistic_rows[message_id])
        self._spinner_status("queued for the running turn")

    def _claim_inbox_event(self, event: LoopEvent) -> bool:
        """False when this event's row is already on screen, because the
        message was rendered optimistically when it was inserted."""

        detail = event.detail or ""
        if not detail.startswith(_INBOX_DETAIL_PREFIX):
            return True
        message_id = detail[len(_INBOX_DETAIL_PREFIX) :]
        if message_id not in self._optimistic_rows:
            return True
        self._optimistic_rows.pop(message_id)
        return False

    def action_clear_log(self) -> None:
        if self._log is not None:
            self._log.remove_children()
            self._reset_rows()

    def action_copy_text(self) -> None:
        text = self.screen.get_selected_text()
        if text:
            self.copy_to_clipboard(text)
            self.screen.clear_selection()

    def action_page_up(self) -> None:
        if self._log is not None:
            self._log.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        if self._log is not None:
            self._log.scroll_page_down(animate=False)

    def action_scroll_home(self) -> None:
        if self._log is not None:
            self._log.scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        if self._log is not None:
            self._log.scroll_end(animate=False)

    def action_escape_pressed(self) -> None:
        if not self._busy or self._cancel_event is None:
            self._last_escape = 0.0
            return
        now = time.monotonic()
        if now - self._last_escape <= _ESC_DOUBLE_PRESS_WINDOW:
            self._last_escape = 0.0
            self._esc_hint = "stopping after the current step…"
            self._esc_hint_until = float("inf")
            self._cancel_event.set()
            self._spinner_status(self._esc_hint)
        else:
            self._last_escape = now
            self._esc_hint = "press esc again to stop"
            self._esc_hint_until = now + _ESC_DOUBLE_PRESS_WINDOW
            self._spinner_status(self._esc_hint)

    # ── chat log rows ───────────────────────────────────────────────────────

    def _reset_rows(self) -> None:
        """Drop the row list alongside `remove_children()` (logo/clear/load)."""

        self._window.reset()
        self._optimistic_rows.clear()
        self._stream_text_row = None
        self._stream_thinking_row = None

    def _apply_stream_text(self, delta: str) -> None:
        """Extend the current message's live text row (UI thread only).

        The reference is cleared on every path that invalidates the row
        (`_reset_rows`, `_finalize_stream`, and the message-boundary events
        in `_add_event_row`), so a row that is gone — evicted by window
        pressure, or dropped by a session reload — is never updated again
        and never re-mounted, which would put the message out of order.
        Window eviction leaves the row tracked but off-DOM; appending to it
        there is a string concat that no layout ever sees, and it keeps the
        row correct if a later chunk-load mounts it again.
        """

        if self._log is None or not delta:
            return
        row = self._stream_text_row
        if row is None:
            row = StreamRow(delta)
            self._stream_text_row = row
            self._mount_row(row)
            return
        row.append(delta)

    def _apply_stream_thinking(self, delta: str) -> None:
        """Extend the current message's live reasoning row (UI thread only)."""

        if self._log is None or not delta:
            return
        row = self._stream_thinking_row
        if row is None:
            row = ThinkingRow()
            self._stream_thinking_row = row
            self._mount_row(row)
            row.begin_stream()
        row.append(delta)

    def _finalize_stream(self) -> None:
        """Drop the live rows once the message's canonical form exists.

        The streamed text is a prefix of the message the final answer (or
        the recorded `thinking` / `response` event) already carries, so
        keeping the live row would render the same content twice.
        """

        for row in (self._stream_text_row, self._stream_thinking_row):
            if row is not None:
                self._window.remove(row)
        self._stream_text_row = None
        self._stream_thinking_row = None

    def _mount_row(self, widget: Static, msg_ref: str | None = None) -> None:
        if msg_ref is not None:
            widget.msg_ref = msg_ref
        if self._log is None:
            return
        self._window.append(self._log, widget, defer_mount=self._bulk_loading)
        self._schedule_jump_button_sync()

    def _mount_tail_window(self) -> None:
        """After a bulk load, mount only the trailing window of rows."""

        if self._log is not None:
            self._window.mount_tail(self._log)
        self._schedule_jump_button_sync()

    def _schedule_jump_button_sync(self) -> None:
        """Re-check the jump button once the reflow has grown the log.

        Appending rows moves `max_scroll_y`, so a button decision taken
        before the mount would be stale; the tail of the turn reaches here
        for free, but the idle tail has no scroll event to hang it on.
        """

        if self._log is not None:
            self._log.call_after_refresh(self._sync_jump_button)

    def _on_log_scroll(self, scroll_y: float) -> None:
        if self._log is not None:
            self._window.maybe_load_older(self._log, scroll_y)
        self._sync_jump_button()

    def _add_event_row(self, event: LoopEvent) -> None:
        if event.kind == "thinking":
            live = self._stream_thinking_row
            self._stream_thinking_row = None
            if live is not None and live.is_attached:
                # The streamed reasoning was this message's prefix; the
                # whole-text event is the canonical copy.
                live.finalize(event.text)
                live.msg_ref = event.msg_ref
                return
            self._mount_row(ThinkingRow(event.text), event.msg_ref)
            return
        if event.kind == "tool":
            row = ToolRow(
                event.text,
                detail=getattr(event, "detail", None),
                tool_name=event.tool_name,
            )
            row.msg_ref = event.msg_ref
            self._pending_tool_rows.append(row)
            self._mount_row(row)
            return
        if event.kind == "tool_result":
            text = event.text or ""
            is_error = text.startswith("error: ")
            pending: ToolRow | None = None
            for candidate in self._pending_tool_rows:
                if (
                    candidate.tool_name == event.tool_name
                    and not candidate.has_result
                ):
                    pending = candidate
                    break
            if pending is not None:
                self._pending_tool_rows.remove(pending)
                pending.set_result(text, is_error=is_error)
            else:
                # Defensive fallback: a tool_result with no matching tool
                # event (replay oddity, out-of-order delivery, partial
                # session). Mount a plain muted row so the result isn't lost.
                row = EventRow(Text(text))
                if is_error:
                    row.add_class("-error")
                self._mount_row(row, event.msg_ref)
            return
        if event.kind == "response" and self._stream_text_row is not None:
            # The deltas already rendered this message in the live row
            # above; a second copy would duplicate the text.
            self._stream_text_row = None
            return
        if event.kind == "alarm":
            if not self._claim_inbox_event(event):
                return
            self._post_alarm_row(event.text)
            return
        if event.kind == inbox.USER_KIND:
            if not self._claim_inbox_event(event):
                return
            self._post_user(event.text)
            return
        if event.kind in ("response", "context"):
            # Message boundary: the next deltas belong to a new message and
            # need their own live row.
            self._stream_text_row = None
            self._stream_thinking_row = None
        row = EventRow(Text(event.text))
        if event.kind == "error":
            row.add_class("-error")
        self._mount_row(row, event.msg_ref)

    def _remove_event_rows(self, msg_refs: list[str]) -> None:
        """Drop the rows of messages the agent loop removed (UI thread).

        A `tool` row and its `tool_result` share one `msg_ref` — the
        assistant message that issued the call — so pruning by ref takes
        both or neither and a result can never be left orphaned. The
        pending-pair list is filtered for the same reason: it holds live
        row references that a later `tool_result` would otherwise try to
        attach to a row that is no longer displayed.

        The blank `Static` spacers between turns carry no `msg_ref` at
        all, hence the `getattr` default — they are never prunable.
        """

        refs = set(msg_refs)
        if not refs:
            return
        for row in list(self._window.rows):
            if getattr(row, "msg_ref", None) in refs:
                self._window.remove(row)
        self._pending_tool_rows = [
            r for r in self._pending_tool_rows if r.msg_ref not in refs
        ]

    def _user_row(self, text: str) -> RowStatic:
        return RowStatic(
            Text("> ", style=f"bold {self.current_theme.accent}") + Text(text)
        )

    def _post_user(self, text: str) -> None:
        self._mount_row(self._user_row(text))

    def _post_alarm_row(self, text: str) -> None:
        """Render an alarm as the user message the model actually receives.

        An alarm is a synthetic `role="user"` message (invariant 16), so it
        reads like one — the same construction as `_user_row` — plus the
        `-alarm` class, which keeps it identifiable as a system notice.
        """

        row = self._user_row(text)
        row.add_class("-alarm")
        self._mount_row(row)

    def _post_system(self, text: str) -> None:
        self._mount_row(RowStatic(Text(f"[{text}]", style=self.current_theme.warning)))

    def _render_spinner(self, frame: str, text: str) -> None:
        if self._panel is not None:
            self._panel.render_spinner(frame, text)

    # ── animation ticker ────────────────────────────────────────────────────
    #
    # ONE ticker drives every animated indicator: the chat status line, the
    # wizard's "fetching…" line, and the per-row spinners of live background
    # jobs. It runs while any of them has something to show, and stops (with
    # the bottom row blanked) the moment none does — a spinner that froze
    # mid-frame, or a stale line left over after the work was done, is worse
    # than no indicator at all.

    def _set_chat_working(self, working: bool) -> None:
        self._chat_working = working
        self._sync_ticker()

    def _set_activity(self, text: str) -> None:
        self._activity = text or _SPINNER_DEFAULT_TEXT

    def _sync_ticker(self) -> None:
        needed = self._chat_working or self._wizard_working
        if needed:
            if self._spin_timer is None:
                self._spin_frame_idx = 0
                self._spin_timer = self.set_interval(_SPINNER_INTERVAL, self._tick_spin)
                self._paint_spinner(_SPINNER_FRAMES[0])
        elif self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
            self._render_spinner("", "")

    def _tick_spin(self) -> None:
        frame = _SPINNER_FRAMES[self._spin_frame_idx % len(_SPINNER_FRAMES)]
        self._spin_frame_idx += 1
        self._paint_spinner(frame)
        # A turn that just ended can leave nothing to animate, so re-check
        # instead of leaving a timer running on nothing.
        self._sync_ticker()

    def _paint_spinner(self, frame: str) -> None:
        if self._chat_working:
            self._render_spinner(frame, self._spin_text())
        elif self._wizard_working and self._wizard_status:
            self._render_spinner(frame, self._wizard_status)
        else:
            self._render_spinner("", "")

    def _spin_text(self) -> str:
        text = self._activity
        if self._esc_hint is not None:
            if time.monotonic() < self._esc_hint_until:
                return self._esc_hint
            self._esc_hint = None
        return text

    def _spinner_status(self, text: str, *, working: bool = False) -> None:
        """Put a line in the bottom row: `working=True` animates it, else it
        is a plain notice (no glyph — see `BottomPanel.render_spinner`)."""

        self._wizard_status = text
        self._wizard_working = working
        self._sync_ticker()
        if not working:
            self._render_spinner("", text)

    def _clear_spinner_status(self) -> None:
        self._wizard_status = None
        self._wizard_working = False
        self._sync_ticker()
        if not self._chat_working:
            # `_sync_ticker` only blanks as part of stopping a timer it owns;
            # a notice never had one, so blank the line explicitly.
            self._render_spinner("", "")

    def _deliver_pending_messages(self) -> None:
        """Turn queued inbox messages into a real turn (UI thread).

        The in-turn path (`runtime._make_alarm_hook`, on BEFORE_LLM_CALL) is
        the only delivery a running turn needs — it wins while `_busy`, so it
        owns the queue and this must not double-deliver. Between turns no
        hook runs, and the message would sit invisible until the user typed
        something; instead the text becomes the prompt of a turn, so
        `turn.user`, the chat row and the model's own view are the same
        string by construction. Several messages coalesce into one prompt
        and one turn.
        """

        if self._busy:
            return
        try:
            messages = inbox.drain(self._inbox_session())
        except (OSError, ValueError):
            return
        if not messages:
            return
        text = "\n\n".join(m.text for m in messages)
        for message in messages:
            # A message inserted mid-turn was already shown where the user
            # typed it; retire that row so the prompt row below replaces it
            # instead of duplicating it.
            stale = self._optimistic_rows.pop(message.id, None)
            if stale is not None:
                self._window.remove(stale)
        if self._logo_shown and self._log is not None:
            self._log.remove_children()
            self._reset_rows()
            self._logo_shown = False
        self._post_user(text)
        if self._log is not None:
            self._log.anchor()
        self.run_worker(self._dispatch(text), exclusive=True)

    def _apply_handoff(self) -> None:
        """Move the conversation into the session a summary hand-off opened.

        The OLD conversation visibly ENDS with the marker (and keeps it on
        disk), then the pin moves — which is what makes the `_deliver_pending_messages`
        call below drain the NEW session's inbox, where the continuation
        message already is, and turn it into the next prompt with no user
        action.
        """

        handoff = summary.consume_handoff()
        if handoff is None:
            return
        previous = self._pinned_session()
        self._mount_row(EventRow(Text(handoff_marker(handoff))))
        _persist_handoff_marker(previous, handoff)
        self._pin_session(handoff.new_session)
        self._refresh_chips()
        self._spinner_status(f"handoff → {handoff.new_session}")

    async def _dispatch(self, prompt: str) -> None:
        assert self._log is not None
        first_message = not self._chat_started
        self._chat_started = True
        self._busy = True
        # A previous turn that died mid-stream can leave live rows behind.
        self._finalize_stream()
        if first_message and not self._session_assigned:
            # Gated on `_session_assigned`, NOT on `_session_name is None`:
            # the one-shot adoption in `_pinned_session` sets the name alone,
            # and this TUI must still mint its own `tui-…` session over it.
            self._adopt_minted_session()
            self._refresh_chips()
        s = settings_store.load()
        # A TUI run is always plain, so identity is all it registers: the
        # summarization lane reads the session back to build the summary
        # request and, after `continue_session`, the continuation message.
        summary.set_run_contract(session=self._pinned_session(), style=s.style or "")
        sink = TUIEventSink(self)
        self._cancel_event = threading.Event()
        self._last_escape = 0.0
        try:
            opts = ChatOptions(
                prompt=prompt,
                session=self._pinned_session(),
                model=s.model or None,
                continue_session=True,
                cancel_event=self._cancel_event,
            )
            await asyncio.to_thread(run_chat, opts, sink)
        except LoopCancelledError:
            self._post_system("stopped by user (esc esc)")
        except Exception as exc:
            self._post_system(f"error: {exc}")
        self._cancel_event = None
        self._busy = False
        self._esc_hint = None
        self._refresh_chips()
        # A summary hand-off may have ended that turn; the session moves first
        # so the delivery below reads the NEW session's inbox.
        self._apply_handoff()
        # A message can land while the final answer is already streaming,
        # after the last BEFORE_LLM_CALL — nothing else will consume it.
        self._deliver_pending_messages()


def run_tui() -> None:
    # Probe the terminal BEFORE Textual starts: the graphics probe reads the
    # terminal's reply on stdin, and Textual's reader thread takes stdin
    # over the moment the app runs — after that the reply is never seen and
    # the probe only burns its timeout. Doing it here means the mode is
    # known before the first frame, and the render path never waits.
    detect_graphics_mode()
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
