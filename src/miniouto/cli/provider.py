"""Provider subcommands.

Three groups:

* **catalog** browse / add (sourced from the upstream lma catalog at
  https://lma.blp.sh, but exposed to users as the "catalog"): `providers`,
  `models`, `add`.
* **storage** ops on already-configured providers: `list`, `remove`, `default`.
* **custom** manual config (no catalog lookup): `provider custom add`.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from ..core import lma as catalog_api
from ..core.providers import SUPPORTED_FORMATS, add_provider_from_lma, sdk_to_format
from ..storage import paths
from ..storage import providers as provider_store
from ..storage import settings as settings_store

app = typer.Typer(help="Manage LLM providers (catalog browse + custom config).")
custom_app = typer.Typer(help="Manually configure a custom provider (advanced).")
app.add_typer(custom_app, name="custom")

console = Console()

FORMAT_HELP = (
    "API compatibility: "
    "openai (OpenAI Chat Completions), "
    "openai-response (OpenAI Responses API), "
    "anthropic (Anthropic Messages), "
    "google (Google Generative AI / Gemini)."
)


# ─── catalog commands ────────────────────────────────────────────────────────


@app.command("providers")
def providers_cmd() -> None:
    """List every provider available in the catalog."""

    try:
        providers = catalog_api.list_providers()
    except Exception as exc:
        console.print(f"[red]✗[/red] Failed to reach catalog: {exc}")
        raise typer.Exit(code=1) from exc
    if not providers:
        console.print("[yellow]No providers returned by the catalog.[/yellow]")
        return

    table = Table(
        title=f"Catalog providers ({len(providers)})",
        show_header=True,
        header_style="bold",
    )
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
    """List every model the catalog knows for a provider."""

    try:
        models = catalog_api.list_models(provider_name)
    except Exception as exc:
        console.print(f"[red]✗[/red] Failed to reach catalog: {exc}")
        raise typer.Exit(code=1) from exc
    if not models:
        console.print(f"[yellow]No models returned for {provider_name!r}.[/yellow]")
        raise typer.Exit(code=1)
    table = Table(
        title=f"Catalog models for {provider_name!r} ({len(models)})",
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
        ..., help="Catalog provider name (e.g. 'OpenAI', 'Anthropic', 'GitHub Copilot')."
    ),
    api_key: str = typer.Option(..., "--api-key", help="API key for the provider."),
    default_model: str = typer.Option(
        "",
        "--default-model",
        help="Default model id. If empty, the first model the catalog lists for this provider is used.",
    ),
) -> None:
    """Add a provider from the catalog by name + API key.

    For manual configuration (custom base URL / format), use
    `miniouto provider custom add` instead.
    """

    catalog_provider = catalog_api.find_provider(provider_name)
    if catalog_provider is None:
        console.print(
            f"[red]✗[/red] No catalog provider matched {provider_name!r}. "
            "Run `miniouto provider providers` to see the catalog."
        )
        raise typer.Exit(code=1)

    name = catalog_api.slugify(catalog_provider["name"])
    sdk = catalog_provider.get("sdk")
    api = catalog_provider.get("api")

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
            models = catalog_api.list_models(catalog_provider["name"])
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
        f"[green]✓[/green] Added provider [bold]{name}[/bold] "
        f"({provider.api_format}, default-model={provider.default_model or '-'})."
    )


# ─── storage commands (unchanged behavior) ───────────────────────────────────


@app.command("list")
def list_cmd() -> None:
    """List configured providers."""

    rows = provider_store.load_all()
    if not rows:
        console.print(
            "[yellow]No providers configured.[/yellow] "
            "Run `miniouto provider add <name>` or `miniouto provider custom add`."
        )
        return
    current = settings_store.load().provider
    table = Table(title="Providers", show_header=True, header_style="bold")
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Format")
    table.add_column("Base URL")
    table.add_column("Default Model")
    table.add_column("Default", justify="center")
    for p in rows.values():
        marker = "[green]●[/green]" if p.name == current else ""
        kind = "custom" if p.source == "custom" else "catalog"
        table.add_row(
            p.name,
            kind,
            p.api_format,
            p.base_url or "-",
            p.default_model or "-",
            marker,
        )
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


# ─── custom subcommands ──────────────────────────────────────────────────────


@custom_app.command("add")
def add_custom(
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
    """Manually add or update a custom provider.

    For catalog providers (OpenAI, Anthropic, etc.), use
    `miniouto provider add <name>` instead — it auto-fills base URL, format,
    and default model.
    """

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
    console.print(f"[green]✓[/green] Saved custom provider [bold]{name}[/bold] ({api_format}).")
