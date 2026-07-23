# Storage Layer

The `storage/` subpackage is the **only layer in miniouto that reads from or writes to the filesystem** (apart from the `tools/` subpackage, which mutates files as part of executing agent actions). Everything is human-inspectable plain text: TOML for config, Markdown for styles, JSON for session history.

All paths are rooted at `~/.miniouto/` and can be overridden via the `MINIOUTO_HOME` environment variable.

## Filesystem layout

```
~/.miniouto/                      (or $MINIOUTO_HOME)
├── providers.toml                # provider configurations (one top-level TOML table per provider)
├── settings.toml                 # active provider / model / style / session / theme
├── style_repos.toml              # recorded repo URLs added via `style add` (re-fetched by `style update`)
├── style/
│   ├── default.md                # seeded + force-refreshed from src/miniouto/default_style/default.md
│   ├── claude.md                 # force-refreshed
│   ├── codex.md                  # force-refreshed
│   ├── opencode.md               # force-refreshed
│   ├── oh-my-opencode.md         # force-refreshed
│   └── codebuff.md               # force-refreshed
├── sessions/
│   └── <name>.json               # conversation history per session
└── logs/                         # reserved (currently unused by code)
```

Skills live **outside** `~/.miniouto/` at `~/.agents/skills/<name>/SKILL.md` (the Anthropic convention). See `docs/skills.md`.

## Path constants

Defined in `src/miniouto/storage/paths.py`:

```python
ROOT               = Path(os.environ.get("MINIOUTO_HOME") or Path.home() / ".miniouto").expanduser()
PROVIDERS_FILE     = ROOT / "providers.toml"
SETTINGS_FILE      = ROOT / "settings.toml"
STYLE_DIR          = ROOT / "style"
STYLE_REPOS_FILE   = ROOT / "style_repos.toml"
SESSION_DIR        = ROOT / "sessions"
LOG_DIR            = ROOT / "logs"
```

`ensure_dirs()` (called by the CLI root callback before every command):
1. Creates `ROOT`, `STYLE_DIR`, `SESSION_DIR`, `LOG_DIR` if missing.
2. For every `*.md` in the bundled `src/miniouto/default_style/` package data, overwrites the matching `STYLE_DIR/<name>.md` with the current bundled content **whenever the content differs** — bundled templates are force-refreshed, so editing a bundled style in place does not survive a reinstall/relaunch. To customize a bundled style, copy it to a new name. Files whose names do not match a bundled template are left untouched.

## File format schemas

### `providers.toml`

```toml
# Miniouto provider registry
# One top-level TOML table per provider (keyed by the provider name).
# Created/updated via `miniouto provider add` (catalog) or `miniouto provider custom add`.

[openai]
name = "openai"
api_format = "openai"          # one of: openai, openai-response, anthropic, google
source = "custom"              # "custom" (manual) or "lma" (added from the catalog)
base_url = "https://api.openai.com/v1"   # empty values are omitted on write; defaulted on read
api_key = "sk-..."
default_model = "gpt-5.5"      # used when chat --model is not given

[anthropic]
name = "anthropic"
api_format = "anthropic"
source = "lma"                 # added via `provider add Anthropic …`
default_model = "claude-opus-4.5"

# Optional per-provider model-cap overrides (set via the TUI custom-model
# editor). When present they win over anything lma reports for the
# default_model — useful for custom providers lma has never heard of.
# Both are omitted on write when unset.
max_context_window = 128000
max_output_tokens = 8000
```

Each provider is a **top-level TOML table named after the provider** (e.g. `[openai]`, not `[providers.openai]`). The `Provider` dataclass in `storage/providers.py` has an `extra: dict` field for unknown keys — they're preserved through round-trips so future fields won't be lost.

Notes on the on-disk format:
- Empty-string fields (`base_url=""`, `api_key=""`) are **omitted on write** by `Provider.to_dict()` (which filters `None`/`{}`/`""`). `from_dict()` restores them to defaults on read, so round-trips cleanly.
- `name` is redundantly stored inside the table body (it is also the table key); `from_dict(name, body)` ignores the body's `name` for the field but does not put it in `extra`.
- `source` is always written (its default `"custom"` is non-empty so survives the filter).
- `extra` (default `{}`) is omitted on write and reconstituted as unknown keys on read.

