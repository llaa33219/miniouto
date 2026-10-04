"""Repetition watchdog: notice a model stuck in a loop and force-stop it.

A model that has stopped making progress rarely goes quiet — it keeps
talking and keeps calling tools, burning tokens, so the STALL watchdog in
`core.runtime` never fires for it. This module is the other half of that
failure, and detects exactly two shapes:

- PHRASE — the same normalized sentence or line in the model's thinking or
  its content, seen `REPEAT_PHRASE_THRESHOLD` times.
- TOOL — the same tool call (name + arguments), issued
  `REPEAT_TOOL_THRESHOLD` times.

Both counts are CUMULATIVE, not consecutive: a model that alternates two
phrases forever is looping just as hard as one that repeats one.

Counting is exact and offline — identical normalized segments only, no
edit distance and no model call — so a verdict is deterministic, cheap,
and reproducible in a test.

ACTORS. Counters are keyed by an actor key its caller supplies
(`current_subagent_id() or "outo"`), so one subagent looping can never
trip another subagent or outo. Enforcement is deliberately asymmetric,
because the actors are not supervised the same way:

- outo's turn runs under `core.runtime.supervised_run`, which force-stops
  the attempt and RESUMES it with the alarm as the new user message.
- a background job (`core.subagents.submit`) runs on the daemon loop with
  no supervisor, so repetition there is enforced by
  `cancel_background_loop`: the job is cancelled and its own existing
  completion path reports the stop (result file + one alarm).

Counters live for one turn: `reset()` is called from
`runtime._register_watchdog_trackers`, where the live message lists are
cleared, so a verdict can never leak into the next turn.

IMMEDIACY. A verdict trips at note time, so it is also ANNOUNCED at note
time through `set_trip_notifier`: a supervisor subscribes to its own actor
key and wakes on the event, instead of discovering the trip on its next
poll (WATCHDOG_CHECK_SECONDS). The announcement fires once per trip — the
verdict is sticky, so the increments after it stay silent.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from . import subagents
from .subagents import ALARM_HEADER

# User-facing contract: how much repetition counts as a loop.
REPEAT_PHRASE_THRESHOLD = 10
REPEAT_TOOL_THRESHOLD = 30
# How many times a supervised turn may be force-stopped for repetition
# before the turn is abandoned. Deliberately independent of (and unrelated
# to) the stall watchdog's WATCHDOG_MAX_WAKEUPS: a repeating model is
# active, not wedged, and the two budgets must not spend each other.
REPEAT_MAX_RESTARTS = 3

# Segments shorter than this are junk ("ok.", "yes!") and are not counted —
# a model legitimately says "sure" many times in a long turn.
MIN_SEGMENT_CHARS = 4

# Sentence and line ends, ASCII and CJK. Splitting on these is what makes
# "the same phrase" a phrase instead of an entire response; the fullwidth
# marks are the CJK sentence enders (ambiguous-unicode false positives).
_SEGMENT_BREAKS = re.compile(r"[\r\n.?!、。？！]+")  # noqa: RUF001


@dataclass(frozen=True, slots=True)
class LoopVerdict:
    """One tripped repetition detector, for one actor.

    `kind` is `"phrase"` or `"tool"`; `detail` is the offending segment or
    tool call plus its count, for the human reading the log.
    """

    kind: Literal["phrase", "tool"]
    detail: str


@dataclass(slots=True)
class _ActorCounters:
    phrases: Counter[str] = field(default_factory=Counter)
    tools: Counter[str] = field(default_factory=Counter)
    verdict: LoopVerdict | None = None


# One lock for every actor: hooks fire on the turn's event loop AND on the
# background jobs' daemon loop, so counting is genuinely concurrent.
_LOCK = threading.RLock()
_ACTORS: dict[str, _ActorCounters] = {}

# One trip notifier, so a supervisor can wake on the trip itself instead of
# discovering it on its next poll. Read under `_LOCK` by the note functions.
_TRIP_NOTIFIER: Callable[[str], None] | None = None


def set_trip_notifier(cb: Callable[[str], None] | None) -> None:
    """Install the callback fired with the actor key when a verdict TRIPS.

    Fired exactly once per trip — the verdict is sticky, so the increments
    after it must stay silent — and under `contextlib.suppress(Exception)`,
    so a broken notifier can never break the counting it observes. Pass None
    to uninstall. The slot is global, so save the previous value yourself and
    put it back when you are done (see `runtime.supervised_run`).

    THREADS. `note_text` / `note_tool_call` are called from the turn's event
    loop AND from the daemon thread that owns the background jobs, so the
    callback may run on either. It must thread-hop itself (e.g.
    `loop.call_soon_threadsafe`) before touching anything loop-affine.
    """

    global _TRIP_NOTIFIER
    with _LOCK:
        _TRIP_NOTIFIER = cb


def _notify_trip(notifier: Callable[[str], None] | None, actor_key: str) -> None:
    if notifier is not None:
        with contextlib.suppress(Exception):
            notifier(actor_key)


def note_text(actor_key: str, text: str) -> None:
    """Count `text` toward the phrase detector for one actor.

    Fed from the model's thinking and its content. Whole responses today;
    a streaming milestone can feed deltas through the same call, since the
    segments are counted, not the surrounding text.
    """

    if not text:
        return
    notify: Callable[[str], None] | None = None
    with _LOCK:
        counters = _counters_for(actor_key)
        for segment in _segments(text):
            count = counters.phrases[segment] + 1
            counters.phrases[segment] = count
            if count >= REPEAT_PHRASE_THRESHOLD and counters.verdict is None:
                counters.verdict = LoopVerdict(
                    kind="phrase", detail=f"{count}x: {segment}"
                )
                notify = _TRIP_NOTIFIER
    _notify_trip(notify, actor_key)


def note_tool_call(actor_key: str, name: str, arguments: Any) -> None:
    """Count one tool call toward the tool detector for one actor."""

    if not name:
        return
    key = f"{name}\0{_canonical_json(arguments)}"
    notify: Callable[[str], None] | None = None
    with _LOCK:
        counters = _counters_for(actor_key)
        count = counters.tools[key] + 1
        counters.tools[key] = count
        if count >= REPEAT_TOOL_THRESHOLD and counters.verdict is None:
            counters.verdict = LoopVerdict(
                kind="tool", detail=f"{count}x: {name} {_canonical_json(arguments)}"
            )
            notify = _TRIP_NOTIFIER
    _notify_trip(notify, actor_key)


def check(actor_key: str) -> LoopVerdict | None:
    """Return the actor's verdict, or None. Sticky until consumed/reset."""

    with _LOCK:
        counters = _ACTORS.get(actor_key)
        return None if counters is None else counters.verdict


