"""Theme tests for the PyRatatui TUI.

The miniouto palette is OWNER-PINNED: these hexes are not samples, they are the
contract, and a port that nudges one of them is a visual regression nobody
asked for. The builtin themes are held to a weaker standard — the names and the
fields Textual declares must survive the port, and the derived fields must obey
the ordering the TUI actually depends on (an answer layer brighter than body
text, body text brighter than disabled text).
"""

from __future__ import annotations

# The TUI paints with the shim, so its types are what these tests compare.
import miniouto.cli.tui.pt as pt
from miniouto.cli.tui.render import hex_to_rgb
from miniouto.cli.tui.theme import (
    BUILTIN_THEMES,
    DEFAULT_THEME_NAME,
    MINIOUTO_THEME,
    THEME_ORDER,
    TuiTheme,
    color,
    get_theme,
)


def luminance(hex_value: str) -> float:
    """Relative luminance, enough to rank two palette entries."""

    red, green, blue = hex_to_rgb(hex_value)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


# ─── the owner's theme ───────────────────────────────────────────────────────


def test_miniouto_hexes_are_the_owners_exact_values() -> None:
    theme = MINIOUTO_THEME
    assert theme.name == "miniouto"
    assert theme.dark is True
    assert theme.background == "#0a0e14"
    assert theme.surface == "#0f141b"
    assert theme.panel == "#151b24"
    assert theme.primary == "#2fc2ff"
    assert theme.accent == "#2fc2ff"
    assert theme.secondary == "#8f86d8"
    assert theme.success == "#52bd8a"
    assert theme.warning == "#c99a4e"
    assert theme.error == "#d16f7e"
    assert theme.foreground == "#a9bac5"
    assert theme.boost == "#1d2530"
    assert theme.text_muted == "#7b8490"
    assert theme.text_disabled == "#434a52"
    assert theme.text_answer == "#c5d0d8"


def test_miniouto_primary_and_accent_are_the_same_owner_pinned_color() -> None:
    # The logo and the tool-row glyph render through accent; two different
    # accent colors is the failure this pins.
    assert MINIOUTO_THEME.primary == MINIOUTO_THEME.accent == "#2fc2ff"


def test_miniouto_text_ladder_is_monotonic() -> None:
    # The whole point of the 3-layer hierarchy: answer > body > muted > disabled.
    theme = MINIOUTO_THEME
    ladder = [theme.text_disabled, theme.text_muted, theme.foreground, theme.text_answer]
    brightness = [luminance(value) for value in ladder]
    assert brightness == sorted(brightness)
    assert len(set(brightness)) == len(brightness), "each layer must be distinct"


def test_miniouto_surfaces_step_up_from_the_background() -> None:
    theme = MINIOUTO_THEME
    steps = [theme.background, theme.surface, theme.panel, theme.boost]
    brightness = [luminance(value) for value in steps]
    assert brightness == sorted(brightness)
    assert len(set(brightness)) == len(brightness)


def test_every_miniouto_color_is_a_six_digit_hex() -> None:
    for field in TuiTheme.__dataclass_fields__:
        value = getattr(MINIOUTO_THEME, field)
        if field in ("name", "dark"):
            continue
        assert isinstance(value, str) and value.startswith("#"), field
        assert len(value) == 7, f"{field}={value!r}"
        hex_to_rgb(value)  # parses, or it is not a color


# ─── builtin themes ──────────────────────────────────────────────────────────


def test_expected_builtin_names_are_present() -> None:
    expected = {
        "textual-dark",
        "textual-light",
        "nord",
        "gruvbox",
        "catppuccin-mocha",
        "dracula",
        "tokyo-night",
        "monokai",
        "flexoki",
        "catppuccin-latte",
        "catppuccin-frappe",
        "catppuccin-macchiato",
        "solarized-light",
        "solarized-dark",
        "atom-one-dark",
        "atom-one-light",
    }
    assert expected <= set(BUILTIN_THEMES)


def test_theme_names_match_their_keys() -> None:
    for key, theme in BUILTIN_THEMES.items():
        assert theme.name == key


