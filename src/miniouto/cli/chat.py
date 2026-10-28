"""`miniouto chat` — the one command that runs a turn, and the delegation door.

There is no second runtime path. A delegated run IS a chat run: same tools,
same chain, same watchdog. It differs from outo in exactly four ways, each one
a flag on this command:

- `--role TAG --style NAME`     → the system prompt is that role's body; the
                                  style is required, so the run is self-contained
- `--use-subagent-provider`      → provider/model/reasoning come from the
                                  subagent trio (this session's pin, else the
                                  global default)
- `--alarm-session NAME`         → one completion alarm lands in that inbox
- `--return [PATH]`              → the final answer is written as markdown

and a fifth, orthogonal one — the WAKEUP door:

- `--alarms -c [--name S]`       → the turn's prompt IS this session's queued
                                  inbox messages (joined blank-line apart)

`--return` is the only one of these whose value is optional (`cli/__init__.py`
expands the bare form). Every other value must be spelled out: a delegated
command line is run by `Bash`, and one that reads `settings.session` or
`settings.style` behind the caller's back is not the same run twice.

PROVIDER SELECTION is per session first, per call second: a run without
`--pm` follows this session's pin (`storage/session_config.py`), else the
global default; `--pm "PROVIDER/MODEL[:REASONING]"` overrides that trio for
one call and nothing else. One compact flag is what makes a spawned process
explicitly pinnable — the multi-process answer to the pin.

`--bg` detaches the whole thing into a new OS process, whose completion arrives
later as a message in a session inbox (`core/inbox.py`) — a WAKEUP, not
something this run waits for. This command never waits: it runs its turn,
prints the answer behind one `------finish------`, and exits, however much is
still queued. `--alarms -c` is the explicit inverse: wake a session by
draining its inbox into the turn's prompt. `core/spawn_state.py`'s marker is
the on-disk record a monitor reads, not a promise this command keeps.

A summarization hand-off (`core/summary.py`) is a fifth, orthogonal move: the
model writes summary documents and calls `continue_session`, and THIS layer
performs the session transition — the run continues in the new session, its
delegation contract intact, and the post-step reports the session it finally
ended in rather than the one it started in.
"""

from __future__ import annotations

import datetime
import os
import secrets
import subprocess
import sys
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

import typer

from ..core import inbox, spawn_state, summary
from ..core.chat import ChatOptions, run_chat
from ..core.events import ConsoleEventSink
from ..core.inbox import ALARM_HEADER
from ..core.runtime import ChatOverrides, _resolve_subagent, default_provider_config
from ..core.spawn_state import SpawnMarker, finish_marker, write_marker
from ..storage import session_config
from ..storage import sessions as session_store
from ..storage import settings as settings_store
from ..storage import styles as style_store
from ..storage.paths import SPAWN_LOG_DIR, SUBAGENT_RESULTS_DIR

# A run NEVER waits. A queued message — a detached run's completion alarm, a
# human's interjection — is a WAKEUP for whoever is awake, not a debt this
# process settles on their behalf: `miniouto chat "..."` answers its prompt,
# prints `------finish------` and exits, and whatever arrived afterwards stays
# in the inbox for the next wakeup (the TUI's poll, or `chat --alarms`). The
# mid-turn path is unaffected: a message that arrives while the loop is still
# running is delivered by the runtime's BEFORE_LLM_CALL hook, which appends it
# to the live history (`core/runtime.py`), not by anything here.
#
# Waiting would also have been a closed-ecosystem assumption: it could only
# ever have been satisfied by the markers THIS run happened to start, while any
# producer anywhere may `miniouto message insert` into any inbox.

# A summarization hand-off moves a run to a NEW session, and a run may do it
# more than once (summary → cont-1 → cont-2). This is the ONE loop left here:
# the model summarized, called `continue_session`, and the work continues in
# the same run — the model must not have to be re-woken to read its own
# continuation message. The bound exists because a model that keeps calling
# `continue_session` must cost a warning, not an unbounded chain.
HANDOFF_MAX_TRANSITIONS = 5

# Without a result file the alarm is the only channel carrying the answer, so
# it is capped at a length a model will still read whole.
MAX_ALARM_ANSWER_CHARS = 4000

_STATUS_DONE = "done"
_STATUS_ERROR = "error"
_STATUS_STOPPED = "stopped"

