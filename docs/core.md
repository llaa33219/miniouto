# Core Runtime

The `core/` subpackage is where the agent loop is wired together. It is a thin orchestration layer on top of `coreouto` — it doesn't implement an agent loop itself, just translates miniouto's CLI/storage model into coreouto's primitives.

`src/miniouto/core/__init__.py` re-exports `chat`, `events`, `lma`, `providers`, `runtime` (but **not** `context`, which is an implementation detail of `runtime.build_runtime`).

## Files

| File | LOC | Purpose |
|---|---|---|
| `chat.py` | ~320 | Per-turn chat runner, history persistence, failure diagnostics, sink dispatchers |
| `context.py` | ~185 | Context-window monitoring (lma `/model`), auto-summarization hook |
| `error_rules.py` | ~190 | Per-format `ErrorRule` lists for coreouto >= 0.10 provider-level `error_handling` |
| `events.py` | ~115 | `LoopEvent`, `EventSink` protocol, `NullSink`, `ConsoleEventSink` (CLI rendering) |
| `lma.py` | ~140 | `lma.blp.sh` REST client: list_providers, list_models, get_model, find_provider, slugify |
| `providers.py` | ~135 | Maps `Provider` records → coreouto provider classes; clears global state; maps lma SDKs to coreouto formats |
| `reasoning.py` | ~200 | Resolves reasoning choices into provider-native request kwargs from lma `reasoning_options`; UI picker helpers |
| `runtime.py` | ~400 | `RuntimeConfig`, `build_runtime`, subagent tool, hooks |

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
    │  ├─ parse_style + _load_active_skills               → outo body + list[SubagentSpec]
    │  ├─ co.register_agent_preset("outo", …)
    │  ├─ co.register_agent_preset(<name> for each SubagentSpec, …)
    │  ├─ _build_subagent_tool(specs, …)                  → co.register_tool
    │  │    └─ _wrap_subagent_handler: hands (name, brief) to subagents.submit
    │  │       (skipped when specs is empty — outo-only style)
   │  ├─ co.register_hook(BEFORE_TOOL_CALL, _make_tool_call_logger(on_tool_call))
   │  ├─ co.register_hook(AFTER_TOOL_CALL, _make_tool_result_logger(on_tool_result))
   │  ├─ co.register_hook(ON_ITERATION, make_summarize_hook(...) → (on_iteration, before_llm_call)) + BEFORE_LLM_CALL compaction hook
   │  ├─ co.register_hook(BEFORE_LLM_CALL, _make_alarm_hook(on_alarms))   ← background-job completion alarms
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
    ├─ set_subagent_observer(_make_subagent_dispatcher(sink))
    ├─ asyncio.run(_run_with_watchdog(agent, prompt, core_msgs, sink=..., cancel_event=...))
    │     ├─ agent.call() runs as a task; a 5s poll watches the contrib tracker states
    │     │    (activity / progress / live-messages — installed by build_runtime)
    │     ├─ silent > API_STALL_TIMEOUT_SECONDS and phase != "tool_call"
    │     │    → cancel task, sanitize live history, restart with resume prompt (max 3×)
    │     │    repetition verdict (checked FIRST) → cancel task, sanitize live history,
    │     │    restart with the [SYSTEM ALARM] text AS the user message (own 3× budget)
    │     ├─ cancel_event set → cancel task promptly, raise LoopCancelledError
    │     ├─ ON_ITERATION hooks → summarize at 80% (counter resets); emit progress LoopEvent
   │     ├─ LLM call → provider → response
   │     ├─ AFTER_LLM_CALL hook → on_response(content, has_tool_calls) → LoopEvent
   │     ├─ ON_THINKING hook → on_thinking(text) → LoopEvent(kind="thinking")
    │     ├─ call_subagent start → observer → LoopEvent(subagent_start, subagent_id, subagent_name)
    │     ├─ background job finished (this or an earlier turn) → BEFORE_LLM_CALL alarm hook
    │     │    appends the alarm as a user message → LoopEvent(alarm, actor="outo")
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

- **`LoopEvent`** — dataclass with `actor: str` (`"outo"` / `"{name}-{6hex}"` — `"subagent-<6hex>"` for the legacy `"subagent"` name — / `"provider"` / `"watchdog"`), `kind: str`, `text: str`, optional `tool_name`, optional `subagent_id` (the 6-hex invocation id, set on every event emitted inside a subagent), optional `subagent_name` (the persona name — `"editor"`, `"reviewer"`, … — set alongside `subagent_id`), optional `detail` (extra payload not shown by the CLI — currently the full untruncated Bash command on `kind="tool"` events). optional `msg_ref` (`str(id(message))` of the live coreouto `Message` the event came from — the prune key for invariant 18). Kinds: `"tool"` / `"tool_result"` / `"response"` / `"thinking"` / `"context"` / `"error"` / `"wakeup"` / `"alarm"` / `"subagent_start"` / `"subagent_end"`. `to_dict()`/`from_dict()` provide the sparse JSON form stored in session turns (optional fields are only serialized when truthy, so older session files load unchanged).
- **`EventSink`** — `Protocol`: `begin_working()`, `update_activity(text)`, `end_working()`, `emit_loop_event(event)`, `emit_final_answer(content, session_name)`, `emit_stream_delta(kind, text)`, `remove_events(msg_refs)`. `emit_stream_delta` is **transient and display-only** (`kind` is `"text"` or `"thinking"` — the `StreamKind` literal): deltas are incremental fragments of a message the model is still generating, never records. All four implementers must define it — hooks are global and there is no `__getattr__` delegation, so a missing method raises `AttributeError` on the first delta. `NullSink` and `ConsoleEventSink` are no-ops (the CLI renders whole messages only, keeping its output byte-identical), `_RecordingSink` forwards without recording, `TUIEventSink` coalesces into a live row. `remove_events(msg_refs)` is the display-prune entry point: it drops every recorded event carrying one of those `msg_ref`s, forwards to the wrapped sink (the TUI removes matching rows, the console prints a dim `[removed N corrupted messages from the record]` note), and is called **only** by the two removal sources (history repair + `sanitize_history`) — never by the summarizer.
- **`NullSink`** — no-op implementation (used when `run_chat` is called without a sink).
- **`ConsoleEventSink`** — CLI implementation. Renders loop events as `{actor}: {text}` in `orange3`; `kind="thinking"` renders the **full** reasoning text as `{actor}:thinking: {text}` (dim); `subagent_start`/`subagent_end` render as a single-line preview (whitespace-flattened, 120 chars; `subagent_end` dim); `kind="tool_result"` is **ignored** (early return — the events exist for the TUI; CLI output stays byte-identical to the pre-tool_result behavior); `kind="alarm"` falls into the default branch and prints the whole alarm text as `outo: [SYSTEM ALARM - THIS IS NOT A USER MESSAGE] …`, since the user needs the result-file path in the transcript; runs a `rich.status` spinner updated by `update_activity`; writes the final answer as plain stdout followed by a `------finish------` marker.

---

## `core/chat.py`

### Module-level state

