# Architecture

## Purpose

`miniouto` is a **thin orchestration layer** on top of the [`coreouto`](https://github.com/llaa33219/coreouto) agent library. It provides:

1. A CLI (`miniouto`) and an optional Textual TUI for interactive use.
2. File-driven configuration (TOML for providers/settings, Markdown for styles, JSON for sessions).
3. Bundled agent "style" templates (three personas: a minimal default and two orchestrators).
4. A minimal tool surface (Bash + Image/Video/Audio media viewers, plus `call_subagent` when the active style declares ≥1 named subagent).
5. Persistence of session history.
6. Per-turn diagnostic output to stderr.

The harness is **stateless across invocations except for what's persisted to `~/.miniouto/`**. No server, no separate daemon process. Every CLI run is a fresh Python process (except TUI mode, which is one long-lived process). The one thing that lives in the background is a *thread* inside the running process: `core/subagents.py` owns a daemon thread with its own event loop so a delegated subagent can outlive the turn that started it (invariant 5).

## Three principles

From `README.md`:

1. **Minimalism** — No bloat. Extend with styles.
2. **Automation-friendly** — Full CLI; TUI is optional.
3. **Fluidity** — Adapts to any environment.

## Package layout

```
src/miniouto/
├── __init__.py              # __version__ = "0.1.1"
├── paths_runtime.py         # INVOCATION_CWD: Path  (captured cwd at import)
├── cli/                     # Typer commands + Textual TUI
├── core/                    # Chat loop + runtime assembly + event sinks
│   ├── __init__.py
│   ├── chat.py
│   ├── context.py
│   ├── events.py            # LoopEvent, EventSink protocol, NullSink, ConsoleEventSink
│   ├── lma.py
│   ├── providers.py
│   ├── repetition.py         # repetition watchdog (phrase / tool loops)
│   ├── runtime.py
│   └── subagents.py          # background job registry on a daemon event loop
├── storage/                 # Filesystem persistence (the only layer that touches disk, besides tools/)
├── tools/                   # Bash + media loaders (pure stdlib + async)
├── default_style/           # Bundled .md prompts (seeded into ~/.miniouto/style/ on first run)
├── tui/                     # EMPTY placeholder — TUI code lives in cli/tui.py
└── utils/                   # EMPTY placeholder
```

> **Note on empty dirs:** `tui/` and `utils/` are 0-byte placeholders. The TUI is in `cli/tui.py`. There is no `utils/` package — utilities live in `core/`, `storage/`, and `tools/`. Do not add modules to `tui/` or `utils/` without first deciding whether the contents should be moved into the proper package.

## Dependency graph

```
                            ┌──────────────────────────────────────┐
                            │   cli/  (Typer app + Textual TUI)    │
                            │   __init__, chat, provider, style,   │
                            │   skill, tui                         │
                            └──────┬───────────────────────┬───────┘
                                   │                       │
                                   ▼                       ▼
                             ┌──────────┐           ┌──────────────┐
                             │ core/    │           │ storage/     │
                             │ chat,    │◄─────────►│ paths,       │
                             │ events,  │           │ providers,   │
                             │ runtime, │           │ settings,    │
                             │ providers,           │ sessions,    │
                             │ context, │           │ styles,      │
                             │ lma      │           │ skills,      │
                             └────┬─────┘           │ toml_io      │
                                  │                 └──────┬───────┘
                                  ▼                        │
                             ┌──────────┐                  │
                             │ tools/   │                  │
                             │ bash,    │                  │
                             │ media,   │                  │
                             │ registry │                  │
                             └────┬─────┘                  │
                                 │                        │
                                 ▼                        ▼
                         ┌──────────────────────────────────────┐
                         │  paths_runtime.INVOCATION_CWD        │
                         │  coreouto (external)                 │
         ┌──────────────►│  ~/.miniouto/* (filesystem)          │
         │               └──────────────────────────────────────┘
    coreouto ◄────── all runtime/state APIs

    default_style/ (read-only templates) ──► storage/paths.ensure_dirs (seeded on first run)
```

**Layer rules:**

| Layer | Touches the filesystem? | Touches coreouto? | Touches the network? |
|---|---|---|---|
| `cli/` | No (delegates to storage) | No (delegates to core) | No |
| `core/` | No (delegates to storage) | Yes | Yes (`lma` REST client in `context.py` + `lma.py`) |
| `storage/` | Yes — its primary job | No | Yes (`add_from_repo` for `style add`) |
| `tools/` | Yes (each tool mutates files) | No (only `tools/registry.py` does) | No |
| `default_style/` | No (read-only packaged assets) | No | No |

`tools/` is the only layer that touches both the filesystem and coreouto (via `registry.py`). Keep `bash.py` and `media.py` coreouto-free so they stay portable and testable.

## Runtime data flow

```
user input
  │
  ▼
cli/__init__.py:app (Typer)
  │ ── no subcommand ──► tui.run_tui() (Textual ChatTUI)
  │                          │
  │                          ▼
  │                      core.chat.run_chat(opts, sink)
  │
  ├── miniouto chat "..."
  │     └─► cli/chat.chat_cmd
  │             └─► core.chat.run_chat(ChatOptions, sink=ConsoleEventSink())
  │                     ├─► runtime.resolve_runtime_from_settings
  │                     ├─► runtime.build_runtime
  │                     │     ├─► core.providers.build_coreouto_provider (×2: outo + subagent)
  │                     │     ├─► tools.registry.register_all
  │                     │     ├─► storage.styles.parse_style (→ outo body + N SubagentSpec) + skills
  │                     │     ├─► coreouto.register_agent_preset("outo")
  │                     │     ├─► coreouto.register_agent_preset(<name> for each subagent)
  │                     │     ├─► _build_subagent_tool + _wrap_subagent_handler
  │                     │     │     (registered only when ≥1 SubagentSpec;
  │                     │     │      single tool name "call_subagent" taking name + ONE brief)
  │                     │     ├─► coreouto.register_hook(BEFORE_TOOL_CALL, _make_tool_call_logger)
  │                     │     ├─► coreouto.register_hook(ON_ITERATION, make_summarize_hook)
  │                     │     ├─► coreouto.register_hook(BEFORE_LLM_CALL, _make_alarm_hook)
  │                     │     ├─► coreouto.register_hook(ON_ITERATION, _make_iteration_logger)
  │                     │     └─► coreouto.register_hook(AFTER_LLM_CALL, _make_response_logger)
  │                     ├─► storage.sessions.load (if --continue)
  │                     ├─► co.Agent.call_sync(prompt, history)
  │                     │     ├─► tools.bash / .media (via registry)
  │                     │     ├─► call_subagent → core.subagents.submit (BACKGROUND, on the
  │                     │     │    daemon loop) → ack returned immediately; later the job
  │                     │     │    writes ~/.miniouto/subagent-results/<sid>-<ts>.md and its
  │                     │     │    completion alarm is appended as a user message by the
  │                     │     │    BEFORE_LLM_CALL alarm hook
  │                     │     ├─► ON_STREAM_TEXT / ON_STREAM_THINKING → sink.emit_stream_delta
  │                     │     │    (display only; never recorded)
  │                     │     └─► on_tool_call closure (from _make_tool_call_dispatcher,
  │                     │          bridged by _make_tool_call_logger BEFORE_TOOL_CALL hook)
  │                     └─► storage.sessions.append(...)
  │
  ├── miniouto provider providers/models/add ──► core.lma + storage.providers + settings.update
  ├── miniouto provider custom add            ──► storage.providers + settings.update
  ├── miniouto provider [list|remove|default] ──► storage.providers.* + settings.update
  ├── miniouto style    [list|set|add|show]   ──► storage.styles.*   + settings.update
  ├── miniouto skill    [list|show]           ──► storage.skills.*
  └── miniouto status                          ──► read everything, print
```

## Key invariants

These are non-obvious rules that hold throughout the codebase. **Breaking any of these will silently degrade or break the system.**

### 1. Provider registry is rebuilt every turn
`core/runtime.build_runtime` calls `core.providers.clear_coreouto_state()` (which calls `co.clear_providers()`, `co.clear_agent_presets()`, `co.clear_tools()`, `co.clear_hooks()`) at the very start, then re-registers everything. This makes `build_runtime` **idempotent across CLI invocations** (necessary because TUI mode is a long-lived process that re-enters the function many times).

### 2. Style documents are parsed top-level — the tag name IS the subagent name
Each style file is parsed by `storage.styles.parse_style(content) -> tuple[str, list[SubagentSpec]]` with a single sequential scan over the document's top level. `SubagentSpec` is a frozen dataclass with `name: str` (the tag name) and `prompt: str` (the tag body, used as that subagent's system prompt). Rules:

