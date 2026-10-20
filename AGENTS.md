# AGENTS.md

> **Read this first.** This file is the single source of truth for the miniouto project. It tells you what every file does, why it exists, what depends on it, and what to watch out for when modifying it. The detailed docs live in [`docs/`](./docs/) — this file orients you to them.

---

## What this project is

**miniouto** is a minimal, file-driven CLI agent harness built on top of [`coreouto`](https://github.com/llaa33219/coreouto). It exposes a single `miniouto` command that can:

- Run a **one-shot chat turn** (`miniouto chat "..."`).
- Launch a **Textual TUI** (`miniouto` with no args).
- **Delegate** one brief to a nested run of the harness — the *same* `miniouto chat` command with `--style <name> --role <tag>` (`miniouto chat "<brief>" --style <style> --role <tag> --bg --pm <provider>/<model> --alarm-session <session> --return`), run through `Bash`, in its own process. There is no second delegation command.
- Accept **cross-process messages** (`miniouto message insert --session <s> "text"`), including ones inserted by a human mid-turn in the TUI.
- Manage **providers**, **styles**, and **skills** via CLI subcommands.
- Persist **session history** as plain JSON files.

Three principles from `README.md`: **Minimalism** (no bloat — extend with styles), **Automation-friendly** (full CLI, TUI optional), **Fluidity** (adapts to any environment).

**Version**: `0.11.0` (alpha). **Python**: `>=3.10`. **Build**: `hatchling`. **Console script**: `miniouto = "miniouto.cli:app"`.

---

## The 60-second mental model

```
~/.miniouto/                     ← user's storage (override via $MINIOUTO_HOME)
├── providers.toml               ← LLM API connections (one top-level table per provider)
├── settings.toml                ← global DEFAULT provider / style / session / theme
├── session-config/<session>.toml← that session's PINNED provider trio (outo + subagent) + its style
├── style/*.md                   ← system prompts (outo body + N named role bodies)
├── sessions/*.json              ← schema v2: restorable history + display turns
├── inbox/<session>/             ← cross-process message inbox (1 message = 1 JSON file)
├── spawn-logs/                  ← bg liveness markers (<id>.json) + bg stdio logs (<id>.log)
├── computer-screens/            ← .art Unicode sub-cell screen dumps (`screen` + `screenshot`)
├── subagent-results/            ← answer files of finished delegated runs (the final answer only)
└── logs/                        ← reserved (default)

./skills/                        ← project-local skills, FIRST (a default root)
~/.agents/skills/                ← global skills, SECOND (NOT under ~/.miniouto/)
└── <name>/SKILL.md              ← YAML frontmatter + markdown body

<cwd>/.miniouto/summary/          ← PROJECT-LOCAL, not under ~/.miniouto/: a summary
└── <session>/*.md               is a project artifact, meant to be reviewed and
                                  committed with the work it describes (invariant 21)

src/miniouto/
├── cli/      ← Typer commands + Textual TUI
├── core/     ← chat loop, runtime assembly, role mode, inbox, event sinks, summary contract
├── storage/  ← the only layer that touches disk (apart from tools/)
├── tools/    ← Bash + media loaders + Computer + continue_session (only bash is async)
├── default_style/  ← 5 bundled .md templates, force-refreshed on every run
└── __init__.py, __main__.py, paths_runtime.py
```

**Data flow per chat turn:**

```
CLI flag bag → ChatOptions (core/chat.py)   ← `--pm` is the ONLY provider override
            → resolve_runtime_from_settings (core/runtime.py)
              resolution: overrides (--pm) > session pin > global default
            → RuntimeConfig   (+ `role` for a delegated run, role mode)
            → build_runtime (core/runtime.py)
                → ALL slow resolution hoisted ABOVE clear_coreouto_state()
                  (style I/O, lma caps, both reasoning passthroughs) so the
                  clear+register window is pure in-memory — a delegating run
                  is a SEPARATE PROCESS, not a job in this one (invariant 5)
                → coreouto provider registry + tool registry + hooks
                    (sink loggers on BEFORE/AFTER_TOOL_CALL, ON_ITERATION×2 —
                    the iteration logger + the context monitor (invariant 21) —,
                    AFTER_LLM_CALL, ON_THINKING, ON_STREAM_TEXT/THINKING,
                    ON_PROVIDER_ERROR + watchdog
                    trackers: activity/loop-progress + BEFORE_LLM_CALL capture,
                    plus the unconditional BEFORE_LLM_CALL inbox hook)
                → co.Agent(outo_config)
            → run_chat(opts, sink) → asyncio.run(_run_with_watchdog(agent, ...))
                → stall watchdog (runtime.supervised_run): 30-min silence
                  outside a tool call cancels the attempt and restarts it
                  from the sanitized live history with NO user message
                  (coreouto >= 0.11.1, examples/27+28; max 3 wakeups);
                  repetition restart is the sibling path and resumes with
                  the alarm AS the user message (start(alarm, history)).
                → Bash/Image/Video/Audio/Computer (via tools/registry.py);
                  Computer only when the platform registered it — the tool
                  list is IDENTICAL in outo and role mode
                → Bash runs `miniouto chat "<brief>" --style <style> --role <tag>
                  [--bg] --pm <provider>/<model> --alarm-session <session>
                  --return`: a whole
                  new process (and, with --bg, a detached one) that is an
                  ordinary chat run with a different system prompt; it writes
                  the final answer to subagent-results/<id>-<ts>.md and
                  finishes with ONE alarm message inserted into inbox/<s>/
                  (invariants 5 + 20)
                → ON_STREAM_TEXT/THINKING (per token) → sink.emit_stream_delta
                  (TUI live row only; never recorded — invariant 8)
                → BEFORE_LLM_CALL: inbox.drain(session) appends whatever
                  arrived (alarm or user) to the live messages; an IDLE
                  session is woken instead — the TUI's 1 s poll, or
                  `chat --alarms -c`, which drains the queue into a fresh
                  turn's prompt (invariant 16)
            → incremental persistence: begin_turn before the loop, event +
              sanitized-history snapshots during it, then finish_turn stamps
              the turn done (history rewritten from Response.messages minus
              system) or interrupted (incremental history kept); removed
              messages also prune their display/file events (invariant 18)
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
| Add/modify a skill | `./skills/<name>/SKILL.md` (project-local) or `~/.agents/skills/<name>/SKILL.md` (global) | [`docs/skills.md`](./docs/skills.md) |
| Browse lma providers/models or change SDK mapping | `core/lma.py`, `core/providers.py:sdk_to_format`, `cli/provider.py` | [`docs/lma.md`](./docs/lma.md) |
| Change how a provider/model/reasoning is resolved | `core/runtime.py` (`default_provider_config` / `_pinned_for`), `storage/session_config.py` (the pin), `cli/chat.py` (`--pm`), `cli/tui.py` (`ProviderSettingsPopup`) | [`docs/core.md`](./docs/core.md) |
| Change how delegation works | `cli/chat.py` (flags + post-step + `--bg` detach), `core/inbox.py` (result delivery), `core/spawn_state.py` (liveness) | [`docs/core.md`](./docs/core.md) |
| Insert a message into a live session from anywhere | `cli/message.py` → `core/inbox.py` | [`docs/cli.md`](./docs/cli.md) |
| Read/render a captured screen as `.art` | `tools/ascii_art.py` (format), `cli/computer.py` (`miniouto computer show`) | [`docs/tools.md`](./docs/tools.md) |
| Set up dev environment / release | `pyproject.toml`, `uv.lock` | [`docs/development.md`](./docs/development.md) |

---

## Development environment

- **Work inside the uv-managed venv.** `uv sync` creates/updates `.venv`; afterwards always invoke through `uv run …` (or `.venv/bin/…`). Never use a system interpreter — the project resolves its deps (`coreouto[all]`, textual, py-Vwayland) only inside that venv.
- **Keep `uv.lock` fresh.** Whenever `pyproject.toml` dependencies change, run `uv lock` (or `uv lock --upgrade-package <pkg>` for a single bump) and commit `uv.lock` in the **same** change. A stale lock silently reinstalls the old resolution.
- **Tests**: `uv run pytest` (config-free; `pytest` is the only member of the `dev` dependency group, which `default-groups = ["dev"]` pulls in). The suite is ~300 tests; keep it green.
- **Lint**: `uvx ruff check src/ tests/`. Ruff is **not** in the project's dev group — `uv run ruff` will not resolve it; use `uvx`. Config lives in `pyproject.toml` (`line-length = 100`, `target-version = "py310"`, `select = ["E","F","W","I","B","UP","N","SIM","RUF"]`).
- **Never hand-edit generated state** in `~/.miniouto/` for debugging; set `MINIOUTO_HOME` to a scratch dir to get an isolated tree.

---

## The 21 invariants (do NOT break these)

These rules hold throughout the codebase. Breaking any of them silently degrades or breaks the system. **If your change seems to require breaking one, stop and ask.**

### 1. Provider registry is rebuilt every turn
`core/runtime.py:build_runtime` calls `core.providers.clear_coreouto_state()` at the very start, then re-registers providers, presets, tools, hooks. This makes `build_runtime` **idempotent across CLI invocations** — necessary because TUI mode is a long-lived process that re-enters the function many times. **Do not remove `clear_coreouto_state()`.**

Because that clear wipes the registries, every slow resolution in `build_runtime` — the provider store lookup, `_resolve_both_styles` file I/O, `get_max_output_tokens`, both `resolve_reasoning_passthrough` calls, and the context monitor's window lookup (`make_context_monitor`) — happens **above** the clear, so the clear+register window is pure in-memory dict work. A new network or disk call below `clear_coreouto_state()` is a race waiting to happen. (Nothing in-process outlives a turn any more now that a delegated run is a separate `chat` process — invariant 5 — so the cost is now latency, not correctness; the discipline stays anyway.) `core/chat.py:run_chat` clears the coreouto state on any failure from resolve/build, so a half-built turn never leaves the previous turn's hooks registered.

### 2. Style documents are parsed top-level — the tag name IS the role name
`storage/styles.py:parse_style(content) -> tuple[str, list[SubagentSpec]]` parses each `.md` with a single sequential scan over the document's top level. `SubagentSpec` is a frozen dataclass with `name: str` (the tag name) and `prompt: str` (the tag body, used as that role's system prompt). Rules:

- `<outo>...</outo>` is the outo prompt. If absent, the **whole document** is the outo prompt (unchanged from the old behavior).
- **Each** `<name>...</name>` pair at the top level, outside `<outo>`, is one **role** — the tag name IS the name, and `miniouto chat "<brief>" --style <style> --role <tag>` selects it. Tag names match `[A-Za-z][A-Za-z0-9_-]*` (hyphens allowed: `file-picker`, `researcher-docs`).
- `outo` is reserved (always the outo prompt).
- **Legacy compat**: a `<subagent>...</subagent>` block is parsed as a role named `"subagent"`. Old style files keep working unchanged — `chat --role subagent` runs them.
- **Zero roles is legal**: `chat` with no `--role` runs the outo prompt. `--role <tag>` on a style with no matching tag **errors before the turn** (`cli/chat.py:_validate_role`, listing the available roles, or `(none)`), so a typo costs no model call; `core/runtime.py` repeats the same check for a role resolved from settings.
- Parse is **top-level only**: a pair nested inside another tag body is treated as content, not as a role. Stray prose outside any tag is ignored.
- Duplicate names at the top level raise `ValueError`.

The delegation *surface* is not conditional: `Bash` is always registered, so a delegated `miniouto chat --role <tag>` run is reachable from the model in every style. What a style controls is only which role bodies exist. If you change the tag format, update `parse_style`, `default_style/*.md`, and `docs/styles.md`. (`split_style` is gone — do not reintroduce it.)

### 3. The system prompt IS the token-expanded style body
There is **one layer**. `build_runtime` assembles the system prompt as `expand_prompt_tokens(<style body>)` and nothing else — for the `<outo>` body **and** every role body. The implicit cwd preamble and the automatic skill-catalog prepend are **deleted** (neither helper exists any more); a style that names no tokens tells the model no working directory and no skills, and there is **no compatibility fallback**. Layout is the style author's decision: where (and whether) `{cwd}` and `{skills}` appear, in what order, or not at all. Expansion happens fresh on every turn (nothing is persisted), and a role run is assembled by the same call, so there is no second assembly path to drift.

**Dynamic style tokens.** `core/runtime.py:expand_prompt_tokens(text, *, session, style)` substitutes exactly five tokens into a style body, applied to **both** the outo body and every role body:

| Token | Expands to |
|---|---|
| `{cwd}` | `str(INVOCATION_CWD)` — the directory miniouto was invoked from (`paths_runtime.INVOCATION_CWD`, captured at import) |
| `{skills}` | the skill catalog for the **two default roots, LOCAL FIRST**: the invocation directory's `./skills/` (resolved against `INVOCATION_CWD`), then `~/.agents/skills` (see below). `storage/skills.py:default_roots(cwd)` owns that pair |
| `{skills:<dir>[,<dir>…]}` | the catalog merged from the named roots, in the order listed. An empty argument (bare `{skills}` or `{skills:}`) means the two default roots (local first). `~` is expanded; a relative path resolves against `INVOCATION_CWD`, so a style can point at `./skills`. Duplicate roots are kept — a style listing one twice is its own business. Missing or unreadable roots are **skipped silently** (a bogus path must never fail the turn that expanded it). Entries are ordered by root order, then by name within a root, because a style listing several roots states a precedence. A name present in two roots appears **twice**, each line naming the root it came from. Nothing found → the **empty string** |
| `{this_session}` | this run's own session name (`runtime.session`, or `"default"` when empty — the same value the inbox hook drains, so a style can teach a literal `--alarm-session`) |
| `{style}` | the resolved style name (`runtime.style_name`) |
| `{subagent_pm}` | the effective subagent provider config in `--pm` form, `PROVIDER/MODEL` or `PROVIDER/MODEL:REASONING` (the **subagent trio** — this session's pinned `subagent_provider`/`subagent_model`/`subagent_reasoning`, else the `settings.subagent_*` global default, else outo's own provider/model/reasoning per key). Left **literal** when the trio does not resolve |

(The first row is `{cwd}`; the two skill rows are one token in two forms. Six spellings, five tokens — `{skills}` is `{skills:}` with the default-root argument.)

**Expansion order** (`expand_prompt_tokens`, the only assembly step):
1. The parameterized `{skills…}` regex pass (`_SKILLS_TOKEN = re.compile(r"\{skills(?::([^}]*))?\}")`) → `_replace_skills_token` → `_resolve_skill_roots` + `_skills_catalog`. It runs **first** because the catalog text is itself brace-bearing and must never be re-scanned by the plain replaces below.
2. Then the plain `str.replace` passes, in source order: `{cwd}`, then `{this_session}` and `{style}`, then `{subagent_pm}` — which is computed only if the token is still present (`{subagent_pm} not in text` returns early), and left literal when the trio does not resolve, so the model never copies a half-filled `--pm`.

Any other `{...}` text is left verbatim: there is **no escape mechanism** and an unknown token is not an error. The **model never sees the token syntax** — it reads a fully literal, copy-ready command, which is what keeps Bash idempotent (invariant 5). This is the style-authoring mechanism that replaced the old ambient bare-flag defaults: bundled styles write `--style {style} … --pm {subagent_pm} … --alarm-session {this_session}`, and expansion turns them into real values per run.

**The skill catalog (`{skills}` / `{skills:…}`).** `_skills_catalog(roots)` renders a `# Available Skills` block: a note saying each skill lives in its own directory under `<root>/<name>/` containing a `SKILL.md`, that the model should read it (and the files it references) via Bash when a task matches, then one `- <name>: <description>` line per skill. It is a **catalog, never a body**: SKILL.md content is not injected, the model reads it on demand (lazy loading). Each root is read through `storage/skills.py:list_skills(directory)`, whose `None` argument means **both default roots** (`default_roots()` → `[<invocation cwd>/skills, SKILLS_DIR]`, local first) and which stamps each returned `Skill.directory` with the root it came from; an explicit `directory` is read alone, so a style can still point at whatever roots it wants. **Skill discovery covers both roots by default**: the project's own `skills/` first, then the global `~/.agents/skills/` — portable project content, never under `~/.miniouto/` (invariant 11); the *listing* remains style-owned. Every bundled style places `{cwd}` and `{skills}` in each body it defines, outo and role alike.

**Prompt-beats-history precedence.** The delegation command values in the CURRENT system prompt are authoritative over any `--style` / `--role` / `--pm` / `--alarm-session` values appearing in earlier conversation history. `build_runtime` re-assembles the prompt from the style file on every turn (invariant 1), so `{style}` / `{subagent_pm}` / `{this_session}` are always current — while the literals inside history are frozen at the time that turn ran. Styles genuinely change mid-session (the TUI style picker, `miniouto style set`), and models readily imitate a command they saw earlier in the transcript rather than the one in front of them. That failure is prevented by prompt prose alone, not by any flag or injection: the bundled styles state that the command is to be taken `never from commands in earlier turns`.

### 4. Context-window safety
`core/context.py` enforces a **16K-token output floor** by calling `https://lma.blp.sh/model?model-name=...&provider-name=...` (via `core.lma.get_model`):

- **Floor**: `DEFAULT_MAX_OUTPUT_TOKENS = 16384`. Without this, Anthropic's default of 1024 silently truncates long tool calls (e.g. heredoc file writes).
- There is **no ceiling**. The previous `MAX_OUTPUT_TOKENS_CEILING = 16384` was a defense against the legacy `lcw-api.blp.sh/context-window` endpoint reporting inflated theoretical streaming caps; lma reports accurate per-request non-streaming caps so the clamp is no longer needed.

If you touch `get_max_output_tokens`, you must preserve the floor. Always thread the `provider_name` argument through so the lma lookup is scoped to the active provider (otherwise lma returns matches across every provider and we only see the first hit).

There is **one** deliberate precedence tier above lma: a per-provider `max_output_tokens` override (field on the `Provider` dataclass, set via the TUI custom-model editor). `_provider_caps_override` consults `provider_store` on every call (no caching — the TUI is long-lived and edits these mid-session). The override wins over lma. The CLI `chat --max-tokens` flag still wins over the override. Precedence: `--max-tokens` > provider override > lma `max_output_tokens` > lma `context_window` > `DEFAULT_MAX_OUTPUT_TOKENS`. The same override path exists for `max_context_window` via `get_context_window`.

`core/context.py:_MODEL_CACHE` (no TTL) follows invariant 14's rule: a cached `{}` means "lma answered, no info for this key", but a **transport failure is never cached** — the empty dict an exception produced is returned uncached, so one network blip can never pin a model to "no caps" (compaction off, 16K floor) for the life of the process. `clear_model_cache()` mirrors `core.lma.clear_cache()`.

### 5. Delegation is a Bash command — `miniouto chat --role` — in its own process
There is no `call_subagent` tool (deleted; `tests/test_no_call_subagent.py` guards it) **and there is no second delegation command** — the old dedicated spawn subcommand was deleted and merged into `chat`; do not reintroduce one. Delegation happens because the model runs the harness through `tools/bash.py` — the very same command the user runs:

```
Bash: miniouto chat "<brief>" --style <style> --role <tag> [--bg] --pm <provider>/<model> --alarm-session <session> --return
```

A delegated run **is** an ordinary chat run: same `ChatOptions` assembly, same `run_turn_chain`, same watchdog, same tool list. It differs from outo in **exactly four ways**, each one a flag on `cli/chat.py`:

1. **`--style <name> --role <tag>`** → the system prompt is that `<tag>` body, and **`--role` requires `--style`**: the default style can differ per installation/session, so a delegated command that omitted it would not be the same run twice. The check sits ahead of every resolution in `chat_cmd`, so a rejected command leaves no session file and costs no model call, and its message is exactly `--role requires --style (the default style may differ per session; make the run self-contained)`. `--style` alone (no `--role`) still runs the outo prompt; `_validate_role` rejects an unknown role before any model call, listing the available tags. Which style/session values the model should copy is settled by prompt precedence, not flags — see invariant 3, *Prompt-beats-history precedence*.
2. **The provider trio** → the bundled recipes carry `--pm {subagent_pm}` (invariant 3): an explicit `--pm` naming the effective subagent provider, model and reasoning, so the copied command is literal and idempotent — the same reason `--alarm-session` requires a value. `--use-subagent-provider` remains available as the **call-time-resolving alternative**: provider/model/reasoning then come from the **subagent trio**, resolved pin-first — this session's `session-config/<session>.toml` pin (`session_config.effective`), else the `settings.subagent_*` global default, via `runtime._resolve_subagent(ChatOverrides(...), settings, pinned)`, the helper `build_runtime` no longer needs. It resolves against **THIS run's own** session (`_resolve_session` runs first), so a delegated run never drains its caller's config. An explicit `--pm` still wins over it. `miniouto subagent set/clear` manages the global half of that trio.
3. **`--alarm-session NAME`** → one completion alarm lands in that session's inbox. **The value is required — there is no bare form.** `--alarm-session` used to mean "my session" through `settings.session`, which is ambient state: the same Bash command typed from two agents in two sessions would alarm two different inboxes. Removing it is what makes a delegated command line self-contained and Bash-idempotent; `{this_session}` (invariant 3) is how a bundled style still teaches the literal command.
4. **`--return [PATH]`** → the final answer is written as markdown. `--return` keeps its optional-value grammar (absent = no file, bare = `subagent-results/` with a generated name, `--return PATH` = that path; a directory — existing, or trailing `/` — receives the generated `<id>-<YYYY-MM-DD-HH-MM>.md` name); `cli/__init__.py:_OPTIONAL_VALUE_FLAGS` rewrites the bare spelling to `--return=` before parsing, and it is the **only** flag in that set. **The file holds ONLY the answer text** — no meta header, no brief. Without the flag there is no file and the alarm carries the answer inline.

The brief is the whole input — a delegated run starts with **no history**, so a brief that is not self-contained (goal, paths, constraints, done-criteria) produces garbage. **One brief per invocation; there is no fan-out flag, do not reintroduce one.** Parallelism = several `miniouto chat --role …` commands in one Bash call, or several Bash calls.

- **`--bg` must stay detached.** `_detach` re-emits the child's whole command line from the *resolved* values (`_bg_argv`, never `sys.argv`, so a bare `--return` is already expanded to a concrete value, and a resolved trio becomes an explicit `--pm` via `_pm_from_run` so the child cannot re-resolve against a different pin), then `subprocess.Popen(..., stdin=DEVNULL, stdout=<log>, stderr=STDOUT, start_new_session=True)`. Both halves are load-bearing: `tools/bash.py` kills the whole process group on timeout/cancel **and** grace-kills pipe-holding children for 5 s after the shell exits, so a background run survives only with its own session and FILE stdio. Its stdio goes to `spawn-logs/<id>.log`.
- **The `--bg` ack is a 5-line block, and every channel is named.** The detached process is gone by the time its alarm arrives, so the launcher must tell the caller where the transcript goes and which file holds the status — verbatim from `chat_cmd`:

  ```
  Started run '<id>' (bg).
  Session: <name> — <abs sessions/<name>.json>
  Log: <spawn-logs/<id>.log>
  Status: <spawn-logs/<id>.json> (running → done|error|stopped)
  Alarm → session '<x>' when finished.        # or: No alarm configured — check the status file.
  ```
- **Two output channels, always both.** On completion (success, error, or Ctrl-C) the post-step (`_run_delegated` → `_complete`) writes the answer file and inserts **exactly one** inbox message (`_deliver_alarm`, `kind="alarm"`, `source="chat"`) built by `_alarm_text`: `ALARM_HEADER`, then one line naming role/id/status, then a `Session: <name> — <abs path>` line naming the session the run wrote its own transcript to (`session_store.path_for`), then either the result-file path (when a file was written) or the answer capped at `MAX_ALARM_ANSWER_CHARS = 4000`. The alarm's `meta` carries `{"id", "role", "style", "return_path"}`. The `Session:` line and the marker's `session` field (invariant 20) are why a receiver can find the transcript: the alarm session is the *caller's*, and for `--bg` the run's own session is a generated `spawn-…` name that matches neither.
- **Session persistence is unconditional.** Every chat run writes its session file — plain outo, a delegated `--role` run, and `--answer-only`/`-a` — because `run_chat` builds its `_TurnPersister` before any branch on delegation. `-a` suppresses **stdout only** (the `------{session}------` marker, loop events, finish marker); the file on disk is identical either way.
- **A delegated run is a GUEST: it never writes `settings.session`.** The single writer of the active session is `chat_cmd`'s `settings_store.update(session=session_name)`, gated on `_is_plain_run(run, bg=bg)` — `not bg and run.role is None and not run.delegated`. Every "owes something extra" marker counts against it (`--role`, `--alarm-session`, `--return`, `--bg`, `--bg-child`), so a delegated run mints its own `chat-`/`spawn-` name, writes its transcript there, and leaves the user's active session pointing where it was. Plain `miniouto chat` still persists, with or without `--name`. This was a bug: a delegated run repointed the active session at a subagent transcript, and the TUI (which then re-read `settings.session` per turn) silently jumped the live conversation onto the child's session file — the "three sessions holding the same content" symptom.
- **A delegated run BORROWS its own name as `runtime.session`.** `resolve_runtime_from_settings` has no per-call session override, so the session a turn resolves — the one `_make_alarm_hook` drains and `{this_session}` expands — *is* `settings.session`. `_runtime_session_scope(run)` (a `@contextlib.contextmanager` around `_run_delegated`, restoring the previous value in `finally`, and a no-op when the names already match) makes the child resolve **its own** session for the duration of its chain. Without it the child would drain the **caller's** inbox and answer messages addressed to the caller (stealing its alarms). The borrow is a scoped write, not a persistence one — `finally` restores the caller's name.
- **The post-step fires when the run ends**, not when the first turn does — `run_turn_chain` returns the LAST turn's answer (the one after any summarization hand-off), and that is what `--return` records. A run is "delegated" (`_Run.delegated`) when it re-entered as a `--bg-child`, or `--alarm-session`, or `--return` was given; a plain `chat` owes nothing and skips the whole thing.
- **Foreground and background share one path.** A `--bg` child re-enters the same command with hidden `--bg-child --job-id <id>` and runs the foreground body; it *adopts* the parent's marker (start time + log path) rather than restating them. `--bg` + `--bg-child` is an error.
- **Status is stamped in a `finally`**: `finish_marker(id, status, finished=…)` with `done` / `error` / `stopped`, and a non-`done` status exits 1.
- **`--bg` without `--name` generates its own session** (`spawn-<YYYYMMDD-HHMMSS>-<6hex>`): a detached run must never share a session writer, since whole-file saves from two processes lose turns. For the same reason `--bg` never touches `settings.session` — and neither does any other delegated run (see the guest-session rule above). The minted name is seeded from the caller's pin (`_resolve_session` → `session_config.seed_from`, copy-if-absent), so the child starts on the provider the caller resolved instead of whatever the global default says at the child's moment.
- **A delegated run is fully supervised in its own turn** — `supervised_run` gives it the stall watchdog and the repetition restart exactly like outo. The old v1 limitation ("a background job has no supervisor, no hooks") was a property of in-process jobs and is simply gone; the only thing it does not share is the parent's event stream (see invariant 8).
- **A delegated run's alarm is a WAKEUP, not a debt the caller settles.** The caller returns and dies (invariant 16): the completion message sits in the inbox until the next wakeup consumes it. Nothing about this run may block, poll, or keep a session alive — its only promise is the marker on disk, which a monitor reads.

### 6. Every actor is `outo`; a delegated run's metadata rides on the inbox message
The ContextVars (`_SUBAGENT_DEPTH` / `_SUBAGENT_NAME` / `_SUBAGENT_ID`) and the `_SUBAGENT_OBSERVER` slot are **deleted** along with in-process jobs: a delegated run is a separate process, so there is nothing in this address space to attribute. `core/chat.py:_actor_label()` is kept as the single place that answers "who is speaking" and now always returns `("outo", None)`; the dispatchers still call it rather than hardcoding the string, so the actor plumbing stays uniform.

`LoopEvent.subagent_id` / `subagent_name` **survive** — not from ContextVars, but from the inbox message `meta`: `_make_alarm_dispatcher` maps each drained `InboxMessage` to one event with `subagent_id=meta["id"]`, `subagent_name=meta["role"]`. These are *bookkeeping for a delegated run*, not actor identity, and they are `None` for `kind="user"` messages (a human interjection has no run behind it).

`detail="inbox:<id>"` is the **optimistic-row suppression key**: a message typed in the TUI mid-turn is inserted into the inbox and rendered immediately as a row keyed by its message id; when the drain later produces the matching loop event, `ChatTUI._claim_inbox_event` matches the id and drops the duplicate event rather than rendering it twice. Any event whose `detail` does not carry the `inbox:` prefix (`_INBOX_DETAIL_PREFIX`) renders normally.

### 7. Sessions are schema v2: `history` (restorable) vs `turns` (display), persisted incrementally
`storage/sessions.py` writes `{"version": 2, "history": [...], "turns": [...]}`:

- `history` = coreouto `Message.model_dump` dicts **minus system messages** (coreouto prepends a fresh system prompt every `call()` — persisting it duplicates it per turn; see coreouto `examples/21_loop_history.py`), **with media blocks flattened to text placeholders** (`_dump_message` — coreouto persists media tool results as provider wire dicts that fail `Message` validation on reload; the flatten also keeps `tool_call_id`/`name` so tool pairing never breaks — dropping it makes the provider HTTP 400 the whole restored request). Rewritten in full every turn from `Response.messages` (no implicit compaction ever rewrites it — invariant 21). **Additionally snapshotted mid-turn**: the `ON_ITERATION` dispatcher persists `sanitize_history(messages)` via `update_history` (outo depth only), so a force-killed process leaves a resumable transcript instead of rolling back to the previous turn.
- `turns` = display-only `TurnRecord`s (user, assistant, `LoopEvent` dicts). **Thinking lives only here** — coreouto providers never put thinking into history `Message` objects; it's captured via the `ON_THINKING` hook.

Turn lifecycle: `begin_turn` appends a `status="running"` turn before the loop starts (reaping stale running turns from dead writers to `"interrupted"`); `_RecordingSink.on_event` → `update_turn_events` streams events to disk as they happen (throttled for `thinking`/`context`); `finish_turn` stamps the turn `"done"` (with the authoritative history rewrite) or `"interrupted"` (keeping the incremental history). A turn still `"running"` on disk means the writer died mid-turn — the TUI renders an explicit "turn interrupted" marker in place of the missing final answer. All session saves are atomic (tmp file + `os.replace`) because they now happen on nearly every loop event.

`load()` migrates v1 files and never raises on corrupt content. If you change the schema again, bump `SCHEMA_VERSION` and extend the migration — do not break old files.

The inbox (invariant 20) lives in its **own directory tree** and never touches a session file, so an inbox insert cannot race or corrupt a turn save.

### 8. Stream deltas are display-only — never a record
`EventSink.emit_stream_delta(kind, text)` (`kind` is the `StreamKind` literal `"text"` / `"thinking"`) carries the incremental fragments coreouto fires from `ON_STREAM_TEXT` / `ON_STREAM_THINKING` — one hook **per token**, not the accumulated message. Three rules hold it together:

- **The protocol is closed.** All four implementers must define it (`NullSink`, `ConsoleEventSink`, `_RecordingSink`, `TUIEventSink`): hooks are global and there is no `__getattr__` delegation, so a missing method raises `AttributeError` on the first delta of the first turn.
- **Nothing persists them.** `_RecordingSink.emit_stream_delta` forwards to the wrapped sink and stops there — not `self.events`, not `on_event` (the persister). The whole message is recorded once, when it completes (as a `thinking` / `response` loop event, or the turn's final answer). Recording fragments too would duplicate every streamed message in the session JSON and make replay render it twice. `ConsoleEventSink` is a no-op: the CLI prints whole messages only, and its output must stay byte-identical.
- **Repetition IS fed from streams, through the shared buffering splitter.** `_make_stream_dispatcher` calls `repetition.note_stream_delta(actor_key, delta)`, which buffers the unfinished tail and counts each **completed** segment the moment it completes — both counting paths cut at the same `_SEGMENT_BREAKS` through the ONE `_split_completed`, so a segment compares equal whichever path saw it. This is the pre-authorized design, not a bolt-on: message-boundary counting cannot see a model repeating inside ONE answer that never ends (its boundary is the call returning, i.e. max_tokens, minutes later). **The double-count contract**: because those deltas already counted the message, `note_text` for a message with an active stream degenerates to `flush_stream` (only the un-counted tail) — otherwise every repeat would be counted twice and the threshold would trip at half of it. `ON_ITERATION` is the message boundary (it fires for the terminal message too): it calls `partials.close_message` + `repetition.flush_stream` (the tail is flushed so a phrase cut mid-sentence still counts, and nothing else is cleared — phrase runs deliberately span message boundaries, invariant 17). Deltas are still never persisted — only the **flushed** truncated message becomes an event (see invariant 17).

In the TUI, deltas land in a live `StreamRow` (one row per message, updated in place) / a `●`-marked `ThinkingRow`, coalesced to ~30 Hz by `_StreamCoalescer` and finalized by the canonical whole-message render (`_finalize_stream` + the `response`-row suppression in `_add_event_row`). A cross-process (`--bg`) run has no hooks left once its turn ends — its deltas never reach this sink at all.

### 9. File manipulation is Bash-only
There are no dedicated Write/Edit/Delete tools — `tools/bash.py` covers all file work (`cat`/`grep`/`find`, heredocs/`tee`, `sed -i`, `rm`). The dedicated tools were removed (error-prone; the agent reached for Bash anyway). **Do not reintroduce them without explicit discussion.** This is now doubly load-bearing: delegation itself is a Bash command (`miniouto chat "<brief>" --style <style> --role <tag>`, invariant 5), so a Bash-less harness could not delegate at all. To add any other tool, follow the "Add a new tool" recipe below.

### 10. Async only where needed
`tools/bash.py` is async (it spawns a subprocess). The media loaders are sync. The Computer tool is sync too (py-Vwayland's blocking IPC, and the `screen` action's PNG decode is pure-stdlib `ascii_art.decode_png_rgba`; coreouto runs sync handlers via `asyncio.to_thread`). The TUI uses `asyncio.to_thread(run_chat, opts, sink)` to call the sync `core.chat.run_chat` without blocking the Textual event loop. **Don't make the media loaders async** — they don't need to be, and it complicates the TUI dispatch.

### 11. Skill discovery lives outside `~/.miniouto/`
`storage/skills.py` reads from **two roots**: the invocation directory's `./skills/<name>/SKILL.md` (project-local) and `~/.agents/skills/<name>/SKILL.md` (the Anthropic convention) — local FIRST, so a project's own skills take precedence and a name in both is listed twice. Neither is under `~/.miniouto/`: skills are portable content, not per-installation config. **Do not move them into `~/.miniouto/`.** The rule is about *where*, not *how many roots*: a style may name any roots it likes with `{skills:<dir>…}`. Skills are still NOT affected by `MINIOUTO_HOME`.

### 12. Provider config is per-session pinned, and the TUI edits it through one popup
`storage/session_config.py` gives every session its own pin at `~/.miniouto/session-config/<session>.toml` — **seven** keys (`provider`, `model`, `reasoning`, `subagent_provider`, `subagent_model`, `subagent_reasoning`, `style`). **Resolution order, per key: explicit override (`--pm` / `ChatOverrides`) > this session's pin > the global default** (`settings.provider` / `settings.subagent_*` plus the resolved provider record's `default_model` / `reasoning_effort`, computed by `core.runtime.default_provider_config` — storage must not import core, so the caller injects it).

**The STYLE is pinned like the model** (`style` is the seventh pin key). The style a session runs under resolves as: explicit `--style` > this session's pin > the global `settings.style` > `"default"`. It used to read the global default straight, so a style chosen for one session followed the user into the next one and the TUI's style picker rewrote the global default for EVERY session.

**Choosing a style makes it the DEFAULT, and the running sessions keep theirs.** `core/runtime.py:set_active_style(style, session=…)` is the ONE place a style becomes the default (the TUI picker and `miniouto style set` both go through it, so they behave identically): it sets `settings.style` (a session started NOW runs it) and FREEZES the style of every session that already exists into that session's own pin (`session_config.freeze_style` — only the `style` key, never the provider trio). A session that already pins a style keeps it; one without a pin freezes the style it was running under. So the choice is the default for the NEXT session and never reaches into a conversation the user is already in. `default_provider_config()` carries the style, so a session with no pin freezes the default it started with.

Three rules make it safe with many processes running at once:

- **A stored value is CONCRETE and `""` means "follow the global default at read time."** The pin is a set of overrides on the default, not a replacement for it: `set_fields(session, model="")` hands that one key back to the default without touching the others.
- **FREEZE AT FIRST USE.** `effective(session, default=…)` materializes the pin: a session with no pin file gets the current global default written as its pin and that value returned. So a model switch made by the TUI in another process can never redirect a session that is already running.
- **Seeding is copy-if-absent.** A freshly minted session inherits its source's pin via `seed_from(new, source)` — a delegated run's minted `chat-`/`spawn-` name (`cli/chat.py:_resolve_session`), a summary hand-off's `cont-*` name (`tools/continue_session.py`), and the TUI's "new session" (`cli/tui.py`) all seed from the session they continue. A session that already carries a pin keeps it, and a source with no pin seeds nothing (the new session freezes the default itself).

The global default is the **most-recently-changed** one — the TUI writes it. The per-call override is one flag: `miniouto chat --pm 'PROVIDER/MODEL[:REASONING]'` (e.g. `--pm "minimax/MiniMax-M3.1:high"`), the **only** provider/model/reasoning override `chat` has; `--provider`, `--model`, `--reasoning`, `--subagent-provider`, `--subagent-model` are deleted and must not come back. `--pm` splits on the FIRST `/` (a model name may contain `/`) and then on the LAST `:` (an optional reasoning suffix), and a missing/empty provider or model is rejected before any resolution.

The **TUI provider settings popup** (`cli/tui.py:ProviderSettingsPopup`, one instance serving both the `outo` and `subagent` chips via `TrioTarget`) shows three rows — `Provider` / `Model` / `Reasoning` — each opening its own picker. **Provider and Model both reset Reasoning**: an effort level belongs to a model, so either pick lands on that model's default. Every change goes through `_persist_trio`, which writes **BOTH** the session pin (so the edit survives a later default change) and the global default (so other processes and the next session follow the newest choice). For the outo trio that global write means `settings.provider` + the provider record's `default_model` / `reasoning_effort` (`dataclasses.replace` + `provider_store.upsert`); `settings.model` is never written. For the subagent trio it means the three `settings.subagent_*` keys, where empty still means "same as outo". A legacy `settings.model` from an older install is still read at the bottom of the default chain (`default_provider_config`), but it is invisible in the TUI.

The standalone TUI provider/model editors are **deleted**, including the one that edited an existing provider's `max_context_window` / `max_output_tokens` — those caps are set when the provider is added (the `+ add custom…` wizard asks for them) or by hand in `providers.toml`. **Do not reintroduce a per-provider edit screen or a `settings.model`-as-TUI-override path.**

### 13. lma-backed vs custom providers
`storage/providers.py:Provider` carries a `source: str` field, one of `SOURCE_CUSTOM` (default) or `SOURCE_LMA`. lma-backed (catalog) providers are added via `miniouto provider add …` or the TUI `+ add from catalog…` wizard; custom providers come from `provider custom add` or the TUI `+ add custom…` wizard. Both are reachable from the popup's `Provider` row (`SENTINEL_CATALOG_ADD` / `SENTINEL_CUSTOM_ADD` in `_pick_provider`), and the popup's `Model` row dispatches on `source`:

- `source == "lma"` → `ProviderSettingsPopup._model_flow` (fetches `/model-list` and shows a `SelectionModal`).
- `source == "custom"` → the same `_model_flow`, whose "no models" case falls back to a free-text `TextInputModal` (a hand-added endpoint need not exist in the catalog).

The CLI command `miniouto provider providers` filters its "Addable?" column through `core.providers.sdk_to_format(sdk, api)`. Providers whose SDK cannot be hosted by any of the four coreouto builtins (openai / openai-response / anthropic / google) return `(None, None)` and are skipped by both the CLI and the TUI wizard.

Note: the source string remains the literal `"lma"` (it predates the "catalog" UI rename) — `SOURCE_LMA = "lma"`. The TUI and CLI surface call these "catalog" providers, but the underlying field value is still `"lma"`.

### 14. lma cache lives in `core.lma._CACHE`
`core/lma.py` mirrors lma's 10-minute server-side TTL with a module-level dict. Cache keys are explicit (`"providers"`, `f"models:{provider.lower()}"`, `f"model:{provider.lower()}:{model.lower()}"` — both segments are lowercased); a cached `None` payload is meaningful (means "lma returned 404"). `core.lma.clear_cache()` exists for tests / manual refresh. **Do not cache anything other than `None` and successful payloads** — a transient transport error must not pollute the cache for 10 minutes.

### 15. Computer tool: screen registry, outo-only, auto-detected
The `Computer` tool (`tools/computer.py`, backed by py-Vwayland) drives virtual screens from a **module-level registry** (`_SCREENS`) — one compositor == one screen == one app, so multi-app work means explicitly spawning multiple screens (`spawn`/`kill`/`list` actions, `screen=` targeting; omission resolves lazily when zero or one screen is up, errors when several are). All screens are killed at process exit by vwayland's own atexit hook. Five rules follow:

- It is visible to **every** run, outo and delegated role alike: a role-mode run is a separate process that owns its own vwayland screens, so the old "one shared screen" rationale for hiding it from role runs is void — the tool list is now identical in both modes. It is still **platform-gated**: `core/runtime.py:PLATFORM_GATED_TOOLS` means "tools appended when the platform registered them", and the preset filters that list down to what actually got registered, so an unregistered Computer never reaches the preset.
- **Screenshot degradation by provider format, decided at call time**: `register_all(api_format=...)` refreshes a module flag on every `build_runtime` (per turn); the one registered Computer handler checks it when `screenshot` runs. openai (Chat Completions) rejects multimodal tool results while the provider formats the request — there `screenshot` returns an explicit notice (scoped to the CURRENT provider, and stating that a retry after a provider switch will work — so a notice burned into session history never blocks a later image-capable provider) instead of raising `ValueError` and killing the turn. anthropic / openai-response / google return the image as always. Call-time (not registration-time) decision keeps mid-session provider switches in the long-lived TUI correct. The registered handler annotates `action`/`scroll_direction` with Literals — safe since coreouto 0.11.3 emits `type` for them (type-less enums before that were rejected by strict validators with a 400, which is why the 0.11.3 floor exists); runtime validation of the values happens inside `computer()`.
- **The `screen` action deliberately BYPASSES that degradation gate.** It returns a `str` (never a `Screenshot`), renders the current capture as Unicode **sub-cell** art (`tools/ascii_art.py:render_subcell`), writes it to `~/.miniouto/computer-screens/<id>.art`, and returns a "Saved to: <path>" notice — so it stays fully usable on openai Chat Completions where `screenshot` returns a notice. Its extra params are `chars` (16..200 terminal cells, the art width target), `color` (truecolour SGR vs the plain ramp) and `glyph` (`"mix"` default / `"octant"` / `"braille"`). Art carries no clickable coordinates: coordinates still come from `screenshot`. `miniouto computer show [PATH] [--plain] [--cols N] [--head N]` re-renders a capture out of band (newest by default), which is also how a delegated role-mode run reads what the orchestrator captured.
- **Sub-cell art: 8 sub-pixels per cell, three glyphs, and one asymmetry.** `render_subcell` puts a **2x4** pixel grid in one cell (half-block's 2) and cuts each cell to two palette entries plus an 8-bit sub-cell mask by a farthest-pair-seeded 2-means; the mask picks the glyph — `█` for a uniform cell (every sub-cell within `UNIFORM_SPREAD = 12.0` of the mean), a **BLOCK OCTANT** (U+1CD00..U+1CDE5, Unicode 16.0) when the two entries are ≥ `OCTANT_MIN_SEPARATION = 60.0` apart and every sub-cell is ≤ `OCTANT_MAX_INTRA_ERROR = 24.0` from its own entry, else a **BRAILLE** pattern (`U+2800 + mask`) for anything that should blend. The 26 masks Unicode left unencoded fall back to braille. **BLOCK OCTANT digits are row-major, braille dots and the internal mask are column-major** — `_name_mask_to_internal` is the single permutation and both families read the same mask, so that asymmetry must never leak into a second code path. BLOCK OCTANT is Unicode 16.0, so a font without it renders tofu: `glyph="braille"` is the escape hatch. Mode string `subcell-truecolor` is new; legacy `halfblock-truecolor` still parses and renders byte-identically via the kept `render_halfblock`. `glyph` is **not** recorded in `.art` meta. After the glyph class is chosen, every **patterned** cell's pair is contrast-boosted about the cell mean to at least `MIN_LUMA_DELTA = 40.0` (Rec. 601, primary) and `MIN_PAIR_DISTANCE = 50.0` (secondary) — along its own direction (hue preserved) capped at `MAX_CONTRAST_BOOST = 2.0`, then along the neutral axis, both clamped to `[0,255]`; unreachable in gamut (clipped pair, structureless noise) collapses the cell to the mean `█`. Geometry: `subcell_rows` is **the same formula as `render_halfblock`** — `rows = max(1, round(cols * height / width / 2.0))`, the `/ 2.0` being only the cell's 1:2 aspect correction, since the 2x4 sub-pixels inside a sub-cell are square. A sub-cell capture therefore has the **same** cell rows as a half-block one (1280x720 @100 cols → `grid: 100x28`, `cell: 6x6` sub-pixel source size) and buys resolution *inside* the cell, never fewer rows.
- **`screenshot` writes the SAME art as a best-effort sibling, plus the raw PNG.** `_write_art_best_effort` renders the capture's `.art`; `_write_png_best_effort` writes the same frame to a sibling `.png` and hands the path back on `Screenshot.png_path`. `tools/registry.py` appends both lines to the screenshot's summary text — `Image saved to: <path>.png` first, then `Saved to: <path>.art` LAST (the older hook the TUI's `art_path_in_result` keys on). Best effort on purpose: a decode / render / write failure costs the sibling files and nothing else, never the capture. Captures are display artifacts, not archives: every write prunes `computer-screens/` to the newest `MAX_CAPTURE_FILES = 100` files (oldest-first by the timestamped name, `.art`+`.png` pairs retire together, prune failure never breaks a capture). The TUI renders the PNG as **real terminal graphics** (Kitty TGP / Sixel via `textual-image`) when the terminal supports it, mounting a widget sized to the `.art` lane's footprint; ANY failure (no package, non-graphics terminal, missing png, PIL decode error, mount failure) falls through to the `.art` text render unchanged. The `screen` action writes only `.art`, so it always renders as text art even on a graphics terminal.

**The graphics mode is detected ONCE, BEFORE Textual starts.** `tui.detect_graphics_mode()` runs in `run_tui()` and caches one of `GRAPHICS_MODE_TGP` / `GRAPHICS_MODE_SIXEL` / `GRAPHICS_MODE_TEXT`; the render path (`graphics_mode()`) only READS that value and never probes. The order is the whole point: the probe asks the terminal a question and reads the answer off **stdin**, and Textual's reader thread takes stdin over the moment the app runs — probing after that never sees the reply and only burns the 2-second timeout. Measured in a pty: the old render-time probe cost 0.65 s (2.0 s against a terminal that does not answer) on the FIRST screenshot the user waits for; the new boot-time probe costs 0 s there. Detection also warms the `textual_image.widget` import (Pillow, ~0.3 s) so the first screenshot does not stall on an import. A failed detection (no package, no tty, a closed stdout, a timeout) is `GRAPHICS_MODE_TEXT` — the unchanged `.art` lane, never an error.

**SIXEL WINS OVER TGP when the terminal offers both.** TGP is the better protocol, but its image-placement scheme needs a terminal that implements the placeholder protocol properly, and several terminals only *report* TGP support (they answer the query) without implementing it — Konsole and wezterm are the known cases, and there the TGP image never appears. Sixel is a plain byte stream any Sixel terminal renders, so it is the safe choice whenever available. TGP is therefore for terminals with Sixel absent — in practice Kitty, the one terminal that implements the placeholder scheme properly. (textual-image's own auto-renderable picks Sixel first for the same reason.) The mode picks the widget class too: `TGPImage` for TGP, `SixelImage` for Sixel, so the renderer and the probe can never disagree about the protocol.

**The PNG is checked BEFORE the `.art`.** A capture whose `.art` write failed (a full disk, a read-only home) still has its pixels, and those are exactly what the graphics lane shows — so the image gate must not sit behind the `.art` gate. The `.art` remains the fallback whenever the image lane declines.

**The image is a SIBLING row in the log, not a child of the text row.** Two reasons, both found by rendering the real app: a child widget is laid out at the TOP of its parent's content area — exactly where the parent's own caption is, so the caption and the image were drawn ON TOP OF EACH OTHER; and a `Static` with any child becomes a container, and a container whose only child is `display: none` computes its auto height as ZERO, so hiding the image collapsed the whole row out of the log. As a sibling row the image has its own lines, and hiding it leaves the text row's height alone. The image fills the log's content width (`width: 100%`, `height: auto`) — a fixed cell count overflows a narrow terminal and paints over the log beside it.

**The TGP widget uploads ONCE (`cli/graphics.py:CachedTGPImage`).** `textual_image`'s stock TGP widget calls `cleanup()` (a DELETE to the terminal) and rebuilds its renderable on EVERY repaint, which re-base64s and re-sends the whole PNG — ~1.8 MB per repaint on a 1280x720 capture, so scrolling the log re-uploaded the picture over and over (the "opening a screenshot lags" symptom). The TGP protocol does not need that: an image is uploaded once and then PLACED (`a=p`) wherever the cell grid says. `CachedTGPImage` keeps the renderable for the widget's lifetime (so `terminal_image_id` survives and no re-upload happens) and deletes the image from the terminal on unmount. Measured: a full repaint with a 1280x720 capture on screen cost 47.8 ms with the stock widget and 39.4 ms with this one, against 37.6 ms for a 33-line TEXT widget of the same size — the picture now adds ~1.8 ms (the diacritic grid) instead of ~10 ms of CPU plus a fresh 1.8 MB upload to the terminal. Verified end to end in a pty: 40 scroll moves → 1 upload, 1 placement. The SIXEL path needs no such wrapper — `textual_image`'s Sixel widget caches its encoded sixels itself.
- Every action runs under one module lock and the tool is registered `parallelizable=False` — vwayland opens a fresh IPC socket per call and its upstream spawn bookkeeping is not thread-safe, so without serialization concurrent tool calls could race (even across different screens).
- py-Vwayland is a **default dependency** (its wheel is pure-Python packaging with a bundled native binary, so it installs everywhere) but the compositor only runs on Linux x86_64 glibc ≥ 2.28. `tools/computer.py:computer_supported()` probes the platform once per process (cached) and `tools/registry.py:register_all` registers the tool only when supported — **unsupported platforms never advertise the tool to the model** (no schema tokens burned, no runtime failure). `MINIOUTO_COMPUTER=0` disables it explicitly; `=1` skips the probe (e.g. aarch64 source builds).
- The bundled styles still mention Computer unconditionally; on unsupported platforms a stray model call to it fails with coreouto's unknown-tool error, which is an acceptable teaching signal.

### 16. Alarm delivery: a real user message — append to live messages, or wake a turn
A delegated run's completion alarm — and any message a human or script inserted — is a synthetic **user** message whose first line is exactly `ALARM_HEADER = "[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]"` (`core/inbox.py:ALARM_HEADER`, the single source of truth shared with `core/repetition.py:alarm_text`, `cli/chat.py`, and the TUI). It is inserted with `kind="alarm"` and `source="chat"`. It must reach BOTH the chat (displayed as a user message) and the model. It may sit in the inbox **between** turns — that is what a queue is for — but it is never dropped and never left to rot: the moment somebody is awake, it is delivered.

- **The queue is a directory, not a deque** (invariant 20). `core/runtime.py:_make_alarm_hook` on `BEFORE_LLM_CALL` calls `inbox.drain(session)` every iteration and, if anything came back, joins the texts with a blank line and appends ONE `co.Message(role="user")` to the LIVE `messages` list, so the very next `provider.create` already carries it. There is **no depth gate**: the inbox is per-session, not per-actor, so any run bound to that session consumes it.
- **Idle** (the common case — the run already printed its answer and exited): somebody must wake the session, and there are exactly two wakeups. (a) The TUI's `ChatTUI._deliver_pending_messages` drains the inbox and starts a turn whose prompt IS the joined message texts (`turn.user` records it, so display / replay / model perception are the same string by construction), from the 1 s poll (`_INBOX_POLL_INTERVAL`, because nothing pushes any more — a detached process may write at any moment) or at the tail of `_dispatch`. (b) `miniouto chat --alarms -c [--name <s>]`, the explicit CLI wakeup: no prompt (the queued texts are the prompt), `-c` **mandatory** (history is never loaded implicitly), and the named session must exist or the run is rejected with `✗ nothing to wake: session '<s>' does not exist`. An **empty inbox is a no-op, not a turn** — one line, exit 0, no session marker, no finish marker, no model call — so a wake loop costs nothing when there is nothing to say. All four rejections (prompt given, no `-c`, no session named, session missing) are checked before any model call and before any session file is written. Either way the message is consumed exactly once, because `drain` unlinks.
- **A one-shot `chat` does NOT wake itself.** `run_turn_chain` is one turn plus the summarization hand-offs that turn requested (invariant 21); when the turn ends the run prints its one `------finish------`, prints the answer, and exits — even when `spawn_state.pending_spawns(session)` is non-empty or more messages have already arrived. Those stay in the inbox for the next wakeup. The old follow-up chain — its turn/wall/poll bounds and its `------follow-up ------` banner — is **deleted** and must not come back: waiting could only ever be satisfied by the markers this one run happened to start, while any producer anywhere may `miniouto message insert` into any inbox. `tests/test_chat_wakeup.py` is the spec.
- **Why not `Agent.inject_user_message`:** that queue is drained at the TOP of coreouto's loop iteration, i.e. *before* the hook runs, so a message queued from there would land a full LLM round-trip later; and it cannot wake a turn that already ended — which is exactly what `--alarms` and the TUI poll exist for.
- **Why fold instead of always appending:** if the last message is already a user message the text is folded into it, because providers reject two consecutive user messages — the same hazard `_cut_poisoned_turn` works around.
- Typing into the TUI **mid-turn** takes the same path: `_queue_inbox_message` calls `inbox.insert(session, text, kind=USER_KIND, source="tui")` rather than poking the live list, so one code path serves human interjections, alarms, and `--alarms` wakeups.
- Each drained message becomes one `LoopEvent(kind=<inbox kind>, actor="outo", subagent_id=meta["id"], subagent_name=meta["role"], detail=f"inbox:{id}")` via `core/chat.py:_make_alarm_dispatcher`, and the TUI renders both kinds as USER-style rows (the model receives them as `role=user`, so the display matches) both live and on replay. The idle path emits NO event — its `turn.user` IS the record, and a second record would double-render. A broken sink must not break the provider path — the hook body runs under `contextlib.suppress`.
- A repetition restart (`core/repetition.py:alarm_text`) shares the same header but is deliberately **not** routed through the inbox: `supervised_run` passes it as the resumed attempt's user message, and a `drain()` would deliver the same text a second time.

### 17. Repetition watchdog: force-stop a looping model, don't wait for the stall watchdog
A model stuck in a loop keeps talking and keeps calling tools, so the 30-minute stall watchdog never fires for it. `core/repetition.py` detects exactly two shapes per actor (`current_subagent_id() or "outo"`), offline and exactly (no edit distance, no model call). They count on different terms:

- **Phrase** — a **CONSECUTIVE RUN of a phrase UNIT**, never a total: the same normalized segment (split on newlines and `.?!。？！`, whitespace collapsed, casefolded, ≥ 4 chars) repeated `REPEAT_PHRASE_THRESHOLD = 50` times **back to back**. The unit generalises that to `MAX_UNIT_SEGMENTS = 8` segments, so `"A B A B ... A B"` trips too — `"A B"` itself is one consecutively repeated unit — while `"A B C D A F C A B B A"` (words reused in a changing order) is normal speech and never trips. Sizes are tried **ascending**, so the smallest unit that reaches the threshold is the one reported (`"50x: a b"` for a pair, `"50x: <segment>"` for one — the detail shape is byte-identical to the single-phrase case for `p=1`). Each size is measured by walking the `p`-blocks backwards from the END of the segment stream and stopping at the first mismatch, so the alignment must hold for the whole chain: a stream that merely contains repeated blocks (a shifted alignment, one interleaved segment) never fakes a trip. The stream is the subsequence of PHRASE segments across the whole TURN (thinking + content + deltas), fed through the shared buffering splitter of invariant 8, because a model repeating itself inside ONE never-ending answer is invisible to a boundary check. Runs therefore **SPAN messages** — a loop alternating two phrases across ten messages is the same loop — and **tool calls are not segments**: they neither grow nor break a run, symmetric with the tool streak, which narration does not break. Junk segments (< `MIN_SEGMENT_CHARS`) never enter the stream, so they neither grow nor break a run either. `ON_ITERATION` only **flushes the tail** (`flush_stream`), so a phrase cut mid-sentence at the boundary still counts; the run state itself is never cleared at a boundary. `reset_phrases` is the one clear — a discarded (sanitized-away) tail — and it drops the run state without flushing.
- **Tool** — **consecutive**: the same call signature (name + `json.dumps(args, sort_keys=True)`) `REPEAT_TOOL_THRESHOLD = 30` times **back to back**; any different call in between resets the streak to one. Consecutive on purpose: a long turn legitimately re-issues the same utility call (`git status`, a screenshot) scattered among other work, and warning on 30 scattered calls would teach the user to distrust the detector. Only an unbroken streak means a stuck model. (`_ActorCounters` therefore keeps `last_tool_key` + `tool_streak` instead of a `Counter`; verdict stickiness and the one-shot trip notifier are unchanged.)

A verdict is **sticky** until consumed (`consume_verdict` also clears the counters, so a resumed attempt must earn a new one). Enforcement is deliberately asymmetric, because the actors are not supervised the same way:

- outo's turn runs under `core/runtime.py:supervised_run`, which checks repetition **before** the stall test (a looping model is active, not wedged), cancels the attempt, sanitizes the live history, and resumes with the alarm **as the user message** (`start(alarm, history)`; merged into the original prompt when no transcript is recoverable). The stall path is the sibling that resumes with `start(None, history)` — the difference is exactly the injected alarm. Because the stream path trips the verdict mid-flight, this can cut a message that **never completed**; the record rule below is what keeps that text from vanishing.
- **The truncated answer is recorded, not retracted.** `core/chat.py:_run_with_watchdog`'s `on_repetition` FIRST calls `repetition.flush_stream` and emits the still-open stream partials (`_PartialStreams.drain`) as ordinary `LoopEvent(kind="thinking"|"response", …)` events — the verbatim concatenated deltas, no marker, no trimming — and only then the `kind="alarm"` event, then clears them. `msg_ref` stays `None` because the assistant message never existed, which is also exactly why no removal path (invariant 18) can ever take the row back: the turn is stopped, not deleted. `ON_ITERATION` (`partials.close_message`) is the normal counterpart and hands the text to the completed message's own event, so the completion path (a repeated final answer, no cut) flushes nothing. In the TUI the truncated event finalizes the live row in place: a `response` event is suppressed when a live row exists, a `thinking` event finalizes the live thinking row.
- The budget is its own: `REPEAT_MAX_RESTARTS = 3`, deliberately **independent of** `WATCHDOG_MAX_WAKEUPS`, because the two budgets must not spend each other. Exhausting it raises `LoopRepeatedError`, a **sibling of `LoopStalledError`, not of `LoopCancelledError`** — a repeat is the model's own failure, never a user stop, and `run_chat` handles it in its own branch.
- **There is no unsupervised actor.** `core/repetition.py:cancel_background_loop` is deleted: every run — outo and every delegated `chat --role` run — executes inside its own `supervised_run`, so a trip is always enforced by restart, never by cancelling a job.
- Counters live for one turn: `reset()` runs from `runtime._register_watchdog_trackers` alongside `_LIVE_MESSAGES.clear()`.

### 18. Message-removal consistency: what the model loses, the record loses
`LoopEvent.msg_ref` is `str(id(message))` of the live coreouto `Message` an event came from. It is stamped in exactly one place: `core/chat.py:_make_iteration_dispatcher`, because `ON_ITERATION` is the only hook that fires *after* coreouto appends the assistant message, so `messages[-1]` IS that message. Thinking and response rows are stashed until that hook (so the stamp can be attached) and flushed there; the tool dispatchers read the per-actor `turn_refs` slot `BEFORE_TOOL_CALL`/`AFTER_TOOL_CALL` published there, which is why a `tool` event and its `tool_result` always share one ref and are removed together.

Two removal sources report the ids they discarded, **before** deleting:

- `core/runtime.py:_make_history_repair_hook` → `_repair_pairing` (dedupes duplicate tool results) and `_cut_poisoned_turn` (excises one poisoned assistant tool-call turn plus its own results) on a 400/422 retry.
- `core/runtime.py:sanitize_history` (the stall/repetition restart path) truncates the dangling tail.

Both feed `build_runtime(on_messages_removed=…)` → `core/chat.py`'s depth-gated `on_messages_removed` → `EventSink.remove_events(msg_refs)`, which prunes `self.events` **in place** (the persister holds that exact list), notifies the persister **unconditionally** (a repair can cut a message from an *earlier* turn of a resumed session, whose events exist only in the file — hence `sessions.prune_turn_events`, which sweeps every turn), and always forwards to the wrapped sink so the TUI drops matching rows via `_RowWindow.remove()` and the console prints its removal note.

Three exclusions are deliberate: **a hand-off never prunes** (invariant 21's summary switch changes sessions, never edits history — but the display transcript is the human record of what happened); **additions are not removals** (a filled-in tool result, an explanation note folded into a surviving user message) and are never reported; and **alarm/wakeup events carry no ref at all**, so they are additions that are never pruned. `msg_ref` is process-local, so pruning is a live-turn repair: a session reloaded in a later process keeps what it already recorded.

### 19. Sessions are grouped by the directory they were created in
`SessionData.cwd` records `INVOCATION_CWD` and is stamped by `storage/sessions.py:begin_turn` **on the first turn only** — a session belongs to the directory it was created in, so later turns (even from another directory) never overwrite it. The field is additive: `save()` writes it only when non-empty and `load()`'s v2 branch reads it, so `SCHEMA_VERSION` stays 2 and older files round-trip with `cwd=""` (they group as "other").

`list_sessions_with_meta() -> list[(name, cwd, mtime)]` parses the session JSON **directly** and never goes through `load()`, because `load()` calls `ensure_dirs()` and a read-only picker must never write (every picker open would otherwise force-refresh the five bundled style files). Missing, unreadable or unparseable files yield `("", 0.0)`.

Grouping is a presentation concern: the TUI session picker renders two `SelectionModal` sections ("This directory" first, then "Other directories" with a `[basename]` prefix and `[?]` for legacy files) via the modal's opt-in `sections=` parameter (default None = the old flat rendering, so the style/provider/model pickers are byte-identical), and `miniouto status` prints two lines, `Sessions (this directory)` and `Sessions (other)`.

### 20. Message insertion is a cross-process file inbox
`core/inbox.py` is the hand-off between processes, so its shape is load-bearing, not incidental:

- **One message = one atomically-written file.** `insert` writes `<ts>-<6hex>.tmp` then `os.replace`s it into `inbox/<session>/`, so a drainer sees a message either not at all or complete — never half-written. A writer racing a drain is never lost, only deferred.
- **`drain` is `listdir → read → unlink`** (unlink *after* a successful parse, so a crash re-delivers rather than drops). No lock is needed and none is taken: an append-only `.jsonl` would have to hold a lock across the read while the writer waits on it. Corrupt content is renamed `<stem>.bad` and skipped — never raises, same as session files. `MAX_INBOX_MESSAGES = 200` are kept per session (`prune` deletes the oldest).
- **Both kinds are user messages.** `kind` is `alarm` (a delegated run's completion, `source="chat"`; a summary request, `source="context"`; a summary continuation, `source="summary"`) or `user` (a human/script interjection); all render as USER-style rows and reach the model as `role=user` (invariant 16).
- **Single consumer per session, deliberately.** Nothing serializes two *drainers*: whichever process lists the directory first unlinks the files and the other never sees them. Two live processes bound to one session steal each other's messages — isolate them with a distinct `MINIOUTO_HOME` (or session name) instead of adding a locking protocol here. A delegated run is **not** an exception: the child borrows its own name as `runtime.session` for its chain (invariant 5, `_runtime_session_scope`), so it drains **its own** `inbox/<session>/`, never the caller's.
- **A bg run's liveness is a separate on-disk fact** (`core/spawn_state.py`): one `spawn-logs/<id>.json` marker per detached run, `running` → `done` / `error` / `stopped`. The marker carries **two** session names: `alarm_session` (the caller's, which receives the completion message and is what `pending_spawns(session)` filters on) and `session` (the run's **own** session, where its transcript lives — for `--bg` a generated `spawn-…` name that never coincides with the caller's). That second field is how a receiver of an alarm locates the transcript. Liveness is a promise, not a proof: a SIGKILLed run never calls `finish_marker`, so the marker says `running` forever — `pending_spawns` therefore keeps only markers whose `pid` is still alive (`is_pid_alive`, where an unknown pid counts as alive), which is what a **monitor** reads, not what a chat run waits on.

### 21. Summarization is explicit and file-based
**There is no implicit summarization or compaction.** The old auto-compaction machinery is gone: nothing rewrites the transcript behind the model's back, no second LLM produces a handoff, and `history` is never compacted (invariant 7's invariant is "the record matches what the model saw"). The harness instead **asks**, and the model **writes files**, and a tool **changes session**. The rule the owner pinned: don't summarize implicitly.

- **The trigger is a measurement, not a rewrite.** `core/context.py:make_context_monitor(model, session, provider_name)` is built per turn in `core/runtime.py` (same lifecycle as the watchdog trackers) and registered as one `ON_ITERATION` hook. Occupancy is best-effort and silent when unknown: `response.usage.prompt_tokens` → `usage.total_tokens` → a chars/4 estimate over the live messages (`_tokens_used`); `None` means no message at all. At `CONTEXT_SUMMARY_TRIGGER_PCT = 80` it fires **once per hook instance** and does exactly one thing: `inbox.insert(session, build_summary_request(...), kind="alarm", source="context")`. It returns `None` (no hook) when the window size is unknown, since a threshold needs a denominator. Delivery then rides the existing `BEFORE_LLM_CALL` inbox hook (invariant 16), which is what makes the request *recorded and replayed* like any other alarm rather than a private side channel.
- **The request pins the whole contract**, `core/summary.py:build_summary_request`. The shared header `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]` (`core/inbox.py:ALARM_HEADER`), then the second line `Context budget: {pct}% of this session's window is consumed ({used}/{window} tokens). Explicit summarization is required now.`, then the current situation (usage, window, used) / **WHY SUMMARIZE** (this harness does not summarize implicitly) / **HOW TO SUMMARIZE** (`mkdir -p ./.miniouto/summary/<session>/` via Bash, then `SUMMARY_FORMAT_SPEC`: `00-overview.md` **required** plus optional `NN-<topic>.md`, in the 7 sections Background & Goal / Current State / Key Decisions & Rationale / Next Work / Gotchas & Verification / File Map / Original Brief & Contract) / **AFTER SUMMARIZING** (call `continue_session(summary_dir=…)`) / **IF YOU SKIP THIS** (skipping it loses context and repeats work). The text is part of the model-facing contract, pinned as literals with only the `{...}` values substituted.
- **The folder is project-local, on purpose.** `summary_dir_for(session)` = `<INVOCATION_CWD>/.miniouto/summary/<session>/`, **not** under `~/.miniouto/`. A summary is a project artifact: it is meant to be read, reviewed and committed with the work it describes. Nothing else in the harness may write there, and the tool resolves relative paths against `INVOCATION_CWD`.
- **`continue_session(summary_dir)` is the only way a session changes** (`tools/continue_session.py`, sync, in `BASE_TOOLS`, so outo and every delegated role run have it). It validates the folder is a directory holding at least one `*.md` — and nothing more, because only the reading session can judge the summary — mints `cont-<YYYYMMDD-HHMMSS>-<6hex>`, inserts `build_continuation_message` (header `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]` + `New session opened from a summary — continue the previous work here.`) into the **new** session's inbox (`kind="alarm"`, `source="summary"`, `meta={"from": <old>}`), records `summary.request_handoff(...)`, and returns an ack that tells the model to stop. A rejected folder returns the reason (not an exception), so the model can write the docs and retry.
- **A hand-off ends the turn cleanly.** `continue_session` returns → the `AFTER_TOOL_CALL` dispatcher in `core/chat.py` sees `summary.pending_handoff()` and fires `on_handoff` → `_StopOnHandoff` (a `threading.Event` ORed with the caller's cancel event) trips the runtime's cancel guard, so the loop stops at its next step instead of burning the rest of the context on work the new session is about to redo. It travels the ordinary `LoopCancelledError` path and is **re-classified at the call site** into `status="done"`, with a final answer naming the summary dir and the new session. The incrementally persisted history is kept and **the hand-off stays pending** — the loop layer never consumes it, because only the entry layer can move the conversation.
- **The entry layer performs the transition.** `cli/chat.py:_run_handoffs` checks `summary.pending_handoff()` after each turn and, within the *same* run, `_apply_handoff` moves three things together or the continuation would be lost: `opts.session`, the published run contract (re-registered under the new session so a second hand-off summarizes the right one), and `settings.session` (which is what `runtime.session` resolves to — the name the `BEFORE_LLM_CALL` hook drains). This is the **one** loop `run_turn_chain` still has: the model summarized, so it must not be re-woken to read its own continuation message. `_continuation_prompt` drains the new session's inbox once (that drain IS the delivery) and rebuilds the prompt from `core.summary.build_continuation_message` plus the run contract if it came back empty. `HANDOFF_MAX_TRANSITIONS = 5` bounds the chain (`summary → cont-1 → cont-2` …); past it the chain warns, naming the session and the summary folder, and stops. Each transition is announced with `handoff_marker` — `------summary → <new>------` + `summary: <dir>`, followed by the ordinary `------<new>------`, so the CLI reads like `------finish------`. The TUI mounts the same marker as a system row in the **old** conversation, persists it for replay (`_persist_handoff_marker`), re-pins the new session, and lets the 1 s inbox poll auto-deliver the continuation with no user action.
- **Delegation contracts travel across.** `cli/chat.py:_register_run_contract` publishes `session`/`role`/`style`/`alarm_session`/`return_path` into `core/summary.py`'s run-contract slot before the turn; `continue_session` folds it into the continuation message's **DELEGATION CONTRACT (carried over)** block and into the ack. The post-step therefore fires from the **final** session: `ChainState.session` is what the alarm's `Session:` line, the result file path and a `--bg` marker's `session` field name, and `_handoff_note` adds `Continued from: <old> (summary: <dir>)` to both the alarm and (as a trailing `---` section, so the file still leads with the answer) the result markdown. A hand-off is a **session change, never a history edit** — which is why it is one of invariant 18's three pruneless cases.

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
| `__init__.py` | `__version__ = "0.11.0"` |
| `__main__.py` | `python -m miniouto` — same entrypoint as the console script (this is how a `--bg` child re-enters the command) |
| `paths_runtime.py` | `INVOCATION_CWD: Path` (captured cwd at import, used by every tool to absolutize relative paths) |
| `cli/__init__.py` | Typer `app`, root callback (TUI fallback), `status` command |
| `cli/chat.py` | `chat_cmd` + `run_turn_chain` (one-shot chat: ONE turn, then the summarization hand-off transitions bounded by `HANDOFF_MAX_TRANSITIONS` — it never waits on spawn markers or on the inbox) **and the delegation + wakeup surface**: flags `--pm --style --role --use-subagent-provider --alarm-session NAME --return [PATH] --bg`, plus the orthogonal wakeup door `--alarms` (bundled styles teach the delegation recipe with `--pm {subagent_pm}`) (+ hidden `--bg-child --job-id`); `_parse_pm` / `_pm_from_run` (the `'PROVIDER/MODEL[:REASONING]'` spelling `--bg` re-emits), the `--role`-requires-`--style` gate at the top of `chat_cmd`, `_validate_role`, `_resolve_return_path`, `_resolve_alarm_session`, `_resolve_session`, `_bg_argv` + `_detach`, the 5-line `--bg` ack, `_marker` (carries both `alarm_session` and the run's own `session`), `_Run.delegated`, `_run_delegated` → `_write_answer_file` / `_alarm_text` + `_deliver_alarm` / `_complete`, `MAX_ALARM_ANSWER_CHARS = 4000`, **and the summary hand-off transition** (`HANDOFF_MAX_TRANSITIONS = 5`, `_apply_handoff`, `handoff_marker`, `_handoff_note`, `ChainState`, `_register_run_contract`) |
| `cli/computer.py` | `miniouto computer show [PATH] [--plain] [--cols N] [--head N]` — renders a `.art` capture out of band (newest by default); reads `tools/ascii_art.py`, writes nothing |
| `cli/message.py` | `miniouto message insert/list/clear` — the CLI side of `core/inbox.py`; `insert` takes `--session --kind --source --stdin`, `list`/`clear` default to `settings.session`; `_peek` reads without consuming (`drain` is the delivery primitive) |
| `cli/provider.py` | `provider providers/models/add` (catalog browse + add) + `provider custom add` + `provider list/remove/default` |
| `cli/style.py` | `style list/set/add/update/show` |
| `cli/skill.py` | `skill list/show` (read-only) |
| `cli/subagent.py` | `subagent show/set/clear` — manages `settings.subagent_provider/model/reasoning`, the trio `chat --use-subagent-provider` consumes via `runtime._resolve_subagent` |
| `cli/tui.py` | `ChatTUI` (Textual App), `run_tui()`, `tui_summary()`; the harness's own dark theme `MINIOUTO_THEME` (`THEME_NAME = "miniouto"`: **comfort-tuned** with a **THREE-LAYER text hierarchy** — `$background` `#0a0e14` (lifted off the near-black `#05070a` that glared, still darker than stock `#121212`), surfaces `#0f141b`/`$panel` `#151b24`/`$boost` `#1d2530`, and the text ramp ordered **answer > body > muted > disabled**: `$text-answer` `#c5d0d8` at **12.33:1** on `$background` (painted by `AnswerRow` only, so the agent's final answer is the single brightest layer and reads as the focal point), body `$foreground` `#a9bac5` at **9.69:1** (3.4× the chroma of the `#a8b0b1`/8.76:1 it replaced — S≈0.19 vs S≈0.06 — which is what "too gray" meant), `$text-muted` `#7b8490` at 5.11:1 for the event/tool/thinking rows, `$text-disabled` `#434a52` at 2.16:1; desaturated `secondary` `#8f86d8` / `success` `#52bd8a` / `warning` `#c99a4e` / `error` `#d16f7e` (each ≥4.5:1 on all four surfaces), accent `#2fc2ff` carried by BOTH `$primary` and `$accent` — owner-pinned and left exactly as it is; `boost`/`text-muted`/`text-disabled`/`text-answer` ride in the theme's `variables` dict because Textual drops the `boost` field when `panel` is set and derives its own `auto` text ramp. `AnswerRow` carries `color: $text-answer` as a single rule because the row's content is a **Rich** `Markdown` renderable (one `RichVisual`, no DOM children) — a descendant selector would be dead code. `$text` is deliberately NOT overridden: Textual's `auto 87%` ramp is what supplies the near-black ink on the picker's solid-cyan selection bar (8.80:1), while the transcript body paints the declared `foreground`), registered in `__init__` and booted through `_boot_theme` — `settings.theme` wins, an unregistered name falls back to `miniouto`; row-widget chat log (`EventRow`/`ThinkingRow`/`ToolRow` — tool calls render as collapsible boxes carrying only a `▶`/`▼` toggle glyph (highlighted on hover itself, the rest of the row never tints) plus their attached results; a plain click anywhere on the row toggles it, a drag that selects text never does — `ClickToToggle` tracks the press/drag and `RowStatic.text_select_all` is a no-op so a double-click selects no whole row) with live token streaming (`StreamRow` + `_StreamCoalescer`), windowed mounting (`_RowWindow` + `ChatScroll`: only a trailing window of rows is mounted, older chunks mount on scroll-up, far-above-viewport rows evict; follow-mode via Textual's built-in `anchor()`), a `CHAT_MAX_WIDTH = 150` centered column whose header chips sit in two left/right-anchored pairs (`outo`/`style`, `subagent`/`session`) and render their values whole (no truncation, so every picker stays clickable at any terminal width), **a pinned session name** (`self._session_name` / `_pin_session` / `_pinned_session` / `set_session`: minted at first dispatch as `tui-<ts>-<hex>`, or set by the session picker / "new session"; `_dispatch` and the 1 s inbox poll read the pin and never re-read `settings.session` mid-conversation, so an external mutation cannot redirect a live chat), priority scrolling with every action overridden to `animate=False` (PgUp/PgDn/Home/End) plus a jump-to-bottom button, `.art` truecolour rendering of a `Saved to: …art` tool result, and the inbox: `_deliver_pending_messages` on a 1 s poll + `_dispatch` tail, `_queue_inbox_message` for mid-turn typing, `_claim_inbox_event` for optimistic-row de-duplication, plus the summary hand-off (`_apply_handoff` mounts `handoff_marker` as a system row in the OLD conversation and `_persist_handoff_marker` keeps it on disk for replay, then re-pins to the new session so the 1 s poll auto-delivers the continuation). **No `SubagentRow` / `SubagentDetailScreen`** — cross-process delegated runs have no per-actor row stream to drill into. |
| `core/__init__.py` | Re-exports `chat`, `events`, `lma`, `providers`, `runtime` (NOT `context`) |
| `core/chat.py` | `ChatOptions` (incl. `cancel_event`, `role`), `run_chat(opts, sink=None)` (builds its `_TurnPersister` unconditionally — every run, delegated or not, writes its session file; `-a` is stdout-only), `_actor_label` (always `("outo", None)`), `_RecordingSink` (forwards stream deltas without recording them; `remove_events` = the display-prune entry point), `_PartialStreams` (verbatim per-message stream partials, drained to record a force-stopped truncated answer), `_make_stream_dispatcher` (feeds `repetition.note_stream_delta` + the partials; display otherwise), `_run_with_watchdog` (thin level-0 wrapper over `runtime.supervised_run`, also supervising outo's repetition and recording the truncated answer), `_TurnPersister` (incremental session persistence + `prune_turn_events` re-persist), `ToolCallArgsError`, failure diagnostics, sink dispatchers (`_make_tool_call_dispatcher`, `_make_tool_result_dispatcher`, `_make_response_dispatcher`, `_make_thinking_dispatcher`, `_make_alarm_dispatcher` (one loop event per drained `InboxMessage`, `detail="inbox:<id>"`), `_make_iteration_dispatcher` (stamps `msg_ref`)) |
| `core/context.py` | lma `/model` fetcher (via `core.lma.get_model`; transport failures are never cached) + **the context monitor only** — `make_context_monitor(model, session, provider_name)` → one `ON_ITERATION` hook that, at `CONTEXT_SUMMARY_TRIGGER_PCT = 80`, inserts the summary request into the session inbox (best-effort usage: `response.usage.prompt_tokens` → `total_tokens` → chars/4; `None` → silent), `clear_model_cache`, `get_max_output_tokens` / `get_context_window` (invariant 4's caps) |
| `core/summary.py` | The explicit summarization contract (invariant 21): `summary_dir_for` (`<INVOCATION_CWD>/.miniouto/summary/<session>/`), `build_summary_request` / `build_continuation_message` (the two pinned texts, both opening with the shared alarm header), `SUMMARY_FORMAT_SPEC` (the 7-section document layout), `Handoff` + `request_handoff`/`pending_handoff`/`consume_handoff`/`clear_handoff`, and the run-contract triple `set_run_contract`/`get_run_contract`/`clear_run_contract` (`session`, `role`, `style`, `alarm_session`, `return_path`). Touches no disk, no provider, no registry |
| `core/events.py` | `LoopEvent` (with `subagent_id`, `subagent_name`, `detail`, `msg_ref`), `EventSink` protocol (incl. the transient `emit_stream_delta` and the prune hook `remove_events`), `StreamKind`/`STREAM_TEXT`/`STREAM_THINKING`, `NullSink`, `ConsoleEventSink` (CLI spinner + loop-event rendering; skips `tool_result` events and stream deltas; prints a dim removal note) |
| `core/error_rules.py` | Per-format `ErrorRule` lists (coreouto >= 0.10 provider-level `error_handling`) ending with coreouto's `TIMEOUT_ERRORS` preset (>= 0.11, `exc_type` match — the HTTP-level stall wakeup for `API_STALL_TIMEOUT_SECONDS`) + `default_error_handling(api_format)`. Tool-call 400/422s use `reaction="retry"` (NOT `tool_result` — that loops forever on malformed calls; coreouto examples/29) paired with the history-repair hook in `core/runtime.py` |
| `core/lma.py` | `lma.blp.sh` REST client + `slugify` + `find_provider`; in-process 10-min cache |
| `core/inbox.py` | Cross-process message inbox (`~/.miniouto/inbox/<session>/`, one atomically-renamed JSON file per message): `ALARM_HEADER` + `ALARM_KIND`/`USER_KIND`, `insert` (validate → tmp → `os.replace` → prune), `drain` (read → unlink, `.bad` quarantine on corrupt), `pending_count`, `prune` (`MAX_INBOX_MESSAGES = 200`), `InboxMessage` |
| `core/providers.py` | `SUPPORTED_FORMATS`, `sdk_to_format`, `add_provider_from_lma`, `build_coreouto_provider`, `clear_coreouto_state` |
| `core/spawn_state.py` | File-based liveness markers for detached runs (`spawn-logs/<id>.json`, next to the stdio log): `SpawnMarker` (frozen; carries BOTH `alarm_session` — the caller's, which receives the alarm — and `session`, the run's own), `write_marker` (atomic), `finish_marker` (`done`/`error`/`stopped`, idempotent), `pending_spawns(session)`, `read_marker` (never raises) |
| `core/repetition.py` | Repetition watchdog: `note_text` (whole message) / `note_stream_delta` (real-time, buffered tail) / `flush_stream` (message boundary + force-stop path: count the tail) / `reset_phrases` (discarded-message hygiene: clear the run state WITHOUT flushing) / `note_tool_call` / `check` / `consume_verdict` / `reset`, one shared segment splitter (`_split_completed` + `_segments`) plus the run detector (`_unit_run`, block-chain comparison from the end over a bounded per-actor segment tail), `REPEAT_PHRASE_THRESHOLD` + `REPEAT_TOOL_THRESHOLD` + `REPEAT_MAX_RESTARTS` + `MAX_UNIT_SEGMENTS`, `alarm_text` (shares `ALARM_HEADER`) |
| `core/reasoning.py` | lma `reasoning_options` resolver → `provider_passthrough` kwargs (`resolve_reasoning_passthrough` + UI helpers `reasoning_choices`/`default_reasoning_choice`); google unsupported |
| `core/runtime.py` | `RuntimeConfig`, `ChatOverrides`, `build_runtime` (all slow resolution hoisted above `clear_coreouto_state()`; **role mode**: `runtime.role` selects a style tag body as the system prompt — the tool list is IDENTICAL in both modes, `Computer` included), `LoopCancelledError` + cancel-guard hook (cooperative loop stop via `threading.Event`), `supervised_run` + `sanitize_history` + `LoopStalledError` + `LoopRepeatedError` + `WATCHDOG_*` (stall watchdog, coreouto examples/27+28, plus the repetition restart path `start(alarm, history)`), history-repair hook (`_make_history_repair_hook` + `_repair_pairing`/`_cut_poisoned_turn`, coreouto examples/29 — repairs poisoned tool-call history in place before each 400/422 retry; each fire pairs first, then cuts ONE suspect turn — malformed-arguments turns first; both report removed ids via `on_messages_removed`, and `on_history_repair` persists the repaired history so the next turn doesn't reload the poison), inbox alarm hook (`_make_alarm_hook` on `BEFORE_LLM_CALL` → `inbox.drain`), `expand_prompt_tokens` + `_SKILLS_TOKEN` / `_replace_skills_token` / `_resolve_skill_roots` / `_skills_catalog` (substitute `{cwd}` / `{skills}` / `{skills:…}` / `{this_session}` / `{style}` / `{subagent_pm}` into BOTH the outo and role bodies; the regex pass runs first, then the plain replaces; unknown `{...}` stays literal), the provider resolution chain (`default_provider_config` computes the global default tier, `_pinned_for` reads/freeze the session's pin, `resolve_runtime_from_settings` applies overrides > pin > default), `_resolve_subagent` (consumed by `chat --use-subagent-provider`), hooks |
| `storage/__init__.py` | Re-exports submodules (NOT `skills`, NOT `session_config` — both are imported directly) |
| `storage/paths.py` | Path constants (incl. `STYLE_REPOS_FILE`, `INBOX_DIR`, `SPAWN_LOG_DIR`, `COMPUTER_SCREENS_DIR`, `SUBAGENT_RESULTS_DIR`, `SESSION_CONFIG_DIR`) + `ensure_dirs()` (creates the skeleton incl. those dirs, force-refreshes bundled styles) |
| `storage/providers.py` | `Provider` dataclass (with `source: SOURCE_CUSTOM \| SOURCE_LMA`, optional `max_context_window`/`max_output_tokens`/`reasoning_effort` overrides) + `SOURCE_*`/`VALID_SOURCES` constants + TOML CRUD |
| `storage/session_config.py` | The per-session pin at `session-config/<session>.toml` (invariant 12): `ProviderConfig` (seven frozen keys — the six provider ones plus `style`; `""` = follow the default) + `path_for` / `read` / `effective` (freeze-at-first-use, injected `default` callable) / `set_fields` / `seed_from` (copy-if-absent) / `clear`, atomic writes, corrupt pin treated as absent |
| `storage/sessions.py` | `SessionData` (`cwd`, stamped once) + `TurnRecord` (schema v2: restorable `history` + display `turns`, `status` running/done/interrupted) + JSON CRUD with v1 migration + incremental turn persistence (`begin_turn`/`update_turn_events`/`update_history`/`prune_turn_events`/`finish_turn`, atomic saves) + `list_sessions_with_meta` (direct-JSON lister for cwd grouping) |
| `storage/settings.py` | `Settings` (`provider`, `model` (legacy, read-only), `style`, `session`, `theme` (defaults to `miniouto` — the TUI boots on `cli/tui.py:MINIOUTO_THEME` and any other registered name here overrides it), `subagent_provider`, `subagent_model`, `subagent_reasoning` — the GLOBAL DEFAULT half of invariant 12's resolution) + TOML CRUD. the old spawn-defaults keys are GONE — `--return` / `--alarm-session` resolve per call only |
| `storage/skills.py` | `Skill` discovery from the two **default roots, local first** — `default_roots(cwd=None)` → `[<cwd or INVOCATION_CWD>/skills, SKILLS_DIR]`; `list_skills(directory: Path | None = None)` — `None` lists BOTH defaults in order (each `Skill.directory` names its own root), a named root is read alone and is what a style's `{skills:<dir>}` token asks for; `get_skill(name)` searches the defaults local-first (NOT in `__all__`) |
| `storage/styles.py` | Style CRUD + `add_from_repo` (records repo in `style_repos.toml`) + `record_repo`/`list_repos` + `parse_style` (-> `(outo_prompt, list[SubagentSpec])`) + `SubagentSpec` (frozen: `name`, `prompt`) + `builtin_default`. Bodies are returned verbatim, `{this_session}` / `{style}` included — token expansion happens in `core/runtime.py:expand_prompt_tokens`, not here |
| `storage/toml_io.py` | `tomllib` + `tomli_w` wrapper |
| `tools/__init__.py` | Re-exports |
| `tools/ascii_art.py` | `.art` format owner (pure stdlib): `decode_png_rgba` (own PNG decoder), the two renderers — `render_subcell` (default; 2x4 sub-cells, `█`/BLOCK OCTANT/BRAILLE, `glyph=`, contrast-boosted pairs) and the legacy `render_halfblock`, `subcell_rows` (shared row formula) + `OCTANT_GLYPHS` + `_name_mask_to_internal` (row-major octant names → column-major braille mask), `UNIFORM_SPREAD`/`OCTANT_MIN_SEPARATION`/`OCTANT_MAX_INTRA_ERROR` + `MIN_LUMA_DELTA`/`MIN_PAIR_DISTANCE`/`MAX_CONTRAST_BOOST`, `build_art` / `parse_art` (`ART_VERSION`/`ART_BEGIN`/`ART_END` envelope + meta header), `art_to_plain` (+ `strip_sgr`, coverage-blends both glyph families), `MODE_COLOR_SUBCELL`/`MODE_COLOR`/`MODE_PLAIN` |
| `tools/bash.py` | `async bash(command, *, cwd, env)` — stdin is `/dev/null` (never inherited; a stdin-reading command would block forever), `start_new_session=True` + `os.killpg` so timeout/cancel kills the whole process group, and a `PIPE_GRACE_SECONDS` (5 s) grace kills pipe-holding background children after the shell exits (1-hour hard cap: `BASH_TIMEOUT_SECONDS`) |
| `tools/continue_session.py` | `continue_session(summary_dir)` (sync, in `BASE_TOOLS`) — the only way a session changes: validates the folder holds at least one `*.md`, mints `cont-<ts>-<hex>`, inserts the continuation message (`core.summary.build_continuation_message`, `kind="alarm"`, `source="summary"`, `meta={"from": ...}`) into the NEW session's inbox, records the hand-off via `summary.request_handoff`, and returns an ack telling the model to end the turn. Relative paths resolve against `INVOCATION_CWD` |
| `tools/media.py` | `load_image/load_video/load_audio(file_path)` → `LoadedMedia` (pure stdlib; `registry.py` wraps results into `co.ImageBlock`/`VideoBlock`/`AudioBlock`) |
| `tools/computer.py` | `computer(action, …)` — virtual-display GUI control via py-Vwayland; module-level screen registry (`spawn`/`kill`/`list`, one app per screen) + lock; the `screen` action renders the capture as sub-cell art (`render_subcell`, with the `glyph` param threading `computer()` → `_dispatch` → `_render_screen_art` / `_write_art` / `_write_art_best_effort`) and writes `computer-screens/<id>.art` (and `screenshot` writes the same art best-effort onto `Screenshot.art_path`); `computer_supported()` platform probe gates registration (`MINIOUTO_COMPUTER=0` disables); returns `str` or `Screenshot` |
| `tools/registry.py` | `register_all(api_format=…)` — wires Bash/Image/Video/Audio/Computer into coreouto (Computer with `parallelizable=False`; the openai no-image gate is set per turn, and `screen` bypasses it) |
| `default_style/default.md` | Minimal fallback style |
| `default_style/coding.md` | Expert coding agent (~1.6 KB, deliberately minimal) — Bash-only file work, one self-contained delegation brief per `miniouto chat --style {style} --role <tag>` run (the tokens expand per run), verify-don't-fabricate, ask-before-destructive |
| `default_style/coding-pro.md` | Senior staff engineer orchestrator (~32 KB) — AGENTS.md startup read, delegate-vs-DIY decision framework, 5-stage loop, 6-section delegation brief, parallel tool-call batching mechanics, hard blocks (no sudo / no mass delete / no unprompted commit-push). Renamed from `pro.md` at 0.8.1 (stale seeds + `settings.style` migrated by `ensure_dirs`). Named role roster declared as top-level tags, selected by `miniouto chat --role <tag>` (the pinned roster: file-picker / thinker / editor / reviewer / validator) |
| `default_style/coding-work.md` | Pragmatic senior engineer orchestrator (~38 KB) — full autonomy protocol (never ask mid-loop, soft blocks decided + logged, three-strike rule, four true hard blocks), scale-based distribution: read-and-do work goes direct with batched reads (no subagent ceremony on small work), large-scale understanding goes to parallel context roles, multi-site changes go to one editor per independent slice as parallel `miniouto chat --role` runs; five-phase workflow (EXPLORE/PLAN/EXECUTE/REVIEW/VERIFY). Named role roster declared as top-level tags (the pinned roster: file-picker / code-searcher / directory-lister / researcher / thinker / editor / reviewer / validator — 8 roles) |
| `default_style/coding-ultra.md` | MAX-mode execution orchestrator (~81 KB) — zero-excuse protocol, layered role fan-out (3+ file-pickers, best-of-N editors, multi-focus reviewers), canonical layer sequence 0–9, `.miniouto/docs/` documentation system, worktree-by-default, boulder state. Renamed from `ultra.md` at 0.8.1. Named role roster declared as top-level tags (the pinned roster: 17 roles spanning context gathering, editing, multi-focus review, validation, verification, and doc sync) |
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
3. `core/runtime.py`: add name to `BASE_TOOLS` (visible to every run) or `PLATFORM_GATED_TOOLS` (platform-gated: appended only when the platform registered it, in every mode).
4. `core/chat.py`: add the name to `_LOGGABLE_TOOL_NAMES`, the tool-name set in `_make_tool_call_dispatcher`, and a branch in `_short_arg_summary` so loop events + failure diagnostics render the tool.
5. Update `default_style/*.md` if the tool's name or behavior should be documented to the model.
6. If the tool returns multimodal content, note that provider support varies (OpenAI Chat Completions rejects all multimodal blocks; OpenAI Responses API rejects video/audio). **Do not** put provider names in the description or style prompts — the agent cannot introspect its provider, so such hints are unactionable. Let provider rejections surface as `ValueError` at call time. Document the matrix in `docs/tools.md` for human operators.

### "Add a new bundled style"
1. Create `src/miniouto/default_style/<name>.md` (use the tag-as-name structure: a single top-level `<outo>...</outo>` block plus zero or more `<name>...</name>` blocks — one per named role).
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
`src/miniouto/core/runtime.py:BASE_TOOLS` is the tool set every run gets, outo and role mode alike (`Bash`, `Image`, `Video`, `Audio`, `continue_session`); `PLATFORM_GATED_TOOLS` (currently just `Computer`) means "tools appended when the platform registered them", and the preset filters it by what actually registered. The tool list is identical in both modes because a delegated run is a separate process owning its own vwayland screens. Delegation is not a tool at all: `miniouto chat "<brief>" --style <style> --role <tag>` rides on `Bash` (invariant 5), so a Bash-less harness could not delegate. (Do **not** confuse this with `parse_style`, which only resolves the style *prompts* — i.e. which `--role` tags exist — not the tool lists.)

### "Add tests"
The suite is ~300 tests under `tests/` and runs with `uv run pytest` (see "Development environment"). `tests/test_no_call_subagent.py` guards the Bash-only delegation surface; `tests/test_chat_wakeup.py` (the WAKEUP behavior spec), `test_chat_delegation.py`, `test_inbox.py`, `test_spawn_state.py`, `test_role_runtime.py`, `test_ascii_art.py`, `test_computer_art.py`, `test_tui_navigation.py` cover the refactor. Suggested setup in `docs/development.md`.

---

## Things that look like bugs but aren't (and things that ARE bugs)

### NOT bugs (intentional)

- **`tui/` and `utils/` are empty** — placeholder directories from an older layout. The TUI code is in `cli/tui.py`. Don't add code to those empty dirs without first deciding the right home for it.
- **`core/context.py` is not in `core/__init__.py`'s `__all__`** — it's an implementation detail of `runtime.build_runtime`.
- **`storage/skills.py` and `storage/session_config.py` are not in `storage/__init__.py`'s `__all__`** — they're imported directly as `skill_store` / `session_config`.
- **`continue_loop` is referenced in every bundled style** but **not registered** in `tools/registry.py`. Models improvise. If you want this to actually work, register a no-op tool and add it to `BASE_TOOLS`.
- **`SubagentRow` / `SubagentDetailScreen` are gone from `cli/tui.py`, intentionally.** They drilled into an in-process background job's event stream. Delegation is a separate process now (invariant 5), so there is no per-actor row stream to open; the result file and the inbox message are the whole surface. If a user-facing doc still mentions a subagent drill-in, it is stale.
- **The inbox has one consumer per session, by design** (invariant 20). Two live processes bound to one session steal each other's messages; that's not a bug to "fix" with a lock — isolate with `MINIOUTO_HOME`.
- **`screen` bypassing the screenshot-degradation gate is deliberate** (invariant 15). It returns a `str`, so it works on openai Chat Completions where `screenshot` returns a notice.
- **`LoopEvent.subagent_id` / `subagent_name` are now bookkeeping for a delegated run, not actor identity** (invariant 6). They are `None` for `kind="user"` messages.

### Bugs / cleanup opportunities

1. **`tools/bash.py` is the highest-value remaining test target** — everything else the refactor touched is covered (see "Add tests").
2. **`tools/registry.py:_register_if_missing` accepts but silently discards the `schema` parameter** — the `_xxx_schema()` dicts are computed at registration time but never passed to `coreouto.register_tool`. Only the handler's Python type hints and the `description` string reach the model. The schema dicts are effectively dead code.

---

## Dependency reference

| Package | Min version | Role |
|---|---|---|
| `coreouto[all]` | 0.11.3 | Agent loop, providers, tool registry, hooks (the runtime). 0.11.2: openai provider uses low-level `create(stream=True)` instead of the SDK's `.stream()` events helper (which hard-rejects non-`strict` tools and broke every openai-format provider on 0.11.1). 0.11.3: the schema generator emits `type` for str-based Literals — 0.11.2 and earlier produced type-less enums that strict validators (moonshot flavored JSON schema) rejected with a 400 (their `__version__` constants are stale through 0.11.1 — trust the dist-info). 0.10.0+ required for provider-level `error_handling` (see `core/error_rules.py`); 0.10.0 removed `retry_intervals`/`ON_RETRY` (never used here). Provider-level `stream=True` (see `core/providers.py:_instantiate`) requires 0.9.0+. 0.11.0+ required for the provider constructor `timeout` param, `ErrorRule.exc_type` matching + the `TIMEOUT_ERRORS` preset, and the contrib tracker hooks (`activity_tracker_hook`/`loop_progress_hook`) the stall watchdog is built on (`examples/27_wakeup.py`). 0.11.1+ required for optional `user_message` — `call(history=...)` alone continues a transcript (`examples/28_resume_interrupted.py`), which is how the watchdog restarts without injecting an artificial user message. |
| `typer` | 0.12.0 | CLI framework |
| `rich` | 13.7.0 | Terminal output, tables, markdown |
| `textual` | 0.80.0 | TUI framework |
| `pydantic` | 2.0 | Data models (used by coreouto) |
| `httpx` | 0.27.0 | HTTP client (`lma` REST client, style repo fetcher) |
| `tomli-w` | 1.0.0 | TOML serializer (paired with stdlib `tomllib`) |
| `py-Vwayland` | 0.1.0 | Virtual Wayland compositor behind the `Computer` tool; wheel installs anywhere, bundled binary runs on Linux x86_64 glibc ≥ 2.28 only (registration-time probe gates the tool) |
| `textual-image` | 0.12.0 | TUI inline image rendering (Kitty TGP / Sixel) for Computer screenshots; pulls in Pillow. Probe-gated at render time — a non-graphics terminal falls back to the `.art` text lane unchanged. |

The `[all]` extra on `coreouto` pulls in all four provider SDKs. Anything beyond these nine packages should be discussed before adding.

---

## When in doubt

1. **Search the docs first.** `docs/README.md` is the index. Use `grep` on `docs/*.md` for keywords.
2. **Read the relevant module's `__init__.py`** — it usually lists what the package exports.
3. **Look at the bundled styles** for examples of how the model sees the system.
4. **Check `docs/development.md` "Known sharp edges"** before doing anything risky.
5. **If you're about to delete code, run `git blame` first** — the comments often explain *why* it exists.
