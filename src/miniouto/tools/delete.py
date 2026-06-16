"""Delete tool: remove a file or empty directory."""

from __future__ import annotations

from pathlib import Path

from ..paths_runtime import INVOCATION_CWD


def delete(file_path: str) -> str:
    path = Path(file_path)
    if not path.is_absolute():
        path = (INVOCATION_CWD / path).resolve()
    if not path.exists():
        raise DeleteError(f"Path not found: {path}")
    if path.is_dir():
        if any(path.iterdir()):
            raise DeleteError(
                f"{path} is a non-empty directory. Delete its contents first, "
                "or use Bash with `rm -rf` if you really mean it."
            )
        path.rmdir()
        return f"Removed empty directory {path}."
    path.unlink()
    return f"Deleted {path}."


class DeleteError(Exception):
    pass
