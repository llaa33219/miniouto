"""Repetition watchdog: notice a model stuck in a loop and force-stop it.

A model that has stopped making progress rarely goes quiet — it keeps
talking and keeps calling tools, burning tokens, so the STALL watchdog in
`core.runtime` never fires for it. This module is the other half of that
failure, and detects exactly two shapes:

- PHRASE — the same normalized sentence or line in the model's thinking or
  its content, seen `REPEAT_PHRASE_THRESHOLD` times.
- TOOL — the same tool call (name + every argument detail), issued
  `REPEAT_TOOL_THRESHOLD` times IN A ROW.

The two detectors count on different terms, and both are right:

PHRASE is CUMULATIVE: a model that alternates two phrases forever is
looping just as hard as one that repeats a single phrase, so every
occurrence counts toward the threshold.

TOOL is CONSECUTIVE. A long turn legitimately re-issues the same utility
call — `git status`, a screenshot — scattered among other work, and 30 of
those scattered over the turn is normal, not a loop; warning there would
teach the user to distrust the detector. Only a back-to-back streak of a
byte-identical call means a model is stuck, so ANY different call in
between resets the streak to one.

Counting is exact and offline — identical normalized segments only, no
edit distance and no model call — so a verdict is deterministic, cheap,
and reproducible in a test.

TWO PATHS, ONE SPLITTER. Phrases are counted from the live stream
(`note_stream_delta`, once per delta) and from the completed message
(`note_text`, whole thinking/content). Both cut at the same
`_SEGMENT_BREAKS` through `_split_completed`, so a segment compares equal
whichever path saw it. The stream path buffers the unfinished tail —
deltas fragment mid-phrase — and counts a segment the moment it
completes, which is what makes a runaway SINGLE answer detectable: that
message never ends, so its boundary is minutes away. The whole-message
path skips a message the stream already counted, so no repeat is ever
counted twice (invariant 8's double-count hazard).

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
    last_tool_key: str | None = None
    tool_streak: int = 0
    verdict: LoopVerdict | None = None


@dataclass(slots=True)
class _StreamState:
    """One actor's live stream: the tail not yet counted, and whether a
    message is currently streaming for that actor.

    `buffer` holds the verbatim tail since the last segment break —
    deltas fragment mid-phrase, so a segment is only comparable once it is
    COMPLETE. `active` is what separates "this message arrived as deltas"
    from "this actor never streams": without it, a message whose last
    character happened to be a break would leave an empty buffer and the
    whole-message `note_text` would count every repeat a second time.
    """

    buffer: str = ""
    active: bool = False


# One lock for every actor: hooks fire on the turn's event loop AND on the
# background jobs' daemon loop, so counting is genuinely concurrent.
_LOCK = threading.RLock()
_ACTORS: dict[str, _ActorCounters] = {}
_STREAMS: dict[str, _StreamState] = {}

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

    Fed from the model's thinking and its content, once per completed
    message. A message that arrived as STREAM deltas is a special case:
    every one of its segments was already counted the instant it completed
    (see `note_stream_delta`), so counting the whole text again would
    double every repeat and trip the threshold at 5 real ones. The
    whole-message call therefore degenerates to a flush of the tail the
    deltas left over. With no stream active — a non-streaming provider —
    the whole text is counted exactly as it always was.
    """

    if not text:
        return
    with _LOCK:
        stream = _STREAMS.get(actor_key)
        notify = (
            _flush_locked(actor_key) if stream is not None and stream.active
            else _count_segments_locked(actor_key, text)
        )
    _notify_trip(notify, actor_key)


def note_stream_delta(actor_key: str, delta: str) -> None:
    """Count ONE live fragment of the message `actor_key` is streaming.

    The real-time half of the phrase detector, and the only one that can
    see a model repeating the same sentence inside ONE answer that never
    ends — a message boundary is minutes and tens of thousands of tokens
    away in that case, because the message only returns at max_tokens.

    Deltas fragment mid-segment, so the fragment is appended to this
    actor's buffer and only the COMPLETED segments (up to the last
    `_SEGMENT_BREAKS` boundary, split by the same `_split_completed` the
    whole-message path uses) are counted. The unfinished tail stays in the
    buffer and counts at the next fragment, at `flush_stream`, or never.
    A threshold-th occurrence trips the verdict on the spot, and the trip
    notifier wakes the supervisor mid-stream instead of at message end.

    Same thread-safety posture as `note_text`: one lock for all counting,
    because the turn's event loop and the jobs' daemon loop both feed it.
    """

    if not delta:
        return
    with _LOCK:
        stream = _STREAMS.setdefault(actor_key, _StreamState())
        stream.active = True
        head, stream.buffer = _split_completed(stream.buffer + delta)
        notify = _count_segments_locked(actor_key, head)
    _notify_trip(notify, actor_key)


