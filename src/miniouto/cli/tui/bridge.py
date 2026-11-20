"""The subprocess lane between the TUI and the ``miniouto`` CLI.

The TUI is the OFFICIAL example of a third-party CLI wrapper: it owns the
screen (xnano canvas, rows, modals) and talks to the agent harness the way
any external program must — by running ``python -m miniouto …`` and parsing
stdout / the files the CLI writes. This module is the whole subprocess
surface, so every rule lives in one place:

* **D7 — never the global ``miniouto`` on PATH.** Every argv starts with
  ``[sys.executable, "-m", "miniouto", …]``: THIS interpreter's miniouto,
  never a possibly-stale installed snapshot. The env (``MINIOUTO_HOME``) and
  the cwd are inherited, so a child sees exactly the storage the TUI sees.
* **The TUI writes nothing to storage itself.** Mutations (config, providers,
  styles, the active-session pointer, inbox inserts) are CLI commands; the
  reads that remain in-process are display reads of CLI-produced artifacts
  (session JSON, providers.toml, style listings).
* **Machine output is YAML or one id.** Listing commands print YAML through
  ``cli/_yaml.py``; ``message insert`` prints the message id. Errors go to
  stderr with a non-zero exit, so a wrapper never finds prose in the data
  lane.

Tests stub THIS module (or ``chat_argv``) rather than the world: it is the
one seam between the state machine and the operating system.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "asyncio_create_subprocess_exec",
    "chat_argv",
    "child_env",
    "cli_argv",
    "config_seed",
    "config_set_active_session",
    "config_set_theme",
    "config_set_trio",
    "config_show",
    "list_catalog_models",
    "list_catalog_providers",
    "message_insert",
    "message_list",
    "provider_add",
    "provider_custom_add",
    "provider_edit_caps",
    "provider_edit_compatibility",
    "style_set",
    "wait_for_children",
]


# ─── the one spawn rule (D7) ────────────────────────────────────────────────


def cli_argv(*args: str) -> list[str]:
    """The argv of one miniouto CLI invocation — always THIS interpreter."""

    return [sys.executable, "-m", "miniouto", *args]


def child_env() -> dict[str, str]:
    """The child's environment: inherited, with rich kept from wrapping.

    ``COLUMNS`` only affects the rich-formatted progress lines the CLI
    prints; keeping them unwrapped is what lets a wrapper that reads stdout
    treat each of them as exactly one line. Token deltas are raw writes and
    are unaffected.
    """

    env = dict(os.environ)
    env.setdefault("COLUMNS", "4096")
    return env


async def asyncio_create_subprocess_exec(*argv: str, **kwargs: Any) -> Any:
    """``asyncio.create_subprocess_exec`` — a seam so the tests own the spawn.

    The real function runs the CLI child; a test replaces it with a double
    that speaks the same stdout contract without a process. Living on the
    bridge means one patch target for every spawn the TUI makes.
    """

    import asyncio

    return await asyncio.create_subprocess_exec(*argv, **kwargs)


# In-flight subprocess count: a caller that tears the TUI down (the test
# isolation, an embedding app) can wait for the children it started instead of
# racing them — a cancelled worker's thread keeps its `subprocess.run` alive to
# completion, and what that child writes must not land in the NEXT test.
_INFLIGHT = threading.Semaphore(0)
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_COUNT = 0


def _inflight_enter() -> None:
    global _INFLIGHT_COUNT
    with _INFLIGHT_LOCK:
        _INFLIGHT_COUNT += 1


def _inflight_leave() -> None:
    global _INFLIGHT_COUNT
    with _INFLIGHT_LOCK:
        _INFLIGHT_COUNT -= 1
    _INFLIGHT.release()


def wait_for_children(timeout: float = 30.0) -> bool:
    """Wait until no CLI child of this process is still running.

    Returns False on timeout. The wait is COUNT-based, not permit-based: a
    timed-out wait used to leave the semaphore permits it did acquire, so a
    LATER wait could return True immediately while children were still
    running. The count is re-read at the end and any permits the wait
    consumed are released back, so the semaphore stays a pure signal.
    """

    deadline = time.monotonic() + timeout
    while True:
        with _INFLIGHT_LOCK:
            outstanding = _INFLIGHT_COUNT
        if outstanding <= 0:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if _INFLIGHT.acquire(timeout=min(max(0.01, timeout / 10.0), remaining)):
            _INFLIGHT.release()  # the count, not the permit, is the truth
        with _INFLIGHT_LOCK:
            if _INFLIGHT_COUNT <= 0:
                return True


def run_sync(args: list[str], *, timeout: float | None = 60.0) -> tuple[int, str, str]:
    """Run one CLI command to completion; ``(rc, stdout, stderr)``.

    Never raises on a non-zero exit — the caller decides what a failure
    means. A timeout kills the child and reports it through the return.
    """

    _inflight_enter()
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=child_env(),
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(args[:6])}"
    except OSError as exc:
        # A spawn failure (FileNotFoundError, ENOMEM, …) is a FAILED RUN,
        # not an exception: the worker tasks that call this (`config set`,
        # the session publish) would otherwise die with an unretrieved
        # exception and the write silently never happens.
        return 127, "", f"could not start {' '.join(args[:6])}: {exc}"
    finally:
        _inflight_leave()
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def run_yaml(args: list[str]) -> dict[str, Any] | None:
    """One YAML-listing command parsed, or ``None`` when it failed."""

    code, out, _err = run_sync(args)
    if code != 0:
        return None
    try:
        data = yaml.safe_load(out)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


# ─── config (the trio lane, invariant 12) ───────────────────────────────────

# ``config show`` is a READ the chips and the popup make several times per
# interaction (open, refresh, the reasoning target's own read), and each call
# is a ~0.5 s child. The answer is memoized PER SESSION against the STAT of
# the two files it is a function of — the session's pin TOML and the global
# settings TOML — so the memo is correct no matter WHO changed them (this
# TUI, another process, a test): any write bumps an mtime and the next read
# spawns a fresh child. One repaint therefore costs one child, not one per
# row, and a stale answer is impossible.
_SHOW_CACHE: dict[str, tuple[tuple[int, int], tuple[int, int], tuple[int, int], dict[str, Any]]] = {}


def _stat_of(path: Path) -> tuple[int, int]:
    """A file's ``(mtime_ns, size)``, or ``(0, -1)`` when it is absent."""

    try:
        stat = path.stat()
    except OSError:
        return (0, -1)
    return (stat.st_mtime_ns, stat.st_size)


