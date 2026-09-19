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
              on_iteration, on_provider_error, reasoning) → co.Agent
   │
   │  ┌─ clear_coreouto_state()
   │  ├─ build_coreouto_provider(runtime.provider)      → co.register_provider
   │  ├─ build_coreouto_provider(subagent_provider)
   │  ├─ tools.registry.register_all()                   → Bash/Image/Video/Audio
   │  ├─ _resolve_both_styles(style_name)                → split_style + skills
   │  ├─ co.register_agent_preset("outo", …)
   │  ├─ co.register_agent_preset("subagent", …)
   │  ├─ _build_subagent_tool("subagent", …)             → co.register_tool
   │  │    └─ _wrap_subagent_handler: mints subagent-<6hex> id per invocation
   │  ├─ co.register_hook(BEFORE_TOOL_CALL, _make_tool_call_logger(on_tool_call))
   │  ├─ co.register_hook(AFTER_TOOL_CALL, _make_tool_result_logger(on_tool_result))
   │  ├─ co.register_hook(ON_ITERATION, make_summarize_hook(...) → (on_iteration, before_llm_call)) + BEFORE_LLM_CALL compaction hook
    │  ├─ co.register_hook(ON_ITERATION, _make_iteration_logger(on_iteration))
    │  ├─ co.register_hook(AFTER_LLM_CALL, _make_response_logger(on_response))
    │  ├─ co.register_hook(ON_THINKING, _make_thinking_logger(on_thinking))
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
    │     ├─ cancel_event set → cancel task promptly, raise LoopCancelledError
    │     ├─ ON_ITERATION hooks → summarize at 80% (counter resets); emit progress LoopEvent
   │     ├─ LLM call → provider → response
   │     ├─ AFTER_LLM_CALL hook → on_response(content, has_tool_calls) → LoopEvent
   │     ├─ ON_THINKING hook → on_thinking(text) → LoopEvent(kind="thinking")
   │     ├─ call_subagent start/end → observer → LoopEvent(subagent_start/end, subagent_id)
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

- **`LoopEvent`** — dataclass with `actor: str` (`"outo"` / `"subagent-<6hex>"` / `"provider"` / `"watchdog"`), `kind: str`, `text: str`, optional `tool_name`, optional `subagent_id` (the 6-hex invocation id, set on every event emitted inside a subagent), optional `detail` (extra payload not shown by the CLI — currently the full untruncated Bash command on `kind="tool"` events). Kinds: `"tool"` / `"tool_result"` / `"response"` / `"thinking"` / `"context"` / `"error"` / `"wakeup"` / `"subagent_start"` / `"subagent_end"`. `to_dict()`/`from_dict()` provide the sparse JSON form stored in session turns (optional fields are only serialized when truthy, so older session files load unchanged).
- **`EventSink`** — `Protocol`: `begin_working()`, `update_activity(text)`, `end_working()`, `emit_loop_event(event)`, `emit_final_answer(content, session_name)`.
- **`NullSink`** — no-op implementation (used when `run_chat` is called without a sink).
- **`ConsoleEventSink`** — CLI implementation. Renders loop events as `{actor}: {text}` in `orange3`; `kind="thinking"` renders the **full** reasoning text as `{actor}:thinking: {text}` (dim); `subagent_start`/`subagent_end` render as a single-line preview (whitespace-flattened, 120 chars; `subagent_end` dim); `kind="tool_result"` is **ignored** (early return — the events exist for the TUI; CLI output stays byte-identical to the pre-tool_result behavior); runs a `rich.status` spinner updated by `update_activity`; writes the final answer as plain stdout followed by a `------finish------` marker.

---

## `core/chat.py`

### Module-level state