def flush_stream(actor_key: str) -> None:
    """Count an actor's buffered tail as a final segment and clear it.

    Called at a message boundary and at a force-stop, so a phrase that was
    still mid-sentence when the message ended (or was cut) is never lost.
    Idempotent: a second call has an empty buffer to count.
    """

    with _LOCK:
        notify = _flush_locked(actor_key)
    _notify_trip(notify, actor_key)


def note_tool_call(actor_key: str, name: str, arguments: Any) -> None:
    """Count one tool call toward the tool detector for one actor.

    CONSECUTIVE, not cumulative: only a back-to-back streak of the very same
    call (name + every argument detail, via `_canonical_json`) reaches the
    threshold. Any different call in between resets the streak to one, so a
    legitimately repeated utility call scattered across a long turn never
    warns — only a model stuck re-issuing the identical call does.
    """

    if not name:
        return
    key = f"{name}\0{_canonical_json(arguments)}"
    notify: Callable[[str], None] | None = None
    with _LOCK:
        counters = _counters_for(actor_key)
        if key == counters.last_tool_key:
            counters.tool_streak += 1
        else:
            counters.last_tool_key = key
            counters.tool_streak = 1
        streak = counters.tool_streak
        if streak >= REPEAT_TOOL_THRESHOLD and counters.verdict is None:
            counters.verdict = LoopVerdict(
                kind="tool", detail=f"{streak}x: {name} {_canonical_json(arguments)}"
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
    """Drop one actor's counters, verdict and stream buffer."""

    with _LOCK:
        _ACTORS.pop(actor_key, None)
        _STREAMS.pop(actor_key, None)


def reset() -> None:
    """Drop every actor's counters and stream buffers. Once per turn."""

    with _LOCK:
        _ACTORS.clear()
        _STREAMS.clear()


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


def _count_segments_locked(
    actor_key: str, text: str
) -> Callable[[str], None] | None:
    """Count every segment of `text`; return the notifier iff a verdict TRIPS.

    One transition, `None → verdict`, for both counting paths, so the
    announcement stays exactly-once-per-trip: the verdict is sticky, so
    every increment after it returns None and stays silent.
    """

    if not text:
        return None
    counters = _counters_for(actor_key)
    notify: Callable[[str], None] | None = None
    for segment in _segments(text):
        count = counters.phrases[segment] + 1
        counters.phrases[segment] = count
        if count >= REPEAT_PHRASE_THRESHOLD and counters.verdict is None:
            counters.verdict = LoopVerdict(kind="phrase", detail=f"{count}x: {segment}")
            notify = _TRIP_NOTIFIER
    return notify


def _flush_locked(actor_key: str) -> Callable[[str], None] | None:
    """Count the buffered tail as a final segment. Caller holds `_LOCK`."""

    stream = _STREAMS.get(actor_key)
    if stream is None:
        return None
    tail, stream.buffer = stream.buffer, ""
    stream.active = False
    return _count_segments_locked(actor_key, tail)


def _counters_for(actor_key: str) -> _ActorCounters:
    counters = _ACTORS.get(actor_key)
    if counters is None:
        counters = _ActorCounters()
        _ACTORS[actor_key] = counters
    return counters


def _split_completed(text: str) -> tuple[str, str]:
    """Split at the LAST segment break: `(head, tail)`.

    THE shared splitter of both counting paths — whole text and live deltas
    cut at the same `_SEGMENT_BREAKS`, so a segment means the same thing
    whichever path counted it. The tail carries no break by construction,
    so it is exactly the one segment still in flight.
    """

    end = max((m.end() for m in _SEGMENT_BREAKS.finditer(text)), default=0)
    return text[:end], text[end:]


def _segments(text: str) -> Iterator[str]:
    """Split into comparable segments: whitespace-collapsed and casefolded."""

    for raw in _SEGMENT_BREAKS.split(text):
        segment = " ".join(raw.split()).casefold()
        if len(segment) >= MIN_SEGMENT_CHARS:
            yield segment


def _canonical_json(arguments: Any) -> str:
    """Stable rendering of a tool call's arguments, independent of dict order."""

    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=repr)
