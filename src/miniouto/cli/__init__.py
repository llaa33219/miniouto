"""miniouto CLI entrypoint."""

from __future__ import annotations

import typer
from rich.console import Console

from ..storage import paths
from . import lma as lma_module
from . import provider as provider_module
from . import skill as skill_module
from . import style as style_module
from . import tui as tui_module
from .chat import chat_cmd

app = typer.Typer(
    name="miniouto",
    help="A minimal, file-driven CLI agent harness built on coreouto.",
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
)
app.add_typer(provider_module.app, name="provider")
app.add_typer(style_module.app, name="style")
app.add_typer(skill_module.app, name="skill")
app.add_typer(lma_module.app, name="lma")
app.command("chat", help="Run a single chat turn.")(chat_cmd)

console = Console()


@app.callback()
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show version and exit."),
) -> None:
    paths.ensure_dirs()
    if version:
        from .. import __version__

        console.print(f"miniouto {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        tui_module.run_tui()


@app.command("status")
def status() -> None:
    """Show current configuration."""

    from ..storage import providers as provider_store
    from ..storage import sessions as session_store
    from ..storage import settings as settings_store
    from ..storage import skills as skill_store
    from ..storage import styles as style_store

    s = settings_store.load()
    active_provider = provider_store.get(s.provider) if s.provider else None
    default_model = active_provider.default_model if active_provider else ""
    console.print(f"[bold]Default provider:[/bold] {s.provider or '-'}")
    console.print(f"[bold]Default model:[/bold]    {default_model or '- (use chat --model)'}")
    console.print(f"[bold]Active style:[/bold]    {s.style or '-'}")
    console.print(f"[bold]Session:[/bold]         {s.session or '-'}")
    console.print(f"[bold]Storage:[/bold]         {paths.ROOT}")
    console.print(f"[bold]Providers:[/bold]       {', '.join(provider_store.load_all()) or '-'}")
    console.print(f"[bold]Styles:[/bold]          {', '.join(style_store.list_styles()) or '-'}")
    console.print(f"[bold]Skills:[/bold]          {', '.join(s.name for s in skill_store.list_skills()) or '-'}")
    console.print(f"[bold]Sessions:[/bold]        {', '.join(session_store.list_sessions()) or '-'}")


if __name__ == "__main__":
    app()
