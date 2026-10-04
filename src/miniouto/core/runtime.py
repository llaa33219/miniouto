"""Build a runtime: register providers, presets, subagent tool, and resolve outo."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import coreouto as co

from ..paths_runtime import INVOCATION_CWD
from ..storage import providers as provider_store
from ..storage import settings as settings_store
from ..storage import skills as skill_store
from ..storage import styles as style_store
from ..storage.paths import SUBAGENT_RESULTS_DIR
from ..storage.styles import SubagentSpec
from ..tools import bash as bash_tool
from ..tools import registry as tool_registry
from . import repetition, subagents
from .providers import API_STALL_TIMEOUT_SECONDS, build_coreouto_provider, clear_coreouto_state

# Tools visible to both presets. The subagent tool is appended per-preset
# only when the active style declares ≥1 named subagent (see build_runtime).
BASE_TOOLS = ["Bash", "Image", "Video", "Audio"]
SUBAGENT_TOOL_NAME = "call_subagent"
# ALL_TOOLS kept for any external imports; the per-preset tool lists below
# compose BASE_TOOLS + (subagent tool when registered) explicitly so a
# zero-subagent style exposes no subagent tool at all.
ALL_TOOLS = [*BASE_TOOLS, SUBAGENT_TOOL_NAME]

# Computer is outo-only: it drives a single shared virtual screen, and
# parallel subagents clicking/typing on that screen would interleave
# unpredictably. Subagents that need GUI work must ask outo to do it.
# Registration itself is conditional (see tools.registry.register_all), so
# the preset below filters this list down to what actually got registered —
# on unsupported platforms the outo preset simply has no Computer tool.
OUTO_ONLY_TOOLS = ["Computer"]

# Tracks how deep we are inside a `call_subagent` invocation. 0 = outo (or
# after `build_runtime` has just been called), >=1 = inside a subagent.
# Read by `core.chat` dispatchers to decide whether a tool call came
# from outo (no prefix) or a nested subagent (`subagent-<id>:` prefix).
# Mutated only by the wrapper installed around `call_subagent`'s handler.
_SUBAGENT_DEPTH: ContextVar[int] = ContextVar("miniouto_subagent_depth", default=0)

# Stable per-invocation id (6 hex chars) of the innermost active subagent
# call. Set alongside _SUBAGENT_DEPTH so every hook fired inside that
# subagent's loop (tool calls, thinking, iterations) can be attributed to
# one specific invocation — with parallel subagents this is the only way
# to tell them apart. ContextVars are copied per asyncio task, so
# concurrent `call_subagent` handlers each see their own id.
_SUBAGENT_ID: ContextVar[str | None] = ContextVar("miniouto_subagent_id", default=None)

# Name of the innermost active subagent's spec ("editor", "file-picker",
# "subagent", ...). None outside a `call_subagent` invocation. Feeds the
# actor label the sinks render (`name-<sid>` — e.g. `editor-abc123`); the
# `subagent-<sid>` shape is preserved for the legacy `subagent` spec by
# construction (the tag name IS the subagent name, and legacy
# `<subagent>...</subagent>` blocks are parsed as a spec named `subagent`).
_SUBAGENT_NAME: ContextVar[str | None] = ContextVar(
    "miniouto_subagent_name", default=None
)

# Lifecycle observer for subagent invocations:
# callable(phase, sid, name, text) where phase ∈ {"start", "wakeup", "end"}.
# `name` is the spec name of the subagent running this invocation
# ("subagent" for legacy <subagent> blocks). Set per-turn by
# `core.chat.run_chat` (the hooks are global, so this module-level slot
# is the bridge between the wrapped handler and the active turn's sink).
# None outside a turn.
_SUBAGENT_OBSERVER: Callable[[str, str, str, str], None] | None = None

# Watchdog state (coreouto >= 0.11 contrib trackers), rebuilt by every
# build_runtime call alongside the hooks that feed them. The supervised
# runners (`supervised_run`) read them to decide whether a call is wedged:
#   _ACTIVITY_STATE — seconds since the last loop event of any kind
#   _PROGRESS_STATE — current phase ("llm_call" / "tool_call" / None)
#   _LIVE_MESSAGES  — live message lists keyed by actor: "outo" for the
#                     top-level agent, the 6-hex sid per subagent
#                     invocation (by reference; the loop keeps mutating
#                     them, so they are always current)
_ACTIVITY_STATE: Any = None
_PROGRESS_STATE: Any = None
_LIVE_MESSAGES: dict[str, Any] = {}

# Active supervisor levels (0 = outo's turn, >=1 = nested subagent
# invocations). A supervisor defers to deeper ones: if any level > its own
# is active, the silence belongs to the deeper call's supervisor, which
# will do the cancelling and restart. Keys are unique tokens (parallel
# sibling subagents share a level, so a bare set would collapse them).
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
    level: int,
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
    BASH_TIMEOUT_SECONDS) AND no deeper supervisor is active, the attempt
    task is cancelled, the actor's live message list is sanitized, and
    the call restarts — transcript-only when anything is recoverable,
    the original prompt otherwise. Up to WATCHDOG_MAX_WAKEUPS restarts,
    then LoopStalledError (inside a subagent handler this becomes an
    error ToolResult, so the parent sees the failure and can re-delegate).
    The `cancel_event` fast path (outo only) makes ESC ESC responsive
    during an in-flight LLM call, which the hook-boundary cancel guard
    alone cannot interrupt.

    REPETITION (`restart_on_repetition` = the actor key, e.g. "outo").
    A repeating model is active, not stalled, so this check runs BEFORE
    the stall test and has its own budget (REPEAT_MAX_RESTARTS, unrelated
    to WATCHDOG_MAX_WAKEUPS) and its own terminal error
    (LoopRepeatedError). It is per-actor and only this supervisor consumes
    its actor's verdict, so the deeper-supervisor deferral below does not
    apply to it.

    ENFORCEMENT IS EVENT-DRIVEN AND COVERS THE COMPLETION PATH. A verdict
    trips at note time, so this supervisor subscribes to it with
    `repetition.set_trip_notifier` (for the duration of the run) and waits on
    a `trip.wait()` task alongside the attempt: the force-stop is immediate
    instead of arriving with the next WATCHDOG_CHECK_SECONDS poll, which
    stays the stall check's cadence. `call_soon_threadsafe` is required —
    a background job notes from the daemon thread.

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
    It is deliberately NOT pushed through `subagents`' alarm deque: that
    deque is drained into the live list by `_make_alarm_hook` and would
    deliver the same text a second time. `on_repetition` covers the
    display side (one `alarm` loop event on the turn's sink).

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
    _ACTIVE_SUPERVISORS[token] = level
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
        # interval (5s in production) can never reach a threshold. Same
        # discipline as `repetition.cancel_background_loop`.
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
            if any(lv > level for lv in _ACTIVE_SUPERVISORS.values()):
                continue  # a deeper supervisor owns the current silence
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


def current_subagent_depth() -> int:
    """Return how many subagent layers we're currently nested inside."""

    return _SUBAGENT_DEPTH.get()


