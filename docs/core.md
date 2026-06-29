# Core Runtime

The `core/` subpackage is where the agent loop is wired together. It is a thin orchestration layer on top of `coreouto` — it doesn't implement an agent loop itself, just translates miniouto's CLI/storage model into coreouto's primitives.

`src/miniouto/core/__init__.py` re-exports `chat`, `events`, `lma`, `providers`, `runtime` (but **not** `context`, which is an implementation detail of `runtime.build_runtime`).

## Files

| File | LOC | Purpose |
|---|---|---|
| `chat.py` | ~320 | Per-turn chat runner, history persistence, failure diagnostics, sink dispatchers |
| `context.py` | ~185 | Context-window monitoring (lma `/model`), auto-summarization hook |
| `events.py` | ~115 | `LoopEvent`, `EventSink` protocol, `NullSink`, `ConsoleEventSink` (CLI rendering) |
| `lma.py` | ~140 | `lma.blp.sh` REST client: list_providers, list_models, get_model, find_provider, slugify |
| `providers.py` | ~135 | Maps `Provider` records → coreouto provider classes; clears global state; maps lma SDKs to coreouto formats |
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
              on_tool_call, on_response, on_iteration) → co.Agent
   │
   │  ┌─ clear_coreouto_state()
   │  ├─ build_coreouto_provider(runtime.provider)      → co.register_provider
   │  ├─ build_coreouto_provider(subagent_provider)
   │  ├─ tools.registry.register_all()                   → Write/Edit/Delete/Bash
   │  ├─ _resolve_both_styles(style_name)                → split_style + skills
   │  ├─ co.register_agent_preset("outo", …)
   │  ├─ co.register_agent_preset("subagent", …)
   │  ├─ _build_subagent_tool("subagent", …)             → co.register_tool
   │  ├─ co.register_hook(BEFORE_TOOL_CALL, _make_tool_call_logger(on_tool_call))
   │  ├─ co.register_hook(ON_ITERATION, make_summarize_hook(model, session, provider))
   │  ├─ co.register_hook(ON_ITERATION, _make_iteration_logger(on_iteration))
   │  └─ co.register_hook(AFTER_LLM_CALL, _make_response_logger(on_response))
   │
   ▼
run_chat(ChatOptions, sink=None) → str
   ├─ load history (if continue_session)
   ├─ persist user MessageRecord
   ├─ build sink dispatchers: _make_tool_call_dispatcher(sink),
   │                          _make_response_dispatcher(sink),
   │                          _make_iteration_dispatcher(sink)
   ├─ agent.call_sync(prompt, history=core_msgs)
   │     ├─ ON_ITERATION hooks → summarize at 80%; emit progress LoopEvent
   │     ├─ LLM call → provider → response
   │     ├─ AFTER_LLM_CALL hook → on_response(content, has_tool_calls) → LoopEvent
   │     └─ tool call?
   │          ├─ BEFORE_TOOL_CALL → _make_tool_call_logger bridges to on_tool_call closure
   │          └─ handler(**args)
   ├─ on exception → _dump_failure_diagnostics (re-raise)
   └─ persist assistant MessageRecord (with tool_calls)
```

---

## `core/events.py`

The sink layer — a tiny abstraction so the chat loop can emit progress/trace events without coupling to either the CLI (`rich.Console`) or the TUI (Textual widgets). Introduced when the TUI was added so `run_chat` could drive both surfaces from the same code path.

- **`LoopEvent`** — frozen dataclass with `actor: str` (`"outo"` / `"subagent"`), `kind: str` (`"tool_call"` / `"response"` / `"context"` / …), and `text: str` (rendered representation).
- **`EventSink`** — `Protocol` with three methods: `emit_loop_event(event)`, `update_activity(name)`, and `emit_final_answer(text)`.
- **`NullSink`** — no-op implementation (used when `run_chat` is called without a sink).
- **`ConsoleEventSink`** — CLI implementation. Renders loop events with `rich` in `orange3`; runs a `rich.status` spinner that is updated by `update_activity` and stopped by `emit_final_answer`; writes the final answer as plain stdout (no rich color) followed by a `------finish------` marker.

---

## `core/chat.py`

### Module-level state

- `_tool_trace: list[dict]` + `_tool_trace_lock: threading.Lock` — last 5 tool calls observed this turn, used by failure diagnostics. Locked because subagent handlers may run concurrently.
- `_LOGGABLE_TOOL_NAMES = ("Bash", "Write", "Edit", "Delete", "call_subagent")` — tools whose arguments get summarized in failure output.
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
    continue_session: bool = False
```

