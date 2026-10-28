# Core Runtime

The `core/` subpackage is where the agent loop is wired together. It is a thin orchestration layer on top of `coreouto` — it doesn't implement an agent loop itself, just translates miniouto's CLI/storage model into coreouto's primitives.

`src/miniouto/core/__init__.py` re-exports `chat`, `events`, `lma`, `providers`, `runtime` (but **not** `context`, which is an implementation detail of `runtime.build_runtime`).

## Files

| File | LOC | Purpose |
|---|---|---|
| `chat.py` | ~320 | Per-turn chat runner, history persistence, failure diagnostics, sink dispatchers |
| `context.py` | ~230 | Context-window monitoring (lma `/model` caps, invariant-4 floors), the explicit summary trigger |
| `error_rules.py` | ~190 | Per-format `ErrorRule` lists for coreouto >= 0.10 provider-level `error_handling` |
| `events.py` | ~115 | `LoopEvent`, `EventSink` protocol, `NullSink`, `ConsoleEventSink` (CLI rendering) |
| `inbox.py` | ~185 | Cross-process message inbox: `ALARM_HEADER`, `insert` / `drain` / `pending_count` / `prune`, `InboxMessage` |
| `lma.py` | ~140 | `lma.blp.sh` REST client: list_providers, list_models, get_model, find_provider, slugify |
| `providers.py` | ~135 | Maps `Provider` records → coreouto provider classes; clears global state; maps lma SDKs to coreouto formats |
| `reasoning.py` | ~200 | Resolves reasoning choices into provider-native request kwargs from lma `reasoning_options`; UI picker helpers |
| `runtime.py` | ~400 | `RuntimeConfig`, `build_runtime` (incl. role mode), inbox alarm hook, watchdogs, history repair |
| `spawn_state.py` | ~130 | File-based liveness markers for detached `chat --bg` runs (`SpawnMarker`, pid-aware `pending_spawns`) |
| `summary.py` | ~330 | The explicit summarization contract: the two pinned messages, `SUMMARY_FORMAT_SPEC`, `Handoff` state, the run-contract slot |

## Data flow at the core layer

```
ChatOptions (CLI flags + ChatOverrides)
   │
   ▼
resolve_runtime_from_settings(overrides) → RuntimeConfig
   │
   ▼
build_runtime(runtime, *, style_overrides, provider_config,
              on_tool_call, on_tool_result, on_response, on_thinking,
              on_stream_text, on_stream_thinking, on_iteration,
              on_provider_error, reasoning) → co.Agent
   │
   │  ┌─ clear_coreouto_state()
   │  ├─ build_coreouto_provider(runtime.provider)      → co.register_provider
   │  ├─ build_coreouto_provider(subagent_provider)
   │  ├─ tools.registry.register_all()                   → Bash/Image/Video/Audio
│  ├─ parse_style + expand_prompt_tokens              → outo body + list[SubagentSpec] (token-expanded; nothing else is prepended)
   │  ├─ ROLE MODE: runtime.role picks the matching spec's prompt as the system prompt
   │  ├─ co.register_agent_preset("outo", …)               (ONE preset; BASE_TOOLS, + Computer when registered)
    │  ├─ co.register_hook(BEFORE_TOOL_CALL, _make_tool_call_logger(on_tool_call))
    │  ├─ co.register_hook(AFTER_TOOL_CALL, _make_tool_result_logger(on_tool_result))
    │  ├─ co.register_hook(ON_ITERATION, make_context_monitor(...))  ← asks for a summary at 80% (inbox, not compaction)
    │  ├─ co.register_hook(BEFORE_LLM_CALL, _make_alarm_hook(session, on_inbox_messages))   ← drains core/inbox.py
     │  ├─ co.register_hook(ON_ITERATION, _make_iteration_logger(on_iteration))
     │  ├─ co.register_hook(AFTER_LLM_CALL, _make_response_logger(on_response))
     │  ├─ co.register_hook(ON_THINKING, _make_thinking_logger(on_thinking))
     │  ├─ co.register_hook(ON_STREAM_TEXT / ON_STREAM_THINKING, _make_stream_logger(...))
     │  └─ co.register_hook(ON_PROVIDER_ERROR, _make_provider_error_logger(on_provider_error))
    │
    ▼
 run_chat(ChatOptions, sink=None) → str
   ├─ wrap sink in _RecordingSink (captures LoopEvents for the session turn)
   ├─ load history (if continue_session): session history dicts → co.Message.model_validate
    ├─ asyncio.run(_run_with_watchdog(agent, prompt, core_msgs, sink=..., cancel_event=...))
    │     ├─ agent.call() runs as a task; a 5s poll watches the contrib tracker states
    │     │    (activity / progress / live-messages — installed by build_runtime)
    │     ├─ silent > API_STALL_TIMEOUT_SECONDS and phase != "tool_call"
    │     │    → cancel task, sanitize live history, restart with resume prompt (max 3×)
    │     │    repetition verdict (checked FIRST) → cancel task, sanitize live history,
    │     │    restart with the [SYSTEM ALARM] text AS the user message (own 3× budget)
    │     ├─ cancel_event set → cancel task promptly, raise LoopCancelledError
    │     ├─ ON_ITERATION hooks → context monitor inserts ONE summary request at 80% (once per turn); emit progress LoopEvent
   │     ├─ LLM call → provider → response
│     ├─ AFTER_LLM_CALL hook → on_response(content, has_tool_calls) → LoopEvent
     │     ├─ ON_THINKING hook → on_thinking(text) → LoopEvent(kind="thinking")
     │     ├─ cross-process message arrived (spawn completion alarm, or a human
     │     │    interjection) → BEFORE_LLM_CALL inbox hook appends the joined text as
     │     │    ONE user message → LoopEvent(kind="alarm"|"user", detail="inbox:<id>")
     │     └─ other tool call?
   │          ├─ BEFORE_TOOL_CALL → _make_tool_call_logger bridges to on_tool_call closure
   │          ├─ handler(**args)
   │          └─ AFTER_TOOL_CALL → on_tool_result(name, result) → LoopEvent(kind="tool_result")
    ├─ on exception → finish turn as "interrupted" (record-only error event)
    │   → _dump_failure_diagnostics (re-raise)
    └─ _finish_turn: history = Response.messages minus system (full rewrite);
       stamp the running TurnRecord done (assistant + recorded events)
```

---

## `core/events.py`

The sink layer — a tiny abstraction so the chat loop can emit progress/trace events without coupling to either the CLI (`rich.Console`) or the TUI (Textual widgets). Introduced when the TUI was added so `run_chat` could drive both surfaces from the same code path.

