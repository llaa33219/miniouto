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
   ├─ chat <prompt> [options]    → one-shot chat turn (also the delegation door)
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
   │    ├─ list
   │    └─ show <name>
└─ subagent
       ├─ show
       ├─ set [--provider NAME] [--model NAME] [--reasoning TEXT]
       └─ clear
   ├─ message
   │    ├─ insert [TEXT ...] --session NAME [--kind alarm|user] [--source STR] [--stdin]
   │    ├─ list [--session NAME]
   │    └─ clear [--session NAME] [--yes]
   └─ computer
        └─ show [PATH] [--plain] [--cols N] [--head N]
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
| `ThinkingRow` | reasoning/thinking — **collapsed by default** (`▶ thinking`), a plain click anywhere on the row or `Enter` to expand the full text (`▼ thinking` + content), clicking again to re-collapse. A drag that selects text never toggles it, and a double-click selects no whole row. While reasoning is still streaming, the header carries a `●` marker; the completed message's whole text then replaces the streamed fragments in place. Same translucent border + muted styling as `EventRow` |
| `StreamRow` | the **live** assistant text while the model is still generating it: mounted on the first text delta of a message and updated in place for each subsequent one (one widget per message, not per token). Replaced by the canonical `AnswerRow` when the turn's final answer arrives, or kept as the record of an intermediate message whose whole text is otherwise suppressed |
| `EventRow` | any other intermediate loop output — tool calls, iteration/token progress, provider errors — rendered with a **translucent left border (`$primary 40%`) and muted gray text** (provider errors use `$error`). No `actor:` prefixes |

There is **no** `SubagentRow` / `SubagentDetailScreen`: a spawned run is a separate process with its own event stream, so there is no per-actor row stream to drill into. What a spawned run contributes to this transcript is its **completion alarm** (a user-style row — see "Messages" below).

Tool rows start as a single `▶ <summary>` line whose toggle glyph highlights on hover, expanded on a plain click anywhere on the row (glyph, header text, expanded body, attached result) or `Enter` to reveal the full command and (once finished) its attached result. Click vs. text-selection is decided by the gesture, not by position: a drag that takes at least one character is a selection and leaves the row alone, while a gesture that takes none is a plain click and toggles. Double-clicking no longer selects the whole row (the second click of a double-click is consumed, so the row opens at most once).

### Live token streaming

