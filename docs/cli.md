# CLI Reference

`miniouto` is built on [Typer](https://typer.tiangolo.com/). The console script `miniouto` is registered in `pyproject.toml`:

```toml
[project.scripts]
miniouto = "miniouto.cli:app"
```

This dispatches into the `app: typer.Typer` object defined in `src/miniouto/cli/__init__.py`.

## Command tree

Commands marked **YAML** below print exactly one `yaml.safe_load`-able document
on stdout (all through `cli/_yaml.py`) — the official machine interface for
third-party wrappers. Errors of those commands go to stderr with exit 1; every
other command prints rich text.

```
miniouto [--version]
   ├─ (no subcommand)            → TUI mode (cli/tui/app.py:run_tui)
   ├─ status                     → show current configuration (YAML)
   ├─ chat <prompt> [options]    → one-shot chat turn (also the delegation door)
   ├─ config
   │    ├─ show --session NAME                → one session's pin + effective config (YAML)
   │    └─ set [--session NAME] [--target outo|subagent --provider P --model M --reasoning R]
   │                                          [--theme NAME] [--active-session NAME]
   ├─ provider
   │    ├─ providers                          → list the modelslist catalog index (YAML)
   │    ├─ models <provider-name>             → list models for a catalog provider (YAML)
   │    ├─ add <provider-name> --api-key …    → add a provider from the catalog
   │    ├─ custom add  --name --format --base-url --api-key --default-model
   │    │                        [--max-context-window N] [--max-output-tokens N]
   │    ├─ list                               → list configured providers (YAML)
   │    ├─ remove <name>
   │    ├─ default <name>
   │    └─ edit <name> [--default-model M] [--reasoning R] [--max-context-window N]
   │                 [--max-output-tokens N] [--base-url URL] [--api-key KEY]
   │                 [--compatibility DIALECT]
   ├─ style
   │    ├─ list                               → list installed styles (YAML)
   │    ├─ set <name> [--session NAME]
   │    ├─ add <repo_url> [--name NAME]
   │    ├─ update
   │    └─ show <name>
   ├─ message
   │    ├─ insert [TEXT ...] --session NAME [--kind alarm|user] [--source STR] [--stdin]
   │    ├─ list [--session NAME]              → peek pending messages (YAML)
   │    └─ clear [--session NAME] [--yes]
   ├─ subagent
   │    ├─ show                               → resolved + override provider/model/reasoning (YAML)
   │    ├─ set [--provider NAME] [--model NAME] [--reasoning TEXT]
   │    └─ clear
   └─ computer
        └─ show [PATH] [--plain] [--cols N] [--head N]
```

> **Naming note:** the catalog commands (`providers`, `models`, `add`) source their data from `https://modelslist.blp.sh` (see `cli/provider.py` importing `core.modelslist as catalog_api`, and the `+ add from catalog…` TUI sentinel). Newly added providers are stored with `source="modelslist"` (`SOURCE_MODELSLIST`). The legacy `SOURCE_LMA = "lma"` value still exists — it is only ever **read**, so an old `providers.toml` keeps catalog behavior; no `lma.blp.sh` request is made. See `docs/modelslist.md`.

## Root callback behavior

`@app.callback()` in `cli/__init__.py` runs before any subcommand. It:

1. Calls `storage.paths.ensure_dirs()` to guarantee `~/.miniouto/` and its `style/`, `sessions/`, `logs/` subdirs exist (also force-refreshes bundled styles — see `storage/paths.py`).
2. If `--version` was passed: prints `miniouto {__version__}` and exits 0.
3. If no subcommand was invoked (`ctx.invoked_subcommand is None`): calls `tui.run_tui()` which launches the xnano TUI.

The root app is configured with `no_args_is_help=False` and `invoke_without_command=True` so that `miniouto` alone runs the TUI without printing help text.

---

## `miniouto` (no args) — TUI mode

Delegates to `cli/tui/app.py:run_tui()`, which detects the terminal's graphics mode (invariant 15), constructs a `ChatTUI` and runs it on an asyncio loop until the user quits.

**The TUI is a CLI wrapper.** It owns the screen (xnano canvas, rows, modals) and talks to the agent harness the way any third-party program must — by running the `miniouto` CLI as a subprocess and parsing its stdout plus the files the CLI writes. `cli/tui/bridge.py` is the whole subprocess surface, so every rule lives in one place:

- **D7 — never the global `miniouto` on PATH.** Every argv starts with `[sys.executable, "-m", "miniouto", …]`: THIS interpreter's miniouto, never a possibly-stale installed snapshot. The env (`MINIOUTO_HOME`) and the cwd are inherited, so a child sees exactly the storage the TUI sees.
- **The TUI writes nothing to storage itself.** Mutations (config trios, theme, active-session pointer, provider records, style pins, inbox inserts) are CLI commands; the reads that remain in-process are display reads of CLI-produced artifacts (session JSON, providers.toml, style listings).
- **Machine output is YAML or one id.** Listing commands print YAML through `cli/_yaml.py`; `message insert` prints the message id. Errors go to stderr with a non-zero exit, so a wrapper never finds prose in the data lane.

A turn is `chat <prompt> --name <session> -c` as a **foreground subprocess** (the session pin carries provider and style, so no model flags are needed); the idle alarm wakeup is the same child with `--alarms` instead of a prompt. Live display is **hybrid**: answer token deltas come from the child's stdout (the raw, unprefixed lane above), and every other live event (tool calls and results, completed thinking, context, drained alarms) comes from tailing the session JSON the CLI increments on every event. At `------finish------` the final answer is read from the session file — the file's `turn.assistant` is the canonical text. A hand-off is detected from the `------summary → X------` + `summary: <dir>` marker pair on stdout; the CLI persists the marker into the OLD session (so a reload replays it) and the TUI re-pins the new session.

The substrate is **`xnano`** (a retained-mode canvas) with an immediate-mode frame builder on top, reached through a pyratatui-shaped compat shim. `cli/tui/pt.py` re-implements the exact `pt` surface the package uses (`Color`/`Style`/`Span`/`Line`/`Text`/`Paragraph`/`Rect`/`TextState`/`Terminal`) on `xnano` + `xnano-core`, so every call site reads `from . import pt` and nothing else about the layers changed. Each ~60 Hz tick composes a whole frame of styled lines and paints it one row at a time through `Terminal.draw` — each row is `canvas.write(..., height=1)` and the placement it returns is REUSED when the same cell/width is painted again, so only the text is replaced and xnano's diff can emit nothing when nothing changed; the canvas itself — the alt screen, the blit loop, the diff — is xnano's.

**Input belongs to xnano, not to us.** `pt.Terminal(real_screen=True)` registers one hook per kind (`key`/`mouse`/`scroll`/`paste`/`resize`) *before* the canvas opens — xnano decides which terminal settings to turn on from the hooks that exist at open time, and a hook attached afterwards never runs — and each hook translates its xnano reading into our own `Key`/`Mouse`/`Paste`/`Resize` and hops it onto the asyncio loop thread, so every handler still runs where it always ran. The `InputReader`/`InputParser` pair in `cli/tui/input.py` is retired on the real terminal (it would block on a descriptor xnano owns) but is **not** deleted: the event dataclasses and the headless `Pilot` still use it. Two `canvas.open()` flags are load-bearing, both measured on a pty: `keyboard="disambiguated"` (the default reports a bare CR as `Ctrl+J`, so Enter never reached the router and submit was dead) and `capture=False` (xnano's default replaces fd 1 with a pipe, which kills the OSC title/clipboard lanes *and* the `TIOCGWINSZ` resize poll with `Inappropriate ioctl for device`). `interrupt=False` keeps `Ctrl+C` as the app's own key event rather than letting xnano stop the canvas. The lone-ESC `flush_timeout` is gone too — xnano disambiguates a bare Escape itself.

The package still owns its cell-correct word wrap (`rich.cells.cell_len`, never the shim's `Span.width()`, which answers CELLS precisely so it never mis-wraps CJK — the old pyratatui `width()` counted characters), and its headless twin: `ChatTUI(terminal=None)` driven by `nodes.py:run_test` / `Pilot` / `Compositor`, which is what the behavioural suite runs against. `pt.Terminal`'s headless mode owns an offscreen `xnano.new()` canvas and exposes `output_text()` / `output_lines()` / `output_ansi()` for assertions; both modes share the whole painting path, so the frame the tests read is the frame a terminal shows.

Layout (top to bottom):
- `Header` — a full-width bar: the centred `title — sub` string (no icon)
- log (the windowed chat transcript) + `ChatInput` (a 1..N-row multiline editor), both inside the centred `CHAT_MAX_WIDTH` column
- `BottomPanel` (height 3) with three rows:
  1. spinner row (a braille spinner, one ticker at ~12.5 fps, shown while a chat turn is in flight)
  2. chip row A — `outo` on the left, `style` on the right
  3. chip row B — `subagent` on the left, `session` on the right

There is **no** Footer key bar — it was dropped deliberately, so every action lives on a key (see "Keybindings") or in the command palette. The header's background and the panel's background span the terminal; both stop their *content* at the chat column so the chips line up with the log and input. A modal renders itself over a dimmed copy of the frame below it.

### Update check

At startup the TUI checks `https://pypi.org/pypi/miniouto/json` in a background worker, so the first frame and chat input are not delayed by the network. It compares PyPI's advertised version with the installed `miniouto` distribution's version. Only a strictly newer version produces a warning row with the current and latest versions and the upgrade command (`uv tool upgrade miniouto`). An equal or older installed version produces no warning. The request has a 3-second timeout; an unavailable server, HTTP error, or malformed response is ignored without interrupting the TUI.

### Chat log rows

The chat log is a list of **row objects**, not widgets: the frame builder is immediate-mode, so a row owns only STATE (its text, its collapsed flag, its attached result, its image) and is essentially a function of `(state, width, theme)` → styled lines. `RowStatic` is the base every row derives from: it builds the pre-wrapped lines the app paints (heights are therefore EXACT, which is what the scroll window and the mouse hit-testing need), caches them per width, and paints drag selection itself with plain **reverse video (fg/bg inversion)** — Textual's `$screen-selection-*` path is gone, and Markdown was never painted by it anyway — character-accurate (CJK-safe, via `cell_len`) and clipped at the text end. `Ctrl+Shift+C` copies the extraction, padding stripped.

| Row object | Shows |
|---|---|
| `EventRow` (plain) | user prompts (`> ` in accent), blank spacers, `[system]` lines in warning color |
| `AnswerRow` | the final answer as Markdown (`render_markdown`), painted on the `text_answer` layer — the top of the text hierarchy, which is why it is its own class rather than a flag on `RowStatic` |
| `ThinkingRow` | reasoning/thinking — **collapsed by default** (`▶ thinking`), a plain click anywhere on the row or `Enter` to expand the full text (`▼ thinking` + content), clicking again to re-collapse. A drag that selects text never toggles it, and a double-click selects no whole row. While reasoning is still streaming, the header carries a `●` marker plus a one-line dim tail of the reasoning so far (whitespace-collapsed, showing the newest words, `…`-prefixed when the head was cut); on completion both vanish and the message's whole text replaces the streamed fragments in place, so a settled collapsed row is exactly `▶ thinking`. Same translucent border + muted styling as `EventRow` |
| `StreamRow` | the **live** assistant text while the model is still generating it: created on the first text delta of a message and updated in place for each subsequent flush (one row per message, not per token). Replaced by the canonical `AnswerRow` when the turn's final answer arrives, or kept as the record of an intermediate message whose whole text is otherwise suppressed |
| `ToolRow` | a tool call — a collapsible row whose header carries a per-tool badge in place of the `▶`/`▼` glyph (`[>_]` Bash, `[⮜]` Computer, `[⣏⣹]` Image/Video, `[♫︎]` Audio; tools without a badge keep the glyph), the verbatim call preview, and once expanded the command (Bash only) and the attached result in bordered boxes, including the `.art` render of a Computer capture and, on a graphics terminal, the PNG placed as an overlay (invariant 15) |

There is **no** `SubagentRow` / `SubagentDetailScreen`: a spawned run is a separate process with its own event stream, so there is no per-actor row stream to drill into. What a spawned run contributes to this transcript is its **completion alarm** (a user-style row — see "Messages" below).

Tool rows start as a single header line — `[>_] Bash ls -la` for a badged tool, `▶ <summary>` for one without a badge — whose badge/glyph highlights on hover (measured with `cell_len`, so the whole `[>_]` reverses, not just its first cell). A collapsed **Bash** row paints exactly ONE line: the header's first visual line, cell-cut with a trailing `…`, never a wrapped wall; every other tool keeps its wrapping header (its preview is one line by construction). A plain click anywhere on the row (badge, header text, expanded body, attached result) or `Enter` expands it: a Bash row reveals the FULL verbatim command in a bordered box with the output/result in a second box below, and its header shows the badge ONLY (`[>_]`, plus the ` ✗` error suffix when errored) — the command lives verbatim in the box below, so repeating it in the header would duplicate that box; every other badged tool keeps badge+preview expanded (its box holds only the result), and a badge-less row keeps the legacy layout untouched. Below the boxes a Computer capture keeps its `.art` text render / PNG overlay lane exactly as before. Click vs. text-selection is decided by the gesture, not by position: a drag that takes at least one character is a selection and leaves the row alone, while a gesture that takes none is a plain click and toggles. Drag selection is CELL-accurate on the painted line: the app converts the mouse cell column to a CHARACTER offset by walking the painted content line (`cell_len` per character), so a drag over CJK glyphs (1 char / 2 cells) or the Audio badge's U+FE0E variation selector (1 char / 0 cells) extracts exactly the characters the pointer swept over. Double-clicking no longer selects the whole row (the second click of a double-click is consumed, so the row opens at most once).

### Live token streaming

While the provider is streaming, **both surfaces render text as it arrives** (coreouto's `ON_STREAM_TEXT` / `ON_STREAM_THINKING` fire once per token).

**The CLI streams tokens straight to stdout** — raw, no `outo:` prefix, flushed per fragment, on a tty AND on a pipe (the TUI-as-CLI-wrapper project reads the token stream off a piped stdout). The contract:

- **Text deltas print as they arrive.** A delta first stops the spinner (`end_working`) if it is live: while a message is mid-stream the tokens themselves are the liveness signal, and the spinner is not re-lit until the message completes. Thinking deltas are liveness exactly the same way — a reasoning fragment stops the spinner too.
- **Thinking deltas print on a prefixed lane that round-trips.** A `thinking` fragment goes out as prefixed physical lines — one per line of the fragment, so the prefix never sits mid-line and a wrapper's line parser stays sound — and each line's prefix says whether the newline ending it is CONTENT or mere FRAMING: `outo:thinking: <line>` (that newline is content — the fragment carried it) vs `outo:thinking| <line>` (the unclosed mark: the fragment did NOT end in a newline, so that newline is framing only). Every line the fragment breaks after prints on the plain prefix; a fragment without a trailing newline prints its final line on the unclosed mark, while one with it needs no extra line (its final content line's newline IS the content). The TUI reassembles the fragment byte for byte by stripping one prefix per line — re-adding the newline only of plain-prefixed lines, never of unclosed ones — so a burst of fragments (`"The"`, `" user asks:"`) reassembles as `"The user asks:"` with no fake line break at any fragment boundary (the early-wrap defect). The prefix is fixed at `outo` because there is one actor per process.
- **Completed messages are not printed twice.** The whole-text `response` event of a message whose tokens already went out is suppressed, and so is the whole-text `thinking` event whose fragments already went out on the prefixed lane; a `response` or `thinking` from a non-streaming turn still renders as one line, exactly as before.
- **The final answer is not reprinted when it streamed.** `cli/chat.py:_print_answer` asks the last turn's sink whether the answer text already went out as tokens; when it did, only the run's `------finish------` marker is written — preceded by one newline when the stream ended mid-line, so the marker never shares a line with answer text. An answer that did NOT stream (a synthesized hand-off message, or content that differs from the stream) prints in full as before.
- **Message boundaries reset the state.** The `context` event of each iteration closes the message: the streamed text is retired into "already shown" state and the spinner may return for the next LLM call / tool execution.
- **Quiet stays byte-identical.** `-a` / `--answer-only` / `--with-session` print no deltas at all (the sink's stream path is suppressed, spinner untouched), so their output contract — markers included — is unchanged.

In the **TUI** the token lane is the child's stdout: `_read_chat_stdout` reads chunks as they arrive (never a single read-to-EOF, which would hold every token until the turn ended), `outo:thinking:` / `outo:thinking|` delta lines land on the live `ThinkingRow` immediately (a partial line is shown as soon as its prefix is complete, and a chunk boundary that splits a line mid-content is reassembled via the open-line flag, which remembers the split line's mark so its completion adds the newline only when the mark said content), other marker/actor-prefixed lines belong to the structured lane, and anything else is answer text that lands on the live `StreamRow` immediately. Ordering is safe by construction — stdout deltas arrive while the message is still streaming and the session file's completion events land after it — and `_add_event_row`'s existing suppression (a `response` event with a live stream row is dropped; a `thinking` event finalizes the live row in place, `●` → collapsed) shows each message once. The session tail (`_SessionTail.pump`) replays the RUNNING turn's events as they land and keeps its cursor ON that turn — advancing only through the events the turn already holds — because a tool turn's second message writes its events to the SAME running turn seconds after the first; a cursor that left the turn as soon as it had one event never saw the rest, and a second streamed reasoning lost its whole-text event (its live row was swept away instead of finalized). The cursor moves past a turn only once it is `done`/`interrupted`.

Deltas are **display-only** in both surfaces: they are never written to the session file (`_RecordingSink` forwards them to the wrapped sink without recording), so a reloaded session is byte-identical to a pre-streaming one. (The repetition watchdog IS fed from them — see invariant 8 in `AGENTS.md`.)

Finalization avoids showing the same text twice: the live `StreamRow` is dropped and the Markdown `AnswerRow` takes its place for the final answer, the whole-text `thinking` event finalizes the live `ThinkingRow` in place, and the stashed `response` row is suppressed whenever deltas already showed that message (a non-streaming turn renders exactly as before).

### Messages (the inbox), width cap, and scrolling

- **Mid-turn insertion.** Typing while a turn is running does not poke the live message list: it goes to `miniouto message insert --session S --kind user --source tui` (a bridge subprocess; the id it prints keys the optimistic row). One code path then serves human interjections, spawn alarms, and `chat --alarms` wakeups. The row renders optimistically, and when the drain later produces the matching loop event `ChatTUI._claim_inbox_event` matches `detail="inbox:<id>"` and drops the duplicate.
- **Idle delivery.** No registry callback pushes messages any more (a detached process may write at any moment), so the TUI polls the inbox every `_INBOX_POLL_INTERVAL` (1 s) and at the tail of `_dispatch`. The poll is **stat-gated**: the inbox directory's `(mtime_ns, size)` is read first and only a CHANGE spends a subprocess on `message list --session S`; a changed listing starts a turn whose prompt **is** the joined message text (the `chat --alarms -c --name S` child), recorded as that turn's `user` so display, replay, and model perception are the same string by construction. While a turn is busy the child chat's own `BEFORE_LLM_CALL` hook drains the inbox mid-turn, exactly as before.
- **Single consumer.** One session belongs to one live process; a second process bound to the same session would steal each other's messages (see `docs/core.md` § `core/inbox.py`). Before a TUI has chosen a session, it polls a unique `tui-pending-<hex>` queue, never the shared `default` or another process's published session.
- **Summary hand-off.** If a turn ends because the model called `continue_session`, `ChatTUI._apply_handoff` appends the `------summary → <new>------` + `summary: <dir>` marker as a system row in the **old** conversation, re-pins the new `cont-*` session, and lets the 1 s inbox poll auto-deliver the continuation message — so the transition needs no user action. The on-disk record of the marker in the OLD session is written by the CLI layer's `_persist_handoff_marker` (same event shape), so a TUI reload replays it. The old conversation keeps its history: a hand-off is a session change, never a history edit. **Talking to a summarized session follows its redirect**: a submitted message resolves `resolve_redirect` of the pinned session first and, when the chain is longer than one hop, the TUI moves there (`_pin_session` + history reload + a `session <old> was summarized → redirected to <final> (via …). Continue here.` notice), and the typed text is restored to the input UNSENT (left for the operator to re-send) — it is for the live work, not the stopped transcript. Opening a session from the picker is plain viewing and never follows a redirect.
- **Width cap.** The log column is centered and capped at `CHAT_MAX_WIDTH = 150` cells (theme variable `$chat-max-width`), so wide terminals do not stretch the transcript. The header chips sit in two left/right-anchored pairs inside that column (`outo` / `style`, then `subagent` / `session`) and render their values whole — two chips a row is what keeps every value short enough not to truncate, so every picker stays clickable at any terminal width.
- **Scrolling.** `PgUp` / `PgDn` / `Home` / `End` act **instantly**: the TUI takes them at the app level before the input sees them (they would otherwise move the caret), and no scroll is ever animated — animating through a long transcript's heights would lose the target. The wheel scrolls `WHEEL_LINES = 3` rows per detent. A windowed log (`_RowWindow`: a trailing window of rows is painted, older chunks load as the view nears the top, far-above rows evict) keeps a long session's per-frame cost flat, and a jump-to-bottom button sits under the log whenever the view is scrolled up.

### Clickable chips

Each `StatusChip` is a focusable node. Click it (or focus it with `Tab` and press `Enter`) to open a modal:

| Chip | Modal | Notes |
|---|---|---|
| `outo` | `ProviderSettingsPopup` — trio rows `Provider` / `Model` / `Reasoning`, then `Compatibility` for catalog providers or `Caps` for custom ones | `TrioTarget` = `OUTO_TARGET` (`provider`/`model`/`reasoning`) |
| `subagent` | the **same** `ProviderSettingsPopup`, with `SUBAGENT_TARGET` (`subagent_provider`/`subagent_model`/`subagent_reasoning`) | empty still reads `same as outo`, and an empty subagent key means "inherit" |
| `style` | `SelectionModal` — list of installed styles | writes `settings.style` |
| `session` | `SelectionModal` with `sections=` — grouped by the session's creation directory, plus `+ new session…` | writes the session **pin** (`self._pinned_session`), which is what `_dispatch` and the inbox poll route on |

**Every provider/model/reasoning edit goes through the CLI** — the popup's `_persist_trio` (in `cli/tui/config.py`) issues `config set --target outo|subagent --session S --provider P --model M --reasoning R` (a bridge subprocess), and the CLI performs the whole invariant-12 write: **both halves** (`docs/storage.md` § `session-config/`) — the session pin at `session-config/<pinned session>.toml`, so the edit survives a later default change, and the global default, so other processes and the next session follow the most recently changed choice. For the **outo** trio the global half is the **paired** `settings.provider` + `settings.model` + `settings.reasoning` — written together as one choice, through `save` (not `update`, which skips empty strings, so an emptied model CLEARS instead of leaving the old one). The provider record's `default_model` still follows the pick, so the change is the new default for every process that reads the record — but its `reasoning_effort` is LEFT ALONE, because a subagent on the same provider resolves its effort from that record and rewriting it is what dragged the subagent along with every outo reasoning change. The settings trio is what the TUI's own rows read back, and it is kept SEPARATE from the record so that editing outo cannot move a subagent (a subagent resolves its model from its own key and its own provider record, never from outo's). For the **subagent** trio it is the three `settings.subagent_*` keys. **Provider and Model both reset Reasoning** — an effort level belongs to a model, so either pick lands on that model's default (the provider record's own preference, else modelslist's auto default); Reasoning is one more click away when that is not what you want. An empty value clears the pin key and hands it back to the global default.

The catalog `Compatibility` row shows the saved modelslist format id (or `native SDK` for a legacy record). Its picker lists every hostable format with its exact base URL, plus the saved choice when the current catalog no longer reports it; selecting one persists `catalog_dialect`, `api_format`, and `base_url` on the provider record. Like `Caps`, this is provider-global, not part of the session trio. An inheriting subagent popup edits the effective outo provider.

The popup's `Caps` row (custom providers) and `Compatibility` row (catalog providers) edit the provider RECORD through the CLI: `provider edit <name> --max-context-window N --max-output-tokens N` and `provider edit <name> --compatibility <dialect>` (see the `provider edit` section above) — provider-global writes, not part of the session trio. A script can make the same edits without the TUI.

There is **no** session chip — the session label in row 3 is a plain `Static`. To change sessions, use the command palette (`Ctrl+P`) → "Pick session" / "New session".

The "Pick session" modal is the one place that uses `SelectionModal`'s opt-in `sections=` argument, and it groups by the session's creation directory: a **"This directory"** section first, then **"Other directories"** with each entry prefixed by its basename (`[myproject] old-thing`, or `[?] legacy` for files written before the `cwd` field existed), then the `+ new session…` row. Section headers are non-selectable display rows. Every other picker passes no `sections=` and renders exactly as before.

The popup's `Provider` row accepts two sentinels (rendered as `extra_options` rows at the bottom of the list):

- The provider picker opens **even when no provider is configured**: the two `+ add …` entries below are the only way to create the first one, so an empty list must not short-circuit into a dead-end message. The modal's title says `No providers yet — add one` in that case.
- `+ add from catalog…` — runs `_catalog_add_flow`: fetches the modelslist root index, filters to entries whose `formats` yields a supported coreouto `api_format`, lets you pick one, prompts for the API key, then saves the provider with `source="modelslist"` (named after the catalog `slug`) and the first model of its catalog-owned list as `default_model`; when the model has reasoning options it also offers a picker and stores the result as `reasoning_effort`. See `core/modelslist.py` and `core/providers.py:DIALECT_TO_FORMAT`.
- `+ add custom…` — runs `_open_custom_add_wizard`: a five-step wizard (name → api_format → base_url → api_key → default_model), saving with `source="custom"`.

Modal results are persisted through the CLI lane: the trio write is `config set` (see above), a style pick is `style set <name> --session S`, and the theme choice is `config set --theme <name>` — the TUI writes none of them itself. The chips repaint on the next frame, and the active theme is restored from `settings.theme` on launch.

### Theme

`ChatTUI` ships the harness's own dark theme, `MINIOUTO_THEME` (name `miniouto`, `cli/tui/theme.py`), and boots on it. Because the port paints the canvas directly (`pt.Color` → `xnano` styles), a theme is a frozen `TuiTheme` dataclass of plain `#rrggbb` strings — there are no CSS `$variables` to resolve at paint time. The palette is comfort-tuned for long sessions: the floor is lifted off near-black (background `#0a0e14`, ~2× the old `#05070a`'s relative luminance, and still darker than the stock dark `#121212`), the surfaces step `#0f141b` → `#151b24` → `#1d2530`, and the ceiling comes down from the old 13.28:1 glare — but not all the way to one undifferentiated gray, because the agent's **final answer must still be the brightest thing on screen**. The text therefore forms an explicit **three-layer hierarchy** (plus the disabled ramp):

| Layer | Field | Hex | On `background` | Painted on |
| --- | --- | --- | --- | --- |
| answer | `text_answer` | `#c5d0d8` | **12.33:1** | the final-answer row only (`AnswerRow`) |
| body | `foreground` | `#a9bac5` | **9.69:1** | everything else in the transcript |
| muted | `text_muted` | `#7b8490` | **5.11:1** | event, tool and thinking rows |
| disabled | `text_disabled` | `#434a52` | 2.16:1 | disabled controls (WCAG-exempt) |

The answer layer is the only near-white in the palette, so the hierarchy reads at a glance: answer > body > muted. `foreground` also got its chroma back — `#a9bac5` is 3.4× the saturation of the `#a8b0b1` it replaces (S≈0.19 vs S≈0.06), which is what "too gray" was; it carries the theme's blue family without going clinical. Muted and disabled are unchanged. The four semantic colors are one notch down (secondary `#8f86d8`, success `#52bd8a`, warning `#c99a4e`, error `#d16f7e`) and each clears 4.5:1 on all four surfaces, and the owner-pinned `#2fc2ff` accent — carried by **both** `primary` and `accent`, so the logo, the focus rules, block cursors and the tool-row toggle glyph are all the same cyan — is left exactly as it was; it reads calmer for free (9.87:1 → 9.47:1) because the background underneath it is lighter.

`boost`, `text_muted`, `text_disabled` and `text_answer` are real `TuiTheme` fields, not derived at paint time: Textual used to compute them (`boost` from `panel`, the ramp as `foreground` composited at 60% / 38% over the background), and `theme.py` reproduces that same arithmetic so the ported palettes look byte-identical to the Textual ones they replace — `panel` when a theme omits it is `surface` lerped 10% toward `primary`, `foreground` when omitted is the background's inverse, `boost` steps 8% toward white (or black on a light theme), and the ramp is alpha compositing rather than a darkening curve (reading "60%" the other way inverts the ladder). `AnswerRow` paints `text_answer` because its content is the Markdown answer; the near-black ink on the picker's solid-cyan selection bar comes from `render.auto_text` (the port of Textual's `auto 87%` — pinning it to the solid `foreground` drops that row to 1.08:1), while the transcript body paints the declared `foreground`. `get_css_variables()` still exposes the resolved palette under the old CSS names (that is what the theme tests read), and `$chat-max-width` is reported there from `CHAT_MAX_WIDTH` even though no CSS consumes it.

`settings.theme` overrides the default at launch (`ChatTUI._boot_theme`); a name no build registers falls back to `miniouto` rather than failing the launch. `Ctrl+P` → Theme changes it at runtime, and the choice is written back through the CLI (`config set --theme <name>`, on a worker so the subprocess round-trip never freezes the UI thread). The picker order is `miniouto` first, then the stock themes re-derived as `TuiTheme`s (`textual-dark`, `textual-light`, `nord`, `gruvbox`, `catppuccin-*`, `dracula`, `tokyo-night`, `monokai`, `flexoki`, `solarized-*`, `atom-one-*`).

### Model resolution order

`resolve_runtime_from_settings` (core/runtime.py) resolves the active provider/model/reasoning in this order, per key:
1. the explicit override (`ChatOptions`, i.e. `chat --pm`)
2. **this session's pin**, `session-config/<session>.toml` — frozen at the session's first resolution, so a model switch in another process can never redirect a live conversation
3. the global default: `Settings.provider` / `Settings.model` / `Settings.reasoning` (the outo trio, one paired choice) and `Settings.subagent_*`, with the resolved provider record's `default_model` / `reasoning_effort` answering where nothing else names a model

In the TUI, every row shows the **configured** value when there is one, else what it resolves to (the provider record's own default), else `-`.

Keybindings (one app-level router in `ChatTUI._handle_key`: global keys first, then the modal on top, then the focused node):
- App-level: `Ctrl+L` — clear log; `Ctrl+C` — quit; `Ctrl+Shift+C` — copy the selection; `Ctrl+P` — open (or dismiss) the command palette; `Esc Esc` (two presses within 2.0 s while the agent is busy) — force-stop the running turn. The turn is a chat **subprocess**, so the stop is a **SIGINT to the child**: the CLI's plain-chat Ctrl-C path stamps the running turn `interrupted` on disk immediately, prints `------finish------` + `stopped: cancelled`, and exits **130** (see "Behavior" under `miniouto chat`). The TUI reads the outcome back — the child's exit code and the session file's turn status — and shows `stopped by user (esc esc)`; a single `Esc` while busy shows a persistent "press esc again to stop" hint in the spinner line (it survives spinner ticks for the full 2.0 s window; after a successful double-press, "stopping after the current step…" persists until the turn ends).
- `Ctrl+P` — the command palette (`modals.py:CommandPalette` + `default_system_commands`, behind `get_system_commands`): a centred dialog titled **Menu** listing every command — new session, pick session, configure outo, configure subagent, pick style, theme, keys, quit. There is **no search input**: the list is unfiltered and scrolls (`Up`/`Down`/`Tab` move, `Enter` runs, `PageUp`/`PageDown` page, mouse clicks a row, `Esc` or a click outside closes). No numeric prefixes (`00`, `01`, … were Textual's alphabetical-sort workaround) and no clear-log / maximize / screenshot entries; `clear log` is still on `Ctrl+L`. The splash text on boot says "Press Ctrl+P for commands."
- Node-level (inside modals / chips / the input): `Tab` / `Shift+Tab` — cycle focus; `Enter` — confirm; `Esc` — cancel.

Submission flow: each submitted prompt is dispatched by `self.run_worker(self._dispatch(prompt), exclusive=True)`, which spawns the `chat` child subprocess and pumps its stdout on the asyncio loop (so the ~60 Hz draw stays responsive). The busy flag (`self._busy`) prevents re-entrancy. Output becomes row objects (see "Chat log rows" above), composed into the next frame; a refused child's first stderr line renders as `chat: …`, a non-zero exit as `chat exited with <code>`.

`cli/tui/app.py:tui_summary() -> dict` is a programmatic snapshot helper (currently unused by the runtime) that returns `{provider, model, style, session, styles_available}`.

---

## `miniouto --version`

Prints `miniouto <version>` (from `miniouto.__version__`, currently `"0.13.0"`) and raises `typer.Exit()` (exit code 0).

---

## `miniouto status`

File: `cli/__init__.py`.

Reads:
- `storage.settings.load()` → active `provider`, `style`, `session`; `model` is derived from the active provider's `default_model` (or `'- (use chat --pm)'` when empty)
- `storage.paths.ROOT` → storage path
- `storage.providers.load_all()` → all configured provider names
- `storage.styles.list_styles()` → installed style names
- `storage.skills.list_skills()` → all visible skill names (hidden skills excluded)
- `storage.sessions.list_sessions_with_meta()` → `(name, cwd, mtime)` per session file, read by parsing the JSON directly (never through `load()`, which would write)

Prints one YAML document (see `cli/_yaml.py`) — the official machine-readable snapshot, parsed by third-party wrappers with `yaml.safe_load`:

```yaml
provider: minimax
model: MiniMax-M2.1
subagent:
  provider: minimax
  model: same as outo (minimax/MiniMax-M2.1)
style: coding-work
session: default
storage: /home/you/.miniouto
providers:
- minimax
- local-llama
styles:
- coding
- coding-work
- default
skills:
- hf-cli
- mojo-syntax
sessions:
  this_directory:
  - default
  - tui-20260723-103412-a1b2c3
  other:
  - old-thing
```

Always exits 0 (no error states). A session's `cwd` is stamped from the launch directory on its **first** turn only, so the grouping means "the directory this session was created in", not "wherever it was last used". The `subagent.model` value is the resolved subagent `provider/model` when `settings.subagent_provider` or `settings.subagent_model` is set, or `same as outo (<provider>/<model>)` when it inherits. This trio is the **global default** a delegated run inherits — every `chat --role <tag>` run that carries no `--pm` and asks for the subagent profile (`--use-subagent-provider`) uses it.

---

## `miniouto config ...`

File: `cli/config.py`. Sub-app `app = typer.Typer(help="Show and edit provider, style and session configuration.")`.

The **config lane of the trio writes** (invariant 12): the TUI settings popup's `_persist_trio` writes a provider trio through BOTH the session pin and the global default, and `config set` is that write's canonical CLI form — a script can make the same edit without opening the TUI. `config show` is the read side: it reproduces the TUI chips' merged interpretation and never freezes a pin (looking at a session's configuration must not pin it — freezing belongs to the turn).

### `config show --session NAME`

Prints one YAML document — the session's raw pin beside the effective configuration the runtime would apply:

```yaml
session: default
pin:
  provider: local
  model: qwen3-32b
  reasoning: high
  subagent_provider: ''
  subagent_model: ''
  subagent_reasoning: ''
  style: ''
effective:
  provider: local
  model: qwen3-32b
  reasoning: high
  style: ''
  subagent_provider: ''
  subagent_model: ''
  subagent_reasoning: ''
```

- `pin` is the RAW stored pin (`session_config.read`, `""` everywhere when the session has none). Read-only: showing never freezes a pin.
- `effective` is the merged interpretation the TUI chips show (`_merged_config`): the pin is a set of overrides ON the default, so an empty **subagent** key is filled from the global `subagent_*` default. The **outo** trio is left as the pin has it — filling it from the default would show the default's reasoning beside the pin's model, a combination the runtime never produces.

### `config set [--session NAME] [--target outo|subagent] [--provider P] [--model M] [--reasoning R] [--theme NAME] [--active-session NAME]`

At least one of `--target`, `--theme`, `--active-session` is required — none given → red `✗ Pass at least one of --target, --theme, or --active-session.` + exit 1.

**`--target` (the trio write).** Requires `--session` and all three of `--provider` / `--model` / `--reasoning` (an empty `--reasoning` clears it; `--provider/--model/--reasoning` without `--target` is an error, and an unknown `--target` is rejected). The provider must already be configured (`provider_store.get`) — `✗ Provider '<name>' is not configured.` + exit 1. The write is the whole invariant-12 edit:

1. the **session pin** (`session_config.set_fields`) — what THIS session resolves, so the edit survives a later default change;
2. the **global default** — the outo trio writes the PAIRED `settings.provider` + `model` + `reasoning` through `save` (never `update`, which skips empty strings, so an emptied value CLEARS the stored one); the subagent trio writes the three `settings.subagent_*` keys;
3. the provider RECORD's `default_model` follows the outo pick (`dataclasses.replace` + `upsert`) — the ENDPOINT's default, what a fresh install resolves. The record's `reasoning_effort` is left alone: a subagent on the same provider resolves its effort from that record, and rewriting it is what dragged the subagent along with every outo reasoning change.

On success: `✓ {target} trio → {provider or -}/{model or -}[:{reasoning}] (pin '<session>' + global default).`

**`--theme NAME`** writes `settings.theme` (any name; the TUI falls back to `miniouto` at boot when no build registers it). **`--active-session NAME`** publishes the active-session pointer (`settings.session` — the value `chat -c` without `--name` resolves and `status` shows; need not exist yet, because a TUI pins its session before the first turn creates the file). The two combine with a trio write in one invocation; each names its own part in the ack.

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
| `--continue` / `-c` | Prepend the session's previous history. Never implied — a plain `chat` starts fresh, and a wakeup must spell it out. A delegated run using `--continue` must also name `--name SESSION`, never inherit the caller's active session |
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
| A summarized session is **handed over, not run** | `_apply_session_redirect` resolves `resolve_redirect` of the named session (after `_resolve_session`, before any write/drain/model call): when the chain is longer than one hop it prints `session <old> was summarized → the work continues in <final> (via …). Re-run with --name <final> to continue there.` and exits WITHOUT a turn — the queued messages survive for the re-run the notice asks for. A fresh/minted session never has a redirect |
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
   - then `_apply_session_redirect(session_name)`: an INTERACTIVE resume (`-c`, `--alarms -c`) of a summarized session prints the hand-over notice and stops the run (returns before any write, drain, or model call); a session with no redirect keeps its own name.
2. Calls `settings.update(session=session_name)` — **only for a plain run** (`_is_plain_run`: not `--bg`, not `--bg-child`, no `--role`, no `--alarm-session`, no `--return`). A delegated run is a guest: it mints its own `chat-`/`spawn-` name and writes its transcript there, but never repoints the active session (otherwise the caller's conversation — and the TUI, which reads the active session — would jump onto the subagent's file). Plain `miniouto chat` still persists the active session, with or without `--name`. Every turn instead passes `ChatOptions.session` through `ChatOverrides.session` into `runtime.session`, so it drains its own inbox and expands `{this_session}` to its own name — never the caller's — without temporarily rewriting the shared active-session setting. A delegated `--continue` run must provide `--name SESSION` (it cannot inherit the caller's active session).
3. Emits the `------{session_name}------` marker to stdout unless `--answer-only` was passed, then builds a `core.chat.ChatOptions` dataclass from the flags and dispatches to `core.chat.run_chat(opts, sink=ConsoleEventSink(quiet=answer_only or with_session))`. **Session persistence is unconditional** — `run_chat` builds its `_TurnPersister` before any branch on delegation, so every run (plain outo, a delegated `--role` run, and `--answer-only`/`-a`) writes its session file; `-a` suppresses stdout only, and the file on disk is identical either way.
4. On exception: `run_chat` itself calls `_dump_failure_diagnostics` (appends `✗ {ExceptionType}: {msg}` + the last ≤5 tool calls + a full traceback to `logs/failure-<session>.log`, and prints the same to **stderr** — the CLI leaves `quiet_diagnostics` False, so this output is unchanged) and **re-raises**. Typer prints the traceback and exits 1. A *delegated* run catches it instead — see the post-step below.
   **Ctrl-C (SIGINT) is not an exception to diagnose**: `run_chat`'s handlers catch `Exception` only, so the `KeyboardInterrupt` propagates to the plain branch of `_run_foreground`, which stamps the session's still-`running` turn `interrupted` immediately (`_stamp_interrupted` — it does not wait for a later `begin_turn`, possibly in another process, to reap the stale record), closes any open stream line, prints `------finish------` + `stopped: cancelled` (the delegated branch's own wording), and exits **130**. Quiet (`-a`) adds nothing to stdout but still stamps. A turn already finished is never re-stamped, and a run with no turn on disk invents none.
5. On success: the reply reaches stdout — as a **live token stream** when the provider streamed (verbose mode only; see [Live token streaming](#live-token-streaming)), otherwise as the plain whole answer. In verbose mode the answer is preceded by a `------finish------` marker and loop events (tool calls, intermediate responses) rendered in `orange3`; when the answer already streamed, the marker follows the stream directly (plus one newline if the stream ended mid-line) instead of the text repeating. In quiet mode only the raw answer (and the session marker, for `--with-session`) reaches stdout — no deltas, markers unchanged.


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

The OLD conversation is also closed **on disk**: `_apply_handoff` calls `_persist_handoff_marker` just before printing, appending one display turn holding a single `kind="handoff"` event (the marker text, on outo's actor line) to the OLD session's `turns`. A reader that reloads the file — the TUI after a session reload, a wrapper, plain viewing — sees the transition where it happened instead of a transcript that just stops mid-work. It is its own turn rather than an event of the last one, because replay renders a turn's events before its answer; merged, the marker would sit above the final message. The CLI owns this record (the TUI wrote its own copy until its version was retired); the event shape is unchanged, so old files replay identically.

What `_apply_handoff` moves: `opts.session` and the published run contract (re-registered under the new session). The next turn carries that session directly into runtime resolution. Only a plain run publishes the new active session for later unnamed `-c` wakeups; a delegated guest never does. The continuation message is already waiting in the new session's inbox, so the next drain turns it into the next prompt — no user action, no new command. `HANDOFF_MAX_TRANSITIONS = 5` bounds it (`summary → cont-1 → cont-2` …); past that it warns, naming the session and the summary folder, and stops. The continuation prompt is the drained inbox of the new session — a one-shot drain, rebuilt from `core.summary.build_continuation_message` (plus the run contract) if the drain came back empty.

A delegated run's **delegation contract travels**: role, style, alarm session and result file are published before the turn (`_register_run_contract`) and re-stated in the continuation message, and the post-step fires from the **final** session — the alarm's `Session:` line, the result file path and a `--bg` marker's `session` field all name the `cont-*` session, with `Continued from: <old> (summary: <dir>)` added to the alarm and as a trailing `---` section of the result markdown (which still leads with the answer). The TUI does the same thing from outside the CLI: it mounts the marker in the old conversation, persists it for replay, re-pins the new session, and the 1 s inbox poll auto-delivers the continuation.

### Model resolution

The provider/model/reasoning a call runs on is the **first match in**, per key:

1. `--pm 'PROVIDER/MODEL[:REASONING]'` — this call only
2. this session's pin, `session-config/<session>.toml` (frozen at the session's first resolution)
3. the global default — the paired `settings.provider` / `settings.model` / `settings.reasoning`, with the resolved provider record's `default_model` / `reasoning_effort` answering where nothing else names a model
4. **error** — no provider configured / no model can be inferred

An override that names a **different** provider invalidates that session's pinned model and reasoning (they belong to the pinned provider), so the global chain answers for the override's provider instead. See `docs/core.md` § `resolve_runtime_from_settings` and `docs/storage.md` § `session-config/`.

### Subagent provider/model resolution

The provider/model a **delegated run** uses with `--use-subagent-provider` resolves through `runtime._resolve_subagent`, pin-first against **this run's own** session: provider: `--pm` > the session pin's `subagent_provider` > `settings.subagent_provider` > inherit outo's; model: `--pm` > the pin's `subagent_model` > `settings.subagent_model` > the subagent provider's own `default_model` > inherit outo's (**only** when the subagent runs on outo's own provider — a subagent on another provider NEVER inherits outo's model, which is what kept an outo model edit from resetting it). Reasoning: the pin's `subagent_reasoning` > `settings.subagent_reasoning` > the subagent provider's `reasoning_effort` > modelslist's default. A provider named without a model is completed from that provider's own record, so "provider but no model" is not a state that can be stored. Without the flag the trio is inert and the run uses outo's provider, model and reasoning. The `{subagent_pm}` style token renders this same resolution as a literal `--pm` value, which is what bundled styles emit. `miniouto subagent set/clear` manages the **global half** of that trio; a TUI edit to the `subagent` chip writes both halves (see `docs/usage.md`). See `docs/core.md` § `resolve_runtime_from_settings`.

### Delegation: the post-step

A run that promises something extra is one whose `_Run.delegated` is true — i.e. it re-entered as a `--bg-child`, or `--alarm-session`, or `--return` was given. `_run_delegated` wraps `run_turn_chain` in a try, and on the way out (`_complete`, whatever the outcome) writes the answer file, delivers the alarm, prints `----- chat<id> complete ----- result: <path>|(not saved)`, and stamps the liveness marker — in a `finally`, so a raising or interrupted turn still reports. Status is `done`, or `error` (`error: {Type}: {msg}`) / `stopped` (`stopped: cancelled`); a non-`done` status exits 1. The recorded answer is the **last** turn's — the post-step fires when the run ends, after any summarization hand-offs, not when the first turn does. A hand-off may have moved the run into a **different** session, so the post-step reports the session the run finally ended in (`ChainState.session`), not the one it started in.

The brief is the **whole input**: a delegated run starts with no history, so a brief that is not self-contained (goal, paths, constraints, done-criteria) produces garbage. **One brief per invocation; there is no fan-out flag.** Parallelism = several `chat --role` commands in one Bash call, or several Bash calls.

**Result file.** Default `subagent-results/<id>-<YYYY-MM-DD-HH-MM>.md`, written atomically (tmp + `os.replace`) on completion, error, or Ctrl-C alike. It holds **only the final answer** — no meta header, no brief section, nothing but what the run produced:

```markdown
<the run's final answer>
```

With no `--return` there is no file, and the alarm carries the answer inline instead.

**Completion alarm.** Exactly one message per run is inserted into the alarm session's inbox (`core/inbox.py`), with `kind="alarm"` and `source="chat"`, whose first line is always `core.inbox.ALARM_HEADER` = `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]`. The insert **follows the receiver's summarization redirect**: `_deliver_alarm` resolves `resolve_redirect(run.alarm_session)` (chains included) and inserts into the FINAL session — an alarm pointed at a summarized session belongs to the session that continues the work — appending `session <old> was summarized → continuing in <final> (via …)` (`_redirect_notice`) when the chain is longer than one hop:

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

Shows the messages still waiting, oldest first, as one YAML document:

```yaml
session: default
messages:
- id: 20260723T103412-a1b2c3
  ts: '2026-07-23T10:34:12'
  kind: alarm
  source: spawn
  text: |
    Delegated run 'editor' (id: a1b2c3) finished (status: done).
    Session: default — /home/you/.miniouto/sessions/default.json
```

`--session` defaults to `settings.session`; an empty session is an error, never a guess. It **peeks without consuming** — the payloads are read straight out of the inbox directory and nothing is unlinked, so `list` can never deliver a message by accident. Every `_peek` field is preserved (`id`, `ts`, `kind`, `source`, `text`) and multiline bodies arrive as literal block scalars. An empty inbox is `messages: []` (exit 0), so a poll loop's emptiness test is `messages == []`, not a text match.

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

Calls `core.modelslist.list_providers()` (`GET https://modelslist.blp.sh/`). Prints one YAML document — the catalog index in machine-readable form:

```yaml
providers:
- name: Z.AI Coding Plan
  slug: zai-coding-plan
  format: anthropic
  base_url: https://api.z.ai/api/anthropic
  compatibility:
  - anthropic
  - openai
  - openai-responses
  addable: true
```

`format`/`base_url` are the selected dialect — the first `formats` entry whose `format` maps through `DIALECT_TO_FORMAT`; `typesafe` is not an LLM wire protocol and never appears. An unhostable provider has `format: ''`, `base_url: ''`, `compatibility: []`, `addable: false`. An empty result is `providers: []` (exit 0). Transport failure → red `✗ Failed to reach catalog: {exc}` on **stderr** + exit 1 (stdout stays empty).

### `provider models <provider-name>`

Positional argument; modelslist lookup is case-/whitespace-/punctuation-insensitive (`_normalize` strips spaces, `-`, `_`, `.`) and falls through slug → name → saved-endpoint match → fuzzy substring (the endpoint step outranks the fuzzy one, so a stored record is never redirected by a name that merely resembles another provider). Fetches the provider's `modelLists` entry with `source == "modelslist"` and reads its `data` array. Prints one YAML document headed by the resolved provider name:

```yaml
provider: Anthropic
models:
- id: claude-sonnet-4-5
  name: Claude Sonnet 4.5
- id: claude-opus-4-1
  name: Claude Opus 4.1
```

Empty result → yellow "No models returned for `<name>`." on **stderr** + exit 1. Transport failure → red `✗ Failed to reach catalog: {exc}` on stderr + exit 1.

### `provider add <provider-name> --api-key <key> [--default-model <id>] [--reasoning <v>]`

Catalog add. Positional `provider_name` (resolved via `core.modelslist.find_provider`), required `--api-key`, optional `--default-model` (default `""`), optional `--reasoning` (default `None`).

- If `find_provider` returns `None`: red `✗ No catalog provider matched <name>. Run 'miniouto provider providers' to see the catalog.` + exit 1.
- Calls `core.providers.add_provider_from_catalog(...)` to build a `Provider` named after the catalog `slug`, with `source="modelslist"`, the first hostable format's `api_format`/`base_url`, and that format id in `catalog_dialect`.
- If `--default-model` is empty, re-fetches the provider's model list and uses the first model id (re-invokes `add_provider_from_catalog` with that id).
- `--reasoning` is stored verbatim as `Provider.reasoning_effort`; when omitted, it is auto-filled from `core.reasoning.default_reasoning_choice(default_model, provider_name)` (best-effort, silent when modelslist has no data).
- If the provider already exists: yellow `! Provider <name> already exists; overwriting.`
- On success: `✓ Added provider <name> (<api_format>, default-model=<model or ->, reasoning=<value or ->).`

If the provider has no hostable format or no usable `slug` (`add_provider_from_catalog` raises `ValueError`): red `✗ {exc}` + exit 1.

### `provider custom add`

Manual add via a nested sub-app (`custom_app`). Flags:

```
add_custom(
    --name               TEXT  # required
    --format             TEXT  # default "openai"; one of SUPPORTED_FORMATS:
                               #   openai, openai-response, anthropic, google
    --base-url           TEXT  # default ""
    --api-key            TEXT  # default "" (omit to read from env at call time)
    --default-model      TEXT  # default "" (used when neither chat --pm nor the session pin names a model)
    --reasoning          TEXT  # default None (stored verbatim as reasoning_effort)
    --max-context-window INT   # default 0 = follow modelslist (invariant 4)
    --max-output-tokens  INT   # default 0 = follow modelslist (the 16K floor applies)
)
```

- Validates `--format` against `core.providers.SUPPORTED_FORMATS = ("openai", "openai-response", "anthropic", "google")`. Unknown → red `✗ Unknown format <fmt>. Supported: …` + exit 1.
- Calls `storage.paths.ensure_dirs()`, builds a `storage.providers.Provider(...)` (with `source="custom"`; a non-zero caps flag becomes the record's override, `0`/omitted stays `None` = follow modelslist), calls `storage.providers.upsert(provider)`.
- Prints `✓ Saved custom provider <name> (<api_format>, reasoning=<value or ->).`

### `provider list`

Lists the configured providers as one YAML document:

```yaml
providers:
- name: minimax
  source: catalog
  format: openai-response
  compatibility: openai-responses
  base_url: https://api.minimax.io/v1/responses
  default_model: MiniMax-M2.1
  reasoning: high
  default: true
- name: local
  source: custom
  format: openai
  compatibility: manual
  base_url: http://127.0.0.1:8080/v1
  default_model: qwen3-32b
  reasoning: ''
  default: false
```

`compatibility` shows the saved `catalog_dialect`, `catalog` for a catalog source with no saved dialect, or `manual` for a custom provider. `source` is `custom` or `catalog` (via `is_catalog_source`; `SOURCE_MODELSLIST` and the legacy `SOURCE_LMA` both count as catalog). `reasoning` is the stored `reasoning_effort`, `''` when unset. `default` marks the active provider per `settings.toml`. No providers configured → `providers: []` (exit 0) — the same document shape, not a warning.

### `provider remove <name>`

Calls `storage.providers.remove(name)`. Prints `✓ Removed provider <name>.` on success; red `✗ Provider <name> does not exist.` + exit 1 if not found.

### `provider default <name>`

Validates via `storage.providers.get(name)`. If `None` → red `✗ Provider <name> is not configured.` + exit 1. Otherwise calls `settings.update(provider=name)` and prints `✓ Default provider is now <name>.`.

### `provider edit <name> [flags]`

Edits a stored provider record in place — only the flags you pass are written, everything else keeps its stored value (`provider_store.get` + `dataclasses.replace` + `upsert`). Missing name → red `✗ Provider <name> is not configured.` on **stderr** + exit 1.

| Flag | Effect |
|---|---|
| `--default-model M` | Sets the endpoint's `default_model` (what a fresh install resolves) |
| `--reasoning R` | Sets `reasoning_effort`; an explicit empty string (`--reasoning ""`) clears it to auto (`None`) |
| `--max-context-window N` | Context-window override (invariant 4). **`0` clears the override** — back to "follow modelslist" |
| `--max-output-tokens N` | Output-token override (sits above modelslist and the 16K floor; `chat --max-tokens` still outranks it). **`0` clears** |
| `--base-url URL` | Sets the endpoint's base URL |
| `--api-key KEY` | Sets the API key |
| `--compatibility DIALECT` | Catalog-source records only — see below |

`--compatibility` applies to **catalog** providers only (the same flow the TUI popup's `Compatibility` row runs): it resolves the record's catalog entry (`find_provider_by_slug`), picks the named dialect out of `catalog_compatibility`, and rewrites `api_format`, `base_url` AND `catalog_dialect` from that dialect's mapping in one write. A custom record is refused with `✗ --compatibility applies to catalog providers only; <name> is custom — edit --base-url here, or re-add it with 'provider custom add --format …' for a new format.` + exit 1 (there is no `--format` flag on `edit` — a custom provider's format is set at `custom add` time); a dialect the catalog does not offer is refused with the list of what IS available; no catalog metadata for the slug is refused too.

A caps edit also drops `core.context`'s modelslist cache (`clear_model_cache`): the override is read from the record on every call, but the FALLBACK it hands back to is cached for the life of the process — without the drop this process would serve pre-edit caps until it exits.

On success: `✓ Provider <name> updated.`

---

## `miniouto style ...`

File: `cli/style.py`. Sub-app `app = typer.Typer(help="Manage agent style documents.")`.

### `style list`

Iterates `storage.styles.list_styles()` and prints one YAML document:

```yaml
styles:
- name: coding
  active: false
- name: coding-work
  active: true
- name: default
  active: false
```

`active` marks the style `settings.toml` names. An empty installation is `styles: []`.

### `style set <name> [--session NAME]`

Calls `storage.styles.read(name)`. If `None` → red `✗ ... is not installed.` + exit 1. Otherwise calls `core.runtime.set_active_style(name, session=…)`: the style becomes `settings.style` (the default for sessions started from now on) and every session that already exists FREEZES the style it is running under into its own pin (`session_config.freeze_style`) — the same rule the TUI's style picker follows, so existing conversations are never restyled underneath the user. `--session NAME` additionally pins the choice to that session (the write the TUI's picker makes for the conversation it belongs to): `✓ Active style is now <name> (pinned to session '<session>').` Without it the change is the global default only.

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

Iterates `storage.skills.list_skills()` with no argument, which searches **both** default roots (`./skills/` first, then `~/.agents/skills/`) and stamps each skill with the root it came from — **hidden skills are excluded** (the function filters `not skill.hidden`). Prints one YAML document:

```yaml
skills:
- name: hf-cli
  description: Hugging Face Hub CLI (`hf`) for downloading, uploading, and managing
  directory: /home/you/project/skills
```

Descriptions arrive **whole** — no 80-char truncation. A name installed under both roots lists twice, each row naming its own root. No skills found → `skills: []`.

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

Manages the **global default** subagent provider/model/reasoning (`settings.subagent_provider` / `settings.subagent_model` / `settings.subagent_reasoning`). A delegated run started with `chat --use-subagent-provider` resolves its subagent trio pin-first against **its own** session, so it inherits *this* trio only when its session pin has no subagent keys; empty settings = the run uses outo's provider and model, and falls back to the resolved provider's `reasoning_effort`. (The TUI's `subagent` chip writes this trio **and** the session pin at once — through `config set --target subagent`, the CLI form of the same edit.) The command does not run anything itself; it only edits `settings.toml`.

### `subagent show`

Prints one YAML document — the resolution the runtime applies, beside the raw override keys that produced it:

```yaml
provider: local
model: qwen3-32b
reasoning: high
overrides:
  provider: ''
  model: ''
  reasoning: ''
```

The top three keys are the **effective** trio `runtime._effective_subagent_pm` resolves right now, per key: `settings.subagent_*`, then (for the model) the subagent provider's own record `default_model`, then — only when the subagent inherits outo's provider — outo's own trio (`settings.provider` / `model` / `reasoning`, with the provider record's defaults answering where nothing names a model). An unresolved key stays `""`: the honest answer for a machine interface, no `-` placeholder and no display sentence. The `overrides` block echoes the stored `settings.subagent_*` keys verbatim — empty strings mean "not overridden" (the inherited case above).

### `subagent set [--provider NAME] [--model NAME] [--reasoning TEXT]`

Persists an override via `settings.update(...)`. At least one of `--provider`/`--model`/`--reasoning` is required — none given → red `✗ Pass at least one of --provider, --model, or --reasoning.` + exit 1. `--provider` is validated against the provider store: unknown name → red `✗ Provider <name> is not configured.` + exit 1. `--reasoning` persists a per-subagent reasoning effort; passing an explicit empty string (`--reasoning ""`) clears it (empty strings can't go through `update()` — the command does `save(replace(load(), subagent_reasoning=""))` directly, the same pattern as `clear`). On success prints `✓ Subagent override saved.` plus the resolved model. If only `--provider` is given and no model is resolvable (no stored `subagent_model` and the provider has no `default_model`), a yellow warning tells you to set one via `miniouto subagent set --model <name>`.

### `subagent clear`

Resets all three settings keys (`subagent_provider`, `subagent_model`, `subagent_reasoning`) to `""` so the subagent inherits outo again. Because `Settings.merge()`/`settings.update()` skip empty-string values, clearing goes through `save(replace(load(), subagent_provider="", subagent_model="", subagent_reasoning=""))` directly. Prints `✓ Subagent override cleared — subagent now inherits outo's provider/model and the provider's reasoning effort.`

---

## Catalog endpoint caching

The catalog commands (`provider providers`, `provider models`, `provider add`) and the TUI catalog flows all hit `https://modelslist.blp.sh` via `core/modelslist.py`. The root index and each provider's model list are cached for `CACHE_TTL_SECONDS = 300` (5 minutes) in `core.modelslist._CACHE`, keyed `"providers"` and `f"models:{_normalize(provider)}"`; a cached empty list is meaningful, a transport failure is never cached. `core.modelslist.clear_cache()` drops everything (used by tests). See `docs/modelslist.md` for the endpoint reference, the `data` envelope and the `DIALECT_TO_FORMAT` table.

---

## Error handling & exit codes

| Scenario | Behavior | Exit code |
|---|---|---|
| Successful command | stdout output, no error | 0 |
| Unhandled exception in `chat_cmd` / `run_chat` | `run_chat._dump_failure_diagnostics` appends `✗ {ExceptionType}: {msg}` + last ≤5 tool calls + traceback to `logs/failure-<session>.log` and prints the same to stderr, then re-raises; Typer prints the traceback | 1 |
| Ctrl-C (SIGINT) on a plain `chat` | running turn stamped `interrupted` immediately; `------finish------` + `stopped: cancelled` on stdout (nothing in quiet mode); exit **130** — a stop, not an error | 130 |
| `config set` with no flag | `✗ Pass at least one of --target, --theme, or --active-session.` + `typer.Exit(1)` | 1 |
| `config set --target` without `--session`, or without all three trio flags | `✗ --target requires --session NAME (the pin it writes).` / `✗ --target writes the whole trio: pass --provider, --model and --reasoning (an empty --reasoning clears it).` + exit 1 | 1 |
| `config set --target` naming an unconfigured provider | `✗ Provider '<name>' is not configured.` + exit 1 (on stderr — `config show`'s YAML lane stays clean) | 1 |
| `provider edit` on a missing name | `✗ Provider <name> is not configured.` on stderr + `typer.Exit(1)` | 1 |
| `provider edit --compatibility` on a custom record | `✗ --compatibility applies to catalog providers only; <name> is custom — edit --base-url here, or re-add it with 'provider custom add --format …' for a new format.` + exit 1 | 1 |
| `provider custom add` with unknown `--format` | `✗ Unknown format <fmt>. Supported: …` + `typer.Exit(1)` | 1 |
| `provider add` for an unknown catalog provider name | `✗ No catalog provider matched <name>. Run 'miniouto provider providers' to see the catalog.` + exit 1 | 1 |
| `provider add` for a provider with no hostable format | `✗ {exc}` (ValueError from `add_provider_from_catalog`) + exit 1 | 1 |
| `provider add` overwriting an existing provider | yellow `! Provider <name> already exists; overwriting.` | 0 |
| `provider providers` / `provider models` network failure | `✗ Failed to reach catalog: {exc}` on stderr (stdout empty) + exit 1 | 1 |
| `provider models` empty result | yellow "No models returned for `<name>`." on stderr + exit 1 | 1 |
| `provider providers` empty result | `providers: []` YAML document, exit 0 | 0 |
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

**Pattern**: Storage / style / skill / catalog handlers catch exceptions explicitly and exit via `typer.Exit(code=1)`, preserving tracebacks via `from exc`. `chat_cmd` does **not** catch — `run_chat` does its own diagnostics and re-raises so the caller sees a real traceback. The TUI runs its turn as a `chat` **subprocess** (see "TUI mode"): a refused child surfaces its first stderr line as a `chat: …` system row, a non-zero exit as `chat exited with <code>`, and a 130 (Ctrl-C / esc-esc) as `stopped by user (esc esc)` — the app itself never crashes on a turn.

## Output conventions

**Listing commands emit YAML.** `status`, `config show`, `provider providers/models/list`, `skill list`, `style list`, `subagent show`, and `message list` print exactly one YAML document on stdout and nothing else — no banner, no comment, no rich markup. That output is the **official machine interface**: third-party CLI wrappers (a TUI wrapper, a script) parse it with `yaml.safe_load`. All six go through one helper, `cli/_yaml.py:emit_yaml/print_yaml`, whose dump options are part of the contract — `allow_unicode=True`, `sort_keys=False` (the schema's key order stands), `default_flow_style=False` (block style), `width=100` (long text folds), and multiline strings as literal `|` block scalars. Errors of these commands go to **stderr** (rich ✗ text) with exit 1, so the stdout lane never carries anything but YAML.

**Mutation commands keep their rich text.** `✓` (green) — success; `✗` (red) — failure; `!` (yellow) — warnings (e.g. provider overwrite); `orange3` — loop events in `ConsoleEventSink` (tool calls, intermediate responses). The final answer is written as plain stdout with a `------finish------` marker (no rich color). `message insert` keeps printing the bare message id (wrappers parse it), and `message clear` keeps its confirmation wording.

## Module dependency graph (CLI layer only)

```
cli/__init__.py ──┬─→ cli/_yaml.py    ─→ yaml (the shared emitter: emit_yaml/print_yaml)
                  ├─→ cli/config.py   ─→ storage.{session_config,settings,providers}, core.runtime.default_provider_config, cli/_yaml
                  ├─→ cli/provider.py ─→ storage.paths, storage.providers, storage.settings, core.{modelslist,providers,context,reasoning}, cli/_yaml
                  ├─→ cli/style.py    ─→ storage.paths, storage.settings, storage.styles, core.runtime.set_active_style, cli/_yaml
                  ├─→ cli/skill.py    ─→ storage.skills, cli/_yaml
                  ├─→ cli/subagent.py ─→ storage.providers, storage.settings, cli/_yaml
                  ├─→ cli/message.py  ─→ core.inbox, storage.settings, cli/_yaml
                  ├─→ cli/computer.py ─→ storage.paths, tools.ascii_art
                  ├─→ cli/chat.py     ─→ core.{chat,events,inbox,spawn_state,runtime,summary}, storage.{paths,settings,styles,sessions,session_config}
                  └─→ cli/tui/ (app)  ─→ cli/tui/bridge.py (the subprocess lane: [sys.executable, -m, miniouto] + YAML parsing),
                                       display reads of core.{chat,events,inbox,modelslist}, core.providers,
                                       storage.{paths,providers,settings,styles,sessions,session_config}
```

External libs the CLI directly imports: `typer`, `yaml` (via `cli/_yaml.py` and `cli/tui/bridge.py`, which parses the YAML the CLI itself emits), `rich.console`, `rich.markdown`, `rich.text`, `rich.cells`, `rich.style`, `xnano` (+ `xnano.styles` — the canvas, styles and the placement API), `xnano_core` (`cell_grid_to_ansi`, the graphics lane's grid → ANSI writer) and `xnano_image` (`TerminalImage`, `TerminalGraphics`, `cell_grid_size` — imported lazily inside the graphics lane), `asyncio`, `threading`, `dataclasses.replace`. Note `rich` is not optional: the TUI measures with `rich.cells.cell_len`, and `render.py` (`style_from_rich`/`text_to_lines`) is the Rich → `pt` bridge — the port keeps its own bridge rather than xnano's `from_rich`.