def current_subagent_id() -> str | None:
    """Return the innermost active subagent invocation id, or None for outo."""

    return _SUBAGENT_ID.get()


def current_subagent_name() -> str | None:
    """Return the innermost active subagent spec name, or None for outo.

    The actor label format the sinks render is `f"{name}-{sid}"`, which
    for a legacy `<subagent>` block (spec name "subagent") produces the
    historical `subagent-<sid>` shape by construction.
    """

    return _SUBAGENT_NAME.get()


def set_subagent_observer(
    observer: Callable[[str, str, str, str], None] | None,
) -> None:
    """Install (or clear, with None) the subagent lifecycle observer."""

    global _SUBAGENT_OBSERVER
    _SUBAGENT_OBSERVER = observer


def _notify_subagent(phase: str, sid: str, name: str, text: str) -> None:
    observer = _SUBAGENT_OBSERVER
    if observer is not None:
        # an observer must never break the subagent loop
        with contextlib.suppress(Exception):
            observer(phase, sid, name, text)


def _wrap_subagent_handler(
    agents: dict[str, Any],
    specs: list[SubagentSpec],
) -> Callable[..., Any]:
    """Build `call_subagent`'s handler: resolve a name, start a background job.

    Fire-and-forget. The handler validates `(name, task)`, hands the brief and
    the per-spec Agent to `subagents.submit` (which owns the id, the daemon
    event loop, and the completion alarm) and returns an acknowledgement. The
    subagent's answer is NEVER in the return value: it reaches the model later
    as a synthetic user message carrying the completion alarm, delivered by
    `_make_alarm_hook` at the next `BEFORE_LLM_CALL`. Several calls in one turn
    start several concurrent jobs, so no fan-out parameters are needed.

    `agents` maps each declared spec name to the `co.Agent` built for it by
    `_build_subagent_tool` — the job engine only needs something with
    `call(brief)`.
    """

    def _resolve(requested: str) -> str:
        """Resolve a name to one of `specs`. Empty → fallback rules.

        Fallback: a spec literally named `subagent` (legacy compat),
        else the sole declared spec, else ValueError. An explicit name
        that matches nothing fails fast, before any job is started.
        """

        if requested:
            if not any(s.name == requested for s in specs):
                raise ValueError(
                    f"Unknown subagent name {requested!r}. "
                    f"Available names: {', '.join(s.name for s in specs)}"
                )
            return requested
        if any(s.name == "subagent" for s in specs):
            return "subagent"
        if len(specs) == 1:
            return specs[0].name
        raise ValueError(
            "`name` is required when multiple subagents are declared. "
            f"Available names: {', '.join(s.name for s in specs)}"
        )

    async def wrapped(name: str = "", task: str = "") -> str:
        if not isinstance(task, str) or not task.strip():
            raise ValueError(
                "call_subagent requires `task`: one self-contained brief. "
                "To run several subagents at once, issue several calls."
            )
        resolved = _resolve(name)
        job = subagents.submit(agents[resolved], resolved, task)
        # The result file's own name is only known once the job finishes, so
        # the ack points at the directory the alarm will name a file in.
        return (
            f"Started background subagent '{resolved}-{job.sid}' (id: {job.sid}). "
            "It runs in the background and this returns immediately — this "
            "text is NOT the result. When it finishes you will receive a user "
            "message starting with [SYSTEM ALARM - THIS IS NOT A USER MESSAGE] "
            f"naming the result file at {SUBAGENT_RESULTS_DIR}. Do not wait or "
            "poll; continue with other work."
        )

    return wrapped


