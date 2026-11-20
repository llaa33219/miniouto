"""Provider subcommands.

Three groups:

* **catalog** browse / add (sourced from https://modelslist.blp.sh):
  `providers`,
  `models`, `add`.
* **storage** ops on already-configured providers: `list`, `remove`, `default`.
* **custom** manual config (no catalog lookup): `provider custom add`.
"""

from __future__ import annotations

from dataclasses import replace

import typer
from rich.console import Console

from ..core import context as context_api
from ..core import modelslist as catalog_api
from ..core.providers import (
    SUPPORTED_FORMATS,
    add_provider_from_catalog,
    catalog_compatibility,
    resolve_catalog_dialect,
)
from ..core.reasoning import default_reasoning_choice
from ..storage import paths
from ..storage import providers as provider_store
from ..storage import settings as settings_store
from ..storage.providers import is_catalog_source
from ._yaml import print_yaml

app = typer.Typer(help="Manage LLM providers (catalog browse + custom config).")
custom_app = typer.Typer(help="Manually configure a custom provider (advanced).")
app.add_typer(custom_app, name="custom")

console = Console()
# Listing commands print pure YAML on stdout, so their error text goes to a
# stderr console instead — a wrapper must never find markup in the YAML lane.
console_err = Console(stderr=True)

FORMAT_HELP = (
    "API compatibility: "
    "openai (OpenAI Chat Completions), "
    "openai-response (OpenAI Responses API), "
    "anthropic (Anthropic Messages), "
    "google (Google Generative AI / Gemini)."
)


# ─── catalog commands ────────────────────────────────────────────────────────


def _catalog_row(catalog_provider: dict) -> dict:
    """One `provider providers` entry: what miniouto can do with this provider."""

    selected = resolve_catalog_dialect(catalog_provider)
    options = catalog_compatibility(catalog_provider)
    return {
        "name": str(catalog_provider.get("name", "?")),
        "slug": str(catalog_provider.get("slug", "?")),
        "format": selected.api_format if selected else "",
        "base_url": selected.base_url if selected else "",
        "compatibility": [option.dialect for option in options],
        "addable": selected is not None,
    }


@app.command("providers")
def providers_cmd() -> None:
    """List every provider available in the catalog, as YAML."""

    try:
        providers = catalog_api.list_providers()
    except Exception as exc:
        console_err.print(f"[red]✗[/red] Failed to reach catalog: {exc}")
        raise typer.Exit(code=1) from exc

    print_yaml({"providers": [_catalog_row(p) for p in providers]})


@app.command("models")
def models_cmd(
    provider_name: str = typer.Argument(
        ..., help="Provider name (fuzzy match, e.g. 'anthropic', 'open ai')."
    ),
) -> None:
    """List every model the catalog knows for a provider, as YAML."""

    try:
        models = catalog_api.list_models(provider_name)
    except Exception as exc:
        console_err.print(f"[red]✗[/red] Failed to reach catalog: {exc}")
        raise typer.Exit(code=1) from exc
    if not models:
        console_err.print(f"[yellow]No models returned for {provider_name!r}.[/yellow]")
        raise typer.Exit(code=1)
    resolved = catalog_api.find_provider(provider_name)
    print_yaml(
        {
            "provider": str(resolved.get("name", provider_name)) if resolved else provider_name,
            "models": [
                {"id": str(m.get("id", "?")), "name": str(m.get("name", "?"))} for m in models
            ],
        }
    )


