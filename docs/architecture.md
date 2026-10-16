# Architecture

## Purpose

`miniouto` is a **thin orchestration layer** on top of the [`coreouto`](https://github.com/llaa33219/coreouto) agent library. It provides:

1. A CLI (`miniouto`) and an optional Textual TUI for interactive use.
2. File-driven configuration (TOML for providers/settings, Markdown for styles, JSON for sessions).
3. Bundled agent "style" templates (three personas: a minimal default and two orchestrators).
4. A minimal tool surface (Bash + Image/Video/Audio media viewers, plus `Computer` when the platform supports it) and **delegation as a Bash command** — `miniouto chat "<brief>" --style <style> --role <tag>`, run by the model through `Bash`.
5. Persistence of session history.
6. Per-turn diagnostic output to stderr.
7. A cross-process message inbox (`inbox/<session>/`) so a detached run can talk back to the session that launched it.

The harness is **stateless across invocations except for what's persisted to `~/.miniouto/`**. No server, no daemon thread. Every CLI run is a fresh Python process (except TUI mode, which is one long-lived process), and **delegation is a second process, not a thread**: the model runs `miniouto chat "<brief>" --style <style> --role <tag>` through `Bash`, and that child builds its own runtime and runs its own turn (invariant 5). `--bg` detaches it into its own OS session with file stdio, so it survives the `Bash` tool's process-group kill.

## Three principles

From `README.md`:

1. **Minimalism** — No bloat. Extend with styles.
2. **Automation-friendly** — Full CLI; TUI is optional.
3. **Fluidity** — Adapts to any environment.

## Package layout

```
src/miniouto/
├── __init__.py              # __version__ = "0.1.1"
├── paths_runtime.py         # INVOCATION_CWD: Path  (captured cwd at import)
├── cli/                     # Typer commands + Textual TUI
├── core/                    # Chat loop + runtime assembly + event sinks
│   ├── __init__.py
│   ├── chat.py
│   ├── context.py
│   ├── events.py            # LoopEvent, EventSink protocol, NullSink, ConsoleEventSink
│   ├── inbox.py              # cross-process message inbox (one file per message)
│   ├── lma.py
│   ├── providers.py
│   ├── repetition.py         # repetition watchdog (phrase / tool loops)
│   ├── runtime.py
│   └── spawn_state.py        # liveness markers for detached `chat --bg` runs
├── storage/                 # Filesystem persistence (the only layer that touches disk, besides tools/)
├── tools/                   # Bash + media + Computer + .art codec (pure stdlib + async)
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
                             │ bash,    │                  │
                             │ media,   │                  │
                             │ registry │                  │
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

`tools/` is the only layer that touches both the filesystem and coreouto (via `registry.py`). Keep `bash.py` and `media.py` coreouto-free so they stay portable and testable.

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
  │                     │     ├─► core.providers.build_coreouto_provider (the run's own)
  │                     │     ├─► tools.registry.register_all(api_format)
  │                     │     ├─► storage.styles.parse_style (→ outo body + N role specs) + skills
  │                     │     ├─► runtime.role picks the matching spec's prompt (ROLE MODE),
  │                     │     │    otherwise the outo body
  │                     │     ├─► coreouto.register_agent_preset("outo")   ← ONE preset;
  │                     │     │    tools = BASE_TOOLS (+ Computer when the platform registered it)
  │                     │     ├─► coreouto.register_hook(BEFORE_TOOL_CALL, _make_tool_call_logger)
  │                     │     ├─► coreouto.register_hook(ON_ITERATION, make_context_monitor)
  │                     │     ├─► coreouto.register_hook(BEFORE_LLM_CALL, _make_alarm_hook)
  │                     │     ├─► coreouto.register_hook(ON_ITERATION, _make_iteration_logger)
  │                     │     └─► coreouto.register_hook(AFTER_LLM_CALL, _make_response_logger)
  │                     ├─► storage.sessions.load (if --continue)
  │                     ├─► supervised co.Agent.call(prompt, history)  ← stall + repetition watchdogs
  │                     │     ├─► tools.bash / .media / .computer (via registry)
  │                     │     ├─► context monitor at 80%: inbox.insert(summary request,
  │                     │     │    kind=alarm, source=context) → delivered on the next
  │                     │     │    BEFORE_LLM_CALL as a user message; nothing is compacted
  │                     │     ├─► Bash: miniouto chat "<brief>" --style <style> --role <tag> [--bg]
  │                     │     │    → a SEPARATE PROCESS runs its own build_runtime + turn; it
  │                     │     │      writes subagent-results/<id>-<ts>.md (the answer only) and
  │                     │     │      inserts ONE message into inbox/<session>/, which this
  │                     │     │      process delivers on the next BEFORE_LLM_CALL — as a user
  │                     │     │      message, plus one "alarm" loop event carrying the
  │                     │     │      run's id/role
  │                     │     ├─► ON_STREAM_TEXT / ON_STREAM_THINKING → sink.emit_stream_delta
  │                     │     │    (display only; never recorded) + repetition.note_stream_delta
  │                     │     └─► on_tool_call closure (from _make_tool_call_dispatcher,
  │                     │          bridged by _make_tool_call_logger BEFORE_TOOL_CALL hook)
  │                     └─► storage.sessions: begin_turn → update_turn_events / update_history
  │                          → finish_turn
  │
  ├── miniouto chat "<brief>" --style <style> --role <tag> [options] ─► core.chat.run_chat in a SEPARATE process
   │     ├─► --bg → detach (start_new_session=True) + spawn-logs/<id>.log + <id>.json marker
   │     ├─► writes subagent-results/<id>-<ts>.md (the final answer, nothing else)
   │     └─► core.inbox.insert(alarm_session, alarm)   ← delivered next turn
   ├─ miniouto message insert/list/clear ─► core.inbox + storage.settings
   ├─ miniouto computer show [PATH]     ─► tools.ascii_art.parse_art (renders a .art capture)
   ├─ miniouto provider providers/models/add ──► core.lma + storage.providers + settings.update
  ├── miniouto provider custom add            ──► storage.providers + settings.update
  ├── miniouto provider [list|remove|default] ──► storage.providers.* + settings.update
  ├── miniouto style    [list|set|add|update|show] ──► storage.styles.* + settings.update
  ├── miniouto skill    [list|show]           ──► storage.skills.*
   ├─ miniouto subagent [show|set|clear]      ──► storage.providers + settings (what `chat --use-subagent-provider` reads)
  └── miniouto status                          ──► read everything, print
```

## Key invariants

These are non-obvious rules that hold throughout the codebase. **Breaking any of these will silently degrade or break the system.**

### 1. Provider registry is rebuilt every turn
`core/runtime.build_runtime` calls `core.providers.clear_coreouto_state()` (which calls `co.clear_providers()`, `co.clear_agent_presets()`, `co.clear_tools()`, `co.clear_hooks()`) at the very start, then re-registers everything. This makes `build_runtime` **idempotent across CLI invocations** (necessary because TUI mode is a long-lived process that re-enters the function many times).

### 2. Style documents are parsed top-level — the tag name IS the subagent name
Each style file is parsed by `storage.styles.parse_style(content) -> tuple[str, list[SubagentSpec]]` with a single sequential scan over the document's top level. `SubagentSpec` is a frozen dataclass with `name: str` (the tag name) and `prompt: str` (the tag body, used as that subagent's system prompt). Rules:

- `<outo>...</outo>` is the outo prompt. If absent, the **whole document** is the outo prompt.
- Each **top-level** `<name>...</name>` pair outside `<outo>` is one **role** for `miniouto chat "…" --style <s> --role`; the tag name IS the name. Tag names match `[A-Za-z][A-Za-z0-9_-]*` (hyphens allowed).
- `outo` is reserved (always the outo prompt).
- A legacy `<subagent>...</subagent>` block is parsed as a role named `"subagent"`. Old style files keep working unchanged — `--role subagent` runs them.
- **Zero roles is legal**: `chat` with no `--role` runs the outo prompt, and `--role <tag>` with no match **errors before the turn** (listing the available roles, or `(none)`), so a typo costs no model call.
- Parse is **top-level only**: a pair nested inside another tag body is content, not a role.
- Duplicate names at the top level raise `ValueError`.

The **delegation surface itself is not conditional**: `Bash` is always registered, so a delegated `miniouto chat --role <tag>` run is reachable from the model in every style. What a style controls is only which role bodies exist.

### 3. Prompt assembly: the token-expanded style body
The system prompt **is** the `<outo>` section of the active style (or the whole-document fallback) with its tokens expanded by `core.runtime.expand_prompt_tokens` — `{cwd}`, `{skills}`, `{skills:<dir>…}`, `{this_session}`, `{style}`, `{subagent_pm}`. Nothing else is prepended: the old cwd preamble and the implicit skill catalog are gone, so the working directory and the skill listing appear exactly where the style puts them (or not at all).

