# Development

Notes for working on miniouto itself.

## Install

```bash
# Clone and enter the project
cd /home/luke/miniouto

# Sync deps into .venv/
uv sync

# Install as a console tool (editable)
uv tool install --editable . --force

# Or: install pinned build
uv tool install .
```

`uv` is the recommended package manager (matches `uv.lock`). Standard `pip install -e .` works too but won't pick up the lockfile.

## Project structure

```
miniouto/
├── pyproject.toml          # hatchling build, entry point, deps, ruff config
├── uv.lock                 # pinned dependency lockfile
├── README.md               # user-facing quickstart
├── logo.svg
├── docs/                   # ← this directory
└── src/miniouto/           # src-layout package
    ├── cli/                # Typer commands + Textual TUI
    ├── core/               # chat loop + runtime
    ├── storage/            # filesystem persistence
    ├── tools/              # Write/Edit/Delete/Bash
    ├── default_style/      # bundled .md prompt templates
    ├── __init__.py         # __version__ only
    ├── paths_runtime.py    # INVOCATION_CWD
    ├── tui/                # EMPTY placeholder
    └── utils/              # EMPTY placeholder
```

## Python version

`>=3.10`. Uses PEP 604 union syntax (`str | None`) and `match` statements in a few places. No 3.11+ features.

## Dependencies

| Package | Min version | Role |
|---|---|---|
| `coreouto[all]` | 0.4.2 | Agent loop, providers, tools, hooks |
| `typer` | 0.12.0 | CLI framework |
| `rich` | 13.7.0 | Terminal output, tables, markdown |
| `textual` | 0.80.0 | TUI framework |
| `pydantic` | 2.0 | Data models |
| `httpx` | 0.27.0 | HTTP client (lcw-api + style fetcher) |
| `tomli-w` | 1.0.0 | TOML serializer |

The `[all]` extra on `coreouto` pulls in all four provider SDKs (openai, openai-response, anthropic, google).

## Lint & format

`pyproject.toml` configures `ruff`:

```toml
[tool.ruff]
line-length = 100
target-version = "py310"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP", "N", "SIM", "RUF"]
ignore = ["E501"]
```

Run:

```bash
ruff check src/
ruff format src/        # not configured in pyproject, but works
```

`E501` (line too long) is ignored because `coreouto`'s dataclasses and several JSON schemas produce long lines.

## Tests