### `settings.toml`

```toml
provider = "openai"     # default provider name (must match a top-level table in providers.toml)
model    = ""           # optional; legacy per-session model override (cleared by TUI model picker)
style    = "default"    # default style (must exist in ~/.miniouto/style/)
session  = "default"    # default session name (auto-set to the most recent chat session)
theme    = ""           # optional; TUI theme name (persisted by the TUI theme picker)
```

All five keys are optional. Missing keys fall back to: empty string for `provider`/`model`/`theme`, `"default"` for `style`/`session`.

The `Settings` dataclass in `storage/settings.py` exposes `merge(overrides) -> Settings` — non-empty/non-None override values win. `to_dict()` drops empty values (same rule as `Provider`).

### `sessions/<name>.json`

Schema v2 (`"version": 2`). Two sections with distinct jobs:

```json
{
  "version": 2,
  "session": "<name>",
  "updated": "2026-07-23T10:34:12Z",
  "history": [
    {"role": "user", "content": "hello"},
    {
      "role": "assistant",
      "content": "",
      "tool_calls": [{"id": "call_abc", "name": "Bash", "arguments": {"command": "ls"}}]
    },
    {"role": "tool", "content": "file1.py\n", "tool_call_id": "call_abc", "name": "Bash"},
    {"role": "assistant", "content": "You have file1.py"}
  ],
  "turns": [
    {
      "ts": "2026-07-23T10:34:12Z",
      "user": "hello",
      "assistant": "You have file1.py",
      "events": [
        {"actor": "outo", "kind": "thinking", "text": "the user wants a listing…"},
        {"actor": "outo", "kind": "tool", "text": "Bash ls", "tool_name": "Bash"},
        {"actor": "subagent-a1b2c3", "kind": "subagent_start", "text": "inspect the dir", "tool_name": "call_subagent", "subagent_id": "a1b2c3"},
        {"actor": "subagent-a1b2c3", "kind": "subagent_end", "text": "done", "subagent_id": "a1b2c3"}
      ]
    }
  ]
}
```

- **`history`** — restorable model context: raw coreouto `Message.model_dump(mode="json")` dicts, **system messages excluded** (coreouto prepends a fresh system prompt on every `call()`, so persisting it would duplicate it — see coreouto `examples/21_loop_history.py`). Contains the *full* loop transcript: intermediate assistant messages, tool calls, and tool results. **Rewritten in full after every turn** from `Response.messages`, so it always matches what the model actually saw — including any in-loop compaction done by the summarize hook. Reloaded via `co.Message.model_validate`.
- **`turns`** — display-only log, appended once per turn. `events` are `LoopEvent` dicts (`actor`, `kind`, `text`, optional `tool_name` / `subagent_id`). Thinking/reasoning lives **only here** as `kind="thinking"` events: coreouto's providers never put thinking into history `Message` objects, so it is captured from the `ON_THINKING` hook and cannot be part of the restorable history. The TUI renders past sessions from `turns`.

Notes:
- **v1 migration**: files without a `version` key (flat `messages` list) are migrated on load — records become `history` entries, and user/assistant pairs are synthesized into `turns`. The `(session created)` system marker is dropped.
- **Tolerant loading**: corrupt JSON, non-dict envelopes, and unknown record fields never raise — they yield an empty `SessionData`.
- **Media caveat**: a message whose content blocks carry raw bytes that fail JSON serialization degrades to `{"role", "content": <text>}` for that message only (see `core/chat.py:_dump_message`).

Schema of one `history` entry (coreouto `Message`):

| Field | Type | Notes |
|---|---|---|
| `role` | `str` | `user` / `assistant` / `tool` (never `system`) |
| `content` | `str \| list[dict]` | str, or provider-shaped blocks (Anthropic interleaves `TextBlock`/`ToolCall` dicts) |
| `tool_calls` | `list[dict] \| null` | assistant-only: `{id, name, arguments}` (flat — coreouto `ToolCall`, **not** the OpenAI nested `function` shape) |
| `tool_call_id` | `str \| null` | tool-only: matches an assistant's `tool_calls[].id` |
| `name` | `str \| null` | tool-only: tool name (e.g. `Bash`) |