- `_tool_trace: list[dict]` + `_tool_trace_lock: threading.Lock` — last 5 tool calls observed this turn, used by failure diagnostics. Locked because subagent handlers may run concurrently.
- `_LOGGABLE_TOOL_NAMES = ("Bash", "Image", "Video", "Audio", "call_subagent")` — tools whose arguments get summarized in failure output.
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
3. Builds the sink dispatchers (`_make_tool_call_dispatcher`, `_make_tool_result_dispatcher`, `_make_response_dispatcher`, `_make_thinking_dispatcher`, `_make_iteration_dispatcher`, `_make_provider_error_dispatcher`) and calls `build_runtime(...)` to get a `co.Agent`.
4. Loads prior history if `continue_session=True` via `_load_coreouto_history` — session `history` dicts are validated back into `co.Message` objects; invalid entries degrade to plain text (media block lists are flattened via `_flatten_media_blocks`) but **keep `tool_call_id`/`name` for tool messages** so tool_use/tool_result pairing survives even unknown schema drift (without the id the provider 400s the whole request on resume).
5. Installs `_make_subagent_dispatcher(sink)` as the subagent observer (`set_subagent_observer`), cleared in a `finally` after the call.
6. Clears `_tool_trace`, runs the turn as `asyncio.run(_run_with_watchdog(agent, opts.prompt, core_msgs, sink=sink, cancel_event=opts.cancel_event))` inside try/except — see the watchdog subsection below.
7. On `LoopCancelledError` (the `cancel_event` was set), finishes the turn as `status="interrupted"` and re-raises WITHOUT `_dump_failure_diagnostics` — a user force-stop is a clean cancel, not a failure. On any other exception, appends a record-only `kind="error"` LoopEvent (so a session reload shows why the turn ended), finishes the turn as `"interrupted"`, calls `_dump_failure_diagnostics`, re-raises.
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

### `supervised_run(start, prompt, history, *, live_key, level, on_wakeup, cancel_event=None, states=None)` (`core/runtime.py`)

The stall watchdog, following coreouto `examples/27_wakeup.py` + `examples/28_resume_interrupted.py` (coreouto >= 0.11.1). `start(prompt, history)` creates one attempt's coroutine; `start(None, history)` must resume from a transcript alone. Every `call_subagent` invocation runs its own supervisor at `level=depth` (`_wrap_subagent_handler`), and outo's turn runs at `level=0`. A 5-second poll (`WATCHDOG_CHECK_SECONDS`) reads the contrib tracker states installed by `build_runtime` (`activity_tracker_hook` + `loop_progress_hook` + a BEFORE_LLM_CALL live-messages capture keyed by actor — see `_register_watchdog_trackers`):

- **Stall wakeup** — when `activity.seconds_since_last_activity() > API_STALL_TIMEOUT_SECONDS` (30 min) AND `progress.phase != "tool_call"` AND no deeper supervisor is active (`_ACTIVE_SUPERVISORS`), the attempt task is cancelled, the actor's live message list is recovered via `sanitize_history` (drops system messages and any trailing assistant turn with unanswered tool calls — providers 400 on dangling tool calls), `on_wakeup` fires, and the call restarts **without a user message**: coreouto >= 0.11.1's `call(history=...)` sends the sanitized transcript exactly as declared and simply continues. If nothing is recoverable (wedged before the first LLM call), it falls back to the original prompt. Max `WATCHDOG_MAX_WAKEUPS = 3` restarts, then `LoopStalledError`.
- **Supervisor deferral** — a supervisor stays quiet while any deeper level is active: the deeper call's supervisor owns the silence and does the cancelling/restart in place. This is what keeps a subagent stall from tearing down the parent turn.
- **Phase exemption** — while `phase == "tool_call"` the watchdog stays quiet: tools carry their own bounds (Bash: `BASH_TIMEOUT_SECONDS`). A healthy nested agent keeps the global activity clock fresh through its own hook firings, so only a genuinely silent tool-free loop wakes a supervisor.

The SDK timeout (`API_STALL_TIMEOUT_SECONDS` on every provider constructor) + `TIMEOUT_ERRORS` rules catch hung HTTP requests; supervisors catch everything else (wedged between operations, deadlocked hook, ...). The resumed call's `Response.messages` is the sanitized transcript plus the new turns — no injected user message, so the persisted session history reads as one uninterrupted conversation.

### `LoopStalledError(Exception)` (`core/runtime.py`)

Raised when a supervisor exhausts its wakeups — the loop re-wedges within the stall limit every time, so something is deterministically wrong (e.g. a deadlocked hook). At outo level it surfaces through the normal failure-diagnostics path; inside a subagent handler it becomes an error `ToolResult` so the parent can re-delegate.

### `_actor_label() -> (str, str | None)`

Shared by all dispatchers: returns `("subagent-<6hex>", sid)` inside a subagent invocation (the id ContextVar is always set there), `("subagent", None)` as a defensive depth-only fallback, `("outo", None)` otherwise.

### `_make_tool_call_dispatcher(sink: EventSink)`

