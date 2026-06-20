"""Write tool: create a new file. Overwriting an existing file is rejected."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..paths_runtime import INVOCATION_CWD

# Beyond this many characters the Write tool refuses and redirects the model
# to use Bash with a heredoc / `tee` / redirected shell command instead. The
# limit is in characters (close enough to UTF-8 bytes for ASCII-heavy files)
# and is deliberately conservative: large inline content tends to be truncated
# at the model layer (max_tokens) or to bloat the conversation history, both
# of which produce a corrupt or partially-written file with no clear signal.
MAX_CONTENT_CHARS = 50_000


def write(file_path: str, content: str, *, encoding: str = "utf-8") -> str:
    if not file_path or not isinstance(file_path, str) or not file_path.strip():
        raise WriteError(
            f"file_path is required and must be a non-empty string, got "
            f"{file_path!r}. Re-emit the call with file_path set to the "
            "absolute target path (or a path relative to the directory "
            "miniouto was invoked from)."
        )
    path = Path(file_path)
    if not path.is_absolute():
        path = (INVOCATION_CWD / path).resolve()

    if path.exists():
        raise WriteError(
            f"File already exists: {path}. Use the Edit tool to modify it. "
            "The Write tool only creates new files."
        )
    if not isinstance(content, str):
        raise WriteError(
            f"content must be a string, got {type(content).__name__}. "
            "The Write tool only accepts inline text content."
        )
    if len(content) > MAX_CONTENT_CHARS:
        raise WriteError(
            f"content is {len(content)} characters, which exceeds the "
            f"{MAX_CONTENT_CHARS}-character cap of the Write tool. "
            "Large inline content is unsafe: the model may truncate it "
            "silently, producing a partial file. Generate the content with "
            "Bash instead (e.g. `python -c '...'` with an open()/write() "
            "call, or a shell heredoc / printf / seq loop) and then call "
            "the Write tool with no content beyond a verification step, or "
            "skip Write entirely and write the file directly with Bash."
        )

    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)

    _atomic_write_text(path, content, encoding=encoding)
    return f"Created {path} ({len(content)} chars)."


def _atomic_write_text(path: Path, content: str, *, encoding: str) -> None:
    """Write content to path atomically: write to a sibling temp file, then rename.

    `Path.write_text` opens the target directly and is not atomic — a crash
    or ENOSPC mid-write leaves a half-written file at the requested path.
    Writing to a sibling temp file in the same directory guarantees the
    rename is on the same filesystem, then `os.replace` swaps them in one
    syscall. On failure the target is untouched.
    """

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class WriteError(Exception):
    pass
