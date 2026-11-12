"""Theme data for the PyRatatui TUI: the miniouto palette plus Textual's builtins.

The old TUI handed colors to Textual and read them back as ``$variables``
strings. The port paints ``pyratatui`` directly, so every color has to exist as
a concrete ``#rrggbb`` value up front — including the ones Textual used to
DERIVE (``surface`` / ``panel`` / ``boost`` and the text ramp). Those are
computed here with the same arithmetic Textual used, so the ported themes look
byte-identical to the Textual ones they replace.

Derivation decisions worth knowing:

* ``panel`` when a theme omits it is ``surface`` lerped 10% toward ``primary``
  — Textual's ``ColorSystem._generate`` does exactly that.
* ``foreground`` when omitted is the background's inverse, as in Textual.
* The text ramp is ALPHA-style compositing, not a darkening curve:
  ``text_muted`` is the foreground at 60% over the background and
  ``text_disabled`` at 38% (Textual's ``foreground.with_alpha(0.60/0.38)``).
  That keeps the ordering the theme actually relies on — muted is brighter
  than disabled, which is what MINIOUTO_THEME's own #7b8490 / #434a52 pair
  shows. Reading "60%" as "60% of the way toward the background" instead
  would invert the ladder and paint hints dimmer than disabled text.
* ``boost`` and ``text_answer`` step AWAY from the background: toward white on
  a dark theme, toward black on a light one. Literal "lighten to white" on a
  light theme would make the answer layer vanish into the paper.

Colors are plain strings and the dataclass is frozen: rows hold them, compare
them and serialize them; only :func:`color` turns one into a ``pyratatui.Color``
and it caches, because the render loop asks for the same dozen values on every
frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from . import pt
from .render import blend, hex_to_rgb, rgb_to_hex

# ─── the themes ──────────────────────────────────────────────────────────────
__all__ = [
    "BUILTIN_THEMES",
    "DEFAULT_THEME_NAME",
    "MINIOUTO_THEME",
    "THEME_ORDER",
    "TuiTheme",
    "color",
    "get_theme",
    "hex_to_rgb",
    "rgb_to_hex",
]

DEFAULT_THEME_NAME = "miniouto"
"""The theme the TUI boots on (``settings.theme`` may name another one)."""

_WHITE = "#ffffff"
_BLACK = "#000000"

_TEXT_MUTED_ALPHA = 0.60
_TEXT_DISABLED_ALPHA = 0.38
_ANSWER_STEP = 0.20
_BOOST_STEP = 0.08
_PANEL_PRIMARY_MIX = 0.10


@dataclass(frozen=True)
class TuiTheme:
    """One resolved palette. Every color is a ``#rrggbb`` string."""

    name: str
    dark: bool
    background: str
    surface: str
    panel: str
    primary: str
    accent: str
    secondary: str
    success: str
    warning: str
    error: str
    foreground: str
    boost: str
    text_muted: str
    text_disabled: str
    text_answer: str

    def color(self, hex_value: str) -> pt.Color:
        """Shorthand for ``theme.color(theme.primary)`` at a call site."""

        return color(hex_value)


MINIOUTO_THEME = TuiTheme(
    name=DEFAULT_THEME_NAME,
    dark=True,
    # A midnight blue-black family, comfort-tuned, with a THREE-LAYER text
    # hierarchy: text-answer (12.33:1) > foreground (9.69:1) > text-muted
    # (5.11:1) > text-disabled (2.16:1), all on `background`. The values are
    # the owner's, ported verbatim from the Textual TUI so the port is a
    # repaint, not a restyle; the surfaces step up in even increments
    # (channel sums 44 → 62 → 84 → 114).
    background="#0a0e14",
    surface="#0f141b",
    panel="#151b24",
    # `primary` drives focus borders, the input's left rule, block cursors and
    # the scrollbar; `accent` is the same color because the logo and the
    # tool-row toggle glyph render through it. OWNER-PINNED.
    primary="#2fc2ff",
    accent="#2fc2ff",
    # The loud colors, one notch down: same hue families, trimmed chroma and
    # luminance so they stop shouting on a dark ground.
    secondary="#8f86d8",
    success="#52bd8a",
    warning="#c99a4e",
    # One step above #d16b7a, which fell short of the 4.5:1 floor on `boost`.
    error="#d16f7e",
    foreground="#a9bac5",
    # `boost` is the chip / selection / markdown-code surface. The ramp entries
    # ride alongside it because the derived defaults are not this palette's
    # muted/disabled grays; `text-answer` is the top of the ladder and the only
    # near-white in the palette, so the agent's final answer reads as the
    # focal layer.
    boost="#1d2530",
    text_muted="#7b8490",
    text_disabled="#434a52",
    text_answer="#c5d0d8",
)
"""The owner's theme: dark surfaces, `#2fc2ff` accent."""