- `_tool_trace: list[dict]` + `_tool_trace_lock: threading.Lock` — last 5 tool calls observed this turn, used by failure diagnostics. Locked because subagent handlers may run concurrently.
- `_LOGGABLE_TOOL_NAMES = ("Bash", "Image", "Video", "Audio", "call_subagent")` — tools whose arguments get summarized in failure output.
- `pending_responses` / `pending_thinking` / `turn_refs` — per-actor buffers created in `_run_chat_prepared` (see the dispatchers below). `turn_refs` is the per-actor `str(id(assistant_message))` published by `ON_ITERATION` and read by the two tool dispatchers, which is why a call row and its result row always share one `msg_ref`.
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
    subagent_provider: str | None = None
    subagent_model: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    reasoning: str | None = None
    continue_session: bool = False
    cancel_event: threading.Event | None = None
```

Mirrors the CLI flags for `miniouto chat`. The CLI is responsible for translating `--name` → `session`, `--continue` → `continue_session`, etc.

### `ToolCallArgsError(Exception)`

Raised when an LLM tool call arrives with non-dict or `None` arguments. This prevents the cryptic `TypeError: 'NoneType' object is not iterable` that would fire when coreouto calls `handler(**None)`.

### `run_chat(opts: ChatOptions, sink: EventSink | None = None) -> str`

The main entry point. `sink=None` defaults to `NullSink()`; either way the sink is wrapped in a `_RecordingSink` that captures every `LoopEvent` (for the session's turn record) before delegating. Steps:

1. Resolves a `RuntimeConfig` from settings + overrides.
2. Computes `provider_config` — `max_tokens` defaults to `get_max_output_tokens(runtime.model, runtime.provider_name)` so providers with low hard caps (Anthropic's 1024) don't truncate long tool calls (e.g. heredoc file writes). The `provider_name` argument scopes the lma lookup so we don't accept the first cross-provider match.
3. Builds the sink dispatchers (`_make_tool_call_dispatcher`, `_make_tool_result_dispatcher`, `_make_response_dispatcher`, `_make_thinking_dispatcher`, `_make_stream_dispatcher` ×2, `_make_iteration_dispatcher`, `_make_provider_error_dispatcher`, `_make_alarm_dispatcher`) plus the `on_iteration` / `on_history_repair` / `on_messages_removed` persistence bridges (all three depth-gated to outo — a subagent's transcript is not the session's record), and calls `build_runtime(...)` to get a `co.Agent`.
4. Loads prior history if `continue_session=True` via `_load_coreouto_history` — session `history` dicts are validated back into `co.Message` objects; invalid entries degrade to plain text (media block lists are flattened via `_flatten_media_blocks`) but **keep `tool_call_id`/`name` for tool messages** so tool_use/tool_result pairing survives even unknown schema drift (without the id the provider 400s the whole request on resume).
5. Installs `_make_subagent_dispatcher(sink)` as the subagent observer (`set_subagent_observer`), cleared in a `finally` after the call.
6. Clears `_tool_trace`, runs the turn as `asyncio.run(_run_with_watchdog(agent, opts.prompt, core_msgs, sink=sink, cancel_event=opts.cancel_event, on_removed=sink.remove_events))` inside try/except — see the watchdog subsection below. The whole prepared run is wrapped by `run_chat` in a `try/except` that calls `providers.clear_coreouto_state()` before re-raising: hooks are global and close over this turn's sink and persister, so a turn dying during resolve/build (a provider deleted mid-session, a malformed style) must not leave the previous turn's hooks registered against the new state.
7. On `LoopCancelledError` (the `cancel_event` was set), finishes the turn as `status="interrupted"` and re-raises WITHOUT `_dump_failure_diagnostics` — a user force-stop is a clean cancel, not a failure. On `LoopRepeatedError` (its own branch — a repeat is not a cancel) it records `repetition-stop: …` and follows the same interrupted/failure path. On any other exception, appends a record-only `kind="error"` LoopEvent (so a session reload shows why the turn ended), finishes the turn as `"interrupted"`, calls `_dump_failure_diagnostics`, re-raises.
8. `_finish_turn`: on success rewrites the session `history` with `[_dump_message(m) for m in response.messages if m.role != "system"]` (media blocks flattened to text placeholders, tool pairing fields always preserved) and stamps the running `TurnRecord` done with `assistant` + recorded events; on cancel/error the incrementally persisted on-disk history is kept (it already reflects mid-turn progress). Persistence failures are swallowed — they must never mask the turn's outcome.
9. Calls `sink.emit_final_answer(response.content)`; returns `response.content`.

### Incremental persistence (`_TurnPersister`)

The session is written **throughout** the turn, not just at the end, so a force-killed process (SIGKILL, terminal close, hard crash) leaves a fully reloadable record instead of rolling back to the previous turn:

- `begin_turn` appends the turn as `status="running"` before the loop starts (and reaps stale running turns from dead writers).
- `_RecordingSink.on_event` → `update_turn_events` on nearly every loop event; `thinking`/`context` events are throttled to one write per 0.5 s, structural events flush immediately.
- The `ON_ITERATION` dispatcher additionally calls `persist_history`, which runs `sanitize_history` (drops the trailing assistant message whose tool calls have no results yet — ON_ITERATION fires before tool execution) and stores it via `update_history`. Guarded by `current_subagent_depth() == 0` — hooks are global, and a subagent's transcript is not the session's restorable history.
- Combined with the atomic `sessions.save` (tmp file + `os.replace`), the worst-case loss on SIGKILL is a sub-second thinking line.

### `_run_with_watchdog(agent, prompt, history, *, sink, cancel_event, states=None) -> co.Response`

Thin level-0 wrapper around `core/runtime.py:supervised_run` (see below): runs outo's `agent.call()` supervised, emitting `watchdog:` loop events (`kind="wakeup"`) on each restart, and wires the user's `cancel_event` into the fast path (ESC ESC hard-cancels even an in-flight LLM call within one 5 s poll — the hook-boundary cancel guard alone cannot interrupt one).

### `supervised_run(start, prompt, history, *, live_key, level, on_wakeup, cancel_event=None, states=None, restart_on_repetition=None, on_repetition=None, on_removed=None)` (`core/runtime.py`)

The stall watchdog, following coreouto `examples/27_wakeup.py` + `examples/28_resume_interrupted.py` (coreouto >= 0.11.1). `start(prompt, history)` creates one attempt's coroutine; `start(None, history)` must resume from a transcript alone. **Only outo's turn is supervised today** (`_run_with_watchdog` passes `level=0`): a background subagent job runs on the daemon loop with no supervisor, because the watchdog state it would read is per-turn and every `build_runtime` clears it. A 5-second poll (`WATCHDOG_CHECK_SECONDS`) reads the contrib tracker states installed by `build_runtime` (`activity_tracker_hook` + `loop_progress_hook` + a BEFORE_LLM_CALL live-messages capture keyed by actor — see `_register_watchdog_trackers`):

- **Stall wakeup** — when `activity.seconds_since_last_activity() > API_STALL_TIMEOUT_SECONDS` (30 min) AND `progress.phase != "tool_call"` AND no deeper supervisor is active (`_ACTIVE_SUPERVISORS`), the attempt task is cancelled, the actor's live message list is recovered via `sanitize_history` (drops system messages and any trailing assistant turn with unanswered tool calls — providers 400 on dangling tool calls), `on_wakeup` fires, and the call restarts **without a user message**: coreouto >= 0.11.1's `call(history=...)` sends the sanitized transcript exactly as declared and simply continues. If nothing is recoverable (wedged before the first LLM call), it falls back to the original prompt. Max `WATCHDOG_MAX_WAKEUPS = 3` restarts, then `LoopStalledError`.
- **Supervisor deferral** — a supervisor stays quiet while any deeper level is active (`_ACTIVE_SUPERVISORS`): the deeper call's supervisor owns the silence and does the cancelling/restart in place. Harmless with one level today, and the reason outo's level is 0.
- **Repetition restart** (`restart_on_repetition` = the actor key, `"outo"`) — checked **before** the stall test, because a looping model is *active*, not wedged, so the stall path would never fire for it. On a sticky verdict the attempt is cancelled, the live history sanitized, `on_repetition` fires, and the call restarts with the **alarm as the user message**: `start(alarm, history)`, or `start(f"{prompt}\n\n{alarm}", history)` when no transcript is recoverable (otherwise the task itself would be lost). The alarm is deliberately **not** pushed through the subagent alarm deque — that deque is drained into the live list by `_make_alarm_hook` and would deliver the same text a second time. Budget: `REPEAT_MAX_RESTARTS = 3`, independent of `WATCHDOG_MAX_WAKEUPS`, then `LoopRepeatedError`. The deeper-supervisor deferral does not apply to it — it is per-actor and only this supervisor consumes its own actor's verdict.
- `on_removed` is the display side of **both** restart paths: every `sanitize_history` truncation reports the discarded messages' refs there, so the record loses the events of a turn the resumed attempt no longer has. A stall keeps the answered prefix; a repetition restart keeps it plus the alarm. A discarded tail is a discard either way.
- **Phase exemption** — while `phase == "tool_call"` the watchdog stays quiet: tools carry their own bounds (Bash: `BASH_TIMEOUT_SECONDS`). A healthy nested agent keeps the global activity clock fresh through its own hook firings, so only a genuinely silent tool-free loop wakes a supervisor.

The SDK timeout (`API_STALL_TIMEOUT_SECONDS` on every provider constructor) + `TIMEOUT_ERRORS` rules catch hung HTTP requests; supervisors catch everything else (wedged between operations, deadlocked hook, ...). The resumed call's `Response.messages` is the sanitized transcript plus the new turns — no injected user message, so the persisted session history reads as one uninterrupted conversation.

### `LoopStalledError` / `LoopRepeatedError` (`core/runtime.py`)

`LoopStalledError` — raised when a supervisor exhausts its wakeups: the loop re-wedges within the stall limit every time, so something is deterministically wrong (e.g. a deadlocked hook). Surfaces through the normal failure-diagnostics path.

`LoopRepeatedError` — raised when the repetition budget is exhausted: the model kept repeating itself through every restart. A deliberate **sibling of `LoopStalledError`, not of `LoopCancelledError`** — a repeat is the model's own failure, never a user stop, so the cancellation branch must not swallow it. `core/chat.py:run_chat` has its own `except LoopRepeatedError` branch that records a `repetition-stop: …` error event, stamps the turn interrupted, and re-raises.

### `LoopCancelledError(Exception)` (`core/runtime.py`)

Raised by the cancel guard from a hook once the user's `cancel_event` is set. coreouto's hook `trigger` does not swallow exceptions, so it propagates out of the call and terminates the loop at the next hook boundary.

### `_actor_label() -> (str, str | None)`

Shared by all dispatchers: returns `(f"{name}-{6hex}", sid)` inside a background subagent job where `name` is `_SUBAGENT_NAME` (so `"subagent-<6hex>"` for the legacy name, `"editor-abc123"` for an `editor` job), `("subagent", None)` as a defensive depth-only fallback, `("outo", None)` otherwise. The ContextVars it reads are set inside the job's own asyncio task by `subagents._enter_context`.

### `_make_tool_call_dispatcher(sink: EventSink)`

Builds the `on_tool_call(name, arguments)` closure passed to `build_runtime`. For each tool call:

1. Validates `arguments` is a dict via `_validate_tool_call_args` (raises `ToolCallArgsError` otherwise).
2. Computes actor + subagent id via `_actor_label()`.
3. Feeds the repetition detector (`note_tool_call` + `cancel_background_loop`) **before any skip branch** — a model re-spawning the same brief forever must trip, and the subagent's internal calls carry the job's own sid. Appends to `_tool_trace`.
4. For `call_subagent`, **emits nothing** — the subagent observer emits the `subagent_start` event with the minted id and persona name immediately after (the `BEFORE_TOOL_CALL` hook for `call_subagent` still runs in the *parent* context, so the id does not exist yet here).
5. For `Bash/Image/Video/Audio`, emits a `LoopEvent(kind="tool", text=f"{name} {preview}", subagent_id=sid)` and updates the spinner activity (the subagent label when nested, else the tool name). For `Bash` the event also carries `detail=arguments["command"]` — the **full untruncated command, verbatim** (no newline flattening) — for sinks that want the raw input; `detail` is `None` for the media tools.

Carries the per-actor `msg_ref` from `refs` (published by the iteration dispatcher), so the row and its result are pruned together.

### `_make_tool_result_dispatcher(sink: EventSink)`

Builds the `on_tool_result(name, result)` closure wired into coreouto's `AFTER_TOOL_CALL` hook (via `_make_tool_result_logger`), where `result` is coreouto's `ToolResult` (`.content`, `.blocks`, `.is_error`, `.flatten_text()`). For each tool result:

1. Skips `call_subagent` entirely — the subagent observer's `subagent_end` event already carries that result.
2. Only emits for `Bash/Image/Video/Audio`.
3. `text = result.flatten_text()`, prefixed with `"error: "` when `result.is_error` (mirrors the `subagent_end` "error:" convention the TUI keys on), truncated to 4000 chars with a `"\n… [truncated]"` suffix when over.
4. Emits `LoopEvent(kind="tool_result", text=text, tool_name=name, subagent_id=sid, msg_ref=refs.get(sid or ""))` with the current actor/subagent id via `_actor_label()` — the **same** `msg_ref` the `tool` event carries, which is what keeps a removed message from orphaning its result row. The CLI's `ConsoleEventSink` ignores this kind; it exists so the TUI can display tool return values.

The whole body is wrapped in `try/except Exception` — a sink failure here must never break the agent loop.

### `_make_subagent_dispatcher(sink: EventSink)`

Builds the `(phase, sid, name, text)` lifecycle callback installed via `set_subagent_observer` (note the argument order: **sid before name**). Only `"start"` fires in v1: text is the brief preview (160 chars) and it emits `LoopEvent(kind="subagent_start", actor=f"{name}-{sid}", subagent_id=sid, subagent_name=name)`, switching the spinner activity to the subagent label. Completion is **not** reported here — it arrives as the `alarm` loop event from `_make_alarm_dispatcher`, so a job finishing mid-turn and one finishing between turns look identical. The dispatcher also still handles the `"wakeup"` / `"end"` phases defensively, but the background engine never emits them.

### `_make_thinking_dispatcher(sink: EventSink)`

Builds the `on_thinking(thinking)` closure wired into coreouto's `ON_THINKING` hook. The text is **stashed** per actor and flushed by `_make_iteration_dispatcher` — coreouto fires `ON_THINKING` *before* it appends the assistant message, so there is no message to stamp a `msg_ref` with at that point. Repetition counting still runs here, at the earliest possible moment; only the display is deferred. Reasoning from a background job is labeled `{name}-{6hex}` automatically.

### `_make_response_dispatcher(sink: EventSink, pending: dict)`

Builds the `on_response(content, has_tool_calls)` closure. When `has_tool_calls` is true (intermediate LLM response that triggers a tool call), the text is **stashed in `pending`** (keyed by actor: `""` for outo, the subagent id inside a subagent) instead of being emitted immediately — coreouto fires `AFTER_LLM_CALL` *before* `ON_THINKING`, so an immediate emit would place the model's text above its own thinking row. The stash is flushed by `_make_iteration_dispatcher` (below), which `ON_ITERATION` guarantees runs after `ON_THINKING` — every sink therefore sees `thinking` → `response` in that order. The stashed tuple carries the actor/subagent id captured at stash time, so parallel subagent streams stay self-consistent.

### `_make_stream_dispatcher(sink: EventSink, kind: StreamKind)`

Builds the `on_stream_delta(delta)` closure wired into coreouto's `ON_STREAM_TEXT` / `ON_STREAM_THINKING` hooks, which fire **once per incremental fragment** (not the accumulated text) whenever the provider streams. It only calls `sink.emit_stream_delta(kind, delta)` — a display-only path. Two deliberate omissions:

- **No repetition feed.** Deltas fragment mid-segment, so the phrase detector would count half-phrases; combined with the whole-message `note_text` calls in the thinking/response paths, every message would be counted twice and the 10x threshold would trip at 5 real repeats. The repetition watchdog therefore stays on whole-message boundaries, which cover the same content.
- **No subagent deltas.** A subagent stream would need per-actor rows, and a *background* job has no hooks at all once the turn that started it has ended (it keeps streaming against its own agent loop), so v1 renders outo's own text only. Deltas arriving at `current_subagent_depth() > 0` are dropped.

### `_make_alarm_dispatcher(sink: EventSink)`

Builds the `on_alarms(alarms)` callback that `runtime._make_alarm_hook` forwards drained completion alarms to. The hook has **already** put the alarm text into the model's live message list; this only records it: one `LoopEvent(actor="outo", kind="alarm", text=alarm.text, subagent_id=alarm.sid, subagent_name=alarm.name)` per alarm. The actor is `outo` on purpose — the alarm is outo's own next input, not the subagent speaking — and the TUI matches `subagent_id` against its running subagent rows to close them out.

### `_make_iteration_dispatcher(sink: EventSink, pending, pending_thinking, refs)`

Builds the `on_iteration(*, iteration, messages, response, **kwargs)` closure. `ON_ITERATION` is where the turn's message identity becomes available: coreouto appends the assistant message and only then fires this hook, so `messages[-1]` IS the message every event of this iteration came from. Its `str(id(...))` is published into the per-actor `refs` slot and stamped as `msg_ref` onto everything flushed here — the stashed thinking and response text, and the progress line. The tool dispatchers read the same slot, which is why a `tool` event and its `tool_result` always share one ref and are pruned together. Flush order per iteration is thinking → response → context (net display order); the summarizer lives separately in `make_summarize_hook`, registered as a second `ON_ITERATION` hook. This dispatcher also feeds the phrase detector (`_note_repetition_text`) on the whole response text.

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
| (other) | `str(args)[:120]` |

---

## `core/context.py`

### Constants

- `SUMMARIZE_THRESHOLD = 0.8` — summarization fires at 80% of context window.
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

### `make_summarize_hook(model, session_name, provider_name=None) -> tuple[Callable, Callable]`

Returns `(on_iteration, before_llm_call)` — a hook pair (registered as a second `ON_ITERATION` hook alongside `_make_iteration_logger`, plus a `BEFORE_LLM_CALL` hook). The `session_name` argument is captured but currently unused in the body. `provider_name` is threaded into `get_context_window` for an accurate lookup **and** used as the summarizer agent's provider (guaranteed registered by `build_runtime`).

- If no context window is known, returns two no-op hooks.
- The compaction is a **context handoff**, not a prose summary — the agent continues with ONLY the compacted message, so it must carry everything still relevant.

  **`on_iteration(*, iteration, messages, response, **_kwargs)`** — Accumulates `response.usage.total_tokens`. When the cumulative total reaches 80% of the window it only **arms a pending flag**; it never compacts here. coreouto fires `ON_ITERATION` *before* executing the response's tool calls, so compacting at that point would wipe the just-appended assistant tool_use and orphan the tool results that follow (provider 400: "tool result's tool id not found").

  **`before_llm_call(*, messages, **_kwargs)`** — When armed, runs the summarizer and replaces the message list. `BEFORE_LLM_CALL` of the next iteration is the safe point: every tool-call pair in the list is complete. Registered before the watchdog's `capture_messages` hook so the watchdog captures the post-compaction list.

  **`summarizer(messages) -> list[co.Message]`** — Builds the handoff by:
  1. Splitting out system messages.
  2. Detecting and carrying forward any previous `"[Summary…"` user message ("Previous compaction" — facts survive repeated compactions).
  3. Flattening user/assistant/tool messages into text **including the assistant's tool CALLS** (`Agent tool call: Bash({...})` — without them the results are unattributable) with tool results truncated at 4000 chars.
  4. Extracting the current task **verbatim in code** (never LLM-paraphrased).
  5. Awaiting a separate **async** `co.Agent(name="summarizer", provider=provider_name, max_iterations=1, provider_config={"max_tokens": get_max_output_tokens(model, provider_name)})` call with a 7-section handoff prompt (TASK / DONE / IN PROGRESS / FILES / DECISIONS / ERRORS / NEXT; paths and commands verbatim). The output cap resolves through the normal precedence (provider TUI override > lma `max_output_tokens` > lma `context_window` > 16K floor), fresh at each compaction. The call must be async — the hook fires inside coreouto's running event loop, where a sync `call_sync` raises "cannot be called from a running event loop"; with the old sync hook the LLM handoff silently never worked.
  6. On failure, prints the exception to stderr (visible, not silent) and falls back to a deterministic verbatim tail of the recent conversation — never a content-free apology.
  7. Wrapping the result in a **minimal positive frame** — "the earlier conversation was compacted into the handoff below; read it carefully, then continue the task naturally" — with no prohibitions: naming forbidden recovery behavior (sessions, `.miniouto`, transcripts) plants the very idea, and a handoff that carries everything leaves nothing to recover. The frame embeds the verbatim task. **Always** returns `[*system_msgs, summary_msg]` — never overwrites with garbage.

  **Critical guard:** if the summarizer returns a non-list, prints a yellow warning to stderr and keeps the original messages (this is the divergence from `coreouto.contrib.hooks.auto_summarize_hook`, which would `clear()` and `extend()` with the non-iterable and both corrupt the turn *and* raise `TypeError`). **The counter resets to 0 after each compaction** (coreouto `examples/23` pattern) — without the reset, every later iteration that reports usage re-triggers the LLM summarizer.

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

Suppresses any exception from `co.clear_tools()` (used at start of new runs to remove a stale `call_subagent` from a previous process — relevant when the active style changed between turns and a no-longer-needed `call_subagent` is still registered).

### `provider_kwargs(provider_config, passthrough)`

Helper that bundles the two dicts under the keys coreouto's `AgentConfig` expects. Defined but currently unused in the main code paths; `build_runtime` does its own merging.

---

## `core/subagents.py` — background job registry

A job must outlive the turn that spawned it, and `run_chat` runs every turn under `asyncio.run(...)`, which tears down anything still detached at turn end. So the engine is a module-level registry over a **daemon thread that owns its own `asyncio.new_event_loop()`** and never stops (`_ensure_loop` / `_run_loop`).

| Symbol | Purpose |
|---|---|
| `ALARM_HEADER = "[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]"` | First line of every completion alarm; single source of truth shared with `repetition.alarm_text` and the TUI |
| `SPAWN_HANDSHAKE_TIMEOUT_SECONDS = 2.0` | How long `submit` waits for the job to prove it entered `agent.call` |
| `BRIEF_PREVIEW_CHARS = 160` | Truncation of the brief in the `start` observer event |
| `submit(agent, name, brief) -> Job` | Mints `sid = secrets.token_hex(3)`, registers the job, emits the `start` event, schedules `_run_job` on the daemon loop, waits out the handshake, returns the `Job` |
| `take_pending_alarms() -> list[Alarm]` | Drain the completion deque, oldest first |
| `running_jobs() -> list[(sid, name)]` / `is_running(sid) -> bool` | Liveness, for the ack text and for `repetition.cancel_background_loop` |
| `cancel_job(sid, reason) -> bool` | Cancel a running job; its completion path still writes the file and queues the alarm, so the parent learns about the stop |
| `reset()` | Test hook: bump `_GENERATION`, drop registry state, cancel futures — a completion landing afterwards writes no file and queues no alarm |
| `format_completion_alarm(job) -> str` | The exact alarm text: header + "Background subagent '<name>' (id) finished (status)" + result-file path + the still-running list |
| `Job` | Mutable record: `sid`, `name`, `brief`, `started`, `generation`, `status` (`running`/`done`/`error`/`stopped`), `result_path`, `stop_reason`, `spawned` (the handshake Event), `future` |
| `Alarm` | Frozen `(text, sid, name)` — the text is the verbatim contract, the ids are for attribution without re-parsing |

**Spawn handshake.** coreouto's `Agent.call()` resolves the provider and its tools **by NAME** from the global registries synchronously at `call()` entry, and the next turn's `build_runtime` wipes those registries. `_run_job` therefore sets `job.spawned` as its very first statement, before touching `agent.call`; `submit` waits on it. A turn cannot reach its next `build_runtime` before the tool handler that called `submit` returns, so the handshake proves `call()` was entered while the submitting turn's registries were intact. Resolution happens once, at entry — that is what makes the job safe to keep running.

**Completion** (`_finish`, idempotent per job): writes `~/.miniouto/subagent-results/<sid>-<YYYY-MM-DD-HH-MM>.md` (meta header: id, name, status, started, finished; then `## Brief` and `## Result`) and enqueues **exactly one** `Alarm`. `cancel_job` and an exception route through the same path, so a stopped or failed job is reported identically.

