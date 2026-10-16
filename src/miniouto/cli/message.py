"""Cross-process message insertion: the CLI side of `core/inbox.py`.

A live session (a TUI, or a one-shot `chat` chain still following background
runs) is drained by its own process; anything external — a script, a human,
a detached `miniouto chat --bg` run — writes into the same session inbox from
here.
The inbox owns the file format; this module only chooses *what* to write.
"""

from __future__ import annotations

import json
import sys
from typing import Annotated

import typer
from rich.console import Console

from ..core import inbox
from ..storage import settings as settings_store

app = typer.Typer(help="Insert and inspect pending session messages.")
console = Console()

_PREVIEW_CHARS = 60


def _fail(message: str) -> typer.Exit:
    console.print(f"[red]✗[/red] {message}")
    return typer.Exit(code=1)


def _session_or_default(session: str | None) -> str:
    """`--session`, else `settings.session`; empty is an error, never a guess."""

    name = session or settings_store.load().session
    if not name:
        raise _fail(
            "No session: pass --session NAME, or set one with `miniouto chat --name NAME`."
        )
    return name


@app.command("insert")
def insert_cmd(
    text: Annotated[
        list[str] | None,
        typer.Argument(help="Message text. Words are joined with single spaces."),
    ] = None,
    session: str = typer.Option(..., "--session", help="Session whose inbox receives the message."),
    kind: str = typer.Option(
        inbox.USER_KIND, "--kind", help="alarm (system announcement) or user (interjection)."
    ),
    source: str = typer.Option("cli", "--source", help="Free-form origin label stored with the message."),
    stdin: bool = typer.Option(False, "--stdin", help="Read the text from stdin instead of TEXT."),
) -> None:
    """Insert one message into a session's inbox."""

    if stdin and text:
        raise _fail("Pass either TEXT or --stdin, not both.")
    if not stdin and not text:
        raise _fail("Pass message text, or --stdin to read it from standard input.")

    body = sys.stdin.read() if stdin else " ".join(text or [])
    if not body.strip():
        raise _fail("Message text must not be empty or whitespace-only.")

    try:
        message_id = inbox.insert(session, body, kind=kind, source=source)
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    console.print(message_id)


@app.command("list")
def list_cmd(
    session: str | None = typer.Option(None, "--session", help="Session to inspect. Defaults to settings.session."),
) -> None:
    """Show the messages still waiting in a session's inbox.

    Silent on an empty inbox (exit 0), so it doubles as a poll loop's emptiness
    test: `[ -n "$(miniouto message list --session S)" ]` needs no text match.
    """

    name = _session_or_default(session)
    messages = _peek(name)
    for message in messages:
        preview = " ".join(message["text"].split())[:_PREVIEW_CHARS]
        console.print(
            f"[dim]{message['ts']}[/dim]  {message['kind']}  {message['source']}  [dim]{preview}[/dim]"
        )


@app.command("clear")
def clear_cmd(
    session: str | None = typer.Option(None, "--session", help="Session to drain. Defaults to settings.session."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Discard every pending message of a session."""

    name = _session_or_default(session)
    count = inbox.pending_count(name)
    if count == 0:
        console.print("[dim]no pending messages[/dim]")
        return
    if not yes:
        typer.confirm(f"Discard {count} message(s) from session '{name}'?", abort=True)
    inbox.drain(name)
    console.print(f"[green]✓[/green] Discarded {count} message(s) from session '{name}'.")


def _peek(session: str) -> list[dict[str, str]]:
    """Read the inbox WITHOUT consuming it, oldest first.

    `inbox.drain` is the delivery primitive — it unlinks. A `list` must not
    deliver, so this reads the same payload keys out of the same directory and
    rewrites nothing; `insert` stays the only writer.
    """

    directory = inbox.inbox_dir(session)
    if not directory.is_dir():
        return []
    rows: list[dict[str, str]] = []
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows.append(
                {
                    "id": path.stem,
                    "kind": str(payload["kind"]),
                    "text": str(payload["text"]),
                    "ts": str(payload["ts"]),
                    "source": str(payload["source"]),
                }
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue  # corrupt content is the drainer's problem, never the lister's
    rows.sort(key=lambda row: row["id"])
    return rows