Builds the `on_tool_call(name, arguments)` closure passed to `build_runtime`. For each tool call:

1. Validates `arguments` is a dict via `_validate_tool_call_args` (raises `ToolCallArgsError` otherwise).
2. Computes actor + subagent id via `_actor_label()`.
3. Appends to `_tool_trace`.
4. For `call_subagent`, **emits nothing** — the subagent observer emits the `subagent_start` event with the minted id immediately after (the `BEFORE_TOOL_CALL` hook for `call_subagent` still runs in the *parent* context, so the id does not exist yet here).
5. For `Bash/Image/Video/Audio`, emits a `LoopEvent(kind="tool", text=f"{name} {preview}", subagent_id=sid)` and updates the spinner activity (the subagent label when nested, else the tool name). For `Bash` the event also carries `detail=arguments["command"]` — the **full untruncated command, verbatim** (no newline flattening) — for sinks that want the raw input; `detail` is `None` for the media tools.

### `_make_tool_result_dispatcher(sink: EventSink)`

Builds the `on_tool_result(name, result)` closure wired into coreouto's `AFTER_TOOL_CALL` hook (via `_make_tool_result_logger`), where `result` is coreouto's `ToolResult` (`.content`, `.blocks`, `.is_error`, `.flatten_text()`). For each tool result:

1. Skips `call_subagent` entirely — the subagent observer's `subagent_end` event already carries that result.
2. Only emits for `Bash/Image/Video/Audio`.
3. `text = result.flatten_text()`, prefixed with `"error: "` when `result.is_error` (mirrors the `subagent_end` "error:" convention the TUI keys on), truncated to 4000 chars with a `"\n… [truncated]"` suffix when over.
4. Emits `LoopEvent(kind="tool_result", text=text, tool_name=name)` with the current actor/subagent id via `_actor_label()`. The CLI's `ConsoleEventSink` ignores this kind; it exists so the TUI can display tool return values.

The whole body is wrapped in `try/except Exception` — a sink failure here must never break the agent loop.

### `_make_subagent_dispatcher(sink: EventSink)`

Builds the `(phase, sid, text)` lifecycle callback installed via `set_subagent_observer`. `"start"` (text = task brief) emits `LoopEvent(kind="subagent_start", actor=f"subagent-{sid}", subagent_id=sid)` and switches the spinner activity to the subagent label; `"end"` (text = final result or `error: …`) emits `kind="subagent_end"`. The **full** task/result text goes into the event (and thus the session turn and the TUI detail screen); each sink truncates for its own display (CLI: one line, 120 chars).

### `_make_thinking_dispatcher(sink: EventSink)`

Builds the `on_thinking(thinking)` closure wired into coreouto's `ON_THINKING` hook. Emits `LoopEvent(kind="thinking", text=thinking)` with the current actor/subagent id — reasoning fires inside subagent loops too and is labeled `subagent-<6hex>` automatically. The full text is preserved in the event (and thus in the session turn); sinks decide how much to display.

### `_make_response_dispatcher(sink: EventSink, pending: dict)`

Builds the `on_response(content, has_tool_calls)` closure. When `has_tool_calls` is true (intermediate LLM response that triggers a tool call), the text is **stashed in `pending`** (keyed by actor: `""` for outo, the subagent id inside a subagent) instead of being emitted immediately — coreouto fires `AFTER_LLM_CALL` *before* `ON_THINKING`, so an immediate emit would place the model's text above its own thinking row. The stash is flushed by `_make_iteration_dispatcher` (below), which `ON_ITERATION` guarantees runs after `ON_THINKING` — every sink therefore sees `thinking` → `response` in that order. The stashed tuple carries the actor/subagent id captured at stash time, so parallel subagent streams stay self-consistent.

### `_make_iteration_dispatcher(sink: EventSink, pending: dict)`

Builds the `on_iteration(*, iteration, messages, response, **kwargs)` closure that first flushes any response text stashed for the current actor (emitting the deferred `LoopEvent(kind="response")`), then emits a `LoopEvent(kind="context", text=...)` for progress reporting — net display order per iteration is thinking → model text → context line. (Summarization logic lives separately in `make_summarize_hook`, registered as a second `ON_ITERATION` hook.)

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