**ContextVars**: `_enter_context` / `_exit_context` set runtime's `_SUBAGENT_DEPTH` / `_SUBAGENT_ID` / `_SUBAGENT_NAME` at the top of the job's own asyncio task, which is what gives concurrent jobs distinguishable actor labels.

**v1 limitations** (deliberate, documented): no per-job stall supervisor (the watchdog state is per-turn and every `build_runtime` clears it) and no job-internal event streaming (hooks are resolved live per event, so a job keeps streaming against an agent whose hooks the next turn replaced). A wedged job therefore stays in `running_jobs()` until `cancel_job` ends it. The result file and the completion alarm are the only channels.

## `core/repetition.py` — repetition watchdog

A model that has stopped making progress rarely goes quiet, so the stall watchdog never fires for it. This module catches exactly two shapes, counting **cumulatively** (not consecutively) and **exactly** (identical normalized segments only — no edit distance, no model call), so a verdict is deterministic and cheap.

| Symbol | Purpose |
|---|---|
| `REPEAT_PHRASE_THRESHOLD = 10` | The same normalized segment seen this many times in thinking or content |
| `REPEAT_TOOL_THRESHOLD = 30` | The same tool call (name + `json.dumps(args, sort_keys=True)`) this many times |
| `REPEAT_MAX_RESTARTS = 3` | A supervised turn's own restart budget, independent of `WATCHDOG_MAX_WAKEUPS` |
| `MIN_SEGMENT_CHARS = 4` | Shorter segments ("ok.", "yes!") are junk and not counted |
| `note_text(actor_key, text)` / `note_tool_call(actor_key, name, arguments)` | Count one message / one tool call toward that actor's detectors |
| `check` / `consume_verdict(actor_key)` | Peek at a sticky verdict / take it and clear the counters, so a resumed attempt must earn a new one |
| `reset()` / `reset_actor(actor_key)` | Drop every / one actor's counters; `reset()` runs once per turn from `runtime._register_watchdog_trackers` |
| `cancel_background_loop(actor_key)` | Enforcement for unsupervised jobs: cancel the job instead of restarting it; a supervised actor is deliberately left alone so its sticky verdict survives |
| `alarm_text(verdict)` | The exact resumed user message: the phrase shape tells the model to proceed with a *different pattern*; the tool shape adds *try a different method* if repeating is not producing progress |