### `style/<name>.md`

Plain Markdown. Optional XML structure:

```markdown
<outo>
You are outo…
[main agent prompt]
</outo>

<subagent>
You are subagent…
[delegated agent prompt]
</subagent>
```

The `<outo>` tag is required (or the whole document is treated as the outo prompt). The `<subagent>` tag is optional — if absent, the subagent gets `core.runtime._fallback_style("subagent")` (a hardcoded minimal prompt).

`storage.styles.split_style(content) -> (outo_part, subagent_part)` does the parsing. See `docs/styles.md` for full details.

## Module API reference

### `storage.paths`

| Symbol | Type | Purpose |
|---|---|---|
| `ROOT` | `Path` | `~/.miniouto` or `$MINIOUTO_HOME` |
| `PROVIDERS_FILE` | `Path` | `ROOT/providers.toml` |
| `SETTINGS_FILE` | `Path` | `ROOT/settings.toml` |
| `STYLE_DIR` | `Path` | `ROOT/style` |
| `STYLE_REPOS_FILE` | `Path` | `ROOT/style_repos.toml` (repo URLs recorded by `style add`) |
| `SESSION_DIR` | `Path` | `ROOT/sessions` |
| `LOG_DIR` | `Path` | `ROOT/logs` |
| `ensure_dirs() -> None` | function | Create dirs + force-refresh bundled styles |

### `storage.providers`

Module-level constants:

```python
SOURCE_CUSTOM = "custom"                       # manual provider (provider custom add)
SOURCE_LMA    = "lma"                          # catalog provider (provider add)
VALID_SOURCES = (SOURCE_CUSTOM, SOURCE_LMA)
```

```python
@dataclass
class Provider:
    name: str
    api_format: str = "openai"      # validated against core.providers.SUPPORTED_FORMATS
    base_url: str = ""
    api_key: str = ""
    default_model: str = ""
    source: str = SOURCE_CUSTOM     # one of SOURCE_CUSTOM | SOURCE_LMA
    max_context_window: int | None = None   # TUI-only override of default_model's context window
    max_output_tokens: int | None = None    # TUI-only override of default_model's max output tokens
    extra: dict[str, Any] = field(default_factory=dict)
```

`source` selects which TUI model picker is used: `SOURCE_LMA` → catalog model list (`_catalog_model_picker_flow`); `SOURCE_CUSTOM` → free-text editor (`_open_custom_model_editor`). See `docs/lma.md`. Invalid `source` values on load are coerced back to `SOURCE_CUSTOM`.

`max_context_window` / `max_output_tokens` are written only by the TUI custom-model editor and read only by `core/context.py`. When set, they win over anything lma reports for the model. Both default to `None` (omitted on write). Non-positive or garbage values loaded from disk are coerced back to `None` by `_coerce_positive_int`. See `docs/lma.md` for the full precedence ladder.

| Function | Returns | Notes |
|---|---|---|
| `load_all()` | `dict[str, Provider]` | keyed by name (top-level TOML tables) |
| `get(name)` | `Provider \| None` | |
| `upsert(provider)` | `None` | overwrites by name |
| `remove(name)` | `bool` | True if a row was deleted |
| `Provider.to_dict()` | `dict` | drops `None`/`{}`/`""` |
| `Provider.from_dict(name, data)` | `Provider` | unknown keys → `extra`; invalid `source` → `SOURCE_CUSTOM` |

### `storage.settings`

```python
@dataclass
class Settings:
    provider: str = ""
    model: str = ""           # legacy per-session model override; cleared by TUI model picker
    style: str = "default"
    session: str = "default"
    theme: str = ""           # TUI theme name
```

| Function | Returns | Notes |
|---|---|---|
| `load()` | `Settings` | returns defaults if file is missing |
| `save(settings)` | `None` | atomic via `tomli_w` |
| `update(**kwargs)` | `Settings` | load → merge → save → return merged |
| `Settings.to_dict()` | `dict` | drops `None`/`""` |
| `Settings.merge(overrides)` | `Settings` | non-empty/non-None override values win |