- **`LoopEvent`** — dataclass with `actor: str` (always `"outo"` — plus `"provider"` / `"watchdog"` for synthetic events), `kind: str`, `text: str`, optional `tool_name`, optional `subagent_id` / `subagent_name` (**spawn bookkeeping, not actor identity** — they are copied off the drained inbox message's `meta`, and are `None` for `kind="user"` messages), optional `detail` (extra payload not shown by the CLI — the full untruncated Bash command on `kind="tool"`, and `inbox:<id>` on drained inbox messages), optional `msg_ref` (`str(id(message))` of the live coreouto `Message` the event came from — the prune key for invariant 18). Kinds: `"tool"` / `"tool_result"` / `"response"` / `"thinking"` / `"context"` / `"error"` / `"wakeup"` / `"alarm"` / `"user"` / `"subagent_start"` / `"subagent_end"` (the last two survive only for old session files). `to_dict()`/`from_dict()` provide the sparse JSON form stored in session turns (optional fields are only serialized when truthy, so older session files load unchanged).
- **`EventSink`** — `Protocol`: `begin_working()`, `update_activity(text)`, `end_working()`, `emit_loop_event(event)`, `emit_final_answer(content, session_name)`, `emit_stream_delta(kind, text)`, `remove_events(msg_refs)`. `emit_stream_delta` is **transient and display-only** (`kind` is `"text"` or `"thinking"` — the `StreamKind` literal): deltas are incremental fragments of a message the model is still generating, never records. All four implementers must define it — hooks are global and there is no `__getattr__` delegation, so a missing method raises `AttributeError` on the first delta. `NullSink` and `ConsoleEventSink` are no-ops (the CLI renders whole messages only, keeping its output byte-identical), `_RecordingSink` forwards without recording, `TUIEventSink` coalesces into a live row. `remove_events(msg_refs)` is the display-prune entry point: it drops every recorded event carrying one of those `msg_ref`s, forwards to the wrapped sink (the TUI removes matching rows, the console prints a dim `[removed N corrupted messages from the record]` note), and is called **only** by the two removal sources (history repair + `sanitize_history`) — never by the summary hand-off, which changes sessions rather than editing history.
- **`NullSink`** — no-op implementation (used when `run_chat` is called without a sink).
- **`ConsoleEventSink`** — CLI implementation. Renders loop events as `{actor}: {text}` in `orange3`; `kind="thinking"` renders the **full** reasoning text as `{actor}:thinking: {text}` (dim); `subagent_start`/`subagent_end` render as a single-line preview (whitespace-flattened, 120 chars; `subagent_end` dim); `kind="tool_result"` is **ignored** (early return — the events exist for the TUI; CLI output stays byte-identical to the pre-tool_result behavior); `kind="alarm"` falls into the default branch and prints the whole alarm text as `outo: [SYSTEM ALARM - THIS IS NOT A USER MESSAGE] …`, since the user needs the result-file path in the transcript; runs a `rich.status` spinner updated by `update_activity`; writes the final answer as plain stdout followed by a `------finish------` marker.

---

## `core/chat.py`

### Module-level state

- `_tool_trace: list[dict]` + `_tool_trace_lock: threading.Lock` — last 5 tool calls observed this turn, used by failure diagnostics. Locked because tool handlers may run concurrently.
- `_LOGGABLE_TOOL_NAMES = ("Bash", "Image", "Video", "Audio", "Computer")` — the live tool set, one entry per tool registered by `tools/registry.py`, so every tool call a turn can make gets a readable `_short_arg_summary` in failure output instead of a raw `repr(args)`. Delegation is deliberately absent: it rides on `Bash` (`miniouto chat … --role <tag>`), and a delegated run's completion comes back as an inbox alarm message, not as a tool call.
- `pending_responses` / `pending_thinking` / `turn_refs` — buffers created in `_run_chat_prepared` (see the dispatchers below). Since every actor is `outo`, each is keyed by `""`. `turn_refs` is the `str(id(assistant_message))` published by `ON_ITERATION` and read by the two tool dispatchers, which is why a call row and its result row always share one `msg_ref`.
- `_fail_console = rich.Console(stderr=True, soft_wrap=False, highlight=False)` — for diagnostic output (failure trace + traceback to stderr).
- Imports `EventSink, LoopEvent, NullSink` from `.events`.

### `ChatOptions`

```python
@dataclass
class ChatOptions:
    prompt: str
    session: str | None = None
    provider: str | None = None
    model: str | None = None
    style: str | None = None
    role: str | None = None           # ROLE MODE — the <name> tag body to run as
    subagent_provider: str | None = None
    subagent_model: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    reasoning: str | None = None
    continue_session: bool = False
    cancel_event: threading.Event | None = None
```

Mirrors the CLI flags for `miniouto chat`. The CLI is responsible for translating `--name` → `session`, `--continue` → `continue_session`, etc. `role` is set **only** by `cli/chat.py` from `--role`, never by the TUI — see "Role mode" under `core/runtime.py`.

### `ToolCallArgsError(Exception)`

Raised when an LLM tool call arrives with non-dict or `None` arguments. This prevents the cryptic `TypeError: 'NoneType' object is not iterable` that would fire when coreouto calls `handler(**None)`.

### `run_chat(opts: ChatOptions, sink: EventSink | None = None) -> str`

The main entry point. `sink=None` defaults to `NullSink()`; either way the sink is wrapped in a `_RecordingSink` that captures every `LoopEvent` (for the session's turn record) before delegating. Steps:

1. Resolves a `RuntimeConfig` from settings + overrides.
2. Computes `provider_config` — `max_tokens` defaults to `get_max_output_tokens(runtime.model, runtime.provider_name)` so providers with low hard caps (Anthropic's 1024) don't truncate long tool calls (e.g. heredoc file writes). The `provider_name` argument scopes the lma lookup so we don't accept the first cross-provider match.
3. Builds the sink dispatchers (`_make_tool_call_dispatcher`, `_make_tool_result_dispatcher`, `_make_response_dispatcher`, `_make_thinking_dispatcher`, `_make_stream_dispatcher` ×2, `_make_iteration_dispatcher`, `_make_provider_error_dispatcher`, `_make_alarm_dispatcher`) plus the `on_iteration` / `on_history_repair` / `on_messages_removed` persistence bridges, and calls `build_runtime(..., on_inbox_messages=_make_alarm_dispatcher(sink))` to get a `co.Agent`.
4. Loads prior history if `continue_session=True` via `_load_coreouto_history` — session `history` dicts are validated back into `co.Message` objects; invalid entries degrade to plain text (media block lists are flattened via `_flatten_media_blocks`) but **keep `tool_call_id`/`name` for tool messages** so tool_use/tool_result pairing survives even unknown schema drift (without the id the provider 400s the whole request on resume).
5. Clears `_tool_trace`, runs the turn as `asyncio.run(_run_with_watchdog(agent, opts.prompt, core_msgs, sink=sink, cancel_event=opts.cancel_event, on_removed=sink.remove_events))` inside try/except — see the watchdog subsection below. There is no subagent observer to install and no level to pass: one process supervises one `"outo"`. The whole prepared run is wrapped by `run_chat` in a `try/except` that calls `providers.clear_coreouto_state()` before re-raising: hooks are global and close over this turn's sink and persister, so a turn dying during resolve/build (a provider deleted mid-session, a malformed style) must not leave the previous turn's hooks registered against the new state.
6. On `LoopCancelledError` (the `cancel_event` was set), finishes the turn as `status="interrupted"` and re-raises WITHOUT `_dump_failure_diagnostics` — a user force-stop is a clean cancel, not a failure. On `LoopRepeatedError` (its own branch — a repeat is not a cancel) it records `repetition-stop: …` and follows the same interrupted/failure path. On any other exception, appends a record-only `kind="error"` LoopEvent (so a session reload shows why the turn ended), finishes the turn as `"interrupted"`, calls `_dump_failure_diagnostics`, re-raises.
7. `_finish_turn`: on success rewrites the session `history` with `[_dump_message(m) for m in response.messages if m.role != "system"]` (media blocks flattened to text placeholders, tool pairing fields always preserved) and stamps the running `TurnRecord` done with `assistant` + recorded events; on cancel/error the incrementally persisted on-disk history is kept (it already reflects mid-turn progress). Persistence failures are swallowed — they must never mask the turn's outcome.
9. Calls `sink.emit_final_answer(response.content)`; returns `response.content`.

### Incremental persistence (`_TurnPersister`)

The session is written **throughout** the turn, not just at the end, so a force-killed process (SIGKILL, terminal close, hard crash) leaves a fully reloadable record instead of rolling back to the previous turn:

- `begin_turn` appends the turn as `status="running"` before the loop starts (and reaps stale running turns from dead writers).
- `_RecordingSink.on_event` → `update_turn_events` on nearly every loop event; `thinking`/`context` events are throttled to one write per 0.5 s, structural events flush immediately.
- The `ON_ITERATION` dispatcher additionally calls `persist_history`, which runs `sanitize_history` (drops the trailing assistant message whose tool calls have no results yet — ON_ITERATION fires before tool execution) and stores it via `update_history`. Written unconditionally — every run in this process IS the session's transcript.
- Combined with the atomic `sessions.save` (tmp file + `os.replace`), the worst-case loss on SIGKILL is a sub-second thinking line.

### `_run_with_watchdog(agent, prompt, history, *, sink, cancel_event, states=None) -> co.Response`

Thin level-0 wrapper around `core/runtime.py:supervised_run` (see below): runs outo's `agent.call()` supervised, emitting `watchdog:` loop events (`kind="wakeup"`) on each restart, and wires the user's `cancel_event` into the fast path (ESC ESC hard-cancels even an in-flight LLM call within one 5 s poll — the hook-boundary cancel guard alone cannot interrupt one).

### `supervised_run(start, prompt, history, *, live_key, on_wakeup, cancel_event=None, states=None, restart_on_repetition=None, on_repetition=None, on_removed=None)` (`core/runtime.py`)

The stall watchdog, following coreouto `examples/27_wakeup.py` + `examples/28_resume_interrupted.py` (coreouto >= 0.11.1). `start(prompt, history)` creates one attempt's coroutine; `start(None, history)` must resume from a transcript alone. **There is no `level` parameter**: one process supervises one actor. That covers both a `chat`/TUI turn and a delegated `chat --role <tag>` run — a delegated run is a normal supervised turn in its own process, so it gets the same stall watchdog and the same repetition restart; what it does **not** share is the parent's event stream. `live_key` is `"outo"`. A 5-second poll (`WATCHDOG_CHECK_SECONDS`) reads the contrib tracker states installed by `build_runtime` (`activity_tracker_hook` + `loop_progress_hook` + a BEFORE_LLM_CALL live-messages capture keyed by actor — see `_register_watchdog_trackers`):

- **Stall wakeup** — when `activity.seconds_since_last_activity() > API_STALL_TIMEOUT_SECONDS` (30 min) AND `progress.phase != "tool_call"` AND no deeper supervisor is active (`_ACTIVE_SUPERVISORS`), the attempt task is cancelled, the actor's live message list is recovered via `sanitize_history` (drops system messages and any trailing assistant turn with unanswered tool calls — providers 400 on dangling tool calls), `on_wakeup` fires, and the call restarts **without a user message**: coreouto >= 0.11.1's `call(history=...)` sends the sanitized transcript exactly as declared and simply continues. If nothing is recoverable (wedged before the first LLM call), it falls back to the original prompt. Max `WATCHDOG_MAX_WAKEUPS = 3` restarts, then `LoopStalledError`.
- **Supervisor deferral** — a supervisor stays quiet while another supervisor is active (`_ACTIVE_SUPERVISORS`): the inner call's supervisor owns the silence and does the cancelling/restart in place. Harmless with one supervisor in practice; the registry survives so the check can be asserted rather than assumed.
- **Repetition restart** (`restart_on_repetition` = the actor key, `"outo"`) — checked **before** the stall test, because a looping model is *active*, not wedged, so the stall path would never fire for it. On a sticky verdict the attempt is cancelled, the live history sanitized, `on_repetition` fires, and the call restarts with the **alarm as the user message**: `start(alarm, history)`, or `start(f"{prompt}\n\n{alarm}", history)` when no transcript is recoverable (otherwise the task itself would be lost). The alarm is deliberately **not** routed through the inbox: a `drain()` by `_make_alarm_hook` would deliver the same text a second time. Budget: `REPEAT_MAX_RESTARTS = 3`, independent of `WATCHDOG_MAX_WAKEUPS`, then `LoopRepeatedError`. The deferral above does not apply to it — it is per-actor and only this supervisor consumes its own actor's verdict.
- `on_removed` is the display side of **both** restart paths: every `sanitize_history` truncation reports the discarded messages' refs there, so the record loses the events of a turn the resumed attempt no longer has. A stall keeps the answered prefix; a repetition restart keeps it plus the alarm. A discarded tail is a discard either way.
- **Phase exemption** — while `phase == "tool_call"` the watchdog stays quiet: tools carry their own bounds (Bash: `BASH_TIMEOUT_SECONDS`). A healthy nested agent keeps the global activity clock fresh through its own hook firings, so only a genuinely silent tool-free loop wakes a supervisor.

The SDK timeout (`API_STALL_TIMEOUT_SECONDS` on every provider constructor) + `TIMEOUT_ERRORS` rules catch hung HTTP requests; supervisors catch everything else (wedged between operations, deadlocked hook, ...). The resumed call's `Response.messages` is the sanitized transcript plus the new turns — no injected user message, so the persisted session history reads as one uninterrupted conversation.

### `LoopStalledError` / `LoopRepeatedError` (`core/runtime.py`)

`LoopStalledError` — raised when a supervisor exhausts its wakeups: the loop re-wedges within the stall limit every time, so something is deterministically wrong (e.g. a deadlocked hook). Surfaces through the normal failure-diagnostics path.

`LoopRepeatedError` — raised when the repetition budget is exhausted: the model kept repeating itself through every restart. A deliberate **sibling of `LoopStalledError`, not of `LoopCancelledError`** — a repeat is the model's own failure, never a user stop, so the cancellation branch must not swallow it. `core/chat.py:run_chat` has its own `except LoopRepeatedError` branch that records a `repetition-stop: …` error event, stamps the turn interrupted, and re-raises.

### `LoopCancelledError(Exception)` (`core/runtime.py`)

Raised by the cancel guard from a hook once the user's `cancel_event` is set. coreouto's hook `trigger` does not swallow exceptions, so it propagates out of the call and terminates the loop at the next hook boundary.

### `_actor_label() -> (str, str | None)`

Kept as the single place that answers "who is speaking"; it now always returns `("outo", None)`. The ContextVars it once read (`_SUBAGENT_DEPTH` / `_SUBAGENT_NAME` / `_SUBAGENT_ID`) are **deleted** along with in-process jobs — a spawned run is a separate process, so there is nothing in this address space to attribute. The dispatchers still call it rather than hardcoding the string, so the actor plumbing stays uniform.

### `_make_tool_call_dispatcher(sink: EventSink)`

Builds the `on_tool_call(name, arguments)` closure passed to `build_runtime`. For each tool call:

1. Validates `arguments` is a dict via `_validate_tool_call_args` (raises `ToolCallArgsError` otherwise).
2. Computes actor + subagent id via `_actor_label()`.
3. Feeds the repetition detector (`note_tool_call`) **before any skip branch** — a model re-issuing the same call forever must trip. Appends to `_tool_trace`.
4. Emits a `LoopEvent(kind="tool", text=f"{name} {preview}", subagent_id=sid)` and updates the spinner activity. For `Bash` the event also carries `detail=arguments["command"]` — the **full untruncated command, verbatim** (no newline flattening) — for sinks that want the raw input; `detail` is `None` for the media tools.

Carries the per-actor `msg_ref` from `refs` (published by the iteration dispatcher), so the row and its result are pruned together.

### `_make_tool_result_dispatcher(sink: EventSink)`

Builds the `on_tool_result(name, result)` closure wired into coreouto's `AFTER_TOOL_CALL` hook (via `_make_tool_result_logger`), where `result` is coreouto's `ToolResult` (`.content`, `.blocks`, `.is_error`, `.flatten_text()`). For each tool result:

1. Emits for every tool (`Bash` / `Image` / `Video` / `Audio` / `Computer` / `continue_session`).
2. `text = result.flatten_text()`, prefixed with `"error: "` when `result.is_error` (the convention the TUI keys on), truncated to 4000 chars with a `"\n… [truncated]"` suffix when over.
3. Emits `LoopEvent(kind="tool_result", text=text, tool_name=name, subagent_id=sid, msg_ref=refs.get(sid or ""))` with the current actor/subagent id via `_actor_label()` — the **same** `msg_ref` the `tool` event carries, which is what keeps a removed message from orphaning its result row. The CLI's `ConsoleEventSink` ignores this kind; it exists so the TUI can display tool return values.

If the dispatcher was built with `on_handoff`, and this tool result left a pending `summary.pending_handoff()`, it calls `on_handoff()` — that is how the summary hand-off ends the loop cleanly.

The whole body is wrapped in `try/except Exception` — a sink failure here must never break the agent loop.

### `_make_thinking_dispatcher(sink: EventSink)`

Builds the `on_thinking(thinking)` closure wired into coreouto's `ON_THINKING` hook. The text is **stashed** per actor and flushed by `_make_iteration_dispatcher` — coreouto fires `ON_THINKING` *before* it appends the assistant message, so there is no message to stamp a `msg_ref` with at that point. Repetition counting still runs here, at the earliest possible moment; only the display is deferred.

### `_make_response_dispatcher(sink: EventSink, pending: dict)`

Builds the `on_response(content, has_tool_calls)` closure. When `has_tool_calls` is true (intermediate LLM response that triggers a tool call), the text is **stashed in `pending`** (keyed by actor: always `""` now) instead of being emitted immediately — coreouto fires `AFTER_LLM_CALL` *before* `ON_THINKING`, so an immediate emit would place the model's text above its own thinking row. The stash is flushed by `_make_iteration_dispatcher` (below), which `ON_ITERATION` guarantees runs after `ON_THINKING` — every sink therefore sees `thinking` → `response` in that order. The stashed tuple carries the actor captured at stash time.

### `_make_stream_dispatcher(sink: EventSink, kind: StreamKind)`

Builds the `on_stream_delta(delta)` closure wired into coreouto's `ON_STREAM_TEXT` / `ON_STREAM_THINKING` hooks, which fire **once per incremental fragment** (not the accumulated text) whenever the provider streams. It calls `sink.emit_stream_delta(kind, delta)` — a display-only path — and in addition feeds `repetition.note_stream_delta(actor_key, delta)` and the turn's `_PartialStreams` (the verbatim text of the message in flight, which the force-stop records as a truncated answer). Stream feeding is what catches a model repeating inside **one** never-ending answer, whose boundary is the call returning, minutes later; the detector buffers the unfinished tail and counts each *completed* segment, sharing one splitter with the whole-message path so nothing is double-counted. A cross-process (`--bg`) run has no hooks left once its turn ends — its deltas never reach this sink at all.

### `_make_alarm_dispatcher(sink: EventSink)`

Builds the `on_inbox_messages(messages)` callback that `runtime._make_alarm_hook` forwards drained inbox messages to (wired in as `build_runtime(on_inbox_messages=…)`). The hook has **already** put each drained text into the model's live message list; this only records it: one `LoopEvent(actor="outo", kind=message.kind, text=message.text, subagent_id=message.meta.get("id"), subagent_name=message.meta.get("role"), detail=f"inbox:{message.id}")` per message. `kind` mirrors the inbox kind (`"alarm"` / `"user"`), the actor is `outo` because the message is outo's own next input rather than a spawned process speaking, and `subagent_id`/`subagent_name` are **spawn bookkeeping** carried over from the message's `meta` — both `None` for a human interjection. `detail="inbox:<id>"` is the optimistic-row suppression key: a message typed into the TUI mid-turn is rendered immediately, and this matching event is dropped rather than rendered twice.

### `_make_iteration_dispatcher(sink: EventSink, pending, pending_thinking, refs)`

Builds the `on_iteration(*, iteration, messages, response, **kwargs)` closure. `ON_ITERATION` is where the turn's message identity becomes available: coreouto appends the assistant message and only then fires this hook, so `messages[-1]` IS the message every event of this iteration came from. Its `str(id(...))` is published into the per-actor `refs` slot and stamped as `msg_ref` onto everything flushed here — the stashed thinking and response text, and the progress line. The tool dispatchers read the same slot, which is why a `tool` event and its `tool_result` always share one ref and are pruned together. Flush order per iteration is thinking → response → context (net display order); the context monitor lives separately in `core/context.py`, registered as a second `ON_ITERATION` hook. This dispatcher also feeds the phrase detector (`_note_repetition_text`) on the whole response text.

### `_make_provider_error_dispatcher(sink: EventSink)`

Builds the `on_provider_error(*, status_code, error_message, reaction, reaction_message, **kwargs)` closure wired into coreouto's `ON_PROVIDER_ERROR` hook. Emits a `LoopEvent(actor="provider", kind="error", text="HTTP {code} → {reaction}: {message}")` for every rule-matched provider error, and switches the spinner activity to `"provider retry"` for retry reactions. This is the only surface for rule-matched errors — they no longer raise out of the turn (see `core/error_rules.py` below), so without this hook a retry storm or a 401 would be invisible.

### `_dump_failure_diagnostics(exc, session_name)`

On exception, prints to **stderr** via `_fail_console`:
- `✗ {type(exc).__name__}: {exc}` in red.
- A "Last tool call before failure" header followed by the last 5 traced tool calls (or a "No tool call observed" notice).
- For known tools, a one-line summary via `_short_arg_summary`; otherwise `repr(args)[:160]`.
- A full traceback via `traceback.format_exc()`.

Then re-raises so the caller (`chat_cmd` or the TUI worker) can handle or propagate.

### `_short_arg_summary(name, args)`

Per-tool one-liner:

| Tool | Format |
|---|---|
| `Bash` | joined command, no length cap (literal `\n` → space) |
| `Image` / `Video` / `Audio` | just the path |
| `continue_session` | `args["summary_dir"]`, or `?` |
| (other) | `str(args)[:120]` |

---

## `core/context.py`

### Constants

- `CONTEXT_SUMMARY_TRIGGER_PCT = 80` — the measured context usage (%) at which the model is **asked** to summarize. Deliberately late: the remaining window is still enough to write the summary documents and hand off, which is the only thing left that needs context.
- `SUMMARY_SOURCE = "context"` — the `source` stamped on the injected request, so a receiver can tell a context trigger apart from a delegated run's completion alarm.
- `DEFAULT_MAX_OUTPUT_TOKENS = 16384` — hard floor. Rationale: Anthropic defaults to 1024 if you don't set it explicitly, silently truncating long tool-call outputs (e.g. file writes).
- There is no ceiling. The previous `MAX_OUTPUT_TOKENS_CEILING = 16384` was a defense against the legacy `lcw-api.blp.sh/context-window` endpoint reporting inflated theoretical streaming caps; lma reports accurate per-request non-streaming caps so the clamp is no longer needed.

### `_fetch_model_caps(model, provider_name=None) -> dict[str, int]`

Calls `lma.get_model(model, provider_name)`. Reads `info["context_window"]` and `info["max_output_tokens"]` from the dict that `lma.get_model` returns (lma already unwraps the `models` array server-side). On any exception (network, JSON, etc.) it returns an empty dict **without caching it** (fail-soft, never raises) — caching that empty dict would pin the key to "no caps" (compaction off, 16K floor) for the life of the process off a single network blip. A successful answer IS memoized in the module-level `_MODEL_CACHE`, including a genuinely empty result ("lma answered, no info for this key"). That cache has **no TTL** (unlike `lma._CACHE`'s 10 minutes); `clear_model_cache()` drops it.

### `get_context_window(model, provider_name=None) -> int | None`

Returns the model's context window in tokens, or `None` if unknown. Passing `provider_name` scopes the lma lookup (recommended — otherwise lma returns matches across every provider and we only see the first).

### `get_max_output_tokens(model, provider_name=None) -> int`

Resolution order (highest wins):
1. Per-provider `max_output_tokens` override (set via the TUI custom-model editor; read fresh each call by `_provider_caps_override`).
2. `caps["maxOutputTokens"]` (from lma).
3. `caps["contextWindow"]` (fallback proxy).
4. `DEFAULT_MAX_OUTPUT_TOKENS` (16K hard floor).

No ceiling clamp. See `docs/lma.md` for rationale.

### `make_context_monitor(*, model, session, provider_name=None) -> Callable | None`

Builds the single `ON_ITERATION` hook that **asks** for a summary at the threshold. There is no compaction and no second LLM: nothing here rewrites the transcript.

- `build_runtime` builds a fresh monitor per turn (the same lifecycle as the watchdog trackers), so "fires at most once" is per turn with no module-level state to reset.
- Returns `None` (no hook at all) when the window size or session name is unknown — a threshold needs a denominator, and inventing a window would fire the request at an arbitrary moment.
- `hook(*, messages=None, response=None, **_kwargs)`:
  1. `_tokens_used(response, messages)` measures occupancy, best-effort: `response.usage.prompt_tokens` → `usage.total_tokens` → `_estimate_tokens` (chars / 4 over the live messages, including tool-call arguments). `None` (no message at all) → stay silent.
  2. `pct = used * 100 // window`; below `CONTEXT_SUMMARY_TRIGGER_PCT` it returns.
  3. At the threshold it sets the one-shot flag and does exactly one thing: `inbox.insert(session, build_summary_request(...), kind=inbox.ALARM_KIND, source=SUMMARY_SOURCE)`. The insert runs under `contextlib.suppress(Exception)` — a broken inbox must not take the turn down; the request is a courtesy, not part of the loop's contract.
- Delivery rides the existing `BEFORE_LLM_CALL` inbox hook, which is what makes the request **recorded, replayed and rendered** exactly like any other alarm rather than a private side channel.

---

## `core/summary.py`

The explicit summarization contract — pure vocabulary, no disk, no provider, no registry.

### The two pinned messages

Both texts are part of the model-facing contract, so they are literals with only the `{...}` values substituted. They share the harness's standard alarm header, `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]` (`core/inbox.py:ALARM_HEADER`), followed by one message-specific line: the context-budget reading for the request, the "continue the previous work here" note for the continuation.

- **`SUMMARY_REQUEST_HEADER_TEMPLATE`** = `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]`, then `Context budget: {pct}% of this session's window is consumed ({used}/{window} tokens). Explicit summarization is required now.`
- **`CONTINUATION_HEADER`** = `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]`, then `New session opened from a summary — continue the previous work here.`

### `summary_dir_for(session) -> Path`

`<INVOCATION_CWD>/.miniouto/summary/<session>/`. **Project-local, not under `~/.miniouto/`**: a summary is a project artifact, meant to be read, reviewed and committed with the work it describes. Raises `ValueError` on an empty session name.

### `build_summary_request(*, session, pct, window_tokens, used_tokens, summary_dir) -> str`

The message asking the model to summarize before its context runs out. Sections, in order:

1. the header + the context-budget line (`Context budget: {pct}% ... Explicit summarization is required now.`), whose token parenthetical (`{used}/{window} tokens`) is dropped rather than rendered with a hole when either figure is `None`;
2. the current situation — the token figures (window / used);
3. **WHY SUMMARIZE** — the remaining window isn't enough to finish, and *this harness does not summarize implicitly*: you write the summary yourself, as files;
4. **HOW TO SUMMARIZE** — (1) `mkdir -p <summary_dir>` via Bash, (2) write documents per `SUMMARY_FORMAT_SPEC`, (3) the bar: reading only that folder must be enough to continue;
5. **AFTER SUMMARIZING** — call `continue_session(summary_dir="<dir>")`; the current conversation ends there and a new session continues from the summary; a delegated run's alarm-session / result-file contract is carried over automatically;
6. **IF YOU SKIP THIS** — skipping it fails at the context limit or loses earlier content, repeats work, or reverses settled decisions.

### `SUMMARY_FORMAT_SPEC`

The document layout, embedded verbatim: `./.miniouto/summary/<session>/` holding `00-overview.md` (required) plus any number of `NN-<topic>.md` (optional). The overview has **seven** sections: `## 1. Background & Goal (Why)` / `## 2. Current State (What)` / `## 3. Key Decisions & Rationale (Decisions)` / `## 4. Next Work (Next)` / `## 5. Gotchas & Verification (Gotchas & Verify)` / `## 6. File Map (File map)` / `## 7. Original Brief & Contract (Brief & contract)`.

### `build_continuation_message(*, new_session, from_session, summary_dir, contract=None) -> str`

The message that opens the NEW session: the shared header + "New session opened from a summary — continue the previous work here.", the summary folder, **WHAT TO DO** (read every document, `00-overview.md` first; continue `from_session`'s work), the **CAUTIONS** (the summary is a record of the past — verify against the code; the conversation was **not** carried over; follow its "Next Work" order; check files and test state before starting; keep updating the summary as you go) and **RECOMMENDED**. When `contract` is present, a **DELEGATION CONTRACT (carried over)** block appends one line per present key — role/style share an identity line, alarm session / result file get their own — and states that the contract must be completed in this session.

### Hand-off state and the run contract

- `Handoff` — frozen dataclass: `new_session`, `from_session`, `summary_dir`. Plus `request_handoff(...)` / `pending_handoff()` (peek) / `consume_handoff()` (read + clear) / `clear_handoff()`, guarded by a module lock. In-process by design: a delegated run is its own process, so nothing here has to survive the turn.
- `set_run_contract(*, session, role=None, style=None, alarm_session=None, return_path=None)` / `get_run_contract()` (returns a copy) / `clear_run_contract()`. Empty values are dropped, so a plain outo run clears the slot instead of recording a blank block. `cli/chat.py:_register_run_contract` publishes it before the turn; the `continue_session` tool reads it so the hand-off can restate what the run owes its caller.

---

## `tools/continue_session.py`

`continue_session(summary_dir: str) -> str` (sync, in `BASE_TOOLS`, so outo and every delegated role run have it). The only way a session changes.

1. `_resolve` turns a relative path into `INVOCATION_CWD / path` (`~` expanded, `.resolve()`d) and validates it is an existing directory holding at least one `*.md`. **Anything else** — empty, missing, not a directory, no markdown — returns the reason as the tool result (never an exception), so the model can write the documents and retry. It deliberately does *not* judge whether the summary is *good*: only the session that reads it can.
2. `_mint_session` produces `cont-<YYYYMMDD-HHMMSS>-<6hex>`.
3. `inbox.insert(new_session, build_continuation_message(...), kind=inbox.ALARM_KIND, source="summary", meta={"from": from_session})`.
4. `summary.request_handoff(new_session, from_session=..., summary_dir=str(directory))` — the entry layer consumes this; the loop layer never does.
5. `_ack` tells the model the session's conversation is over, names the new session and folder, repeats that a delegation contract travelled along, and ends with "do no more work here, end your turn".

### The hand-off, end to end

1. **The loop layer stops, it does not move.** `run_chat` passes a `_StopOnHandoff` (a `threading.Event` subclass whose `is_set()` ORs the caller's cancel event with its own hand-off flag) as the turn's `cancel_event`, so `build_runtime`'s cancel guard sees it like any user stop. The `LoopCancelledError` is then **re-classified at the call site**: if `summary.pending_handoff()` is set, the turn is stamped `status="done"` with a final answer naming the summary folder and the new session, the incrementally persisted history is kept, and **the hand-off stays pending** — only the entry layer can move a conversation.
2. **The entry layer moves it.** `cli/chat.py:_run_handoffs` checks `pending_handoff()` after each turn; `_apply_handoff` switches `opts.session` and re-registers the run contract under the new session. The next turn passes that name directly into runtime resolution. A plain run publishes the new name for later unnamed `-c` wakeups; a delegated guest does not update `settings.session`. It prints `handoff_marker` (`------summary → <new>------` + `summary: <dir>`) followed by `------<new>------`. The continuation message is already waiting in the new session's inbox, so a one-shot drain (`_continuation_prompt`) turns it into the next prompt — and rebuilds it from `core.summary.build_continuation_message` plus the run contract if that drain came back empty. The same run continues; this is the ONE loop inside `run_turn_chain`, and it is not a wait on anything external.
3. **`HANDOFF_MAX_TRANSITIONS = 5`** bounds the chain (`summary → cont-1 → cont-2` …); past it the chain warns, naming the session and the summary folder, and stops.
4. **The TUI does the same thing without a chain.** `ChatTUI._apply_handoff` mounts the marker as a system row in the **old** conversation, persists it there for replay (`_persist_handoff_marker`, a trailing turn holding one `handoff` event), re-pins the new session, and lets the 1 s inbox poll auto-deliver the continuation.
5. **The post-step reports the session it ended in.** `ChainState.session` is what the alarm's `Session:` line, the result file's name and a `--bg` marker's `session` field use; `_handoff_note` (`Continued from: <old> (summary: <dir>)`) is added to the alarm and, as a trailing `---` section, to the result markdown — which still leads with the answer.

---

## `core/error_rules.py`

Single source of truth for the provider-level `error_handling` lists (coreouto >= 0.10). coreouto 0.10 replaced agent-level `retry_intervals` (removed, along with the `ON_RETRY` hook) with per-provider `list[ErrorRule]` matching: each rule matches a provider exception by `status_code` (or google-genai's `.code`) plus an optional `content_contains` substring of the rendered error (first match wins) and reacts with one of:

| Reaction | Loop behavior |
|---|---|
| `retry` | Sleeps `retry_after * retry_backoff^attempt`, up to `retry_max` attempts, then re-raises. Fires `ON_PROVIDER_ERROR` before EACH attempt and re-calls `provider.create()` with the current messages — this is what makes in-place history repair possible |
| `terminate` | Ends the loop with `Response(stop_reason="failed", content=rule.message)` |
| `tool_result` | Appends an `is_error=True` tool result to the last assistant tool call so the model self-corrects. **Not used by these lists** — see below |
| `user_message` | Injects `rule.message` as a user message and continues |

Why no `tool_result` reaction: a 400/422 about tool calls means the poison is IN the request history, so appending a synthetic result and resending the same payload loops forever (coreouto `examples/29_malformed_tool_call.py` — each repetition also piles a duplicate result onto the same tool call, its own 400 reason). These lists use `reaction="retry"` (retry_max=3, zero delay) for every tool-call 400/422 instead, paired with `core/runtime.py:_make_history_repair_hook` — the hook fires before each retry attempt and repairs the live message list in place (pair/dedupe first, then cut one suspect turn per attempt), so the retry actually reaches the model. If all repair passes fail, the exception raises and the turn ends.

`default_error_handling(api_format)` returns the per-format list; the lists mirror coreouto's own recipes (examples 18-20 in the coreouto repo), tuned to each SDK's exception hierarchy:

- **openai / openai-response** — shared list built on `contrib.error_presets.COMMON_HTTP_ERRORS` (429/500/503 retry, 401/403 terminate) plus 400 splits (`context_length_exceeded` → terminate; `invalid_schema`/`tool` → retry+repair), 404+model → terminate, 422 → retry+repair.
- **anthropic** — adds the Anthropic-specific 529 overload retry and 413 terminate; 400+context → terminate; 400+tool → retry+repair; 422 → retry+repair.
- **google** — google-genai collapses all 4xx into `ClientError`, so the 400 rules split by `.status`-enum substrings (`tool`/`safety`/`precondition`) ahead of a generic 400 → retry+repair fallback, plus 404 → terminate. Ordering matters — the specific matchers precede the fallback.
- **all formats, last entries** — coreouto's `TIMEOUT_ERRORS` preset (coreouto >= 0.11): matches the SDK timeout exceptions raised by the per-request stall timeout (`API_STALL_TIMEOUT_SECONDS = 1800`, `core/providers.py`) by `exc_type` (MRO class names — `APITimeoutError` for openai/anthropic, `TimeoutException`/`TimeoutError` for google's httpx) and retries with backoff. This retry is the HTTP-level stall **wakeup**: a hung request is aborted by the SDK and coreouto re-issues it instead of the turn dying silently. It stays last so the specific status-code rules above always win. Anything the SDK timeout can't catch (a loop wedged outside an HTTP request) is covered by the watchdog in `core/chat.py:_run_with_watchdog`.

The stall timeout itself is wired through the coreouto >= 0.11 provider constructor `timeout` parameter (seconds) in `_instantiate` — coreouto forwards it to the SDK client and handles google's millisecond `HttpOptions` conversion internally. Semantics are the SDKs' httpx-level timeout: a request dies only when no bytes arrive for 30 minutes — an actively streaming response is never cut. There is deliberately no hard *total*-duration cap; coreouto offers no asyncio-level cancellation point inside `provider.create` (the watchdog cancels from OUTSIDE the call instead).

Rule-matched errors **do not raise** — they surface via the `ON_PROVIDER_ERROR` hook (forwarded to the sink as `provider:` loop events by `_make_provider_error_dispatcher`). Unmatched errors (e.g. network failures with no `status_code`) propagate to `_dump_failure_diagnostics` as before.

---

## `core/providers.py`

### `SUPPORTED_FORMATS`

```python
SUPPORTED_FORMATS = ("openai", "openai-response", "anthropic", "google")
```

Whitelist of `Provider.api_format` values. Validated by `cli/provider.py:add_custom` and by `_instantiate`.

### `sdk_to_format(sdk, api) -> (api_format, base_url)`

Maps an lma provider's `sdk`/`api` pair to a `(api_format, base_url)` tuple. Returns `(None, None)` when the SDK cannot be hosted by any supported coreouto builtin (e.g. `amazon-bedrock`, `bedrock`, anything else without an `api` URL). Unknown SDKs with a non-null `api` URL fall back to `("openai", api)` — the universal gateway shape. Used by `cli/provider.py:providers_cmd` (catalog browse) and `cli/provider.py:add_cmd` (catalog add), plus the TUI `_catalog_add_flow` to filter `lma.list_providers` output to addable entries. See `docs/lma.md` for the full mapping table.

### `add_provider_from_lma(name, api_key, sdk, api, default_model="", reasoning_effort=None) -> Provider`

Builds a `storage.providers.Provider` from lma metadata + a user-supplied API key, with `source="lma"` set. `reasoning_effort` is stored verbatim on the Provider (see step 11 of `build_runtime`). Raises `ValueError` when `sdk_to_format` returns `(None, None)`. Used by `cli/provider.py:add_cmd` (catalog add) and `cli/tui.py:_catalog_add_flow`.

### `build_coreouto_provider(provider: Provider) -> None`

Validates `api_format`, instantiates the right coreouto class via `_instantiate`, and calls `co.register_provider(provider.name, instance)`. The instance is constructed with `api_key=provider.api_key or None` and `base_url=provider.base_url or None` (Google wraps `base_url` in `http_options={"base_url": ...}` instead).

### `_instantiate(api_format, api_key, base_url)`

Lazy `import` of each provider module (avoids loading all four SDKs at startup). Raises `ValueError("Unsupported api_format: …")` for anything not in `SUPPORTED_FORMATS`. Every instance is constructed with `error_handling=default_error_handling(api_format)` (see `core/error_rules.py`), so outo and subagent providers share the same retry/terminate behavior.

### `clear_coreouto_state()`

Wipes four coreouto globals: `clear_providers()`, `clear_agent_presets()`, `clear_tools()`, `clear_hooks()`. Called at the start of every `build_runtime` to make the function idempotent across CLI invocations (the process is long-lived for TUI mode).

### `reset_subagent_registration()`

Docstring: *"Remove stale registry entries left over from a prior run."* Body is a single `with contextlib.suppress(Exception): co.clear_tools()` — a defensive "never let a stale tool registration break a run" guard. Since delegation is a separate `chat` process (not an in-process tool registration), nothing in the current code path calls it; the real reset is `clear_coreouto_state()` at the top of every `build_runtime`.

### `provider_kwargs(provider_config, passthrough)`

Helper that bundles the two dicts under the keys coreouto's `AgentConfig` expects. Defined but currently unused in the main code paths; `build_runtime` does its own merging.

---

## `core/inbox.py` — the cross-process message inbox

A session's inbox is `inbox/<session>/`, one JSON file per pending message. It is the durable hand-off from a detached `chat --bg` process back to the session that launched it: writer and drainer are separate OS processes, so the channel has to survive the writer exiting.

| Symbol | Purpose |
|---|---|
| `ALARM_HEADER = "[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]"` | First line of every completion alarm; the single source of truth shared with `repetition.alarm_text`, `cli/chat.py`, and the TUI |
| `ALARM_KIND = "alarm"` / `USER_KIND = "user"` / `VALID_KINDS` | Both kinds are **user messages** to the model; `alarm` is a system announcement, `user` a human/script interjection |
| `MAX_INBOX_MESSAGES = 200` / `MAX_TEXT_CHARS = 100_000` | Prune bound and per-message text cap |
| `InboxMessage` | Frozen `(id, kind, text, ts, source, meta)`; `id` is the filename stem |
| `inbox_dir(session)` | `INBOX_DIR / session`; a blank name is a `ValueError` |
| `insert(session, text, *, kind, source, meta) -> str` | Validate → write `<id>.tmp` → `os.replace` into place → `prune`; returns the id |
| `drain(session) -> list[InboxMessage]` | `listdir → read → unlink`, oldest first; unlinks only after a successful parse |
| `pending_count(session)` / `prune(session, keep=200)` | Count files / delete the oldest beyond `keep` |
| `_read_one` / `_quarantine` | Parse one file; on corrupt content rename it `<stem>.bad` and skip — **never raises** |

Two rules follow from the file-per-message shape:

1. **Insert-vs-drain is safe by construction.** A writer never appends to a file somebody else is reading — it creates its own file and renames it atomically — so a drainer sees a message either not at all or complete. An append-only `.jsonl` could not offer this: consuming it needs a lock held across the read while the writer waits on it.
2. **One consumer per session, deliberately.** Nothing serializes two *drainers*: whichever process lists the directory first unlinks the file and the other never sees it. Two live processes bound to one session would steal each other's messages — isolate them with a distinct `MINIOUTO_HOME` (or session name) instead of adding a locking protocol here.

Delivery is at-least-once (the unlink happens after the parse, so a crash re-delivers rather than drops).

## `core/spawn_state.py` — liveness markers for detached runs

`miniouto chat --bg` detaches a real OS process, so an in-memory job registry inside the launching process cannot tell another process whether that run is still going to alarm it. One JSON marker per run under `spawn-logs/<id>.json`, next to the run's stdio log, is that signal.

| Symbol | Purpose |
|---|---|
| `SpawnMarker` | Frozen `id`, `alarm_session`, `started`, `role`, `style`, `return_path`, `status` (`running`/`done`/`error`/`stopped`), `finished`, `log`; `to_payload()` writes exactly those keys |
| `write_marker(marker)` | Atomic write (tmp + `os.replace`), returns the path |
| `finish_marker(job_id, status, *, finished=None)` | Stamp a terminal status; idempotent, no-op when the marker is absent |
| `pending_spawns(session) -> list[SpawnMarker]` | Every marker of `session` still claiming `running`, sorted by id |
| `read_marker(path)` | Parse one marker; missing or corrupt content yields `None`, never a raise |

A monitor's whole rule is `pending_spawns(session)` — non-empty means "something may still write an alarm into this session's inbox". Nothing inside `miniouto chat` waits on it: a run answers its prompt, prints one `------finish------` and exits, and the queued alarm is delivered by the next wakeup (`chat --alarms -c`, or the TUI's poll). The filter is pid-aware, so it keeps only markers whose process is still alive (`is_pid_alive`; an unknown pid counts as alive). **Liveness is a promise, not a proof**: a SIGKILLed run never calls `finish_marker`, so its marker would say `running` forever — the pid probe is what makes that harmless for a monitor.

## `core/repetition.py` — repetition watchdog

A model that has stopped making progress rarely goes quiet, so the stall watchdog never fires for it. This module catches exactly two shapes, counting **exactly** (identical normalized segments only — no edit distance, no model call), so a verdict is deterministic and cheap. Both counters measure a **consecutive RUN, never a total** (a legitimately repeated lead-in scattered over a long turn must never warn — only back-to-back repetition means a stuck loop). Tool counting is the byte-identical streak (`REPEAT_TOOL_THRESHOLD` in a row). Phrase counting generalises that to a **unit** of 1..`MAX_UNIT_SEGMENTS` segments: a single phrase repeated 50x in a row, a pair "A B" repeated 50x in a row, a three-clause cycle repeated 50x — all the same threshold, because a unit is what actually repeats. Sizes are tried ascending so the smallest unit that trips is the one reported, and the chain is compared from the END of the segment stream, so a stream that merely contains repeated blocks (a shifted alignment, one interleaved segment) never counts. "A B A B ..." trips because "A B" is one consecutively repeated unit; "A B C D A F C A B B A" is normal speech — the words recur but no run does. Runs **span messages** (a loop alternating two phrases across ten messages is the same loop); `reset_phrases` is the sole exception, for a discarded tail.

| Symbol | Purpose |
|---|---|
| `REPEAT_PHRASE_THRESHOLD = 50` | The same phrase UNIT repeated this many times back to back (a unit is 1..`MAX_UNIT_SEGMENTS` segments, so a pair repeated 50x trips at the same number as a single phrase) |
| `REPEAT_TOOL_THRESHOLD = 30` | The same tool call (name + `json.dumps(args, sort_keys=True)`) this many times back to back |
| `REPEAT_MAX_RESTARTS = 3` | A supervised turn's own restart budget, independent of `WATCHDOG_MAX_WAKEUPS` |
| `MIN_SEGMENT_CHARS = 4` | Shorter segments ("ok.", "yes!") are junk and not counted — they never enter the stream, so they neither grow nor break a run |
| `MAX_UNIT_SEGMENTS = 8` | Longest phrase unit, in segments, that run detection recognises; bounds the check to at most 8 block comparisons per segment |
| `note_text(actor_key, text)` / `note_tool_call(actor_key, name, arguments)` | Count one message / one tool call toward that actor's detectors |
| `check` / `consume_verdict(actor_key)` | Peek at a sticky verdict / take it and clear the counters, so a resumed attempt must earn a new one |
| `reset()` / `reset_actor(actor_key)` | Drop every / one actor's counters; `reset()` runs once per turn from `runtime._register_watchdog_trackers` (counters live for one turn) |
| `flush_stream(actor_key)` | Count the buffered, still-unfinished tail as a final segment (message boundary + force-stop path), so a phrase cut mid-sentence is never lost |
| `reset_phrases(actor_key)` | Discard a dropped tail's run state and buffer WITHOUT flushing — the stall-restart path, so a sanitized-away tail cannot leak into the resumed attempt |
| `alarm_text(verdict)` | The exact resumed user message: the phrase shape tells the model to proceed with a *different pattern*; the tool shape adds *try a different method* if repeating is not producing progress |

Segments come from splitting on newlines, sentence enders (`.?!。？！`), then collapsing whitespace and casefolding. The actor key is always `"outo"` — there is no other actor in a process — and one lock covers every actor. Counters are fed from the whole-message paths (the thinking dispatcher, the iteration dispatcher's response flush, and every tool call) **and** from the stream deltas (invariant 8), because message-boundary counting cannot see a model repeating inside one never-ending answer. A tool call is not a phrase segment, so it neither grows nor breaks a run, symmetric with the tool streak, which narration does not break.

**There is no unsupervised actor.** `cancel_background_loop` is **deleted**: every run — a `chat`/TUI turn *and* every delegated `chat --role <tag>` run, which supervises itself inside its own process — executes inside `supervised_run`, so a trip is always enforced by restart, never by cancelling a job.

---

## `core/runtime.py`

### Module-level constants / state

- `BASE_TOOLS = ["Bash", "Image", "Video", "Audio"]` — the tool set every run gets. Delegation is **not** a tool: it rides on `Bash` as `miniouto chat "<brief>" --style <style> --role <tag>` (AGENTS.md invariant 5), so a Bash-less harness could not delegate at all.
- `PLATFORM_GATED_TOOLS = ["Computer"]` — **platform-gated** tools (the constant is just "tools appended when the platform registered them"). It is appended in role mode too: a delegated run is a separate process with its own vwayland screens, so there is no shared screen to interleave on, and the tool list must stay identical to outo's. The list is filtered down to what `register_all` actually registered (`co.get_tool(t) is not None`), so an unsupported platform never sees `Computer` in either mode.
- The `_SUBAGENT_DEPTH` / `_SUBAGENT_NAME` / `_SUBAGENT_ID` ContextVars and the `_SUBAGENT_OBSERVER` slot are **deleted** along with in-process jobs. A delegated run is a separate OS process, so there is nothing in this address space to attribute: every actor is `outo`. Spawn metadata still rides on `LoopEvent.subagent_id` / `subagent_name`, copied off the drained inbox message's `meta`.
- `_ACTIVITY_STATE` / `_PROGRESS_STATE` / `_LIVE_MESSAGES` — the watchdog's per-turn contrib-tracker state. `_LIVE_MESSAGES` is keyed by the literal `"outo"`.
- `_ACTIVE_SUPERVISORS: dict[object, int]` — tokens of the supervisors currently running. The `level` bookkeeping is gone (one actor, one supervisor); the registry stays so the supervisor can tell "a supervisor of mine is active" from a future second one, and so tests can assert nothing leaked.

### Prompt assembly: the token-expanded style body

The system prompt is **one layer**: `expand_prompt_tokens(<style body>)` and nothing else. The implicit cwd preamble and the automatic skill-catalog prepend are **deleted** (neither helper exists any more), and there is no compatibility fallback.

Before (two extra layers on top of every body):

```
<cwd preamble>
<# Available Skills catalog from ./skills/ (local) + ~/.agents/skills>
<the style body>
```

After (the body alone; the style decides the layout):

```
<the style body, with {cwd} / {skills} / {skills:…} / {this_session} / {style} / {subagent_pm} expanded>
```

A body naming no tokens tells the model no working directory and no skills, which is a legitimate choice. Both the outo body and every role body go through the same call, so there is no second assembly path. See `docs/styles.md` § Dynamic tokens for the per-token rules and `AGENTS.md` invariant 3 for the invariant form.

### Role mode (`miniouto chat "<brief>" --style <style> --role <tag>`)

A delegated run executes **the same agent on the same `"outo"` preset**; exactly one thing changes:

1. `runtime.role` selects a parsed `SubagentSpec` by name and uses **its prompt as the system prompt** (`build_runtime`, right after token expansion). An unknown tag raises `ValueError` listing the available roles — and `cli/chat.py:_validate_role` catches a typo even earlier, before the turn starts, so a bad `--role` costs no model call. `role=None` runs the `<outo>` body as before.

That is the whole difference. **The tool list, the preset, and every hook are identical in both modes** — including `Computer`, which is why it is no longer gated on `runtime.role is None`. (The old shared-screen rationale for hiding it is void: separate processes own separate vwayland screens.)

The role prompt is one of the specs built by `_resolve_both_styles`, expanded by the same `expand_prompt_tokens` call as the outo body — the same single layer outo gets, with the same tokens available to it. A role body that needs the working directory or the skill catalog places `{cwd}` / `{skills}` itself; nothing is added implicitly. There is no second assembly path to drift. `ChatOptions.role` is set only by `cli/chat.py` from `--role`; the TUI never sets it.

### `_make_alarm_hook(session, on_messages=None)`

The `BEFORE_LLM_CALL` hook that drains the **cross-process inbox** into the live message list, **mid-turn**. The session it drains is `runtime.session`, resolved directly from `ChatOptions.session` via `ChatOverrides.session`; the published `settings.session` is only a fallback when no turn selected one. A **delegated** run thus drains its own inbox without ever rewriting the global active session. When no turn is active the delivery path is different and lives outside this hook: the TUI polls its session's inbox every second (`ChatTUI._deliver_pending_messages`) and starts a turn whose prompt IS the joined message text, and `miniouto chat --alarms -c [--name <s>]` does the same explicitly — drained queue as the prompt, `-c` mandatory, the named session required to exist, an empty inbox a no-op — see AGENTS.md invariant 16. A plain one-shot `chat` **never waits**: `run_turn_chain` is one turn plus hand-off transitions, so anything queued afterwards stays in the queue for the next wakeup. Registered **before** the watchdog's `capture_messages` hook, so the supervisor's live message list already carries the delivered messages.

- **No depth gate.** The inbox is per-session, not per-actor, so any run bound to that session consumes it.
- `inbox.drain(session or "default")`, join the texts with a blank line, and append **one** `co.Message(role="user")` to the live `messages` list, so the very next `provider.create` already carries it.
- **Appends directly, never `Agent.inject_user_message`**: that queue is drained at the TOP of the loop iteration (coreouto `agent.py:301-307`), i.e. *before* this hook, so a message queued from here would land a full LLM round-trip later — and it could never wake a turn that already ended.
- **Fold rule**: if the last message is already a user message the text is folded into it — providers reject two consecutive user messages, the same hazard `_cut_poisoned_turn` works around.
- The batch is forwarded to `on_messages` (from `core.chat._make_alarm_dispatcher`), and the whole body runs under `contextlib.suppress` so a broken sink can never break the provider path.

**Who writes into that inbox:** the delegation post-step in `cli/chat.py` (`_run_delegated` → `_complete`). A delegated run writes its answer file, inserts **exactly one** alarm (`kind="alarm"`, `source="chat"`, first line `ALARM_HEADER`), and stamps its `SpawnMarker` — in a `finally`, so success, error and Ctrl-C all report the same way, and only once the whole run has ended (after any summarization hand-offs). See `docs/cli.md` § `Delegation: the post-step`.

### `RuntimeConfig`

```python
@dataclass
class RuntimeConfig:
    provider_name: str
    model: str
    style_name: str = "default"
    role: str | None = None                # ROLE MODE: run as this style tag; None = outo
    subagent_model: str | None = None      # the defaults a spawned run inherits
    subagent_provider: str | None = None   # falls back to `provider_name`
    subagent_reasoning: str | None = None  # falls back to subagent provider's `reasoning_effort`
    session: str | None = None             # defaults to "default"
```

Resolved per-call configuration.

### `ChatOverrides`

```python
@dataclass
class ChatOverrides:
    provider: str | None = None
    model: str | None = None
    style: str | None = None
    role: str | None = None
    subagent_provider: str | None = None
    subagent_model: str | None = None
```

Per-call overrides from CLI flags. All optional.

### `build_runtime(runtime, *, style_overrides=None, provider_config=None, on_tool_call=None, on_tool_result=None, on_response=None, on_thinking=None, on_stream_text=None, on_stream_thinking=None, on_iteration=None, on_provider_error=None, on_history_repair=None, on_messages_removed=None, on_inbox_messages=None, reasoning=None, cancel_event=None) -> co.Agent`

The heart of miniouto. Steps:

1. **Slow resolution, all hoisted ABOVE the clear** — provider lookup, `_resolve_both_styles` file I/O, `expand_prompt_tokens` per body (which reads the skill roots a body names), both reasoning passthroughs, and the context monitor's window lookup (`make_context_monitor`). Everything below the `clear_coreouto_state()` line must be pure in-memory dict work: the clear wipes coreouto's global registries, so a network fetch or file read inside the clear+register window leaves them empty for as long as it takes.
   - **`clear_coreouto_state()`** — Reset all four coreouto registries.
   - **`cancel_event` guard** — If a `threading.Event` is passed, a `_make_cancel_guard` hook is registered FIRST on both `BEFORE_LLM_CALL` and `BEFORE_TOOL_CALL`: once the event is set the hook raises `LoopCancelledError`, which coreouto's `trigger` does not swallow, so it propagates out of the turn. This is cooperative cancellation — the loop stops before the next step; an in-flight tool execution is not interrupted by the guard (Bash polls the same event and kills its own process). Independently, the watchdog's poll (`core/chat.py:_run_with_watchdog`) checks the same event every 5 s and hard-cancels the task, so even an in-flight LLM call is interrupted promptly.
2. **Provider registration** — `build_coreouto_provider(provider)` for `runtime.provider_name` (looked up above the clear; a missing provider is a `RuntimeError`).
3. **`tools.registry.register_all(api_format=provider.api_format)`** — Registers `Bash/Image/Video/Audio` and, when `computer_supported()`, `Computer` (with `parallelizable=False`). The `api_format` refreshes the module flag that decides, at call time, whether `screenshot` may return an image or a notice.
4. **`parse_style` + `expand_prompt_tokens`** — Loads the named style document (or `builtin_default` for `"default"`) and parses it into `(outo_body, list[SubagentSpec])`, then expands the style tokens in the outo body and in **every** spec body. That expansion **is** the system prompt: nothing else is prepended. Where `{cwd}` (`INVOCATION_CWD`) and `{skills}` land, and whether they appear at all, is the style's own decision; a body without them tells the model neither. Re-expanded on every call, never persisted.
5. **Role selection** — If `runtime.role` is set, the matching spec's prompt becomes `system_prompt` (see "Role mode" above); otherwise the outo body. `ValueError` on an unknown role.
6. **Register the preset** — `co.register_agent_preset("outo", model=…, provider=…, system_prompt=…, tools=outo_tools, max_iterations=None)`. **ONE preset, two modes**: `outo_tools` is `BASE_TOOLS` plus `PLATFORM_GATED_TOOLS`, filtered down to what actually registered — in **both** modes. There are no per-role presets and no per-role tool list: a role run *is* the outo run with a different system prompt.
7. **Register hooks:**
   - `BEFORE_TOOL_CALL` → `_make_tool_call_logger(on_tool_call)` (only if `on_tool_call` is not None).
   - `AFTER_TOOL_CALL` → `_make_tool_result_logger(on_tool_result)` (only if `on_tool_result` is not None — `chat.run_chat` always supplies one).
   - `ON_ITERATION` → `context_monitor` from `make_context_monitor(model=runtime.model, session=runtime.session or "default", provider_name=runtime.provider_name)` — registered only when the window size is known (the hook is `None` otherwise).
   - `ON_ITERATION` → `_make_iteration_logger(on_iteration)` (only if `on_iteration` is not None — `chat.run_chat` always supplies one).
   - `AFTER_LLM_CALL` → `_make_response_logger(on_response)` (only if `on_response` is not None).
   - `ON_THINKING` → `_make_thinking_logger(on_thinking)` (only if `on_thinking` is not None — `chat.run_chat` always supplies one).
   - `BEFORE_LLM_CALL` → `_make_alarm_hook(runtime.session, on_inbox_messages)` (**always**, like the repair hook — a spawn that finished between turns has no other way to reach the model). Drains `core/inbox.py` and appends the joined texts to the live message list as one user message (folded into a trailing user message when there is one), then forwards the batch to `on_inbox_messages` for the record. Registered **before** the watchdog's `capture_messages` hook so the supervisor's live list already carries them.
   - `ON_STREAM_TEXT` / `ON_STREAM_THINKING` → `_make_stream_logger(on_stream_text)` / `_make_stream_logger(on_stream_thinking)` (only if not None — `chat.run_chat` always supplies both). Registered **in addition to** the watchdog's own tracker registrations for the same two events; each forwards the per-delta `text` payload to the display-only stream dispatcher.
    - `ON_PROVIDER_ERROR` → `_make_provider_error_logger(on_provider_error)` (only if `on_provider_error` is not None — `chat.run_chat` always supplies one).
    - `ON_PROVIDER_ERROR` → `_make_history_repair_hook(on_history_repair)` (**always**, unlike the sink loggers) — repairs the live message list in place before each retry attempt of the 400/422 tool-call rules in `core/error_rules.py`. Each fire first runs `_repair_pairing` (drop duplicate tool results, fill dangling tool calls on the last assistant turn with error results, inserted at the correct position right after that turn's existing results); if that changed nothing, `_cut_poisoned_turn` excises ONE suspect assistant tool-call turn plus its own results — preferring a turn with structurally malformed (non-dict) arguments, else the most recent tool-call turn, so repeated attempts walk backwards through history — and folds an explanatory note into the preceding user message (or inserts one), preserving later turns and the user's current prompt. Whenever a pass mutates the list, the `on_history_repair` callback (wired by `core.chat.run_chat`) persists the repaired history, so the next turn doesn't reload the same poison from disk. Gated to `reaction == "retry"` with status 400/422; rate-limit/overload retries pass through untouched.
    - **Watchdog trackers** (`_register_watchdog_trackers`, always) — `activity_tracker_hook` on `AFTER_LLM_CALL`/`AFTER_TOOL_CALL`/`ON_ITERATION`/`ON_PROVIDER_ERROR`/`ON_STREAM_TEXT`/`ON_STREAM_THINKING`, all five `loop_progress_hook` entries, and a `BEFORE_LLM_CALL` live-messages capture stored under the literal key `"outo"`. Clears `_LIVE_MESSAGES` and calls `repetition.reset()` — repetition counters are scoped to one turn, exactly like the live lists. States are exposed via `watchdog_states()` for `supervised_run`.
8. **Finalize the config** — `co.get_agent_preset("outo").to_config()`, merge in caller's `provider_config` and the resolved `provider_passthrough`, instantiate `co.Agent(outo_config)`. Returns the agent.

> **Reasoning resolution** (done above the clear, listed here for the precedence rules) — `core.reasoning.resolve_reasoning_passthrough(api_format, model, provider_name, choice)` turns the resolved reasoning choice plus lma's per-model `reasoning_options` into provider-native request kwargs, merged into `provider_passthrough` for both outo and subagent. This is what makes providers return thinking blocks at all (the `ON_THINKING` display path is dead without it). It must go through `provider_passthrough`, not `provider_config`: miniouto registers providers under user-chosen names, and coreouto's `normalize_provider_config` passes config through **untranslated** for unknown provider names — a canonical `reasoning_effort` key would reach the SDK raw and `TypeError`. Precedence (highest wins) for outo: `chat --pm '…:REASONING'` > the session pin's `reasoning` (`session-config/<session>.toml`) > `Provider.reasoning_effort` (stored per provider) > lma's default for the model's option type (effort → `"medium"` or the middle non-`"none"` value; toggle/budget → `"on"`) > off (no kwargs when lma has no reasoning data — no blind defaults). For the **subagent**, the choice inserts one extra tier: the `--pm` reasoning suffix (applies to both) > `RuntimeConfig.subagent_reasoning` (the pin's `subagent_reasoning`, else the persisted `settings.subagent_reasoning` via `miniouto subagent set --reasoning`) > the subagent provider's `reasoning_effort` > lma default. `"none"`/`"off"` always disables. Only two request shapes are ever emitted — an effort value and anthropic's `thinking: {"type": "adaptive"}`. Token-budget shapes (`enabled` + `budget_tokens`) are deliberately NOT emitted: many providers reject budget parameters, so lma `budget_tokens` models are driven as a plain toggle and legacy digit choices degrade to `"on"`. Mapping:

    | api_format | lma option type | passthrough |
    |---|---|---|
    | `anthropic` | toggle / budget_tokens | on → `{"thinking": {"type": "adaptive"}}`; off → `{}` |
    | `anthropic` | effort | `{"thinking": {"type": "adaptive"}, "output_config": {"effort": v}}` (`"none"` → `{}`) |
    | `openai-response` | effort | `{"reasoning": {"effort": v}}` (`"none"` → `{}`) |
    | `openai-response` | toggle / budget_tokens | on → `{"reasoning": {"effort": "medium"}}` |
    | `openai` | effort | `{"reasoning_effort": v}` (`"none"` → `{}`) |
    | `openai` | toggle / budget_tokens | on → `{"thinking": {"type": "enabled"}}` |
    | `google` | any | `{}` — **unsupported**: coreouto builds `GenerateContentConfig` itself from only system_instruction + tools and extra kwargs land on `generate_content(**kwargs)` top-level, so a `thinking_config` key would TypeError |

    With an explicit choice but **no** lma data, the choice is applied per-format best-effort (`"on"`/digits → the toggle mapping; otherwise the effort mapping). `core/reasoning.py` also exports `reasoning_choices(model, provider_name)` and `default_reasoning_choice(model, provider_name)` for UI pickers (both fail soft to `None` on lma errors).

### Hook helpers

- **`_make_tool_call_logger(callback)`** — Bridges coreouto's `BEFORE_TOOL_CALL` hook signature to the simpler `on_tool_call(name, args)` contract used by `chat.py`. Forwards `(name, arguments)` to `callback`.
- **`_make_tool_result_logger(callback)`** — Bridges coreouto's `AFTER_TOOL_CALL` hook signature to the simpler `on_tool_result(name, result)` contract. Forwards `(name, result)` to `callback`; this hook is the only surface for tool return values (BEFORE_TOOL_CALL fires before the handler runs).
- **`_make_response_logger(callback)`** — Returns an `AFTER_LLM_CALL` hook that invokes `callback(response.content, bool(tool_calls))` for non-empty responses. No printing — rendering is the sink's job.
- **`_make_iteration_logger(callback)`** — Returns an `ON_ITERATION` hook that forwards to `callback` so the sink can render progress.
- **`_make_thinking_logger(callback)`** — Returns an `ON_THINKING` hook that invokes `callback(thinking)` for each LLM response carrying reasoning text. Providers never put thinking into history messages (coreouto's `format_assistant_message` drops it), so this hook is the only surface for reasoning.
- **`_make_provider_error_logger(callback)`** — Returns an `ON_PROVIDER_ERROR` hook that forwards `status_code`, `error_message`, `reaction`, `reaction_message` (dropping the raw exception and the message list, which a sink can't render).
- **`_make_history_repair_hook(on_repair, on_removed)`** — Returns the `ON_PROVIDER_ERROR` repair hook paired with the retry rules in `core/error_rules.py` (see step 9 of `build_runtime` and the `core/error_rules.py` section). Uses `_repair_pairing` / `_cut_poisoned_turn`; both report the `str(id(message))` of every message they removed through `on_removed`, so the display record can be pruned beside the repair.
- **`_make_alarm_hook(session, on_messages)`** — Returns the `BEFORE_LLM_CALL` hook that drains `core/inbox.py` and appends the messages to the live message list. See its own section above.
- **`_make_stream_logger(callback)`** — Returns an `ON_STREAM_TEXT` / `ON_STREAM_THINKING` hook forwarding one incremental `text` fragment per firing (not the accumulated message). Display-only.
- **`_make_cancel_guard(cancel_event)`** — Returns the pair of `BEFORE_LLM_CALL` / `BEFORE_TOOL_CALL` hooks that raise `LoopCancelledError` once the event is set.
- **`_resolve_both_styles(style_name, overrides)`** — Calls `parse_style` on the raw style; returns `(outo_body, list[SubagentSpec])`. Each spec's prompt is later token-expanded by `build_runtime` through the same `expand_prompt_tokens` call as the outo body, so a role run gets exactly the same single layer.
- **`_read_raw_style(name, overrides)`** — Checks in-memory `overrides` first, then `style_store.read(name)`, then the builtin default.
- **`expand_prompt_tokens(text, *, session, style)`** — Substitutes the five style tokens into a style-derived body, and the result is the whole system prompt. `{cwd}` → `str(INVOCATION_CWD)`; `{skills}` / `{skills:<dir>…}` → the lazy-load skill catalog for the default roots (`<INVOCATION_CWD>/skills` first, then `~/.agents/skills`) or for the named roots (`~` expanded, relative paths against `INVOCATION_CWD`, missing roots skipped silently, entries ordered by root then name, duplicates listed once per root, `""` when nothing is found); `{this_session}` → the run's own session name (`runtime.session`, or `"default"` when empty; for a delegated run this is the explicitly carried guest name, invariant 5); `{style}` → the resolved style name (`runtime.style_name`); `{subagent_pm}` → the effective subagent trio in `--pm` spelling, **left literal** when it does not resolve. The parameterized `{skills…}` regex pass (`_SKILLS_TOKEN`) runs **first**, because the catalog text is itself brace-bearing; then the plain `str.replace` passes in source order (`{cwd}`, then `{this_session}` / `{style}`, then `{subagent_pm}`, which is only computed if the token survives the earlier passes). Applied to the outo body and to **every** role body. Any other `{...}` text is left verbatim: there is no escape mechanism and an unknown token is not an error. The model never sees the token syntax, only the expanded literal, which is what keeps a bundled style's delegation recipe copy-ready and Bash-idempotent (a `--alarm-session` that read `settings.session` behind the caller's back was not the same command twice). See `docs/styles.md` § Dynamic tokens.
- **`_replace_skills_token(match)`** / **`_resolve_skill_roots(arg)`** — The regex callback and the root resolver behind `{skills…}`. An empty argument (bare `{skills}` or `{skills:}`) means the two default roots, local first (`skill_store.default_roots(INVOCATION_CWD)`); entries are comma-separated, stripped, `~`-expanded, and made absolute against `INVOCATION_CWD` when relative. Duplicate roots are kept.
- **`_skills_catalog(roots)`** — Renders the lazy-load catalog: a `# Available Skills` heading, a note that each skill lives in its own directory under `<root>/<name>/` containing a `SKILL.md` to read via Bash when a task matches, then `- <name>: <description>` lines (each line names its root when more than one is listed). Reads each root through `storage.skills.list_skills(root)`. Skill bodies are NOT injected. Returns `""` when no root yields a visible skill.
- `_fallback_style(name)` is still used for unreadable style documents and for a role tag with an empty body.

### `resolve_runtime_from_settings(overrides=None) -> RuntimeConfig`

**Three lanes, per key, and the middle one is what makes many processes safe**: explicit override (`--pm` / `ChatOverrides`) > **this session's pin** > the global default.

1. Loads `settings_store.load()` and the optional `ChatOverrides`.
2. Reads the session's pin — `_pinned_for(s, session=session)` → `session_config.effective(overrides.session or s.session or "default", default=default_provider_config)`. `effective` **materializes** the pin: with no pin file it computes the current global default, writes it as the pin, and returns it, so a later global change (the TUI switching models in another process) can never redirect a session that is already running. The explicit `overrides.session` wins over `s.session` and is the same value the inbox hook drains and `{this_session}` expands; no global-session rewrite participates in resolution. Writes are atomic and a corrupt pin is treated as absent rather than raised over.
3. **Override invalidates a foreign pin.** If `overrides.provider` names a provider other than the pinned one, the pinned model/reasoning belong to the provider being left behind, so the whole outo trio is dropped (`dataclasses.replace(pinned, provider="", model="", reasoning="")`) and the global chain answers for the override's provider.
4. Provider: `overrides.provider or pinned.provider or s.provider`; raises `RuntimeError("No default provider set. …")` if all are missing.
5. Model: `overrides.model or pinned.model or provider.default_model`; raises `RuntimeError("No model specified for provider …")` if still missing. (A legacy `s.model` still reads at the bottom, inside `default_provider_config` — old installs keep working, and the TUI never writes it.)
6. Reasoning: `pinned.reasoning or None` — the per-call tier is `overrides`, and a `build_runtime(reasoning=…)` argument still wins over the pin.
7. Style: `overrides.style or pinned.style or s.style or "default"`. Role: `overrides.role or None` (`ChatOptions.role`, set only by `chat --role`).
8. Session: `overrides.session or s.session or "default"`.
9. Subagent (`_resolve_subagent(overrides, s, pinned)` — kept as a standalone helper because `cli/chat.py` calls it under `--use-subagent-provider` while assembling the run's `ChatOptions`; `build_runtime` no longer needs it, and it takes `pinned` as an argument so the caller resolves the pin **once per turn** against its own session):
   - Provider: `overrides.subagent_provider or pinned.subagent_provider or s.subagent_provider or None` — `None` means inherit outo's provider (`build_runtime`'s `runtime.subagent_provider or runtime.provider_name` fallback handles it). When a provider IS resolved but isn't configured in the provider store, raises `RuntimeError("Subagent provider … is not configured. …")`.
   - Model: `overrides.subagent_model or pinned.subagent_model or s.subagent_model or <subagent provider's default_model> or None`. When a subagent provider IS resolved but no model is, raises `RuntimeError("No model specified for subagent provider …")` telling the user to set one in the TUI's `subagent` popup or via `miniouto subagent set --model`. A model-only override (no subagent provider) is valid — the subagent then runs outo's provider with the overridden model. (`None` model only ever reaches `build_runtime` together with a `None` provider, where both inherit outo's.)
10. Subagent reasoning: `pinned.subagent_reasoning or s.subagent_reasoning or None` — populated **independently** of whether a subagent provider/model is set (a reasoning-only override on outo's provider+model is valid). `cli/chat.py` reads it under `--use-subagent-provider`, where an explicit `--pm` reasoning suffix wins and otherwise the pin answers (the run then falls back to its provider's `reasoning_effort`).

Returns a `RuntimeConfig` with `subagent_provider`/`subagent_model`/`subagent_reasoning` populated per the rules above.

**Seeding** is the other half of the pin's safety story: `session_config.seed_from(new, source)` is copy-if-absent, and every mint path calls it — a delegated run's `chat-`/`spawn-` name (`cli/chat.py:_resolve_session`), a summary hand-off's `cont-*` name (`tools/continue_session.py`), and the TUI's "new session". So a new session continues on the provider its source was configured with, instead of freezing whatever the global default happened to be at its first turn.

## External API calls & I/O

| Source | Target | Purpose | Failure mode |
|---|---|---|---|
| `core/lma.py` | `https://lma.blp.sh/{provider,model-list,model}` (GET, 15s timeout, `httpx`) | Discover providers, list models, fetch context + max-output caps | `list_*` / `get_model` propagate `httpx.HTTPError`; `find_provider` swallows and returns `None`; 10-minute in-process cache (`_CACHE`) |
| `core/context.py` | (via `core.lma.get_model`) | Fetch `context_window` + `max_output_tokens` for a model | Caught; a **transport failure is returned uncached** (the empty dict is never stored), so one blip can't pin a model to "no caps" for the process; a successful-but-empty answer IS cached. `_MODEL_CACHE` has no TTL; `clear_model_cache()` mirrors `lma.clear_cache()` |
| `core/providers.py` | `co.register_provider`, `co.clear_*` | coreouto global registry I/O | None caught |
| `core/runtime.py` | `tool_registry.register_all`, `co.register_agent_preset`, `co.register_tool`, `co.register_hook` | coreouto global registry I/O | None caught |
| `core/inbox.py` | `INBOX_DIR/<session>/<ts>-<6hex>.json` | Cross-process message hand-off: one atomically-renamed file per pending message | Insert validates (kind/text/meta) and raises `ValueError`; `drain` never raises — corrupt files are renamed `.bad` |
| `core/chat.py` | `session_store.begin_turn/update_turn_events/update_history/prune_turn_events/finish_turn`, `session_store.load` | JSON session persistence (schema v2, incremental) | Load is tolerant (never raises); write failures swallowed (`_TurnPersister`, `_finish_turn`) |
| `core/spawn_state.py` | `SPAWN_LOG_DIR/<id>.json` | Liveness marker for a detached `--bg` run, read by a monitor (nothing in `chat` waits on it) | Written atomically; `read_marker` never raises (missing/corrupt → `None`) |
| `core/chat.py` | `_run_with_watchdog` → `agent.call(prompt, history=...)` | LLM call (network to configured provider) | Caught locally for diagnostics, re-raised |
| `core/chat.py` | `_fail_console.print(...)` | Human-readable diagnostic lines to stderr | None — best-effort |

No file I/O directly inside `core/` — all disk access is delegated to `storage/` (providers, settings, sessions, styles, skills) and `tools/registry.py`.

## Error & exception patterns

| Class / Pattern | File | When |
|---|---|---|
| `ToolCallArgsError(Exception)` | `core/chat.py` | LLM tool call arrived with non-dict/`None` `arguments` |
| `ValueError("Unsupported api_format: …")` | `core/providers.py` | `build_coreouto_provider` given a format not in `SUPPORTED_FORMATS` |
| `ValueError("Unhandled api_format: …")` | `core/providers.py` | Defensive — `_instantiate` fallthrough (should be unreachable) |
| `RuntimeError("Provider … is not configured.")` | `core/runtime.py` | `provider_store.get(name)` returned `None` in `build_runtime` |
| `RuntimeError("Subagent provider … is not configured.")` / `RuntimeError("No model specified for subagent provider …")` | `core/runtime.py:_resolve_subagent` | The `subagent_*` trio does not resolve — `cli/chat.py` under `--use-subagent-provider` turns either into a clean `✗` + exit 1 |
| `ValueError("Unknown role … Available roles: …")` | `core/runtime.py` | `runtime.role` names a tag the parsed style does not declare (`cli/chat.py:_validate_role` pre-validates first) |
| `RuntimeError("No default provider set. …")` | `core/runtime.py` | Neither override nor settings has a provider |
| `RuntimeError("No model specified for provider …")` | `core/runtime.py` | Neither override nor `provider.default_model` is set |
| `httpx.HTTPError` / `JSONDecodeError` | `core/context.py` | Caught silently inside `_fetch_model_caps` |
| `Exception` from the inbox insert in the context monitor | `core/context.py` | Suppressed; the turn continues (the summary request is a courtesy, not a loop contract) |
| `Exception` from the turn (`_run_with_watchdog`) | `core/chat.py` | Caught to dump tool trace + traceback, then re-raised |
| `LoopStalledError` | `core/runtime.py` → `core/chat.py` | Watchdog exhausted its 3 wakeups — loop re-wedges deterministically; normal failure-diagnostics path |
| `LoopRepeatedError` | `core/runtime.py` → `core/chat.py` | Repetition budget exhausted (3 restarts) — the model kept repeating itself; own branch, not the cancel branch |
| `LoopCancelledError` | `core/runtime.py` → `core/chat.py` | The user's `cancel_event` was set at a hook boundary — clean stop, no failure dump |
| Provider exceptions matching an `ErrorRule` | `core/error_rules.py` + coreouto loop | Absorbed per rule reaction (retry / terminate / user_message); 400/422 tool-call rules additionally repair history via `_make_history_repair_hook` before retrying; surfaced via `ON_PROVIDER_ERROR` → `provider:` loop event, never raised (until repair attempts are exhausted) |

## Defensive patterns worth highlighting

1. **`core/summary.py` is text-only** — it holds the two pinned model-facing messages, the hand-off slot and the run contract, and touches no disk, provider or registry, so the words the model reads and the words the entry layer acts on cannot drift apart.
2. **`chat.py` ToolCallArgsError** — fires *before* coreouto's handler so the LLM sees a precise, single message about which argument is missing, and the user sees the tool name in the failure trace.
3. **`cli/chat.py:_detach`** — the `--bg` child gets `stdin=DEVNULL`, file stdio and `start_new_session=True`. `tools/bash.py` kills whole process groups and grace-kills pipe-holding children, so a background run survives only with **both** halves; without the new session the parent Bash call would take it down, and without file stdio a pipe would keep it attached.
4. **`core/inbox.py`** — file-per-message + `os.replace` makes insert-vs-drain safe without a lock (which a `.jsonl` would need across the read); corrupt content is quarantined as `.bad` instead of raising, exactly like session files; and one consumer per session is a deliberate constraint, not an oversight.
5. **`runtime.py` `_make_alarm_hook`** — appends drained messages directly to the live message list instead of using `Agent.inject_user_message`, whose queue is drained *before* this hook and would delay the alarm by a full round-trip; folds into a trailing user message because providers reject two consecutive user messages; and runs under `contextlib.suppress` so a broken sink can never break the provider path.
6. **`chat.py` `_RecordingSink.remove_events`** — prunes the event list **in place** (slice assignment) because `_TurnPersister` holds that exact list object, notifies the persister unconditionally (a repair can cut a message from an earlier turn of a resumed session, whose events live only in the file), and never fires for a summary hand-off: a hand-off switches sessions and must never erase the human-visible record.