Segments come from splitting on newlines, sentence enders (`.?!。？！`), then collapsing whitespace and casefolding. The actor key is `current_subagent_id() or "outo"`, and one lock covers every actor because hooks fire on both the turn's loop and the daemon loop. Counters are fed from the whole-message paths only — the thinking dispatcher, the iteration dispatcher's response flush, and every tool call (`call_subagent` included, counted before the skip branches). Stream deltas are deliberately **not** fed (see invariant 8).

---

## `core/runtime.py`

### Module-level constants / state

- `BASE_TOOLS = ["Bash", "Image", "Video", "Audio"]` — the shared tool set used for both presets. `call_subagent` is **not** part of the shared list — it is registered conditionally on every `build_runtime` based on whether the active style declares ≥1 named subagent.
- `OUTO_ONLY_TOOLS = ["Computer"]` — appended for the outo preset only (the virtual screen is a single shared resource).
- `_SUBAGENT_DEPTH: ContextVar[int]` — defaults to 0. Read by `chat.py` dispatchers (via `current_subagent_depth()`) as a defensive fallback label, and as the outo/alarm gate. Mutated only by `subagents._enter_context` / `_exit_context`, inside the background job's asyncio task.
- `_SUBAGENT_NAME: ContextVar[str | None]` — defaults to None. The persona name of the running background job (e.g. `"editor"`, `"reviewer"`, `"subagent"`); read by `chat._actor_label()` to build `{name}-{6hex}` labels. ContextVars are copied per asyncio task, so concurrent jobs each see their own name + id.
- `_SUBAGENT_ID: ContextVar[str | None]` — defaults to None. The 6-hex id of the running background job; read by `chat._actor_label()` to build the id half of the label, and used as the repetition **actor key** (`sid or "outo"`).
- `_SUBAGENT_OBSERVER` — module-level `(phase, sid, name, text) -> None` slot set per turn by `chat.run_chat` via `set_subagent_observer()`. Note the order: **sid before name**. Because coreouto's hooks are global, this slot is the bridge between the background job engine and the active turn's sink. Observer exceptions are suppressed (`contextlib.suppress`) so a broken sink can never break a job. Only the `"start"` phase is ever emitted (invariant 6).

