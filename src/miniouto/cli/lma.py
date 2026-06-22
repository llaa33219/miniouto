"""Browse the lma (llm-model-api) provider and model catalog."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from ..core import lma as lma_api
from ..core.providers import add_provider_from_lma, sdk_to_format
from ..storage import paths
from ..storage import providers as provider_store

app = typer.Typer(help="Browse the lma (llm-model-api) provider and model catalog.")
console = Console()


@app.command("providers")
def providers_cmd() -> None:
    """List every provider known to lma (https://lma.blp.sh)."""

    try:
        providers = lma_api.list_providers()
    except Exception as exc:
        console.print(f"[red]✗[/red] Failed to reach lma: {exc}")
        raise typer.Exit(code=1) from exc
    if not providers:
        console.print("[yellow]No providers returned by lma.[/yellow]")
        return

    table = Table(title=f"lma providers ({len(providers)})", show_header=True, header_style="bold")
    table.add_column("Name", style="cyan")
    table.add_column("SDK")
    table.add_column("API URL")
    table.add_column("miniouto format", style="magenta")
    table.add_column("Addable?", justify="center")
    for p in providers:
        name = p.get("name", "?")
        sdk = p.get("sdk") or "-"
        api = p.get("api") or "-"
        fmt, _ = sdk_to_format(sdk, api)
        fmt_str = fmt or "[red]unsupported[/red]"
        addable = "[green]✓[/green]" if fmt else "[dim]✗[/dim]"
        table.add_row(name, sdk, api, fmt_str, addable)
    console.print(table)


@app.command("models")
def models_cmd(
    provider_name: str = typer.Argument(
        ..., help="Provider name (fuzzy match, e.g. 'anthropic', 'open ai')."
    ),
) -> None:
    """List every model lma knows for a provider."""

    try:
        models = lma_api.list_models(provider_name)
    except Exception as exc:
        console.print(f"[red]✗[/red] Failed to reach lma: {exc}")
        raise typer.Exit(code=1) from exc
    if not models:
        console.print(f"[yellow]No models returned for {provider_name!r}.[/yellow]")
        raise typer.Exit(code=1)
    table = Table(
        title=f"lma models for {provider_name!r} ({len(models)})",
        show_header=True,
        header_style="bold",
    )
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    for m in models:
        table.add_row(m.get("id", "?"), m.get("name", "?"))
    console.print(table)


@app.command("add")
def add_cmd(
    provider_name: str = typer.Argument(
        ..., help="lma provider name (e.g. 'OpenAI', 'Anthropic', 'GitHub Copilot')."
    ),
    api_key: str = typer.Option(..., "--api-key", help="API key for the provider."),
    default_model: str = typer.Option(
        "",
        "--default-model",
        help="Default model id. If empty, the first model lma lists for this provider is used.",
    ),
) -> None:
    """Add an lma provider by name + API key (source='lma')."""

    lma_provider = lma_api.find_provider(provider_name)
    if lma_provider is None:
        console.print(
            f"[red]✗[/red] No lma provider matched {provider_name!r}. "
            "Run `miniouto lma providers` to see the catalog."
        )
        raise typer.Exit(code=1)

    name = lma_api.slugify(lma_provider["name"])
    sdk = lma_provider.get("sdk")
    api = lma_provider.get("api")

    try:
        provider = add_provider_from_lma(
            name=name,
            api_key=api_key,
            sdk=sdk,
            api=api,
            default_model=default_model,
        )
    except ValueError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not provider.default_model:
        try:
            models = lma_api.list_models(lma_provider["name"])
            if models:
                provider = add_provider_from_lma(
                    name=name,
                    api_key=api_key,
                    sdk=sdk,
                    api=api,
                    default_model=models[0].get("id", ""),
                )
        except Exception:
            pass

    paths.ensure_dirs()
    if provider_store.get(name) is not None:
        console.print(
            f"[yellow]![/yellow] Provider [bold]{name}[/bold] already exists; overwriting."
        )
    provider_store.upsert(provider)
    console.print(
        f"[green]✓[/green] Added lma provider [bold]{name}[/bold] "
        f"({provider.api_format}, default-model={provider.default_model or '-'}, "
        f"source=lma)."
    )
