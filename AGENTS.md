# AGENTS.md

> **Read this first.** This file is the single source of truth for the miniouto project. It tells you what every file does, why it exists, what depends on it, and what to watch out for when modifying it. The detailed docs live in [`docs/`](./docs/) — this file orients you to them.

---

## What this project is

**miniouto** is a minimal, file-driven CLI agent harness built on top of [`coreouto`](https://github.com/llaa33219/coreouto). It exposes a single `miniouto` command that can:

- Run a **one-shot chat turn** (`miniouto chat "..."`).
- Launch a **Textual TUI** (`miniouto` with no args).
- Manage **providers**, **styles**, and **skills** via CLI subcommands.
- Persist **session history** as plain JSON files.

Three principles from `README.md`: **Minimalism** (no bloat — extend with styles), **Automation-friendly** (full CLI, TUI optional), **Fluidity** (adapts to any environment).

**Version**: `0.1.1` (alpha). **Python**: `>=3.10`. **Build**: `hatchling`. **Console script**: `miniouto = "miniouto.cli:app"`.

---

## The 60-second mental model

```
~/.miniouto/                     ← user's storage (override via $MINIOUTO_HOME)
├── providers.toml               ← LLM API connections (one top-level table per provider)
├── settings.toml                ← active provider / model / style / session / theme
├── style/*.md                   ← system prompts (outo + subagent halves)
├── sessions/*.json              ← conversation history
└── logs/                        ← reserved (currently unused)

~/.agents/skills/                ← skills (NOT under ~/.miniouto/)
└── <name>/SKILL.md              ← YAML frontmatter + markdown body

src/miniouto/
├── cli/      ← Typer commands + Textual TUI
├── core/     ← chat loop, runtime assembly, subagent dispatch, event sinks
├── storage/  ← the only layer that touches disk (apart from tools/)
├── tools/    ← Write / Edit / Delete / Bash (only bash is async)
├── default_style/  ← 6 bundled .md templates, force-refreshed on every run
└── __init__.py, paths_runtime.py
```

**Data flow per chat turn:**

```
CLI flag bag → ChatOptions (core/chat.py)
            → resolve_runtime_from_settings (core/runtime.py)
            → RuntimeConfig
            → build_runtime (core/runtime.py)
                → coreouto provider registry + tool registry + up to 4 hooks
                    (BEFORE_TOOL_CALL, ON_ITERATION×2, AFTER_LLM_CALL)
                → co.Agent(outo_config)
            → run_chat(opts, sink) → agent.call_sync(prompt, history=...)
                → Write/Edit/Delete/Bash/Image/Video/Audio (via tools/registry.py)
                → call_subagent (delegates to subagent preset)
            → persist user + assistant MessageRecord
```

---

## Where to find what

| You want to… | Go to | Doc |
|---|---|---|
| Understand the architecture | `src/miniouto/` (tree) | [`docs/architecture.md`](./docs/architecture.md) |
| Add/modify a CLI command | `src/miniouto/cli/` | [`docs/cli.md`](./docs/cli.md) |
| Change storage layout or schemas | `src/miniouto/storage/` | [`docs/storage.md`](./docs/storage.md) |
| Modify the chat loop or runtime | `src/miniouto/core/` | [`docs/core.md`](./docs/core.md) |
| Add/modify a tool | `src/miniouto/tools/` | [`docs/tools.md`](./docs/tools.md) |
| Edit/create a style prompt | `~/.miniouto/style/*.md` or `src/miniouto/default_style/` | [`docs/styles.md`](./docs/styles.md) |
| Add/modify a skill | `~/.agents/skills/<name>/SKILL.md` | [`docs/skills.md`](./docs/skills.md) |
| Browse lma providers/models or change SDK mapping | `core/lma.py`, `core/providers.py:sdk_to_format`, `cli/provider.py` | [`docs/lma.md`](./docs/lma.md) |
| Set up dev environment / release | `pyproject.toml`, `uv.lock` | [`docs/development.md`](./docs/development.md) |

---

## The 13 invariants (do NOT break these)

These rules hold throughout the codebase. Breaking any of them silently degrades or breaks the system. **If your change seems to require breaking one, stop and ask.**

### 1. Provider registry is rebuilt every turn
`core/runtime.py:build_runtime` calls `core.providers.clear_coreouto_state()` at the very start, then re-registers providers, presets, tools, hooks. This makes `build_runtime` **idempotent across CLI invocations** — necessary because TUI mode is a long-lived process that re-enters the function many times. **Do not remove `clear_coreouto_state()`.**

### 2. Style documents are split into two halves
`storage/styles.py:split_style` parses each `.md` into `(outo_part, subagent_part)` using `<outo>...</outo>` and `<subagent>...</subagent>` tags:

- `<outo>` is required (or the whole document is used).
- `<subagent>` is **optional** — if absent, `core/runtime.py:_fallback_style("subagent")` provides a hardcoded minimal prompt.

If you change the tag format, update `split_style`, `default_style/*.md`, and `docs/styles.md`.

### 3. Two-layer prompt assembly
The final prompt the outo model sees, top to bottom:
1. **Per-call cwd preamble** (`_with_cwd("outo", …)`) — the user's working directory at miniouto invocation.
2. **All active skills** from `~/.agents/skills/` joined by `\n\n---\n\n`.
3. **The `<outo>` section** of the active style (or whole-document fallback).

Subagent mirrors this with `<subagent>` content and a different cwd preamble. The cwd preamble is regenerated on every call (not persisted).

### 4. Context-window safety
`core/context.py` enforces a **16K-token output floor** by calling `https://lma.blp.sh/model?model-name=...&provider-name=...` (via `core.lma.get_model`):

- **Floor**: `DEFAULT_MAX_OUTPUT_TOKENS = 16384`. Without this, Anthropic's default of 1024 silently truncates Write tool calls.
- There is **no ceiling**. The previous `MAX_OUTPUT_TOKENS_CEILING = 16384` was a defense against the legacy `lcw-api.blp.sh/context-window` endpoint reporting inflated theoretical streaming caps; lma reports accurate per-request non-streaming caps so the clamp is no longer needed.

If you touch `get_max_output_tokens`, you must preserve the floor. Always thread the `provider_name` argument through so the lma lookup is scoped to the active provider (otherwise lma returns matches across every provider and we only see the first hit).

There is **one** deliberate precedence tier above lma: a per-provider `max_output_tokens` override (field on the `Provider` dataclass, set via the TUI custom-model editor). `_provider_caps_override` consults `provider_store` on every call (no caching — the TUI is long-lived and edits these mid-session). The override wins over lma. The CLI `chat --max-tokens` flag still wins over the override. Precedence: `--max-tokens` > provider override > lma `max_output_tokens` > lma `context_window` > `DEFAULT_MAX_OUTPUT_TOKENS`. The same override path exists for `max_context_window` via `get_context_window`.

### 5. Subagent is a re-implemented `agent_as_tool`
`core/runtime.py:_build_subagent_tool` does NOT use `coreouto.contrib.agent_as_tool`. The stock helper drops `provider_config` when calling `preset.to_config()` — that means subagent `Write` calls inherit the provider's low hard cap (1024 for Anthropic → silent truncation). This implementation explicitly merges `provider_config` (containing `max_tokens`) into the subagent's `AgentConfig`.

**Do not "simplify" this back to `coreouto.contrib.agent_as_tool`.**

### 6. `BEFORE_TOOL_CALL` is global — depth tracked via ContextVar
coreouto's `BEFORE_TOOL_CALL` hook has no per-agent context. `core/runtime.py:_SUBAGENT_DEPTH: ContextVar[int]` is the only signal of "are we currently inside a subagent?" The `on_tool_call` closure built by `core/chat.py:_make_tool_call_dispatcher` reads it (via `current_subagent_depth()`) to label each tool trace with actor `outo` vs `subagent`. The bridge from coreouto's hook to that closure is `core/runtime.py:_make_tool_call_logger`. The var is bumped only inside `_wrap_subagent_handler`.

If you add a new async tool that itself calls subagents, route it through `_wrap_subagent_handler` or the depth tracking will be wrong.

### 7. Edit tool: 6 enforced rules
`tools/edit.py` enforces six rules:
1. **Exact match** priority (then fuzzy fallback).
2. **Uniqueness** — multiple matches raise `EditError` with all line numbers.
3. **All edits located against the original content** (no chaining).
4. **No overlaps** — sorted-span check.
5. **Reject empty / no-op edits** (empty `oldText`, identical `oldText`/`newText`).
6. **Errors carry line numbers + how-to-fix** hints.

The fuzzy fallback normalizes smart quotes, dashes, NBSP, BOM, CRLF, zero-width chars, and trailing whitespace. These rules are testable — write tests before refactoring.

### 8. `Write` refuses overwrite
`tools/write.py` raises `WriteError` if the target file exists. The intended workflow is `Write` to create, `Edit` to modify. **Do not add an `--overwrite` flag without explicit discussion** — accidental clobbering is the whole point of the refusal.

### 9. Async only where needed
`tools/bash.py` is async (it spawns a subprocess). The other three tools are sync. The TUI uses `asyncio.to_thread(run_chat, opts, sink)` to call the sync `core.chat.run_chat` without blocking the Textual event loop. **Don't make the other tools async** — they don't need to be, and it complicates the TUI dispatch.

### 10. Skill discovery lives outside `~/.miniouto/`
`storage/skills.py` reads from `~/.agents/skills/<name>/SKILL.md` (the Anthropic convention), NOT from `~/.miniouto/`. Skills are portable content, not per-installation config. **Do not move them into `~/.miniouto/`.**

### 11. TUI model editor manages `Provider.default_model`, not `Settings.model`
In the TUI, picking or typing a model always writes to `provider.default_model` (via `dataclasses.replace(p, default_model=...)` + `provider_store.upsert`) and clears any prior `settings.model`. The model chip displays `provider.default_model` only — `settings.model` is reserved for the `chat --model` CLI flag. Legacy `settings.model` values from older sessions keep working at the runtime layer (`resolve_runtime_from_settings` still treats them as priority 2) but are invisible in the TUI. **Do not reintroduce a `settings.model`-as-TUI-override path.**

### 12. lma-backed vs custom providers
`storage/providers.py:Provider` carries a `source: str` field, one of `SOURCE_CUSTOM` (default) or `SOURCE_LMA`. lma-backed (catalog) providers are added via `miniouto provider add …` or the TUI `+ add from catalog…` wizard; custom providers come from `provider custom add` or the TUI `+ add custom…` wizard. The TUI model picker dispatches on `source`:

- `source == "lma"` → `cli/tui.py:_catalog_model_picker_flow` (fetches `/model-list` and shows a `SelectionModal`).
- `source == "custom"` → `cli/tui.py:_open_custom_model_editor` (free-text `TextInputModal`).

The CLI command `miniouto provider providers` filters its "Addable?" column through `core.providers.sdk_to_format(sdk, api)`. Providers whose SDK cannot be hosted by any of the four coreouto builtins (openai / openai-response / anthropic / google) return `(None, None)` and are skipped by both the CLI and the TUI wizard.

Note: the source string remains the literal `"lma"` (it predates the "catalog" UI rename) — `SOURCE_LMA = "lma"`. The TUI and CLI surface call these "catalog" providers, but the underlying field value is still `"lma"`.

### 13. lma cache lives in `core.lma._CACHE`
`core/lma.py` mirrors lma's 10-minute server-side TTL with a module-level dict. Cache keys are explicit (`"providers"`, `f"models:{provider.lower()}"`, `f"model:{provider.lower()}:{model.lower()}"` — both segments are lowercased); a cached `None` payload is meaningful (means "lma returned 404"). `core.lma.clear_cache()` exists for tests / manual refresh. **Do not cache anything other than `None` and successful payloads** — a transient transport error must not pollute the cache for 10 minutes.

---

## File-by-file cheat sheet

### Top-level

| File | Purpose |
|---|---|
| `pyproject.toml` | Project metadata, deps, console script, hatchling config, ruff config |
| `uv.lock` | Pinned dependency lockfile |
| `README.md` | User-facing quickstart (install, commands, storage layout) |
| `logo.svg` | Project logo |
| `.gitignore` | Python ignores + `.ruff_cache/`, `miniouto.toml` |
| `.venv/` | uv-managed virtualenv |
| `.ruff_cache/` | Ruff cache |

### `src/miniouto/`

| File | Purpose |
|---|---|
| `__init__.py` | `__version__ = "0.1.1"` |
| `paths_runtime.py` | `INVOCATION_CWD: Path` (captured cwd at import, used by every tool to absolutize relative paths) |
| `cli/__init__.py` | Typer `app`, root callback (TUI fallback), `status` command |
| `cli/chat.py` | `chat_cmd` — one-shot chat command |
| `cli/provider.py` | `provider providers/models/add` (catalog browse + add) + `provider custom add` + `provider list/remove/default` |
| `cli/style.py` | `style list/set/add/update/show` |
| `cli/skill.py` | `skill list/show` (read-only) |
| `cli/tui.py` | `ChatTUI` (Textual App), `run_tui()`, `tui_summary()`; provider catalog/custom add wizards + model picker |
| `core/__init__.py` | Re-exports `chat`, `events`, `lma`, `providers`, `runtime` (NOT `context`) |
| `core/chat.py` | `ChatOptions`, `run_chat(opts, sink=None)`, `ToolCallArgsError`, failure diagnostics, sink dispatchers (`_make_tool_call_dispatcher`, `_make_response_dispatcher`, `_make_iteration_dispatcher`) |
| `core/context.py` | lma `/model` fetcher (via `core.lma.get_model`), `make_summarize_hook` |
| `core/events.py` | `LoopEvent`, `EventSink` protocol, `NullSink`, `ConsoleEventSink` (CLI spinner + loop-event rendering) |
| `core/lma.py` | `lma.blp.sh` REST client + `slugify` + `find_provider`; in-process 10-min cache |
| `core/providers.py` | `SUPPORTED_FORMATS`, `sdk_to_format`, `add_provider_from_lma`, `build_coreouto_provider`, `clear_coreouto_state` |
| `core/runtime.py` | `RuntimeConfig`, `ChatOverrides`, `build_runtime`, subagent tool, hooks |
| `storage/__init__.py` | Re-exports submodules (NOT `skills`) |
| `storage/paths.py` | Path constants (incl. `STYLE_REPOS_FILE`) + `ensure_dirs()` (force-refreshes bundled styles) |
| `storage/providers.py` | `Provider` dataclass (with `source: SOURCE_CUSTOM \| SOURCE_LMA`) + `SOURCE_*`/`VALID_SOURCES` constants + TOML CRUD |
| `storage/sessions.py` | `MessageRecord` + JSON CRUD |
| `storage/settings.py` | `Settings` (`provider`, `model`, `style`, `session`, `theme`) + TOML CRUD |
| `storage/skills.py` | `Skill` discovery from `~/.agents/skills/` (NOT in `__all__`) |
| `storage/styles.py` | Style CRUD + `add_from_repo` (records repo in `style_repos.toml`) + `record_repo`/`list_repos` + `split_style` + `builtin_default` |
| `storage/toml_io.py` | `tomllib` + `tomli_w` wrapper |
| `tools/__init__.py` | Re-exports + `normalize_for_matching` |
| `tools/_normalize.py` | smart-quote/dash/NBSP/zero-width normalization |
| `tools/bash.py` | `async bash(command, *, timeout_seconds, cwd, env)` |
| `tools/delete.py` | `delete(file_path)` |
| `tools/edit.py` | `edit(file_path, edits)` — batch search/replace |
| `tools/media.py` | `load_image/load_video/load_audio(file_path)` → `LoadedMedia` (pure stdlib; `registry.py` wraps results into `co.ImageBlock`/`VideoBlock`/`AudioBlock`) |
| `tools/write.py` | `write(file_path, content)` — refuses overwrite |
| `tools/registry.py` | `register_all()` — wires Write/Edit/Delete/Bash/Image/Video/Audio into coreouto |
| `default_style/default.md` | Minimal fallback style |
| `default_style/claude.md` | Claude Code-style (~14 KB) |
| `default_style/codex.md` | OpenAI Codex CLI-style (~16 KB) |
| `default_style/opencode.md` | OpenCode-style (~9 KB) |
| `default_style/oh-my-opencode.md` | "Sisyphus" orchestrator (~11 KB) |
| `default_style/codebuff.md` | "Buffy" orchestrator (~10 KB) |
| `tui/` | **EMPTY placeholder** — TUI code lives in `cli/tui.py` |
| `utils/` | **EMPTY placeholder** — no code anywhere |

### `docs/` (this directory's documentation)

| File | Covers |
|---|---|
| `docs/README.md` | Index of all docs |
| `docs/architecture.md` | High-level architecture & data flow |
| `docs/cli.md` | All CLI commands reference |
| `docs/storage.md` | Filesystem layout & persistence layer |
| `docs/core.md` | Chat loop, runtime, providers, context |
| `docs/tools.md` | Write/Edit/Delete/Bash tool internals |
| `docs/styles.md` | Style document system & bundled templates |
| `docs/skills.md` | Skills system from `~/.agents/skills/` |
| `docs/lma.md` | lma (llm-model-api) integration: provider/model discovery, context caps, CLI + TUI flows |
| `docs/development.md` | Dev setup, build, lint, contributing notes |

---

## Common modifications (quick recipes)

### "Add a new CLI subcommand"
1. Create `src/miniouto/cli/<name>.py` with `app = typer.Typer(...)`.
2. Add `app.add_typer(<name>_module.app, name="<name>")` in `cli/__init__.py`.
3. Document in `docs/cli.md`.

### "Add a new tool"
See `docs/tools.md` § "Adding a new tool". TL;DR:
1. `src/miniouto/tools/<name>.py` with the function (pure stdlib — no coreouto import; return a plain data structure if multimodal, like `media.py`'s `LoadedMedia`).
2. `tools/registry.py`: schema, description, handler, `_register_if_missing` call. For multimodal tools the handler returns `list[co.ContentBlock]` — see the `Image`/`Video`/`Audio` handlers.
3. `core/runtime.py`: add name to `ALL_TOOLS` (visible to both presets) or a new list.
4. `core/chat.py`: add the name to `_LOGGABLE_TOOL_NAMES`, the tool-name set in `_make_tool_call_dispatcher`, and a branch in `_short_arg_summary` so loop events + failure diagnostics render the tool.
5. Update `default_style/*.md` if the tool's name or behavior should be documented to the model.
6. If the tool returns multimodal content, note that provider support varies (OpenAI Chat Completions rejects all multimodal blocks; OpenAI Responses API rejects video/audio). **Do not** put provider names in the description or style prompts — the agent cannot introspect its provider, so such hints are unactionable. Let provider rejections surface as `ValueError` at call time. Document the matrix in `docs/tools.md` for human operators.

### "Add a new bundled style"
1. Create `src/miniouto/default_style/<name>.md` (use the `<outo>` / `<subagent>` structure).
2. It auto-seeds into `~/.miniouto/style/` and is force-refreshed on every `ensure_dirs()` call (overwrites any same-name installed file when content differs). To let users customize it, copy to a new name rather than editing the bundled one.
3. Document in `docs/styles.md`.

### "Add a new provider format"
1. `core/providers.py`: add to `SUPPORTED_FORMATS`, add an `_instantiate` branch.
2. `cli/provider.py`: update `FORMAT_HELP`.
3. If the new format is reachable via an lma SDK, also extend `_SDK_TO_FORMAT` in `core/providers.py` so the TUI "add from catalog…" wizard and the `provider providers` CLI both surface it as addable.
4. Document in `docs/cli.md`, `docs/core.md`, and `docs/lma.md`.

### "Change the storage root"
Only one place: `src/miniouto/storage/paths.py`. Update the `ROOT` constant.

### "Change the active tool set"
`src/miniouto/core/runtime.py:ALL_TOOLS`. Both presets share this list (both `register_agent_preset("outo", tools=ALL_TOOLS, …)` and `register_agent_preset("subagent", tools=ALL_TOOLS, …)` reference it). If you need asymmetric visibility, create separate lists and edit the `tools=` argument in each `register_agent_preset` call. (Do **not** confuse this with `_resolve_both_styles`, which only resolves the style *prompts*, not the tool lists.)

### "Add tests"
The test directory doesn't exist yet. Suggested setup in `docs/development.md`. Start with `tools/edit.py` — highest-value, easiest to break with refactors.

---

## Things that look like bugs but aren't (and things that ARE bugs)

### NOT bugs (intentional)

- **`tui/` and `utils/` are empty** — placeholder directories from an older layout. The TUI code is in `cli/tui.py`. Don't add code to those empty dirs without first deciding the right home for it.
- **`core/context.py` is not in `core/__init__.py`'s `__all__`** — it's an implementation detail of `runtime.build_runtime`.
- **`storage/skills.py` is not in `storage/__init__.py`'s `__all__`** — it's imported directly as `skill_store`.
- **`Write` refuses to overwrite** — by design. The agent should use `Edit` for modifications.
- **`continue_loop` is referenced in every bundled style** but **not registered** in `tools/registry.py`. Models improvise. If you want this to actually work, register a no-op tool and add it to `ALL_TOOLS`.

### Bugs / cleanup opportunities

1. **No tests directory exists.** The codebase has zero automated test coverage. `tools/edit.py` and `core/context.py:make_summarize_hook` are the highest-value test targets.
2. **Four of the six bundled styles** (`claude.md`, `codex.md`, `opencode.md`, `oh-my-opencode.md`) describe a `claude.md` / `codex.md` / `oh-my-opencode.md` / `opencode.md` CWD memory file. **No such loader exists in miniouto.** Either implement it (in `core/runtime.py:_load_active_skills` or a sibling) or edit the styles to remove the misleading references.
3. **`tools/registry.py:_register_if_missing` accepts but silently discards the `schema` parameter** — the `_xxx_schema()` dicts are computed at registration time but never passed to `coreouto.register_tool`. Only the handler's Python type hints and the `description` string reach the model. The schema dicts are effectively dead code.

---

## Dependency reference

| Package | Min version | Role |
|---|---|---|
| `coreouto[all]` | 0.4.2 | Agent loop, providers, tool registry, hooks (the runtime) |
| `typer` | 0.12.0 | CLI framework |
| `rich` | 13.7.0 | Terminal output, tables, markdown |
| `textual` | 0.80.0 | TUI framework |
| `pydantic` | 2.0 | Data models (used by coreouto) |
| `httpx` | 0.27.0 | HTTP client (`lma` REST client, style repo fetcher) |
| `tomli-w` | 1.0.0 | TOML serializer (paired with stdlib `tomllib`) |

The `[all]` extra on `coreouto` pulls in all four provider SDKs. Anything beyond these seven packages should be discussed before adding.

---

## When in doubt

1. **Search the docs first.** `docs/README.md` is the index. Use `grep` on `docs/*.md` for keywords.
2. **Read the relevant module's `__init__.py`** — it usually lists what the package exports.
3. **Look at the bundled styles** for examples of how the model sees the system.
4. **Check `docs/development.md` "Known sharp edges"** before doing anything risky.
5. **If you're about to delete code, run `git blame` first** — the comments often explain *why* it exists.
