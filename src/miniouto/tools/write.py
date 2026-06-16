"""Write tool: create a new file. Overwriting an existing file is rejected."""

from __future__ import annotations

from pathlib import Path

from ..paths_runtime import INVOCATION_CWD


def write(file_path: str, content: str, *, encoding: str = "utf-8") -> str:
    path = Path(file_path)
    if not path.is_absolute():
        path = (INVOCATION_CWD / path).resolve()

    if path.exists():
        raise WriteError(
            f"File already exists: {path}. Use the Edit tool to modify it. "
            "The Write tool only creates new files."
        )
    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding=encoding)
    return f"Created {path} ({len(content)} bytes)."


class WriteError(Exception):
    pass
