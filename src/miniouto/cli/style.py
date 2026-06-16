"""Style subcommands."""

from __future__ import annotations

import typer
from rich.console import Console

from ..storage import paths
from ..storage import settings as settings_store
from ..storage import styles as style_store

app = typer.Typer(help="Manage agent style documents.")
console = Console()


@app.command("list")
def list_cmd() -> None:
    """List installed style documents."""

    names = style_store.list_styles()
    if not names:
        console.print("[yellow]No styles installed.[/yellow]")
        return
    current = settings_store.load().style
    for name in names:
        marker = " [green]●[/green]" if name == current else ""
        console.print(f"  - {name}{marker}")


@app.command("set")
def set_cmd(name: str) -> None:
    """Activate an installed style as the default."""

    if style_store.read(name) is None:
        console.print(f"[red]✗[/red] Style [bold]{name}[/bold] is not installed.")
        raise typer.Exit(code=1)
    settings_store.update(style=name)
    console.print(f"[green]✓[/green] Active style is now [bold]{name}[/bold].")


@app.command("add")
def add(
    repo_url: str = typer.Argument(..., help="Git host URL whose /style-md/ directory defines styles."),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Override style name (otherwise the file's basename is used).",
    ),
) -> None:
    """Add styles by fetching /style-md/ from a remote repository."""

    paths.ensure_dirs()
    try:
        added = style_store.add_from_repo(repo_url, name_override=name)
    except Exception as exc:
        console.print(f"[red]✗[/red] Failed to fetch styles: {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]✓[/green] Added/updated styles: {', '.join(added)}")


@app.command("show")
def show(name: str) -> None:
    """Print the contents of a style document."""

    content = style_store.read(name)
    if content is None:
        console.print(f"[red]✗[/red] Style [bold]{name}[/bold] is not installed.")
        raise typer.Exit(code=1)
    console.print(content)