A role run (`chat --role <tag>`) is that tag's body through the same expansion call — assembled by the same `build_runtime`, so there is no second assembly path to drift.

### 4. Context-window safety
`core/context.py` enforces a **16K-token output floor** by calling `https://lma.blp.sh/model?model-name=...&provider-name=...` (via `core.lma.get_model`):

- **Floor:** `DEFAULT_MAX_OUTPUT_TOKENS = 16384`. Without this, Anthropic's default of 1024 silently truncates long tool calls (e.g. heredoc file writes).
- **No ceiling.** The previous `MAX_OUTPUT_TOKENS_CEILING = 16384` was a defense against the legacy `lcw-api.blp.sh/context-window` endpoint reporting inflated theoretical streaming caps; lma reports accurate per-request non-streaming caps so the clamp is no longer needed.

### 5. Delegation is a Bash command — `miniouto chat --role` — in its own process
There is **no** `call_subagent` tool and **no** separate spawn command. Delegation happens because the model runs the same `chat` command the user runs, through `Bash`:

```
Bash: miniouto chat "<brief>" --style <style> --role <tag> [--bg] --alarm-session <session>
```

`cli/chat.py` builds a `ChatOptions` (with `role=…`) and calls `core.chat.run_chat` exactly as it does for any other `chat` — the two modes differ **only** in the system prompt (`--style <name> --role <tag>` — `--role` requires `--style`, and `--alarm-session` requires an explicit session name, so a delegated command line is never ambient), the provider trio (`--pm 'PROVIDER/MODEL[:REASONING]'`, which bundled styles emit as `--pm {subagent_pm}`; `--use-subagent-provider` resolves the same subagent trio at call time), the completion alarm (`--alarm-session <session>`), and the answer file (`--return`). Tool list, preset, hooks and watchdog are identical. One brief per invocation — several commands in one Bash call is how parallelism is expressed; there is no fan-out flag.

Two output channels, always both: an answer file at `subagent-results/<id>-<ts>.md` holding **only** the final answer, and **exactly one** message inserted into `inbox/<session>/` whose first line is `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]`. A third channel is only a record: the `spawn-logs/<id>.json` liveness marker, which a **monitor** reads (pid-aware, so a SIGKILLed run drops out) and which nothing inside a chat run waits on.

**`--bg` must stay detached.** The child is re-emitted from the *resolved* values with `start_new_session=True` and file stdio (`spawn-logs/<id>.log`). Both halves are load-bearing: `tools/bash.py` kills whole process groups and grace-kills pipe-holding children, so a background run survives only with its own session **and** never a pipe.

A delegated run is **fully supervised in its own turn** — `supervised_run` gives it the same stall watchdog and repetition restart as outo. What it does not share is the parent's event stream: its loop events go into its own transcript, not the launching session's.

### 6. Every actor is `outo`; a delegated run's metadata rides on the inbox message
Delegation is cross-process, so there is no in-process identity to attribute: the `_SUBAGENT_DEPTH` / `_SUBAGENT_NAME` / `_SUBAGENT_ID` ContextVars and the observer slot are **deleted**. `core.chat._actor_label()` is kept as the single place that answers "who is speaking" and now always returns `("outo", None)` — including in a delegated role run, which owns its own `"outo"`.

`LoopEvent.subagent_id` / `subagent_name` **survive**, but not from ContextVars: `_make_alarm_dispatcher` copies them off the drained `InboxMessage`'s `meta`, so they are **bookkeeping for a delegated run** and are `None` for a `kind="user"` message. `detail="inbox:<id>"` is the **optimistic-row suppression key**: a message typed into the TUI mid-turn renders immediately as a row, and the matching loop event is dropped rather than drawn twice.

