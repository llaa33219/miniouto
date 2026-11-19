# Development

Notes for working on miniouto itself.

## Development environment

Two rules, both non-negotiable:

**1. Always work inside the uv-managed venv.** `uv sync` creates/updates `.venv/`, and every command goes through it:

```bash
uv sync
uv run pytest
uv run miniouto status
# or call the venv's binaries directly:
.venv/bin/python -m pytest
.venv/bin/miniouto chat "hello"
```

Never run a system `python`, `pip install -e .`, or a bare `pytest` against this checkout. The dependency resolution (including `coreouto[all]` and `py-Vwayland`) is defined by `uv.lock` and only exists inside `.venv/`; a system interpreter silently runs against a different, unpinned set of packages.

**2. Keep `uv.lock` fresh and commit it.** Whenever `pyproject.toml` dependencies change:

```bash
uv lock                          # re-resolve everything
uv lock --upgrade-package typer  # refresh a single package
uv sync                          # apply the new lock to .venv/
```

Commit `uv.lock` **in the same change** as the `pyproject.toml` edit — a stale lockfile silently reinstalls the old resolution on the next `uv sync`. Never hand-edit `uv.lock`.

To lint, use `uvx` (ruff is intentionally *not* in the project's dependency groups, so `uv run ruff` will not resolve):

```bash
uvx ruff check src/ tests/
```

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

`uv` is the required package manager (it owns `uv.lock`). A plain `pip install -e .` works for a one-off smoke test but ignores the lockfile and the venv — don't develop that way.

## Project structure

```
miniouto/
├── pyproject.toml          # hatchling build, entry point, deps, ruff config
├── uv.lock                 # pinned dependency lockfile
├── README.md               # user-facing quickstart
├── logo.svg
├── docs/                   # ← this directory
└── src/miniouto/           # src-layout package
    ├── cli/                # Typer commands + xnano TUI (chat.py, message.py, computer.py, tui/)
    ├── core/               # chat loop + runtime (incl. inbox.py, spawn_state.py)
    ├── storage/            # filesystem persistence
    ├── tools/              # Bash + media loaders + Computer
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
| `coreouto[all]` | 0.11.3 | Agent loop, providers, tools, hooks |
| `typer` | 0.12.0 | CLI framework |
| `rich` | 13.7.0 | Terminal output, tables, markdown |
| `xnano[image]` | ==2.3.0 | The TUI substrate: a retained-mode canvas. **Pinned exactly** — the API churns between minor versions, and `cli/tui/{pt,graphics,app}.py` are written against this build |
| `xnano-core` | ==2.2.3 | Styles/colors, input event types, the cell grid (`cell_grid_to_ansi`). Pinned |
| `xnano-image` | ==0.2.0 | Kitty/Sixel encoding for Computer screenshots; a Rust extension, no Python deps. Pinned |
| `pydantic` | 2.0 | Data models |
| `httpx` | 0.27.0 | HTTP client (modelslist REST client + style repo fetcher + TUI update check) |
| `packaging` | 23.0 | PEP 440 version comparison for the TUI update check |
| `tomli-w` | 1.0.0 | TOML serializer |
| `py-Vwayland` | 0.1.0 | Bundled virtual-display compositor behind the Computer tool |

The `[all]` extra on `coreouto` pulls in all four provider SDKs (openai, openai-response, anthropic, google).

The `dev` dependency group holds `pytest` and `pillow`: Pillow is **test-fixture only** — the graphics tests author real PNGs to measure placements against. Nothing in `src/` imports PIL, and the runtime image lane needs no Python imaging library because `xnano-image` is a Rust extension.

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
uvx ruff check src/ tests/
```

Use `uvx`, not `uv run ruff` — ruff is not in the `[dependency-groups]` in `pyproject.toml`, so `uv run ruff` fails to resolve. `ruff format` is not configured in `pyproject` but works (`uvx ruff format src/`).

`E501` (line too long) is ignored because `coreouto`'s dataclasses and several JSON schemas produce long lines.

## Tests

The suite is ~1480 tests and must stay green:

```bash
uv run pytest
uv run pytest tests/test_chat_delegation.py -v
```

Where to start reading, in rough order of usefulness as orientation:

1. `tests/test_inbox.py` — the cross-process message store every alarm and `miniouto message insert` goes through.
2. `tests/test_chat_delegation.py` — the delegation flag surface, answer files, alarm text, `--bg` argv reconstruction.
3. `tests/test_tui_navigation.py` — TUI paging, jump-to-bottom, and the width-capped chat column.
4. `tests/test_message_cli.py`, `tests/test_computer_art.py`, `tests/test_ascii_art.py` — the CLI surfaces.
5. `tests/test_styles.py` — `parse_style` edge cases (missing `<outo>`, nested tags, legacy `<subagent>`, duplicate names, zero-role styles).
6. `tests/test_no_call_subagent.py` — guards the deleted in-process delegation tool; keep it passing.

`respx` is useful for mocking `httpx` calls to `modelslist.blp.sh` and the style repo fetchers if you extend the suite.

## Build

```bash
uv build          # produces dist/miniouto-<version>-py3-none-any.whl (+ sdist)
```

Build backend: `hatchling`. Wheel packages: `["src/miniouto"]`. The console script is `miniouto = "miniouto.cli:app"`, so the built wheel installs a `miniouto` command.

## Release

Releases are published automatically by `.github/workflows/release.yml`. Push a tag and CI handles the rest:

```bash
# 1. Bump version in pyproject.toml (keep src/miniouto/__init__.py in sync if it ever diverges)
$EDITOR pyproject.toml

# 2. Commit and tag
git commit -am "v0.12.9"
git tag v0.12.9
git push origin v0.12.9
```

The workflow triggers on any `v*` tag. It builds the wheel + sdist with `uv build`, then publishes to PyPI using OIDC trusted publishing (no API token in the repo).

### One-time PyPI setup (trusted publisher)

If this is the first release, register the trusted publisher once on PyPI:
<https://docs.pypi.org/trusted-publishers/adding-a-publisher/>

- PyPI Project Name: `miniouto`
- Owner: the GitHub user/org that owns this repo
- Repository name: `miniouto`
- Workflow filename: `release.yml`
- Environment name: `pypi`

The workflow file already declares `environment: pypi` and `permissions: id-token: write`, so once the publisher is registered the first `git push origin vX.Y.Z` will publish.

### Manual publish (fallback)

If you ever need to publish by hand (e.g. CI is broken):

```bash
uv build
uv publish   # one-time `uv publish` auth via browser
```

(Local-only builds without publishing are covered in [Build](#build) above.)

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

Add a `print` at the end of `core/runtime.py:build_runtime` to dump the resolved `outo_config` (there is no second `subagent_config` anymore — a spawned run resolves its own runtime in its own process).

### I want to test a config without polluting `~/.miniouto/`

```bash
MINIOUTO_HOME=/tmp/miniouto-test miniouto status
MINIOUTO_HOME=/tmp/miniouto-test miniouto provider custom add --name openai --format openai
```

### I want to see which style the model sees

Add a `print(prompt)` at the end of `core/runtime.py:build_runtime` (or wrap the call to `co.register_agent_preset("outo", ...)` to log the system prompt).

### I want to test a style edit

```bash
$EDITOR ~/.miniouto/style/default.md
miniouto chat "hello"   # next turn uses the new prompt
```

### TUI is misbehaving

The TUI has no dev-mode server (nor does its `xnano` substrate). Debug it through the **headless harness** instead — the same `nodes.py:run_test` / `Pilot` path the behavioural suite uses, which boots a real `ChatTUI` against a fixed screen size and lets you read the composited frame:

```bash
uv run python - <<'PY'
import asyncio
import sys
sys.path.insert(0, "tests")
from tui_harness import BOOT_SIZE            # (120, 32), the pty baseline
from miniouto.cli.tui import ChatTUI


async def main():
    async with ChatTUI().run_test(size=BOOT_SIZE) as pilot:
        print(pilot.app.screen._compositor.render_strips()[0].text)


asyncio.run(main())
PY
```

For a live session, run the real thing under a pty (`script -q /dev/null miniouto` is the cheapest recorder) and read the captured bytes; `MINIOUTO_HOME=/tmp/scratch miniouto` keeps the run out of your real `~/.miniouto/`. `uv run pytest tests/test_tui_*.py` re-runs the behavioural suite against the same headless path.

## Known sharp edges (the easy-to-miss stuff)

1. **Test coverage is uneven.** `tests/` covers inbox, delegation, message CLI, computer art, TUI navigation, styles, the summary hand-off (`test_handoff.py`), and a set of runtime/storage cases, plus the regression suite for the five provider-settings issues (`tests/test_issue_fixes.py`); `tools/bash.py` is still untested. Run `uv run pytest` before assuming a change is safe.
2. **`tui/` and `utils/` are empty.** They look like package directories but contain no code. Don't add modules there without first deciding whether the contents should be moved into the proper package (TUI lives in the `cli/tui/` package, utilities are scattered).
3. **`storage/skills.py` is not in `storage/__init__.py`'s `__all__`** — it's imported directly via `from ..storage import skills as skill_store`. Don't add it to `__all__` without auditing the import sites first.
4. **There is no `continue_loop` tool, and there never should be.** coreouto's termination policy already covers progress updates: a response carrying text AND a tool call keeps the loop going and shows that text, while text with NO tool call is the final answer. So a progress update is just "say something while calling a tool" — the bundled styles and `_fallback_style` state exactly that. An earlier draft taught the model to call a `continue_loop` tool that was never registered, which made it invent calls that never executed; that reference has been removed everywhere (styles, docs, `_fallback_style`).
5. **There is no in-process subagent engine.** The old `core/subagents.py` job registry, its `ContextVar`s, and the `call_subagent` tool are gone — `tests/test_no_call_subagent.py` guards against the tool returning. Delegation is `miniouto chat "<brief>" --style <style> --role <tag>` (see `cli/chat.py`) plus the cross-process inbox (`core/inbox.py`) — there is no second command and no second runtime path. A delegated run's bookkeeping rides on the inbox message's `meta` (`id`, `role`), not on ContextVars, so there is nothing in-process to attribute; the run gets the full `supervised_run` treatment inside its own process.
6. **`tools/registry.py:_register_if_missing` accepts but silently discards the `schema` parameter** — the `_xxx_schema()` dicts are computed at registration time but never passed to `coreouto.register_tool`. Only the handler's Python type hints and the `description` string reach the model. The schema dicts are effectively dead code; do not rely on them affecting model behavior.

## Common modifications

### "Add a new CLI subcommand"

1. Create `src/miniouto/cli/<name>.py` with `app = typer.Typer(...)`.
2. Add `app.add_typer(<name>_module.app, name="<name>")` in `cli/__init__.py`.
3. Document in `docs/cli.md`.

### "Add a new tool"

See `docs/tools.md` § "Adding a new tool".

### "Add a new bundled style"

1. Create `src/miniouto/default_style/<name>.md` (follow the tag-as-name structure: one top-level `<outo>...</outo>` block plus zero or more `<name>...</name>` blocks — one per named subagent).
2. It will be auto-seeded into `~/.miniouto/style/` on first run for new installs.
3. Document in `docs/styles.md`.

### "Add a new provider format"

1. Add the format string to `core/providers.py:SUPPORTED_FORMATS`.
2. Add an `_instantiate` branch with the right `coreouto` provider class import + constructor call.
3. If modelslist can advertise it, add its `formats[].format` id to `DIALECT_TO_FORMAT` so the catalog wizard can select it (`typesafe` stays absent — it is not an LLM wire protocol).
4. Update the `cli/provider.py:FORMAT_HELP` text.
5. Document in `docs/cli.md`, `docs/core.md` and `docs/modelslist.md`.

### "Change the storage root"

Only one place: `src/miniouto/storage/paths.py`. Update the `ROOT` constant to read from a different env var, and the entire storage layer follows.

## Style notes for PRs

- Match the existing code style: 4-space indents, dataclasses for value objects, `rich` for terminal output, type hints everywhere (including return types).
- Don't add new top-level dependencies without discussion. `coreouto`, `typer`, `rich`, `xnano`, `xnano-core`, `xnano-image`, `pydantic`, `httpx`, `packaging`, `tomli-w`, `py-Vwayland` is the entire runtime footprint.
- Don't refactor while fixing — open separate PRs.
- If you change a tool's behavior, update the corresponding `_<name>_description` in `tools/registry.py` (the description is what the LLM sees).
- If you change a style's structure, re-test all five bundled templates for consistency.