**There are currently no tests.** The `src/` layout reserves space for `tests/` (referenced in `pyproject.toml`'s `tool.ruff.src`) but no test files exist.

Suggested testing setup:

```bash
uv add --dev pytest pytest-asyncio respx
mkdir -p tests
```

Suggested test priorities (in order):

1. `tools/edit.py` — exact match, ambiguity, overlap, fuzzy fallback, empty edits. (Highest value: most rules, easiest to break with refactors.)
2. `tools/write.py` — refuse overwrite, atomic write, content length cap.
3. `core/context.py` — `make_summarize_hook`'s non-list guard. The divergence from `coreouto.contrib.hooks.auto_summarize_hook` is the most fragile bit.
4. `storage/styles.py` — `split_style` regex edge cases (nested tags, missing tags, tag in content).
5. `core/chat.py` — `ToolCallArgsError` path; `_dump_failure_diagnostics` output.
6. `cli/provider.py` — `add`/`list`/`remove`/`default` happy + sad paths.

`respx` is recommended for mocking `httpx` calls to `lcw-api` and the style repo fetchers.

## Build

```bash
uv build          # produces dist/miniouto-0.1.0-py3-none-any.whl
```

Build backend: `hatchling`. Wheel packages: `["src/miniouto"]`.

## Release

```bash
# Bump version in pyproject.toml and src/miniouto/__init__.py
$EDITOR pyproject.toml src/miniouto/__init__.py

# Build
uv build

# Publish (one-time uv auth required)
uv publish
```

Tagging follows `vX.Y.Z` semver. There's no CI configured yet.

## Entry point

```toml
[project.scripts]
miniouto = "miniouto.cli:app"
```

After `uv tool install --editable .`, the `miniouto` command is on `$PATH` and dispatches to `cli/__init__.py:app`.

## Debugging tips

### I want to see what's happening inside `run_chat`

The `BEFORE_TOOL_CALL` hook prints tool traces to stderr (via `rich.Console(stderr=True)`). Run:

```bash
miniouto chat "list the files" 2>debug.log
```

### I want to inspect the resolved runtime

Add a `print` at the end of `core/runtime.py:build_runtime` to dump `outo_config` and `subagent_config` dicts.

### I want to test a config without polluting `~/.miniouto/`

```bash
MINIOUTO_HOME=/tmp/miniouto-test miniouto status
MINIOUTO_HOME=/tmp/miniouto-test miniouto provider add --name openai --format openai
```

### I want to see which style the model sees

Add a `print(prompt)` at the end of `core/runtime.py:build_runtime` (or wrap the call to `co.register_agent_preset("outo", ...)` to log the system prompt).

### I want to test a style edit

```bash
$EDITOR ~/.miniouto/style/default.md
miniouto chat "hello"   # next turn uses the new prompt
```

### TUI is misbehaving

The TUI uses Textual's dev mode for debugging:

```bash
TEXTUAL_DEVTOOLS=1 miniouto
```

Then visit the printed URL.

## Known sharp edges (the easy-to-miss stuff)

1. **Dead code in `core/runtime.py` lines ~102–109** — a duplicate `async def wrapped` block appears *after* a `return co.Tool(...)` and is unreachable. The real wrapping happens later via `_wrap_subagent_handler`. Worth deleting as a cleanup PR.
2. **No tests directory exists.** Don't assume one is being created behind the scenes.
3. **Four of the six bundled styles** describe a `claude.md` / `codex.md` / `oh-my-opencode.md` / `opencode.md` CWD memory file. **No such loader exists in miniouto.** Either implement it or edit the styles to remove the misleading references.
4. **`tui/` and `utils/` are empty.** They look like package directories but contain no code. Don't add modules there without first deciding whether the contents should be moved into the proper package (TUI lives in `cli/tui.py`, utilities are scattered).
5. **`storage/skills.py` is not in `storage/__init__.py`'s `__all__`** — it's imported directly via `from ..storage import skills as skill_store`. Don't add it to `__all__` without auditing the import sites first.
6. **The 12-byte file `    ` (four spaces) at repo root** is a stray editor artifact, not a project file. Safe to delete.
7. **`continue_loop` tool is referenced in every bundled style** but **not** registered in `tools/registry.py`. Models currently improvise. To enable, register a no-op `continue_loop` handler and add the name to `core/runtime.ALL_TOOLS`.
8. **`core/runtime.py:_SUBAGENT_DEPTH` is a module-level `ContextVar`** that is set inside `_wrap_subagent_handler`. If you add new async tools that themselves call subagents (recursive delegation), make sure they go through the wrapper or the depth tracking will be wrong.

## Common modifications

### "Add a new CLI subcommand"

1. Create `src/miniouto/cli/<name>.py` with `app = typer.Typer(...)`.
2. Add `app.add_typer(<name>_module.app, name="<name>")` in `cli/__init__.py`.
3. Document in `docs/cli.md`.

### "Add a new tool"

See `docs/tools.md` § "Adding a new tool".

### "Add a new bundled style"

1. Create `src/miniouto/default_style/<name>.md` (follow the `<outo>` / `<subagent>` structure).
2. It will be auto-seeded into `~/.miniouto/style/` on first run for new installs.
3. Document in `docs/styles.md`.

### "Add a new provider format"

1. Add the format string to `core/providers.py:SUPPORTED_FORMATS`.
2. Add an `_instantiate` branch with the right `coreouto` provider class import + constructor call.
3. Update the `cli/provider.py:FORMAT_HELP` text.
4. Document in `docs/cli.md` and `docs/core.md`.

### "Change the storage root"

Only one place: `src/miniouto/storage/paths.py`. Update the `ROOT` constant to read from a different env var, and the entire storage layer follows.

## Style notes for PRs

- Match the existing code style: 4-space indents, dataclasses for value objects, `rich` for terminal output, type hints everywhere (including return types).
- Don't add new top-level dependencies without discussion. `coreouto`, `typer`, `rich`, `textual`, `pydantic`, `httpx`, `tomli-w` is the entire runtime footprint.
- Don't refactor while fixing — open separate PRs.
- If you change a tool's behavior, update the corresponding `_<name>_description` in `tools/registry.py` (the description is what the LLM sees).
- If you change a style's structure, re-test all six bundled templates for consistency.
