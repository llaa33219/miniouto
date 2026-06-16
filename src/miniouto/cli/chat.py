"""Single-shot chat command."""

from __future__ import annotations

import typer
from rich.console import Console

from ..core.chat import ChatOptions, run_chat
from ..storage import settings as settings_store

console = Console()


def chat_cmd(
    prompt: str = typer.Argument(..., help="Prompt to send to the agent."),
    name: str | None = typer.Option(None, "--name", help="Session name (default: from settings)."),
    provider: str | None = typer.Option(None, "--provider", help="Override the active provider."),
    model: str | None = typer.Option(None, "--model", help="Override the default model."),
    style: str | None = typer.Option(None, "--style", help="Override the active style."),
    max_tokens: int | None = typer.Option(None, "--max-tokens", help="Cap output tokens."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    continue_session: bool = typer.Option(
        False, "--continue", "-c", help="Prepend the session's previous history."
    ),
) -> None:
    """Run one prompt and print the agent's reply."""

    session_name = name or settings_store.load().session or "default"
    if name is not None or continue_session:
        settings_store.update(session=session_name)

    opts = ChatOptions(
        prompt=prompt,
        session=session_name,
        provider=provider,
        model=model,
        style=style,
        max_tokens=max_tokens,
        temperature=temperature,
        continue_session=continue_session,
    )
    try:
        reply = run_chat(opts)
    except Exception as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(reply)