# "The pid of this process" as a default, distinct from a real `None`, which
# means "not known yet" in a marker's payload.
_THIS_PROCESS = object()


def run_turn_chain(
    opts: ChatOptions, *, quiet: bool, state: ChainState | None = None,
    publish_session: bool = True,
) -> str:
    """Run the prompt's turn, then follow any summarization hand-off. Nothing else.

    The run does NOT wait: one prompt, one answer, one `------finish------`,
    then the process is gone. A message that arrives after the turn is a
    wakeup for the next one, not this run's business (see `--alarms`).

    What does continue the run is a hand-off (`core/summary.py`): the model
    summarized, called `continue_session`, and the continuation message is
    already sitting in the NEW session's inbox — so the chain moves its routing
    there and keeps going. The delegation contract (role, alarm session, result
    file) travels with it, and the post-step fires from whichever session the
    chain ends in. `state` is how that outcome is reported back.

    No turn here prints an answer block of its own: each sink is built with
    `emit_final=False` because only this layer can tell a turn from the run.
    A turn that turned out not to be the last one has its answer printed as
    plain text here; the final answer is printed by the caller, after the
    post-step and immediately before the process exits.
    """

    answer = _run_turn(opts, quiet=quiet)
    if state is not None:
        state.session = opts.session
    return _run_handoffs(
        opts, quiet=quiet, state=state, last=answer, publish_session=publish_session
    ) or answer


def _run_turn(opts: ChatOptions, *, quiet: bool) -> str:
    """One turn, with the answer block left for the chain (see `run_turn_chain`)."""

    return run_chat(opts, sink=ConsoleEventSink(quiet=quiet, emit_final=False))


def _print_answer(answer: str, *, quiet: bool, finish: bool) -> None:
    """Write one answer, optionally behind the run's single finish marker.

    Byte-for-byte what `ConsoleEventSink.emit_final_answer` wrote, moved here
    because the marker is a property of the RUN: `finish=True` is passed once,
    by the caller, after the post-step. An intermediate turn's answer is the
    same text with `finish=False` — no marker, because the run continues.
    """

    if finish and not quiet:
        sys.stdout.write("------finish------\n")
    sys.stdout.write(answer or "")
    if not answer or not answer.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def _run_handoffs(
    opts: ChatOptions, *, quiet: bool, state: ChainState | None = None, last: str = "",
    publish_session: bool = True,
) -> str:
    """Continue the run in each session a summarization hand-off moves it to.

    Returns the LAST turn's answer — the previous one when the run stopped at
    the transition bound (the caller falls back to it the same way).
    """

    transitions = 0
    while (handoff := summary.pending_handoff()) is not None:
        if transitions >= HANDOFF_MAX_TRANSITIONS:
            _warn(
                f"✗ stopping after {HANDOFF_MAX_TRANSITIONS} summary hand-offs: the "
                f"work is still in session '{handoff.from_session}' with its summary "
                f"in {handoff.summary_dir}. Start a new session to continue it."
            )
            return ""
        # The previous answer is superseded: plain, no finish marker — the run
        # continues below the transition.
        _print_answer(last, quiet=quiet, finish=False)
        transitions += 1
        summary.consume_handoff()
        opts = _apply_handoff(
            opts, handoff, quiet=quiet, state=state, publish_session=publish_session
        )
        last = _run_turn(
            replace(opts, prompt=_continuation_prompt(handoff, opts.session), continue_session=True),
            quiet=quiet,
        )
        if state is not None:
            state.session = opts.session
    return last


def _continuation_prompt(handoff: summary.Handoff, session: str) -> str:
    """The next turn's prompt: the new session's queued messages, or the contract.

    The tool put the continuation message into the new session's inbox, so the
    drain is the delivery and its text — plus anything else queued alongside it —
    is the prompt. An empty drain is rebuilt from the hand-off facts: the model
    asked to continue, so it must still be told what it is continuing.
    """

    messages = inbox.drain(session)
    if messages:
        return "\n\n".join(m.text for m in messages)
    return summary.build_continuation_message(
        new_session=handoff.new_session,
        from_session=handoff.from_session,
        summary_dir=Path(handoff.summary_dir),
        contract=summary.get_run_contract(),
    )


