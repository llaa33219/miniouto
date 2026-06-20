# miniouto Documentation

A minimal, file-driven CLI agent harness built on [`coreouto`](https://github.com/llaa33219/coreouto).

This directory contains a complete reference for the project. Read [`architecture.md`](./architecture.md) first to get the mental model, then jump to whichever layer you need to modify.

## Index

| File | Covers |
|---|---|
| [`architecture.md`](./architecture.md) | High-level design: package layout, dependency graph, runtime data flow, key invariants. **Read this first.** |
| [`cli.md`](./cli.md) | Every CLI command, flag, exit code, and configuration path. The user-facing surface. |
| [`storage.md`](./storage.md) | `~/.miniouto/` filesystem layout, TOML/JSON schemas, style/skills storage. The persistence layer. |
| [`core.md`](./core.md) | Chat loop, `RuntimeConfig` resolution, provider construction, context-window management, subagent dispatch. |
| [`tools.md`](./tools.md) | The Write / Edit / Delete / Bash tools — handlers, schemas, edit rules, fuzzy fallback. |
| [`styles.md`](./styles.md) | Style document format, `<outo>` / `<subagent>` tags, the six bundled templates. |
| [`skills.md`](./skills.md) | Skill discovery from `~/.agents/skills/`, frontmatter schema. |
| [`development.md`](./development.md) | Install, build, lint, release, contributing notes, known issues. |

## Source map

Every file in `src/miniouto/`:

```
src/miniouto/
├── __init__.py              # __version__ only
├── paths_runtime.py         # INVOCATION_CWD (captured cwd at import)
├── cli/                     # Typer commands + Textual TUI  → see cli.md
│   ├── __init__.py          # app, console, _root callback, status command
│   ├── chat.py              # chat_cmd
│   ├── provider.py          # provider add/list/remove/default
│   ├── style.py             # style list/set/add/show
│   ├── skill.py             # skill list/show
│   └── tui.py               # StatusBar, ChatTUI, run_tui()
├── core/                    # Chat loop + runtime assembly   → see core.md
│   ├── __init__.py          # re-exports chat, providers, runtime
│   ├── chat.py              # ChatOptions, run_chat, ToolCallArgsError, diagnostics
│   ├── context.py           # lcw-api fetcher, make_summarize_hook
│   ├── providers.py         # SUPPORTED_FORMATS, build_coreouto_provider
│   └── runtime.py           # RuntimeConfig, build_runtime, subagent tool
├── storage/                 # Filesystem persistence         → see storage.md
│   ├── __init__.py
│   ├── paths.py             # ROOT, PROVIDERS_FILE, …, ensure_dirs()
│   ├── providers.py         # Provider dataclass + TOML CRUD
│   ├── sessions.py          # MessageRecord + JSON CRUD
│   ├── settings.py          # Settings dataclass + TOML CRUD
│   ├── skills.py            # Skill dataclass + ~/.agents/skills/ discovery
│   ├── styles.py            # style CRUD + add_from_repo + split_style
│   └── toml_io.py           # tiny tomllib + tomli_w wrapper
├── tools/                   # File/bash tools                 → see tools.md
│   ├── __init__.py
│   ├── _normalize.py        # smart-quote/dash/NBSP/zero-width normalization
│   ├── bash.py              # async bash(command, …)
│   ├── delete.py            # delete(file_path)
│   ├── edit.py              # edit(file_path, edits)
│   ├── write.py             # write(file_path, content)
│   └── registry.py          # register_all() — wires tools into coreouto
├── default_style/           # Bundled .md prompts             → see styles.md
│   ├── default.md           # minimal fallback
│   ├── claude.md            # Claude Code-style
│   ├── codex.md             # OpenAI Codex CLI-style
│   ├── opencode.md          # OpenCode-style
│   ├── oh-my-opencode.md    # Sisyphus orchestrator
│   └── codebuff.md          # Buffy orchestrator
├── tui/                     # EMPTY placeholder (TUI lives in cli/tui.py)
└── utils/                   # EMPTY placeholder
```

## Conventions used in this documentation

- **Absolute paths** are shown as `/home/luke/miniouto/src/miniouto/...`.
- **External runtime root** is `~/.miniouto/` (overridable via the `MINIOUTO_HOME` env var).
- **The agent is called `outo`**. The delegable nested agent is called `subagent`.
- Code snippets come from the actual source. Module docstrings are reproduced when present.
- When a behavior depends on a flag, the flag is named in backticks (e.g. `` `chat --continue` ``).
