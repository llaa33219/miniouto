"""Provider-config helpers shared by the settings popup and the app layer.

WHY this is its own module: the trio resolution/display/persist rules are
invariant 12 in AGENTS.md — the popup, the chips and the app all have to agree
on them — and they are pure config logic with no rendering in them at all.
They are ported VERBATIM (behavior and comments) from the old Textual
``cli/tui.py`` so the port is a repaint, not a rewrite; only the relative
imports moved (``..core`` became ``...core``, because the TUI is now the
package ``miniouto.cli.tui`` rather than the module ``miniouto.cli.tui``).

Two seams matter to tests and to the app layer:

* ``_get_reasoning_api`` is monkeypatched by the TUI tests to stub modelslist's
  reasoning ladder, and it is looked up through THIS module's globals — patch
  ``miniouto.cli.tui.config._get_reasoning_api``, not a copy someone
  ``from``-imported.
* ``catalog_api`` / ``context_api`` / ``session_config`` / ``provider_store`` /
  ``settings_store`` are module ALIASES on purpose: a test (or the app) that
  patches ``miniouto.core.modelslist.list_models`` must reach every caller.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from typing import Any

from ...core.runtime import default_provider_config
from ...storage import providers as provider_store
from ...storage import session_config
from ...storage import settings as settings_store

__all__ = [
    "CAPS_FIELD",
    "COMPATIBILITY_FIELD",
    "OUTO_TARGET",
    "SENTINEL_CATALOG_ADD",
    "SENTINEL_CUSTOM_ADD",
    "SENTINEL_SAME_AS_OUTO",
    "SUBAGENT_TARGET",
    "TrioTarget",
]

SENTINEL_CATALOG_ADD = "__catalog_add__"
SENTINEL_CUSTOM_ADD = "__custom_add__"
SENTINEL_SAME_AS_OUTO = "__same_as_outo__"

# Field name of the settings-popup row that edits a custom provider's caps.
# `SettingRow` capitalizes the field, so this value IS the visible label.
CAPS_FIELD = "caps"
COMPATIBILITY_FIELD = "compatibility"

# Sentinel for `_construct_provider_with_reasoning(reasoning_effort=...)`:
# don't touch the provider's existing value (vs. None which would clear it).
_SENTINEL_NO_REASONING = object()


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
        from ...core import reasoning as reasoning_api
    except ImportError:
        return None
    return reasoning_api


_PRISTINE_GET_REASONING_API = _get_reasoning_api
"""The function object as defined here — the identity check the seam needs."""


def _current_reasoning_api() -> Any:
    """``_get_reasoning_api()``, honouring a monkeypatch on EITHER module.

    The old TUI suites patch ``miniouto.cli.tui._get_reasoning_api`` (the
    re-exported name) while the trio helpers below are the real callers. A plain
    ``from .config import _get_reasoning_api`` in the package would bind a COPY,
    so the patch would miss every caller and the test would silently exercise
    the real modelslist ladder.

    Resolving through the package module object at CALL time makes the patch
    authoritative. The identity check is what keeps a patch applied HERE (the
    owning module) working too: while the package still holds the pristine
    function, this module's own global is used, which is exactly what
    ``monkeypatch.setattr(config_module, "_get_reasoning_api", …)`` replaces.
    """

    package = sys.modules.get("miniouto.cli.tui")
    value = getattr(package, "_get_reasoning_api", None) if package is not None else None
    if value is None or value is _PRISTINE_GET_REASONING_API:
        return _get_reasoning_api()
    return value()


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
    """modelslist's auto reasoning default for a model, or None (no options / lookup failed)."""

    reasoning_api = _current_reasoning_api()
    if reasoning_api is None or not model:
        return None
    try:
        return reasoning_api.default_reasoning_choice(model, provider_name)
    except Exception:
        return None


def _reasoning_choices(model: str, provider_name: str) -> list[str] | None:
    """The effort levels a model offers, or None when it has no reasoning.

    A modelslist transport failure is indistinguishable from "no reasoning options"
    here, exactly as it is in `resolve_reasoning_passthrough`: refusing to
    send reasoning is never the safe default, so the caller degrades to
    "leave whatever is configured alone".
    """

    reasoning_api = _current_reasoning_api()
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
    preference another process set for the same provider. modelslist's auto default
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

    return _merged_config(session)


