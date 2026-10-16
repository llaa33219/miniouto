"""miniouto CLI entrypoint."""

from __future__ import annotations

import sys

import typer
from rich.console import Console

from .. import paths_runtime
from ..storage import paths
from . import computer as computer_module
from . import message as message_module
from . import provider as provider_module
from . import skill as skill_module
from . import style as style_module
from . import subagent as subagent_module
from . import tui as tui_module
from .chat import chat_cmd

# `--return` is the ONE flag whose value is optional: `--return` alone means
# "the default path". Every other option demands its value, so that a command
# line typed into a Bash tool is idempotent — it never reads ambient state
# (`settings.session`, the default style) to fill in what was left out.
_OPTIONAL_VALUE_FLAGS = frozenset({"--return"})


def _expand_bare_value_flags(argv: list[str]) -> list[str]:
    """Rewrite a bare `--return` to click's `--flag=` spelling.

    Click treats `--flag <value>` as demanding that value, so the bare form
    would be a usage error, and typer drops click's own `flag_value` support.
    Only a following token starting with `-` (or nothing at all) counts as "no
    value", so an ordinary path or session name is never swallowed.
    """

    expanded: list[str] = []
    for index, token in enumerate(argv):
        following = argv[index + 1] if index + 1 < len(argv) else ""
        if token in _OPTIONAL_VALUE_FLAGS and (not following or following.startswith("-")):
            token = f"{token}="
        expanded.append(token)
    return expanded


class _App(typer.Typer):
    """The root app, with the optional-value flag grammar applied on entry.

    `miniouto` and `python -m miniouto` both go through `__call__`, so this is
    the one seam that sees argv before click parses it.
    """

    def __call__(self, *args: object, **kwargs: object) -> object:
        if args:
            return super().__call__(*(_expand_bare_value_flags(list(args[0])), *args[1:]), **kwargs)
        return super().__call__(_expand_bare_value_flags(sys.argv[1:]), **kwargs)


app = _App(
    name="miniouto",
    help="A minimal, file-driven CLI agent harness built on coreouto.",
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
)
app.add_typer(provider_module.app, name="provider")
app.add_typer(style_module.app, name="style")
app.add_typer(skill_module.app, name="skill")
app.add_typer(subagent_module.app, name="subagent")
app.add_typer(message_module.app, name="message")
app.add_typer(computer_module.app, name="computer")
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
    if s.subagent_provider or s.subagent_model:
        sub_name = s.subagent_provider or s.provider
        sub = provider_store.get(sub_name) if sub_name else None
        sub_model = s.subagent_model or (sub.default_model if sub else "") or "-"
        console.print(f"[bold]Subagent:[/bold]        {sub_name or '-'}/{sub_model}")
    else:
        console.print(f"[bold]Subagent:[/bold]        same as outo ({s.provider or '-'}/{default_model or '-'})")
    console.print(f"[bold]Active style:[/bold]    {s.style or '-'}")
    console.print(f"[bold]Session:[/bold]         {s.session or '-'}")
    console.print(f"[bold]Storage:[/bold]         {paths.ROOT}")
    console.print(f"[bold]Providers:[/bold]       {', '.join(provider_store.load_all()) or '-'}")
    console.print(f"[bold]Styles:[/bold]          {', '.join(style_store.list_styles()) or '-'}")
    console.print(f"[bold]Skills:[/bold]          {', '.join(s.name for s in skill_store.list_skills()) or '-'}")
    here = str(paths_runtime.INVOCATION_CWD)
    here_names: list[str] = []
    other_names: list[str] = []
    for name, cwd, _mtime in session_store.list_sessions_with_meta():
        (here_names if cwd == here else other_names).append(name)
    console.print(f"[bold]Sessions (this directory):[/bold] {', '.join(here_names) or '-'}")
    console.print(f"[bold]Sessions (other):[/bold]       {', '.join(other_names) or '-'}")



if __name__ == "__main__":
    app()
