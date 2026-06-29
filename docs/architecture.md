# Architecture

## Purpose

`miniouto` is a **thin orchestration layer** on top of the [`coreouto`](https://github.com/llaa33219/coreouto) agent library. It provides:

1. A CLI (`miniouto`) and an optional Textual TUI for interactive use.
2. File-driven configuration (TOML for providers/settings, Markdown for styles, JSON for sessions).
3. Bundled agent "style" templates (six personas, including orchestrators).
4. A minimal tool surface (Write, Edit, Delete, Bash, `call_subagent`).
5. Persistence of session history.
6. Per-turn diagnostic output to stderr.

The harness is **stateless across invocations except for what's persisted to `~/.miniouto/`**. No background daemons, no server. Every CLI run is a fresh Python process (except TUI mode, which is one long-lived process).

## Three principles

From `README.md`:

1. **Minimalism** — No bloat. Extend with styles.
2. **Automation-friendly** — Full CLI; TUI is optional.
3. **Fluidity** — Adapts to any environment.

## Package layout

```
src/miniouto/
├── __init__.py              # __version__ = "0.1.0"
├── paths_runtime.py         # INVOCATION_CWD: Path  (captured cwd at import)
├── cli/                     # Typer commands + Textual TUI
├── core/                    # Chat loop + runtime assembly + event sinks
│   ├── __init__.py
│   ├── chat.py
│   ├── context.py
│   ├── events.py            # LoopEvent, EventSink protocol, NullSink, ConsoleEventSink
│   ├── lma.py
│   ├── providers.py
│   └── runtime.py
├── storage/                 # Filesystem persistence (the only layer that touches disk, besides tools/)
├── tools/                   # Write/Edit/Delete/Bash (pure stdlib + async)
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
                            │ write,   │                  │
                            │ edit,    │                  │
                            │ delete,  │                  │
                            │ bash,    │                  │
                            │ registry │                  │
                            │ _normalize                  │
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

`tools/` is the only layer that touches both the filesystem and coreouto (via `registry.py`). Keep `bash.py`, `write.py`, `edit.py`, `delete.py` coreouto-free so they stay portable and testable.

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
  │                     │     ├─► storage.styles.split_style + skills
  │                     │     ├─► coreouto.register_agent_preset("outo")
  │                     │     ├─► coreouto.register_agent_preset("subagent")
  │                     │     ├─► _build_subagent_tool + _wrap_subagent_handler
  │                     │     ├─► coreouto.register_hook(BEFORE_TOOL_CALL, _make_tool_call_logger)
  │                     │     ├─► coreouto.register_hook(ON_ITERATION, make_summarize_hook)
  │                     │     ├─► coreouto.register_hook(ON_ITERATION, _make_iteration_logger)
  │                     │     └─► coreouto.register_hook(AFTER_LLM_CALL, _make_response_logger)
  │                     ├─► storage.sessions.load (if --continue)
  │                     ├─► co.Agent.call_sync(prompt, history)
  │                     │     ├─► tools.bash / .write / .edit / .delete (via registry)
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

### 2. Style documents are split into two halves
Each style file is parsed by `storage.styles.split_style()` into a tuple `(outo_part, subagent_part)` using the tags `<outo>...</outo>` and `<subagent>...</subagent>`:

- `<outo>...</outo>` is required (or the whole document is treated as the outo prompt).
- `<subagent>...</subagent>` is **optional** — if absent, the subagent gets a hardcoded fallback prompt (see `core.runtime._fallback_style("subagent")`).

Both halves then have active skills prepended, and a cwd preamble prepended on top of that.

### 3. Two-layer prompt assembly (in order)
The final prompt the outo model sees, top to bottom:
1. A per-call cwd preamble (`core.runtime._with_cwd("outo", ...)`) — informs the model of the user's working directory.
2. All active skills from `~/.agents/skills/` (joined with `\n\n---\n\n`).
3. The `<outo>` section of the active style (or whole-document fallback).

The subagent prompt mirrors this with `<subagent>` content and a different cwd preamble.

### 4. Context-window safety
`core/context.py` enforces a **16K-token output cap** by calling `https://lma.blp.sh/model?model-name=...&provider-name=...` (via `core.lma.get_model`) and clamping the result:

- **Floor:** `DEFAULT_MAX_OUTPUT_TOKENS = 16384`. Without this, Anthropic's default of 1024 silently truncates Write tool calls.
- **Ceiling:** `MAX_OUTPUT_TOKENS_CEILING = 16384`. Some lma entries report theoretical streaming caps (e.g. 512K) that the non-streaming API rejects.

### 5. Subagent is a re-implemented `agent_as_tool`
`core.runtime._build_subagent_tool` does NOT use `coreouto.contrib.agent_as_tool`. The stock helper drops `provider_config` when calling `preset.to_config()`, which means subagent `Write` calls inherit the provider's low hard cap. This implementation explicitly merges `provider_config` (containing `max_tokens`) into the subagent's `AgentConfig`.

### 6. `BEFORE_TOOL_CALL` is global — depth tracked via ContextVar
coreouto's `BEFORE_TOOL_CALL` hook has no per-agent context, so `core.runtime._SUBAGENT_DEPTH: ContextVar[int]` is the only signal of "are we currently inside a subagent?" The `on_tool_call` closure built by `core.chat._make_tool_call_dispatcher` reads it (via `current_subagent_depth()`) to label each tool trace with actor `outo` vs `subagent`. The bridge from coreouto's hook to that closure is `core.runtime._make_tool_call_logger`. The var is bumped only inside `_wrap_subagent_handler`.

### 7. Edit tool: 6 rules
`tools/edit.py` enforces six rules:
1. **Exact match** priority (then fuzzy fallback).
2. **Uniqueness** — multiple matches raise `EditError` with all line numbers.
3. **All edits located against the original content** (no chaining).
4. **No overlaps** — sorted-span check.
5. **Reject empty / no-op edits** (empty `oldText`, identical `oldText`/`newText`).
6. **Errors carry line numbers + how-to-fix** hints.

The fuzzy fallback normalizes smart quotes, dashes, NBSP, BOM, CRLF, zero-width chars, and trailing whitespace before comparing.

### 8. `Write` refuses overwrite
`tools/write.py` is **non-idempotent** by design: it raises `WriteError` if the target file exists. The intended workflow is `Write` to create, `Edit` to modify. This prevents accidental clobbering.

### 9. Async only where needed
`tools/bash.py` is async (it spawns a subprocess). The other three tools are sync. The TUI uses `asyncio.to_thread(run_chat, opts, sink)` to call the sync `core.chat.run_chat` without blocking the Textual event loop.

### 10. Skill discovery lives outside `~/.miniouto/`
`storage/skills.py` reads from `~/.agents/skills/<name>/SKILL.md` (the Anthropic-style convention), NOT from `~/.miniouto/`. This is intentional: skills are a portable, project-shared concept, not a per-installation setting.

## Domain entities

| Entity | Defined in | Purpose |
|---|---|---|
| **Provider** | `storage/providers.py` | LLM API connection: `name`, `api_format` (openai/openai-response/anthropic/google), `base_url`, `api_key`, `default_model`, `source` (`SOURCE_CUSTOM`/`SOURCE_LMA`), `extra` |
| **Settings** | `storage/settings.py` | Active `provider`, `model` (legacy), `style`, `session`, `theme` |
| **Style** | `storage/styles.py` | Markdown system prompt, optionally split into outo + subagent sections via `<subagent>…</subagent>` tags |
| **Skill** | `storage/skills.py` | YAML-frontmatter markdown, discovered from `~/.agents/skills/`, prepended to every style |
| **Session** | `storage/sessions.py` | JSON conversation history keyed by name |
| **MessageRecord** | `storage/sessions.py` | Single message: `role`, `content`, `tool_calls`, `tool_call_id`, `name`, `ts` |
| **RuntimeConfig** | `core/runtime.py` | Resolved per-call configuration (provider, model, style, session) |
| **ChatOptions** | `core/chat.py` | Raw CLI flag bag for a single chat turn |
| **LoopEvent** | `core/events.py` | Single trace event (actor=`outo`/`subagent`, kind, text) emitted via the `EventSink` |
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
                    ├─ _resolve_both_styles + _load_active_skills
                    ├─ registers "outo" + "subagent" presets
                    ├─ builds call_subagent tool (preserves max_tokens)
                    └─ installs up to 4 hooks (BEFORE_TOOL_CALL, ON_ITERATION ×2, AFTER_LLM_CALL)
                    └─ returns co.Agent(outo_config)
        └─> run_chat (chat.py)
              ├─ loads history (if --continue)
              ├─ builds sink dispatchers from sink (tool/response/iteration)
              ├─ calls agent.call_sync(prompt, history=core_msgs)
              ├─ on exception: _dump_failure_diagnostics
              └─ appends user + assistant MessageRecord to session
```

## External dependencies

| Package | Version | Role |
|---|---|---|
| `coreouto[all]` | `>=0.4.2` | Agent loop, providers, tool registry, hooks |
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

1. **No tests directory exists.** There are zero tests at the time of writing.
2. **Four of the six bundled styles** describe a `claude.md` / `codex.md` / `oh-my-opencode.md` / `opencode.md` CWD memory file. **No such loader exists in miniouto.** Either implement it or remove the misleading references.
3. **`tui/` and `utils/` are empty.** They look like package directories but contain no code.
4. **`storage/skills.py` is not in `storage/__init__.py`'s `__all__`** — it's imported directly via `from ..storage import skills as skill_store`. Don't add it to `__all__` without auditing the import sites first.
5. **`tools/registry.py:_register_if_missing` silently discards the `schema` parameter** — only the handler's type hints and the `description` string reach the model. The `_xxx_schema()` dicts are dead code.
6. **`core/runtime.py` can register `ON_ITERATION` twice.** `summarize_hook` (always) and `_make_iteration_logger(on_iteration)` (when `on_iteration` is supplied, which `chat.run_chat` always does). Both fire on every iteration.
