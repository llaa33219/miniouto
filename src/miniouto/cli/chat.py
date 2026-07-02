"""Single-shot chat command."""

from __future__ import annotations

import datetime
import sys
import uuid

import typer

from ..core.chat import ChatOptions, run_chat
from ..core.events import ConsoleEventSink
from ..storage import settings as settings_store


def chat_cmd(
    prompt: str = typer.Argument(..., help="Prompt to send to the agent."),
    name: str | None = typer.Option(
        None, "--name", help="Session name. Without --name and --continue, a fresh session is generated each call."
    ),
    provider: str | None = typer.Option(None, "--provider", help="Override the active provider."),
    model: str | None = typer.Option(None, "--model", help="Override the default model."),
    style: str | None = typer.Option(None, "--style", help="Override the active style."),
    max_tokens: int | None = typer.Option(None, "--max-tokens", help="Cap output tokens."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    continue_session: bool = typer.Option(
        False, "--continue", "-c", help="Prepend the session's previous history."
    ),
    answer_only: bool = typer.Option(
        False, "--answer-only", "-a",
        help="Print only the final answer. Suppresses the session marker, loop events, and finish marker.",
    ),
    with_session: bool = typer.Option(
        False, "--with-session",
        help="Print only the session marker and the final answer. Suppresses loop events and the finish marker.",
    ),
) -> None:
    """Run one prompt and print the agent's reply."""

    if answer_only and with_session:
        typer.secho(
            "✗ --answer-only and --with-session are mutually exclusive.", err=True, fg="red"
        )
        raise typer.Exit(1)

    if continue_session:
        session_name = name or settings_store.load().session or "default"
    elif name is not None:
        session_name = name
    else:
        # Fresh session each call: timestamp + short UUID keeps names
        # unique even within the same second and sortable by recency,
        # so the previous chat's `settings.session` doesn't bleed in.
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = uuid.uuid4().hex[:6]
        session_name = f"chat-{ts}-{suffix}"
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
    # The sink handles all output: braille spinner + loop events share
    # stdout (Rich's Live display owns one channel and keeps them
    # vertically separated). The session marker is printed up front so
    # callers know which session the output belongs to.
    quiet = answer_only or with_session
    if not answer_only:
        sys.stdout.write(f"------{session_name}------\n")
        sys.stdout.flush()
    run_chat(opts, sink=ConsoleEventSink(quiet=quiet))
