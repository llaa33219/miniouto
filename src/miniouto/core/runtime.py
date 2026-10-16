"""Build a runtime: register providers, presets, and resolve outo.

One process supervises exactly ONE actor. Delegation is cross-process
(a detached `miniouto chat --bg` run + the inbox in `core/inbox.py`), so there
is no in-process
subagent engine, no subagent tool, and no second preset: a spawned run
is the SAME runtime built with `role` set, which swaps the system prompt for
one of the active style's named subagent specs. That is the ONLY difference:
the tool list, the preset, and every hook are identical in both modes.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import coreouto as co

from ..paths_runtime import INVOCATION_CWD
from ..storage import providers as provider_store
from ..storage import session_config
from ..storage import settings as settings_store
from ..storage import skills as skill_store
from ..storage import styles as style_store
from ..storage.styles import SubagentSpec
from ..tools import bash as bash_tool
from ..tools import registry as tool_registry
from . import inbox, repetition
from .providers import API_STALL_TIMEOUT_SECONDS, build_coreouto_provider, clear_coreouto_state

# Tools every runtime sees, in every mode — role-mode (spawned) runs
# included. See the module docstring.
BASE_TOOLS = ["Bash", "Image", "Video", "Audio", "continue_session"]

# Platform-gated tools. Registration is conditional (see
# tools.registry.register_all), so the preset below filters this list down to
# what actually got registered — on unsupported platforms the preset simply has
# no Computer tool. NOT about mode parity: a role-mode run sees the same list.
PLATFORM_GATED_TOOLS = ["Computer"]

# Watchdog state (coreouto >= 0.11 contrib trackers), rebuilt by every
# build_runtime call alongside the hooks that feed them. The supervised
# runner (`supervised_run`) reads them to decide whether a call is wedged:
#   _ACTIVITY_STATE — seconds since the last loop event of any kind
#   _PROGRESS_STATE — current phase ("llm_call" / "tool_call" / None)
#   _LIVE_MESSAGES  — the live message list under the literal key "outo"
#                     (by reference; the loop keeps mutating it, so it is
#                     always current)
_ACTIVITY_STATE: Any = None
_PROGRESS_STATE: Any = None
_LIVE_MESSAGES: dict[str, Any] = {}

# Tokens of the supervisors currently running. The level bookkeeping is gone
# (one actor, one supervisor); the registry stays so the supervisor can tell
# "is a supervisor of mine active" from a future second one, and so tests can
# assert nothing leaked.
_ACTIVE_SUPERVISORS: dict[object, int] = {}

WATCHDOG_CHECK_SECONDS = 5.0
WATCHDOG_MAX_WAKEUPS = 3


class LoopStalledError(Exception):
    """A supervisor exhausted its wakeups — the loop re-wedges every time."""


class LoopRepeatedError(Exception):
    """A supervisor exhausted its repetition restarts — the loop repeats.

    Sibling of `LoopStalledError`, NOT of `LoopCancelledError`: a repeat is
    the model's own failure, never a user stop, so cancellation handling
    (which reports a clean, expected stop) must not swallow it.
    """


def watchdog_states() -> tuple[Any, Any, dict[str, Any]]:
    """Return (activity_state, progress_state, live_messages) for supervisors."""

    return _ACTIVITY_STATE, _PROGRESS_STATE, _LIVE_MESSAGES


def sanitize_history(
    messages: list[co.Message] | None,
    *,
    on_removed: Callable[[list[str]], None] | None = None,
) -> list[co.Message] | None:
    """Prepare a cancelled loop's messages for `call(history=...)`.

    From coreouto examples/27+28 (`cut_to_last_answered_turn`): drops
    system messages (call() prepends its own) and a trailing assistant
    turn whose tool calls never got their results — providers reject
    dangling tool calls with HTTP 400. coreouto appends a turn's tool
    results only after ALL of its tool calls finish, so a cancellation
    lands either before or after the whole batch — the tail is always a
    bare assistant message, never a partially-answered one.

    `on_removed` receives the `str(id(message))` of every truncated
    message so `core.chat` can prune the display record of a turn the
    model is about to forget. The dropped SYSTEM messages are reported
    as nothing: no loop event is ever produced from one, so a ref could
    only ever add a phantom count to the removal note.
    """

    if not messages:
        return None
    history = [m for m in messages if m.role != "system"]
    for i in range(len(history) - 1, -1, -1):
        msg = history[i]
        if msg.role == "assistant" and msg.tool_calls:
            answered = {m.tool_call_id for m in history[i + 1:] if m.role == "tool"}
            if {tc.id for tc in msg.tool_calls} - answered:
                if on_removed is not None:
                    on_removed([str(id(m)) for m in history[i:]])
                del history[i:]
            break
    return history or None


def _repair_pairing(
    messages: list[Any],
    *,
    on_removed: Callable[[list[str]], None] | None = None,
) -> bool:
    """Gentle in-place repair of tool-call pairing; True if anything changed.

    Two fixes, both about making the request payload structurally valid:

    - Drop duplicate tool-result messages (same `tool_call_id` answered
      twice). Strict providers reject duplicates with a 400, and the old
      `tool_result` error reaction used to pile them up — a session
      persisted mid-poisoning can still carry them. Each dropped copy is
      reported through `on_removed`; its own ref is harmless because no
      loop event is ever stamped with a tool message's identity (results
      carry their ASSISTANT message's ref instead), so the prune it
      triggers removes nothing.
    - Fill dangling tool calls on the LAST assistant tool-call turn with
      error results (coreouto examples/29 shape A). coreouto appends a
      turn's results as one batch, so only the tail can dangle; an
      interrupted turn restored from disk is the usual source. Filling
      beats cutting here: the turn's earlier work is kept. FILLING IS AN
      ADDITION — nothing is removed, so `on_removed` is not called.
    """

    changed = False
    seen: set[str] = set()
    deduped: list[Any] = []
    dropped: list[Any] = []
    for m in messages:
        if m.role == "tool":
            call_id = getattr(m, "tool_call_id", None)
            if call_id is not None:
                if call_id in seen:
                    changed = True
                    dropped.append(m)
                    continue
                seen.add(call_id)
        deduped.append(m)
    if changed:
        messages[:] = deduped
        if on_removed is not None:
            on_removed([str(id(m)) for m in dropped])

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.role == "assistant" and msg.tool_calls:
            answered = {m.tool_call_id for m in messages[i + 1:] if m.role == "tool"}
            missing = [tc for tc in msg.tool_calls if tc.id not in answered]
            if missing:
                # Insert right after the turn's existing results — NOT at
                # the end of the list. When the dangling turn sits before
                # newer messages (a resumed session's fresh user prompt is
                # the common case), appending puts a tool result after a
                # user message, which is itself a provider 400.
                insert_at = i + 1
                while insert_at < len(messages) and messages[insert_at].role == "tool":
                    insert_at += 1
                for tc in missing:
                    messages.insert(
                        insert_at,
                        co.Message(
                            role="tool",
                            content=(
                                "This tool call never completed (the turn was "
                                "interrupted before a result was recorded). Do "
                                "not assume it succeeded; redo the work if needed."
                            ),
                            tool_call_id=tc.id,
                            name=tc.name,
                        ),
                    )
                    insert_at += 1
                    changed = True
            break
    return changed


def _malformed_tool_turn_index(messages: list[Any]) -> int | None:
    """Index of the newest assistant turn with a structurally broken tool call.

    Non-dict `arguments` (None / str / list — e.g. the model emitted
    `arguments: null`) is the one payload shape every provider rejects
    outright, so a turn carrying it is certainly the poison; detecting it
    beats guessing from position.
    """

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                if not isinstance(getattr(tc, "arguments", None), dict):
                    return i
    return None


def _cut_poisoned_turn(
    messages: list[Any],
    *,
    on_removed: Callable[[list[str]], None] | None = None,
) -> bool:
    """Excise ONE assistant tool-call turn plus its results, in place.

    coreouto examples/29 shape B: a malformed tool call sitting in the
    history (bad argument types, broken JSON, schema violation) makes the
    provider reject EVERY request that includes it, so no synthetic
    result can ever help — the offending assistant message AND its tool
    results must go (orphaned results are rejected too).

    The cut is precise: only the suspect turn and its own results are
    removed — later turns and, critically, the user's current prompt
    survive (the old `del messages[i:]` destroyed both, which is why a
    resumed session could never continue). Target: a turn with
    structurally malformed arguments when one exists, else the most
    recent tool-call turn (the statistically likely culprit — the turn
    that just ran). Repeated calls walk backwards through history, so
    each retry attempt strips one more suspect until the poison is gone.

    A note replaces the cut turn so the model knows what happened and
    continues with full context instead of repeating the call.

    `on_removed` receives the cut messages' refs, captured BEFORE the
    deletion. The note that replaces the turn is an ADDITION — folded
    into the preceding user message when there is one (some providers
    reject two consecutive user messages), inserted otherwise — and an
    in-place edit of a surviving message never prunes anything.
    """

    i = _malformed_tool_turn_index(messages)
    if i is None:
        for k in range(len(messages) - 1, -1, -1):
            msg = messages[k]
            if msg.role == "assistant" and msg.tool_calls:
                i = k
                break
    if i is None:
        return False
    j = i + 1
    while j < len(messages) and messages[j].role == "tool":
        j += 1
    if on_removed is not None:
        on_removed([str(id(m)) for m in messages[i:j]])
    del messages[i:j]
    explanation = (
        "A previous tool call was rejected by the provider (HTTP 400: the "
        "call itself was malformed), so that turn was removed from the "
        "history. Continue the task without repeating that call; re-issue "
        "it with valid arguments if the work is still needed."
    )
    # Fold into the preceding user message when possible — some providers
    # reject two consecutive user messages.
    prev = messages[i - 1] if i > 0 else None
    if prev is not None and prev.role == "user" and isinstance(prev.content, str):
        prev.content = f"{prev.content}\n\n{explanation}" if prev.content else explanation
    else:
        messages.insert(i, co.Message(role="user", content=explanation))
    return True


def _make_history_repair_hook(
    *,
    on_repair: Callable[[list[Any]], None] | None = None,
    on_removed: Callable[[list[str]], None] | None = None,
):
    """Build the ON_PROVIDER_ERROR hook that repairs history before retries.

    Pairs with `core.error_rules`: the 400/422 tool-call rules there use
    `reaction="retry"`, and coreouto fires ON_PROVIDER_ERROR before each
    retry attempt, then re-calls `provider.create()` with the CURRENT
    messages. This hook gets the live list and repairs it in place so the
    retry actually reaches the model instead of resending the poison.

    Per firing, two escalating passes:

    1. `_repair_pairing` — dedupe results, fill dangling calls. The
       gentle fix; if it changed anything, stop there and let the retry
       try the structurally valid payload.
    2. `_cut_poisoned_turn` — the payload is still (or inherently)
       malformed, so excise one suspect turn. Each retry cuts one turn
       deeper, so a poisoned turn buried under healthy ones is reached
       within the rule's `retry_max`.

    `on_repair` (optional) is called with the live messages whenever a
    pass changed them — `core.chat` uses it to persist the repaired
    history, so the fix survives the turn instead of being reloaded from
    disk (and re-400ing) on the next prompt.

    `on_removed` (optional) is called with the `str(id(message))` of
    every message the pass REMOVED, so `core.chat` can prune the display
    record beside the history fix. It is reported separately from
    `on_repair` because the two answer different questions: "what does
    the model now have" vs "what did the user just watch happen".

    NOTE on a deduped tool result: the dropped copy is reported, but its
    own ref matches no event — a `tool_result` is stamped with the
    ASSISTANT message's ref, not its own, so the prune that ref triggers
    removes nothing. The record keeps showing the turn's call and the one
    result that survived, which is exactly what the model now has. No row
    is orphaned, because a call and its result always share one ref.

    Gated to `reaction == "retry"` with status 400/422 so rate-limit and
    overload retries (which need no repair) pass through untouched.
    """

    def hook(
        *,
        status_code: int | None,
        reaction: str,
        messages: Any = None,
        **_kwargs: Any,
    ) -> None:
        if status_code not in (400, 422) or reaction != "retry" or messages is None:
            return
        changed = _repair_pairing(messages, on_removed=on_removed)
        if not changed:
            changed = _cut_poisoned_turn(messages, on_removed=on_removed)
        if changed and on_repair is not None:
            with contextlib.suppress(Exception):
                on_repair(messages)

    return hook


async def supervised_run(
    start: Callable[..., Any],
    prompt: str,
    history: list[co.Message] | None,
    *,
    live_key: str,
    on_wakeup: Callable[[int, float, Any], None],
    cancel_event: threading.Event | None = None,
    states: tuple[Any, Any, dict[str, Any]] | None = None,
    restart_on_repetition: str | None = None,
    on_repetition: Callable[[repetition.LoopVerdict, str], None] | None = None,
    on_removed: Callable[[list[str]], None] | None = None,
) -> Any:
    """Run `start(prompt, history)` under the stall watchdog (examples/27+28).

    `start(prompt, history)` must return the coroutine for one attempt;
    `start(None, history)` must resume from a transcript with no user
    message (coreouto >= 0.11.1). The SDK-level timeout
    (`API_STALL_TIMEOUT_SECONDS`, wired into every provider constructor)
    plus coreouto's TIMEOUT_ERRORS rules already turn a hung HTTP call
    into a retry; this supervisor is the layer above, catching everything
    that is NOT an in-flight HTTP request — a loop wedged between
    operations, a deadlocked hook, a stalled call the SDK never flagged.

    When nothing has happened for the stall limit AND the loop is not
    inside a tool (tools carry their own bounds — Bash has
    BASH_TIMEOUT_SECONDS), the attempt task is cancelled, the actor's live
    message list is sanitized, and the call restarts — transcript-only when
    anything is recoverable, the original prompt otherwise. Up to
    WATCHDOG_MAX_WAKEUPS restarts, then LoopStalledError.
    The `cancel_event` fast path makes ESC ESC responsive during an
    in-flight LLM call, which the hook-boundary cancel guard alone cannot
    interrupt.

    REPETITION (`restart_on_repetition` = the actor key, "outo").
    A repeating model is active, not stalled, so this check runs BEFORE
    the stall test and has its own budget (REPEAT_MAX_RESTARTS, unrelated
    to WATCHDOG_MAX_WAKEUPS) and its own terminal error
    (LoopRepeatedError). It is per-actor, and only this supervisor consumes
    its actor's verdict.

    ENFORCEMENT IS EVENT-DRIVEN AND COVERS THE COMPLETION PATH. A verdict
    trips at note time, so this supervisor subscribes to it with
    `repetition.set_trip_notifier` (for the duration of the run) and waits on
    a `trip.wait()` task alongside the attempt: the force-stop is immediate
    instead of arriving with the next WATCHDOG_CHECK_SECONDS poll, which
    stays the stall check's cadence. `call_soon_threadsafe` is required —
    a verdict may be noted from a thread that is not the event loop's.

    The verdict is also consulted when the attempt task is ALREADY DONE.
    The bug that forces this: coreouto fires ON_ITERATION (where the reply
    text is noted) and then terminates on a content-only response, so a
    final answer that is ITSELF the repetition trips the detector
    microseconds before the task completes — checking `task.done()` first
    would return that garbage answer and let the next turn's `reset()` wipe
    the verdict. So a completed-but-repeating attempt is DISCARDED and
    resumed like a force-stopped one; the garbage answer never reaches
    `run_chat`. A crashed attempt is exempt (its verdict is left unconsumed):
    a repetition stop must never mask a real provider error.

    WHY THE TWO RESTART PATHS DIFFER. The stall path resumes with
    `start(None, history)`: nothing is wrong, the transport just hung, and
    the transcript alone tells the model to carry on. A repetition stop
    needs the model to CHANGE ITS BEHAVIOUR, and a bare transcript cannot
    say that — so the alarm is delivered as the resumed attempt's user
    message (`start(alarm, history)`), which also lands in
    `Response.messages` and therefore in the persisted session history.
    It is deliberately NOT routed through the inbox: a `drain()` by
    `_make_alarm_hook` would deliver the same text a second time.
    `on_repetition` covers the display side (one `alarm` loop event on the
    turn's sink).

    `on_removed` is the display side of BOTH restart paths: every
    `sanitize_history` truncation it performs reports the discarded
    messages' refs there, so the record loses the events of a turn the
    resumed attempt no longer has. The two paths differ in what survives
    — a stall keeps the answered prefix, a repetition restart keeps it
    plus the alarm — but a discarded tail is a discard on both.
    """

    if states is None:
        states = watchdog_states()
    activity, progress, live = states
    if activity is None or progress is None:
        return await start(prompt, history)

    token = object()
    _ACTIVE_SUPERVISORS[token] = 0
    loop = asyncio.get_running_loop()
    trip = asyncio.Event()
    previous_notifier = repetition._TRIP_NOTIFIER
    waiter: asyncio.Task[bool] | None = None

    def on_trip(actor_key: str) -> None:
        if actor_key == restart_on_repetition:
            loop.call_soon_threadsafe(trip.set)

    def take_verdict() -> repetition.LoopVerdict | None:
        # PEEK before consuming: `consume_verdict` pops the actor's counters
        # even when it returns None, so an unconditional poll erases the
        # counts every WATCHDOG_CHECK_SECONDS and a loop slower than the poll
        # interval (5s in production) can never reach a threshold.
        if repetition.check(restart_on_repetition) is None:
            return None
        return repetition.consume_verdict(restart_on_repetition)

    repetition.set_trip_notifier(on_trip)
    try:
        task = asyncio.create_task(start(prompt, history))
        wakeups = 0
        restarts = 0
        waiter = asyncio.create_task(trip.wait())

        def resume_with_alarm(verdict: repetition.LoopVerdict) -> asyncio.Task[Any]:
            """The one repetition restart sequence, shared by both branches."""
            nonlocal history, restarts
            restarts += 1
            if restarts > repetition.REPEAT_MAX_RESTARTS:
                raise LoopRepeatedError(
                    f"loop is repeating itself — {verdict.kind}: "
                    f"{verdict.detail} — after {repetition.REPEAT_MAX_RESTARTS} "
                    f"restarts, giving up"
                )
            alarm = repetition.alarm_text(verdict)
            history = (
                sanitize_history(live.get(live_key), on_removed=on_removed) or history
            )
            if on_repetition is not None:
                with contextlib.suppress(Exception):
                    on_repetition(verdict, alarm)
            # No recoverable transcript means the loop tripped before its
            # first LLM call finished — the alarm alone would lose the task,
            # so it rides along with the original prompt.
            return asyncio.create_task(
                start(alarm if history else f"{prompt}\n\n{alarm}", history)
            )

        while True:
            # `asyncio.wait` returns as soon as EITHER the attempt finishes or
            # a verdict trips; the timeout is the stall check's cadence only.
            # A bare `sleep` would make every turn pay a full poll interval
            # before its own result could be returned.
            await asyncio.wait(
                {task, waiter}, timeout=WATCHDOG_CHECK_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
            if task.done():
                crashed = task.cancelled() or task.exception() is not None
                verdict = None
                if restart_on_repetition is not None and not crashed:
                    verdict = take_verdict()
                if verdict is None:
                    return task.result()
                # A repeating final answer: discard it and resume.
                task = resume_with_alarm(verdict)
                trip.clear()
                waiter = asyncio.create_task(trip.wait())
                continue
            trip.clear()
            waiter = asyncio.create_task(trip.wait())
            if cancel_event is not None and cancel_event.is_set():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise LoopCancelledError("stopped by user")
            if restart_on_repetition is not None:
                verdict = take_verdict()
                if verdict is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    task = resume_with_alarm(verdict)
                    continue
            silent = activity.seconds_since_last_activity()
            if silent <= API_STALL_TIMEOUT_SECONDS or progress.phase == "tool_call":
                continue

            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            wakeups += 1
            if wakeups > WATCHDOG_MAX_WAKEUPS:
                raise LoopStalledError(
                    f"loop stalled repeatedly ({WATCHDOG_MAX_WAKEUPS} wakeups, "
                    f"last phase={progress.phase!r}) — giving up"
                )
            history = sanitize_history(live.get(live_key), on_removed=on_removed) or history
            # The wedged tail is discarded, not recorded, so its phrases must
            # not count against the resumed attempt. The repetition restart
            # gets this for free (`consume_verdict` pops the whole actor).
            if restart_on_repetition is not None:
                repetition.reset_phrases(restart_on_repetition)
            on_wakeup(wakeups, silent, progress.phase)
            if history:
                task = asyncio.create_task(start(None, history))
            else:
                # Nothing recoverable (wedged before the first LLM call) —
                # start over with the original prompt.
                task = asyncio.create_task(start(prompt, history))
    finally:
        repetition.set_trip_notifier(previous_notifier)
        if waiter is not None:
            waiter.cancel()
        _ACTIVE_SUPERVISORS.pop(token, None)


class LoopCancelledError(Exception):
    """Raised from a hook when the caller's cancel event is set.

    coreouto's hook `trigger` does not swallow exceptions, so raising here
    propagates out of `agent.call_sync` and terminates the loop at the next
    hook boundary (before the next LLM call or tool execution).
    """


def _make_cancel_guard(cancel_event: threading.Event) -> Callable[..., None]:
    """Build a hook that raises LoopCancelledError once the event is set."""

    def _guard(**kwargs: Any) -> None:
        if cancel_event.is_set():
            raise LoopCancelledError("stopped by user")

    return _guard


def _make_alarm_hook(
    session: str | None,
    on_messages: Callable[[list[inbox.InboxMessage]], None] | None = None,
) -> Callable[..., None]:
    """Build the BEFORE_LLM_CALL hook that drains the cross-process inbox.

    A detached `chat --bg` process that finished drops one JSON file per message
    into the session's inbox directory. Every iteration this hook drains it
    and, if anything came back, appends the texts to the LIVE `messages` list
    as ONE synthetic user message (joined with a blank line), so the very next
    `provider.create` already carries them.

    Appended directly, never through `Agent.inject_user_message`: that queue
    is drained at the TOP of the loop iteration (coreouto agent.py:301-307),
    i.e. BEFORE this hook runs, so a message queued from here would only land
    a full LLM round-trip later.

    FOLD RULE: if the last message is already a user message, the text is
    folded into it instead of appended — providers reject two consecutive
    user messages, the same hazard `_cut_poisoned_turn` works around.

    `on_messages` (from `core.chat`) records one loop event per drained
    message so the session and the TUI show it; a broken sink must not break
    the provider path, so the whole body runs under `contextlib.suppress`.
    """

    def hook(*, messages: Any = None, **_kwargs: Any) -> None:
        if messages is None:
            return
        with contextlib.suppress(Exception):
            batch = inbox.drain(session or "default")
            if not batch:
                return
            text = "\n\n".join(message.text for message in batch)
            last = messages[-1] if messages else None
            if last is not None and last.role == "user" and isinstance(last.content, str):
                last.content = f"{last.content}\n\n{text}" if last.content else text
            else:
                messages.append(co.Message(role="user", content=text))
            if on_messages is not None:
                on_messages(batch)

    return hook


@dataclass
class RuntimeConfig:
    provider_name: str
    model: str
    style_name: str
    # Role mode: run AS one of the style's named subagent specs instead of as
    # outo. Set by `miniouto chat --role`; None = the interactive outo run.
    role: str | None = None
    subagent_model: str | None = None
    subagent_provider: str | None = None
    subagent_reasoning: str | None = None
    # The session pin's outo reasoning (storage/session_config.py). A
    # per-call `reasoning` argument to build_runtime still wins; this is
    # the lane between it and the provider's own `reasoning_effort`.
    reasoning: str | None = None
    session: str | None = None


@dataclass
class ChatOverrides:
    provider: str | None = None
    model: str | None = None
    style: str | None = None
    role: str | None = None
    subagent_provider: str | None = None
    subagent_model: str | None = None


def build_runtime(
    runtime: RuntimeConfig,
    *,
    style_overrides: dict[str, str] | None = None,
    provider_config: dict[str, Any] | None = None,
    on_tool_call: Callable[[str, dict[str, Any]], None] | None = None,
    on_tool_result: Callable[[str, Any], None] | None = None,
    on_response: Callable[[str, bool], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_stream_text: Callable[[str], None] | None = None,
    on_stream_thinking: Callable[[str], None] | None = None,
    on_iteration: Callable[..., None] | None = None,
    on_provider_error: Callable[..., None] | None = None,
    on_history_repair: Callable[[list[Any]], None] | None = None,
    on_messages_removed: Callable[[list[str]], None] | None = None,
    on_inbox_messages: Callable[[list[inbox.InboxMessage]], None] | None = None,
    reasoning: str | None = None,
    cancel_event: threading.Event | None = None,
) -> co.Agent:
    """Construct the outo Agent from the active style (or, with `role`, a role spec).

    The style-derived prompt text — the outo body AND every role (`--role`)
    body — is token-expanded via `expand_prompt_tokens` against
    `runtime.session` and `runtime.style_name`. That expansion is the WHOLE
    system prompt: nothing (no cwd preamble, no skill catalog) is prepended
    implicitly, so a style decides for itself whether and where to place
    `{cwd}` and `{skills}`.

    `provider_config` is merged into the outo Agent's `provider_config` so that
    canonical settings (max_tokens, temperature, ...) flow through coreouto's
    normalizer without us having to mutate the config after construction.
    `on_tool_call` is called for every tool invocation with
    `(tool_name, arguments)`. Pass None to skip.
    `on_tool_result` is called after every tool handler returns (coreouto's
    AFTER_TOOL_CALL) with `(tool_name, result)` where result is coreouto's
    ToolResult — this is the only way to surface tool return values to a
    sink, since the BEFORE_TOOL_CALL hook fires before the handler runs.
    Pass None to skip.
    `on_response` is called after each LLM response with `(content, has_tool_calls)`
    so the caller can stream intermediate model text. Pass None to skip.
    `on_thinking` is called after each LLM response that carries reasoning
    text (coreouto's ON_THINKING; providers never put thinking into history
    messages, so this hook is the only way to surface it). Pass None to skip.
    `on_stream_text` / `on_stream_thinking` are called once per incremental
    fragment as the provider streams (coreouto's ON_STREAM_TEXT /
    ON_STREAM_THINKING) — display-only, so the whole message is what a sink
    should record. Pass None to skip.
    `on_iteration` is called after each agent-loop iteration with the same
    kwargs coreouto passes to ON_ITERATION (iteration, messages, response),
    letting the caller stream loop-progress signals. Pass None to skip.
    `on_provider_error` is called whenever an `error_handling` rule matches a
    provider exception (coreouto >= 0.10), with `status_code`, `error_message`,
    `reaction`, `reaction_message` kwargs. Rule-matched errors don't raise, so
    without this hook a retry or termination is invisible to the user.
    Pass None to skip.
    `on_history_repair` is called with the live message list whenever the
    history-repair hook actually mutates it before a 400/422 retry —
    `core.chat` persists the repaired history through it so the fix
    survives the turn. Pass None to skip persistence.
    `on_messages_removed` is called with the `str(id(message))` of every
    message a repair REMOVED, beside (and independently of)
    `on_history_repair`: it lets `core.chat` prune the display record so
    the turn record describes exactly the messages that survived.
    Additions (a filled-in tool result, the explanation note folded into
    the preceding user message) are never reported. Pass None to skip.
    `on_inbox_messages` is called with every message the BEFORE_LLM_CALL
    inbox hook drained, after appending it to the live message list —
    `core.chat` turns each into a loop event (`alarm` or `user`, see
    `inbox.ALARM_KIND` / `inbox.USER_KIND`) so the session and the TUI show
    what the spawned processes reported. Pass None to skip the display
    only; the messages are still delivered to the model.
    `reasoning` overrides reasoning for this call (effort level / "on" /
    token budget / "none"). None = auto: the provider's stored
    `reasoning_effort` wins if set, otherwise lma's per-model default.
    Resolved by `core.reasoning.resolve_reasoning_passthrough` into
    provider-native kwargs merged into `provider_passthrough` — this is
    what makes providers return thinking blocks at all.
    `cancel_event` (optional) is a threading.Event polled at every
    BEFORE_LLM_CALL / BEFORE_TOOL_CALL hook; once set, the hook raises
    LoopCancelledError, terminating the loop at the next hook boundary.
    An in-flight Bash call IS killed (tools/bash.py polls the same event —
    its 1-hour hard cap, BASH_TIMEOUT_SECONDS, is far too slow to serve as
    the stop mechanism), and the turn watchdog in core/chat.py polls the
    event every 5 s to hard-cancel even an in-flight LLM call. Other
    in-flight tools finish — the loop stops cooperatively before the
    next step.
    """

    from .context import make_context_monitor
    from .reasoning import resolve_reasoning_passthrough

    # ---- Slow resolution, hoisted ABOVE `clear_coreouto_state()` ----
    # Everything below this point must be pure in-memory work: the clear
    # wipes coreouto's global provider/preset/tool/hook registries, so a
    # multi-second network fetch or file read inside the clear+register
    # window leaves them empty for as long as it takes. Style file I/O and
    # both lma lookups therefore resolve first; only dict/registry
    # operations follow the clear.
    provider = provider_store.get(runtime.provider_name)
    if provider is None:
        raise RuntimeError(
            f"Provider {runtime.provider_name!r} is not configured. "
            "Run `miniouto provider add` first."
        )

    outo_style, parsed_specs = _resolve_both_styles(runtime.style_name, style_overrides)
    # `{this_session}` resolves to the SAME session value the inbox hook
    # drains below (`runtime.session`, defaulting to "default"), so a style
    # can teach a delegation command with a literal `--alarm-session`.
    session_for_tokens = runtime.session or "default"
    outo_prompt = expand_prompt_tokens(
        outo_style, session=session_for_tokens, style=runtime.style_name
    )
    specs = [
        SubagentSpec(
            name=spec.name,
            prompt=expand_prompt_tokens(
                spec.prompt, session=session_for_tokens, style=runtime.style_name
            ),
        )
        for spec in parsed_specs
    ]

    # ROLE MODE (`miniouto chat --role <name>`). The spawned process runs
    # the SAME agent on the SAME preset with the SAME tools; only the system
    # prompt changes. The prompt is one of the `specs` above — expanded
    # through the same single token pass as the outo body — so there is no
    # second assembly path to drift.
    system_prompt = outo_prompt
    if runtime.role:
        role_spec = next((s for s in specs if s.name == runtime.role), None)
        if role_spec is None:
            raise ValueError(
                f"Unknown role {runtime.role!r}. "
                f"Available roles: {', '.join(s.name for s in specs) or '(none)'}"
            )
        system_prompt = role_spec.prompt

    # Thinking display (ON_THINKING → CLI/TUI) only works when the model is
    # asked to reason — providers never emit thinking blocks unrequested.
    # The resolver consults lma's per-model reasoning_options; an explicit
    # `reasoning` choice wins over the session pin (runtime.reasoning),
    # which in turn wins over the provider's stored reasoning_effort.
    outo_choice = reasoning if reasoning is not None else (runtime.reasoning or provider.reasoning_effort)
    outo_passthrough = resolve_reasoning_passthrough(
        provider.api_format,
        runtime.model,
        runtime.provider_name,
        outo_choice,
    )

    # The context monitor resolves the window size once (lma, unless a
    # provider override supplies it) — above the clear, hooked below.
    context_monitor = make_context_monitor(
        model=runtime.model,
        session=runtime.session or "default",
        provider_name=runtime.provider_name,
    )

    # ---- Registry rebuild: pure in-memory from here to the return ----
    clear_coreouto_state()
    bash_tool.set_cancel_event(cancel_event)

    if cancel_event is not None:
        # Register first so the guard runs before any logging hook on the
        # same event — a cancelled tool call should never be logged as if
        # it were about to run.
        guard = _make_cancel_guard(cancel_event)
        co.register_hook(co.BEFORE_LLM_CALL, guard)
        co.register_hook(co.BEFORE_TOOL_CALL, guard)

    build_coreouto_provider(provider)

    # api_format selects the Computer handler variant: openai Chat
    # Completions cannot carry image tool results, so screenshot there
    # returns an explanatory notice instead of an image (see
    # tools/registry._make_computer_handler).
    tool_registry.register_all(api_format=provider.api_format)

    # ONE preset, one tool list — role mode included: a spawned run differs
    # only in its system prompt. PLATFORM_GATED_TOOLS is filtered by
    # registration, so unsupported platforms never see Computer in either mode.
    outo_tools: list[str] = list(BASE_TOOLS)
    outo_tools.extend(t for t in PLATFORM_GATED_TOOLS if co.get_tool(t) is not None)

    co.register_agent_preset(
        "outo",
        model=runtime.model,
        provider=runtime.provider_name,
        system_prompt=system_prompt,
        tools=outo_tools,
        max_iterations=None,
    )

    if on_tool_call is not None:
        co.register_hook(co.BEFORE_TOOL_CALL, _make_tool_call_logger(on_tool_call))

    if on_tool_result is not None:
        co.register_hook(co.AFTER_TOOL_CALL, _make_tool_result_logger(on_tool_result))

    # Nothing is ever summarized implicitly. At the usage threshold the
    # monitor inserts ONE summary request into the session inbox; the alarm
    # hook below delivers it to the model as a user message.
    if context_monitor is not None:
        co.register_hook(co.ON_ITERATION, context_monitor)

    # Unconditional (like the repair hook): a job that finished between
    # turns has no other way to reach the model. Registered before the
    # watchdog's capture hook below, so the live message list the
    # supervisor holds already carries the delivered alarms.
    co.register_hook(
        co.BEFORE_LLM_CALL, _make_alarm_hook(runtime.session, on_inbox_messages)
    )

    _register_watchdog_trackers()

    # Unconditional (unlike the sink loggers): repair makes the retry rules recover.
    co.register_hook(
        co.ON_PROVIDER_ERROR,
        _make_history_repair_hook(
            on_repair=on_history_repair, on_removed=on_messages_removed
        ),
    )

    if on_response is not None:
        co.register_hook(co.AFTER_LLM_CALL, _make_response_logger(on_response))

    if on_thinking is not None:
        co.register_hook(co.ON_THINKING, _make_thinking_logger(on_thinking))

    # Registered in addition to the watchdog activity tracker's own
    # registrations for these two events: display deltas are the user's
    # only view of a message while it is still being generated.
    if on_stream_text is not None:
        co.register_hook(co.ON_STREAM_TEXT, _make_stream_logger(on_stream_text))

    if on_stream_thinking is not None:
        co.register_hook(co.ON_STREAM_THINKING, _make_stream_logger(on_stream_thinking))

    if on_iteration is not None:
        co.register_hook(co.ON_ITERATION, _make_iteration_logger(on_iteration))

    if on_provider_error is not None:
        co.register_hook(co.ON_PROVIDER_ERROR, _make_provider_error_logger(on_provider_error))

    outo_config = co.get_agent_preset("outo").to_config()
    if provider_config:
        outo_config.provider_config.update(provider_config)
    if outo_passthrough:
        outo_config.provider_passthrough.update(outo_passthrough)

    return co.Agent(outo_config)


def _register_watchdog_trackers() -> None:
    """Install the coreouto contrib tracker hooks the loop watchdog reads.

    Follows coreouto examples/27_wakeup.py: the activity tracker resets on
    every sign of life (LLM responses, tool results, iterations, provider
    retries, stream chunks — miniouto always streams, so a long healthy
    stream must count as activity), the progress tracker records which
    phase the loop is in, and the BEFORE_LLM_CALL capture keeps a
    reference to the live message list (under the literal key "outo") so
    the supervisor can recover history after cancelling a wedged call.
    The dict is cleared on every call, so it never grows across turns.
    """

    global _ACTIVITY_STATE, _PROGRESS_STATE
    from coreouto.contrib.hooks import activity_tracker_hook, loop_progress_hook

    activity_hook, _ACTIVITY_STATE = activity_tracker_hook()
    for event in (
        co.AFTER_LLM_CALL,
        co.AFTER_TOOL_CALL,
        co.ON_ITERATION,
        co.ON_PROVIDER_ERROR,
        co.ON_STREAM_TEXT,
        co.ON_STREAM_THINKING,
    ):
        co.register_hook(event, activity_hook)

    progress_hooks, _PROGRESS_STATE = loop_progress_hook()
    for event, fn in progress_hooks.items():
        co.register_hook(event, fn)

    _LIVE_MESSAGES.clear()
    # Same lifecycle: a repetition counter is scoped to one turn, so a
    # verdict from the previous turn can never stop the next one.
    repetition.reset()

    def capture_messages(*, messages: Any, **_kwargs: Any) -> None:
        _LIVE_MESSAGES["outo"] = messages

    co.register_hook(co.BEFORE_LLM_CALL, capture_messages)


def _make_tool_call_logger(callback: Callable[[str, dict[str, Any]], None]):
    def hook(*, name: str, arguments: dict[str, Any], **kwargs: Any) -> None:
        callback(name, arguments)

    return hook


def _make_tool_result_logger(callback: Callable[[str, Any], None]):
    """Build an AFTER_TOOL_CALL hook that forwards the tool's return value.

    coreouto fires AFTER_TOOL_CALL after each tool handler completes with
    `(name, result)` where result is a ToolResult (`.content`, `.blocks`,
    `.is_error`, `.flatten_text()`). Only the name and the result object
    are forwarded — everything else in the payload is already available
    from the BEFORE_TOOL_CALL hook.
    """

    def hook(*, name: str, result: Any, **kwargs: Any) -> None:
        callback(name, result)

    return hook


def _make_provider_error_logger(callback: Callable[..., None]):
    """Build an ON_PROVIDER_ERROR hook that forwards the rule-match payload.

    coreouto fires ON_PROVIDER_ERROR after an `error_handling` rule matches
    a provider exception, before executing the reaction. Only the four
    renderable fields are forwarded — the raw exception and the full
    message list are also in the payload but are useless to a sink.
    """

    def hook(
        *,
        status_code: int | None,
        error_message: str,
        reaction: str,
        reaction_message: str,
        **kwargs: Any,
    ) -> None:
        callback(
            status_code=status_code,
            error_message=error_message,
            reaction=reaction,
            reaction_message=reaction_message,
        )

    return hook


def _make_iteration_logger(callback: Callable[..., None]):
    """Build an ON_ITERATION hook that forwards coreouto's kwargs verbatim.

    coreouto fires ON_ITERATION after each agent-loop iteration with
    `(iteration, messages, response)`. Forwarding them as-is lets the
    caller stream loop-progress signals without us binding the public
    hook signature to a particular coreouto version.
    """

    def hook(*, iteration: int, messages: Any, response: Any, **kwargs: Any) -> None:
        callback(iteration=iteration, messages=messages, response=response)

    return hook


def _make_response_logger(
    callback: Callable[[str, bool], None],
):
    """Build the AFTER_LLM_CALL hook that streams intermediate model text.

    `callback(content, has_tool_calls)` receives the full response text and
    a flag indicating whether the model emitted tool calls in this turn.
    Final responses (no tool calls) are flagged so the caller can skip them
    — the terminal answer is rendered separately by the sink.
    """

    def hook(*, response: Any, messages: Any, **kwargs: Any) -> None:
        if not response or not hasattr(response, "content") or not response.content:
            return
        tool_calls = getattr(response, "tool_calls", None) or []
        callback(response.content, bool(tool_calls))

    return hook


def _make_stream_logger(callback: Callable[[str], None]):
    """Build the ON_STREAM_TEXT / ON_STREAM_THINKING hook forwarding one delta.

    coreouto fires these per incremental fragment (not the accumulated
    text) with `(text, messages, model)`; only `text` reaches the caller.
    """

    def hook(*, text: str, **_kwargs: Any) -> None:
        if text:
            callback(text)

    return hook


def _make_thinking_logger(callback: Callable[[str], None]):
    """Build the ON_THINKING hook that streams reasoning text.

    coreouto fires ON_THINKING once per LLM response that carries thinking
    (Anthropic extended thinking, OpenAI reasoning summaries).
    """

    def hook(*, thinking: str, **kwargs: Any) -> None:
        if thinking:
            callback(thinking)

    return hook


def _resolve_both_styles(
    style_name: str, overrides: dict[str, str] | None
) -> tuple[str, list[SubagentSpec]]:
    """Return (outo_prompt, specs) from the active style document.

    The parser (`storage.styles.parse_style`) yields one `SubagentSpec` per
    named tag — `<editor>...</editor>` becomes a spec named `editor`, the
    legacy `<subagent>...</subagent>` becomes a spec named `subagent`,
    and a style with no other tags yields zero specs. A spec is a ROLE:
    `build_runtime` selects one of them as the system prompt when the run
    was started with `miniouto chat --role <name>`.

    Active skills and the working directory are NOT prepended here: a style
    body places them with the `{cwd}` / `{skills}` tokens, which
    `build_runtime` expands (a runtime-derived value must never be baked into
    the style loader). An empty/whitespace spec body falls back to
    `_fallback_style("subagent")` content as that spec's prompt.
    """

    raw = _read_raw_style(style_name, overrides)
    outo_part, parsed_specs = style_store.parse_style(raw)

    fallback_sub = _fallback_style("subagent")
    specs: list[SubagentSpec] = []
    for spec in parsed_specs:
        body = spec.prompt.strip() or fallback_sub
        specs.append(SubagentSpec(name=spec.name, prompt=body))

    return outo_part, specs


def _read_raw_style(name: str, overrides: dict[str, str] | None) -> str:
    if overrides and name in overrides:
        return overrides[name]
    content = style_store.read(name)
    if content is None:
        if name == "default":
            return style_store.builtin_default() or _fallback_style("outo")
        return _fallback_style(name)
    return content


_SKILLS_TOKEN = re.compile(r"\{skills(?::([^}]*))?\}")


def expand_prompt_tokens(text: str, *, session: str, style: str) -> str:
    """Substitute the style tokens into a style body.

    `{cwd}` becomes the invocation directory, `{skills}` (or
    `{skills:dir,dir}`) the lazy-load skill catalog, `{this_session}` the
    run's own session name, `{style}` the resolved style name, and
    `{subagent_pm}` the EFFECTIVE subagent provider config rendered in the
    `--pm` spelling (`PROVIDER/MODEL[:REASONING]`) — so a style can teach a
    delegation command whose values vary per run while the model still copies
    a fully literal command (Bash idempotency) — it never sees the token
    syntax. Expansion is plain `str.replace` (plus one regex pass for the
    parameterized `{skills:...}` form) on the style-derived body, applied to
    the outo body AND every role body. Any other `{...}` text is left
    verbatim — there is no escape mechanism and an unknown token is not an
    error.

    The catalog is a *catalog*: skill bodies are never injected, the model
    reads the matching SKILL.md via Bash when a task calls for it.
    """

    text = _SKILLS_TOKEN.sub(_replace_skills_token, text)
    text = text.replace("{cwd}", str(INVOCATION_CWD))
    text = text.replace("{this_session}", session).replace("{style}", style)
    if "{subagent_pm}" not in text:
        return text
    subagent_pm = _effective_subagent_pm(session)
    # An unresolvable provider/model leaves the token LITERAL (same rule as
    # any unknown token) — a half-filled `--pm` would be a malformed command
    # for the model to copy.
    return text.replace("{subagent_pm}", subagent_pm) if subagent_pm else text


def _replace_skills_token(match: re.Match[str]) -> str:
    return _skills_catalog(_resolve_skill_roots(match.group(1)))


def _resolve_skill_roots(arg: str | None) -> list[Path]:
    """The skill roots a `{skills...}` token names, in the order listed.

    An empty argument (bare `{skills}` or `{skills:}`) means the default roots
    — the invocation directory's `./skills/` FIRST, then `~/.agents/skills/`,
    so project-local skills take precedence in the catalog. `~` is expanded; a
    relative path resolves against INVOCATION_CWD, so a style can point at
    `./skills`. Duplicates are kept — a style listing the same root twice is its
    own business.
    """

    raw = [part.strip() for part in (arg or "").split(",") if part.strip()]
    if not raw:
        return skill_store.default_roots(INVOCATION_CWD)
    roots = [Path(part).expanduser() for part in raw]
    return [root if root.is_absolute() else INVOCATION_CWD / root for root in roots]


def _skills_catalog(roots: list[Path]) -> str:
    """Render the lazy-load skill catalog for `roots`, or "" if none exist.

    Ordering is (root order as listed, then name within a root), because a
    style listing several roots states a precedence. A root that yields
    nothing — missing, unreadable, or no visible SKILL.md — is dropped from
    the catalog entirely rather than advertised to the model. A name present
    in two roots appears twice: the two entries live at distinct paths, and
    each line names the root it came from when there is more than one.
    """

    entries = [(skill, root) for root in roots for skill in skill_store.list_skills(root)]
    if not entries:
        return ""
    listed = list(dict.fromkeys(root for _, root in entries))
    where = " or ".join(f"{root}/<name>/" for root in listed)
    lines = [
        "# Available Skills",
        "",
        "The following skills are installed. Each lives in its own directory at "
        f"{where} containing a SKILL.md with "
        "instructions plus any supporting files it references. When a task "
        "matches a skill's description, read that skill's SKILL.md (and the "
        "files it references) via Bash before proceeding — skill instructions "
        "take precedence over the default workflow.",
        "",
    ]
    for skill, root in entries:
        origin = f"  ({root})" if len(listed) > 1 else ""
        lines.append(f"- {skill.name}: {skill.description}{origin}")
    return "\n".join(lines)


def _effective_subagent_pm(session: str) -> str:
    """The effective subagent trio as a `--pm` value, or "" if unresolvable.

    Mirrors `_resolve_subagent`'s "same as outo" semantics, per key: the
    session pin's subagent trio, then the global default's subagent trio
    (`settings.subagent_*`), then the effective OUTO trio for any key still
    empty (the pin's outo keys, then the global outo default) — which is
    what `chat --use-subagent-provider` would resolve to if the run that
    copies the command were the pinned one. A pin read with
    `session_config.read` (never `effective`, which would freeze it): the
    token must not have the side effect of writing a pin file.
    """

    pinned = session_config.read(session) or session_config.EMPTY
    default = default_provider_config()
    provider = (
        pinned.subagent_provider
        or default.subagent_provider
        or pinned.provider
        or default.provider
    )
    model = pinned.subagent_model or default.subagent_model or pinned.model or default.model
    reasoning = (
        pinned.subagent_reasoning
        or default.subagent_reasoning
        or pinned.reasoning
        or default.reasoning
    )
    if not provider or not model:
        return ""
    suffix = f":{reasoning}" if reasoning else ""
    return f"{provider}/{model}{suffix}"


def _fallback_style(name: str) -> str:
    if name == "subagent":
        return (
            "You are subagent. Execute the brief directly using your tools. "
            "To finish, respond with text and no tool call — that text becomes "
            "the final answer returned to the parent. Use the `continue_loop` "
            "tool if you need to send text to the parent while still planning "
            "more tool calls."
        )
    return (
        f"You are {name}. To finish, respond with text and no tool call — "
        "that text becomes the final answer returned to the user. Use the "
        "`continue_loop` tool if you need to share progress while still "
        "planning more tool calls."
    )


def default_provider_config() -> session_config.ProviderConfig:
    """The GLOBAL provider settings a session with no pin falls back to.

    Mirrors the pre-pin fallback chain exactly: `settings.provider` /
    `settings.model`, the resolved provider record's `default_model` /
    `reasoning_effort`, and the `subagent_*` trio (which stays empty when
    unset — empty means "inherit outo"). Lives here, not in
    `storage.session_config`, because storage is the disk layer and must
    not import core.
    """

    s = settings_store.load()
    provider = provider_store.get(s.provider) if s.provider else None
    return session_config.ProviderConfig(
        provider=s.provider,
        model=s.model or (provider.default_model if provider else ""),
        reasoning=(provider.reasoning_effort or "") if provider else "",
        subagent_provider=s.subagent_provider,
        subagent_model=s.subagent_model,
        subagent_reasoning=s.subagent_reasoning,
    )


def _pinned_for(s: settings_store.Settings) -> session_config.ProviderConfig:
    """The run's session pin, read (and frozen) above the registry rebuild.

    The session a turn resolves IS `settings.session` — the same value the
    inbox hook drains and `{this_session}` expands — so this is where the
    delegated-run borrow (`cli/chat.py:_runtime_session_scope`) takes
    effect.
    """

    return session_config.effective(
        s.session or "default", default=default_provider_config
    )


def resolve_runtime_from_settings(overrides: ChatOverrides | None = None) -> RuntimeConfig:
    s = settings_store.load()
    overrides = overrides or ChatOverrides()

    # Lane 2 of the resolution order (see storage/session_config): explicit
    # overrides > this session's pin > the global default. An override that
    # moves outo to ANOTHER provider invalidates the pinned model/reasoning
    # — those belong to the pinned provider — so the whole outo trio is
    # dropped and the global chain answers for the override's provider.
    pinned = _pinned_for(s)
    if overrides.provider and overrides.provider != pinned.provider:
        pinned = dataclasses.replace(pinned, provider="", model="", reasoning="")

    provider_name = overrides.provider or pinned.provider or s.provider
    if not provider_name:
        raise RuntimeError(
            "No default provider set. Run `miniouto provider add` and "
            "`miniouto provider default <name>` first."
        )
    provider = provider_store.get(provider_name)
    if provider is None:
        raise RuntimeError(f"Provider {provider_name!r} is not configured.")

    model = overrides.model or pinned.model or provider.default_model
    if not model:
        raise RuntimeError(
            f"No model specified for provider {provider_name!r}. "
            "Pass --model on the chat command, or set a default via "
            "`miniouto provider add --default-model <name>`."
        )

    subagent_provider, subagent_model = _resolve_subagent(overrides, s, pinned)

    return RuntimeConfig(
        provider_name=provider_name,
        model=model,
        style_name=overrides.style or s.style or "default",
        role=overrides.role or None,
        session=s.session or "default",
        reasoning=pinned.reasoning or None,
        subagent_provider=subagent_provider,
        subagent_model=subagent_model,
        subagent_reasoning=pinned.subagent_reasoning or s.subagent_reasoning or None,
    )


def _resolve_subagent(
    overrides: ChatOverrides,
    s: settings_store.Settings,
    pinned: session_config.ProviderConfig | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the provider/model a SPAWNED run should use.

    Kept as a standalone helper (build_runtime no longer needs it) because
    the spawn CLI resolves it while assembling the child process's
    RuntimeConfig.

    Provider precedence: CLI override > this session's pin > settings >
    None (None = inherit outo's provider). Model precedence: CLI override >
    pin > settings > subagent provider's default_model > None (None =
    inherit outo's model). A model-only override (no subagent provider) is
    valid: the spawned run then uses outo's provider with a different
    model. `pinned` is taken as an argument so the caller resolves the pin
    once per turn; omitted, it is read from `s.session` so a direct
    `--use-subagent-provider` call honours the same pin.
    """

    if pinned is None:
        pinned = _pinned_for(s)

    subagent_provider = (
        overrides.subagent_provider or pinned.subagent_provider or s.subagent_provider or None
    )
    subagent_model = overrides.subagent_model or pinned.subagent_model or s.subagent_model or None
    if subagent_provider is None:
        return None, subagent_model

    sub_provider = provider_store.get(subagent_provider)
    if sub_provider is None:
        raise RuntimeError(
            f"Subagent provider {subagent_provider!r} is not configured. "
            "Run `miniouto provider add`/`miniouto provider custom add` first, "
            "or pick a different one via `miniouto subagent set --provider`."
        )

    if subagent_model is None and sub_provider.default_model:
        subagent_model = sub_provider.default_model
    if subagent_model is None:
        raise RuntimeError(
            f"No model specified for subagent provider {subagent_provider!r}. "
            "Set one via `chat --subagent-model <name>` or "
            "`miniouto subagent set --model <name>`."
        )
    return subagent_provider, subagent_model
