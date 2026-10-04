"""Single-shot chat command."""

from __future__ import annotations

import datetime
import sys
import time
import uuid
from dataclasses import replace

import typer

from ..core import subagents
from ..core.chat import ChatOptions, run_chat
from ..core.events import ConsoleEventSink
from ..storage import settings as settings_store
from ..storage.paths import SUBAGENT_RESULTS_DIR

# A one-shot `chat` must not orphan the background jobs its turn spawned:
# they run on a daemon thread that dies with the process, and their
# completion alarms live in an in-memory deque. So after the main turn the
# chain waits for the jobs and replays their alarms as follow-up turns,
# until nothing is running and nothing is pending.
#
# Both bounds exist because automation must not hang on a wedged job:
# FOLLOWUP_MAX_TURNS caps the turns a single invocation may chain (a
# turn that keeps spawning jobs can never settle), FOLLOWUP_WAIT_SECONDS
# caps the total wall time spent waiting (mirrors API_STALL_TIMEOUT_SECONDS).
FOLLOWUP_MAX_TURNS = 20
FOLLOWUP_WAIT_SECONDS = 1800.0
FOLLOWUP_POLL_SECONDS = 0.5


def run_turn_chain(opts: ChatOptions, *, quiet: bool) -> None:
    """Run the prompt's turn, then keep turning while subagents are alive.

    A background job survives `run_chat`'s event-loop teardown but not
    process exit, so returning after the first turn would kill it and lose
    its alarm. The chain is the only thing that keeps a one-shot chat
    equivalent to a TUI conversation.
    """

    run_chat(opts, sink=ConsoleEventSink(quiet=quiet))
    _run_followups(opts, quiet=quiet)


def _run_followups(opts: ChatOptions, *, quiet: bool) -> None:
    """Chain follow-up turns until no job is running and no alarm is pending."""

    followups = 0
    deadline = time.monotonic() + FOLLOWUP_WAIT_SECONDS
    while True:
        alarms = subagents.take_pending_alarms()
        if alarms:
            if followups >= FOLLOWUP_MAX_TURNS:
                _warn(
                    f"✗ stopping after {FOLLOWUP_MAX_TURNS} follow-up turns: background "
                    f"subagents are still reporting completions. Read {SUBAGENT_RESULTS_DIR}/ "
                    f"for anything that arrived after this command exited."
                )
                return
            prompt = "\n\n".join(alarm.text for alarm in alarms)
            followups += 1
            if not quiet:
                sys.stdout.write(f"------follow-up {followups}------\n")
                sys.stdout.flush()
            run_chat(
                replace(opts, prompt=prompt, continue_session=True),
                sink=ConsoleEventSink(quiet=quiet),
            )
            continue
        if not subagents.running_jobs():
            return
        if time.monotonic() >= deadline:
            running = ", ".join(f"{name}-{sid}" for sid, name in subagents.running_jobs())
            _warn(
                f"✗ giving up after {FOLLOWUP_WAIT_SECONDS:.0f}s waiting for background "
                f"subagents: {running}. Results land in {SUBAGENT_RESULTS_DIR}/ once they finish."
            )
            return
        time.sleep(FOLLOWUP_POLL_SECONDS)


def _warn(message: str) -> None:
    typer.secho(message, err=True, fg="yellow")


def chat_cmd(
    prompt: str = typer.Argument(..., help="Prompt to send to the agent."),
    name: str | None = typer.Option(
        None, "--name", help="Session name. Without --name and --continue, a fresh session is generated each call."
    ),
    provider: str | None = typer.Option(None, "--provider", help="Override the active provider."),
    model: str | None = typer.Option(None, "--model", help="Override the default model."),
    style: str | None = typer.Option(None, "--style", help="Override the active style."),
    subagent_provider: str | None = typer.Option(
        None, "--subagent-provider", help="Override the subagent's provider for this call."
    ),
    subagent_model: str | None = typer.Option(
        None, "--subagent-model", help="Override the subagent's model for this call."
    ),
    max_tokens: int | None = typer.Option(None, "--max-tokens", help="Cap output tokens."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    reasoning: str | None = typer.Option(
        None, "--reasoning",
        help="Override reasoning for this call (effort level / on / none). "
        "Default: provider setting, else lma model default.",
    ),
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
        subagent_provider=subagent_provider,
        subagent_model=subagent_model,
        max_tokens=max_tokens,
        temperature=temperature,
        reasoning=reasoning,
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
    run_turn_chain(opts, quiet=quiet)
