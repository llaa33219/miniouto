"""Skill CLI commands."""

from __future__ import annotations

import typer
from rich.console import Console

from ..storage import skills as skill_store
from ._yaml import print_yaml

app = typer.Typer(help="Manage agent skills.")
console = Console()


@app.command("list")
def list_cmd() -> None:
    """List all available skills, as YAML."""

    skills = [
        {
            "name": skill.name,
            "description": skill.description,
            "directory": str(skill.directory),
        }
        for skill in skill_store.list_skills()
    ]
    print_yaml({"skills": skills})


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