def _config_signature(
    session: str,
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    from ...storage import providers as provider_store
    from ...storage import session_config
    from ...storage import settings as settings_store

    # providers.toml is part of the answer: `effective`/`_trio_display`
    # resolve a pin's model and reasoning through the provider RECORD, so a
    # `provider edit --default-model` must invalidate the memo too — the
    # pin+settings-only signature served a stale chip forever.
    return (
        _stat_of(session_config.path_for(session)),
        _stat_of(settings_store.SETTINGS_FILE),
        _stat_of(provider_store.PROVIDERS_FILE),
    )


def config_show(session: str) -> dict[str, Any] | None:
    """``config show --session S``: ``{session, pin, effective}`` or None.

    Memoized per session while the pin/settings/providers files are
    unchanged, so a repaint that reads the config several times costs ONE
    child. Any write — through this module, another process, or a test —
    changes a file's stat and the next call re-reads.
    """

    signature = _config_signature(session)
    cached = _SHOW_CACHE.get(session)
    if cached is not None and cached[:3] == signature:
        return cached[3]
    data = run_yaml(cli_argv("config", "show", "--session", session))
    if data is not None:
        _SHOW_CACHE[session] = (*signature, data)
    return data


def invalidate_config_show(session: str | None = None) -> None:
    """Drop the ``config show`` memo (one session, or all of them)."""

    if session is None:
        _SHOW_CACHE.clear()
    else:
        _SHOW_CACHE.pop(session, None)


def config_set_trio(
    session: str, target: str, provider: str, model: str, reasoning: str
) -> tuple[bool, str]:
    """Write one trio through the CLI: the pin AND the global default."""

    code, out, err = run_sync(
        cli_argv(
            "config",
            "set",
            "--session",
            session,
            "--target",
            target,
            "--provider",
            provider,
            "--model",
            model,
            "--reasoning",
            reasoning,
        )
    )
    invalidate_config_show()
    return code == 0, (err.strip() or out.strip())


def config_set_theme(theme: str) -> bool:
    code, _out, _err = run_sync(cli_argv("config", "set", "--theme", theme))
    return code == 0


def config_set_active_session(session: str) -> bool:
    """Publish the active-session pointer (the old ``_pin_session`` write)."""

    code, _out, _err = run_sync(cli_argv("config", "set", "--active-session", session))
    return code == 0


def config_seed(source: str, target: str) -> bool:
    """``config seed --source S --target T`` — the mint's copy-if-absent pin.

    The whole pin (provider trios AND style) in one child, with no global
    default write — unlike the per-trio ``config set`` calls the old
    ``_seed_minted_session`` made, which dropped the style key and rewrote
    the installation's default on every mint (invariant 12).
    """

    code, _out, _err = run_sync(cli_argv("config", "seed", "--source", source, "--target", target))
    invalidate_config_show()
    return code == 0


# ─── chat: the argv only — the streaming runner lives in the app ───────────


def chat_argv(prompt: str, session: str, *, alarms: bool = False) -> list[str]:
    """The argv of one TUI-owned turn.

    ``--name`` carries the session (whose pin carries provider/style), ``-c``
    continues it. ``alarms`` swaps the prompt for the inbox drain: the CLI
    wakes the session with its queued messages as the turn's user message.

    The prompt sits BEHIND ``--`` (the ``message insert`` lane's rule): a
    prompt beginning with a dash (``- please answer``, ``--x``) is otherwise
    parsed as an option — the CLI exits 2 with "No such option" and the user
    cannot send it at all.
    """

    args = cli_argv("chat")
    args += ["--name", session]
    if alarms:
        args += ["--alarms"]
    args.append("-c")
    if not alarms:
        args += ["--", prompt]
    return args


# ─── inbox ──────────────────────────────────────────────────────────────────


def message_insert(session: str, text: str, *, kind: str, source: str) -> str | None:
    """``message insert`` — the id is stdout's first non-empty line."""

    code, out, _err = run_sync(
        cli_argv(
            "message",
            "insert",
            "--session",
            session,
            "--kind",
            kind,
            "--source",
            source,
            "--",
            text,
        ),
        timeout=30.0,
    )
    if code != 0:
        return None
    for line in out.splitlines():
        if line.strip():
            return line.strip()
    return None


def message_list(session: str) -> list[dict[str, Any]]:
    """``message list`` — the pending messages, oldest first (non-draining)."""

    data = run_yaml(cli_argv("message", "list", "--session", session))
    if data is None:
        return []
    messages = data.get("messages")
    return [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []


# ─── the provider catalog and records ───────────────────────────────────────


def list_catalog_providers() -> list[dict[str, Any]] | None:
    """``provider providers`` — one dict per catalog entry, or None on error."""

    data = run_yaml(cli_argv("provider", "providers"))
    if data is None:
        return None
    providers = data.get("providers")
    return [p for p in providers if isinstance(p, dict)] if isinstance(providers, list) else []


def list_catalog_models(provider_name: str) -> list[dict[str, Any]] | None:
    """``provider models P`` — ``{id, name}`` per model, or None on error."""

    data = run_yaml(cli_argv("provider", "models", provider_name))
    if data is None:
        return None
    models = data.get("models")
    return [m for m in models if isinstance(m, dict)] if isinstance(models, list) else []


def provider_add(
    provider_name: str,
    *,
    api_key: str,
    default_model: str = "",
    reasoning: str | None = None,
) -> tuple[bool, str]:
    args = cli_argv("provider", "add", provider_name, "--api-key", api_key)
    if default_model:
        args += ["--default-model", default_model]
    if reasoning is not None:
        args += ["--reasoning", reasoning]
    code, out, err = run_sync(args, timeout=120.0)
    return code == 0, (err.strip() or out.strip())


def provider_custom_add(
    *,
    name: str,
    api_format: str,
    base_url: str,
    api_key: str,
    default_model: str,
    reasoning: str | None,
    max_context_window: int | None,
    max_output_tokens: int | None,
) -> tuple[bool, str]:
    args = cli_argv(
        "provider",
        "custom",
        "add",
        "--name",
        name,
        "--format",
        api_format,
        "--base-url",
        base_url,
        "--api-key",
        api_key,
        "--default-model",
        default_model,
    )
    if reasoning is not None:
        args += ["--reasoning", reasoning]
    if max_context_window:
        args += ["--max-context-window", str(max_context_window)]
    if max_output_tokens:
        args += ["--max-output-tokens", str(max_output_tokens)]
    code, out, err = run_sync(args)
    return code == 0, (err.strip() or out.strip())


def provider_edit_caps(
    name: str,
    *,
    max_context_window: int | None = None,
    max_output_tokens: int | None = None,
) -> tuple[bool, str]:
    """Write a custom provider's caps; ``0`` clears, ``None`` leaves stored."""

    args = cli_argv("provider", "edit", name)
    if max_context_window is not None:
        args += ["--max-context-window", str(max_context_window)]
    if max_output_tokens is not None:
        args += ["--max-output-tokens", str(max_output_tokens)]
    code, out, err = run_sync(args)
    return code == 0, (err.strip() or out.strip())


def provider_edit_compatibility(name: str, dialect: str) -> tuple[bool, str]:
    """Switch a catalog provider's format/base URL to a dialect's mapping."""

    code, out, err = run_sync(
        cli_argv("provider", "edit", name, "--compatibility", dialect), timeout=120.0
    )
    return code == 0, (err.strip() or out.strip())


# ─── styles ─────────────────────────────────────────────────────────────────


def style_set(name: str, session: str) -> tuple[bool, str]:
    """``style set NAME --session S`` — the style picker's write."""

    code, out, err = run_sync(cli_argv("style", "set", name, "--session", session))
    return code == 0, (err.strip() or out.strip())
