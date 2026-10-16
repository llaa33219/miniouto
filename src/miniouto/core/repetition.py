"""Repetition watchdog: notice a model stuck in a loop and force-stop it.

A model that has stopped making progress rarely goes quiet — it keeps
talking and keeps calling tools, burning tokens, so the STALL watchdog in
`core.runtime` never fires for it. This module is the other half of that
failure, and detects exactly two shapes:

- PHRASE — the same phrase UNIT (one segment, up to `MAX_UNIT_SEGMENTS`
  of them) repeated `REPEAT_PHRASE_THRESHOLD` times BACK TO BACK.
- TOOL — the same tool call (name + every argument detail), issued
  `REPEAT_TOOL_THRESHOLD` times IN A ROW.

The two detectors both count on CONSECUTIVE runs, and both are right:

PHRASE counts a RUN, not a total. A model stuck in a loop does not sprinkle
one phrase among ten others — it emits the same wording again and again, so
only a chain of identical units END TO END is a loop. Scattered repeats are
how humans and healthy models speak: the same lead-in before each of ten
tool calls, or the same word reused in a different order throughout a
sentence. Warning there teaches the user to distrust the detector, so a
run is required and a total never is.

The UNIT is what makes an alternating loop visible. "A B A B A B ... A B"
never repeats a single segment back to back, but "A B" itself is one unit
repeated consecutively, so it trips at the same threshold as a single
phrase — and "A B C D A F C A B B A", which merely reuses words in a
changing order, is not a run of anything and stays silent. Unit sizes
`p = 1..MAX_UNIT_SEGMENTS` are tried ascending, so the smallest (most
specific) unit that reaches the threshold is the one reported.

The run SPANS messages. A model alternating two phrases across ten
messages is the same loop as one doing it inside a single answer, and a
message boundary means nothing about wording. `reset_phrases` is the one
exception, for a tail that is being DISCARDED rather than closed.

TOOL is CONSECUTIVE for the same reason. A long turn legitimately re-issues
the same utility call — `git status`, a screenshot — scattered among other
work, and 30 of those scattered over the turn is normal, not a loop. Only
a back-to-back streak of a byte-identical call means a model is stuck, so
ANY different call in between resets the streak to one, while the streak
itself spans messages: one identical call per message is 30 real
repetitions.

Counting is exact and offline — identical normalized segments only, no
edit distance and no model call — so a verdict is deterministic, cheap,
and reproducible in a test. Blocks are compared as a chain FROM THE END of
the segment stream, so a misaligned stream can never fake a run: the
alignment must hold for every block of the chain.

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

ACTORS. Counters are keyed by an actor key its caller supplies. One
process supervises exactly one actor now — the spawned-run model makes
every other actor a separate `miniouto chat --bg` process with its own
watchdog — so the key in practice is the literal `"outo"`, but the
parameterized key shape is kept: it costs nothing, keeps every function
testable in isolation, and survives a future second in-process actor.

Enforcement is uniform: the turn runs under `core.runtime.supervised_run`,
which force-stops the attempt and RESUMES it with the alarm as the new
user message.

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
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from .inbox import ALARM_HEADER

# User-facing contract: how much repetition counts as a loop. The phrase
# number counts UNIT REPETITIONS, not segments — one phrase 50x in a row is
# 50 units of one segment, and a pair repeated 50x is 50 units of two.
REPEAT_PHRASE_THRESHOLD = 50
REPEAT_TOOL_THRESHOLD = 30
# How many times a supervised turn may be force-stopped for repetition
# before the turn is abandoned. Deliberately independent of (and unrelated
# to) the stall watchdog's WATCHDOG_MAX_WAKEUPS: a repeating model is
# active, not wedged, and the two budgets must not spend each other.
REPEAT_MAX_RESTARTS = 3

# Segments shorter than this are junk ("ok.", "yes!") and are not counted —
# a model legitimately says "sure" many times in a long turn.
MIN_SEGMENT_CHARS = 4

# Longest phrase UNIT the detector recognises, in segments. p=1 is a single
# segment; p=8 catches an eight-segment cycle. The bound is what keeps the
# check cheap (at most 8 block comparisons per new segment) and keeps a unit
# specific enough that the verdict names the actual repeated wording.
MAX_UNIT_SEGMENTS = 8

# Bounded tail of recent segments per actor — the only state a run needs. It
# must hold the longest chain that can still trip the threshold, plus room
# for the mismatching block that ends the next one, so
# `REPEAT_PHRASE_THRESHOLD * (MAX_UNIT_SEGMENTS + 1)`; 512 leaves slack.
MAX_TAIL_SEGMENTS = 512

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
    segments: deque[str] = field(
        default_factory=lambda: deque(maxlen=MAX_TAIL_SEGMENTS)
    )
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


def reset_phrases(actor_key: str) -> None:
    """Drop one actor's run state and stream buffer, WITHOUT flushing them.

    For a message that is being DISCARDED rather than closed — the stall
    restart sanitizes the wedged tail out of the history, and nothing of it
    is ever recorded, so a run left half-built by that tail could only leak
    into the resumed attempt. No flush, unlike `flush_stream`: what is
    discarded must not count anywhere.

    The verdict and the tool streak deliberately survive. A tripped loop
    must stay tripped across the boundary the supervisor notices it at, and
    a back-to-back streak of one identical call legitimately spans a restart
    (one call per message is 30 real repetitions).

    A no-op for an actor with no counters, so a supervisor that never
    restarted anything pays nothing.
    """

    with _LOCK:
        counters = _ACTORS.get(actor_key)
        if counters is not None:
            counters.segments.clear()
        stream = _STREAMS.get(actor_key)
        if stream is not None:
            stream.buffer = ""
            stream.active = False


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
    """Append every segment of `text` to the actor's stream; notify on a TRIP.

    One transition, `None → verdict`, for both counting paths, so the
    announcement stays exactly-once-per-trip: the verdict is sticky, so
    every increment after it returns None and stays silent.
    """

    if not text:
        return None
    counters = _counters_for(actor_key)
    notify: Callable[[str], None] | None = None
    for segment in _segments(text):
        counters.segments.append(segment)
        if counters.verdict is not None:
            continue
        run = _unit_run(counters.segments)
        if run is not None:
            repeats, unit = run
            counters.verdict = LoopVerdict(
                kind="phrase", detail=f"{repeats}x: {' '.join(unit)}"
            )
            notify = _TRIP_NOTIFIER
    return notify


def _unit_run(tail: deque[str]) -> tuple[int, list[str]] | None:
    """The tightest unit repeated `REPEAT_PHRASE_THRESHOLD` times at the END
    of `tail`, as `(repeats, unit)`; None when no unit reaches it.

    Sizes `p = 1..MAX_UNIT_SEGMENTS` are tried ASCENDING, so the smallest
    unit that trips is reported: a pair run is not explained as a single
    segment repeated. Each size is measured by walking the chain of `p`-blocks
    backwards from the end and stopping at the first mismatch, so a run must
    be exactly aligned end to end — a stream that merely CONTAINS repeated
    blocks (a shifted alignment, an interleaved segment) never counts.
    """

    n = len(tail)
    for p in range(1, MAX_UNIT_SEGMENTS + 1):
        if 2 * p > n:
            break  # no room for two blocks, and every larger p has less
        unit = _block(tail, n - p, p)
        repeats = 1
        while (repeats + 1) * p <= n and _block(tail, n - (repeats + 1) * p, p) == unit:
            repeats += 1
        if repeats >= REPEAT_PHRASE_THRESHOLD:
            return repeats, list(unit)
    return None


def _block(tail: deque[str], start: int, p: int) -> tuple[str, ...]:
    """The `p` segments of `tail` starting at `start`. A deque is not sliceable."""

    return tuple(tail[start + i] for i in range(p))


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
