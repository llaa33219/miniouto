# Storage Layer

The `storage/` subpackage is the **only layer in miniouto that reads from or writes to the filesystem** (apart from the `tools/` subpackage, which mutates files as part of executing agent actions). Everything is human-inspectable plain text: TOML for config, Markdown for styles, JSON for session history.

All paths are rooted at `~/.miniouto/` and can be overridden via the `MINIOUTO_HOME` environment variable.

## Filesystem layout

```
~/.miniouto/                      (or $MINIOUTO_HOME)
├── providers.toml                # provider configurations
├── settings.toml                 # active provider / style / session
├── style/
│   ├── default.md                # seeded on first run from src/miniouto/default_style/default.md
│   ├── claude.md                 # seeded
│   ├── codex.md                  # seeded
│   ├── opencode.md               # seeded
│   ├── oh-my-opencode.md         # seeded
│   └── codebuff.md               # seeded
├── sessions/
│   └── <name>.json               # conversation history per session
└── logs/                         # reserved (currently unused by code)
```

Skills live **outside** `~/.miniouto/` at `~/.agents/skills/<name>/SKILL.md` (the Anthropic convention). See `docs/skills.md`.

## Path constants

Defined in `src/miniouto/storage/paths.py`:

```python
ROOT           = Path(os.environ.get("MINIOUTO_HOME") or Path.home() / ".miniouto").expanduser()
PROVIDERS_FILE = ROOT / "providers.toml"
SETTINGS_FILE  = ROOT / "settings.toml"
STYLE_DIR      = ROOT / "style"
SESSION_DIR    = ROOT / "sessions"
LOG_DIR        = ROOT / "logs"
```

