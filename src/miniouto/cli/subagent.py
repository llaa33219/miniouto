"""Subagent provider/model/reasoning subcommands."""

from __future__ import annotations

from dataclasses import replace

import typer
from rich.console import Console

from ..storage import providers as provider_store
from ..storage import settings as settings_store

app = typer.Typer(help="Manage the subagent's provider and model.")
console = Console()


def _resolved() -> tuple[str, str]:
    """Return (provider, model) the subagent would run with right now.

    Mirrors the runtime precedence (settings value, then the subagent
    provider's default_model) and falls back to "same as outo
    (<provider>/<model>)" when no subagent override is set.
    """

    s = settings_store.load()
    outo_provider = provider_store.get(s.provider) if s.provider else None
    outo_model = outo_provider.default_model if outo_provider else ""

    if s.subagent_provider or s.subagent_model:
        provider_name = s.subagent_provider or s.provider
        sub_provider = provider_store.get(provider_name) if provider_name else None
        model = s.subagent_model or (sub_provider.default_model if sub_provider else "")
        return provider_name or "-", model or "- (no model resolved)"
    return (
        s.provider or "-",
        f"same as outo ({s.provider or '-'}/{outo_model or '-'})",
    )


def _resolved_reasoning() -> str:
    """Return the reasoning effort the subagent would run with right now.

    Mirrors the runtime precedence: settings.subagent_reasoning, then the
    resolved subagent provider's reasoning_effort, else "auto".
    """

    s = settings_store.load()
    if s.subagent_reasoning:
        return s.subagent_reasoning
    provider_name = s.subagent_provider or s.provider
    sub_provider = provider_store.get(provider_name) if provider_name else None
    if sub_provider is not None and sub_provider.reasoning_effort:
        return sub_provider.reasoning_effort
    return "auto"


@app.command("show")
def show() -> None:
    """Show the subagent's resolved provider, model, and reasoning."""

    provider, model = _resolved()
    console.print(f"[bold]Subagent provider:[/bold] {provider}")
    console.print(f"[bold]Subagent model:[/bold]    {model}")
    console.print(f"[bold]Subagent reasoning:[/bold] {_resolved_reasoning()}")


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
    _, resolved_model = _resolved()
    console.print(f"[bold]Subagent model:[/bold]    {resolved_model}")


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