def _derive(
    name: str,
    *,
    dark: bool,
    primary: str,
    secondary: str,
    accent: str,
    warning: str,
    error: str,
    success: str,
    foreground: str | None = None,
    background: str | None = None,
    surface: str | None = None,
    panel: str | None = None,
) -> TuiTheme:
    """Fill a Textual builtin theme's omitted fields the way Textual did."""

    if background is None:
        background = "#121212" if dark else "#efefef"
    if surface is None:
        surface = "#1e1e1e" if dark else "#f5f5f5"
    if foreground is None:
        red, green, blue = hex_to_rgb(background)
        foreground = rgb_to_hex(255 - red, 255 - green, 255 - blue)
    if panel is None:
        panel = blend(primary, surface, _PANEL_PRIMARY_MIX)

    extreme = _WHITE if dark else _BLACK
    return TuiTheme(
        name=name,
        dark=dark,
        background=background,
        surface=surface,
        panel=panel,
        primary=primary,
        accent=accent,
        secondary=secondary,
        success=success,
        warning=warning,
        error=error,
        foreground=foreground,
        boost=blend(extreme, panel, _BOOST_STEP),
        text_muted=blend(foreground, background, _TEXT_MUTED_ALPHA),
        text_disabled=blend(foreground, background, _TEXT_DISABLED_ALPHA),
        text_answer=blend(extreme, foreground, _ANSWER_STEP),
    )