### 7. Sessions are schema v2: `history` (restorable) vs `turns` (display)
`storage/sessions.py` writes `{"version": 2, "history": [...], "turns": [...]}`. `history` = raw coreouto `Message` dumps minus system messages, rewritten in full every turn from `Response.messages` (**never compacted** — there is no implicit summarization) and also snapshotted mid-turn. `turns` = display-only `TurnRecord`s including `LoopEvent` dicts — thinking lives only there (providers never put thinking into history messages). `SessionData.cwd` records the launch directory, stamped by `begin_turn` on the **first** turn only, so a session belongs to the directory it was created in; the TUI session picker and `miniouto status` group by it, and `list_sessions_with_meta()` parses the JSON directly (never through `load()`, which writes). `load()` migrates v1 files and never raises on corrupt content.

### 8. File manipulation is Bash-only
There are no dedicated Write/Edit/Delete tools — `tools/bash.py` covers all file work (`cat`/`grep`/`find`, heredocs/`tee`, `sed -i`, `rm`). The dedicated tools were removed (they were error-prone and the agent reached for Bash anyway). Do not reintroduce them without explicit discussion; to add any other tool, follow `docs/tools.md` § "Adding a new tool".

### 9. Async only where needed
`tools/bash.py` is async (it spawns a subprocess). The media loaders are sync. The TUI uses `asyncio.to_thread(run_chat, opts, sink)` to call the sync `core.chat.run_chat` without blocking the Textual event loop.

### 10. Skill discovery lives outside `~/.miniouto/`
`storage/skills.py` reads from two roots — the invocation directory's `./skills/<name>/SKILL.md` (project-local, FIRST) and `~/.agents/skills/<name>/SKILL.md` (the Anthropic-style convention) — NOT from `~/.miniouto/`. This is intentional: skills are a portable, project-shared concept, not a per-installation setting.

### 11. Stream deltas are display-only — never a record
`EventSink.emit_stream_delta(kind, text)` carries the per-fragment payloads coreouto fires from `ON_STREAM_TEXT` / `ON_STREAM_THINKING`. All four sink implementations define it (hooks are global and there is no `__getattr__` delegation, so a missing method would raise `AttributeError` on the first delta of the first turn). `_RecordingSink` forwards to the wrapped sink and stops — it does not record, and it does not call the persister — so the session file never contains fragments; the whole message is stored once, when it completes. `ConsoleEventSink` is a no-op, keeping CLI output byte-identical. In the TUI the deltas land in one live `StreamRow` per message, coalesced to ~30 Hz. The repetition watchdog is deliberately **not** fed from deltas: they fragment mid-segment, and the whole-message counters already cover the same content.

### 12. Alarm delivery: a real user message — append to live messages, or dispatch a turn
A delegated run's completion alarm (and anything a human or script inserted) is a synthetic **user** message whose first line is exactly `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]` (`core/inbox.py:ALARM_HEADER`, shared with `core/repetition.py:alarm_text`). It must reach both the chat and the model, never sit on disk.