Calls `lma.get_model(model, provider_name)`. Reads `info["context_window"]` and `info["max_output_tokens"]` from the dict that `lma.get_model` returns (lma already unwraps the `models` array server-side). On any exception (network, JSON, etc.), caches an empty dict and returns it (fail-soft, never raises). Result is memoized in the module-level `_MODEL_CACHE` for the lifetime of the process — **note: this cache has no TTL** (unlike `lma._CACHE`'s 10-minute TTL).

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

Suppresses any exception from `co.clear_tools()` (used at start of new runs to remove a stale `call_subagent` from a previous process).

### `provider_kwargs(provider_config, passthrough)`

Helper that bundles the two dicts under the keys coreouto's `AgentConfig` expects. Defined but currently unused in the main code paths; `build_runtime` does its own merging.

---

## `core/runtime.py`

### Module-level constants / state

- `ALL_TOOLS = ["Bash", "Image", "Video", "Audio", "call_subagent"]` — the fixed tool set used for both presets.
- `_SUBAGENT_DEPTH: ContextVar[int]` — defaults to 0. Read by `chat.py` dispatchers (via `current_subagent_depth()`) as a defensive fallback label. Mutated only by `_wrap_subagent_handler`.
- `_SUBAGENT_ID: ContextVar[str | None]` — defaults to None. The 6-hex id of the innermost active subagent invocation; read by `chat._actor_label()` to build `subagent-<6hex>` labels. ContextVars are copied per asyncio task, so parallel `call_subagent` invocations each see their own id — this is what makes concurrent subagents distinguishable.
- `_SUBAGENT_OBSERVER` — module-level `(phase, sid, text) -> None` slot set per turn by `chat.run_chat` via `set_subagent_observer()`. Because coreouto's hooks are global, this slot is the bridge between the wrapped `call_subagent` handler and the active turn's sink. Observer exceptions are suppressed (`contextlib.suppress`) so a sink can never break the subagent loop.

### `current_subagent_depth() -> int` / `current_subagent_id() -> str | None`

ContextVar getters read by `chat.py`.

### `set_subagent_observer(observer | None)`

Installs/clears the lifecycle observer.

### `_wrap_subagent_handler(inner)`

Returns an async wrapper that, per invocation: mints `secrets.token_hex(3)` (6 hex chars), sets `_SUBAGENT_DEPTH` + `_SUBAGENT_ID`, notifies the observer `"start"` (task brief), `"wakeup"` (supervisor restart), and `"end"` (final result, or `error: {type}: {msg}` on exception), pops the invocation's `_LIVE_MESSAGES` entry, and resets both ContextVars in `finally`. Necessary because `co.BEFORE_TOOL_CALL` is a global hook with no per-agent context — and the wrapper runs exactly once per subagent invocation, which is what makes it the correct mint point for the id. The subagent call runs through `supervised_run` at `level=depth`, so a wedged subagent is cancelled and resumed from its own sanitized transcript in place — the parent turn is not taken down. If the subagent's supervisor exhausts its wakeups, the raised `LoopStalledError` becomes an error `ToolResult` via coreouto's tool-call wrapper, so the parent sees the failure and can re-delegate.

### `_build_subagent_tool(preset_name, *, description, provider_config)`

Reimplements `coreouto.contrib.agent_as_tool` for one specific reason: the stock helper drops `provider_config` when calling `preset.to_config()`. This version:

1. Fetches the preset, gets its config.
2. Merges `provider_config` (which always contains at least `max_tokens`) into `config.provider_config`.
3. Builds a new `co.Agent(config)` and wraps it as a `co.Tool` named `call_<preset_name>` (so the tool becomes `call_subagent`).
4. The `parameters` schema is hardcoded: `{"task": {"type": "string"}}`, required: `["task"]`.
5. The async handler returns `sub_agent.call(task).content`.

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

### `build_runtime(runtime, *, style_overrides=None, provider_config=None, on_tool_call=None, on_tool_result=None, on_response=None, on_thinking=None, on_iteration=None, on_provider_error=None, reasoning=None, cancel_event=None) -> co.Agent`

The heart of miniouto. Steps:

1. **`clear_coreouto_state()`** — Reset all four coreouto registries.
   - **`cancel_event` guard** — If a `threading.Event` is passed, a `_make_cancel_guard` hook is registered FIRST on both `BEFORE_LLM_CALL` and `BEFORE_TOOL_CALL`: once the event is set the hook raises `LoopCancelledError`, which coreouto's `trigger` does not swallow, so it propagates out of the turn. This is cooperative cancellation — the loop stops before the next step; an in-flight tool execution is not interrupted by the guard (Bash polls the same event and kills its own process). Independently, the watchdog's poll (`core/chat.py:_run_with_watchdog`) checks the same event every 5 s and hard-cancels the task, so even an in-flight LLM call is interrupted promptly. Inside a `call_subagent` handler the raise is caught by coreouto's tool-call wrapper and surfaces as an error `ToolResult` (killing the subagent immediately); the outo loop then stops at its next `BEFORE_LLM_CALL`.
2. **Provider registration** — Look up `runtime.provider_name` in the provider store; raise `RuntimeError` if missing. Call `build_coreouto_provider` for it. Repeat for the subagent provider (which may differ).
3. **`tools.registry.register_all()`** — Registers `Bash/Image/Video/Audio` tools in coreouto.
4. **`_resolve_both_styles`** — Loads the named style document (or `builtin_default` for `"default"`), splits at `<subagent>…</subagent>` tags, and prepends active skill content to both halves. Returns `(outo_part, subagent_part)`.
5. **`_with_cwd(role, body)`** — Prepends an absolute-cwd preamble (using `INVOCATION_CWD` from `paths_runtime.py`) so the model knows where the user invoked miniouto from.
6. **Register two presets** — `"subagent"` (uses sub-provider + sub-model) and `"outo"` (uses runtime provider + model). Both get `tools=ALL_TOOLS` and `max_iterations=None`.
7. **Subagent `provider_config`** — Pulls `max_tokens` from `get_max_output_tokens(subagent_model, sub_provider_name)` via lma, so subagent file-writing calls don't hit Anthropic's 1024 default.
8. **Build `call_subagent` tool** — Via `_build_subagent_tool("subagent", description=_subagent_description(), provider_config=subagent_provider_config)`. The handler is pre-wrapped with `_wrap_subagent_handler` to track depth. Registered via `co.register_tool(name, description=description)(wrapped_handler)` (function-call form, not decorator).
 9. **Register hooks:**
   - `BEFORE_TOOL_CALL` → `_make_tool_call_logger(on_tool_call)` (only if `on_tool_call` is not None).
   - `AFTER_TOOL_CALL` → `_make_tool_result_logger(on_tool_result)` (only if `on_tool_result` is not None — `chat.run_chat` always supplies one).
   - `ON_ITERATION` → `make_summarize_hook(runtime.model, runtime.session or "default", runtime.provider_name)` — always registered.
   - `ON_ITERATION` → `_make_iteration_logger(on_iteration)` (only if `on_iteration` is not None — `chat.run_chat` always supplies one).
   - `AFTER_LLM_CALL` → `_make_response_logger(on_response)` (only if `on_response` is not None).
   - `ON_THINKING` → `_make_thinking_logger(on_thinking)` (only if `on_thinking` is not None — `chat.run_chat` always supplies one).
    - `ON_PROVIDER_ERROR` → `_make_provider_error_logger(on_provider_error)` (only if `on_provider_error` is not None — `chat.run_chat` always supplies one).
    - `ON_PROVIDER_ERROR` → `_make_history_repair_hook(on_history_repair)` (**always**, unlike the sink loggers) — repairs the live message list in place before each retry attempt of the 400/422 tool-call rules in `core/error_rules.py`. Each fire first runs `_repair_pairing` (drop duplicate tool results, fill dangling tool calls on the last assistant turn with error results, inserted at the correct position right after that turn's existing results); if that changed nothing, `_cut_poisoned_turn` excises ONE suspect assistant tool-call turn plus its own results — preferring a turn with structurally malformed (non-dict) arguments, else the most recent tool-call turn, so repeated attempts walk backwards through history — and folds an explanatory note into the preceding user message (or inserts one), preserving later turns and the user's current prompt. Whenever a pass mutates the list, the `on_history_repair` callback (wired by `core.chat.run_chat`) persists the repaired history at outo depth, so the next turn doesn't reload the same poison from disk. Gated to `reaction == "retry"` with status 400/422; rate-limit/overload retries pass through untouched.
    - **Watchdog trackers** (`_register_watchdog_trackers`, always) — `activity_tracker_hook` on `AFTER_LLM_CALL`/`AFTER_TOOL_CALL`/`ON_ITERATION`/`ON_PROVIDER_ERROR`/`ON_STREAM_TEXT`/`ON_STREAM_THINKING`, all five `loop_progress_hook` entries, and a `BEFORE_LLM_CALL` live-messages capture keyed by actor (`"outo"` or the subagent sid). States are exposed via `watchdog_states()` for `supervised_run`.
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
- **`_make_history_repair_hook()`** — Returns the `ON_PROVIDER_ERROR` repair hook paired with the retry rules in `core/error_rules.py` (see step 9 of `build_runtime` and the `core/error_rules.py` section). Uses `_repair_pairing` / `_cut_poisoned_turn`.
- **`_subagent_description()`** — Hardcoded prompt fragment explaining that the subagent has its own Bash/Image/Video/Audio and a fresh context, blocks until the subagent finishes, returns the final text.
- **`_resolve_both_styles(style_name, overrides)`** — Splits the style at `<subagent>…</subagent>`. If no subagent section exists, uses `_fallback_style("subagent")`. Prepends active skills to both halves.
- **`_read_raw_style(name, overrides)`** — Checks in-memory `overrides` first, then `style_store.read(name)`, then the builtin default, then `_fallback_style`.
- **`_load_active_skills()`** — Lists skills and builds a lazy-load catalog: a `# Available Skills` heading, a note that each skill lives at `~/.agents/skills/<name>/` and should be read via Bash when matched, then `- <name>: <description>` lines. Skill bodies are NOT injected. Returns empty string if no skills.
- **`_with_cwd(role, body)`** — Prepends role-specific preamble. Subagent: *"You operate inside this working directory: {INVOCATION_CWD}…"*; outo: *"The user invoked miniouto from: {INVOCATION_CWD}…"*. Regenerated on every call.
- **`_fallback_style(name)`** — Hardcoded prompts used when no style file exists. Subagent: *"You are subagent. Execute the brief directly…"*; otherwise: *"You are {name}. Use the call_subagent tool for non-trivial work…"*. Both mention the `continue_loop` tool for sending text while still planning more tool calls.

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
| `core/context.py` | (via `core.lma.get_model`) | Fetch `context_window` + `max_output_tokens` for a model | Caught, caches `{}` in `_MODEL_CACHE` (no TTL — process lifetime), returns 16K default |
| `core/providers.py` | `co.register_provider`, `co.clear_*` | coreouto global registry I/O | None caught |
| `core/runtime.py` | `tool_registry.register_all`, `co.register_agent_preset`, `co.register_tool`, `co.register_hook` | coreouto global registry I/O | None caught |
| `core/chat.py` | `session_store.begin_turn/update_turn_events/update_history/finish_turn`, `session_store.load` | JSON session persistence (schema v2, incremental) | Load is tolerant (never raises); write failures swallowed (`_TurnPersister`, `_finish_turn`) |
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
| `LoopStalledError` | `core/chat.py` | Watchdog exhausted its 3 wakeups — loop re-wedges deterministically; normal failure-diagnostics path |
| Provider exceptions matching an `ErrorRule` | `core/error_rules.py` + coreouto loop | Absorbed per rule reaction (retry / terminate / user_message); 400/422 tool-call rules additionally repair history via `_make_history_repair_hook` before retrying; surfaced via `ON_PROVIDER_ERROR` → `provider:` loop event, never raised (until repair attempts are exhausted) |

## Defensive patterns worth highlighting

1. **`context.py` summarizer** — refuses to `messages.clear(); messages.extend(summarized)` unless `summarized` is a `list`, preventing the bug in upstream `coreouto.contrib.hooks.auto_summarize_hook`.
2. **`chat.py` ToolCallArgsError** — fires *before* coreouto's handler so the LLM sees a precise, single message about which argument is missing, and the user sees the tool name in the failure trace.
3. **`runtime.py` subagent `max_tokens`** — re-implements `coreouto.agent_as_tool` because the stock helper drops `provider_config` from the preset, causing Anthropic to silently truncate long tool-call outputs (e.g. file writes) at 1024 tokens.
4. **`runtime.py` `_wrap_subagent_handler`** — uses two `ContextVar`s (depth + per-invocation 6-hex id) because coreouto's `BEFORE_TOOL_CALL` hook is global and has no per-agent context; the id survives `asyncio.gather` (each task gets a context copy), so parallel subagents stay distinguishable.