def _build_subagent_tool(
    *,
    specs: list[SubagentSpec],
    provider_config: dict[str, Any],
    provider_passthrough: dict[str, Any] | None = None,
) -> Callable[..., Any]:
    """Build `call_subagent`'s handler with one Agent per named spec.

    Invariant 5 — DO NOT simplify to `coreouto.contrib.agent_as_tool`:
    that helper drops `provider_config` when calling `preset.to_config()`,
    so the subagent would inherit the provider's low hard default
    (Anthropic: 1024 tokens) and silently truncate long tool calls
    (heredoc writes). We build the per-name Agents ourselves so every one
    carries our `provider_config` (incl. `max_tokens`) AND
    `provider_passthrough` (incl. reasoning).

    Each spec becomes one `co.Agent`, built from a deep copy of the
    shared "subagent" preset's config with that spec's `prompt` set as
    `system_prompt`, so no two personas share mutable config. The returned
    handler is registered with coreouto, which derives the model-facing
    JSON schema from its type hints.
    """

    preset = co.get_agent_preset("subagent")
    base_config = preset.to_config()
    if provider_config:
        base_config.provider_config.update(provider_config)
    if provider_passthrough:
        base_config.provider_passthrough.update(provider_passthrough)

    agents: dict[str, Any] = {}
    for spec in specs:
        cfg = base_config.model_copy(deep=True)
        cfg.system_prompt = spec.prompt
        agents[spec.name] = co.Agent(cfg)

    return _wrap_subagent_handler(agents, specs)


