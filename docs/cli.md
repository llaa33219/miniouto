# CLI Reference

`miniouto` is built on [Typer](https://typer.tiangolo.com/). The console script `miniouto` is registered in `pyproject.toml`:

```toml
[project.scripts]
miniouto = "miniouto.cli:app"
```

This dispatches into the `app: typer.Typer` object defined in `src/miniouto/cli/__init__.py`.

## Command tree

```
miniouto [--version]
   ├─ (no subcommand)            → TUI mode (cli/tui.py:run_tui)
   ├─ status                     → show current configuration
   ├─ chat <prompt> [options]    → one-shot chat turn
   ├─ provider
   │    ├─ providers                          → list all catalog (lma) providers
   │    ├─ models <provider-name>             → list models for a catalog provider
   │    ├─ add <provider-name> --api-key …    → add a provider from the catalog
   │    ├─ custom add  --name --format --base-url --api-key --default-model
   │    ├─ list
   │    ├─ remove <name>
   │    └─ default <name>
   ├─ style
   │    ├─ list
   │    ├─ set <name>
   │    ├─ add <repo_url> [--name NAME]
   │    ├─ update
   │    └─ show <name>
   └─ skill
        ├─ list
        └─ show <name>
```

> **Naming note:** the catalog commands (`providers`, `models`, `add`) source their data from `https://lma.blp.sh` (the "lma" service). The codebase and UI call these **"catalog"** providers (see `cli/provider.py` importing `core.lma as catalog_api` and the `+ add from catalog…` TUI sentinel), but the underlying `Provider.source` field value remains the literal string `"lma"` (`SOURCE_LMA = "lma"`).

## Root callback behavior

`@app.callback()` in `cli/__init__.py` runs before any subcommand. It:

1. Calls `storage.paths.ensure_dirs()` to guarantee `~/.miniouto/` and its `style/`, `sessions/`, `logs/` subdirs exist (also force-refreshes bundled styles — see `storage/paths.py`).
2. If `--version` was passed: prints `miniouto {__version__}` and exits 0.
3. If no subcommand was invoked (`ctx.invoked_subcommand is None`): calls `tui.run_tui()` which launches the Textual TUI.

The root app is configured with `no_args_is_help=False` and `invoke_without_command=True` so that `miniouto` alone runs the TUI without printing help text.

---

## `miniouto` (no args) — TUI mode

Delegates to `cli/tui.py:run_tui()`, which constructs a `ChatTUI` (Textual `App`) and calls `.run()`.

Layout (top to bottom):
- `Header(show_clock=False)`
- `Vertical(VerticalScroll(id="chat"), ChatInput(id="input"))`
- `BottomPanel` (height 4) with four rows:
  1. spinner row (a Textual-`Timer`-driven braille spinner shown while a chat turn is in flight)
  2. chip row with **three** clickable `StatusChip`s — `model`, `provider`, `style`
  3. session row — a plain `Static` label showing the active session name (not clickable)
  4. help-hint row
- `Footer`

### Chat log rows

The chat log is a `VerticalScroll` of row widgets (migrated from `RichLog` so rows can be clickable and live-updating). All row widgets derive from **`RowStatic`**, which gives every row identical drag selection: Textual's native path (theme `$screen-selection-*` colors — and nothing at all for Markdown, whose `RichVisual` ignores selection) is suppressed, and `render_line` paints the selected span itself with plain **reverse video (fg/bg inversion)**, character-accurate (CJK-safe) and clipped at the text end. `Ctrl+Shift+C` copies the extraction (`get_selection`, padding stripped).

| Row widget | Shows |
|---|---|
| `Static` | user prompts (`> ` in accent), blank spacers, `[system]` lines in warning color |
| `AnswerRow` | the final answer as `Markdown` — behavior inherited from `RowStatic`; exists as a distinct class because Markdown goes through `RichVisual` (no native selection painting) |
| `ThinkingRow` | reasoning/thinking — **collapsed by default** (`▸ thinking`), click or `Enter` to expand the full text (`▾ thinking` + content), click again to re-collapse. Same translucent border + muted styling as `EventRow` |
| `EventRow` | any other intermediate loop output — tool calls, iteration/token progress, provider errors — rendered with a **translucent left border (`$primary 40%`) and muted gray text** (provider errors use `$error`). No `actor:` prefixes |
| `SubagentRow` | one clickable line per subagent invocation: a **live braille spinner** + `subagent-<6hex>` + task preview while running, flipping to `✓` (success) / `✗` (error) on completion |

Clicking a `SubagentRow` (or focusing it and pressing `Enter`) pushes a **`SubagentDetailScreen`** — a modal rendering that invocation in the same notation as the main chat: the received task brief as a `> ` row, internal loop events as translucent-border muted rows, and the final result as a Markdown `AnswerRow` (live-refreshing while the subagent is still running). **`Esc` or `q` goes back.** Subagent-internal events never appear in the main chat log — only in the detail screen.

Reloading a session (`Ctrl+P` → "Pick session") re-renders from the session's `turns` section: subagent rows come back finished but stay clickable, with their recorded internal events available in the detail screen.

### Clickable chips

Each `StatusChip` is a focusable widget. Click it (or focus it with `Tab` and press `Enter`) to open a modal:

| Chip | Modal | Notes |
|---|---|---|
| `model` | `SelectionModal` (catalog provider, `source == "lma"`) **or** `TextInputModal` (custom provider, `source == "custom"`) | dispatched via `_open_model_editor` → `_catalog_model_picker_flow` or `_open_custom_model_editor`. Saving writes to `provider.default_model` and clears any prior `settings.model` override |
| `provider` | `SelectionModal` — list of configured providers + `+ add from catalog…` + `+ add custom…` sentinels | selecting a sentinel opens `_catalog_add_flow` or `_open_custom_add_wizard`; selecting an existing provider writes `settings.provider` |
| `style` | `SelectionModal` — list of installed styles | writes `settings.style` |

There is **no** session chip — the session label in row 3 is a plain `Static`. To change sessions, use the command palette (`Ctrl+P`) → "Pick session" / "New session".

The provider picker modal also accepts two sentinels (rendered as `extra_options` rows at the bottom of the list):

- `+ add from catalog…` — runs `_catalog_add_flow`: fetches `https://lma.blp.sh/provider`, filters to entries whose `sdk` maps to a supported coreouto `api_format`, lets you pick one, prompts for the API key, then saves the provider with `source="lma"` and the first model returned by lma as `default_model`. See `core/lma.py` and `core/providers.py:sdk_to_format`.
- `+ add custom…` — runs `_open_custom_add_wizard`: a five-step wizard (name → api_format → base_url → api_key → default_model), saving with `source="custom"`.

Modal results are persisted via `storage.settings.update(...)` (for provider/style/session) or `storage.providers.upsert(replace(...))` (for model changes), and the chip row re-renders. The active Textual theme is persisted to `settings.theme` and restored on launch.

### Model resolution order

`resolve_runtime_from_settings` (core/runtime.py) resolves the active model in this order:
1. `ChatOptions.model` (per-call `--model` flag)
2. `Settings.model` (legacy per-session override; cleared whenever the TUI model picker saves)
3. `Provider.default_model` (set by `provider add --default-model`, `provider custom add --default-model`, or the TUI model chip)

In the TUI, the model chip always shows `Provider.default_model` — the `Settings.model` field is reserved for the `chat --model` CLI override and is no longer surfaced through the UI.

Keybindings:
- App-level (registered on `ChatTUI`): `Ctrl+L` — clear log; `Ctrl+C` — quit.
- `Ctrl+P` — open the Textual system command palette (customized via `get_system_commands`) — new session, pick session, change model/provider/style/theme, clear log. The splash text on boot explicitly says "Press Ctrl+P for commands."
- Widget-level (inside modals / chips): `Tab` / `Shift+Tab` — cycle focus; `Enter` — confirm; `Esc` — cancel.

Submission flow: each submitted prompt is dispatched via `self.run_worker(..., exclusive=True)` to `_dispatch(prompt)`, which calls `core.chat.run_chat(opts, sink)` inside `asyncio.to_thread` so the Textual event loop stays responsive. The busy flag (`self._busy`) prevents re-entrancy. Output is mounted as row widgets (see "Chat log rows" above); errors/system messages render as `[…]` (e.g. `[error: {exc}]`) in theme warning.

`cli/tui.py:tui_summary() -> dict` is a programmatic snapshot helper (currently unused by the runtime) that returns `{provider, model, style, session, styles_available}`.

---

## `miniouto --version`

Prints `miniouto <version>` (from `miniouto.__version__`, currently `"0.3.0"`) and raises `typer.Exit()` (exit code 0).

---

## `miniouto status`

File: `cli/__init__.py` (lines ~45–66).

Reads:
- `storage.settings.load()` → active `provider`, `style`, `session`; `model` is derived from the active provider's `default_model` (or `'- (use chat --model)'` when empty)
- `storage.paths.ROOT` → storage path
- `storage.providers.load_all()` → all configured provider names
- `storage.styles.list_styles()` → installed style names
- `storage.skills.list_skills()` → all visible skill names (hidden skills excluded)
- `storage.sessions.list_sessions()` → session filenames

Prints 9 rich-formatted key/value lines: `Default provider`, `Default model`, `Active style`, `Session`, `Storage`, `Providers`, `Styles`, `Skills`, `Sessions`. Always exits 0 (no error states).

---

## `miniouto chat <prompt>`

File: `cli/chat.py`. Signature:

```
chat_cmd(
    prompt: str,                       # required positional
    --name        TEXT,                # session name. Without --name and --continue, a fresh session is generated each call.
    --provider    TEXT,                # override active provider
    --model       TEXT,                # override resolved model
    --style       TEXT,                # override active style
    --max-tokens  INT,                 # cap output tokens
    --temperature FLOAT,               # sampling temperature
    --continue, -c                     # prepend previous session history
    --answer-only, -a                  # print only the final answer (suppresses session marker, loop events, finish marker)
    --with-session                     # print only session marker + final answer (suppresses loop events + finish marker)
)
```

### Flag reference

| Flag | Effect |
|---|---|
| `--name` | Session name. Without `--name` and `--continue`, a fresh session is generated each call |
| `--provider` | Override the active provider for this call |
| `--model` | Override the resolved model for this call |
| `--style` | Override the active style for this call |
| `--max-tokens` | Cap output tokens |
| `--temperature` | Sampling temperature |
| `--continue` / `-c` | Prepend the session's previous history |
| `--answer-only` / `-a` | Print only the final answer. Suppresses the `------{session}------` marker, loop events, and `------finish------` marker |
| `--with-session` | Print only the `------{session}------` marker + final answer. Suppresses loop events and `------finish------` marker |

`--answer-only` and `--with-session` are **mutually exclusive** (exit 1 with `✗ --answer-only and --with-session are mutually exclusive.`). Both suppress the spinner, loop events, and finish marker by putting the `ConsoleEventSink` into `quiet` mode; they differ only in whether the session marker line is emitted up front. The default (neither flag) keeps the full verbose output: session marker + spinner + loop events + finish marker + answer.

### Behavior

1. Resolves `session_name` via a 3-way branch:
   - If `--continue`: `name or settings.session or "default"`.
   - elif `--name` was supplied: use `name` verbatim.
   - else: generate a fresh `chat-{YYYYMMDD-HHMMSS}-{6hex}` name.
2. **Always** calls `settings.update(session=session_name)` to persist (unconditional — every chat call updates the active session).
3. Emits the `------{session_name}------` marker to stdout unless `--answer-only` was passed, then builds a `core.chat.ChatOptions` dataclass from the flags and dispatches to `core.chat.run_chat(opts, sink=ConsoleEventSink(quiet=answer_only or with_session))`.
4. On exception: `chat_cmd` does **not** catch — `run_chat` itself calls `_dump_failure_diagnostics` (prints `✗ {ExceptionType}: {msg}` + the last ≤5 tool calls + a full traceback to **stderr**) and **re-raises**. Typer prints the traceback and exits 1.
5. On success: `ConsoleEventSink` writes the reply as plain stdout; in verbose mode it is preceded by a `------finish------` marker and loop events (tool calls, intermediate responses) rendered in `orange3`. In quiet mode only the raw answer (and the session marker, for `--with-session`) reaches stdout.

Loop-event notation in verbose CLI output keeps the `name:` prefix style:

```
outo: Bash ls -la                          # outo tool call
outo:thinking: <full reasoning text>       # outo reasoning (dim, untruncated)
subagent-a1b2c3: write the tests           # subagent invocation start (task preview)
subagent-a1b2c3: Bash pytest -q            # a tool call inside that subagent
subagent-a1b2c3:thinking: <full text>      # reasoning inside that subagent
subagent-a1b2c3: done                      # subagent finished (dim; "error: …" on failure)
provider: HTTP 429 → retry: …              # rule-matched provider error
```

Every subagent invocation gets a stable 6-hex id (`subagent-<6hex>`), so parallel `call_subagent` runs are distinguishable line-by-line. Reasoning/thinking is labeled `:thinking:` for both outo and subagents and printed **in full** in the CLI (the TUI shows a truncated gray row in the main chat and the full text in the subagent detail screen).

### Model resolution

The active model is chosen by the first match in (matches the README's 4-step list):

1. `miniouto chat --model <name>` (per-call override)
2. `settings.model` (legacy per-session override; cleared whenever the TUI model picker saves)
3. `provider.default_model` (set by `provider add --default-model`, `provider custom add --default-model`, or the TUI model chip)
4. **error** — no model can be inferred

---

## `miniouto provider ...`

File: `cli/provider.py`. Sub-app `app = typer.Typer(help="Manage LLM providers (catalog browse + custom config).")`.

The provider command has three groups: **catalog** browse/add (`providers`, `models`, `add`), **storage** ops on already-configured providers (`list`, `remove`, `default`), and **custom** manual config (`custom add`).

### `provider providers`

Calls `GET https://lma.blp.sh/provider`. Prints a rich `Table` titled `Catalog providers (N)` with columns `Name | SDK | API URL | miniouto format | Addable?`. The "Addable?" column is `✓` when `core.providers.sdk_to_format(sdk, api)` returns a non-`None` format. Empty result → yellow "No providers returned by the catalog." (exit 0). Transport failure → red `✗ Failed to reach catalog: {exc}` + exit 1.

### `provider models <provider-name>`

Positional argument; lma does case-/whitespace-insensitive fuzzy match. Calls `GET https://lma.blp.sh/model-list?provider-name=<name>`. Prints a `Table` titled `Catalog models for '<name>' (N)` with columns `ID | Name`. Empty result → yellow "No models returned for `<name>`." + exit 1. Transport failure → red `✗ Failed to reach catalog: {exc}` + exit 1.

### `provider add <provider-name> --api-key <key> [--default-model <id>]`

Catalog add. Positional `provider_name` (fuzzy-matched via `core.lma.find_provider`), required `--api-key`, optional `--default-model` (default `""`).

- If `find_provider` returns `None`: red `✗ No catalog provider matched <name>. Run 'miniouto provider providers' to see the catalog.` + exit 1.
- Calls `core.providers.add_provider_from_lma(...)` to build a `Provider` with `source="lma"`.
- If `--default-model` is empty, re-fetches the provider's model list and uses the first model id (re-invokes `add_provider_from_lma` with that id).
- If the provider already exists: yellow `! Provider <name> already exists; overwriting.`
- On success: `✓ Added provider <name> (<api_format>, default-model=<model or ->).`

If `sdk_to_format` cannot map the SDK (raises `ValueError`): red `✗ {exc}` + exit 1.

### `provider custom add`

Manual add via a nested sub-app (`custom_app`). Flags:

```
add_custom(
    --name           TEXT  # required
    --format         TEXT  # default "openai"; one of SUPPORTED_FORMATS:
                           #   openai, openai-response, anthropic, google
    --base-url       TEXT  # default ""
    --api-key        TEXT  # default "" (omit to read from env at call time)
    --default-model  TEXT  # default "" (used when chat --model is not given)
)
```

- Validates `--format` against `core.providers.SUPPORTED_FORMATS = ("openai", "openai-response", "anthropic", "google")`. Unknown → red `✗ Unknown format <fmt>. Supported: …` + exit 1.
- Calls `storage.paths.ensure_dirs()`, builds a `storage.providers.Provider(...)` (with `source="custom"`), calls `storage.providers.upsert(provider)`.
- Prints `✓ Saved custom provider <name> (<api_format>).`

### `provider list`

Pretty-prints a `rich.table.Table` titled `Providers` with columns:

| Name | Type | Format | Base URL | Default Model | Default |

The `Type` column renders `custom` or `catalog` based on `provider.source` (`SOURCE_CUSTOM` → "custom", `SOURCE_LMA` → "catalog"). The active provider (per `settings.toml`) is marked with a green ● in the Default column.

If no providers are configured, prints yellow "No providers configured. Run `miniouto provider add <name>` or `miniouto provider custom add`."

### `provider remove <name>`

Calls `storage.providers.remove(name)`. Prints `✓ Removed provider <name>.` on success; red `✗ Provider <name> does not exist.` + exit 1 if not found.

### `provider default <name>`

Validates via `storage.providers.get(name)`. If `None` → red `✗ Provider <name> is not configured.` + exit 1. Otherwise calls `settings.update(provider=name)` and prints `✓ Default provider is now <name>.`.

---

## `miniouto style ...`

File: `cli/style.py`. Sub-app `app = typer.Typer(help="Manage agent style documents.")`.

### `style list`

Iterates `storage.styles.list_styles()`. Active style (per `settings.toml`) is marked with a green ●. Empty → yellow "No styles installed."

### `style set <name>`

Calls `storage.styles.read(name)`. If `None` → red `✗ ... is not installed.` + exit 1. Otherwise calls `settings.update(style=name)` and prints `✓ Active style is now <name>.`.

### `style add <repo_url> [--name NAME]`

Fetches `/style-md/` from a remote git repo and writes the `.md` files into `~/.miniouto/style/`.

- `repo_url`: any of GitHub (`https://github.com/owner/repo`), GitLab (`https://gitlab.com/owner/repo`), or raw HTML index URL.
- `--name`: override each downloaded file's basename (rarely used).

Internally calls `storage.styles.add_from_repo(repo_url, name_override=name)`. On success the repo URL is appended to `~/.miniouto/style_repos.toml` (deduped) so `style update` can re-fetch it later. On exception: red `✗ Failed to fetch styles: {exc}` + exit 1. On success: prints `✓ Added/updated styles: <comma list>`.

See `docs/storage.md` and the fetchers in `storage/styles.py` (`_fetch_github_tree`, `_fetch_gitlab_tree`, `_fetch_raw_index`) for the URL shapes accepted.

### `style update`

Refreshes every style to its latest source. No arguments.

1. Calls `storage.paths.ensure_dirs()` to force-refresh all bundled styles (any installed file whose name matches a bundled template is overwritten with the current bundled content).
2. Reads every repo URL from `~/.miniouto/style_repos.toml` (recorded by prior `style add` calls) and re-fetches each via `storage.styles.add_from_repo(url)`.
3. Prints `✓ Refreshed bundled styles: <names>` and `✓ Re-fetched N repo(s): <names>`.
4. If there are no recorded repos, prints a dim hint: "No repo styles to update. Use `style add <repo-url>` to track a repo."
5. Per-repo fetch failures are printed as `✗ Failed to update <url>: <err>` but do not abort the remaining repos.

Styles you created by hand (no matching bundled template and no recorded repo) are left untouched.

### `style show <name>`

Prints the file contents of `~/.miniouto/style/<name>.md` to stdout. If missing → red `✗ ... is not installed.` + exit 1.

---

## `miniouto skill ...`

File: `cli/skill.py`. Sub-app `app = typer.Typer(help="Manage agent skills.")`.

**Read-only** over `~/.agents/skills/` (NOT `~/.miniouto/`).

### `skill list`

Iterates `storage.skills.list_skills()` — **hidden skills are excluded** (the function filters `not skill.hidden`). Empty → yellow "No skills found." followed by "Check ~/.agents/skills/". Otherwise prints a rich `Table` titled `Available Skills` with columns `Name | Description`, truncating descriptions to 80 chars + "…".

### `skill show <name>`

- `skill = storage.skills.get_skill(name)` (this does **not** filter on `hidden`, so hidden skills can still be shown by explicit name). If `None` → red `✗ Skill <name> not found.` + exit 1.
- Prints in order:
  - `Name: <name>`
  - `Description: <description>`
  - `License: <license>` (if set)
  - `Allowed Tools: <tools>` (if set)
  - blank line
  - Full `skill.content`.

---

## Catalog (lma) endpoint caching

The catalog commands (`provider providers`, `provider models`, `provider add`) and the TUI catalog flows all hit `https://lma.blp.sh` via `core/lma.py`. Responses are cached for 10 minutes (matching lma's server TTL); the cache lives in `core.lma._CACHE` and can be cleared with `core.lma.clear_cache()` (used by tests). See `docs/lma.md` for the full endpoint reference and `sdk_to_format` mapping table.

---

## Error handling & exit codes

| Scenario | Behavior | Exit code |
|---|---|---|
| Successful command | stdout output, no error | 0 |
| Unhandled exception in `chat_cmd` / `run_chat` | `run_chat._dump_failure_diagnostics` prints `✗ {ExceptionType}: {msg}` + last ≤5 tool calls + traceback to stderr, then re-raises; Typer prints the traceback | 1 |
| `provider custom add` with unknown `--format` | `✗ Unknown format <fmt>. Supported: …` + `typer.Exit(1)` | 1 |
| `provider add` for an unknown catalog provider name | `✗ No catalog provider matched <name>. Run 'miniouto provider providers' to see the catalog.` + exit 1 | 1 |
| `provider add` for an unmappable SDK | `✗ {exc}` (ValueError from `add_provider_from_lma`) + exit 1 | 1 |
| `provider add` overwriting an existing provider | yellow `! Provider <name> already exists; overwriting.` | 0 |
| `provider providers` / `provider models` network failure | `✗ Failed to reach catalog: {exc}` + exit 1 | 1 |
| `provider models` empty result | yellow "No models returned for `<name>`." + exit 1 | 1 |
| `provider providers` empty result | yellow "No providers returned by the catalog." (returns, exit 0) | 0 |
| `provider remove` on missing name | `✗ Provider <name> does not exist.` + `typer.Exit(1)` | 1 |
| `provider default` on unconfigured name | `✗ Provider <name> is not configured.` + `typer.Exit(1)` | 1 |
| `style set` on missing style | `✗ ... is not installed.` + `typer.Exit(1)` | 1 |
| `style add` fetch failure | `✗ Failed to fetch styles: {exc}` + `typer.Exit(1) from exc` | 1 |
| `style update` per-repo fetch failure | `✗ Failed to update <url>: <err>` (printed, remaining repos still attempted; command exits 0) | 0 |
| `style show` on missing style | `✗ ... is not installed.` + `typer.Exit(1)` | 1 |
| `skill show` on missing skill | `✗ Skill <name> not found.` + `typer.Exit(1)` | 1 |
| `--version` | print version + `raise typer.Exit()` (no code → 0) | 0 |
| Typer argument parsing errors | Typer default (red error to stderr) | 2 |

**Pattern**: Storage / style / skill / catalog handlers catch exceptions explicitly and exit via `typer.Exit(code=1)`, preserving tracebacks via `from exc`. `chat_cmd` does **not** catch — `run_chat` does its own diagnostics and re-raises so the caller sees a real traceback. The TUI's `_dispatch` catches everything internally and writes `[error: {exc}]` into the log without crashing the app.

## Rich output conventions

- `✓` (green) — success
- `✗` (red) — failure
- `!` (yellow) — warnings (e.g. provider overwrite)
- `orange3` — loop events in `ConsoleEventSink` (tool calls, intermediate responses). The final answer is written as plain stdout with a `------finish------` marker (no rich color).
- rich `Table` — `provider list`, `provider providers`, `provider models`, `skill list`

## Module dependency graph (CLI layer only)

```
cli/__init__.py ──┬─→ cli/provider.py ─→ storage.paths, storage.providers, storage.settings, core.lma, core.providers
                  ├─→ cli/style.py    ─→ storage.paths, storage.settings, storage.styles
                  ├─→ cli/skill.py    ─→ storage.skills
                  ├─→ cli/tui.py      ─→ core.{chat,events,lma}, core.providers, storage.{paths,providers,settings,styles,sessions}
                  └─→ cli/chat.py     ─→ core.chat, core.events, storage.settings
```

External libs the CLI directly imports: `typer`, `rich.console`, `rich.table`, `rich.markdown`, `textual.app`, `textual.containers`, `textual.binding`, `textual.reactive`, `textual.widgets` (`RichLog`, `Input`, `ListView`, `ListItem`, `Label`, `Static`, `TextArea`), `textual.message`, `textual.screen`, `textual.timer`, `rich.text`, `asyncio`, `dataclasses.replace`.