def test_builtin_themes_declare_the_values_textual_declared() -> None:
    nord = BUILTIN_THEMES["nord"]
    assert nord.primary == "#88C0D0"
    assert nord.background == "#2E3440"
    assert nord.surface == "#3B4252"
    assert nord.panel == "#434C5E"
    assert nord.foreground == "#D8DEE9"
    assert nord.dark is True

    light = BUILTIN_THEMES["textual-light"]
    assert light.dark is False
    assert light.background == "#E0E0E0"


def test_builtin_themes_derive_a_complete_palette() -> None:
    for name, theme in BUILTIN_THEMES.items():
        for field in TuiTheme.__dataclass_fields__:
            if field in ("name", "dark"):
                continue
            value = getattr(theme, field)
            assert isinstance(value, str) and len(value) == 7, f"{name}.{field}={value!r}"


def test_derived_text_ramp_keeps_muted_brighter_than_disabled() -> None:
    # The ordering the TUI relies on for hint text vs disabled controls. A
    # "60% toward the background" reading would silently invert it.
    for name, theme in BUILTIN_THEMES.items():
        shade = 1 if theme.dark else -1
        assert luminance(theme.text_muted) * shade > luminance(theme.text_disabled) * shade, name


def test_derived_answer_layer_steps_away_from_the_background() -> None:
    for name, theme in BUILTIN_THEMES.items():
        shade = 1 if theme.dark else -1
        assert luminance(theme.text_answer) * shade > luminance(theme.foreground) * shade, name


def test_derived_theme_missing_panel_uses_the_textual_mix() -> None:
    # textual-dark declares no panel: Textual derives surface lerped 10% toward
    # primary, which is the value the port must reproduce.
    assert BUILTIN_THEMES["textual-dark"].panel == "#1b2730"
    assert BUILTIN_THEMES["textual-dark"].background == "#121212"
    assert BUILTIN_THEMES["textual-dark"].surface == "#1e1e1e"


def test_derived_theme_missing_foreground_inverts_the_background() -> None:
    # textual-light declares no foreground; Textual uses the background inverse.
    assert BUILTIN_THEMES["textual-light"].foreground == "#1f1f1f"


# ─── lookup and ordering ─────────────────────────────────────────────────────


def test_get_theme_returns_miniouto_by_default() -> None:
    assert get_theme(DEFAULT_THEME_NAME) is MINIOUTO_THEME
    assert get_theme("miniouto") is MINIOUTO_THEME


def test_get_theme_finds_a_builtin() -> None:
    assert get_theme("dracula") is BUILTIN_THEMES["dracula"]


def test_get_theme_returns_none_for_an_unknown_name() -> None:
    # The app falls back to the default rather than raising on a stale
    # settings.theme value.
    assert get_theme("no-such-theme") is None
    assert get_theme("") is None


def test_unknown_names_are_not_silently_matched() -> None:
    assert get_theme("Nord") is None
    assert get_theme(" nord") is None


def test_theme_order_starts_with_miniouto_then_lists_every_builtin() -> None:
    assert THEME_ORDER[0] == DEFAULT_THEME_NAME
    assert THEME_ORDER[1:] == list(BUILTIN_THEMES)
    assert set(THEME_ORDER) == {DEFAULT_THEME_NAME, *BUILTIN_THEMES}
    assert len(THEME_ORDER) == len(set(THEME_ORDER)), "the picker must not repeat a name"


# ─── color() ─────────────────────────────────────────────────────────────────


def test_color_returns_a_pyratatui_color() -> None:
    assert color("#2fc2ff") == pt.Color.rgb(0x2F, 0xC2, 0xFF)


def test_color_is_cached() -> None:
    # The render loop resolves the same palette entries every frame.
    assert color("#52bd8a") is color("#52bd8a")


def test_theme_color_helper_resolves_palette_entries() -> None:
    assert MINIOUTO_THEME.color(MINIOUTO_THEME.primary) == pt.Color.rgb(0x2F, 0xC2, 0xFF)
    for name, theme in BUILTIN_THEMES.items():
        assert theme.color(theme.background) == color(theme.background), name
