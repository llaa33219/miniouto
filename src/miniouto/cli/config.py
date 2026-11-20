"""The `config` command group — the CLI lane of the trio writes (invariant 12).

The TUI's settings popup edits a provider trio through BOTH the session pin
and the global default (`cli/tui/config.py:_persist_trio`); this module is
that write's canonical CLI form, so a script can make the same edit without
opening the TUI. The read side (`config show`) reproduces `_merged_config`
(`session_config.read` plus the global default's SUBAGENT trio) and never
`session_config.effective`: looking at a session's configuration must not
freeze a pin for it — freezing belongs to the turn.

`config set` also carries the two pointer-style settings the TUI publishes as
side effects of its own flows: `--theme` (the TUI theme setter's write) and
`--active-session` (`_pin_session`'s published pointer). Both are plain
`settings_store.update` writes, exactly as the TUI makes them.
"""

from __future__ import annotations

from dataclasses import replace
from typing import NoReturn

import typer
from rich.console import Console

from ..core.runtime import default_provider_config
from ..storage import providers as provider_store
from ..storage import session_config
from ..storage import settings as settings_store
from ._yaml import print_yaml

app = typer.Typer(help="Show and edit provider, style and session configuration.")
console = Console()
# stdout is the YAML/✓ lane, so errors go to a stderr console.
console_err = Console(stderr=True)

OUTO = "outo"
SUBAGENT = "subagent"

# The two trios of invariant 12, as field-name triples the session pin
# (`session_config.ProviderConfig`) and the settings (`storage.Settings`)
# share for that trio.
_TRIO_FIELDS: dict[str, tuple[str, str, str]] = {
    OUTO: ("provider", "model", "reasoning"),
    SUBAGENT: ("subagent_provider", "subagent_model", "subagent_reasoning"),
}

_PIN_KEYS = (
    "provider",
    "model",
    "reasoning",
    "subagent_provider",
    "subagent_model",
    "subagent_reasoning",
    "style",
)
_EFFECTIVE_KEYS = (
    "provider",
    "model",
    "reasoning",
    "style",
    "subagent_provider",
    "subagent_model",
    "subagent_reasoning",
)


def _fail(message: str) -> NoReturn:
    console_err.print(f"[red]✗[/red] {message}")
    raise typer.Exit(code=1)


def _merged_config(session: str) -> session_config.ProviderConfig:
    """The session's config with the SUBAGENT trio filled from the default.

    Same interpretation as the TUI's chips (`cli/tui/config.py:_merged_config`):
    the pin is a set of overrides ON the default, so an empty SUBAGENT key
    means "whatever the global `subagent_*` default says" and is filled from
    `default_provider_config` here. The OUTO trio is left as the pin has it —
    filling it from the default would show the default's reasoning beside the
    pin's model, a combination the runtime never produces. Deliberately
    `session_config.read`, never `effective`: reading must not freeze a pin.
    """

    pinned = session_config.read(session)
    if pinned is None:
        return default_provider_config()
    default = default_provider_config()
    return replace(
        pinned,
        subagent_provider=pinned.subagent_provider or default.subagent_provider,
        subagent_model=pinned.subagent_model or default.subagent_model,
        subagent_reasoning=pinned.subagent_reasoning or default.subagent_reasoning,
    )


def _persist_trio(
    session: str, target: str, provider: str, model: str, reasoning: str
) -> None:
    """Write one trio edit through BOTH the session pin and the global default.

    The pin is what THIS session resolves (`runtime._pinned_for`), so the edit
    survives a later default change; the default is what every other process
    and the next session resolve, and the owner's rule is that the most
    recently changed default wins. Every edit therefore writes both.

    The outo trio writes the PAIRED `settings.provider` + `model` +
    `reasoning` through `save` (never `update`, which skips empty strings, so
    an emptied value CLEARS the stored one) and the provider RECORD's
    `default_model` follows the pick — it is the ENDPOINT's default, what a
    fresh install resolves. The record's `reasoning_effort` is left alone: a
    subagent on the same provider resolves its effort from that record, and
    rewriting it is what dragged the subagent along with every outo reasoning
    change. The subagent trio writes the three `settings.subagent_*` keys,
    where empty still means "same as outo".
    """

    provider_key, model_key, reasoning_key = _TRIO_FIELDS[target]
    session_config.set_fields(
        session,
        **{provider_key: provider, model_key: model, reasoning_key: reasoning},
    )
    if target == SUBAGENT:
        settings_store.save(
            replace(
                settings_store.load(),
                subagent_provider=provider,
                subagent_model=model,
                subagent_reasoning=reasoning,
            )
        )
        return
    settings_store.save(
        replace(
            settings_store.load(),
            provider=provider,
            model=model,
            reasoning=reasoning,
        )
    )
    # Written AFTER the settings snapshot above, so the snapshot sees the
    # model the subagent is running on — not the one this edit installs.
    record = provider_store.get(provider) if provider else None
    if record is not None:
        provider_store.upsert(replace(record, default_model=model))