@app.command("add")
def add_cmd(
    provider_name: str = typer.Argument(
        ..., help="Catalog provider name or slug (e.g. 'Kimi', 'deepseek')."
    ),
    api_key: str = typer.Option(..., "--api-key", help="API key for the provider."),
    default_model: str = typer.Option(
        "",
        "--default-model",
        help="Default model id. If empty, the first model the catalog lists for this provider is used.",
    ),
    reasoning: str | None = typer.Option(
        None,
        "--reasoning",
        help="Reasoning preference stored on the provider (effort level / on / none). "
        "Default: auto-fill from the catalog's model metadata.",
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

    name = catalog_provider["slug"]

    try:
        provider = add_provider_from_catalog(
            catalog_provider,
            api_key=api_key,
            default_model=default_model,
            reasoning_effort=reasoning,
        )
    except ValueError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not provider.default_model:
        try:
            models = catalog_api.list_models(catalog_provider["name"])
            if models:
                provider = add_provider_from_catalog(
                    catalog_provider,
                    api_key=api_key,
                    default_model=models[0].get("id", ""),
                    reasoning_effort=reasoning,
                )
        except Exception:
            pass

    if provider.reasoning_effort is None and provider.default_model:
        # Best-effort auto-fill from modelslist's per-model reasoning metadata so
        # thinking works out of the box; silent when modelslist has no data.
        provider.reasoning_effort = default_reasoning_choice(
            provider.default_model, name
        )

    paths.ensure_dirs()
    if provider_store.get(name) is not None:
        console.print(
            f"[yellow]![/yellow] Provider [bold]{name}[/bold] already exists; overwriting."
        )
    provider_store.upsert(provider)
    console.print(
        f"[green]✓[/green] Added provider [bold]{name}[/bold] "
        f"({provider.api_format}, default-model={provider.default_model or '-'}, "
        f"reasoning={provider.reasoning_effort or '-'})."
    )


# ─── storage commands (unchanged behavior) ───────────────────────────────────


@app.command("list")
def list_cmd() -> None:
    """List configured providers, as YAML."""

    rows = provider_store.load_all()
    current = settings_store.load().provider
    print_yaml(
        {
            "providers": [
                {
                    "name": p.name,
                    "source": "custom" if p.source == "custom" else "catalog",
                    "format": p.api_format,
                    "compatibility": p.catalog_dialect
                    or ("catalog" if is_catalog_source(p.source) else "manual"),
                    "base_url": p.base_url,
                    "default_model": p.default_model,
                    "reasoning": p.reasoning_effort or "",
                    "default": p.name == current,
                }
                for p in rows.values()
            ]
        }
    )


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
    # `update(provider=...)` switches the provider AND the model it runs
    # as one pair — a stale model from the provider being left behind
    # would fail the next call with a model that provider never heard of.
    settings_store.update(provider=name)
    console.print(f"[green]✓[/green] Default provider is now [bold]{name}[/bold].")


@app.command("edit")
def edit_cmd(
    name: str = typer.Argument(..., help="Configured provider name."),
    default_model: str | None = typer.Option(
        None, "--default-model", help="Set the endpoint's default model."
    ),
    reasoning: str | None = typer.Option(
        None,
        "--reasoning",
        help="Reasoning preference stored on the provider (effort level / on / none); "
        "\"\" clears it to auto.",
    ),
    max_context_window: int | None = typer.Option(
        None, "--max-context-window", help="Context-window override; 0 clears (follow modelslist)."
    ),
    max_output_tokens: int | None = typer.Option(
        None, "--max-output-tokens", help="Output-token override; 0 clears (follow modelslist)."
    ),
    base_url: str | None = typer.Option(None, "--base-url", help="Endpoint base URL."),
    api_key: str | None = typer.Option(None, "--api-key", help="API key."),
    compatibility: str | None = typer.Option(
        None,
        "--compatibility",
        help="Catalog dialect to switch the endpoint to (catalog-source records only).",
    ),
) -> None:
    """Edit a stored provider record in place.

    Only the flags you pass are written; everything else keeps its stored
    value. `--compatibility` applies to catalog-source records only and
    rewrites the record's format and base URL from the catalog's mapping
    for that dialect (the same flow the TUI's Compatibility row runs).
    """

    record = provider_store.get(name)
    if record is None:
        console_err.print(f"[red]✗[/red] Provider [bold]{name}[/bold] is not configured.")
        raise typer.Exit(code=1)

    if compatibility is not None:
        if not is_catalog_source(record.source):
            console_err.print(
                f"[red]✗[/red] --compatibility applies to catalog providers only; "
                f"[bold]{name}[/bold] is custom — edit --base-url here, or re-add it "
                "with `provider custom add --format …` for a new format."
            )
            raise typer.Exit(code=1)
        catalog_provider = catalog_api.find_provider_by_slug(name, record.base_url)
        if catalog_provider is None:
            console_err.print(
                f"[red]✗[/red] No catalog metadata found for [bold]{name}[/bold] "
                "— cannot resolve the compatibility mapping."
            )
            raise typer.Exit(code=1)
        option = next(
            (
                candidate
                for candidate in catalog_compatibility(catalog_provider)
                if candidate.dialect == compatibility
            ),
            None,
        )
        if option is None:
            available = ", ".join(
                candidate.dialect for candidate in catalog_compatibility(catalog_provider)
            )
            console_err.print(
                f"[red]✗[/red] Catalog provider [bold]{name}[/bold] offers no "
                f"{compatibility!r} dialect. Available: {available or '(none)'}."
            )
            raise typer.Exit(code=1)
        record = replace(
            record,
            api_format=option.api_format,
            base_url=option.base_url,
            catalog_dialect=option.dialect,
        )

    updates: dict[str, object] = {}
    if default_model is not None:
        updates["default_model"] = default_model
    if reasoning is not None:
        updates["reasoning_effort"] = reasoning or None
    if max_context_window is not None:
        # 0 = clear the override (follow modelslist) — the TUI's empty answer.
        updates["max_context_window"] = max_context_window or None
    if max_output_tokens is not None:
        updates["max_output_tokens"] = max_output_tokens or None
    if base_url is not None:
        updates["base_url"] = base_url
    if api_key is not None:
        updates["api_key"] = api_key
    if updates:
        record = replace(record, **updates)

    provider_store.upsert(record)
    # The caps override is read from the record on every call, but the
    # modelslist FALLBACK it hands back to is cached for the life of the
    # process — drop it or this process serves pre-edit caps until exit
    # (the same call the TUI's caps row makes).
    if "max_context_window" in updates or "max_output_tokens" in updates:
        context_api.clear_model_cache()
    console.print(f"[green]✓[/green] Provider [bold]{name}[/bold] updated.")


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
    reasoning: str | None = typer.Option(
        None,
        "--reasoning",
        help="Reasoning preference stored on the provider (effort level / on / none).",
    ),
    max_context_window: int = typer.Option(
        0, "--max-context-window", help="Context-window override; 0 = follow modelslist."
    ),
    max_output_tokens: int = typer.Option(
        0, "--max-output-tokens", help="Output-token override; 0 = follow modelslist."
    ),
) -> None:
    """Manually add or update a custom provider.

    For catalog providers (Kimi, DeepSeek, etc.), use
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
        reasoning_effort=reasoning,
        max_context_window=max_context_window or None,
        max_output_tokens=max_output_tokens or None,
    )
    provider_store.upsert(provider)
    console.print(
        f"[green]✓[/green] Saved custom provider [bold]{name}[/bold] "
        f"({api_format}, reasoning={provider.reasoning_effort or '-'})."
    )
