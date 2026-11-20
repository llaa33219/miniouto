"""Subagent provider/model/reasoning subcommands."""

from __future__ import annotations

from dataclasses import replace

import typer
from rich.console import Console

from ..storage import providers as provider_store
from ..storage import settings as settings_store
from ._yaml import print_yaml

app = typer.Typer(help="Manage the subagent's provider and model.")
console = Console()


def _effective_trio() -> tuple[str, str, str]:
    """The (provider, model, reasoning) a subagent run resolves right now.

    Per key, the same precedence `runtime._effective_subagent_pm` applies:
    `settings.subagent_*`, then (for the model) the subagent provider's own
    record `default_model`, then — only when the subagent inherits outo's
    provider — outo's own trio (`settings.provider` / `model` / `reasoning`,
    with the provider record's defaults answering where nothing names a
    model). An unresolved key stays `""`, which is the honest answer for a
    machine interface: no "-" placeholder and no display sentence.
    """

    s = settings_store.load()
    outo_record = provider_store.get(s.provider) if s.provider else None
    outo_model = s.model or (outo_record.default_model if outo_record else "")
    outo_reasoning = s.reasoning or (
        (outo_record.reasoning_effort or "") if outo_record else ""
    )

    provider = s.subagent_provider or s.provider
    record = provider_store.get(provider) if provider else None
    if s.subagent_provider:
        # A provider of its OWN: the model comes from the subagent's own
        # sources only — never outo's, which belongs to another endpoint.
        model = s.subagent_model or (record.default_model if record else "")
    else:
        # SAME AS OUTO: the subagent runs on outo's provider and model.
        model = s.subagent_model or outo_model
    reasoning = s.subagent_reasoning or (
        (record.reasoning_effort or "") if record else ""
    )
    if not s.subagent_provider and not s.subagent_reasoning:
        reasoning = reasoning or outo_reasoning
    return provider, model, reasoning


@app.command("show")
def show() -> None:
    """Show the subagent's effective and override provider/model/reasoning, as YAML."""

    s = settings_store.load()
    provider, model, reasoning = _effective_trio()
    print_yaml(
        {
            "provider": provider,
            "model": model,
            "reasoning": reasoning,
            "overrides": {
                "provider": s.subagent_provider,
                "model": s.subagent_model,
                "reasoning": s.subagent_reasoning,
            },
        }
    )


@app.command("set")
def set_cmd(
    provider: str | None = typer.Option(None, "--provider", help="Subagent provider name."),
    model: str | None = typer.Option(None, "--model", help="Subagent model name."),
    reasoning: str | None = typer.Option(
        None,
        "--reasoning",
        help="Subagent reasoning effort. Empty string clears the override.",
    ),
) -> None:
    """Persist a subagent provider, model, and/or reasoning override."""

    if provider is None and model is None and reasoning is None:
        console.print("[red]✗[/red] Pass at least one of --provider, --model, or --reasoning.")
        raise typer.Exit(code=1)

    updates: dict[str, str] = {}
    if provider is not None:
        if provider_store.get(provider) is None:
            console.print(f"[red]✗[/red] Provider [bold]{provider}[/bold] is not configured.")
            raise typer.Exit(code=1)
        updates["subagent_provider"] = provider
    if model is not None:
        updates["subagent_model"] = model
    if reasoning:
        updates["subagent_reasoning"] = reasoning
    if updates:
        settings_store.update(**updates)
    if reasoning == "":
        # update()/merge() skip empty strings, so clearing requires a direct
        # save of a replaced Settings (same pattern as `clear`).
        settings_store.save(replace(settings_store.load(), subagent_reasoning=""))

    console.print("[green]✓[/green] Subagent override saved.")
    if provider is not None and model is None:
        configured = provider_store.get(provider)
        if configured is not None and not (
            settings_store.load().subagent_model or configured.default_model
        ):
            console.print(
                f"[yellow]![/yellow] No model resolvable for [bold]{provider}[/bold] "
                "(no --model given and the provider has no default_model). "
                "Set one via `miniouto subagent set --model <name>`."
            )
    _, resolved_model, _ = _effective_trio()
    console.print(f"[bold]Subagent model:[/bold]    {resolved_model or '-'}")


@app.command("clear")
def clear() -> None:
    """Remove the subagent override (subagent inherits outo again)."""

    # update()/merge() skip empty strings, so clearing requires a direct
    # save of a replaced Settings (see the field comment in
    # storage/settings.py).
    settings_store.save(
        replace(
            settings_store.load(),
            subagent_provider="",
            subagent_model="",
            subagent_reasoning="",
        )
    )
    console.print(
        "[green]✓[/green] Subagent override cleared — subagent now inherits "
        "outo's provider/model and the provider's reasoning effort."
    )