def _apply_handoff(
    opts: ChatOptions,
    handoff: summary.Handoff,
    *,
    quiet: bool,
    state: ChainState | None,
    publish_session: bool,
) -> ChatOptions:
    """Point the chain at the session the summary hand-off opened, and say so.

    The next turn carries `opts.session` into `ChatOverrides.session`, which
    selects its inbox, `{this_session}` and session pin directly. The run
    contract moves too, so a second hand-off summarizes the new session. Only
    a plain run publishes the new active session for later `-c` wakeups; a
    delegated guest never writes that shared setting.
    """

    if not quiet:
        sys.stdout.write(f"{handoff_marker(handoff)}\n------{handoff.new_session}------\n")
        sys.stdout.flush()
    if publish_session:
        settings_store.update(session=handoff.new_session)
    if state is not None:
        state.session = handoff.new_session
        state.transitions.append(handoff)
        if state.contract:
            summary.set_run_contract(**{**state.contract, "session": handoff.new_session})
    return replace(opts, session=handoff.new_session)


def handoff_marker(handoff: summary.Handoff) -> str:
    """The transition, as visible as `------finish------`: two lines, one job.

    The CLI prints it, the TUI mounts it verbatim as a system row, so a
    summarization reads the same in either surface — and `tests/test_handoff.py`
    pins the exact shape.
    """

    return f"------summary → {handoff.new_session}------\nsummary: {handoff.summary_dir}"


def _handoff_note(handoff: summary.Handoff) -> str:
    """The one line a receiver gets when the run moved sessions mid-work."""

    return f"Continued from: {handoff.from_session} (summary: {handoff.summary_dir})"


@dataclass
class ChainState:
    """Where a chain ended, and what it carried to get there.

    `session` is the session the LAST turn ran in — the one the receiver must
    be told about, which is not the run's original session once a summary
    hand-off has moved the work. `contract` is the published run contract,
    re-registered under each new session so the next hand-off summarizes the
    right one and re-states the same delegation promises.
    """

    session: str
    contract: dict[str, str] = field(default_factory=dict)
    transitions: list[summary.Handoff] = field(default_factory=list)


def _warn(message: str) -> None:
    typer.secho(message, err=True, fg="yellow")


def _fail(message: str) -> typer.Exit:
    typer.secho(f"✗ {message}", err=True, fg="red")
    return typer.Exit(code=1)


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _iso(moment: datetime.datetime) -> str:
    return moment.isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class _Run:
    """One `chat` invocation, resolved: exactly what the turn AND `--bg` need.

    `--bg` re-emits the child's command line from these values, never from
    `sys.argv`, so every optional flag reaches the child already expanded to a
    concrete value — a bare `--return` must never survive the hand-off and be
    re-resolved differently.
    """

    prompt: str
    session: str
    style: str
    role: str | None
    provider: str | None
    model: str | None
    reasoning: str | None
    use_subagent_provider: bool
    max_tokens: int | None
    temperature: float | None
    continue_session: bool
    answer_only: bool
    with_session: bool
    # Delegation, present only when this run promises something extra.
    job_id: str = ""
    bg_child: bool = False
    alarm_session: str | None = None
    path: Path | None = None
    started: str = ""

    @property
    def delegated(self) -> bool:
        return self.bg_child or self.alarm_session is not None or self.path is not None

    @property
    def quiet(self) -> bool:
        return self.answer_only or self.with_session


def _is_plain_run(run: _Run, *, bg: bool) -> bool:
    """True only for a plain interactive chat — the sole writer of `settings.session`.

    A delegated run is a guest in the user's tree: it mints its own `chat-`/`spawn-`
    session name, so persisting it would repoint the user's active session (and the
    TUI's conversation) at a subagent transcript. Every promise-that-something-extra
    marker counts: `--role`, `--alarm-session`, `--return`, `--bg`, `--bg-child`.
    """

    return not bg and run.role is None and not run.delegated


# ── resolution ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _PM:
    """One `--pm` value: the provider trio this call overrides, resolved."""

    provider: str
    model: str
    reasoning: str = ""

    def as_option(self) -> str:
        """The concrete `--pm` spelling, which `--bg` hands to its child."""

        return f"{self.provider}/{self.model}" + (f":{self.reasoning}" if self.reasoning else "")


_PM_USAGE = (
    "--pm takes 'PROVIDER/MODEL[:REASONING]', e.g. --pm 'minimax/MiniMax-M3.1:high'. "
    "A model name may contain '/'; the ':REASONING' suffix is optional (omit it, or "
    "end with ':', to leave reasoning to the session pin / default)."
)


