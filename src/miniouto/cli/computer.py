"""`miniouto computer show` — render a capture the Computer tool saved.

The Computer tool's `screen` action dumps one `.art` file per capture under
`~/.miniouto/computer-screens/`; `tools/ascii_art.py` owns the format and this
module only reads it. Default output is the art body verbatim (truecolour
half-blocks), `--plain` the SGR-free ramp for logs and pipes, `--cols` a
re-quantisation to a narrower terminal, `--head` the top rows only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from ..storage.paths import COMPUTER_SCREENS_DIR
from ..tools.ascii_art import ArtFile, art_to_plain, parse_art

app = typer.Typer(help="Inspect screen art captured by the Computer tool.")
console = Console()


@app.callback()
def _group() -> None:
    """Screen-art commands.

    A Typer app holding exactly one command and no callback collapses into
    THAT command, which would make `miniouto computer` itself the renderer and
    eat the literal word `show` as a PATH. The callback pins the group shape.
    """


def _fail(message: str) -> typer.Exit:
    console.print(f"[red]✗[/red] {message}")
    return typer.Exit(code=1)


def _newest_art() -> Path:
    """The most recent capture; an error, never a guess, when there is none."""
    captures = sorted(COMPUTER_SCREENS_DIR.glob("*.art"), key=lambda p: p.stat().st_mtime)
    if not captures:
        raise _fail("no screen art yet — run the Computer tool's screen action first")
    return captures[-1]


def _load(path: Path) -> ArtFile:
    if not path.is_file():
        raise _fail(f"no such screen art: {path}")
    try:
        return parse_art(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _fail(f"{path}: {exc}") from exc


def _header(art: ArtFile, requantised: int | None) -> str:
    """One dim line naming the capture; `requantised` notes a --cols render."""
    fields = " ".join(
        f"{key}={art.meta.get(key, '-')}" for key in ("screen", "source", "grid", "mode")
    )
    if requantised is None:
        return fields
    return f"{fields} (re-quantised to {requantised} cols as plain ramp)"


@app.command("show")
def show_cmd(
    path: Annotated[
        Path | None,
        typer.Argument(help="`.art` file to render. Defaults to the newest capture."),
    ] = None,
    plain: bool = typer.Option(False, "--plain", help="Monochrome ramp, no colour escapes."),
    cols: int | None = typer.Option(
        None, "--cols", min=1, help="Re-quantise to N columns (implies --plain)."
    ),
    head: int | None = typer.Option(None, "--head", min=1, help="Print only the first N rows."),
) -> None:
    """Render one saved screen capture."""

    art = _load(path or _newest_art())
    # Full colour survives only at the recorded grid: a --cols render goes
    # through the ramp, because re-quantising means re-sampling cell colours.
    requantised = cols if plain is False else None
    body = art_to_plain(art, cols=cols) if plain or cols is not None else art.body
    console.print(f"[dim]{_header(art, requantised)}[/dim]", soft_wrap=True)
    rows = body.split("\n")
    for row in rows[: head or len(rows)]:
        print(row)
