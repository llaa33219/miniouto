"""PyRatatui TUI package — the public surface the old ``cli/tui.py`` had.

``miniouto.cli.tui`` used to be a single 4000-line module, so every caller and
every behavioural test names its symbols off the PACKAGE: ``from
miniouto.cli.tui import ChatTUI``, ``tui_module.run_chat``,
``monkeypatch.setattr(tui_module, "_get_reasoning_api", ...)``. The port split
the module into layers (``input`` / ``render`` / ``theme`` / ``graphics`` /
``rows`` / ``widgets`` / ``modals`` / ``config`` / ``app``), so this module
exists to keep those names — and that patchability — intact.

Two rules, both load-bearing:

* **Explicit imports first.** Everything the old module exported is imported
  here by name, so it is present without a module-level ``__getattr__`` hop and
  a reader can see the whole surface in one place.
* **The module aliases are re-exported too** (``run_chat``, ``inbox``,
  ``summary``, ``catalog_api``, ``context_api``, ``settings_store``, …). The
  suites patch these ON THIS PACKAGE, and the implementations resolve them at
  CALL time through :func:`~.nodes.package_seam` — a plain copy would make such
  a patch silently miss the caller (see the ``package_seam`` docstring).

A lazy ``__getattr__`` stays as a FALLBACK, so a name this file forgot still
resolves through its owning layer instead of breaking an import.
"""

from __future__ import annotations

from miniouto import __version__, paths_runtime
from miniouto.cli.chat import handoff_marker
from miniouto.core import context as context_api
from miniouto.core import inbox, runtime, summary, update_check
from miniouto.core import modelslist as catalog_api
from miniouto.core.chat import ChatOptions, run_chat
from miniouto.core.events import STREAM_THINKING, LoopEvent
from miniouto.core.providers import (
    SUPPORTED_FORMATS,
    CatalogDialect,
    add_provider_from_catalog,
    catalog_compatibility,
    resolve_catalog_dialect,
)
from miniouto.core.runtime import LoopCancelledError, default_provider_config
from miniouto.storage import paths, session_config
from miniouto.storage import providers as provider_store
from miniouto.storage import sessions as session_store
from miniouto.storage import settings as settings_store
from miniouto.storage import styles as style_store
from miniouto.storage.providers import SOURCE_CUSTOM, is_catalog_source
from miniouto.tools import ascii_art

from . import pt
from .app import (
    _DEFAULT_SESSION,
    _ESC_DOUBLE_PRESS_WINDOW,
    _INBOX_DETAIL_PREFIX,
    _INBOX_POLL_INTERVAL,
    _SPINNER_DEFAULT_TEXT,
    _SPINNER_FRAMES,
    _SPINNER_INTERVAL,
    _STREAM_BOUNDARY_KINDS,
    _STREAM_FLUSH_INTERVAL,
    CHAT_MAX_WIDTH,
    THEME_NAME,
    WHEEL_LINES,
    ChatTUI,
    Timer,
    TUIEventSink,
    Workers,
    _persist_handoff_marker,
    _render_logo,
    _RowWindow,
    _StreamCoalescer,
    logo_columns,
    run_tui,
    tui_summary,
)
from .config import (
    CAPS_FIELD,
    COMPATIBILITY_FIELD,
    OUTO_TARGET,
    SENTINEL_CATALOG_ADD,
    SENTINEL_CUSTOM_ADD,
    SENTINEL_SAME_AS_OUTO,
    SUBAGENT_TARGET,
    TrioTarget,
    _construct_provider_with_reasoning,
    _default_reasoning,
    _effective_config,
    _get_reasoning_api,
    _merged_config,
    _model_defaults,
    _parse_optional_int,
    _persist_trio,
    _reasoning_chip_value,
    _reasoning_choices,
    _reasoning_target,
    _record,
    _trio,
    _trio_display,
    _with_reasoning_effort,
)
from .graphics import (
    GRAPHICS_MODE_SIXEL,
    GRAPHICS_MODE_TEXT,
    GRAPHICS_MODE_TGP,
    GraphicsOverlay,
    ImagePlacement,
    detect_graphics_mode,
    graphics_mode,
    terminal_graphics_supported,
)
from .input import Key, Mouse, Paste, Resize, terminal_size
from .modals import (
    CommandPalette,
    Modal,
    ProviderSettingsPopup,
    SelectionModal,
    SettingRow,
    SystemCommand,
    TextInputModal,
    default_system_commands,
)
from .nodes import Node, Offset, Rect, Screen, Selection, Size
from .rows import (
    _ART_MAX_ROWS,
    COLLAPSED_GLYPH,
    EXPANDED_GLYPH,
    GLYPH_CELLS,
    LEFT_MOUSE_BUTTON,
    AnswerRow,
    ClickToToggle,
    EventRow,
    RowStatic,
    StreamRow,
    ThinkingRow,
    ToggleGlyphHover,
    ToolRow,
    art_path_in_result,
    png_path_in_result,
)
from .theme import (
    BUILTIN_THEMES,
    DEFAULT_THEME_NAME,
    MINIOUTO_THEME,
    THEME_ORDER,
    TuiTheme,
    get_theme,
)
from .widgets import (
    _EVICT_MARGIN_LINES,
    _TOP_LOAD_LINES,
    _WINDOW_CHUNK_ROWS,
    _WINDOW_INITIAL_ROWS,
    _WINDOW_MAX_ROWS,
    EVICT_MARGIN_LINES,
    HELP_TEXT,
    TOP_LOAD_LINES,
    WINDOW_CHUNK_ROWS,
    WINDOW_INITIAL_ROWS,
    WINDOW_MAX_ROWS,
    BottomPanel,
    ChatInput,
    ChatScroll,
    Header,
    JumpButton,
    StatusChip,
    at_bottom,
)