@app.command("show")
def show_cmd(
    session: str = typer.Option(
        ..., "--session", help="Session whose pin and effective config are shown."
    ),
) -> None:
    """Show one session's pinned and effective configuration, as YAML.

    `pin` is the RAW stored pin (`""` everywhere when the session has none);
    `effective` is the merged interpretation the runtime applies. Read-only:
    showing never freezes a pin.
    """

    pinned = session_config.read(session) or session_config.EMPTY
    effective = _merged_config(session)
    print_yaml(
        {
            "session": session,
            "pin": {key: getattr(pinned, key) for key in _PIN_KEYS},
            "effective": {key: getattr(effective, key) for key in _EFFECTIVE_KEYS},
        }
    )


@app.command("seed")
def seed_cmd(
    source: str = typer.Option(..., "--source", help="Session whose pin the new session inherits."),
    target: str = typer.Option(..., "--target", help="Freshly minted session that receives the pin."),
) -> None:
    """Copy a source session's pin onto a fresh one (copy-if-absent).

    The TUI mint's CLI form: `session_config.seed_from` verbatim, so a minted
    `tui-…` name inherits the WHOLE pin — provider trios AND the style key —
    without touching the global default (invariant 12: a seed copies, it
    never rewrites). A target that already carries a pin keeps it; a source
    with no pin seeds nothing.
    """

    if source == target:
        _fail("--source and --target must differ.")
    session_config.seed_from(target, source)
    pinned = session_config.read(target)
    if pinned is None:
        console.print(f"[green]✓[/green] nothing to seed: '{source}' has no pin.")
        return
    console.print(f"[green]✓[/green] seeded '{target}' from '{source}' ({len(pinned.as_dict())} keys).")


@app.command("set")
def set_cmd(
    session: str | None = typer.Option(
        None, "--session", help="Session whose pin the trio write targets (required with --target)."
    ),
    target: str | None = typer.Option(
        None, "--target", help="Write a provider trio: outo or subagent."
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="Trio provider name (required with --target)."
    ),
    model: str | None = typer.Option(
        None, "--model", help="Trio model name (required with --target)."
    ),
    reasoning: str | None = typer.Option(
        None, "--reasoning", help='Trio reasoning effort; "" clears it (required with --target).'
    ),
    theme: str | None = typer.Option(
        None, "--theme", help="TUI theme name written to settings (any name; boot falls back)."
    ),
    active_session: str | None = typer.Option(
        None, "--active-session", help="Published active-session pointer (need not exist yet)."
    ),
) -> None:
    """Edit the configuration: a provider trio, the theme, and/or the active session."""

    parts: list[str] = []
    if target is None and theme is None and active_session is None:
        _fail("Pass at least one of --target, --theme, or --active-session.")
    if target is None:
        if provider is not None or model is not None or reasoning is not None:
            _fail("--provider/--model/--reasoning need --target outo|subagent.")
    else:
        if target not in _TRIO_FIELDS:
            _fail(f"Unknown --target {target!r}. Use 'outo' or 'subagent'.")
        if not session:
            _fail("--target requires --session NAME (the pin it writes).")
        if provider is None or model is None or reasoning is None:
            _fail(
                "--target writes the whole trio: pass --provider, --model and --reasoning "
                "(an empty --reasoning clears it)."
            )
        if provider and provider_store.get(provider) is None:
            _fail(f"Provider {provider!r} is not configured.")
        _persist_trio(session, target, provider, model, reasoning)
        trio = f"{provider or '-'}/{model or '-'}" + (f":{reasoning}" if reasoning else "")
        parts.append(f"{target} trio → {trio} (pin '{session}' + global default)")
    if theme is not None:
        settings_store.update(theme=theme)
        parts.append(f"theme → {theme}")
    if active_session is not None:
        settings_store.update(session=active_session)
        parts.append(f"active-session → {active_session}")
    console.print(f"[green]✓[/green] {', '.join(parts)}.")