def _make_alarm_hook(
    on_alarms: Callable[[list[subagents.Alarm]], None] | None = None,
) -> Callable[..., None]:
    """Build the BEFORE_LLM_CALL hook that delivers background-subagent alarms.

    A job that finished — mid-turn or between turns — queued a completion
    alarm naming its result file. Every iteration this hook drains the queue
    and, if anything came back, appends the alarms to the LIVE `messages`
    list as ONE synthetic user message (joined with a blank line), so the very
    next `provider.create` already carries them.

    Appended directly, never through `Agent.inject_user_message`: that queue
    is drained at the TOP of the loop iteration (coreouto agent.py:301-307),
    i.e. BEFORE this hook runs, so an alarm queued from here would only land
    a full LLM round-trip later.

    FOLD RULE: if the last message is already a user message, the alarms are
    folded into it instead of appended — providers reject two consecutive
    user messages, the same hazard `_cut_poisoned_turn` works around.

    Gated to outo depth: a subagent's own loop must never consume outo's
    alarms. `on_alarms` (from `core.chat`) records one loop event per alarm so
    the session and the TUI show the completion; a broken sink must not break
    the provider path.
    """

    def hook(*, messages: Any = None, **_kwargs: Any) -> None:
        if messages is None or current_subagent_depth() != 0:
            return
        alarms = subagents.take_pending_alarms()
        if not alarms:
            return
        text = "\n\n".join(alarm.text for alarm in alarms)
        last = messages[-1] if messages else None
        if last is not None and last.role == "user" and isinstance(last.content, str):
            last.content = f"{last.content}\n\n{text}" if last.content else text
        else:
            messages.append(co.Message(role="user", content=text))
        if on_alarms is not None:
            with contextlib.suppress(Exception):
                on_alarms(alarms)

    return hook


@dataclass
class RuntimeConfig:
    provider_name: str
    model: str
    style_name: str
    subagent_model: str | None = None
    subagent_provider: str | None = None
    subagent_reasoning: str | None = None
    session: str | None = None


