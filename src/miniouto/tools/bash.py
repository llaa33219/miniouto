"""Bash tool: run a shell command, capture output, truncate if huge."""

from __future__ import annotations

import asyncio
import os
import time

from ..paths_runtime import INVOCATION_CWD

MAX_OUTPUT_BYTES = 30_000
TRUNCATION_NOTE = (
    "\n\n<NOTE>Output was truncated to {max} bytes. "
    "If you need more, narrow the command (e.g. pipe to `head`, `grep`, or write to a file).</NOTE>"
)
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 600


async def bash(
    command: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    if not command.strip():
        raise BashError("Empty command.")
    timeout_seconds = max(1, min(timeout_seconds, MAX_TIMEOUT_SECONDS))

    workdir = cwd or str(INVOCATION_CWD)
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError as exc:
        raise BashError(f"Failed to spawn shell: {exc}") from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        elapsed = time.monotonic() - start
        raise BashError(
            f"Command exceeded timeout of {timeout_seconds}s (ran for {elapsed:.1f}s) and was killed. "
            f"If you need longer, raise `timeout_seconds` (max {MAX_TIMEOUT_SECONDS})."
        ) from exc

    elapsed = time.monotonic() - start
    out = stdout_b.decode("utf-8", errors="replace")
    err = stderr_b.decode("utf-8", errors="replace")
    rc = proc.returncode or 0
    combined = _format_output(out, err, rc, elapsed)
    return _truncate(combined, MAX_OUTPUT_BYTES)


def _format_output(stdout: str, stderr: str, returncode: int, elapsed: float) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(stdout.rstrip("\n"))
    if stderr:
        parts.append("[stderr]\n" + stderr.rstrip("\n"))
    parts.append(f"\n[exit {returncode} in {elapsed:.2f}s, cwd={INVOCATION_CWD}]")
    return "\n".join(parts)


def _truncate(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
    return truncated + TRUNCATION_NOTE.format(max=max_bytes)


class BashError(Exception):
    pass