### `storage.sessions`

```python
SCHEMA_VERSION = 2

@dataclass
class TurnRecord:                # one user→assistant exchange + its loop events
    user: str
    assistant: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)   # LoopEvent dicts
    ts: str = ""                 # auto-filled with UTC ISO seconds + "Z"

@dataclass
class SessionData:
    name: str
    history: list[dict[str, Any]] = field(default_factory=list)  # coreouto Message dumps
    turns: list[TurnRecord] = field(default_factory=list)
```

| Function | Returns | Notes |
|---|---|---|
| `path_for(name)` | `Path` | |
| `load(name)` | `SessionData` | empty on missing/corrupt; migrates v1 files on the fly |
| `save(name, data)` | `None` | full envelope rewrite |
| `record_turn(name, *, history, turn)` | `None` | load → replace `history` wholesale → append `turn` → save. The per-turn entry point used by `core/chat.py` |
| `create(name)` | `None` | touch an empty session (no-op if the file exists) |
| `clear(name)` | `None` | deletes the file |
| `list_sessions()` | `list[str]` | sorted |

`TurnRecord.to_dict()` omits an empty `events` list; `from_dict` ignores unknown keys and coerces garbage to defaults.

### `storage.styles`

| Function | Returns | Notes |
|---|---|---|
| `list_styles()` | `list[str]` | sorted filenames without `.md` |
| `read(name)` | `str \| None` | |
| `path_for(name)` | `Path` | |
| `write(name, content, *, overwrite=False)` | `Path` | no-op (returns existing path) if target exists and `overwrite=False` — does **not** raise. (This is the *style* `write`, distinct from the `tools/write.py` "refuses overwrite" tool.) |
| `add_from_repo(repo_url, *, name_override=None)` | `list[str]` | names added/updated; also records the repo URL via `record_repo` so `style update` can re-fetch it |
| `record_repo(repo_url)` | `None` | append URL to `style_repos.toml` (deduped, ordered) |
| `list_repos()` | `list[str]` | recorded repo URLs from `style_repos.toml` (`[]` if absent/malformed) |
| `builtin_default()` | `str` | seeds `~/.miniouto/style/default.md` from the bundled copy if absent, then returns its **text content** (or `""` if neither exists). Despite the legacy docstring, it returns content — not a path. |
| `write_default_style(content)` | `None` | writes `default.md` only if absent |
| `split_style(content)` | `tuple[str, str]` | `(outo, subagent)`; missing tag → whole/empty |

`add_from_repo` accepts GitHub URLs (`https://github.com/owner/repo`), GitLab URLs, or any URL whose directory listing exposes `<a href="*.md">` links. Internally dispatches to:
- `_fetch_github_tree(parsed)` — GitHub Contents API for `/style-md/`.
- `_fetch_gitlab_tree(parsed)` — GitLab Repository Tree API + raw file API.
- `_fetch_raw_index(url)` — fallback HTML directory-listing parser.

The fetcher tries three candidate URL suffixes (`/style-md/`, `/tree/main/style-md/`, `/tree/master/style-md/`) and uses the first that returns files; GitHub/GitLab path components beyond owner/repo/branch are ignored, so passing a full `https://github.com/owner/repo/tree/main/style-md` URL also works.

### `storage.skills`

See `docs/skills.md` for the full schema. Lives outside `~/.miniouto/` at `~/.agents/skills/<name>/SKILL.md`.

### `storage.toml_io`

| Function | Returns | Notes |
|---|---|---|
| `load(path)` | `dict[str, Any]` | returns `{}` if file missing |
| `save(path, data)` | `None` | creates parent dirs; uses `tomli_w` |

Tiny wrapper around stdlib `tomllib` + `tomli_w`. Kept separate so unit tests can patch it.

## Override via env

`MINIOUTO_HOME=/some/path miniouto status` will read `/some/path/settings.toml`, `/some/path/providers.toml`, etc., and write to the same tree. The bundled-style seeding still happens on first run (so a fresh `$MINIOUTO_HOME` gets the same defaults as a fresh `~/.miniouto/`).

This is the recommended way to test config changes without polluting your real home directory.