@dataclass
class ChatOverrides:
    provider: str | None = None
    model: str | None = None
    style: str | None = None
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
    on_alarms: Callable[[list[subagents.Alarm]], None] | None = None,
    reasoning: str | None = None,
    cancel_event: threading.Event | None = None,
) -> co.Agent:
    """Construct the outo Agent with the active style and subagent wired in.

    `provider_config` is merged into the outo Agent's `provider_config` so that
    canonical settings (max_tokens, temperature, ...) flow through coreouto's
    normalizer without us having to mutate the config after construction.
    `on_tool_call` is called for every tool invocation (outo and subagent)
    with `(tool_name, arguments)`. Pass None to skip.
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
    `on_alarms` is called with every completion alarm the BEFORE_LLM_CALL
    alarm hook drained, after appending them to the live message list —
    `core.chat` turns each into an `alarm` loop event so the session and
    the TUI show the background job completing. Pass None to skip the
    display only; the alarms are still delivered to the model.
    `reasoning` overrides reasoning for this call (effort level / "on" /
    token budget / "none"). None = auto: the provider's stored
    `reasoning_effort` wins if set, otherwise lma's per-model default.
    Resolved by `core.reasoning.resolve_reasoning_passthrough` into
    provider-native kwargs merged into `provider_passthrough` for both
    outo and subagent — this is what makes providers return thinking
    blocks at all.
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

    from .context import get_max_output_tokens, make_summarize_hook
    from .reasoning import resolve_reasoning_passthrough

    # ---- Slow resolution, hoisted ABOVE `clear_coreouto_state()` ----
    # Everything below this point must be pure in-memory work: the clear
    # wipes coreouto's global provider/preset/tool/hook registries, and a
    # background subagent job resolves its provider and tools by NAME at
    # `call()` entry — so a multi-second network fetch inside the
    # clear+register window is a real race (the subagent would find an
    # empty registry). Style file I/O and both lma lookups therefore
    # resolve first; only dict/registry operations follow the clear.
    provider = provider_store.get(runtime.provider_name)
    if provider is None:
        raise RuntimeError(
            f"Provider {runtime.provider_name!r} is not configured. "
            "Run `miniouto provider add` first."
        )

    outo_style, parsed_specs = _resolve_both_styles(runtime.style_name, style_overrides)
    outo_prompt = _with_cwd("outo", outo_style)
    # Per-spec cwd prepending happens here, not in _resolve_both_styles —
    # cwd is a runtime-derived value (INVOCATION_CWD) regenerated every
    # build_runtime call, so it must not be baked into the style loader.
    specs = [
        SubagentSpec(name=spec.name, prompt=_with_cwd("subagent", spec.prompt))
        for spec in parsed_specs
    ]

    # Subagent runs the same model (or its override) and must share
    # the output-token cap, otherwise long tool calls it issues
    # (heredoc file writes) get truncated at the provider's low
    # default (1024 for Anthropic) and we end up with a half-written
    # file and an "I cut off mid-function" loop. Pull the cap from
    # the same lma endpoint as outo so it tracks the subagent's
    # model when one is configured. `setdefault` still lets a CLI
    # `--max-tokens` override win.
    subagent_model = runtime.subagent_model or runtime.model
    subagent_provider_config: dict[str, Any] = dict(provider_config or {})
    subagent_passthrough: dict[str, Any] = {}
    sub_provider: Any = None
    sub_provider_name: str = ""
    if specs:
        sub_provider_name = runtime.subagent_provider or runtime.provider_name
        sub_provider = provider_store.get(sub_provider_name)
        if sub_provider is None:
            raise RuntimeError(
                f"Subagent provider {sub_provider_name!r} is not configured."
            )
        subagent_provider_config.setdefault(
            "max_tokens", get_max_output_tokens(subagent_model, sub_provider_name)
        )
        subagent_choice = (
            reasoning
            if reasoning is not None
            else (runtime.subagent_reasoning or sub_provider.reasoning_effort)
        )
        subagent_passthrough = resolve_reasoning_passthrough(
            sub_provider.api_format,
            subagent_model,
            sub_provider_name,
            subagent_choice,
        )

    # Thinking display (ON_THINKING → CLI/TUI) only works when the model is
    # asked to reason — providers never emit thinking blocks unrequested.
    # The resolver consults lma's per-model reasoning_options; an explicit
    # `reasoning` choice wins over the provider's stored reasoning_effort.
    outo_choice = reasoning if reasoning is not None else provider.reasoning_effort
    outo_passthrough = resolve_reasoning_passthrough(
        provider.api_format,
        runtime.model,
        runtime.provider_name,
        outo_choice,
    )

    # make_summarize_hook also consults lma for the context window (unless
    # a provider override supplies it) — resolved here, hooked below.
    summarize_on_iteration, summarize_before_llm = make_summarize_hook(
        runtime.model, runtime.session or "default", runtime.provider_name
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

    # outo preset is always registered. The subagent tool is added to its
    # tool list ONLY when the active style declared ≥1 subagent; otherwise
    # outo exposes no subagent tool and the model never sees it in its
    # schema. OUTO_ONLY_TOOLS (Computer) is filtered by registration —
    # unsupported platforms never see it.
    outo_tools: list[str] = list(BASE_TOOLS)
    if specs:
        outo_tools.append(SUBAGENT_TOOL_NAME)
    outo_tools.extend(t for t in OUTO_ONLY_TOOLS if co.get_tool(t) is not None)

    co.register_agent_preset(
        "outo",
        model=runtime.model,
        provider=runtime.provider_name,
        system_prompt=outo_prompt,
        tools=outo_tools,
        max_iterations=None,
    )

    # Per-turn conditional registration. Because `clear_coreouto_state()`
    # wipes providers/presets/tools/hooks at the top of every call, the
    # TUI's mid-session style switches (a style with subagents → one
    # without, or vice versa) correctly add or remove the tool and the
    # preset between turns. No extra caching of specs across turns.
    if specs:
        build_coreouto_provider(sub_provider)

        # The "subagent" preset is a config carrier: it provides the
        # model/provider/tools base that `_build_subagent_tool` deep-copies
        # once per spec to build one Agent per named subagent. Its own
        # system_prompt is never seen by the model (every per-spec Agent
        # overrides it with that spec's prompt).
        co.register_agent_preset(
            "subagent",
            model=runtime.subagent_model or runtime.model,
            provider=sub_provider_name,
            system_prompt=parsed_specs[0].prompt,
            tools=[*BASE_TOOLS, SUBAGENT_TOOL_NAME],
            max_iterations=None,
        )

        subagent_handler = _build_subagent_tool(
            specs=specs,
            provider_config=subagent_provider_config,
            provider_passthrough=subagent_passthrough,
        )
        co.register_tool(
            SUBAGENT_TOOL_NAME, description=_subagent_description(parsed_specs)
        )(subagent_handler)

    if on_tool_call is not None:
        co.register_hook(co.BEFORE_TOOL_CALL, _make_tool_call_logger(on_tool_call))

    if on_tool_result is not None:
        co.register_hook(co.AFTER_TOOL_CALL, _make_tool_result_logger(on_tool_result))

    co.register_hook(co.ON_ITERATION, summarize_on_iteration)
    # Compaction runs at BEFORE_LLM_CALL (all tool-call pairs complete) —
    # see make_summarize_hook's docstring for why ON_ITERATION is unsafe.
    # Registered before the watchdog's capture hook below, so the captured
    # list is the post-compaction one.
    co.register_hook(co.BEFORE_LLM_CALL, summarize_before_llm)

    # Unconditional (like the repair hook): a job that finished between
    # turns has no other way to reach the model. Registered before the
    # watchdog's capture hook below, so the live message list the
    # supervisor holds already carries the delivered alarms.
    co.register_hook(co.BEFORE_LLM_CALL, _make_alarm_hook(on_alarms))

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
    phase the loop is in,     and the BEFORE_LLM_CALL capture keeps a
    reference to each actor's live message list (keyed by subagent id,
    "outo" for the top level) so supervisors can recover history after
    cancelling a wedged call. Subagent entries are popped by the wrapper
    when the invocation ends, so the dict does not grow across turns.
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
        _LIVE_MESSAGES[_SUBAGENT_ID.get() or "outo"] = messages

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
    (Anthropic extended thinking, OpenAI reasoning summaries). Fired inside
    subagent loops too, so the ContextVars above attribute it correctly.
    """

    def hook(*, thinking: str, **kwargs: Any) -> None:
        if thinking:
            callback(thinking)

    return hook


