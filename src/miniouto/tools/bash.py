"""Bash tool: run a shell command, capture output, truncate if huge."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import threading
import time

from ..paths_runtime import INVOCATION_CWD

MAX_OUTPUT_BYTES = 30_000

# Hard cap on a single Bash invocation. A wedged subprocess (NFS hang,
# forgotten `sleep infinity`, a build stuck on a lock) would otherwise pin
# the whole agent loop forever — there is no other timeout around tool
# execution. On expiry the whole process group is killed and the tool
# returns an error, which wakes the loop: the model sees the failure and
# decides how to proceed.
BASH_TIMEOUT_SECONDS = 3600  # 1 hour

# Grace period for the captured pipes after the shell itself exits. Output
# collection waits for pipe EOF, and pipe fds are inherited across
# fork/exec — so a background child (`server &` without a redirect, a
# sloppy daemon) keeps stdout/stderr open forever even though the shell is
# long gone, and the tool call would hang until the 1-hour cap. After this
# grace the surviving children are killed so the pipes reach EOF and the
# call can return with the output captured so far.
PIPE_GRACE_SECONDS = 5.0

TRUNCATION_NOTE = (
    "\n\n<NOTE>Output was truncated to {max} bytes. "
    "If you need more, narrow the command (e.g. pipe to `head`, `grep`, or write to a file).</NOTE>"
)

# Per-turn cancel slot, set by core.runtime.build_runtime. The TUI's
# ESC ESC cancel must be able to kill an in-flight process promptly —
# the loop-level cancel guard only fires between steps. (Bash does have a
# 1-hour hard cap — BASH_TIMEOUT_SECONDS above — but that is far too slow
# to serve as the user-facing stop mechanism.)
_CANCEL_EVENT: threading.Event | None = None


def set_cancel_event(event: threading.Event | None) -> None:
    global _CANCEL_EVENT
    _CANCEL_EVENT = event


async def bash(
    command: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    if not command.strip():
        raise BashError("Empty command.")

    workdir = cwd or str(INVOCATION_CWD)
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            # stdin must NOT be inherited: in TUI mode Textual owns the tty,
            # and a command that reads stdin (`cat` with no args, `sudo`, an
            # ssh host-key prompt) would block forever on input that can
            # never arrive. DEVNULL makes such reads fail fast instead.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env={**os.environ, **(env or {})},
            # Own process group so timeout/cancel can SIGKILL the children
            # too — killing only the shell orphans a running child, which
            # then keeps the pipes open and hangs the readers below.
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise BashError(f"Failed to spawn shell: {exc}") from exc

    out_parts: list[bytes] = []
    err_parts: list[bytes] = []
    readers = [
        asyncio.ensure_future(_drain(proc.stdout, out_parts)),
        asyncio.ensure_future(_drain(proc.stderr, err_parts)),
    ]

    kill_reason: str | None = None
    grace_deadline: float | None = None
    while True:
        if _CANCEL_EVENT is not None and _CANCEL_EVENT.is_set():
            kill_reason = "cancelled"
            break
        now = time.monotonic()
        if now - start > BASH_TIMEOUT_SECONDS:
            kill_reason = "timeout"
            break
        if proc.returncode is not None:
            # The shell itself exited. If the pipes reached EOF too, the
            # call is done; otherwise a surviving child is holding a pipe
            # open — give it a short grace, then kill the group.
            if all(r.done() for r in readers):
                break
            if grace_deadline is None:
                grace_deadline = now + PIPE_GRACE_SECONDS
            elif now > grace_deadline:
                kill_reason = "orphans"
                break
        await asyncio.sleep(0.1)

    if kill_reason is not None:
        _kill_process_group(proc)
    # Killing the group closes every pipe writer, so the readers reach EOF
    # even when a background child was holding them.
    await asyncio.gather(*readers, return_exceptions=True)
    if proc.returncode is None:
        await proc.wait()

    elapsed = time.monotonic() - start
    out = b"".join(out_parts).decode("utf-8", errors="replace")
    err = b"".join(err_parts).decode("utf-8", errors="replace")
    rc = proc.returncode or 0

    if kill_reason == "cancelled":
        raise BashError(f"Command cancelled by user (esc esc) after {elapsed:.1f}s.")
    if kill_reason == "timeout":
        partial = _truncate(_format_output(out, err, rc, elapsed), MAX_OUTPUT_BYTES)
        raise BashError(
            f"Command timed out after {BASH_TIMEOUT_SECONDS}s; the process "
            "group was killed."
            + (f"\nPartial output before the kill:\n{partial}" if partial.strip() else "")
            + "\nDo not retry the same command unchanged — split it into "
            "smaller steps or run it in the background with output "
            "redirected (nohup … > /tmp/out.log 2>&1 &) and poll the log "
            "file."
        )

    combined = _format_output(out, err, rc, elapsed)
    if kill_reason == "orphans":
        combined += (
            "\n\n<NOTE>The shell exited, but background child processes kept "
            "stdout/stderr open, so they were killed after a short grace "
            "period to let this call return. To keep a background process "
            "alive, redirect its output away from the terminal "
            "(nohup … > /tmp/out.log 2>&1 &) and poll the log file.</NOTE>"
        )
    return _truncate(combined, MAX_OUTPUT_BYTES)


async def _drain(stream: asyncio.StreamReader, parts: list[bytes]) -> None:
    while chunk := await stream.read(65536):
        parts.append(chunk)


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    # The shell was started with start_new_session=True, so its pid is the
    # process-group id and SIGKILL reaches every child it spawned.
    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    with contextlib.suppress(ProcessLookupError):
        proc.kill()  # already dead → nothing left to kill


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