def consume_verdict(actor_key: str) -> LoopVerdict | None:
    """Return the actor's verdict and clear it, counters included.

    Clearing the counters too is deliberate: a resumed attempt must earn
    its own verdict from scratch, or the very next poll would re-trip on
    counts that are already past the threshold.
    """

    with _LOCK:
        counters = _ACTORS.pop(actor_key, None)
        return None if counters is None else counters.verdict


def reset_actor(actor_key: str) -> None:
    """Drop one actor's counters and verdict."""

    with _LOCK:
        _ACTORS.pop(actor_key, None)


def reset() -> None:
    """Drop every actor's counters. Called once per turn from build_runtime."""

    with _LOCK:
        _ACTORS.clear()


def cancel_background_loop(actor_key: str) -> LoopVerdict | None:
    """Cancel a background job whose own loop tripped. No-op for any other actor.

    A background job has no supervisor, so there is nothing to restart: the
    only honest enforcement is to stop the job. Its existing completion path
    then writes the result file and queues exactly ONE alarm, so the parent
    turn learns the job stopped without a second channel here. Returns the
    verdict that caused the cancel, or None if nothing was done.

    A supervised actor is deliberately untouched: its verdict stays sticky
    for `runtime.supervised_run` to poll and resume.

    Called on EVERY tool call, so it must not disturb an actor that has not
    tripped — hence the peek before the consume (which clears the counters).
    """

    if not subagents.is_running(actor_key) or check(actor_key) is None:
        return None
    verdict = consume_verdict(actor_key)
    if verdict is None:
        return None
    subagents.cancel_job(
        actor_key, reason=f"repetition: {verdict.kind} loop ({verdict.detail})"
    )
    return verdict


def alarm_text(verdict: LoopVerdict) -> str:
    """The exact user message a tripped verdict delivers. Shared contract."""

    if verdict.kind == "phrase":
        return (
            f"{ALARM_HEADER}\n"
            f"Repetition detected: the same phrase has been repeated >= "
            f"{REPEAT_PHRASE_THRESHOLD} times. The loop was force-stopped and is "
            f"being resumed. Continue the work, but proceed with a DIFFERENT "
            f"pattern or approach instead of repeating the same wording."
        )
    return (
        f"{ALARM_HEADER}\n"
        f"Repetition detected: the same tool call has been issued >= "
        f"{REPEAT_TOOL_THRESHOLD} times. The loop was force-stopped and is being "
        f"resumed. Continue the work differently — if repeating the same action "
        f"is not producing progress, try a different method."
    )


def _counters_for(actor_key: str) -> _ActorCounters:
    counters = _ACTORS.get(actor_key)
    if counters is None:
        counters = _ActorCounters()
        _ACTORS[actor_key] = counters
    return counters


def _segments(text: str) -> Iterator[str]:
    """Split into comparable segments: whitespace-collapsed and casefolded."""

    for raw in _SEGMENT_BREAKS.split(text):
        segment = " ".join(raw.split()).casefold()
        if len(segment) >= MIN_SEGMENT_CHARS:
            yield segment


def _canonical_json(arguments: Any) -> str:
    """Stable rendering of a tool call's arguments, independent of dict order."""

    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=repr)