- `<outo>...</outo>` is the outo prompt. If absent, the **whole document** is the outo prompt.
- Each **top-level** `<name>...</name>` pair outside `<outo>` is one named subagent; the tag name IS the name. Tag names match `[A-Za-z][A-Za-z0-9_-]*` (hyphens allowed).
- `outo` is reserved (always the outo prompt).
- A legacy `<subagent>...</subagent>` block is parsed as a subagent named `"subagent"`. Old style files keep working unchanged; their actor labels stay `subagent-<6hex>`.
- **Zero named subagents → no subagent preset, no `call_subagent` tool registered**. An outo-only style does not advertise the delegation surface at all. There is no fallback subagent prompt anymore.
- Parse is **top-level only**: a pair nested inside another tag body is content, not a subagent.
- Duplicate names at the top level raise `ValueError`.

### 3. Two-layer prompt assembly (in order)
The final prompt the outo model sees, top to bottom:
1. A per-call cwd preamble (`core.runtime._with_cwd("outo", ...)`) — informs the model of the user's working directory.
2. All active skills from `~/.agents/skills/` (joined with `\n\n---\n\n`).
3. The `<outo>` section of the active style (or whole-document fallback).

Each named subagent mirrors this with its own tag body and a different cwd preamble.

### 4. Context-window safety
`core/context.py` enforces a **16K-token output floor** by calling `https://lma.blp.sh/model?model-name=...&provider-name=...` (via `core.lma.get_model`):

