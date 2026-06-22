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
   │    ├─ add   --name --format --base-url --api-key --default-model
   │    ├─ list
   │    ├─ remove <name>
   │    └─ default <name>
   ├─ style
   │    ├─ list
   │    ├─ set <name>
   │    ├─ add <repo_url> [--name NAME]
   │    └─ show <name>
   ├─ skill
   │    ├─ list
   │    └─ show <name>
   └─ lma                        ← NEW: lma (llm-model-api) browser
        ├─ providers                          → list all 144 lma providers
        ├─ models <provider-name>             → list models for a provider
        └─ add <provider-name> --api-key ...  → add a provider from lma
```

## Root callback behavior

`@app.callback()` in `cli/__init__.py` runs before any subcommand. It:

1. Calls `storage.paths.ensure_dirs()` to guarantee `~/.miniouto/{style,sessions,logs}` exist.
2. If `--version` was passed: prints `miniouto {__version__}` and exits 0.
3. If no subcommand was invoked (`ctx.invoked_subcommand is None`): calls `tui.run_tui()` which launches the Textual TUI.

The root app is configured with `no_args_is_help=False` and `invoke_without_command=True` so that `miniouto` alone runs the TUI without printing help text.

---

## `miniouto` (no args) — TUI mode

Delegates to `cli/tui.py:run_tui()`, which constructs a `ChatTUI` (Textual `App`) and calls `.run()`.

Layout (top to bottom):
- `Header(show_clock=False)`
- `Vertical(RichLog(id="chat", wrap=True), Input(placeholder="…", id="input"))`
- `BottomPanel` (height 2):
  - Row 1: four clickable `StatusChip`s — `provider`, `model`, `style`, `session`
  - Row 2: keyboard/click hint
- `Footer`

### Clickable chips

Each `StatusChip` is a focusable widget. Click it (or focus it with `Tab` and press `Enter`) to open a modal:

| Chip | Modal | Notes |
|---|---|---|
| `provider` | `SelectionModal` — list of configured providers + `+ add from lma…` + `+ add custom…` sentinels | selecting a sentinel opens the LMA add wizard or the custom-add wizard; selecting an existing provider writes `settings.provider` |
| `model` | `SelectionModal` (lma-backed provider) **or** `TextInputModal` (custom provider) | the picker is the lma model list when `provider.source == "lma"`, otherwise a free-text input. Saving writes to `provider.default_model` and clears any prior `settings.model` override |
| `style` | `SelectionModal` — list of installed styles | writes `settings.style` |
| `session` | `SelectionModal` — list of sessions + `+ new session…` sentinel | selecting the sentinel opens a `TextInputModal` for the new name |

The provider picker modal also accepts two sentinels (rendered as `extra_options` rows at the bottom of the list):

- `+ add from lma…` — runs the LMA add wizard: fetches `https://lma.blp.sh/provider`, filters to entries whose `sdk` maps to a supported coreouto `api_format`, lets you pick one, prompts for the API key, then saves the provider with `source="lma"` and the first model returned by lma as `default_model`. See `core/lma.py` and `core/providers.py:sdk_to_format`.
- `+ add custom…` — runs a five-step wizard (name → api_format → base_url → api_key → default_model), saving with `source="custom"`.

Modal results are persisted via `storage.settings.update(...)` (for provider/style/session) or `storage.providers.upsert(replace(...))` (for model changes), and the chip row re-renders.

### Model resolution order

`resolve_runtime_from_settings` (core/runtime.py) resolves the active model in this order:
1. `ChatOptions.model` (per-call `--model` flag)
2. `Settings.model` (legacy per-session override; cleared whenever the TUI model picker saves)
3. `Provider.default_model` (set by `provider add --default-model`, `lma add --default-model`, or the TUI model chip)

In the TUI, the model chip always shows `Provider.default_model` — the `Settings.model` field is reserved for the `chat --model` CLI override and is no longer surfaced through the UI.