def _subagent_description(specs: list[SubagentSpec]) -> str:
    available = ", ".join(s.name for s in specs)
    base = (
        "Delegate one self-contained brief to a named background subagent "
        "and return immediately.\n\n"
        "This call does NOT block: the subagent runs in the background, "
        "possibly across turns. The return value is only an acknowledgement "
        '("started ..."), never the subagent\'s answer. When the subagent '
        "finishes, you will receive a user message starting with "
        "[SYSTEM ALARM - THIS IS NOT A USER MESSAGE] naming its result file "
        "under ~/.miniouto/subagent-results/ — read that file (e.g. with "
        "`cat`) to see what it produced. Do not poll for or re-ask the "
        "result.\n\n"
        "To run several subagents at once, issue several calls (each with "
        "one brief) — they run concurrently in the background.\n\n"
        "Arguments:\n"
        "- name: which persona to run. Required when several subagents are "
        "declared; may be omitted when exactly one is declared (or when one "
        'is literally named "subagent").\n'
        "- task: ONE self-contained brief. The subagent sees only its own "
        "persona prompt and this brief — include all file paths, "
        "constraints, and success criteria it needs."
    )
    if available:
        base += f"\n\nAvailable names: {available}"
    return base


def _resolve_both_styles(
    style_name: str, overrides: dict[str, str] | None
) -> tuple[str, list[SubagentSpec]]:
    """Return (outo_prompt, specs) from the active style document.

    The parser (`storage.styles.parse_style`) yields one `SubagentSpec` per
    named tag — `<editor>...</editor>` becomes a spec named `editor`, the
    legacy `<subagent>...</subagent>` becomes a spec named `subagent`,
    and a style with no other tags yields zero specs (no subagents at
    all — `build_runtime` will not register the `call_subagent` tool).

    Active skills are prepended to BOTH the outo prompt and to each spec's
    prompt (new `SubagentSpec` instances — the dataclass is frozen). An
    empty/whitespace spec body falls back to `_fallback_style("subagent")`
    content as that spec's prompt.
    """

    raw = _read_raw_style(style_name, overrides)
    outo_part, parsed_specs = style_store.parse_style(raw)

    skills_content = _load_active_skills()
    fallback_sub = _fallback_style("subagent")
    specs: list[SubagentSpec] = []
    for spec in parsed_specs:
        body = spec.prompt.strip() or fallback_sub
        prompt = (skills_content + "\n\n" + body) if skills_content else body
        specs.append(SubagentSpec(name=spec.name, prompt=prompt))

    if skills_content:
        outo_part = skills_content + "\n\n" + outo_part

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