- **Mid-turn** (the common case for a foreground chat): the `BEFORE_LLM_CALL` hook `core/runtime.py:_make_alarm_hook` calls `inbox.drain(session)`, joins the texts with a blank line, and appends ONE `co.Message(role="user")` to the LIVE list, so the very next provider call already carries it. `Agent.inject_user_message` is deliberately **not** used: its queue is drained at the top of the loop iteration, i.e. *before* this hook, so a message queued from there would land a full round-trip later — and it could never wake a turn that already ended. If the last message is already a user message the text is folded into it, because providers reject two consecutive user messages. There is **no depth gate**: the inbox is per-session, not per-actor.
- **Idle** (the run already printed its answer and exited): the TUI polls the inbox every second and starts a turn whose prompt **is** the joined message text (recorded as that turn's `user`, so display, replay and model perception are the same string), and `miniouto chat --alarms -c [--name <s>]` does the same explicitly. Both consume exactly once because `drain` unlinks. A one-shot `chat` never waits: anything queued after its turn stays queued for the next wakeup.
- Each drained message becomes one `LoopEvent(kind="alarm"|"user", detail="inbox:<id>")` for the record and the TUI.

The queue is a **directory of files, not a deque** (invariant 20): one message = one atomically-renamed JSON file, so a writer racing a drainer is never lost, only deferred, and corrupt content is quarantined as `.bad` instead of raising. **One consumer per session, deliberately** — two live processes bound to one session would steal each other's messages; isolate them with a distinct `MINIOUTO_HOME` instead of adding a lock.

### 13. A looping model is force-stopped, not waited out
`core/repetition.py` counts, per actor, a **consecutive run** — never a total. A phrase *unit* of 1..8 segments repeated `REPEAT_PHRASE_THRESHOLD = 50` times back to back trips (a single phrase 50x, or the pair "A B" 50x, or a three-clause cycle 50x: the smallest unit that trips is the one reported, and the block chain is compared from the end so a misaligned stream never fakes one); runs **span messages**, because a loop alternating two phrases across ten messages is the same loop. Scattered repeats never trip — the same lead-in before each of ten tool calls, or words reused in a changing order, is normal speech. The same tool call repeated `REPEAT_TOOL_THRESHOLD = 30` times back to back trips (any different call in between resets the streak; a streak does span messages, one call per message being 30 real repetitions). Both are exact and offline. A verdict is sticky until consumed. A looping model is *active*, not wedged, so the stall watchdog would never fire for it: `supervised_run` therefore checks repetition **before** the stall test, cancels the attempt, and resumes with the alarm **as the user message** (`start(alarm, history)`) — the stall path's sibling is `start(None, history)`, and the difference is exactly that injected alarm. The budget is its own (`REPEAT_MAX_RESTARTS = 3`, independent of the stall watchdog's `WATCHDOG_MAX_WAKEUPS`); exhausting it raises `LoopRepeatedError`, a sibling of `LoopStalledError` and deliberately not of `LoopCancelledError`. A delegated run supervises itself in its own process, so **there is no unsupervised actor**: `repetition.cancel_background_loop` is deleted and a trip is always enforced by restart, never by cancelling a job. Counters are reset once per turn.

### 14. What the model loses, the record loses
`LoopEvent.msg_ref` is `str(id(message))` of the live coreouto message an event came from, stamped in one place: `ON_ITERATION`, the only hook that fires after coreouto appends the assistant message. Two sources remove messages — the 400/422 history repair (`_repair_pairing` dedupes duplicate tool results, `_cut_poisoned_turn` excises one poisoned turn) and `sanitize_history` (the stall/repetition restart) — and both report the discarded ids. `EventSink.remove_events` then prunes the recorded events carrying those refs, in place, and the persister sweeps every turn of the session file (`sessions.prune_turn_events`), so what the TUI shows, what the file records, and what the model still has all agree. Summarizer compaction is the deliberate **exclusion**: it rewrites history wholesale, but the display transcript is the human record of what happened, so it never prunes. Additions (a filled-in tool result, an explanation note folded into a user message, an alarm) are never removals and are never reported.

## Domain entities