- **Floor:** `DEFAULT_MAX_OUTPUT_TOKENS = 16384`. Without this, Anthropic's default of 1024 silently truncates long tool calls (e.g. heredoc file writes).
- **No ceiling.** The previous `MAX_OUTPUT_TOKENS_CEILING = 16384` was a defense against the legacy `lcw-api.blp.sh/context-window` endpoint reporting inflated theoretical streaming caps; lma reports accurate per-request non-streaming caps so the clamp is no longer needed.

### 5. `call_subagent` is a background one-brief fire-and-forget tool
`core/runtime.py:_wrap_subagent_handler` validates `(name, task)` and delegates to `core/subagents.py:submit`, returning an **acknowledgement** immediately. The subagent's answer is never in the tool result: it is written to `~/.miniouto/subagent-results/<sid>-<ts>.md` and delivered later as a synthetic user message carrying the completion alarm. One brief per call — several calls in one turn start several concurrent jobs, and there is no fan-out parameter.

The job engine is a module-level registry over a **daemon thread that owns its own event loop**: a job must outlive the turn that spawned it, and every turn runs under `asyncio.run(...)`, which tears down anything still detached. A **spawn handshake** (`_run_job` sets `job.spawned` before touching `agent.call`; `submit` waits up to 2 s) is what makes the cross-turn run safe — coreouto resolves the provider and tools by NAME from the global registries at `call()` entry, and the next turn's `build_runtime` wipes them.

`core/runtime.py:_build_subagent_tool` does NOT use `coreouto.contrib.agent_as_tool` for one reason: the stock helper drops `provider_config` when calling `preset.to_config()`, so subagent file-writing calls inherit the provider's low hard cap. This implementation merges `provider_config` (containing `max_tokens`) and `provider_passthrough` (the resolved reasoning kwargs) into every named subagent's `AgentConfig`. **Registration is conditional**: `build_runtime` only registers `call_subagent` when the active style declares at least one named subagent. An outo-only style does not advertise delegation.