BUILTIN_THEMES: dict[str, TuiTheme] = {
    "textual-dark": _derive(
        "textual-dark",
        dark=True,
        primary="#0178D4",
        secondary="#004578",
        accent="#ffa62b",
        warning="#ffa62b",
        error="#ba3c5b",
        success="#4EBF71",
        foreground="#e0e0e0",
    ),
    "textual-light": _derive(
        "textual-light",
        dark=False,
        primary="#004578",
        secondary="#0178D4",
        accent="#ffa62b",
        warning="#ffa62b",
        error="#ba3c5b",
        success="#4EBF71",
        background="#E0E0E0",
        surface="#D8D8D8",
        panel="#D0D0D0",
    ),
    "nord": _derive(
        "nord",
        dark=True,
        primary="#88C0D0",
        secondary="#81A1C1",
        accent="#B48EAD",
        foreground="#D8DEE9",
        background="#2E3440",
        surface="#3B4252",
        panel="#434C5E",
        success="#A3BE8C",
        warning="#EBCB8B",
        error="#BF616A",
    ),
    "gruvbox": _derive(
        "gruvbox",
        dark=True,
        primary="#85A598",
        secondary="#A89A85",
        warning="#fe8019",
        error="#fb4934",
        success="#b8bb26",
        accent="#fabd2f",
        foreground="#fbf1c7",
        background="#282828",
        surface="#3c3836",
        panel="#504945",
    ),
    "catppuccin-mocha": _derive(
        "catppuccin-mocha",
        dark=True,
        primary="#F5C2E7",
        secondary="#cba6f7",
        warning="#FAE3B0",
        error="#F28FAD",
        success="#ABE9B3",
        accent="#fab387",
        foreground="#cdd6f4",
        background="#181825",
        surface="#313244",
        panel="#45475a",
    ),
    "dracula": _derive(
        "dracula",
        dark=True,
        primary="#BD93F9",
        secondary="#6272A4",
        warning="#FFB86C",
        error="#FF5555",
        success="#50FA7B",
        accent="#FF79C6",
        background="#282A36",
        surface="#2B2E3B",
        panel="#313442",
        foreground="#F8F8F2",
    ),
    "tokyo-night": _derive(
        "tokyo-night",
        dark=True,
        primary="#BB9AF7",
        secondary="#7AA2F7",
        warning="#E0AF68",
        error="#F7768E",
        success="#9ECE6A",
        accent="#FF9E64",
        foreground="#a9b1d6",
        background="#1A1B26",
        surface="#24283B",
        panel="#414868",
    ),
    "monokai": _derive(
        "monokai",
        dark=True,
        primary="#AE81FF",
        secondary="#F92672",
        accent="#66D9EF",
        warning="#FD971F",
        error="#F92672",
        success="#A6E22E",
        foreground="#d6d6d6",
        background="#272822",
        surface="#2e2e2e",
        panel="#3E3D32",
    ),
    "flexoki": _derive(
        "flexoki",
        dark=True,
        primary="#205EA6",
        secondary="#24837B",
        warning="#AD8301",
        error="#AF3029",
        success="#66800B",
        accent="#9B76C8",
        background="#100F0F",
        surface="#1C1B1A",
        panel="#282726",
        foreground="#FFFCF0",
    ),
    "catppuccin-latte": _derive(
        "catppuccin-latte",
        dark=False,
        secondary="#DC8A78",
        primary="#8839EF",
        warning="#DF8E1D",
        error="#D20F39",
        success="#40A02B",
        accent="#FE640B",
        foreground="#4C4F69",
        background="#EFF1F5",
        surface="#E6E9EF",
        panel="#CCD0DA",
    ),
    "catppuccin-frappe": _derive(
        "catppuccin-frappe",
        dark=True,
        primary="#CA9EE6",
        secondary="#EF9F76",
        warning="#E5C890",
        error="#E78284",
        success="#A6D189",
        accent="#F4B8E4",
        foreground="#C6D0F5",
        background="#303446",
        surface="#414559",
        panel="#51576D",
    ),
    "catppuccin-macchiato": _derive(
        "catppuccin-macchiato",
        dark=True,
        primary="#C6A0F6",
        secondary="#F5A97F",
        warning="#EED49F",
        error="#ED8796",
        success="#A6DA95",
        accent="#F5BDE6",
        foreground="#CAD3F5",
        background="#24273A",
        surface="#363A4F",
        panel="#494D64",
    ),
    "solarized-light": _derive(
        "solarized-light",
        dark=False,
        primary="#268bd2",
        secondary="#2aa198",
        warning="#cb4b16",
        error="#dc322f",
        success="#859900",
        accent="#6c71c4",
        foreground="#586e75",
        background="#fdf6e3",
        surface="#eee8d5",
        panel="#eee8d5",
    ),
    "solarized-dark": _derive(
        "solarized-dark",
        dark=True,
        primary="#268bd2",
        secondary="#2aa198",
        warning="#cb4b16",
        error="#dc322f",
        success="#859900",
        accent="#6c71c4",
        background="#002b36",
        surface="#073642",
        panel="#073642",
        foreground="#839496",
    ),
    "atom-one-dark": _derive(
        "atom-one-dark",
        dark=True,
        primary="#61AFEF",
        secondary="#C678DD",
        warning="#DEB25B",
        error="#F06262",
        success="#62F062",
        accent="#A378C2",
        foreground="#ABB2BF",
        background="#282C34",
        surface="#3B414D",
        panel="#4F5666",
    ),
    "atom-one-light": _derive(
        "atom-one-light",
        dark=False,
        primary="#4078F2",
        secondary="#A626A4",
        warning="#D8D938",
        error="#F23F3F",
        success="#6CF23F",
        accent="#bf9232",
        foreground="#383A42",
        background="#FAFAFA",
        surface="#E0E0E0",
        panel="#CCCCCC",
    ),
}
"""Textual's builtin themes, ported. Textual's order is preserved.

``ansi-dark`` / ``ansi-light`` are deliberately absent: their palette is
``ansi_blue``-style NAMES, which a ``#rrggbb`` palette cannot express, so
offering them would silently paint the wrong colors.
"""

THEME_ORDER: list[str] = [DEFAULT_THEME_NAME, *BUILTIN_THEMES]
"""Picker order: the owner's theme first, then Textual's own order."""


def get_theme(name: str) -> TuiTheme | None:
    """Look a theme up by name, or ``None`` when it is unknown."""

    if name == DEFAULT_THEME_NAME:
        return MINIOUTO_THEME
    return BUILTIN_THEMES.get(name)


@lru_cache(maxsize=256)
def color(hex_value: str) -> pt.Color:
    """Turn ``#rrggbb`` into a cached ``pyratatui.Color``.

    Cached on purpose: the render loop resolves the same dozen palette entries
    for every row of every frame, and crossing into Rust per call adds up.
    """

    red, green, blue = hex_to_rgb(hex_value)
    return pt.Color.rgb(red, green, blue)
