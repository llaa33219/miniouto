"""Provider subcommands."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from ..core.providers import SUPPORTED_FORMATS
from ..storage import paths
from ..storage import providers as provider_store
from ..storage import settings as settings_store

app = typer.Typer(help="Manage LLM providers.")
console = Console()

FORMAT_HELP = (
    "API compatibility: "
    "openai (OpenAI Chat Completions), "
    "openai-response (OpenAI Responses API), "
    "anthropic (Anthropic Messages), "
    "google (Google Generative AI / Gemini)."
)


@app.command("add")
def add(
    name: str = typer.Option(..., "--name", help="Provider identifier (e.g. openai, minimax)."),
    api_format: str = typer.Option("openai", "--format", help=FORMAT_HELP),
    base_url: str = typer.Option(
        "", "--base-url",
        help="Base URL of the endpoint. For OpenAI-compatible services "
        "(LocalAI, vLLM, MiniMax, Zhipu, Moonshot, etc.) use the OpenAI or "
        "openai-response format with the provider's base URL. Anthropic "
        "format works with the Anthropic SDK and any compatible proxy. "
        "Google format accepts an api_endpoint via client_options.",
    ),
    api_key: str = typer.Option("", "--api-key", help="API key (omit to read from env at call time)."),
    default_model: str = typer.Option(
        "", "--default-model", help="Default model used when chat --model is not given."
    ),
) -> None:
    """Add or update a provider."""

    if api_format not in SUPPORTED_FORMATS:
        console.print(
            f"[red]✗[/red] Unknown format {api_format!r}. "
            f"Supported: {', '.join(SUPPORTED_FORMATS)}."
        )
        raise typer.Exit(code=1)
    paths.ensure_dirs()
    provider = provider_store.Provider(
        name=name,
        api_format=api_format,
        base_url=base_url,
        api_key=api_key,
        default_model=default_model,
    )
    provider_store.upsert(provider)
    console.print(f"[green]✓[/green] Saved provider [bold]{name}[/bold] ({api_format}).")


@app.command("list")
def list_cmd() -> None:
    """List configured providers."""

    rows = provider_store.load_all()
    if not rows:
        console.print("[yellow]No providers configured.[/yellow] Run `miniouto provider add`.")
        return
    current = settings_store.load().provider
    table = Table(title="Providers", show_header=True, header_style="bold")
    table.add_column("Name", style="cyan")
    table.add_column("Format")
    table.add_column("Base URL")
    table.add_column("Default Model")
    table.add_column("Default", justify="center")
    for p in rows.values():
        marker = "[green]●[/green]" if p.name == current else ""
        table.add_row(p.name, p.api_format, p.base_url or "-", p.default_model or "-", marker)
    console.print(table)


@app.command("remove")
def remove(name: str) -> None:
    """Remove a provider by name."""

    if provider_store.remove(name):
        console.print(f"[green]✓[/green] Removed provider [bold]{name}[/bold].")
    else:
        console.print(f"[red]✗[/red] Provider [bold]{name}[/bold] does not exist.")
        raise typer.Exit(code=1)


@app.command("default")
def default(name: str) -> None:
    """Set the default provider for chat."""

    if provider_store.get(name) is None:
        console.print(f"[red]✗[/red] Provider [bold]{name}[/bold] is not configured.")
        raise typer.Exit(code=1)
    settings_store.update(provider=name)
    console.print(f"[green]✓[/green] Default provider is now [bold]{name}[/bold].")