| Entity | Defined in | Purpose |
|---|---|---|
| **Provider** | `storage/providers.py` | LLM API connection: `name`, `api_format` (openai/openai-response/anthropic/google), `base_url`, `api_key`, `default_model`, `source` (`SOURCE_CUSTOM`/`SOURCE_LMA`), `extra` |
| **Settings** | `storage/settings.py` | The **global default**: `provider`, `model` (legacy, read-only), `style`, `session`, `theme`, `subagent_provider`/`subagent_model`/`subagent_reasoning` |
| **ProviderConfig** | `storage/session_config.py` | One session's **pin** at `session-config/<session>.toml`: the same six provider keys, `""` = follow the default. Frozen at the session's first resolution, seeded copy-if-absent into every freshly minted session — the layer that lets several processes run on different providers at once |
| **Style** | `storage/styles.py` | Markdown system prompt, parsed into an outo body and zero or more role bodies via `parse_style` (top-level tag-as-name) |
| **InboxMessage** | `core/inbox.py` | One cross-process message: `(id, kind, text, ts, source, meta)`; `id` is the filename stem |
| **SpawnMarker** | `core/spawn_state.py` | Liveness record for a detached run: `id`, `alarm_session`, `status`, `log`, … |
| **Skill** | `storage/skills.py` | YAML-frontmatter markdown, discovered from the default roots `./skills/` then `~/.agents/skills/` (or any root a style's `{skills:<dir>…}` names) and listed to the model by the `{skills}` token |
| **Session** | `storage/sessions.py` | Schema-v2 JSON: restorable `history` + display `turns`, keyed by name |
| **SessionData / TurnRecord** | `storage/sessions.py` | `SessionData`: `name`, `history` (coreouto Message dicts), `turns`. `TurnRecord`: `user`, `assistant`, `events` (LoopEvent dicts), `ts` |
| **RuntimeConfig** | `core/runtime.py` | Resolved per-call configuration (provider, model, style, session) |
| **ChatOptions** | `core/chat.py` | Raw CLI flag bag for a single chat turn |
| **LoopEvent** | `core/events.py` | Single trace event (actor=`outo` / `provider` / `watchdog`, kind, text, optional `subagent_id`/`subagent_name` carried from an inbox message's `meta`, `detail`, `msg_ref`) emitted via the `EventSink` |
| **EventSink** | `core/events.py` | Protocol implemented by `NullSink` (no-op) and `ConsoleEventSink` (CLI rendering) |
| **ToolCallArgsError** | `core/chat.py` | Local exception for malformed LLM tool arguments |

Relationships:

```
ChatOptions (CLI flags)
  └─> resolve_runtime_from_settings (runtime.py)
        ├─ resolution: explicit override (chat --pm) > this session's pin
        │    (storage/session_config.py, frozen at first use) > the global default
        ├─ reads Settings (storage/settings.py) + Provider (storage/providers.py)
        └─> RuntimeConfig
              └─> build_runtime (runtime.py)
                    ├─ build_coreouto_provider (providers.py)
                    ├─ tool_registry.register_all()
                    ├─ parse_style + expand_prompt_tokens
                    │    → registers "outo" preset + one preset per SubagentSpec
                    ├─ registers the ONE "outo" preset (role mode swaps the
                    │    system prompt only — the tool list is IDENTICAL in
                    │    both modes, Computer included)
                     ├─ installs the sink hooks, the watchdog trackers, and the
                     │    unconditional BEFORE_LLM_CALL alarm + ON_PROVIDER_ERROR repair hooks
                     └─ returns co.Agent(outo_config)
        └─> run_chat (chat.py)
               ├─ loads history (if --continue)
               ├─ builds sink dispatchers (tool/result/response/thinking/iteration/
               │    provider-error/stream/inbox-alarm)
               ├─ supervised agent.call(prompt, history=core_msgs) (stall + repetition watchdogs)
               ├─ on exception: _dump_failure_diagnostics
               └─ incremental persistence: begin_turn before the loop,
                  event + sanitized-history snapshots during it, then
                  finish_turn stamps the running turn done/interrupted
                  (success also rewrites history from Response.messages
                  minus system)
```

## External dependencies

| Package | Version | Role |
|---|---|---|
| `coreouto[all]` | `>=0.11.3` | Agent loop, providers, tool registry, hooks (streaming hooks, contrib watchdog trackers, literal-typed schemas) |
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

1. **Test coverage is thin.** `tests/` covers `parse_style`, delegation (`test_chat_delegation.py`), the summary hand-off (`test_handoff.py`), inbox, spawn_state, role runtime, ascii art and TUI navigation, but `tools/bash.py` is still the highest-value untested target.
2. **`tui/` and `utils/` are empty.** They look like package directories but contain no code.
3. **`storage/skills.py` is not in `storage/__init__.py`'s `__all__`** — it's imported directly via `from ..storage import skills as skill_store`. Don't add it to `__all__` without auditing the import sites first. `storage/session_config.py` is imported the same way (`from ..storage import session_config`).
4. **`tools/registry.py:_register_if_missing` silently discards the `schema` parameter** — only the handler's type hints and the `description` string reach the model. The `_xxx_schema()` dicts are dead code.
5. **`core/runtime.py` registers `ON_ITERATION` twice.** `context_monitor` (when the window size is known) and `_make_iteration_logger(on_iteration)` (when `on_iteration` is supplied, which `chat.run_chat` always does). Both fire on every iteration; the monitor is one-shot per turn.