### `current_subagent_depth() -> int` / `current_subagent_name() -> str | None` / `current_subagent_id() -> str | None`

ContextVar getters read by `chat.py`.

### `set_subagent_observer(observer | None)`

Installs/clears the lifecycle observer.

### `_wrap_subagent_handler(agents, specs)`

Returns the async handler registered as `call_subagent`. Fire-and-forget:

1. `task` must be a non-empty string, else `ValueError("call_subagent requires `task`: one self-contained brief. To run several subagents at once, issue several calls.")`.
2. `_resolve(requested)` picks the persona: an explicit `name` must match a declared spec or it fails fast (before any job starts) with the available names; `""` resolves to a spec literally named `"subagent"` (legacy compat), else the sole declared spec, else `ValueError`.
3. `subagents.submit(agents[resolved], resolved, task)` mints the 6-hex sid, schedules the job on the daemon loop, waits out the spawn handshake, and returns a `Job`.
4. Returns the **acknowledgement** string only: the job id, an explicit "this text is NOT the result", the alarm header the model will later receive, the `SUBAGENT_RESULTS_DIR` the alarm will name a file in, and "do not wait or poll; continue with other work".

There is **one brief per call** and no fan-out parameter. Several calls in one turn start several concurrent jobs, which is how parallelism is expressed now. An unknown persona name fails fast; there is no combined numbered result and no per-brief error section.