def _parse_pm(value: str) -> _PM:
    """Split `PROVIDER/MODEL[:REASONING]` into the three override fields.

    `provider` is everything before the FIRST `/` (model names contain `/`;
    reasoning is split off the LAST `:` afterwards, so neither splits the
    other). An empty segment is as wrong as a missing `/` — the model is what
    makes a `--pm` usable, so reject the whole command before any resolution.
    """

    provider, slash, rest = value.strip().partition("/")
    head, colon, tail = rest.rpartition(":")
    model, reasoning = (head, tail) if colon else (rest, "")
    if not slash or not provider.strip() or not model.strip():
        raise _fail(f"--pm {value!r} is not a provider/model pair. {_PM_USAGE}")
    return _PM(provider=provider.strip(), model=model.strip(), reasoning=reasoning.strip())


def _pm_from_run(provider: str | None, model: str | None, reasoning: str | None) -> str:
    """The `--pm` spelling for the values this run resolved, or "" for none.

    Both halves are required: a `--pm` with no model is not expressible, and the
    child would re-resolve the model from a different pin than the parent saw.
    """

    if not provider or not model:
        return ""
    return _PM(provider, model, reasoning or "").as_option()


def _validate_role(role: str | None, style_name: str) -> None:
    """Reject an unknown role BEFORE the turn, so a typo costs no model call."""

    if role is None:
        return
    raw = style_store.read(style_name)
    if raw is None:
        return  # the runtime reports a missing style with a better message
    _, specs = style_store.parse_style(raw)
    if all(spec.name != role for spec in specs):
        available = ", ".join(spec.name for spec in specs) or "(none)"
        raise _fail(f"Unknown role {role!r} in style {style_name!r}. Available roles: {available}")


def _validate_wakeup(
    prompt: str | None, *, continue_session: bool, name: str | None, s: settings_store.Settings
) -> None:
    """Reject a malformed `--alarms` invocation before it can touch anything.

    Three rules, in the order the operator meets them. `--alarms` takes no
    prompt — the queued messages ARE the prompt. `--continue` is mandatory:
    history is loaded only when the operator asked for it, never implied by a
    wakeup. And a wakeup must name a session that exists — the caller can say
    which one with `--name`, and there is nothing to wake otherwise.
    """

    if prompt is not None:
        raise _fail("--alarms takes no prompt argument — the queued messages are the prompt.")
    if not continue_session:
        raise _fail("a wakeup continues an existing session — pass --continue")
    if not (name or s.session):
        raise _fail("nothing to wake: no session to continue — pass --name <session>")


def _resolve_return_path(return_opt: str | None, *, job_id: str) -> Path | None:
    """`None` (flag absent) = no file; `""` (flag bare) = the storage default.

    A path that names a directory (existing, or trailing `/`) receives the
    generated filename; anything else is the file itself, verbatim.
    """

    if return_opt is None:
        return None
    if not return_opt:
        return SUBAGENT_RESULTS_DIR / _generated_name(job_id)
    path = Path(return_opt).expanduser()
    if return_opt.endswith("/") or path.is_dir():
        return path / _generated_name(job_id)
    return path


def _generated_name(job_id: str) -> str:
    return f"{job_id}-{_now().strftime('%Y-%m-%d-%H-%M')}.md"


def _resolve_alarm_session(alarm_opt: str | None) -> str | None:
    """`None` (flag absent) = no alarm; otherwise the named session.

    There is no bare form: `--alarm-session` means "my session" only through
    `settings.session`, which is ambient state — the same Bash command typed
    from two agents in two sessions would alarm two different inboxes. The
    option demands its value, so a delegated command line is self-contained.
    """

    if alarm_opt is None:
        return None
    return alarm_opt.strip() or None