**v1 limitations**: a background job has no `supervised_run` (its watchdog state is per-turn and every `build_runtime` clears it) and no hooks of its own once the turn that started it ends, so there is no stall recovery and no per-actor stream rows. The result file and the completion alarm are the only channels.

### 6. Actor attribution via ContextVars, set inside the background job task
coreouto's `BEFORE_TOOL_CALL` hook has no per-agent context, so `core.runtime` keeps **three** ContextVars — `_SUBAGENT_DEPTH`, `_SUBAGENT_NAME` (the persona name) and `_SUBAGENT_ID` (the 6-hex id) — all set by `core/subagents.py:_enter_context` at the top of the job's own asyncio task (and reset in a `finally`). `core.chat._actor_label()` combines them into `outo` vs `subagent-<6hex>` (legacy `subagent` name) vs `{name}-{6hex}` (`editor-abc123`, `reviewer-def456`); ContextVars are copied per asyncio task, so concurrent jobs each keep their own name + id. The lifecycle observer signature is `(phase, sid, name, text)` and it emits **only** `"start"` — completion is signalled exclusively through the alarm, so a job finishing mid-turn and one finishing between turns behave identically. The `BEFORE_TOOL_CALL` hook for `call_subagent` itself runs in the parent context and never sees the id, which is why the tool-call dispatcher emits nothing for `call_subagent`.

### 7. Sessions are schema v2: `history` (restorable) vs `turns` (display)
`storage/sessions.py` writes `{"version": 2, "history": [...], "turns": [...]}`. `history` = raw coreouto `Message` dumps minus system messages, rewritten in full every turn from `Response.messages` (consistent with summarize-hook compaction) and also snapshotted mid-turn. `turns` = display-only `TurnRecord`s including `LoopEvent` dicts — thinking lives only there (providers never put thinking into history messages). `SessionData.cwd` records the launch directory, stamped by `begin_turn` on the **first** turn only, so a session belongs to the directory it was created in; the TUI session picker and `miniouto status` group by it, and `list_sessions_with_meta()` parses the JSON directly (never through `load()`, which writes). `load()` migrates v1 files and never raises on corrupt content.

### 8. File manipulation is Bash-only
There are no dedicated Write/Edit/Delete tools — `tools/bash.py` covers all file work (`cat`/`grep`/`find`, heredocs/`tee`, `sed -i`, `rm`). The dedicated tools were removed (they were error-prone and the agent reached for Bash anyway). Do not reintroduce them without explicit discussion; to add any other tool, follow `docs/tools.md` § "Adding a new tool".

### 9. Async only where needed
`tools/bash.py` is async (it spawns a subprocess). The media loaders are sync. The TUI uses `asyncio.to_thread(run_chat, opts, sink)` to call the sync `core.chat.run_chat` without blocking the Textual event loop.

### 10. Skill discovery lives outside `~/.miniouto/`
`storage/skills.py` reads from `~/.agents/skills/<name>/SKILL.md` (the Anthropic-style convention), NOT from `~/.miniouto/`. This is intentional: skills are a portable, project-shared concept, not a per-installation setting.

### 11. Stream deltas are display-only — never a record
`EventSink.emit_stream_delta(kind, text)` carries the per-fragment payloads coreouto fires from `ON_STREAM_TEXT` / `ON_STREAM_THINKING`. All four sink implementations define it (hooks are global and there is no `__getattr__` delegation, so a missing method would raise `AttributeError` on the first delta of the first turn). `_RecordingSink` forwards to the wrapped sink and stops — it does not record, and it does not call the persister — so the session file never contains fragments; the whole message is stored once, when it completes. `ConsoleEventSink` is a no-op, keeping CLI output byte-identical. In the TUI the deltas land in one live `StreamRow` per message, coalesced to ~30 Hz. The repetition watchdog is deliberately **not** fed from deltas: they fragment mid-segment, and the whole-message counters already cover the same content.