def _merged_config(session: str) -> session_config.ProviderConfig:
    """The session's config, with the SUBAGENT trio filled from the default.

    The pin is a set of overrides ON the default (storage/session_config),
    not a replacement for it: an empty key means "follow the global
    default". Reading the pin ALONE therefore hides the subagent trio
    the global default carries — the chip would show "same as outo"
    while the subagent is in fact configured (the display/config
    mismatch reported). Merging that ONE trio gives the chips and the
    popup the SAME resolution the runtime applies, without freezing a
    pin just to look at it.

    The OUTO trio is left as the pin has it: filling it from the default
    would show the DEFAULT's reasoning effort beside the PIN's model —
    a display the runtime never produces.
    """

    pinned = session_config.read(session)
    if pinned is None:
        return default_provider_config()
    default = default_provider_config()
    # ONLY the SUBAGENT trio is merged with the default.
    #
    # The OUTO trio is left as the pin has it: a pin that names a
    # provider says so, and an empty outo key is displayed as "-" (or
    # resolved by `_trio_display`). Filling it from the default here
    # would show the DEFAULT's reasoning effort beside the PIN's model
    # — a display the runtime never produces.
    #
    # The SUBAGENT trio is different: a pin that names only outo's trio
    # leaves the subagent keys empty, and those empty keys mean "whatever
    # the subagent default says" — the global `subagent_*` values the
    # user configured. Reading the pin ALONE therefore showed "same as
    # outo" while the subagent was in fact configured (the reported
    # display/config mismatch).
    return replace(
        pinned,
        subagent_provider=pinned.subagent_provider or default.subagent_provider,
        subagent_model=pinned.subagent_model or default.subagent_model,
        subagent_reasoning=pinned.subagent_reasoning or default.subagent_reasoning,
    )


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

    Unset is the interesting state, not an error: it means "follow modelslist", and
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

    The outo trio writes `settings.provider` + `settings.model` +
    `settings.reasoning` — ONE paired choice, always all three, so the
    global default is exactly what the user last picked (issue: the last
    change not becoming the default).

    The provider RECORD's `default_model` follows the pick too — it is
    the ENDPOINT's default, and it is what a fresh install and every
    other process resolve, so the last change must become the default
    there as well. Its `reasoning_effort` is left alone: a subagent on
    the same provider resolves its effort from that record, and
    rewriting it is what dragged the subagent along with every outo
    reasoning change.

    A subagent that INHERITS outo keeps the MODEL it was already running
    on: the inheritance is frozen into this session's pin when outo's
    trio changes, so an outo edit cannot drag the subagent to a
    different model (issue: an outo edit resetting the subagent). The
    reasoning effort still inherits outo's — that is what the subagent
    trio's empty values mean.

    The subagent trio writes the three `subagent_*` settings, where empty
    means "same as outo". An empty value CLEARS the pin key, handing it
    back to the global default — the documented `session_config`
    semantics, not a reset to nothing.
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
    # The outo trio is ONE paired choice — provider, model, reasoning —
    # and it lives in the SETTINGS. The settings keys describe the
    # INSTALLATION ("what outo runs here"); the provider record's
    # `default_model` describes the ENDPOINT ("what this endpoint
    # serves by default"). Keeping the two apart is what stops an outo
    # edit from dragging a subagent along: a subagent resolves its model
    # from its OWN key, never from outo's (see
    # `runtime._effective_subagent_pm`).
    #
    # Written through `save` (not `update`, which SKIPS empty strings):
    # an empty model or reasoning must CLEAR the stored value, not leave
    # the previous one behind.
    settings_store.save(
        replace(
            settings_store.load(),
            provider=provider,
            model=model,
            reasoning=reasoning,
        )
    )
    # NOTHING to do for the subagent here. "SAME AS OUTO" is a LIVE
    # link: the subagent's provider, model and reasoning follow outo's
    # whenever outo changes (that is what the empty values mean). A
    # subagent that names its OWN settings is independent and is not
    # touched — an outo edit can never reach it.
    #
    # The one thing an outo edit must not disturb is the SUBAGENT's own
    # configuration, and nothing here writes it: the outo branch below
    # touches `settings.provider` / `model` / `reasoning` only.

    # The record's `default_model` follows the outo pick — it is what a
    # fresh install and every other process resolve, so the last change
    # IS the new default (issue: the last change not becoming the
    # default).
    #
    # Its `reasoning_effort` does NOT: a subagent on this same provider
    # resolves its effort from that record, so rewriting it here is what
    # dragged the subagent along with every outo reasoning change
    # (issue: an outo edit resetting the subagent). The record keeps the
    # preference it was configured with; outo's own choice lives in
    # `settings.reasoning`.
    #
    # Written AFTER the snapshot above, so the snapshot sees the model
    # the subagent is running on — not the one this edit installs.
    record = _record(provider)
    if record is not None:
        provider_store.upsert(replace(record, default_model=model))