def _resolve_session(
    session_opt: str | None, *, continue_session: bool, bg: bool, s: settings_store.Settings
) -> str:
    if session_opt:
        return session_opt
    if continue_session and s.session:
        return s.session
    minted = (
        # A detached run must never share a session writer: whole-file saves
        # from two processes lose turns.
        f"spawn-{_now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        if bg
        # Fresh session each call: timestamp + short UUID keeps names unique even
        # within the same second and sortable by recency, so the previous chat's
        # `settings.session` doesn't bleed in.
        else f"chat-{_now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    )
    # A minted session inherits the caller's provider pins (copy-if-absent), so
    # a delegated run lands on the same provider the caller resolved rather than
    # on whatever the global default says at the child's moment.
    session_config.seed_from(minted, s.session)
    return minted


# ── the delegation post-step ──────────────────────────────────────────────────


def _write_answer_file(path: Path, text: str, *, continued: summary.Handoff | None) -> None:
    """Write the final answer (or `error: <Type>: <msg>`) atomically.

    A hand-off note is appended as a short trailing section, so the file still
    LEADS with the answer — a reader taking the first paragraph gets exactly
    what they would have got without the summarization.
    """

    body = text if continued is None else f"{text.rstrip()}\n\n---\n\n{_handoff_note(continued)}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def _alarm_text(
    *,
    role: str | None,
    job_id: str,
    session: str,
    status: str,
    path: Path | None,
    answer: str,
    continued: summary.Handoff | None = None,
) -> str:
    """The completion message: which run, WHICH SESSION it wrote to and where,
    then the result — the receiving agent has no other way to learn any of it."""

    lines = [
        ALARM_HEADER,
        f"Delegated run '{role or 'outo'}' (id: {job_id}) finished (status: {status}).",
        f"Session: {session} — {session_store.path_for(session)}",
    ]
    if continued is not None:
        lines.append(_handoff_note(continued))
    if path is not None:
        lines.append(
            f"Result file: {path} — read this file (e.g. with `cat`) to see what it produced."
        )
        return "\n".join(lines)
    capped = answer[:MAX_ALARM_ANSWER_CHARS]
    truncated = "\n…(truncated)" if len(answer) > MAX_ALARM_ANSWER_CHARS else ""
    return "\n".join(lines) + f"\n\n{capped}{truncated}"


def _deliver_alarm(
    run: _Run, *, status: str, answer: str, continued: summary.Handoff | None = None
) -> None:
    if run.alarm_session is None:
        return
    inbox.insert(
        run.alarm_session,
        _alarm_text(
            role=run.role,
            job_id=run.job_id,
            session=run.session,
            status=status,
            path=run.path,
            answer=answer,
            continued=continued,
        ),
        kind=inbox.ALARM_KIND,
        source="chat",
        meta={
            "id": run.job_id,
            "role": run.role or None,
            "style": run.style or None,
            "return_path": str(run.path) if run.path is not None else None,
        },
    )


def _complete(
    run: _Run,
    status: str,
    answer: str,
    *,
    quiet: bool,
    continued: summary.Handoff | None = None,
) -> None:
    """Write the answer file, deliver the alarm, and report the outcome."""

    try:
        if run.path is not None:
            _write_answer_file(run.path, answer, continued=continued)
        _deliver_alarm(run, status=status, answer=answer, continued=continued)
        if not quiet:
            sys.stdout.write(
                f"----- chat<{run.job_id}> complete ----- result: "
                f"{run.path if run.path is not None else '(not saved)'}\n"
            )
            sys.stdout.flush()
    finally:
        finish_marker(run.job_id, status, finished=_iso(_now()))


# ── --bg detachment ───────────────────────────────────────────────────────────


def _bg_argv(run: _Run) -> list[str]:
    """Rebuild the child's command line from the RESOLVED values.

    Every optional value is expanded here: a bare `--return` becomes
    `--return <path>` and the provider trio becomes one concrete
    `--pm "prov/model[:reasoning]"`, so the child cannot re-resolve anything
    differently.
    """

    argv = [
        sys.executable, "-m", "miniouto", "chat", run.prompt,
        "--bg-child", "--job-id", run.job_id,
        "--name", run.session,
        "--style", run.style,
    ]
    if run.role:
        argv += ["--role", run.role]
    if run.alarm_session is not None:
        argv += ["--alarm-session", run.alarm_session]
    if run.path is not None:
        argv += ["--return", str(run.path)]
    if run.use_subagent_provider:
        argv.append("--use-subagent-provider")
    pm = _pm_from_run(run.provider, run.model, run.reasoning)
    if pm:
        argv += ["--pm", pm]
    if run.max_tokens is not None:
        argv += ["--max-tokens", str(run.max_tokens)]
    if run.temperature is not None:
        argv += ["--temperature", str(run.temperature)]
    if run.continue_session:
        argv.append("--continue")
    if run.answer_only:
        argv.append("--answer-only")
    return argv