### 12. Alarms are appended to the live messages, never injected
A finished background job's completion alarm is a synthetic **user** message whose first line is exactly `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]` (`core/subagents.py:ALARM_HEADER`, shared with `core/repetition.py:alarm_text`). The `BEFORE_LLM_CALL` hook `core/runtime.py:_make_alarm_hook` drains the queue and appends one user message to the live list, so the very next provider call already carries it. `Agent.inject_user_message` is deliberately **not** used: its queue is drained at the top of the loop iteration, i.e. *before* this hook, so an alarm queued from there would land a full round-trip later. If the last message is already a user message the text is folded into it, because providers reject two consecutive user messages. Each alarm also becomes one `LoopEvent(kind="alarm")` for the record and the TUI.

### 13. A looping model is force-stopped, not waited out
`core/repetition.py` counts, per actor, the same normalized phrase `REPEAT_PHRASE_THRESHOLD = 10` times in thinking/content and the same tool call `REPEAT_TOOL_THRESHOLD = 30` times (cumulative, exact, offline). A verdict is sticky until consumed. A looping model is *active*, not wedged, so the stall watchdog would never fire for it: `supervised_run` therefore checks repetition **before** the stall test, cancels the attempt, and resumes with the alarm **as the user message** (`start(alarm, history)`) — the stall path's sibling is `start(None, history)`, and the difference is exactly that injected alarm. The budget is its own (`REPEAT_MAX_RESTARTS = 3`, independent of the stall watchdog's `WATCHDOG_MAX_WAKEUPS`); exhausting it raises `LoopRepeatedError`, a sibling of `LoopStalledError` and deliberately not of `LoopCancelledError`. A background job has no supervisor, so its trip is enforced by cancelling the job instead (`cancel_background_loop`) — the job's own completion path then writes the result file and queues exactly one alarm. Counters are reset once per turn.

### 14. What the model loses, the record loses
`LoopEvent.msg_ref` is `str(id(message))` of the live coreouto message an event came from, stamped in one place: `ON_ITERATION`, the only hook that fires after coreouto appends the assistant message. Two sources remove messages — the 400/422 history repair (`_repair_pairing` dedupes duplicate tool results, `_cut_poisoned_turn` excises one poisoned turn) and `sanitize_history` (the stall/repetition restart) — and both report the discarded ids. `EventSink.remove_events` then prunes the recorded events carrying those refs, in place, and the persister sweeps every turn of the session file (`sessions.prune_turn_events`), so what the TUI shows, what the file records, and what the model still has all agree. Summarizer compaction is the deliberate **exclusion**: it rewrites history wholesale, but the display transcript is the human record of what happened, so it never prunes. Additions (a filled-in tool result, an explanation note folded into a user message, an alarm) are never removals and are never reported.

## Domain entities

| Entity | Defined in | Purpose |
|---|---|---|
| **Provider** | `storage/providers.py` | LLM API connection: `name`, `api_format` (openai/openai-response/anthropic/google), `base_url`, `api_key`, `default_model`, `source` (`SOURCE_CUSTOM`/`SOURCE_LMA`), `extra` |
| **Settings** | `storage/settings.py` | Active `provider`, `model` (legacy), `style`, `session`, `theme` |
| **Style** | `storage/styles.py` | Markdown system prompt, parsed into an outo body and zero or more named subagent bodies via `parse_style` (top-level tag-as-name) |
| **Skill** | `storage/skills.py` | YAML-frontmatter markdown, discovered from `~/.agents/skills/`, prepended to every style |
| **Session** | `storage/sessions.py` | Schema-v2 JSON: restorable `history` + display `turns`, keyed by name |
| **SessionData / TurnRecord** | `storage/sessions.py` | `SessionData`: `name`, `history` (coreouto Message dicts), `turns`. `TurnRecord`: `user`, `assistant`, `events` (LoopEvent dicts), `ts` |
| **RuntimeConfig** | `core/runtime.py` | Resolved per-call configuration (provider, model, style, session) |
| **ChatOptions** | `core/chat.py` | Raw CLI flag bag for a single chat turn |
| **LoopEvent** | `core/events.py` | Single trace event (actor=`outo` / `{name}-{sid}` / `provider`, kind, text, optional `subagent_id` and `subagent_name`) emitted via the `EventSink` |
| **EventSink** | `core/events.py` | Protocol implemented by `NullSink` (no-op) and `ConsoleEventSink` (CLI rendering) |
| **ToolCallArgsError** | `core/chat.py` | Local exception for malformed LLM tool arguments |