Mirrors the CLI flags for `miniouto chat`. The CLI is responsible for translating `--name` → `session`, `--continue` → `continue_session`, etc.

### `ToolCallArgsError(Exception)`

Raised when an LLM tool call arrives with non-dict or `None` arguments. This prevents the cryptic `TypeError: 'NoneType' object is not iterable` that would fire when coreouto calls `handler(**None)`.

### `run_chat(opts: ChatOptions, sink: EventSink | None = None) -> str`

The main entry point. `sink=None` defaults to `NullSink()`. Steps:

1. Resolves a `RuntimeConfig` from settings + overrides.
2. Computes `provider_config` — `max_tokens` defaults to `get_max_output_tokens(runtime.model, runtime.provider_name)` so providers with low hard caps (Anthropic's 1024) don't truncate Write calls. The `provider_name` argument scopes the lma lookup so we don't accept the first cross-provider match.
3. Builds three sink dispatchers (`_make_tool_call_dispatcher`, `_make_response_dispatcher`, `_make_iteration_dispatcher`) and calls `build_runtime(runtime, provider_config=provider_config, on_tool_call=on_tool_call, on_response=_make_response_dispatcher(sink), on_iteration=_make_iteration_dispatcher(sink))` to get a `co.Agent`.
4. Loads prior history if `continue_session=True`; converts `MessageRecord` → `co.Message`.
5. **Persists the user prompt** to the session before running (`session_store.append`).
6. Clears `_tool_trace`, calls `agent.call_sync(prompt, history=core_msgs)` inside try/except.
7. On exception, calls `_dump_failure_diagnostics` and re-raises.
8. Extracts the final assistant message; serializes `tool_calls` via `model_dump()`.
9. Persists the assistant reply + tool calls; calls `sink.emit_final_answer(response.content)`; returns `response.content`.

### `_make_tool_call_dispatcher(sink: EventSink)`

Builds the `on_tool_call(name, arguments)` closure passed to `build_runtime`. For each tool call:

1. Validates `arguments` is a dict via `_validate_tool_call_args` (raises `ToolCallArgsError` otherwise).
2. Computes `actor = "subagent" if current_subagent_depth() > 0 else "outo"`.
3. Appends to `_tool_trace` for any tool in `_LOGGABLE_TOOL_NAMES`.
4. For `call_subagent`, emits a `LoopEvent(kind="tool_call", text=<message-or-task>)` via the sink (no truncation).
5. For `Bash/Write/Edit/Delete`, emits a `LoopEvent(kind="tool_call", text=_short_arg_summary(name, args))` and calls `sink.update_activity(name)`.

This closure does **not** print directly — rendering is the sink's job (`ConsoleEventSink` for the CLI, the TUI widgets for TUI mode).

### `_make_response_dispatcher(sink: EventSink)`

Builds the `on_response(content, has_tool_calls)` closure. When `has_tool_calls` is true (intermediate LLM response that triggers a tool call), emits a `LoopEvent(kind="response", text=content)` so the sink can render the model's intermediate text.

### `_make_iteration_dispatcher(sink: EventSink)`

Builds the `on_iteration(*, iteration, messages, response, **kwargs)` closure that emits a `LoopEvent(kind="context", text=...)` for progress reporting. (Summarization logic lives separately in `make_summarize_hook`, registered as a second `ON_ITERATION` hook.)

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
| `Write` | `"{path} ({size} bytes)"` |
| `Edit` | `"{path} ({n} edits)"` |
| `Delete` | just the path |
| (other) | `str(args)[:120]` |

---

## `core/context.py`

### Constants

- `SUMMARIZE_THRESHOLD = 0.8` — summarization fires at 80% of context window.
- `DEFAULT_MAX_OUTPUT_TOKENS = 16384` — hard floor. Rationale: Anthropic defaults to 1024 if you don't set it explicitly, silently truncating Write outputs.
- `MAX_OUTPUT_TOKENS_CEILING = 16384` — hard ceiling. Some lma entries report theoretical streaming caps (e.g. 512K) that the non-streaming API rejects.

### `_fetch_model_caps(model, provider_name=None) -> dict[str, int]`

Calls `lma.get_model(model, provider_name)`. Reads `info["context_window"]` and `info["max_output_tokens"]` from the dict that `lma.get_model` returns (lma already unwraps the `models` array server-side). On any exception (network, JSON, etc.), caches an empty dict and returns it (fail-soft, never raises). Result is memoized in the module-level `_MODEL_CACHE` for the lifetime of the process — **note: this cache has no TTL** (unlike `lma._CACHE`'s 10-minute TTL).

### `get_context_window(model, provider_name=None) -> int | None`

Returns the model's context window in tokens, or `None` if unknown. Passing `provider_name` scopes the lma lookup (recommended — otherwise lma returns matches across every provider and we only see the first).

### `get_max_output_tokens(model, provider_name=None) -> int`

Resolution order:
1. `caps["maxOutputTokens"]`
2. `caps["contextWindow"]` (fallback proxy)
3. `DEFAULT_MAX_OUTPUT_TOKENS` (16K hard floor)

Then clamps to `MAX_OUTPUT_TOKENS_CEILING` (16K).

### `make_summarize_hook(model, session_name, provider_name=None) -> Callable`

Returns the `ON_ITERATION` hook (registered alongside `_make_iteration_logger`, so two `ON_ITERATION` hooks fire per iteration). The `session_name` argument is captured but currently unused in the body. `provider_name` (optional) is threaded into `get_context_window` for a more accurate lookup.

- If no context window is known, returns a no-op hook.
- Otherwise returns a closure with two parts:

  **`summarizer(messages) -> list[co.Message]`** — Builds a structured "DONE / IN PROGRESS / NEXT" summary by:
  1. Splitting out system messages.
  2. Detecting and preserving any previous `"[Summary…"` user message.
  3. Flattening user/assistant/tool messages into text, truncating tool content at 500 chars.
  4. Calling a separate `co.Agent(name="summarizer", max_iterations=1)` with the summary prompt.
  5. On any failure, returns a static `"[Summary] Unable to generate LLM summary…"` message.
  6. **Always** returns `[*system_msgs, summary_msg]` — never overwrites with garbage.

  **`hook(*, iteration, messages, response, **_kwargs)`** — Accumulates `response.usage.total_tokens`. When total ≥ 80% of window, runs `summarizer(messages)`. **Critical guard:** if the summarizer returns a non-list, prints a yellow warning to stderr and keeps the original messages (this is the divergence from `coreouto.contrib.hooks.auto_summarize_hook`, which would `clear()` and `extend()` with the non-iterable and both corrupt the turn *and* raise `TypeError`).

---

## `core/providers.py`

### `SUPPORTED_FORMATS`

```python
SUPPORTED_FORMATS = ("openai", "openai-response", "anthropic", "google")
```

Whitelist of `Provider.api_format` values. Validated by `cli/provider.py:add_custom` and by `_instantiate`.

### `sdk_to_format(sdk, api) -> (api_format, base_url)`

Maps an lma provider's `sdk`/`api` pair to a `(api_format, base_url)` tuple. Returns `(None, None)` when the SDK cannot be hosted by any supported coreouto builtin (e.g. `amazon-bedrock`, `bedrock`, anything else without an `api` URL). Unknown SDKs with a non-null `api` URL fall back to `("openai", api)` — the universal gateway shape. Used by `cli/provider.py:providers_cmd` (catalog browse) and `cli/provider.py:add_cmd` (catalog add), plus the TUI `_catalog_add_flow` to filter `lma.list_providers` output to addable entries. See `docs/lma.md` for the full mapping table.

### `add_provider_from_lma(name, api_key, sdk, api, default_model="") -> Provider`

Builds a `storage.providers.Provider` from lma metadata + a user-supplied API key, with `source="lma"` set. Raises `ValueError` when `sdk_to_format` returns `(None, None)`. Used by `cli/provider.py:add_cmd` (catalog add) and `cli/tui.py:_catalog_add_flow`.

### `build_coreouto_provider(provider: Provider) -> None`

Validates `api_format`, instantiates the right coreouto class via `_instantiate`, and calls `co.register_provider(provider.name, instance)`. The instance is constructed with `api_key=provider.api_key or None` and `base_url=provider.base_url or None` (Google wraps `base_url` in `http_options={"base_url": ...}` instead).

### `_instantiate(api_format, api_key, base_url)`

Lazy `import` of each provider module (avoids loading all four SDKs at startup). Raises `ValueError("Unsupported api_format: …")` for anything not in `SUPPORTED_FORMATS`.

### `clear_coreouto_state()`

Wipes four coreouto globals: `clear_providers()`, `clear_agent_presets()`, `clear_tools()`, `clear_hooks()`. Called at the start of every `build_runtime` to make the function idempotent across CLI invocations (the process is long-lived for TUI mode).

### `reset_subagent_registration()`

Suppresses any exception from `co.clear_tools()` (used at start of new runs to remove a stale `call_subagent` from a previous process).

### `provider_kwargs(provider_config, passthrough)`

Helper that bundles the two dicts under the keys coreouto's `AgentConfig` expects. Defined but currently unused in the main code paths; `build_runtime` does its own merging.

---

## `core/runtime.py`

### Module-level constants / state

- `ALL_TOOLS = ["Write", "Edit", "Delete", "Bash", "call_subagent"]` — the fixed tool set used for both presets.
- `_SUBAGENT_DEPTH: ContextVar[int]` — defaults to 0. Read by `chat._make_tool_call_dispatcher`'s closure (via `current_subagent_depth()`) to label each tool trace with actor `outo` vs `subagent`. Mutated only by `_wrap_subagent_handler`.

### `current_subagent_depth() -> int`

Returns `_SUBAGENT_DEPTH.get()`. Read by `chat.py`.

### `_wrap_subagent_handler(inner)`

Returns an async wrapper that does `_SUBAGENT_DEPTH.set(get() + 1)` / `reset(token)` around `await inner(task)`. Necessary because `co.BEFORE_TOOL_CALL` is a global hook with no per-agent context — the depth ContextVar is the only signal of "are we currently inside a subagent?".

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
```

Per-call overrides from CLI flags. All optional.

### `build_runtime(runtime, *, style_overrides=None, provider_config=None, on_tool_call=None, on_response=None, on_iteration=None) -> co.Agent`

The heart of miniouto. Steps:

1. **`clear_coreouto_state()`** — Reset all four coreouto registries.
2. **Provider registration** — Look up `runtime.provider_name` in the provider store; raise `RuntimeError` if missing. Call `build_coreouto_provider` for it. Repeat for the subagent provider (which may differ).
3. **`tools.registry.register_all()`** — Registers `Write/Edit/Delete/Bash` tools in coreouto.
4. **`_resolve_both_styles`** — Loads the named style document (or `builtin_default` for `"default"`), splits at `<subagent>…</subagent>` tags, and prepends active skill content to both halves. Returns `(outo_part, subagent_part)`.
5. **`_with_cwd(role, body)`** — Prepends an absolute-cwd preamble (using `INVOCATION_CWD` from `paths_runtime.py`) so the model knows where the user invoked miniouto from.
6. **Register two presets** — `"subagent"` (uses sub-provider + sub-model) and `"outo"` (uses runtime provider + model). Both get `tools=ALL_TOOLS` and `max_iterations=None`.
7. **Subagent `provider_config`** — Pulls `max_tokens` from `get_max_output_tokens(subagent_model, sub_provider_name)` via lma, so subagent Write calls don't hit Anthropic's 1024 default.
8. **Build `call_subagent` tool** — Via `_build_subagent_tool("subagent", description=_subagent_description(), provider_config=subagent_provider_config)`. The handler is pre-wrapped with `_wrap_subagent_handler` to track depth. Registered via `co.register_tool(name, description=description)(wrapped_handler)` (function-call form, not decorator).
9. **Register hooks (up to 4):**
   - `BEFORE_TOOL_CALL` → `_make_tool_call_logger(on_tool_call)` (only if `on_tool_call` is not None).
   - `ON_ITERATION` → `make_summarize_hook(runtime.model, runtime.session or "default", runtime.provider_name)` — always registered.
   - `ON_ITERATION` → `_make_iteration_logger(on_iteration)` (only if `on_iteration` is not None — `chat.run_chat` always supplies one).
   - `AFTER_LLM_CALL` → `_make_response_logger(on_response)` (only if `on_response` is not None).
10. **Finalize the outo config** — `co.get_agent_preset("outo").to_config()`, merge in caller's `provider_config`, instantiate `co.Agent(outo_config)`. Returns the agent.

### Hook helpers

- **`_make_tool_call_logger(callback)`** — Bridges coreouto's `BEFORE_TOOL_CALL` hook signature to the simpler `on_tool_call(name, args)` contract used by `chat.py`. Forwards `(name, arguments)` to `callback`.
- **`_make_response_logger(callback)`** — Returns an `AFTER_LLM_CALL` hook that invokes `callback(response.content, bool(tool_calls))` for non-empty responses. No printing — rendering is the sink's job.
- **`_make_iteration_logger(callback)`** — Returns an `ON_ITERATION` hook that forwards to `callback` so the sink can render progress.
- **`_subagent_description()`** — Hardcoded prompt fragment explaining that the subagent has its own Write/Edit/Delete/Bash and a fresh context, blocks until the subagent finishes, returns the final text.
- **`_resolve_both_styles(style_name, overrides)`** — Splits the style at `<subagent>…</subagent>`. If no subagent section exists, uses `_fallback_style("subagent")`. Prepends active skills to both halves.
- **`_read_raw_style(name, overrides)`** — Checks in-memory `overrides` first, then `style_store.read(name)`, then the builtin default, then `_fallback_style`.
- **`_load_active_skills()`** — Lists skills, formats each as `"# Skill: {name}\n\n{content}"` (skipping skills with empty content), joins with `\n\n---\n\n`. Returns empty string if no skills.
- **`_with_cwd(role, body)`** — Prepends role-specific preamble. Subagent: *"You operate inside this working directory: {INVOCATION_CWD}…"*; outo: *"The user invoked miniouto from: {INVOCATION_CWD}…"*. Regenerated on every call.
- **`_fallback_style(name)`** — Hardcoded prompts used when no style file exists. Subagent: *"You are subagent. Execute the brief directly…"*; otherwise: *"You are {name}. Use the call_subagent tool for non-trivial work…"*. Both mention the `continue_loop` tool for sending text while still planning more tool calls.

### `resolve_runtime_from_settings(overrides=None) -> RuntimeConfig`

1. Loads `settings_store.load()` and the optional `ChatOverrides`.
2. Provider: `overrides.provider or s.provider`; raises `RuntimeError("No default provider set. …")` if both are missing.
3. Model: `overrides.model or s.model or provider.default_model`; raises `RuntimeError("No model specified for provider …")` if still missing. (Note the three-level resolution — `settings.model` is priority 2, kept for the `chat --model` CLI flag and legacy sessions.)
4. Style: `overrides.style or s.style or "default"`.
5. Session: `s.session or "default"`.

Returns a `RuntimeConfig`. Subagent-related fields (`subagent_model`, `subagent_provider`) are left as `None` here — `build_runtime` fills them by falling back to the outo provider/model.

## External API calls & I/O

| Source | Target | Purpose | Failure mode |
|---|---|---|---|
| `core/lma.py` | `https://lma.blp.sh/{provider,model-list,model}` (GET, 15s timeout, `httpx`) | Discover providers, list models, fetch context + max-output caps | `list_*` / `get_model` propagate `httpx.HTTPError`; `find_provider` swallows and returns `None`; 10-minute in-process cache (`_CACHE`) |
| `core/context.py` | (via `core.lma.get_model`) | Fetch `context_window` + `max_output_tokens` for a model | Caught, caches `{}` in `_MODEL_CACHE` (no TTL — process lifetime), returns 16K default |
| `core/providers.py` | `co.register_provider`, `co.clear_*` | coreouto global registry I/O | None caught |
| `core/runtime.py` | `tool_registry.register_all`, `co.register_agent_preset`, `co.register_tool`, `co.register_hook` | coreouto global registry I/O | None caught |
| `core/chat.py` | `session_store.append`, `session_store.load` | JSON session persistence | Propagated to caller |
| `core/chat.py` | `agent.call_sync(prompt, history=core_msgs)` | LLM call (network to configured provider) | Caught locally for diagnostics, re-raised |
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
| `Exception` from `agent.call_sync` | `core/chat.py` | Caught to dump tool trace + traceback, then re-raised |

## Defensive patterns worth highlighting

1. **`context.py` summarizer** — refuses to `messages.clear(); messages.extend(summarized)` unless `summarized` is a `list`, preventing the bug in upstream `coreouto.contrib.hooks.auto_summarize_hook`.
2. **`chat.py` ToolCallArgsError** — fires *before* coreouto's handler so the LLM sees a precise, single message about which argument is missing, and the user sees the tool name in the failure trace.
3. **`runtime.py` subagent `max_tokens`** — re-implements `coreouto.agent_as_tool` because the stock helper drops `provider_config` from the preset, causing Anthropic to silently truncate Write outputs at 1024 tokens.
4. **`runtime.py` `_wrap_subagent_handler`** — uses a `ContextVar` because coreouto's `BEFORE_TOOL_CALL` hook is global and has no per-agent context.