__all__ = [
    "BUILTIN_THEMES",
    "CAPS_FIELD",
    "CHAT_MAX_WIDTH",
    "COLLAPSED_GLYPH",
    "COMPATIBILITY_FIELD",
    "DEFAULT_THEME_NAME",
    "EVICT_MARGIN_LINES",
    "EXPANDED_GLYPH",
    "GLYPH_CELLS",
    "GRAPHICS_MODE_SIXEL",
    "GRAPHICS_MODE_TEXT",
    "GRAPHICS_MODE_TGP",
    "HELP_TEXT",
    "LEFT_MOUSE_BUTTON",
    "MINIOUTO_THEME",
    "OUTO_TARGET",
    "SENTINEL_CATALOG_ADD",
    "SENTINEL_CUSTOM_ADD",
    "SENTINEL_SAME_AS_OUTO",
    "SOURCE_CUSTOM",
    "STREAM_THINKING",
    "SUBAGENT_TARGET",
    "SUPPORTED_FORMATS",
    "THEME_NAME",
    "THEME_ORDER",
    "TOP_LOAD_LINES",
    "WHEEL_LINES",
    "WINDOW_CHUNK_ROWS",
    "WINDOW_INITIAL_ROWS",
    "WINDOW_MAX_ROWS",
    "_ART_MAX_ROWS",
    "_DEFAULT_SESSION",
    "_ESC_DOUBLE_PRESS_WINDOW",
    "_EVICT_MARGIN_LINES",
    "_INBOX_DETAIL_PREFIX",
    "_INBOX_POLL_INTERVAL",
    "_SPINNER_DEFAULT_TEXT",
    "_SPINNER_FRAMES",
    "_SPINNER_INTERVAL",
    "_STREAM_BOUNDARY_KINDS",
    "_STREAM_FLUSH_INTERVAL",
    "_TOP_LOAD_LINES",
    "_WINDOW_CHUNK_ROWS",
    "_WINDOW_INITIAL_ROWS",
    "_WINDOW_MAX_ROWS",
    "AnswerRow",
    "BottomPanel",
    "CatalogDialect",
    "ChatInput",
    "ChatOptions",
    "ChatScroll",
    "ChatTUI",
    "ClickToToggle",
    "CommandPalette",
    "EventRow",
    "GraphicsOverlay",
    "Header",
    "ImagePlacement",
    "JumpButton",
    "Key",
    "LoopCancelledError",
    "LoopEvent",
    "Modal",
    "Mouse",
    "Node",
    "Offset",
    "Paste",
    "ProviderSettingsPopup",
    "Rect",
    "Resize",
    "RowStatic",
    "Screen",
    "Selection",
    "SelectionModal",
    "SettingRow",
    "Size",
    "StatusChip",
    "StreamRow",
    "SystemCommand",
    "TUIEventSink",
    "TextInputModal",
    "ThinkingRow",
    "Timer",
    "ToggleGlyphHover",
    "ToolRow",
    "TrioTarget",
    "TuiTheme",
    "Workers",
    "_RowWindow",
    "_StreamCoalescer",
    "__version__",
    "_construct_provider_with_reasoning",
    "_default_reasoning",
    "_effective_config",
    "_get_reasoning_api",
    "_merged_config",
    "_model_defaults",
    "_parse_optional_int",
    "_persist_handoff_marker",
    "_persist_trio",
    "_reasoning_chip_value",
    "_reasoning_choices",
    "_reasoning_target",
    "_record",
    "_render_logo",
    "_trio",
    "_trio_display",
    "_with_reasoning_effort",
    "add_provider_from_catalog",
    "art_path_in_result",
    "ascii_art",
    "at_bottom",
    "catalog_api",
    "catalog_compatibility",
    "context_api",
    "default_provider_config",
    "default_system_commands",
    "detect_graphics_mode",
    "get_theme",
    "graphics_mode",
    "handoff_marker",
    "inbox",
    "is_catalog_source",
    "logo_columns",
    "paths",
    "paths_runtime",
    "png_path_in_result",
    "provider_store",
    "pt",
    "resolve_catalog_dialect",
    "run_chat",
    "run_tui",
    "runtime",
    "session_config",
    "session_store",
    "settings_store",
    "style_store",
    "summary",
    "terminal_graphics_supported",
    "terminal_size",
    "tui_summary",
    "update_check",
]

_PENDING = (
    "miniouto.cli.tui: '{name}' is not part of the TUI package "
    "(see .miniouto/plans/tui-framework-contract.md)"
)
"""Message the fallback raises for a name no layer provides."""


def __getattr__(name: str) -> object:
    """FALLBACK resolver: find a forgotten name in the layer that owns it."""

    import importlib

    for module_name in (
        ".app",
        ".rows",
        ".widgets",
        ".modals",
        ".config",
        ".graphics",
        ".render",
        ".theme",
        ".input",
        ".nodes",
    ):
        try:
            module = importlib.import_module(module_name, __name__)
        except Exception:
            continue
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(_PENDING.format(name=name))
