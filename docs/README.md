# miniouto Documentation

A minimal, file-driven CLI agent harness built on [`coreouto`](https://github.com/llaa33219/coreouto).

This directory contains a complete reference for the project. Read [`architecture.md`](./architecture.md) first to get the mental model, then jump to whichever layer you need to modify.

## Index

| File | Covers |
|---|---|
| [`architecture.md`](./architecture.md) | High-level design: package layout, dependency graph, runtime data flow, key invariants. **Read this first.** |
| [`cli.md`](./cli.md) | Every CLI command, flag, exit code, and configuration path — `chat` (turn + delegation), `message`, `computer show`, `provider`/`style`/`skill`/`subagent`, TUI. The user-facing surface. |
| [`usage.md`](./usage.md) | **Cookbook of CLI command examples.** chat output modes, provider/style/skill usage patterns, TUI controls, pipeline recipes. |
| [`automation.md`](./automation.md) | **Non-interactive automation reference.** Alarm monitoring & wakeup (insert / observe / wake, polling loops, liveness markers) and history extraction from session files (`jq` recipes), plus post-install auto-setup, `MINIOUTO_HOME`, chat output parsing, auto-registering providers/styles/skills, CI/Docker/cron patterns. |
| [`storage.md`](./storage.md) | `~/.miniouto/` filesystem layout, TOML/JSON schemas, style/skills storage, and the project-local `./.miniouto/summary/` tree. The persistence layer. |
| [`core.md`](./core.md) | Chat loop, `RuntimeConfig` resolution, provider construction, context-window management, the explicit summarization + session hand-off, role mode, the inbox alarm hook, watchdogs, the cross-process inbox and spawn liveness markers. |
| [`tools.md`](./tools.md) | The Bash + media + Computer + `continue_session` tools — handlers, schemas, multimodal blocks, the `.art` screen-art channel, and why delegation is a Bash command. |
| [`styles.md`](./styles.md) | Style document format (tag-as-name grammar: `<outo>` + role tags selected by `chat --role`), the five bundled templates. |
| [`skills.md`](./skills.md) | Skill discovery from `./skills/` (project-local) and `~/.agents/skills/`, frontmatter schema. |
| [`lma.md`](./lma.md) | lma (llm-model-api) integration: provider/model discovery, context caps, the `provider providers/models/add` CLI commands, TUI add flows. |
| [`development.md`](./development.md) | Install, build, lint, release, contributing notes, known issues. |

## Source map

Every file in `src/miniouto/`:

```
src/miniouto/
├── __init__.py              # __version__ only
├── paths_runtime.py         # INVOCATION_CWD (captured cwd at import)
├── cli/                     # Typer commands + Textual TUI  → see cli.md
│   ├── __init__.py          # app, console, _root callback, status command
│   ├── chat.py              # chat_cmd + run_turn_chain (ONE turn + summary hand-off transitions; --alarms wakeup) + the delegation post-step (--bg detach)
│   ├── message.py           # message insert/list/clear (the CLI side of core/inbox.py)
│   ├── computer.py          # computer show [PATH] — renders a .art capture
│   ├── provider.py          # provider providers/models/add + custom add + list/remove/default
│   ├── style.py             # style list/set/add/update/show
│   ├── skill.py             # skill list/show
│   ├── subagent.py          # subagent show/set/clear (what `chat --use-subagent-provider` reads)
│   └── tui.py               # ChatTUI, run_tui(), model/provider pickers, catalog/custom add wizards
├── core/                    # Chat loop + runtime assembly   → see core.md
│   ├── __init__.py          # re-exports chat, events, lma, providers, runtime
│   ├── chat.py              # ChatOptions, run_chat, ToolCallArgsError, diagnostics, sink dispatchers
│   ├── context.py           # lma fetcher (caps) + make_context_monitor (the summary trigger)   → see core.md
│   ├── events.py            # LoopEvent, EventSink protocol, NullSink, ConsoleEventSink
│   ├── error_rules.py       # per-format ErrorRule lists for provider error_handling (coreouto >= 0.10)
│   ├── lma.py               # lma REST client + slugify          → see lma.md
│   ├── providers.py         # SUPPORTED_FORMATS, sdk_to_format, build_coreouto_provider
│   ├── reasoning.py         # reasoning choice → provider_passthrough
│   ├── repetition.py        # repetition watchdog (phrase / tool loops)   → see core.md
│   ├── runtime.py           # RuntimeConfig, build_runtime (role mode), inbox alarm hook, watchdogs, history repair
│   ├── summary.py           # explicit summarization contract: the two pinned messages, hand-off state, run contract   → see core.md
│   ├── inbox.py             # cross-process message inbox (ALARM_HEADER, insert/drain/prune)
│   └── spawn_state.py       # liveness markers for detached `chat --bg` runs (pid-aware pending_spawns)
├── storage/                 # Filesystem persistence         → see storage.md
│   ├── __init__.py
│   ├── paths.py             # ROOT, PROVIDERS_FILE, …, ensure_dirs()
│   ├── providers.py         # Provider dataclass + TOML CRUD (with `source` field)
│   ├── sessions.py          # SessionData/TurnRecord (schema v2, `cwd`) + JSON CRUD
│   ├── settings.py          # Settings dataclass (provider/model/style/session/theme) + TOML CRUD
│   ├── skills.py            # Skill dataclass + ./skills/ then ~/.agents/skills/ discovery
│   ├── styles.py            # style CRUD + add_from_repo + record_repo/list_repos + parse_style + SubagentSpec
│   └── toml_io.py           # tiny tomllib + tomliw wrapper
├── tools/                   # Bash + media + computer tools  → see tools.md
│   ├── __init__.py
│   ├── bash.py              # async bash(command, …)
│   ├── media.py             # load_image/load_video/load_audio → LoadedMedia
│   ├── computer.py          # computer(action, …) — virtual-display GUI control, incl. action="screen" → .art
│   ├── continue_session.py  # continue_session(summary_dir) — the only way a session changes   → see core.md
│   ├── ascii_art.py         # pure-stdlib PNG decode + sub-cell render (█/BLOCK OCTANT/BRAILLE) + the .art format
│   └── registry.py          # register_all(api_format=…) — wires tools into coreouto
├── default_style/           # Bundled .md prompts             → see styles.md
│   ├── default.md           # minimal fallback
│   ├── coding.md            # expert coding agent, minimal
│   ├── coding-pro.md        # senior staff engineer orchestrator
│   ├── coding-work.md        # pragmatic senior engineer orchestrator
│   └── coding-ultra.md      # MAX-mode execution orchestrator
├── tui/                     # EMPTY placeholder (TUI lives in cli/tui.py)
└── utils/                   # EMPTY placeholder
```

## Conventions used in this documentation

- **Absolute paths** are shown as `/home/luke/miniouto/src/miniouto/...`.
- **External runtime root** is `~/.miniouto/` (overridable via the `MINIOUTO_HOME` env var). One exception: summary documents live **project-local** at `<cwd>/.miniouto/summary/<session>/`, because they are artifacts of the work, not harness state.
- **Every agent is called `outo`** — including a delegated `miniouto chat "…" --style <style> --role <tag>` run, which is a separate process with its own transcript. Roles are named by their style tag (a legacy `<subagent>` block keeps the name `subagent`).
- Code snippets come from the actual source. Module docstrings are reproduced when present.
- When a behavior depends on a flag, the flag is named in backticks (e.g. `` `chat --continue` ``).