While the provider is streaming, the TUI renders text and reasoning **as it arrives** (coreouto's `ON_STREAM_TEXT` / `ON_STREAM_THINKING` fire once per token). Per-token hooks fire 100+/s, so `TUIEventSink` folds them through `_StreamCoalescer` into a single UI-thread update roughly every **33 ms (~30 Hz)** — fast enough to read as typing, cheap enough that a long answer repaints a few dozen times instead of hundreds. The final `< interval` window is drained when the turn's spinner stops, so no tail fragment is lost — and every message-boundary event (`thinking` / `response` / `tool` / `context`) flushes the buffer first, so a tail never rides the next message's live row.

Deltas are **display-only**: they are never written to the session file (`_RecordingSink` forwards them to the wrapped sink without recording), so a reloaded session is byte-identical to a pre-streaming one. Nor are they fed to the repetition watchdog — that stays on whole-message boundaries to avoid counting each phrase twice.

Finalization avoids showing the same text twice: the live `StreamRow` is removed and the Markdown `AnswerRow` mounted in its place for the final answer, the whole-text `thinking` event finalizes the live `ThinkingRow` in place, and the stashed `response` row is suppressed whenever deltas already showed that message (a non-streaming turn renders exactly as before).

### Messages (the inbox), width cap, and scrolling

- **Mid-turn insertion.** Typing while a turn is running does not poke the live message list: it goes to `inbox.insert(session, text, kind="user", source="tui")`. One code path then serves human interjections, spawn alarms, and `chat --alarms` wakeups. The row renders optimistically, and when the drain later produces the matching loop event `ChatTUI._claim_inbox_event` matches `detail="inbox:<id>"` and drops the duplicate.
- **Idle delivery.** No registry callback pushes messages any more (a detached process may write at any moment), so the TUI polls the inbox every `_INBOX_POLL_INTERVAL` (1 s) and at the tail of `_dispatch`: anything drained starts a turn whose prompt **is** the joined message text, recorded as that turn's `user` so display, replay, and model perception are the same string by construction.
- **Single consumer.** One session belongs to one live process; a second process bound to the same session would steal each other's messages (see `docs/core.md` § `core/inbox.py`).
- **Summary hand-off.** If a turn ends because the model called `continue_session`, `ChatTUI._apply_handoff` mounts the `------summary → <new>------` + `summary: <dir>` marker as a system row in the **old** conversation, persists it there for replay (`_persist_handoff_marker`), re-pins the new `cont-*` session, and lets the 1 s inbox poll auto-deliver the continuation message — so the transition needs no user action. The old conversation keeps its history: a hand-off is a session change, never a history edit.
- **Width cap.** The log column is centered and capped at `CHAT_MAX_WIDTH = 150` cells (theme variable `$chat-max-width`), so wide terminals do not stretch the transcript. The header chips sit in two left/right-anchored pairs inside that column (`outo` / `style`, then `subagent` / `session`) and render their values whole — two chips a row is what keeps every value short enough not to truncate, so every picker stays clickable at any terminal width.
- **Scrolling.** `PgUp` / `PgDn` / `Home` / `End` are priority bindings overridden with `animate=False`, so they act **instantly** even though the input is a `TextArea` that otherwise owns them for caret movement. A jump-to-bottom button sits under the log.

### Clickable chips

Each `StatusChip` is a focusable widget. Click it (or focus it with `Tab` and press `Enter`) to open a modal:

| Chip | Modal | Notes |
|---|---|---|
| `outo` | `ProviderSettingsPopup` — three rows, `Provider` / `Model` / `Reasoning`, each opening its own picker | `TrioTarget` = `OUTO_TARGET` (`provider`/`model`/`reasoning`) |
| `subagent` | the **same** `ProviderSettingsPopup`, with `SUBAGENT_TARGET` (`subagent_provider`/`subagent_model`/`subagent_reasoning`) | empty still reads `same as outo`, and an empty subagent key means "inherit" |
| `style` | `SelectionModal` — list of installed styles | writes `settings.style` |
| `session` | `SelectionModal` with `sections=` — grouped by the session's creation directory, plus `+ new session…` | writes the session **pin** (`self._pinned_session`), which is what `_dispatch` and the inbox poll route on |

**Every provider/model/reasoning edit goes through `_persist_trio`, which writes BOTH halves** (`docs/storage.md` § `session-config/`): the session pin at `session-config/<pinned session>.toml`, so the edit survives a later default change, and the global default, so other processes and the next session follow the most recently changed choice. For the **outo** trio the global half is `settings.provider` + the provider record's `default_model` / `reasoning_effort` (`dataclasses.replace` + `provider_store.upsert`); `settings.model` is never written. For the **subagent** trio it is the three `settings.subagent_*` keys. **Provider and Model both reset Reasoning** — an effort level belongs to a model, so either pick lands on that model's default (the provider record's own preference, else lma's auto default); Reasoning is one more click away when that is not what you want. An empty value clears the pin key and hands it back to the global default.

There is **no standalone provider or model editor any more** — the popup's rows replaced both, and so did the one that edited an existing provider's `max_context_window` / `max_output_tokens`. Those caps are set when the provider is added (the `+ add custom…` wizard asks for them) or by hand in `providers.toml`.

There is **no** session chip — the session label in row 3 is a plain `Static`. To change sessions, use the command palette (`Ctrl+P`) → "Pick session" / "New session".

The "Pick session" modal is the one place that uses `SelectionModal`'s opt-in `sections=` argument, and it groups by the session's creation directory: a **"This directory"** section first, then **"Other directories"** with each entry prefixed by its basename (`[myproject] old-thing`, or `[?] legacy` for files written before the `cwd` field existed), then the `+ new session…` row. Section headers are non-selectable display rows. Every other picker passes no `sections=` and renders exactly as before.

The popup's `Provider` row accepts two sentinels (rendered as `extra_options` rows at the bottom of the list):

- `+ add from catalog…` — runs `_catalog_add_flow`: fetches `https://lma.blp.sh/provider`, filters to entries whose `sdk` maps to a supported coreouto `api_format`, lets you pick one, prompts for the API key, then saves the provider with `source="lma"` and the first model returned by lma as `default_model`. See `core/lma.py` and `core/providers.py:sdk_to_format`.
- `+ add custom…` — runs `_open_custom_add_wizard`: a five-step wizard (name → api_format → base_url → api_key → default_model), saving with `source="custom"`.

Modal results are persisted through `_persist_trio` (provider/model/reasoning — see above) or `storage.settings.update(...)` (style), and the chip row re-renders. The active Textual theme is persisted to `settings.theme` and restored on launch.

### Theme

`ChatTUI` registers the harness's own dark theme, `MINIOUTO_THEME` (name `miniouto`, `cli/tui.py`), and boots on it. The palette is comfort-tuned for long sessions: the floor is lifted off near-black (background `#0a0e14`, ~2× the old `#05070a`'s relative luminance, and still darker than Textual's stock dark `#121212`), the surfaces step `#0f141b` → `#151b24` → `#1d2530`, and the ceiling comes down from the old 13.28:1 glare — but not all the way to one undifferentiated gray, because the agent's **final answer must still be the brightest thing on screen**. The text therefore forms an explicit **three-layer hierarchy** (plus the disabled ramp):

| Layer | Variable | Hex | On `$background` | Painted on |
| --- | --- | --- | --- | --- |
| answer | `$text-answer` | `#c5d0d8` | **12.33:1** | the final-answer row only (`AnswerRow`) |
| body | `$foreground` | `#a9bac5` | **9.69:1** | everything else in the transcript |
| muted | `$text-muted` | `#7b8490` | **5.11:1** | event, tool and thinking rows |
| disabled | `$text-disabled` | `#434a52` | 2.16:1 | disabled controls (WCAG-exempt) |

The answer layer is the only near-white in the palette, so the hierarchy reads at a glance: answer > body > muted. `$foreground` also got its chroma back — `#a9bac5` is 3.4× the saturation of the `#a8b0b1` it replaces (S≈0.19 vs S≈0.06), which is what "too gray" was; it carries the theme's blue family without going clinical. Muted and disabled are unchanged. The four semantic colors are one notch down (secondary `#8f86d8`, success `#52bd8a`, warning `#c99a4e`, error `#d16f7e`) and each clears 4.5:1 on all four surfaces, and the owner-pinned `#2fc2ff` accent — carried by **both** `$primary` and `$accent`, so the logo, the focus rules, block cursors and the tool-row toggle glyph are all the same cyan — is left exactly as it was; it reads calmer for free (9.87:1 → 9.47:1) because the background underneath it is lighter.

Four fields travel in the theme's `variables` dict rather than as `Theme` fields, because Textual's `ColorSystem` does not derive them here: `boost` (Textual **drops** the `boost` field whenever `panel` is set explicitly, which would leave `$boost` transparent and take the chips, selection lists and `TextArea`/`Markdown` backgrounds with it) and the `text-answer` / `text-muted` / `text-disabled` ramp. `get_theme_variable_defaults()` still supplies the app's own `$chat-max-width`. `AnswerRow` applies `$text-answer` through a single `color` rule: its content is a **Rich** `Markdown` renderable — one `RichVisual` with no DOM children — so a descendant selector would never match. `$text` is deliberately left at Textual's `auto 87%` default: it does not paint the transcript (the declared `foreground` does), and it is what resolves to near-black ink on the picker's selected row, which sits on a solid `#2fc2ff` bar — pinning `$text` to the solid foreground was measured and drops that row to 1.08:1.

`settings.theme` overrides the default at launch (`ChatTUI._boot_theme`); a name no build registers falls back to `miniouto` rather than failing the launch, since `App.theme`'s validator rejects unknown names outright. `Ctrl+P` → Theme changes it at runtime and `watch_theme` writes the choice back.

### Model resolution order

`resolve_runtime_from_settings` (core/runtime.py) resolves the active provider/model/reasoning in this order, per key:
1. the explicit override (`ChatOptions`, i.e. `chat --pm`)
2. **this session's pin**, `session-config/<session>.toml` — frozen at the session's first resolution, so a model switch in another process can never redirect a live conversation
3. the global default: `Settings.provider` / `Settings.subagent_*` plus the resolved provider record's `default_model` / `reasoning_effort` (a legacy `Settings.model` still reads at the bottom of that chain, invisible in the TUI)

In the TUI, every row shows the **configured** value when there is one, else what it resolves to (the provider record's own default), else `-`.

Keybindings:
- App-level (registered on `ChatTUI`): `Ctrl+L` — clear log; `Ctrl+C` — quit; `Esc Esc` (two presses within 2.0 s while the agent is busy) — force-stop the running turn. The stop is cooperative: a `threading.Event` is passed via `ChatOptions.cancel_event`, and the loop's cancel-guard hook raises `LoopCancelledError` at the next hook boundary (before the next LLM call / tool execution) — additionally, the stall watchdog's 5 s poll watches the same event and hard-cancels the turn task, so an in-flight LLM call IS interrupted promptly, and an in-flight **Bash** process IS killed (`tools/bash.py` polls the same event — Bash's 1-hour hard cap, `BASH_TIMEOUT_SECONDS`, is far too slow to serve as the stop mechanism). The TUI shows `[stopped by user (esc esc)]`; a single `Esc` while busy shows a persistent "press esc again to stop" hint in the spinner line (it survives spinner ticks for the full 2.0 s window; after a successful double-press, "stopping after the current step…" persists until the turn ends).
- `Ctrl+P` — open the Textual system command palette (customized via `get_system_commands`) — new session, pick session, change model/provider/style/theme, clear log. The splash text on boot explicitly says "Press Ctrl+P for commands."
- Widget-level (inside modals / chips): `Tab` / `Shift+Tab` — cycle focus; `Enter` — confirm; `Esc` — cancel.

Submission flow: each submitted prompt is dispatched via `self.run_worker(..., exclusive=True)` to `_dispatch(prompt)`, which calls `core.chat.run_chat(opts, sink)` inside `asyncio.to_thread` so the Textual event loop stays responsive. The busy flag (`self._busy`) prevents re-entrancy. Output is mounted as row widgets (see "Chat log rows" above); errors/system messages render as `[…]` (e.g. `[error: {exc}]`) in theme warning.

`cli/tui.py:tui_summary() -> dict` is a programmatic snapshot helper (currently unused by the runtime) that returns `{provider, model, style, session, styles_available}`.

---

## `miniouto --version`

Prints `miniouto <version>` (from `miniouto.__version__`, currently `"0.10.2"`) and raises `typer.Exit()` (exit code 0).

---

## `miniouto status`

File: `cli/__init__.py` (lines ~45–66).

Reads:
- `storage.settings.load()` → active `provider`, `style`, `session`; `model` is derived from the active provider's `default_model` (or `'- (use chat --pm)'` when empty)
- `storage.paths.ROOT` → storage path
- `storage.providers.load_all()` → all configured provider names
- `storage.styles.list_styles()` → installed style names
- `storage.skills.list_skills()` → all visible skill names (hidden skills excluded)
- `storage.sessions.list_sessions_with_meta()` → `(name, cwd, mtime)` per session file, read by parsing the JSON directly (never through `load()`, which would write)

Prints 11 rich-formatted key/value lines: `Default provider`, `Default model`, `Subagent`, `Active style`, `Session`, `Storage`, `Providers`, `Styles`, `Skills`, and the sessions split by directory. Always exits 0 (no error states). The last entry is two lines:

```
Sessions (this directory): default, tui-20260723-103412-a1b2c3
Sessions (other):       old-thing
```

A session's `cwd` is stamped from the launch directory on its **first** turn only, so the grouping means "the directory this session was created in", not "wherever it was last used". The `Subagent` line shows the resolved subagent `provider/model` when `settings.subagent_provider` or `settings.subagent_model` is set, or `same as outo (<provider>/<model>)` when it inherits. This trio is the **global default** a delegated run inherits — every `chat --role <tag>` run that carries no `--pm` and asks for the subagent profile (`--use-subagent-provider`) uses it.

---

## `miniouto chat <prompt>`

File: `cli/chat.py`. **This is the only command that runs a turn — and the delegation door.** A delegated run *is* a chat run: same tools, same `run_turn_chain`, same watchdog. There is no second runtime path and no `call_subagent` tool. It differs from outo in exactly four ways, each one a flag:

- `--style NAME --role TAG` → the system prompt is that role's body. `--role` **requires** `--style`
- `--pm 'PROVIDER/MODEL[:REASONING]'` → the provider/model/reasoning this run uses. Bundled styles teach it with the `{subagent_pm}` token, which renders the effective subagent trio in `--pm` form (`--use-subagent-provider` resolves that same trio at call time instead)
- `--alarm-session NAME` → one completion alarm lands in that session's inbox. The value is **required**
- `--return [PATH]` → the final answer is written as markdown

Every one of these values except `--return`'s must be spelled out: the command line is run by `Bash`, and one that reads `settings.style` or `settings.session` behind the caller's back is not the same run twice. `--role` without `--style` is rejected ahead of every resolution — before any session file is written and before any model call:

```
✗ --role requires --style (the default style may differ per session; make the run self-contained).
```

`--style` on its own is fine (it just runs the `<outo>` body of the named style), and an unknown `--role` still fails before the turn with the list of available tags.

In a continued session, delegation commands quoted in earlier turns may name a stale `--style` / `--role` / `--alarm-session`. Follow the current system prompt instead — it is re-assembled per turn, so its values are always current.

The model reaches all of it through `Bash` (see [`docs/tools.md`](./tools.md)). Signature:

```
chat_cmd(
    prompt: str,                       # required positional
    --name        TEXT,                # session name. Without --name and --continue, a fresh session is generated each call.
    --pm     'PROV/MODEL[:REASONING]', # the ONE per-call provider/model/reasoning override
    --style       TEXT,                # override active style (REQUIRED with --role)
    --role        TAG,                 # run as this style role's system prompt; this is what makes the turn a delegated one
    --use-subagent-provider,          # resolve provider/model/reasoning from the subagent trio at call time
                                    # (bundled styles instead pass --pm 'PROV/MODEL[:REASONING]' literally)
    --alarm-session NAME,              # one completion alarm into that session's inbox; the value is required
    --return     [PATH],               # write the final answer as markdown; bare = subagent-results/
    --bg,                               # detach into its own process and return immediately
    --max-tokens  INT,                 # cap output tokens
    --temperature FLOAT,               # sampling temperature
    --alarms,                          # wake a session from its queued messages instead of from a prompt (REQUIRES -c, no prompt)
    --continue, -c                     # prepend previous session history
    --answer-only, -a                  # print only the final answer (suppresses session marker, loop events, finish marker)
    --with-session                     # print only session marker + final answer (suppresses loop events + finish marker)
)
```

`--return` is the **one** flag with an optional value: bare `--return` is a real spelling, not a usage error. `cli/__init__.py:_App.__call__` rewrites it to click's `--flag=` spelling before parsing; `_OPTIONAL_VALUE_FLAGS = {"--return"}`.

`--alarm-session` has **no bare form** — it once meant "my session" via `settings.session`, but that ambient state made one Bash command mean two different things depending on who typed it. Its value is now required (`_resolve_alarm_session`), which is what makes a delegated command line self-contained. Bundled styles teach the recipe with the `{this_session}` and `{subagent_pm}` tokens, which expand to a literal session name and a literal `--pm 'PROVIDER/MODEL[:REASONING]'` per run (see `docs/styles.md` § Dynamic tokens).

### Flag reference

| Flag | Effect |
|---|---|
| `--name` | Session name. Without `--name` and `--continue`, a fresh session is generated each call |
| `--pm 'PROVIDER/MODEL[:REASONING]'` | The **only** per-call provider/model/reasoning override, for this call and nothing else. `PROVIDER` is everything before the **first** `/` (a model name may contain `/`); the optional `:REASONING` suffix is split off the **last** `:`, so an effort level belongs to a model name that itself has slashes. Omit it (or end with `:`) to leave reasoning to the session pin / default. A missing `/`, or an empty provider or model, is rejected before any resolution: `✗ --pm 'x' is not a provider/model pair.` + exit 1. Precedence: `--pm` > this session's pin (`session-config/<session>.toml`) > the global default. With `--bg` the child is re-emitted with the **resolved** trio as an explicit `--pm`, so it cannot re-resolve against a different pin |
| `--style` | Override the active style for this call. **Required with `--role`** — the default style can differ per session, so a delegated run must name its own style |
| `--role TAG` | Run as that style role: the `<TAG>...</TAG>` body becomes the system prompt. Requires `--style`. Validated **before** the turn — an unknown tag fails with the list of available roles (or `(none)`), costing no model call |
| `--use-subagent-provider` | Resolve provider/model/reasoning from the **subagent trio** instead of outo's, pin-first: this session's `session-config/<session>.toml` subagent keys, else the `settings.subagent_*` global default. It resolves against **this run's own** session, never the caller's. An explicit `--pm` still wins. **Prefer `--pm` in generated commands**: bundled styles emit `--pm {subagent_pm}`, a literal provider/model/reasoning, so a copied command is idempotent; this flag is the call-time-resolving alternative for a command you type by hand |
| `--alarm-session NAME` | Insert **one** completion alarm into that session's inbox on completion. The value is **required**; there is no bare form (it used to resolve to `settings.session`, which is ambient state) |
| `--return [PATH]` | Write the final answer as markdown to this path. A directory (existing, or with a trailing `/`) receives the generated `<id>-<YYYY-MM-DD-HH-MM>.md` name; bare = `subagent-results/` |
| `--bg` | Detach the run into its own process and return immediately (see below) |
| `--max-tokens` | Cap output tokens |
| `--temperature` | Sampling temperature |
| `--alarms` | Wake a session from its queued messages: the inbox is drained and the joined texts become the turn's user message. Takes **no prompt**, **requires** `--continue` (`-c`), and requires the named session to exist. An empty inbox is a no-op. See [§ `--alarms`: waking a session](#--alarms-waking-a-session) |
| `--continue` / `-c` | Prepend the session's previous history. Never implied — a plain `chat` starts fresh, and a wakeup must spell it out |
| `--answer-only` / `-a` | Print only the final answer. Suppresses the `------{session}------` marker, loop events, and `------finish------` marker |
| `--with-session` | Print only the `------{session}------` marker + final answer. Suppresses loop events and `------finish------` marker |

`--answer-only` and `--with-session` are **mutually exclusive** (exit 1 with `✗ --answer-only and --with-session are mutually exclusive.`). Both suppress the spinner, loop events, and finish marker by putting the `ConsoleEventSink` into `quiet` mode; they differ only in whether the session marker line is emitted up front. The default (neither flag) keeps the full verbose output: session marker + spinner + loop events + finish marker + answer.

### Output shape

**A run never waits.** `miniouto chat "prompt"` runs its turn, prints its answer behind exactly one `------finish------`, and exits — however much is still queued in the inbox, and however many detached runs it started. `------finish------` belongs to the **run**, not to a turn, and it is the run's last marker: only the answer follows it, then the process is gone. A queued message is a wakeup for the next turn, not this run's business (see [`--alarms`](#--alarms-waking-a-session)); to process it, wake the session.

Exactly one finish marker is printed per invocation:

```
------{session}------                 # run start
…loop events / tool output…
{intermediate turn answer}            # plain text, NO marker — a summarization hand-off continues the run
------summary → cont-{id}------       # mid-run hand-off, if one happened
summary: {summary_dir}
------cont-{id}------
…loop events / turn output…
----- chat<{id}> complete ----- result: {path}    # delegated runs only, from the post-step
------finish------                    # EXACTLY ONCE — the run's final marker
{final answer}                         # the last turn's answer
<process exits immediately>
```

There is **no `------follow-up N------` banner**: a chat does not chain turns of its own accord. The one loop that survives is the summarization hand-off (bounded by `HANDOFF_MAX_TRANSITIONS = 5`), whose intermediate answers print as plain text because the run has not finished yet.

A single-turn run collapses to `------{session}------` / `------finish------` / answer, byte-for-byte as before. The post-step that writes the result file, delivers the alarm and stamps the marker runs **before** the completion line, so once `------finish------` is printed only the answer text and process teardown remain — nothing lingers, and no second marker can follow. `--answer-only` / `--with-session` keep their suppression (answers only, no markers) under the same rule.

### `--alarms`: waking a session

```
miniouto chat --alarms -c [--name <session>]
```

A wakeup takes no prompt: the queued messages *are* the prompt. It is the explicit inverse of a chat that returns and dies, and it is how a caller — a script, a cron tick, another agent, the TUI's own 1 s poll — drains a session's inbox.

| Rule | Behavior |
|---|---|
| No prompt argument | `chat "…" --alarms` is rejected. Two sources for one user message is a mistake, not a merge |
| `-c` / `--continue` is **mandatory** | History is never loaded implicitly. Asking for it is the operator's decision |
| The session must **exist** | `nothing to wake: session '<s>' does not exist`. With no `--name` and no `settings.session`: `nothing to wake: no session to continue — pass --name <session>` |
| All of the above are checked **before anything is written** | A rejected wakeup costs no model call, writes no session file, and leaves the queue untouched |
| The drained texts, blank-line joined, become the turn's user message | So history rides in only because `-c` was passed |
| An **empty inbox is a no-op, not a turn** | `no pending messages in session '<s>'.` on stdout, exit 0, no session marker, no finish marker, no model call |

`--pm`, `--style`, `--role`, `--use-subagent-provider`, `--answer-only` are orthogonal to `--alarms`: a wakeup can run on a different provider, as a role, or print only its answer.

Example:

```bash
# Something happens, from anywhere
miniouto message insert --session oncall "deploy 4f2a90 finished"

# A separate process (or a cron tick) drains the queue and exits
$ miniouto chat --alarms -c --name oncall
------oncall------
outo: iter 1
------finish------
The deploy log shows no errors; nothing to roll back.

# Asking again with nothing queued is free
$ miniouto chat --alarms -c --name oncall
no pending messages in session 'oncall'.
$ echo $?
0
```

The monitor-and-wake guide (insert / observe / wake, the polling loops, and liveness markers) is [`docs/automation.md`](./automation.md) § 3.

### Behavior

Validation happens **first, ahead of every resolution**: `--answer-only` + `--with-session` is an error, `--bg` + `--bg-child` is an error, and `--role` without `--style` is an error — all exit 1, so a rejected command leaves no session file and costs no model call. Then:

1. Resolves `session_name` via a 3-way branch (`_resolve_session`):
   - If `--continue`: `name or settings.session`.
   - elif `--name` was supplied: use `name` verbatim.
   - elif `--bg`: a fresh `spawn-{YYYYMMDD-HHMMSS}-{6hex}` — a detached run must never share a session writer, since whole-file saves from two processes lose turns.
   - else: generate a fresh `chat-{YYYYMMDD-HHMMSS}-{6hex}` name.
2. Calls `settings.update(session=session_name)` — **only for a plain run** (`_is_plain_run`: not `--bg`, not `--bg-child`, no `--role`, no `--alarm-session`, no `--return`). A delegated run is a guest: it mints its own `chat-`/`spawn-` name and writes its transcript there, but never repoints the active session (otherwise the caller's conversation — and the TUI, which reads the active session — would jump onto the subagent's file). Plain `miniouto chat` still persists the active session, with or without `--name`. A delegated run instead **borrows** its own name as `runtime.session` for the duration of its turn (`_runtime_session_scope`, restored in `finally`), so it drains its own inbox and expands `{this_session}` to its own name — never the caller's.
3. Emits the `------{session_name}------` marker to stdout unless `--answer-only` was passed, then builds a `core.chat.ChatOptions` dataclass from the flags and dispatches to `core.chat.run_chat(opts, sink=ConsoleEventSink(quiet=answer_only or with_session))`. **Session persistence is unconditional** — `run_chat` builds its `_TurnPersister` before any branch on delegation, so every run (plain outo, a delegated `--role` run, and `--answer-only`/`-a`) writes its session file; `-a` suppresses stdout only, and the file on disk is identical either way.
4. On exception: `run_chat` itself calls `_dump_failure_diagnostics` (prints `✗ {ExceptionType}: {msg}` + the last ≤5 tool calls + a full traceback to **stderr**) and **re-raises**. Typer prints the traceback and exits 1. A *delegated* run catches it instead — see the post-step below.
5. On success: `ConsoleEventSink` writes the reply as plain stdout; in verbose mode it is preceded by a `------finish------` marker and loop events (tool calls, intermediate responses) rendered in `orange3`. In quiet mode only the raw answer (and the session marker, for `--with-session`) reaches stdout.


Loop-event notation in verbose CLI output keeps the `name:` prefix style:

```
outo: Bash ls -la                          # outo tool call
outo:thinking: <full reasoning text>       # outo reasoning (dim, untruncated)
outo: [SYSTEM ALARM - THIS IS NOT A USER MESSAGE]   # a delegated run finished; the whole alarm
…                                              # is printed, including the result-file path to `cat`
watchdog: no activity for 1800s (phase='llm_call') — restarting the turn (1/3)
provider: HTTP 429 → retry: …             # rule-matched provider error
```

There is exactly **one** actor label per process: `outo`. A delegated run is a **separate OS process** with its own registry, its own hooks, and its own `"outo"` — so its loop events go to its own transcript, never to this process's. What crosses the process boundary is the completion alarm, which arrives as one `kind="alarm"` (or `kind="user"` for a human interjection) loop event on **outo's** actor line, attributed to the run through `subagent_id` / `subagent_name` taken from the inbox message's `meta`, with `detail="inbox:<id>"` as the display de-duplication key. `ConsoleEventSink` has no special case for `alarm`, so the alarm text prints in full — including the exact result-file path the model was told to read. The `subagent_end` kind still exists in `LoopEvent` for old session files and is rendered dim with a 120-char preview, but nothing emits it anymore.

A message-removal prune (a 400/422 history repair cutting a poisoned tool call, or a watchdog restart truncating a dangling tail) prints a single dim note — `[removed N corrupted messages from the record]` — because stdout cannot be un-printed. It is suppressed under `--answer-only` / `--with-session`. Reasoning/thinking is labeled `:thinking:` and printed **in full** in the CLI.

A `chat` call **never waits**. `run_turn_chain` is one turn, plus any summarization hand-offs the turn requested (bounded by `HANDOFF_MAX_TRANSITIONS = 5`) — nothing else. When the turn ends the run prints its one `------finish------`, prints the answer, and exits, even if `spawn_state.pending_spawns(session)` is non-empty or more messages have already arrived: those stay in the inbox for the next wakeup (`--alarms -c`, or the TUI's poll). Waiting could only ever have been satisfied by the markers this one run happened to start, which is a closed-ecosystem assumption — any producer anywhere can `miniouto message insert` into any inbox.

### Summary hand-off (a session change inside the same run)

There is **no implicit summarization or compaction** — nothing rewrites the transcript behind the model's back. When a turn has consumed ~80% of its context window, the context monitor inserts **one** request into the session's inbox (`kind="alarm"`, `source="context"`), which starts with `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]` + `Context budget: {pct}% of this session's window is consumed ({used}/{window} tokens). Explicit summarization is required now.`. It arrives like any other inbox message (delivered on the next `BEFORE_LLM_CALL`, as a user message), and the model reacts by writing summary documents under `./.miniouto/summary/<session>/` (project-local, **not** under `~/.miniouto/`) and calling `continue_session(summary_dir=...)` — the only tool that changes a session.

The run handles the transition, so the **same invocation continues in the new session** (this is the one loop inside `run_turn_chain`):

```
------tui-20260723-103412-a1b2c3------
…
------summary → cont-20260723-105900-3f9ab2------
summary: /home/luke/repo/.miniouto/summary/tui-20260723-103412-a1b2c3
------cont-20260723-105900-3f9ab2------
```

`handoff_marker` is two lines — `------summary → <new>------` then `summary: <dir>` — followed by the ordinary `------<new>------`, so a session transition reads exactly like the `------finish------` / `------{session}------` idiom around it. It is the same string the TUI mounts as a system row.

What `_apply_handoff` moves, all at once: `opts.session`, the published run contract (re-registered under the new session), and `settings.session` (which is what `runtime.session` resolves to, i.e. what the `BEFORE_LLM_CALL` inbox hook drains). The continuation message is already waiting in the new session's inbox, so the next drain turns it into the next prompt — no user action, no new command. `HANDOFF_MAX_TRANSITIONS = 5` bounds it (`summary → cont-1 → cont-2` …); past that it warns, naming the session and the summary folder, and stops. The continuation prompt is the drained inbox of the new session — a one-shot drain, rebuilt from `core.summary.build_continuation_message` (plus the run contract) if the drain came back empty.

A delegated run's **delegation contract travels**: role, style, alarm session and result file are published before the turn (`_register_run_contract`) and re-stated in the continuation message, and the post-step fires from the **final** session — the alarm's `Session:` line, the result file path and a `--bg` marker's `session` field all name the `cont-*` session, with `Continued from: <old> (summary: <dir>)` added to the alarm and as a trailing `---` section of the result markdown (which still leads with the answer). The TUI does the same thing from outside the CLI: it mounts the marker in the old conversation, persists it for replay, re-pins the new session, and the 1 s inbox poll auto-delivers the continuation.

### Model resolution

The provider/model/reasoning a call runs on is the **first match in**, per key:

1. `--pm 'PROVIDER/MODEL[:REASONING]'` — this call only
2. this session's pin, `session-config/<session>.toml` (frozen at the session's first resolution)
3. the global default — `settings.provider` plus the resolved provider record's `default_model` / `reasoning_effort` (a legacy `settings.model` still reads at the bottom of that chain)
4. **error** — no provider configured / no model can be inferred

An override that names a **different** provider invalidates that session's pinned model and reasoning (they belong to the pinned provider), so the global chain answers for the override's provider instead. See `docs/core.md` § `resolve_runtime_from_settings` and `docs/storage.md` § `session-config/`.

### Subagent provider/model resolution

The provider/model a **delegated run** uses with `--use-subagent-provider` resolves through `runtime._resolve_subagent`, pin-first against **this run's own** session: provider: `--pm` > the session pin's `subagent_provider` > `settings.subagent_provider` > inherit outo's; model: `--pm` > the pin's `subagent_model` > `settings.subagent_model` > the subagent provider's `default_model` > inherit outo's (only reachable when no subagent provider is set). Reasoning: the pin's `subagent_reasoning` > `settings.subagent_reasoning` > the subagent provider's `reasoning_effort` > lma's default. Without the flag the trio is inert and the run uses outo's provider, model and reasoning. The `{subagent_pm}` style token renders this same resolution as a literal `--pm` value, which is what bundled styles emit. `miniouto subagent set/clear` manages the **global half** of that trio; a TUI edit to the `subagent` chip writes both halves (see `docs/usage.md`). See `docs/core.md` § `resolve_runtime_from_settings`.

### Delegation: the post-step

A run that promises something extra is one whose `_Run.delegated` is true — i.e. it re-entered as a `--bg-child`, or `--alarm-session`, or `--return` was given. `_run_delegated` wraps `run_turn_chain` in a try, and on the way out (`_complete`, whatever the outcome) writes the answer file, delivers the alarm, prints `----- chat<id> complete ----- result: <path>|(not saved)`, and stamps the liveness marker — in a `finally`, so a raising or interrupted turn still reports. Status is `done`, or `error` (`error: {Type}: {msg}`) / `stopped` (`stopped: cancelled`); a non-`done` status exits 1. The recorded answer is the **last** turn's — the post-step fires when the run ends, after any summarization hand-offs, not when the first turn does. A hand-off may have moved the run into a **different** session, so the post-step reports the session the run finally ended in (`ChainState.session`), not the one it started in.

The brief is the **whole input**: a delegated run starts with no history, so a brief that is not self-contained (goal, paths, constraints, done-criteria) produces garbage. **One brief per invocation; there is no fan-out flag.** Parallelism = several `chat --role` commands in one Bash call, or several Bash calls.

**Result file.** Default `subagent-results/<id>-<YYYY-MM-DD-HH-MM>.md`, written atomically (tmp + `os.replace`) on completion, error, or Ctrl-C alike. It holds **only the final answer** — no meta header, no brief section, nothing but what the run produced:

```markdown
<the run's final answer>
```

With no `--return` there is no file, and the alarm carries the answer inline instead.

**Completion alarm.** Exactly one message per run is inserted into the alarm session's inbox (`core/inbox.py`), with `kind="alarm"` and `source="chat"`, whose first line is always `core.inbox.ALARM_HEADER` = `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]`:

```
[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]
Delegated run 'editor' (id: a1b2c3) finished (status: done).
Session: spawn-20260723-103412-a1b2c3 — /home/luke/.miniouto/sessions/spawn-20260723-103412-a1b2c3.json
Result file: /home/luke/.miniouto/subagent-results/a1b2c3-2026-07-23-10-34.md — read this file (e.g. with `cat`) to see what it produced.
```

The `Session:` line names the session the run wrote **its own** transcript to — which for a `--bg` run is a generated `spawn-…` name, never the alarm session it was delivered to, and which is a `cont-…` name when the run handed off mid-work. It is what lets the receiving agent open the run's full conversation rather than just its final answer. The same name is the marker's `session` field (see `docs/storage.md`). After a hand-off the alarm carries one extra line, `Continued from: <old session> (summary: <dir>)`.

Without a result file the answer itself rides along, capped at `MAX_ALARM_ANSWER_CHARS = 4000` with a `…(truncated)` suffix. The message's `meta` carries `{id, role, style, return_path}` — that is how the receiving turn attributes the alarm to a delegated run (`LoopEvent.subagent_id` / `subagent_name`). Delivery is described in `docs/core.md` § `_make_alarm_hook`; the inbox has **one consumer per session** by design.

### Foreground vs `--bg`

Foreground runs the turn in this process and prints it (session marker, loop events, answer). `--bg` **detaches** and returns at once:

- The child's whole command line is re-emitted from the **resolved** values (`_bg_argv`, never `sys.argv`), so every optional flag reaches the child already expanded — a bare `--return` becomes `--return <path>` and can never be re-resolved differently.
- `_detach` starts it with `stdin=DEVNULL`, `stdout`/`stderr` → `spawn-logs/<id>.log`, and `start_new_session=True`. Both halves are load-bearing: `tools/bash.py` kills whole process groups and grace-kills pipe-holding children, so a background run survives only with its own session **and** file stdio.
- `--bg` prints a **5-line ack** naming every channel the run will ever use — the process is gone by the time its alarm arrives, so the caller must be told where the transcript goes and which file holds the status:

  ```
  Started run '<id>' (bg).
  Session: <name> — <abs sessions/<name>.json>
  Log: <spawn-logs/<id>.log>
  Status: <spawn-logs/<id>.json> (running → done|error|stopped)
  Alarm → session '<x>' when finished.
  ```

  The last line is `No alarm configured — check the status file.` when no `--alarm-session` was given. A liveness marker is written first (`spawn-logs/<id>.json`), and the child re-enters through the **hidden** `--bg-child` / `--job-id` flags and adopts that same id, start time, and log path rather than minting new bookkeeping. `--bg` together with `--bg-child` is an exit-1 error.
- Both paths share one body: the child runs the foreground code and stamps the marker `done` / `error` / `stopped`, exiting 1 on a non-`done` status.

The child's completion arrives later as one inbox message (above). It is delivered mid-turn by the `BEFORE_LLM_CALL` inbox hook if a turn is running, and otherwise it simply **waits in the queue** until the next wakeup — the TUI's 1 s poll, or `miniouto chat --alarms -c --name <session>`. Nothing polls on the launcher's behalf. `core/spawn_state.py`'s marker is the separate on-disk record a *monitor* reads to know which detached runs are still going.

---

## `miniouto message ...`

File: `cli/message.py`. The CLI side of `core/inbox.py` — the same channel a detached `chat --bg` uses, and the same one the TUI writes to when the user types mid-turn.

### `message insert [TEXT ...] --session NAME [--kind alarm|user] [--source STR] [--stdin]`

Inserts one message into `<NAME>`'s inbox and prints its id. Words are joined with single spaces; `--stdin` reads the body from standard input instead (passing both, or neither, is an error). `--kind` defaults to `user` and must be `alarm` or `user`; `--source` defaults to `cli` and is a free-form origin label stored with the message. Empty/whitespace text and text over `MAX_TEXT_CHARS = 100_000` are rejected. The insertion is atomic (write tmp + `os.replace`), so a live drainer sees it complete or not at all.

```bash
miniouto message insert --session default "the deploy finished; check the log"
git log --oneline -5 | miniouto message insert --stdin --kind alarm --source ci --session default
```

### `message list [--session NAME]`

Shows the messages still waiting, oldest first, one line each: `<ts>  <kind>  <source>  <first 60 chars>`. `--session` defaults to `settings.session`; an empty session is an error, never a guess. It **peeks without consuming** — the payloads are read straight out of the inbox directory and nothing is unlinked, so `list` can never deliver a message by accident. An empty inbox prints **nothing** and exits 0, so the command doubles as a poll loop's emptiness test without any text matching.

### `message clear [--session NAME] [--yes]`

Drains (and thereby discards) every pending message of the session. Prompts for confirmation unless `--yes`; prints `no pending messages` when the inbox is empty.

---

## `miniouto computer show [PATH] [--plain] [--cols N] [--head N]`

File: `cli/computer.py`. Renders one screen capture the Computer tool's `screen` action saved under `~/.miniouto/computer-screens/`.

| Argument / flag | Effect |
|---|---|
| `PATH` (optional) | The `.art` file to render. Defaults to the **newest** capture by mtime; an empty directory is an error (`no screen art yet — run the Computer tool's screen action first`) |
| `--plain` | Render the plain luminance ramp instead of the truecolour body (for logs, pipes, and non-colour terminals). It understands every glyph family both `.art` modes use — `▀`/`█` half-block, and the sub-cell `█` / BLOCK OCTANT / BRAILLE set — by blending each glyph's fg/bg by the fraction of the cell it paints |
| `--cols N` | Re-quantise to `N` columns; implies `--plain`, since re-quantising means re-sampling cell colours |
| `--head N` | Print only the first `N` rows |

A dim header line names the capture (`screen=… source=… grid=… mode=…`, plus a `re-quantised to N cols as plain ramp` note under `--cols`) and the body follows verbatim. The `.art` format is documented in `docs/storage.md`; the file format spec lives in `tools/ascii_art.py`. This is also how a **spawned role-mode run** reads what the orchestrator captured — the art carries no clickable coordinates, so those still come from `Computer(action="screenshot")`.

---

## `miniouto provider ...`

File: `cli/provider.py`. Sub-app `app = typer.Typer(help="Manage LLM providers (catalog browse + custom config).")`.

The provider command has three groups: **catalog** browse/add (`providers`, `models`, `add`), **storage** ops on already-configured providers (`list`, `remove`, `default`), and **custom** manual config (`custom add`).

### `provider providers`

Calls `GET https://lma.blp.sh/provider`. Prints a rich `Table` titled `Catalog providers (N)` with columns `Name | SDK | API URL | miniouto format | Addable?`. The "Addable?" column is `✓` when `core.providers.sdk_to_format(sdk, api)` returns a non-`None` format. Empty result → yellow "No providers returned by the catalog." (exit 0). Transport failure → red `✗ Failed to reach catalog: {exc}` + exit 1.

### `provider models <provider-name>`

Positional argument; lma does case-/whitespace-insensitive fuzzy match. Calls `GET https://lma.blp.sh/model-list?provider-name=<name>`. Prints a `Table` titled `Catalog models for '<name>' (N)` with columns `ID | Name`. Empty result → yellow "No models returned for `<name>`." + exit 1. Transport failure → red `✗ Failed to reach catalog: {exc}` + exit 1.

### `provider add <provider-name> --api-key <key> [--default-model <id>] [--reasoning <v>]`

Catalog add. Positional `provider_name` (fuzzy-matched via `core.lma.find_provider`), required `--api-key`, optional `--default-model` (default `""`), optional `--reasoning` (default `None`).

- If `find_provider` returns `None`: red `✗ No catalog provider matched <name>. Run 'miniouto provider providers' to see the catalog.` + exit 1.
- Calls `core.providers.add_provider_from_lma(...)` to build a `Provider` with `source="lma"`.
- If `--default-model` is empty, re-fetches the provider's model list and uses the first model id (re-invokes `add_provider_from_lma` with that id).
- `--reasoning` is stored verbatim as `Provider.reasoning_effort`; when omitted, it is auto-filled from `core.reasoning.default_reasoning_choice(default_model, provider_name)` (best-effort, silent when lma has no data).
- If the provider already exists: yellow `! Provider <name> already exists; overwriting.`
- On success: `✓ Added provider <name> (<api_format>, default-model=<model or ->, reasoning=<value or ->).`

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
    --default-model  TEXT  # default "" (used when neither chat --pm nor the session pin names a model)
    --reasoning      TEXT  # default None (stored verbatim as reasoning_effort)
)
```

- Validates `--format` against `core.providers.SUPPORTED_FORMATS = ("openai", "openai-response", "anthropic", "google")`. Unknown → red `✗ Unknown format <fmt>. Supported: …` + exit 1.
- Calls `storage.paths.ensure_dirs()`, builds a `storage.providers.Provider(...)` (with `source="custom"`), calls `storage.providers.upsert(provider)`.
- Prints `✓ Saved custom provider <name> (<api_format>, reasoning=<value or ->).`

### `provider list`

Pretty-prints a `rich.table.Table` titled `Providers` with columns:

| Name | Type | Format | Base URL | Default Model | Reasoning | Default |

The `Type` column renders `custom` or `catalog` based on `provider.source` (`SOURCE_CUSTOM` → "custom", `SOURCE_LMA` → "catalog"). The `Reasoning` column shows the provider's stored `reasoning_effort` (or `-`). The active provider (per `settings.toml`) is marked with a green ● in the Default column.

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

**Read-only** over the two default roots — the invocation directory's `./skills/` and `~/.agents/skills/` (NOT `~/.miniouto/`) — local first.

### `skill list`

Iterates `storage.skills.list_skills()` with no argument, which searches **both** default roots (`./skills/` first, then `~/.agents/skills/`) and stamps each skill with the root it came from — **hidden skills are excluded** (the function filters `not skill.hidden`). Empty → yellow "No skills found." followed by both default roots. Otherwise prints a rich `Table` titled `Available Skills` with columns `Name | Description | Root` (root dimmed), truncating descriptions to 80 chars + "…"; a name installed under both roots lists twice, each row naming its own root.

### `skill show <name>`

- `skill = storage.skills.get_skill(name)`, which searches the default roots in order (`./skills/` before `~/.agents/skills/`, so a project-local skill shadows a same-named global one) and does **not** filter on `hidden`, so hidden skills can still be shown by explicit name. If `None` → red `✗ Skill <name> not found.` + exit 1.
- Prints in order:
  - `Name: <name>`
  - `Description: <description>`
  - `Location: <directory>/<name>/` (dimmed)
  - `License: <license>` (if set)
  - `Allowed Tools: <tools>` (if set)
  - blank line
  - Full `skill.content`.

---

## `miniouto subagent ...`

File: `cli/subagent.py`. Sub-app `app = typer.Typer(help="Manage the subagent's provider and model.")`.

Manages the **global default** subagent provider/model/reasoning (`settings.subagent_provider` / `settings.subagent_model` / `settings.subagent_reasoning`). A delegated run started with `chat --use-subagent-provider` resolves its subagent trio pin-first against **its own** session, so it inherits *this* trio only when its session pin has no subagent keys; empty settings = the run uses outo's provider and model, and falls back to the resolved provider's `reasoning_effort`. (The TUI's `subagent` chip writes this trio **and** the session pin at once — `docs/usage.md`.) The command does not run anything itself; it only edits `settings.toml`.

### `subagent show`

Prints three rich-formatted lines — `Subagent provider`, `Subagent model`, and `Subagent reasoning` — using the same resolution as the runtime: settings values first, then the subagent provider's `default_model`, then `same as outo (<provider>/<model>)` when no override is set. The reasoning line shows `settings.subagent_reasoning` if set, else the resolved subagent provider's `reasoning_effort` if set, else `auto`.

### `subagent set [--provider NAME] [--model NAME] [--reasoning TEXT]`

Persists an override via `settings.update(...)`. At least one of `--provider`/`--model`/`--reasoning` is required — none given → red `✗ Pass at least one of --provider, --model, or --reasoning.` + exit 1. `--provider` is validated against the provider store: unknown name → red `✗ Provider <name> is not configured.` + exit 1. `--reasoning` persists a per-subagent reasoning effort; passing an explicit empty string (`--reasoning ""`) clears it (empty strings can't go through `update()` — the command does `save(replace(load(), subagent_reasoning=""))` directly, the same pattern as `clear`). On success prints `✓ Subagent override saved.` plus the resolved model. If only `--provider` is given and no model is resolvable (no stored `subagent_model` and the provider has no `default_model`), a yellow warning tells you to set one via `miniouto subagent set --model <name>`.

### `subagent clear`

Resets all three settings keys (`subagent_provider`, `subagent_model`, `subagent_reasoning`) to `""` so the subagent inherits outo again. Because `Settings.merge()`/`settings.update()` skip empty-string values, clearing goes through `save(replace(load(), subagent_provider="", subagent_model="", subagent_reasoning=""))` directly. Prints `✓ Subagent override cleared — subagent now inherits outo's provider/model and the provider's reasoning effort.`

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
| `subagent set` with no flag | `✗ Pass at least one of --provider, --model, or --reasoning.` + `typer.Exit(1)` | 1 |
| `subagent set --provider` on unconfigured name | `✗ Provider <name> is not configured.` + `typer.Exit(1)` | 1 |
| `spawn --bg` combined with `--bg-child` | `✗ --bg cannot be combined with --bg-child.` (internal flag; the combo only occurs on a hand-written invocation) | 1 |
| `spawn --role` naming a tag the style does not declare | `✗ Unknown role '<tag>' in style '<name>'. Available roles: a, b` — raised **before** the turn, so no model call is made | 1 |
| `--pm` without a `/`, or with an empty provider or model | `✗ --pm '<v>' is not a provider/model pair.` + `typer.Exit(1)` (before any resolution, so no session file and no model call) | 1 |
| `chat --use-subagent-provider` with an unresolvable/invalid subagent trio | `✗ {RuntimeError message}` (from `runtime._resolve_subagent`) + `typer.Exit(1)` | 1 |
| `spawn` finishing with status `error` / `stopped` | result file + alarm still written; marker stamped `error`/`stopped`; `typer.Exit(1)` | 1 |
| `spawn` with no resolvable alarm session | yellow warning that the alarm was **not** delivered; the run itself still succeeded | 0 |
| `message insert` with both TEXT and `--stdin`, or neither | `✗ Pass either TEXT or --stdin, not both.` / `✗ Pass message text, or --stdin …` + `typer.Exit(1)` | 1 |
| `message insert` with empty text, bad `--kind`, or oversized text | `✗ {message}` + `typer.Exit(1)` | 1 |
| `message list` / `clear` with no session in `--session` or settings | `✗ No session: pass --session NAME, or set one with miniouto chat --name NAME.` | 1 |
| `computer show` with no capture yet, or a non-`.art`/malformed file | `✗ no screen art yet — run the Computer tool's screen action first` / `✗ {path}: {exc}` + `typer.Exit(1)` | 1 |
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
                  ├─→ cli/subagent.py ─→ storage.providers, storage.settings
                  ├─→ cli/message.py  ─→ core.inbox, storage.settings
                  ├─→ cli/computer.py ─→ storage.paths, tools.ascii_art
                   ├─→ cli/tui.py      ─→ core.{chat,events,inbox,lma}, core.providers, storage.{paths,providers,settings,styles,sessions}
                   └─→ cli/chat.py     ─→ core.{chat,events,inbox,spawn_state,runtime}, storage.{paths,settings,styles}
```

External libs the CLI directly imports: `typer`, `rich.console`, `rich.table`, `rich.markdown`, `textual.app`, `textual.containers`, `textual.binding`, `textual.reactive`, `textual.widgets` (`RichLog`, `Input`, `ListView`, `ListItem`, `Label`, `Static`, `TextArea`), `textual.message`, `textual.screen`, `textual.timer`, `rich.text`, `asyncio`, `dataclasses.replace`.