Keybindings:
- `Tab` / `Shift+Tab` — cycle chip focus
- `Enter` — open focused chip's modal
- `Esc` — cancel current modal
- `Ctrl+L` — clear log
- `Ctrl+C` — quit

Submission flow: each submitted prompt is dispatched via `self.run_worker(..., exclusive=True)` to `_dispatch(prompt)`, which calls `core.chat.run_chat(opts)` inside `asyncio.to_thread` so the Textual event loop stays responsive. The busy flag (`self._busy`) prevents re-entrancy. Output is posted back into the RichLog; user prompts in normal text, assistant replies in `dark_orange3`, errors prefixed with `[error: …]`.

`cli/tui.py:tui_summary() -> dict` is a programmatic snapshot helper (currently unused by the runtime) that returns `{provider, model, style, session, styles_available}`.

---

## `miniouto --version`

Prints `miniouto <version>` (from `miniouto.__version__`, currently `"0.1.0"`) and raises `typer.Exit()` (exit code 0).

---

## `miniouto status`

File: `cli/__init__.py` (lines ~45–66).

Reads:
- `storage.settings.load()` → active `provider`, `model` (derived from provider's `default_model`), `style`, `session`
- `storage.paths.ROOT` → storage path
- `storage.providers.load_all()` → all configured provider names
- `storage.styles.list_styles()` → installed style names
- `storage.skills.list_skills()` → all skill names
- `storage.sessions.list_sessions()` → session filenames

Prints as rich-formatted key/value lines. Always exits 0 (no error states).

---

## `miniouto chat <prompt>`

File: `cli/chat.py`. Signature:

```
chat_cmd(
    prompt: str,                       # required positional
    --name        TEXT,                # session name (persists to settings.toml)
    --provider    TEXT,                # override active provider
    --model       TEXT,                # override resolved model
    --style       TEXT,                # override active style
    --max-tokens  INT,                 # cap output tokens
    --temperature FLOAT,               # sampling temperature
    --continue, -c                     # prepend previous session history
)
```

### Flag reference

| Flag | Effect |
|---|---|
| `--name` | Session name (persists to `~/.miniouto/settings.toml`) |
| `--provider` | Override the active provider for this call |
| `--model` | Override the resolved model for this call |
| `--style` | Override the active style for this call |
| `--max-tokens` | Cap output tokens |
| `--temperature` | Sampling temperature |
| `--continue` / `-c` | Prepend the session's previous history |

### Behavior

1. Resolves `session_name = name or settings.load().session or "default"`.
2. If `--name` was given OR `--continue` was set, calls `settings.update(session=session_name)` to persist.
3. Builds a `core.chat.ChatOptions` dataclass from the flags and dispatches to `core.chat.run_chat(opts)`.
4. On exception: prints `[red]✗[/red] {exc}` and raises `typer.Exit(code=1) from exc`.
5. On success: prints the reply in `dark_orange3` rich style.

### Model resolution

The active model is chosen by the first match in:

1. `miniouto chat --model <name>` (per-call override)
2. `miniouto provider add --default-model <name>` (provider-level default)
3. **error** — no model can be inferred

---

## `miniouto provider ...`

File: `cli/provider.py`. Sub-app `app = typer.Typer(help="Manage LLM providers.")`.

### `provider add`

```
add(
    --name           TEXT  # required
    --format         TEXT  # default "openai"; one of SUPPORTED_FORMATS:
                          #   openai, openai-response, anthropic, google
    --base-url       TEXT  # default ""
    --api-key        TEXT  # default "" (omit to read from env at call time)
    --default-model  TEXT  # default "" (used when chat --model is not given)
)
```

- Validates `--format` against `core.providers.SUPPORTED_FORMATS = ("openai", "openai-response", "anthropic", "google")`. Unknown → red error + `typer.Exit(1)`.
- Calls `storage.paths.ensure_dirs()`, builds a `storage.providers.Provider(...)`, calls `storage.providers.upsert(provider)`.
- Prints `✓ Added provider "<name>".`

### `provider list`

Pretty-prints a `rich.table.Table` with columns:

| Name | Format | Base URL | Default Model | Default |

The active provider (per `settings.toml`) is marked with a green ● in the Default column.

If no providers are configured, prints yellow "No providers configured. Run `miniouto provider add`."

### `provider remove <name>`

Calls `storage.providers.remove(name)`. Prints `✓ Removed provider "<name>".` on success; red ✗ + `typer.Exit(1) from exc` if the provider was not found.

### `provider default <name>`

Validates via `storage.providers.get(name)`. If `None` → red ✗ + `typer.Exit(1)`.
Otherwise calls `settings.update(provider=name)` and prints `✓ Default provider set to "<name>".`

---

## `miniouto style ...`

File: `cli/style.py`. Sub-app `app = typer.Typer(help="Manage agent style documents.")`.

### `style list`

Iterates `storage.styles.list_styles()`. Active style (per `settings.toml`) is marked with a green ●. Empty → yellow "No styles installed."

### `style set <name>`

Calls `storage.styles.read(name)`. If `None` → red ✗ + `typer.Exit(1)`.
Otherwise calls `settings.update(style=name)` and prints `✓ Active style set to "<name>".`

### `style add <repo_url> [--name NAME]`

Fetches `/style-md/` from a remote git repo and writes the `.md` files into `~/.miniouto/style/`.

- `repo_url`: any of GitHub (`https://github.com/owner/repo`), GitLab (`https://gitlab.com/owner/repo`), or raw HTML index URL.
- `--name`: override each downloaded file's basename (rarely used).

Internally calls `storage.styles.add_from_repo(repo_url, name_override=name)`. On exception: red ✗ + `typer.Exit(1) from exc`. On success: prints `Added/updated styles: <comma list>`.

See `docs/storage.md` and `tools/registry.py`-style fetcher in `storage/styles.py` (`_fetch_dir`, `_fetch_github_tree`, `_fetch_gitlab_tree`, `_fetch_raw_index`) for the URL shapes accepted.

### `style show <name>`

Prints the file contents of `~/.miniouto/style/<name>.md` to stdout. If missing → red ✗ + `typer.Exit(1)`.

---

## `miniouto skill ...`

File: `cli/skill.py`. Sub-app `app = typer.Typer(help="Manage agent skills.")`.

**Read-only** over `~/.agents/skills/` (NOT `~/.miniouto/`).

### `skill list`

Iterates `storage.skills.list_skills()`. Empty → yellow "No skills found. Check `~/.agents/skills/`".
Otherwise prints a rich `Table` with columns `Name | Description`, truncating descriptions to 80 chars + "...".

### `skill show <name>`

- `skill = storage.skills.get_skill(name)`. If `None` → red ✗ + `typer.Exit(1)`.
- Prints in order:
  - `Name: <name>`
  - `Description: <description>`
  - `License: <license>` (if set)
  - `Allowed Tools: <tools>` (if set)
  - blank line
  - Full `skill.content`.

---

## `miniouto lma ...`

File: `cli/lma.py`. Sub-app `app = typer.Typer(help="Browse the lma (llm-model-api) provider and model catalog.")`.

Thin CLI over `core/lma.py`, which wraps `https://lma.blp.sh` (a re-shaped view of `models.dev/api.json` deployed on Cloudflare Workers). The same client is used by the TUI provider add flow and by `core/context.py` for per-model context / max-output-token lookups. See `docs/lma.md` for the full endpoint reference.

### `lma providers`

Calls `GET https://lma.blp.sh/provider`. Prints a rich table with columns `Name | SDK | API URL | miniouto format | Addable?`. The "Addable?" column is `✓` when `core.providers.sdk_to_format(sdk, api)` returns a non-`None` format (i.e. the entry can be added by the TUI wizard or `lma add`). Transport failure → red ✗ + `typer.Exit(1)`.

### `lma models <provider-name>`

Calls `GET https://lma.blp.sh/model-list?provider-name=<name>` (lma does case-/whitespace-insensitive fuzzy match). Prints a table `ID | Name`. Empty result → yellow "No models returned for `<name>`" + `typer.Exit(1)`. Transport failure → red ✗ + `typer.Exit(1)`.

### `lma add <provider-name> --api-key <key> [--default-model <id>]`

Resolves `<provider-name>` via `core.lma.find_provider`. If found, derives `name = core.lma.slugify(...)`, calls `core.providers.add_provider_from_lma(...)`, and stores the resulting `Provider` with `source="lma"`. If `--default-model` is empty, fetches the provider's model list from lma and uses the first entry as `default_model`. Existing providers are overwritten (yellow warning printed first).

The endpoint data is cached for 10 minutes (matching lma's server TTL); the cache lives in `core.lma._CACHE` and can be cleared with `core.lma.clear_cache()` (used by tests).

---

## Error handling & exit codes

| Scenario | Behavior | Exit code |
|---|---|---|
| Successful command | stdout output, no error | 0 |
| Unhandled exception in `chat_cmd` | `[red]✗[/red] {exc}` to stderr + `typer.Exit(code=1) from exc` | 1 |
| `provider add` with unknown `--format` | `[red]✗[/red] Unknown format ...` + `typer.Exit(1)` | 1 |
| `provider remove` on missing name | `[red]✗[/red] ... does not exist.` + `typer.Exit(1)` | 1 |
| `provider default` on unconfigured name | `[red]✗[/red] ... is not configured.` + `typer.Exit(1)` | 1 |
| `style set` on missing style | `[red]✗[/red] ... is not installed.` + `typer.Exit(1)` | 1 |
| `style add` fetch failure | `[red]✗[/red] Failed to fetch styles: {exc}` + `typer.Exit(1) from exc` | 1 |
| `style show` on missing style | `[red]✗[/red] ... is not installed.` + `typer.Exit(1)` | 1 |
| `skill show` on missing skill | `[red]✗[/red] ... not found.` + `typer.Exit(1)` | 1 |
| `lma providers` / `lma models` network failure | `[red]✗[/red] Failed to reach lma: ...` + `typer.Exit(1)` | 1 |
| `lma add` for an unknown provider name | `[red]✗[/red] No lma provider matched ...` + `typer.Exit(1)` | 1 |
| `lma add` for an unmappable SDK | `[red]✗[/red] Cannot map lma provider ...` + `typer.Exit(1)` | 1 |
| `lma add` overwriting an existing provider | `[yellow]![/yellow] ... already exists; overwriting.` | 0 |
| `--version` | print version + `raise typer.Exit()` (no code → 0) | 0 |
| Typer argument parsing errors | Typer default (red error to stderr) | 2 |

**Pattern**: All CLI handlers catch exceptions explicitly and exit via `typer.Exit(code=1)`, preserving tracebacks via `from exc`/`from BaseException`. The TUI's `_dispatch` catches everything internally and writes `[error: {exc}]` into the log without crashing the app.

## Rich output conventions

- `✓` (green) — success
- `✗` (red) — failure
- yellow — warnings
- `dark_orange3` — assistant replies (in both `chat` command and TUI log)
- rich `Table` — `provider list`, `skill list`

## Module dependency graph (CLI layer only)

```
cli/__init__.py ──┬─→ cli/provider.py ─→ storage.paths, storage.providers, storage.settings, core.providers
                  ├─→ cli/style.py    ─→ storage.paths, storage.settings, storage.styles
                  ├─→ cli/skill.py    ─→ storage.skills
                  ├─→ cli/lma.py      ─→ core.lma, core.providers, storage.{paths,providers}
                  ├─→ cli/tui.py      ─→ core.{chat,lma}, core.providers, storage.{paths,providers,settings,styles,sessions}
                  └─→ cli/chat.py     ─→ core.chat, storage.settings
```

External libs the CLI directly imports: `typer`, `rich.console`, `rich.table`, `textual.app`, `textual.containers`, `textual.binding`, `textual.reactive`, `textual.widgets`, `rich.text`, `asyncio`, `dataclasses.replace`.
