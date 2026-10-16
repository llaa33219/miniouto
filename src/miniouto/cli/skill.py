"""Skill CLI commands."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from ..storage import skills as skill_store

app = typer.Typer(help="Manage agent skills.")
console = Console()


@app.command("list")
def list_cmd() -> None:
    """List all available skills."""

    skills = skill_store.list_skills()
    if not skills:
        console.print(
            f"[yellow]No skills found.[/yellow] Check "
            f"{' or '.join(str(root) for root in skill_store.default_roots())}"
        )
        return

    table = Table(title="Available Skills", show_header=True, header_style="bold")
    table.add_column("Name", style="cyan")
    table.add_column("Description")
    table.add_column("Root", style="dim")

    for skill in skills:
        desc = skill.description[:80] + "..." if len(skill.description) > 80 else skill.description
        table.add_row(skill.name, desc, str(skill.directory))

    console.print(table)


@app.command("show")
def show(name: str) -> None:
    """Show skill content."""

    skill = skill_store.get_skill(name)
    if skill is None:
        console.print(f"[red]✗[/red] Skill [bold]{name}[/bold] not found.")
        raise typer.Exit(code=1)

    console.print(f"[bold]Name:[/bold] {skill.name}")
    console.print(f"[bold]Description:[/bold] {skill.description}")
    console.print(f"[bold]Location:[/bold] [dim]{skill.directory}/{skill.name}/[/dim]")
    if skill.license:
        console.print(f"[bold]License:[/bold] {skill.license}")
    if skill.allowed_tools:
        console.print(f"[bold]Allowed Tools:[/bold] {skill.allowed_tools}")
    console.print()
    console.print(skill.content)