### `_make_alarm_hook(on_alarms=None)`

The `BEFORE_LLM_CALL` hook that delivers background-subagent completion alarms **mid-turn**. When no turn is active the delivery path is different and lives outside this hook: the TUI drains the queue and starts a turn whose prompt IS the alarm text (`ChatTUI._deliver_pending_alarms`), and `miniouto chat` chains follow-up turns (`cli/chat.py:run_turn_chain`) — see AGENTS.md invariant 16. Registered **before** the watchdog's `capture_messages` hook, so the supervisor's live message list already carries the delivered alarms.

- Gated to outo depth (`current_subagent_depth() == 0`) — a subagent's own loop must never consume outo's alarms.
- Drains `subagents.take_pending_alarms()`, joins the texts with a blank line, and appends **one** `co.Message(role="user")` to the live `messages` list, so the very next `provider.create` already carries it.
- **Appends directly, never `Agent.inject_user_message`**: that queue is drained at the TOP of the loop iteration (coreouto `agent.py:301-307`), i.e. *before* this hook, so an alarm queued from here would land a full LLM round-trip later.
- **Fold rule**: if the last message is already a user message the text is folded into it — providers reject two consecutive user messages, the same hazard `_cut_poisoned_turn` works around.
- Each drained alarm is forwarded to `on_alarms` (from `core.chat._make_alarm_dispatcher`) under `contextlib.suppress`, so a broken sink can't break the provider path.

### `_subagent_description(specs)`

The model-facing contract string for `call_subagent`. States that the call does **not** block, that the return value is only an acknowledgement (never the answer), that completion arrives as a user message starting with `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]` naming a result file under `~/.miniouto/subagent-results/`, that polling or re-asking is forbidden, that several calls run concurrently, and what `name`/`task` mean. Lists the available `name=` values when more than one subagent is declared.

### `_build_subagent_tool(*, specs, provider_config, provider_passthrough)`

Reimplements `coreouto.contrib.agent_as_tool` for one specific reason: the stock helper drops `provider_config` when calling `preset.to_config()`. This version:

1. Takes the shared `"subagent"` preset's config, merges `provider_config` (contains at least `max_tokens`) AND `provider_passthrough` (the resolved reasoning kwargs) into it, then deep-copies it per `SubagentSpec` with that spec's `prompt` as `system_prompt` and builds one `co.Agent` per spec. Merging both is load-bearing — without `max_tokens` a subagent file-writing call inherits the provider's low hard cap (Anthropic → 1024 → silent truncation), and without `provider_passthrough` the reasoning keys would land on `provider_config` and be forwarded raw to a user-chosen provider name, which `normalize_provider_config` does not translate.
2. Returns `_wrap_subagent_handler(agents, specs)`, registered via `co.register_tool("call_subagent", description=_subagent_description(specs))(handler)`. coreouto derives the model-facing JSON schema from the handler's type hints, so the model sees exactly `name` and `task`.
3. Registration is conditional: `build_runtime` calls this only when `specs` is non-empty. An outo-only style produces no `call_subagent` at all (the model simply has no way to delegate).

### `RuntimeConfig`

```python
@dataclass
class RuntimeConfig:
    provider_name: str
    model: str
    style_name: str = "default"
    subagent_model: str | None = None      # falls back to `model`
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
    subagent_provider: str | None = None
    subagent_model: str | None = None
```

Per-call overrides from CLI flags. All optional.

### `build_runtime(runtime, *, style_overrides=None, provider_config=None, on_tool_call=None, on_tool_result=None, on_response=None, on_thinking=None, on_stream_text=None, on_stream_thinking=None, on_iteration=None, on_provider_error=None, on_history_repair=None, on_alarms=None, reasoning=None, cancel_event=None) -> co.Agent`

The heart of miniouto. Steps:

1. **`clear_coreouto_state()`** — Reset all four coreouto registries.
   - **`cancel_event` guard** — If a `threading.Event` is passed, a `_make_cancel_guard` hook is registered FIRST on both `BEFORE_LLM_CALL` and `BEFORE_TOOL_CALL`: once the event is set the hook raises `LoopCancelledError`, which coreouto's `trigger` does not swallow, so it propagates out of the turn. This is cooperative cancellation — the loop stops before the next step; an in-flight tool execution is not interrupted by the guard (Bash polls the same event and kills its own process). Independently, the watchdog's poll (`core/chat.py:_run_with_watchdog`) checks the same event every 5 s and hard-cancels the task, so even an in-flight LLM call is interrupted promptly. Inside a `call_subagent` handler the raise is caught by coreouto's tool-call wrapper and surfaces as an error `ToolResult` (killing the subagent immediately); the outo loop then stops at its next `BEFORE_LLM_CALL`.