Relationships:

```
ChatOptions (CLI flags)
  └─> resolve_runtime_from_settings (runtime.py)
        ├─ reads Settings (storage/settings.py) + Provider (storage/providers.py)
        └─> RuntimeConfig
              └─> build_runtime (runtime.py)
                    ├─ build_coreouto_provider (providers.py)
                    ├─ tool_registry.register_all()
                    ├─ parse_style + _load_active_skills
                    │    → registers "outo" preset + one preset per SubagentSpec
                    ├─ builds call_subagent tool
                    │    (registered only when ≥1 SubagentSpec; preserves max_tokens)
                     ├─ installs the sink hooks, the watchdog trackers, and the
                     │    unconditional BEFORE_LLM_CALL alarm + ON_PROVIDER_ERROR repair hooks
                     └─ returns co.Agent(outo_config)
        └─> run_chat (chat.py)
               ├─ loads history (if --continue)
               ├─ builds sink dispatchers (tool/result/response/thinking/iteration/
               │    provider-error/stream/alarm) + installs the subagent observer
               ├─ supervised agent.call(prompt, history=core_msgs) (stall + repetition watchdogs)
               ├─ on exception: _dump_failure_diagnostics
               └─ incremental persistence: begin_turn before the loop,
                  event + sanitized-history snapshots during it, then
                  finish_turn stamps the running turn done/interrupted
                  (success also rewrites history from Response.messages
                  minus system)
```

## External dependencies

| Package | Version | Role |
|---|---|---|
| `coreouto[all]` | `>=0.11.3` | Agent loop, providers, tool registry, hooks (streaming hooks, contrib watchdog trackers, literal-typed schemas) |
| `typer` | `>=0.12.0` | CLI framework |
| `rich` | `>=13.7.0` | Terminal output, tables, markdown |
| `textual` | `>=0.80.0` | TUI framework |
| `pydantic` | `>=2.0` | Data models (used by coreouto) |
| `httpx` | `>=0.27.0` | HTTP client (`lma` REST client + style repo fetcher) |
| `tomli-w` | `>=1.0.0` | TOML serializer (paired with stdlib `tomllib`) |

`coreouto` is the only non-trivial runtime dependency; everything else is a thin UI/storage layer.

## Build & entry points

- **Build backend:** `hatchling`
- **Wheel packages:** `["src/miniouto"]` (src-layout)
- **Console script:** `miniouto = "miniouto.cli:app"` (registered in `pyproject.toml`)
- **Python:** `>=3.10`

## Known sharp edges (don't refactor without checking)

1. **Test coverage is thin.** `tests/` covers `parse_style` and a growing set of runtime/storage cases, but `tools/bash.py` and `core/context.py:make_summarize_hook` are still untested.
2. **`tui/` and `utils/` are empty.** They look like package directories but contain no code.
3. **`storage/skills.py` is not in `storage/__init__.py`'s `__all__`** — it's imported directly via `from ..storage import skills as skill_store`. Don't add it to `__all__` without auditing the import sites first.
4. **`tools/registry.py:_register_if_missing` silently discards the `schema` parameter** — only the handler's type hints and the `description` string reach the model. The `_xxx_schema()` dicts are dead code.
5. **`core/runtime.py` can register `ON_ITERATION` twice.** `summarize_hook` (always) and `_make_iteration_logger(on_iteration)` (when `on_iteration` is supplied, which `chat.run_chat` always does). Both fire on every iteration.