`ensure_dirs()` (called by the CLI root callback before every command):
1. Creates `ROOT`, `STYLE_DIR`, `SESSION_DIR`, `LOG_DIR` if missing.
2. For every `*.md` in the bundled `src/miniouto/default_style/` package data, copies it into `STYLE_DIR/` **only if** the target file is absent (won't clobber user edits).

## File format schemas

### `providers.toml`

```toml
# Miniouto provider registry
# One TOML table per provider. Created/updated via `miniouto provider add`.

[providers.openai]
api_format = "openai"          # one of: openai, openai-response, anthropic, google
base_url = ""                  # default provider URL
api_key = ""                   # may be empty (read from env at call time)
default_model = "gpt-5.5"      # used when chat --model is not given

[providers.anthropic]
api_format = "anthropic"
base_url = ""
api_key = ""
default_model = "claude-opus-4.5"
```

The on-disk key is always `providers.<name>` (regardless of nested structure); the loader flattens this. The `Provider` dataclass in `storage/providers.py` has an `extra: dict` field for unknown keys — they're preserved through round-trips so future fields won't be lost.

### `settings.toml`

```toml
provider = "openai"     # default provider name (must match a key in providers.toml)
style    = "default"    # default style (must exist in ~/.miniouto/style/)
session  = "default"    # default session name (used when chat --name is not given)
```

All three keys are optional. Missing keys fall back to: empty string for `provider`, `"default"` for `style`, `"default"` for `session`.

The `Settings` dataclass in `storage/settings.py` exposes `merge(overrides) -> Settings` — non-empty/non-None override values win.

### `sessions/<name>.json`

```json
{
  "session": "<name>",
  "updated": "2026-06-20T10:34:12Z",
  "messages": [
    {
      "role": "user",
      "content": "hello",
      "ts": "2026-06-20T10:34:00Z"
    },
    {
      "role": "assistant",
      "content": "Hi! How can I help?",
      "tool_calls": [
        {
          "id": "call_abc",
          "type": "function",
          "function": {"name": "Bash", "arguments": "{\"command\":\"ls\"}"}
        }
      ],
      "ts": "2026-06-20T10:34:11Z"
    },
    {
      "role": "tool",
      "tool_call_id": "call_abc",
      "name": "Bash",
      "content": "file1.py\nfile2.py\n",
      "ts": "2026-06-20T10:34:12Z"
    }
  ]
}
```

Schema (from `storage/sessions.py:MessageRecord`):

| Field | Type | Notes |
|---|---|---|
| `role` | `str` | `system` / `user` / `assistant` / `tool` |
| `content` | `str \| None` | text content (may be empty string for tool calls) |
| `tool_calls` | `list[dict]` | assistant-only: list of `{id, type, function: {name, arguments}}` |
| `tool_call_id` | `str \| None` | tool-only: matches an assistant's `tool_calls[].id` |
| `name` | `str \| None` | tool-only: tool name (e.g. `Bash`, `Write`) |
| `ts` | `str` | UTC ISO-8601 timestamp, `Z`-suffixed |

`to_dict()` drops fields with `None` / `[]` / `""` to keep the JSON compact. `ts` is auto-set on construction.

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
| `SESSION_DIR` | `Path` | `ROOT/sessions` |
| `LOG_DIR` | `Path` | `ROOT/logs` |
| `ensure_dirs() -> None` | function | Create dirs + seed bundled styles |

### `storage.providers`

```python
@dataclass
class Provider:
    name: str
    api_format: str = "openai"      # validated against core.providers.SUPPORTED_FORMATS
    base_url: str = ""
    api_key: str = ""
    default_model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
```

| Function | Returns | Notes |
|---|---|---|
| `load_all()` | `dict[str, Provider]` | keyed by name |
| `get(name)` | `Provider \| None` | |
| `upsert(provider)` | `None` | overwrites by name |
| `remove(name)` | `bool` | True if a row was deleted |

### `storage.settings`

```python
@dataclass
class Settings:
    provider: str = ""
    style: str = "default"
    session: str = "default"
```

| Function | Returns | Notes |
|---|---|---|
| `load()` | `Settings` | returns defaults if file is missing |
| `save(settings)` | `None` | atomic via tomli_w |
| `update(**kwargs)` | `Settings` | load → merge → save → return merged |

### `storage.sessions`

```python
@dataclass
class MessageRecord:
    role: str
    content: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    ts: str = ""  # auto-filled with UTC ISO seconds + "Z"
```

| Function | Returns | Notes |
|---|---|---|
| `path_for(name)` | `Path` | |
| `load(name)` | `list[MessageRecord]` | `[]` if missing |
| `save(name, messages)` | `None` | overwrites |
| `append(name, message)` | `list[MessageRecord]` | load + append + save, returns full list |
| `clear(name)` | `None` | deletes the file |
| `list_sessions()` | `list[str]` | sorted |
| `to_coreouto_messages(messages)` | `list[dict]` | for external consumers (not the main chat path) |

### `storage.styles`

| Function | Returns | Notes |
|---|---|---|
| `list_styles()` | `list[str]` | sorted filenames without `.md` |
| `read(name)` | `str \| None` | |
| `path_for(name)` | `Path` | |
| `write(name, content, *, overwrite=False)` | `Path` | raises if exists and `overwrite=False` |
| `add_from_repo(repo_url, *, name_override=None)` | `list[str]` | names added/updated |
| `builtin_default()` | `str` | path to seeded `default.md` (seeded if necessary) |
| `write_default_style(content)` | `None` | writes `default.md` only if absent |
| `split_style(content)` | `tuple[str, str]` | `(outo, subagent)`; missing tag → whole/empty |

`add_from_repo` accepts GitHub URLs (`https://github.com/owner/repo`), GitLab URLs, or any URL whose directory listing exposes `<a href="*.md">` links. Internally dispatches to:
- `_fetch_github_tree(parsed)` — GitHub Contents API for `/style-md/`.
- `_fetch_gitlab_tree(parsed)` — GitLab Repository Tree API + raw file API.
- `_fetch_raw_index(url)` — fallback HTML directory-listing parser.

URLs ending in `/tree/main/style-md` or `/tree/master/style-md` are normalized to the parent repo URL before fetching.

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