2. **Provider registration** — Look up `runtime.provider_name` in the provider store; raise `RuntimeError` if missing. Call `build_coreouto_provider` for it. Repeat for the subagent provider (which may differ).
3. **`tools.registry.register_all()`** — Registers `Bash/Image/Video/Audio` tools in coreouto.
4. **`parse_style` + `_load_active_skills`** — Loads the named style document (or `builtin_default` for `"default"`), parses it via `parse_style` into `(outo_body, list[SubagentSpec])`, and prepends the lazy-load skill catalog to the outo body. Each subagent's body is paired with its own per-call skill catalog + cwd preamble (built lazily, not persisted).
5. **`_with_cwd(role, body)`** — Prepends an absolute-cwd preamble (using `INVOCATION_CWD` from `paths_runtime.py`) so the model knows where the user invoked miniouto from.
6. **Register presets** — `"outo"` (uses runtime provider + model), then one preset per `SubagentSpec` named after the spec (uses sub-provider + sub-model). All get `tools=BASE_TOOLS + ("Computer"` for outo only`) and `max_iterations=None`. When `specs` is empty, no subagent presets are registered at all.
7. **Subagent `provider_config` + `provider_passthrough`** — Pulls `max_tokens` from `get_max_output_tokens(subagent_model, sub_provider_name)` via lma, so subagent file-writing calls don't hit Anthropic's 1024 default. The reasoning kwargs (from step 11) are merged into every subagent preset too.
8. **Build `call_subagent` tool (conditional)** — When `specs` is non-empty, calls `_build_subagent_tool(specs, description=_subagent_description(specs), provider_config=subagent_provider_config, provider_passthrough=subagent_passthrough)`. The handler is pre-wrapped with `_wrap_subagent_handler` to track depth + name + id. Registered via `co.register_tool(name, description=description)(wrapped_handler)` (function-call form, not decorator). **When `specs` is empty, this step is skipped entirely** — `call_subagent` is not advertised to the model.
 9. **Register hooks:**
   - `BEFORE_TOOL_CALL` → `_make_tool_call_logger(on_tool_call)` (only if `on_tool_call` is not None).
   - `AFTER_TOOL_CALL` → `_make_tool_result_logger(on_tool_result)` (only if `on_tool_result` is not None — `chat.run_chat` always supplies one).
   - `ON_ITERATION` → `make_summarize_hook(runtime.model, runtime.session or "default", runtime.provider_name)` — always registered.
   - `ON_ITERATION` → `_make_iteration_logger(on_iteration)` (only if `on_iteration` is not None — `chat.run_chat` always supplies one).
   - `AFTER_LLM_CALL` → `_make_response_logger(on_response)` (only if `on_response` is not None).
   - `ON_THINKING` → `_make_thinking_logger(on_thinking)` (only if `on_thinking` is not None — `chat.run_chat` always supplies one).
   - `BEFORE_LLM_CALL` → `_make_alarm_hook(on_alarms)` (**always**, like the repair hook — a job that finished between turns has no other way to reach the model). Drains the subagent alarm deque and appends the alarms to the live message list as one user message (folded into a trailing user message when there is one). Registered **before** the watchdog's `capture_messages` hook so the supervisor's live list already carries them.
   - `ON_STREAM_TEXT` / `ON_STREAM_THINKING` → `_make_stream_logger(on_stream_text)` / `_make_stream_logger(on_stream_thinking)` (only if not None — `chat.run_chat` always supplies both). Registered **in addition to** the watchdog's own tracker registrations for the same two events; each forwards the per-delta `text` payload to the display-only stream dispatcher.
    - `ON_PROVIDER_ERROR` → `_make_provider_error_logger(on_provider_error)` (only if `on_provider_error` is not None — `chat.run_chat` always supplies one).
    - `ON_PROVIDER_ERROR` → `_make_history_repair_hook(on_history_repair)` (**always**, unlike the sink loggers) — repairs the live message list in place before each retry attempt of the 400/422 tool-call rules in `core/error_rules.py`. Each fire first runs `_repair_pairing` (drop duplicate tool results, fill dangling tool calls on the last assistant turn with error results, inserted at the correct position right after that turn's existing results); if that changed nothing, `_cut_poisoned_turn` excises ONE suspect assistant tool-call turn plus its own results — preferring a turn with structurally malformed (non-dict) arguments, else the most recent tool-call turn, so repeated attempts walk backwards through history — and folds an explanatory note into the preceding user message (or inserts one), preserving later turns and the user's current prompt. Whenever a pass mutates the list, the `on_history_repair` callback (wired by `core.chat.run_chat`) persists the repaired history at outo depth, so the next turn doesn't reload the same poison from disk. Gated to `reaction == "retry"` with status 400/422; rate-limit/overload retries pass through untouched.
    - **Watchdog trackers** (`_register_watchdog_trackers`, always) — `activity_tracker_hook` on `AFTER_LLM_CALL`/`AFTER_TOOL_CALL`/`ON_ITERATION`/`ON_PROVIDER_ERROR`/`ON_STREAM_TEXT`/`ON_STREAM_THINKING`, all five `loop_progress_hook` entries, and a `BEFORE_LLM_CALL` live-messages capture keyed by actor (`"outo"` or the subagent sid). Clears `_LIVE_MESSAGES` and calls `repetition.reset()` — repetition counters are scoped to one turn, exactly like the live lists. States are exposed via `watchdog_states()` for `supervised_run`.
10. **Finalize the outo config** — `co.get_agent_preset("outo").to_config()`, merge in caller's `provider_config`, instantiate `co.Agent(outo_config)`. Returns the agent.
11. **Reasoning resolution** — `core.reasoning.resolve_reasoning_passthrough(api_format, model, provider_name, choice)` turns the resolved reasoning choice plus lma's per-model `reasoning_options` into provider-native request kwargs, merged into `provider_passthrough` for both outo and subagent. This is what makes providers return thinking blocks at all (the `ON_THINKING` display path is dead without it). It must go through `provider_passthrough`, not `provider_config`: miniouto registers providers under user-chosen names, and coreouto's `normalize_provider_config` passes config through **untranslated** for unknown provider names — a canonical `reasoning_effort` key would reach the SDK raw and `TypeError`. Precedence (highest wins) for outo: `chat --reasoning <v>` > `Provider.reasoning_effort` (stored per provider) > lma's default for the model's option type (effort → `"medium"` or the middle non-`"none"` value; toggle/budget → `"on"`) > off (no kwargs when lma has no reasoning data — no blind defaults). For the **subagent**, the choice inserts one extra tier: `chat --reasoning <v>` (applies to both) > `RuntimeConfig.subagent_reasoning` (persisted `settings.subagent_reasoning`, via `miniouto subagent set --reasoning`) > the subagent provider's `reasoning_effort` > lma default. `"none"`/`"off"` always disables. Only two request shapes are ever emitted — an effort value and anthropic's `thinking: {"type": "adaptive"}`. Token-budget shapes (`enabled` + `budget_tokens`) are deliberately NOT emitted: many providers reject budget parameters, so lma `budget_tokens` models are driven as a plain toggle and legacy digit choices degrade to `"on"`. Mapping:

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
- **`_make_alarm_hook(on_alarms)`** — Returns the `BEFORE_LLM_CALL` hook that drains `subagents.take_pending_alarms()` and appends the alarms to the live message list. See its own section above.
- **`_make_stream_logger(callback)`** — Returns an `ON_STREAM_TEXT` / `ON_STREAM_THINKING` hook forwarding one incremental `text` fragment per firing (not the accumulated message). Display-only.
- **`_make_cancel_guard(cancel_event)`** — Returns the pair of `BEFORE_LLM_CALL` / `BEFORE_TOOL_CALL` hooks that raise `LoopCancelledError` once the event is set.
- **`_subagent_description(specs)`** — The model-facing background-delegation contract. See its own section above.
- **`_resolve_both_styles(style_name, overrides)`** — Calls `parse_style` on the raw style; returns `(outo_body, list[SubagentSpec])`. The `SubagentSpec.prompt` for each entry is already the body the runtime will assemble (with skills + cwd preamble) — no second parse happens at invocation time.
- **`_read_raw_style(name, overrides)`** — Checks in-memory `overrides` first, then `style_store.read(name)`, then the builtin default.
- **`_load_active_skills()`** — Lists skills and builds a lazy-load catalog: a `# Available Skills` heading, a note that each skill lives at `~/.agents/skills/<name>/` and should be read via Bash when matched, then `- <name>: <description>` lines. Skill bodies are NOT injected. Returns empty string if no skills.
- **`_with_cwd(role, body)`** — Prepends role-specific preamble. Subagent (any named role): *"You operate inside this working directory: {INVOCATION_CWD}…"*; outo: *"The user invoked miniouto from: {INVOCATION_CWD}…"*. Regenerated on every call.
- `_fallback_style(name)` is still used for unreadable style documents and for a subagent tag with an empty body. What is **gone** is the unconditional fallback subagent: an outo-only style (zero named subagents in the parsed style) intentionally registers no `call_subagent` tool at all — see invariant 2.