def _detach(argv: list[str], log_path: Path) -> int:
    """Fully detach the child: its own session, and FILE stdio only.

    `tools/bash.py` kills whole process groups and pipe-holding children, so a
    detached run survives only with `start_new_session=True` and never a pipe.
    Returns the child's pid, which the marker records as the liveness proof.
    """

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return child.pid


def chat_cmd(
    prompt: str | None = typer.Argument(
        None, help="Prompt to send to the agent. Omit it only with --alarms."
    ),
    name: str | None = typer.Option(
        None, "--name", help="Session name. Without --name and --continue, a fresh session is generated each call."
    ),
    pm: str | None = typer.Option(
        None, "--pm",
        help="Provider/model (and optional reasoning) for THIS call, as "
        "'PROVIDER/MODEL[:REASONING]' — e.g. 'minimax/MiniMax-M3.1:high'. "
        "A model name may contain '/'; the ':REASONING' suffix is optional "
        "(omit it, or end with ':', to leave reasoning to the session pin). "
        "Without this flag the run follows this session's provider pin, "
        "falling back to the global default.",
    ),
    style: str | None = typer.Option(None, "--style", help="Override the active style."),
    role: str | None = typer.Option(
        None, "--role",
        help="Run as this style role's system prompt (a <tag>...</tag> body). "
        "Requires --style. Without it, the <outo> body runs. "
        "This is what makes the turn a delegated one.",
    ),
    use_subagent_provider: bool = typer.Option(
        False, "--use-subagent-provider",
        help="Resolve provider/model/reasoning from the subagent trio "
        "(this session's pin, else the settings default) instead of outo's. "
        "An explicit --pm still wins.",
    ),
    alarm_session: str | None = typer.Option(
        None, "--alarm-session",
        help="Insert one completion alarm into THIS session's inbox. The value is "
        "required: a bare flag would resolve to whatever settings.session happens "
        "to be, which is not idempotent.",
    ),
    return_path: str | None = typer.Option(
        None, "--return",
        help="Write the final answer as markdown to this path (a directory, or "
        "trailing '/', gets the generated filename). Given without a value, "
        f"{SUBAGENT_RESULTS_DIR} is used.",
    ),
    bg: bool = typer.Option(
        False, "--bg", help="Detach the run into its own process and return immediately."
    ),
    alarms: bool = typer.Option(
        False, "--alarms",
        help="Wake a session from its queued messages instead of from a prompt: "
        "the inbox is drained and the joined texts become the turn's user "
        "message. Requires --continue (a wakeup continues an existing session "
        "— history is never loaded implicitly) and takes no prompt.",
    ),
    bg_child: bool = typer.Option(
        False, "--bg-child", hidden=True, help="Internal: re-entry of a --bg run. Runs the foreground path."
    ),
    job_id: str | None = typer.Option(
        None, "--job-id", hidden=True, help="Internal: adopt the parent --bg run's job id."
    ),
    max_tokens: int | None = typer.Option(None, "--max-tokens", help="Cap output tokens."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    continue_session: bool = typer.Option(
        False, "--continue", "-c", help="Prepend the session's previous history."
    ),
    answer_only: bool = typer.Option(
        False, "--answer-only", "-a",
        help="Print only the final answer. Suppresses the session marker, loop events, "
        "and the completion line on stdout — never the session file.",
    ),
    with_session: bool = typer.Option(
        False, "--with-session",
        help="Print only the session marker and the final answer. Suppresses loop events and the finish marker.",
    ),
) -> None:
    """Run one prompt and print the agent's reply."""

    if answer_only and with_session:
        raise _fail("--answer-only and --with-session are mutually exclusive.")
    if bg and bg_child:
        raise _fail("--bg cannot be combined with --bg-child.")
    # Checked here, ahead of every resolution, so a rejected command leaves no
    # session file and costs no model call.
    if role is not None and style is None:
        raise _fail(
            "--role requires --style (the default style may differ per session; "
            "make the run self-contained)."
        )
    guest = (
        role is not None
        or alarm_session is not None
        or return_path is not None
        or bg
        or bg_child
    )
    if guest and continue_session and not alarms and name is None:
        raise _fail(
            "a delegated --continue run requires --name SESSION; "
            "the published active session belongs to its caller."
        )

    s = settings_store.load()

    # The wakeup lane is resolved FIRST, and rejected before anything is
    # written: a wrong `--alarms` invocation must cost no session file and no
    # model call. `--continue` is mandatory because loading history is the
    # operator's explicit choice — nothing here may imply it.
    if alarms:
        _validate_wakeup(prompt, continue_session=continue_session, name=name, s=s)
        turn_prompt = ""  # replaced below by the drained inbox
    elif prompt is None:
        raise _fail("a chat needs either a prompt argument or --alarms.")
    else:
        turn_prompt = prompt

    override = _parse_pm(pm) if pm is not None else None
    provider = override.provider if override else None
    model = override.model if override else None
    reasoning = override.reasoning if override and override.reasoning else None

    session_name = _resolve_session(
        name, continue_session=continue_session, bg=bg, s=s
    )

    # The STYLE is pinned per session, like the model: an explicit `--style`
    # wins, then this session's pin, then the global default. Reading the
    # pin here (not `effective`) keeps the lookup side-effect free — the
    # pin is frozen later, by the run itself.
    pinned_style = (session_config.read(session_name) or session_config.EMPTY).style
    style_name = style or pinned_style or s.style or "default"
    _validate_role(role, style_name)

    if alarms and not session_store.path_for(session_name).exists():
        raise _fail(f"nothing to wake: session '{session_name}' does not exist")
    # Drained here, not inside the turn: an inbox with nothing in it means no
    # turn ran at all, and a run that never ran has no session marker and no
    # finish block to print.
    woken = inbox.drain(session_name) if alarms else []
    if alarms and not woken:
        typer.echo(f"no pending messages in session '{session_name}'.")
        return
    turn_prompt = "\n\n".join(m.text for m in woken) if alarms else turn_prompt

    if use_subagent_provider:
        # Resolve against this explicitly selected session, not the published
        # active session, which another process may change at any moment.
        pinned = session_config.effective(session_name, default=default_provider_config)
        try:
            sub_provider, sub_model = _resolve_subagent(
                ChatOverrides(subagent_provider=provider, subagent_model=model), s, pinned
            )
        except (RuntimeError, ValueError) as exc:
            raise _fail(str(exc)) from exc
        provider = sub_provider or provider
        model = sub_model or model
        reasoning = reasoning or pinned.subagent_reasoning or s.subagent_reasoning or None

    resolved_id = job_id or secrets.token_hex(3)
    run = _Run(
        prompt=turn_prompt,
        session=session_name,
        style=style_name,
        role=role,
        provider=provider,
        model=model,
        reasoning=reasoning,
        use_subagent_provider=use_subagent_provider,
        max_tokens=max_tokens,
        temperature=temperature,
        continue_session=continue_session,
        answer_only=answer_only,
        with_session=with_session,
        job_id=resolved_id,
        bg_child=bg_child,
        alarm_session=_resolve_alarm_session(alarm_session),
        path=_resolve_return_path(return_path, job_id=resolved_id),
        started=_iso(_now()),
    )

    # Only a plain interactive chat owns `settings.session` — see `_is_plain_run`.
    if _is_plain_run(run, bg=bg):
        settings_store.update(session=session_name)

    if bg:
        log_path = SPAWN_LOG_DIR / f"{resolved_id}.log"
        marker_path = spawn_state.marker_path(resolved_id)
        # Written before the child exists (an instantly-finishing run still has
        # a marker to stamp), then completed with the child's pid. The first
        # write carries no pid, which reads as "alive" — never a dropped waiter.
        write_marker(_marker(run, log=str(log_path), pid=None))
        _stamp_pid(marker_path, _detach(_bg_argv(run), log_path))
        # Every channel the run will ever use is named here: the process is
        # gone by the time its alarm arrives, so the launcher must be told
        # where the transcript goes and which file holds the status.
        typer.echo(
            f"Started run '{resolved_id}' (bg).\n"
            f"Session: {session_name} — {session_store.path_for(session_name)}\n"
            f"Log: {log_path}\n"
            f"Status: {marker_path} (running → done|error|stopped)\n"
            + (
                f"Alarm → session '{run.alarm_session}' when finished."
                if run.alarm_session is not None
                else "No alarm configured — check the status file."
            )
        )
        return

    _run_foreground(run)


def _marker(
    run: _Run, *, log: str | None = None, pid: int | object | None = _THIS_PROCESS
) -> SpawnMarker:
    """The liveness promise. A `--bg` child adopts its parent's bookkeeping.

    `pid` names the process the promise is about; it defaults to this one,
    because a foreground run IS the run its marker describes. `pid=None` is
    the separate "not known yet" value a marker carries between the two writes
    of the detach path.
    """

    fresh = SpawnMarker(
        id=run.job_id,
        alarm_session=run.alarm_session or "",
        session=run.session,
        started=run.started,
        role=run.role,
        style=run.style,
        return_path=str(run.path) if run.path is not None else None,
        log=log,
        pid=os.getpid() if pid is _THIS_PROCESS else pid,  # type: ignore[arg-type]
    )
    parent = spawn_state.read_marker(spawn_state.marker_path(run.job_id))
    if parent is None:
        return fresh
    # The child's pid IS the one the parent stamped at detach, so the adopted
    # value is kept; the pid here is only a fallback for a marker whose parent
    # record predates the field.
    return replace(
        fresh,
        started=parent.started or fresh.started,
        log=log or parent.log,
        pid=parent.pid if parent.pid is not None else fresh.pid,
    )


def _stamp_pid(path: Path, pid: int) -> None:
    """Attach a detached child's pid to the marker already on disk.

    The marker must exist BEFORE the child starts (a run that finishes
    instantly still has a marker to stamp), but the pid is only known once
    `Popen` returns. So this second write adds one field and nothing else —
    it never resurrects a marker the child already finished.
    """

    marker = spawn_state.read_marker(path)
    if marker is None or marker.status != spawn_state.RUNNING or marker.pid is not None:
        return
    write_marker(replace(marker, pid=pid))


def _chat_options(run: _Run) -> ChatOptions:
    return ChatOptions(
        prompt=run.prompt,
        session=run.session,
        provider=run.provider,
        model=run.model,
        style=run.style,
        role=run.role,
        max_tokens=run.max_tokens,
        temperature=run.temperature,
        reasoning=run.reasoning,
        continue_session=run.continue_session,
    )


def _register_run_contract(run: _Run) -> dict[str, str]:
    """Publish this run's identity for the summarization lane.

    The `continue_session` tool reads this slot to build the continuation
    message, so a delegated run's two outcome contracts — alarm session and
    result file — travel into the session that continues the work. Plain runs
    contribute identity only. Returns the values so the chain can re-register
    them under each new session it moves to.
    """

    contract = {
        "session": run.session,
        "role": run.role or "",
        "style": run.style,
        "alarm_session": run.alarm_session or "",
        "return_path": str(run.path) if run.path is not None else "",
    }
    summary.set_run_contract(**contract)
    return contract


def _run_foreground(run: _Run) -> None:
    """The one code path: build the options, run the chain, then the post-step."""

    quiet = run.quiet
    if not run.answer_only:
        sys.stdout.write(f"------{run.session}------\n")
        sys.stdout.flush()

    state = ChainState(session=run.session, contract=_register_run_contract(run))
    if not run.delegated:
        _print_answer(
            run_turn_chain(
                _chat_options(run), quiet=quiet, state=state, publish_session=True
            ), quiet=quiet, finish=True
        )
        return

    _run_delegated(run, state, quiet=quiet)


def _run_delegated(run: _Run, state: ChainState, *, quiet: bool) -> None:
    """A run that owes something on completion: answer file, alarm, marker."""

    write_marker(_marker(run))
    status, answer = _STATUS_DONE, ""
    try:
        answer = run_turn_chain(
            _chat_options(run), quiet=quiet, state=state, publish_session=False
        )
    except KeyboardInterrupt:
        status, answer = _STATUS_STOPPED, "stopped: cancelled"
    except Exception as exc:
        status, answer = _STATUS_ERROR, f"error: {type(exc).__name__}: {exc}"
    # The run may have ended in a DIFFERENT session than it started in (a
    # summarization hand-off), and everything a receiver is pointed at — the
    # transcript path, the marker's own `session` field — must name that one.
    final = replace(run, session=state.session)
    if final.session != run.session:
        write_marker(_marker(final))
    _complete(
        final,
        status,
        answer,
        quiet=quiet,
        continued=state.transitions[0] if state.transitions else None,
    )
    # The run's ONE finish marker, printed after everything the run owes and
    # immediately before it returns: nothing may follow it.
    _print_answer(answer, quiet=quiet, finish=True)
    if status != _STATUS_DONE:
        raise typer.Exit(code=1)
