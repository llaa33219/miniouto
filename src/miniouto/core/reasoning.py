"""Resolve reasoning/thinking request kwargs from lma model metadata.

The resolver turns a single user-facing choice string ("off"/"on"/an
effort level) plus lma's per-model `reasoning_options` into
provider-native request kwargs, merged into `provider_passthrough` by
`core/runtime.py`. It must go through passthrough, not `provider_config`:
miniouto registers providers under user-chosen names and coreouto's
`normalize_provider_config` passes config through UNTRANSLATED for
unknown names — a canonical `reasoning_effort` key would reach the SDK
raw and TypeError.

Only two request shapes are ever emitted: an effort value
(`output_config.effort` / `reasoning.effort` / `reasoning_effort`) and
anthropic's `thinking: {"type": "adaptive"}`. Token-budget shapes
(`enabled` + `budget_tokens`) are deliberately NOT emitted — many
providers reject budget parameters. lma `budget_tokens` models are
driven as a plain toggle instead; legacy digit choices degrade to "on".

Google is deliberately unsupported: coreouto builds
`GenerateContentConfig` itself from only system_instruction + tools and
extra kwargs land on `generate_content(**kwargs)` top-level, so a
`thinking_config` key would TypeError. The resolver returns {} for
api_format "google" rather than duplicating fragile coreouto internals.
"""

from __future__ import annotations

from typing import Any

from . import lma

_OFF_VALUES = ("none", "off")


def resolve_reasoning_passthrough(
    api_format: str,
    model: str,
    provider_name: str | None,
    choice: str | None,
) -> dict[str, Any]:
    """Return provider-native reasoning kwargs, or {} for "no reasoning".

    `choice` is the resolved explicit value (caller computes CLI flag >
    `Provider.reasoning_effort`; None = auto). "none"/"off" always
    disables. Auto uses lma's default: effort → "medium" (or the middle
    non-"none" value), toggle/budget → "on". When lma has no reasoning
    data for the model: no choice → {} (no blind defaults); a choice →
    per-format best-effort mapping (trust the user).
    """

    if choice is not None:
        stripped = choice.strip()
        if stripped.lower() in _OFF_VALUES:
            return {}
        if stripped.isdigit():
            # Legacy token-budget value — budgets are no longer emitted;
            # degrade to a plain toggle-on.
            choice = "on"
    if api_format == "google":
        return {}
    try:
        info = lma.get_model(model, provider_name)
    except Exception:
        # Transport failure must not disable reasoning; treat as no data.
        info = None
    opt = _first_reasoning_option(info)
    if opt is None:
        if choice is None:
            return {}
        return _best_effort(api_format, choice)
    otype = opt.get("type")
    if otype == "budget_tokens":
        otype = "toggle"
    if choice is None:
        value = _auto_value(otype, opt)
        if value is None:
            return {}
    else:
        value = choice.strip()
        if otype == "effort" and (value.lower() == "on" or value.isdigit()):
            # A toggle-era stored value ("on", or a legacy budget number)
            # against an effort-ladder model — sending effort="on" would be
            # rejected, so degrade to the ladder's default.
            value = _default_effort(opt.get("values") or [])
            if value is None:
                return {}
    return _map_value(api_format, otype, value)


def reasoning_choices(model: str, provider_name: str | None) -> list[str] | None:
    """Choices for UI pickers, or None when the model has no reasoning options.

    Always starts with "off". effort → ["off", *values-minus-"none"];
    toggle/budget → ["off", "on"].
    """

    opt = _try_first_option(model, provider_name)
    if opt is None:
        return None
    otype = opt.get("type")
    if otype == "effort":
        values = [
            str(v) for v in (opt.get("values") or []) if str(v).lower() != "none"
        ]
        return ["off", *values]
    if otype in ("toggle", "budget_tokens"):
        return ["off", "on"]
    return None


def default_reasoning_choice(model: str, provider_name: str | None) -> str | None:
    """The auto default as a storable string, or None when no reasoning options.

    effort → "medium" if in values else middle non-"none" value;
    toggle/budget → "on".
    """

    opt = _try_first_option(model, provider_name)
    if opt is None:
        return None
    otype = opt.get("type")
    if otype == "effort":
        return _default_effort(opt.get("values") or [])
    if otype in ("toggle", "budget_tokens"):
        return "on"
    return None


def _try_first_option(model: str, provider_name: str | None) -> dict[str, Any] | None:
    try:
        return _first_reasoning_option(lma.get_model(model, provider_name))
    except Exception:
        return None


def _first_reasoning_option(info: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pick the richest entry from lma's `reasoning_options` list.

    The list can carry MULTIPLE descriptors — e.g. kimi-k3 returns
    `[{"type": "toggle"}, {"type": "effort", "values": [...]}]`. Reading
    only `options[0]` would hide the effort ladder behind the toggle, so
    prefer effort > budget_tokens > toggle.
    """

    if not info or info.get("reasoning") is False:
        return None
    options = info.get("reasoning_options")
    if not isinstance(options, list):
        return None
    entries = [o for o in options if isinstance(o, dict)]
    for preferred in ("effort", "budget_tokens", "toggle"):
        for entry in entries:
            if entry.get("type") == preferred:
                return entry
    return None


def _auto_value(otype: Any, opt: dict[str, Any]) -> str | None:
    if otype == "effort":
        return _default_effort(opt.get("values") or [])
    if otype == "toggle":
        return "on"
    return None


def _default_effort(values: list[Any]) -> str | None:
    strs = [str(v) for v in values]
    if "medium" in strs:
        return "medium"
    non_none = [v for v in strs if v.lower() != "none"]
    if not non_none:
        return None
    return non_none[len(non_none) // 2]


def _map_value(api_format: str, otype: Any, value: str) -> dict[str, Any]:
    v = value.strip()
    if otype == "toggle":
        return _map_toggle(api_format, v)
    if otype == "effort":
        return _map_effort(api_format, v)
    return {}


def _map_toggle(api_format: str, value: str) -> dict[str, Any]:
    if value.lower() != "on":
        return {}
    if api_format == "anthropic":
        return {"thinking": {"type": "adaptive"}}
    if api_format == "openai-response":
        return {"reasoning": {"effort": "medium"}}
    if api_format == "openai":
        return {"thinking": {"type": "enabled"}}
    return {}


def _map_effort(api_format: str, value: str) -> dict[str, Any]:
    if value.lower() == "none":
        return {}
    if api_format == "anthropic":
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": value}}
    if api_format == "openai-response":
        return {"reasoning": {"effort": value}}
    if api_format == "openai":
        return {"reasoning_effort": value}
    return {}


def _best_effort(api_format: str, choice: str) -> dict[str, Any]:
    """Map an explicit choice with no lma data (trust the user)."""

    v = choice.strip()
    if v.isdigit() or v.lower() == "on":
        return _map_toggle(api_format, "on")
    return _map_effort(api_format, v)