### `resolve_runtime_from_settings(overrides=None) -> RuntimeConfig`

1. Loads `settings_store.load()` and the optional `ChatOverrides`.
2. Provider: `overrides.provider or s.provider`; raises `RuntimeError("No default provider set. …")` if both are missing.
3. Model: `overrides.model or s.model or provider.default_model`; raises `RuntimeError("No model specified for provider …")` if still missing. (Note the three-level resolution — `settings.model` is priority 2, kept for the `chat --model` CLI flag and legacy sessions.)
4. Style: `overrides.style or s.style or "default"`.
5. Session: `s.session or "default"`.
6. Subagent (`_resolve_subagent`):
   - Provider: `overrides.subagent_provider or s.subagent_provider or None` — `None` means inherit outo's provider (`build_runtime`'s `runtime.subagent_provider or runtime.provider_name` fallback handles it). When a provider IS resolved but isn't configured in the provider store, raises `RuntimeError("Subagent provider … is not configured. …")`.
   - Model: `overrides.subagent_model or s.subagent_model or <subagent provider's default_model> or None`. When a subagent provider IS resolved but no model is, raises `RuntimeError("No model specified for subagent provider …")` telling the user to set one via `chat --subagent-model` or `miniouto subagent set --model`. A model-only override (no subagent provider) is valid — the subagent then runs outo's provider with the overridden model. (`None` model only ever reaches `build_runtime` together with a `None` provider, where both inherit outo's.)
7. Subagent reasoning: `s.subagent_reasoning or None` — populated **independently** of whether a subagent provider/model is set (a reasoning-only subagent override on outo's provider+model is valid). Consumed by `build_runtime`'s `subagent_choice`.

Returns a `RuntimeConfig` with `subagent_provider`/`subagent_model`/`subagent_reasoning` populated per the rules above.

## External API calls & I/O

| Source | Target | Purpose | Failure mode |
|---|---|---|---|
| `core/lma.py` | `https://lma.blp.sh/{provider,model-list,model}` (GET, 15s timeout, `httpx`) | Discover providers, list models, fetch context + max-output caps | `list_*` / `get_model` propagate `httpx.HTTPError`; `find_provider` swallows and returns `None`; 10-minute in-process cache (`_CACHE`) |
| `core/context.py` | (via `core.lma.get_model`) | Fetch `context_window` + `max_output_tokens` for a model | Caught; a **transport failure is returned uncached** (the empty dict is never stored), so one blip can't pin a model to "no caps" for the process; a successful-but-empty answer IS cached. `_MODEL_CACHE` has no TTL; `clear_model_cache()` mirrors `lma.clear_cache()` |
| `core/providers.py` | `co.register_provider`, `co.clear_*` | coreouto global registry I/O | None caught |
| `core/runtime.py` | `tool_registry.register_all`, `co.register_agent_preset`, `co.register_tool`, `co.register_hook` | coreouto global registry I/O | None caught |
| `core/chat.py` | `session_store.begin_turn/update_turn_events/update_history/prune_turn_events/finish_turn`, `session_store.load` | JSON session persistence (schema v2, incremental) | Load is tolerant (never raises); write failures swallowed (`_TurnPersister`, `_finish_turn`) |
| `core/subagents.py` | `SUBAGENT_RESULTS_DIR/<sid>-<ts>.md` | Writes each finished background job's markdown result file, and sends one completion alarm | `mkdir(parents=True, exist_ok=True)` before writing; a generation-retired job writes nothing |
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
| `RuntimeError("Subagent provider … is not configured.")` | `core/runtime.py` | Subagent provider missing in `build_runtime` |
| `RuntimeError("No default provider set. …")` | `core/runtime.py` | Neither override nor settings has a provider |
| `RuntimeError("No model specified for provider …")` | `core/runtime.py` | Neither override nor `provider.default_model` is set |
| `httpx.HTTPError` / `JSONDecodeError` | `core/context.py` | Caught silently inside `_fetch_model_caps` |
| `Exception` in summarizer | `core/context.py` | Caught, returns static fallback message (never corrupts messages) |
| `Exception` from the turn (`_run_with_watchdog`) | `core/chat.py` | Caught to dump tool trace + traceback, then re-raised |
| `LoopStalledError` | `core/runtime.py` → `core/chat.py` | Watchdog exhausted its 3 wakeups — loop re-wedges deterministically; normal failure-diagnostics path |
| `LoopRepeatedError` | `core/runtime.py` → `core/chat.py` | Repetition budget exhausted (3 restarts) — the model kept repeating itself; own branch, not the cancel branch |
| `LoopCancelledError` | `core/runtime.py` → `core/chat.py` | The user's `cancel_event` was set at a hook boundary — clean stop, no failure dump |
| Provider exceptions matching an `ErrorRule` | `core/error_rules.py` + coreouto loop | Absorbed per rule reaction (retry / terminate / user_message); 400/422 tool-call rules additionally repair history via `_make_history_repair_hook` before retrying; surfaced via `ON_PROVIDER_ERROR` → `provider:` loop event, never raised (until repair attempts are exhausted) |

## Defensive patterns worth highlighting

1. **`context.py` summarizer** — refuses to `messages.clear(); messages.extend(summarized)` unless `summarized` is a `list`, preventing the bug in upstream `coreouto.contrib.hooks.auto_summarize_hook`.
2. **`chat.py` ToolCallArgsError** — fires *before* coreouto's handler so the LLM sees a precise, single message about which argument is missing, and the user sees the tool name in the failure trace.
3. **`runtime.py` subagent `max_tokens`** — re-implements `coreouto.agent_as_tool` because the stock helper drops `provider_config` from the preset, causing Anthropic to silently truncate long tool-call outputs (e.g. file writes) at 1024 tokens.
4. **`subagents.py` `_enter_context`** — sets three `ContextVar`s (depth + per-job persona name + 6-hex id) inside the job's own asyncio task because coreouto's `BEFORE_TOOL_CALL` hook is global and has no per-agent context; the per-task context copy is what keeps concurrent background jobs distinguishable, and the persona name lets the actor label be `{name}-{sid}` instead of the legacy `subagent-<6hex>`.
5. **`runtime.py` `_make_alarm_hook`** — appends drained alarms directly to the live message list instead of using `Agent.inject_user_message`, whose queue is drained *before* this hook and would delay the alarm by a full round-trip; and folds into a trailing user message because providers reject two consecutive user messages.
6. **`chat.py` `_RecordingSink.remove_events`** — prunes the event list **in place** (slice assignment) because `_TurnPersister` holds that exact list object, notifies the persister unconditionally (a repair can cut a message from an earlier turn of a resumed session, whose events live only in the file), and excludes the summarizer: history compaction must never erase the human-visible record.