def _load_active_skills() -> str:
    """List installed skills (name + description + on-disk location) for lazy loading.

    Only the catalog is injected into the prompt — the model reads the full
    SKILL.md itself (via Bash) when a task matches a skill's description.
    """

    skills = skill_store.list_skills()
    if not skills:
        return ""

    lines = [
        "# Available Skills",
        "",
        "The following skills are installed. Each lives in its own directory at "
        f"{skill_store.SKILLS_DIR}/<name>/ containing a SKILL.md with "
        "instructions plus any supporting files it references. When a task "
        "matches a skill's description, read that skill's SKILL.md (and the "
        "files it references) via Bash before proceeding — skill instructions "
        "take precedence over the default workflow.",
        "",
    ]
    for skill in skills:
        lines.append(f"- {skill.name}: {skill.description}")

    return "\n".join(lines)


def _with_cwd(role: str, body: str) -> str:
    """Prepend an absolute-cwd preamble to a style body at runtime.

    The preamble tells the model where the user invoked miniouto from so
    relative paths and bash commands resolve against the right directory.
    Not stored on disk — injected fresh each build_runtime call.
    """

    if role == "subagent":
        preamble = (
            f"You operate inside this working directory: {INVOCATION_CWD}\n"
            "All relative paths and shell commands resolve against it. "
            "Use absolute paths when in doubt.\n\n"
        )
    else:
        preamble = (
            f"The user invoked miniouto from: {INVOCATION_CWD}\n"
            "When delegating to the subagent, pass relative paths verbatim "
            "or absolute paths explicitly — the subagent's tools resolve "
            "against this directory.\n\n"
        )
    return preamble + body


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


def resolve_runtime_from_settings(overrides: ChatOverrides | None = None) -> RuntimeConfig:
    s = settings_store.load()
    overrides = overrides or ChatOverrides()

    provider_name = overrides.provider or s.provider
    if not provider_name:
        raise RuntimeError(
            "No default provider set. Run `miniouto provider add` and "
            "`miniouto provider default <name>` first."
        )
    provider = provider_store.get(provider_name)
    if provider is None:
        raise RuntimeError(f"Provider {provider_name!r} is not configured.")

    model = overrides.model or s.model or provider.default_model
    if not model:
        raise RuntimeError(
            f"No model specified for provider {provider_name!r}. "
            "Pass --model on the chat command, or set a default via "
            "`miniouto provider add --default-model <name>`."
        )

    subagent_provider, subagent_model = _resolve_subagent(overrides, s)

    return RuntimeConfig(
        provider_name=provider_name,
        model=model,
        style_name=overrides.style or s.style or "default",
        session=s.session or "default",
        subagent_provider=subagent_provider,
        subagent_model=subagent_model,
        subagent_reasoning=s.subagent_reasoning or None,
    )


def _resolve_subagent(
    overrides: ChatOverrides, s: settings_store.Settings
) -> tuple[str | None, str | None]:
    """Resolve the subagent's provider/model for build_runtime.

    Provider precedence: CLI override > settings > None (None = inherit
    outo's provider — build_runtime's `runtime.subagent_provider or
    runtime.provider_name` fallback handles it).
    Model precedence: CLI override > settings > subagent provider's
    default_model > None (None = inherit outo's model via the same
    `or`-fallback in build_runtime). A model-only override (no subagent
    provider) is valid: the subagent then runs outo's provider with a
    different model.
    """

    subagent_provider = overrides.subagent_provider or s.subagent_provider or None
    subagent_model = overrides.subagent_model or s.subagent_model or None
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
