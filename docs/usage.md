# Usage Examples

A cookbook of real-world examples for every CLI command — flags, output shapes, and combination patterns shown as code blocks. For exact flag/behavior specs see [`cli.md`](./cli.md); for storage layout see [`storage.md`](./storage.md); for non-interactive automation patterns see [`automation.md`](./automation.md).

- [Getting started (first-time setup)](#getting-started-first-time-setup)
- [`miniouto status` — inspect current configuration](#miniouto-status--inspect-current-configuration)
- [`miniouto chat` — single-shot turn](#miniouto-chat--single-shot-turn)
  - [Basic usage](#basic-usage)
  - [Session management (`--name`, `--continue`)](#session-management---name---continue)
  - [Output modes (verbose / `--answer-only` / `--with-session`)](#output-modes-verbose---answer-only----with-session)
  - [Runtime overrides (`--pm`, `--style`)](#runtime-overrides---pm---style)
  - [Generation parameters (`--max-tokens`, `--temperature`)](#generation-parameters---max-tokens---temperature)
- [`miniouto provider` — provider management](#miniouto-provider--provider-management)
- [`miniouto style` — style management](#miniouto-style--style-management)
- [`miniouto skill` — inspecting skills](#miniouto-skill--inspecting-skills)
- [TUI mode (`miniouto` with no args)](#tui-mode-miniouto-with-no-args)
- [Pipeline / scripting recipes](#pipeline--scripting-recipes)

---

## Getting started (first-time setup)

The first `miniouto` invocation (any subcommand) runs `storage/paths.py:ensure_dirs()`, which creates the `~/.miniouto/` skeleton and seeds the five bundled styles automatically. After that you register one provider, set it as default, and you're ready to chat.

**Path A — pull from the lma catalog (recommended):**

```bash
# 1. See what's available (≈144 catalog providers)
miniouto provider providers

# 2. List models for a specific provider (name is fuzzy-matched, case/space insensitive)
miniouto provider models anthropic
miniouto provider models "open ai"

# 3. Just the API key — base_url, format, and default model are auto-filled
miniouto provider add Anthropic --api-key sk-ant-...
miniouto provider add OpenAI --api-key sk-... --default-model gpt-5.5

# 4. Set as default
miniouto provider default anthropic

# 5. Pick a style (all five bundles are already installed)
miniouto style list
miniouto style set coding

# 6. First chat
miniouto chat "hello"
```

**Path B — register a custom provider (self-hosted endpoint / proxy / local model):**

```bash
# OpenAI-compatible endpoint (vLLM, LocalAI, MiniMax, Zhipu, Moonshot, Ollama, ...)
miniouto provider custom add \
  --name my-local \
  --format openai \
  --base-url http://localhost:8000/v1 \
  --api-key dummy \
  --default-model llama-3.1-70b

# Anthropic-compatible proxy
miniouto provider custom add \
  --name anthropic-proxy \
  --format anthropic \
  --base-url https://my-proxy.example.com \
  --api-key sk-... \
  --default-model claude-sonnet-4

miniouto provider default my-local
miniouto chat "hello"
```

> The four valid `--format` values: `openai`, `openai-response`, `anthropic`, `google`. (`core/providers.py:SUPPORTED_FORMATS`)

Run `miniouto status` right after install to see the current configuration at a glance.

---

## `miniouto status` — inspect current configuration

```bash
miniouto status
```

Example output shape:

```
Default provider: anthropic
Default model:    claude-sonnet-4
Active style:     coding
Session:          default
Storage:          /home/luke/.miniouto
Providers:        anthropic, openai, my-local
Styles:           coding, coding-pro, coding-ultra, coding-work, default
Skills:           git-master, frontend
Sessions:         default, chat-20260702-103045-a1b2c3
```

Always exits 0. Handy for confirming a change took effect after switching provider/style/model.

---

## `miniouto chat` — single-shot turn

File: `cli/chat.py:chat_cmd`. Sends one prompt and prints the reply. Full flag reference is in [`cli.md`](./cli.md#flag-reference).

### Basic usage

```bash
# Simplest form — a fresh session is generated each call (chat-{YYYYMMDD-HHMMSS}-{6hex})
miniouto chat "implement quicksort in python"

# Use single quotes when the prompt needs newlines or quotes
miniouto chat 'design a database schema for these requirements: ...'
```

Default (no flags) output is the most verbose form:

```
------chat-20260702-103045-a1b2c3------       ← session marker
⠋ Working…                                     ← spinner (in flight)
outo: Bash cat > quicksort.py <<'EOF' ...        ← loop events (orange3)
outo: iter 1 · 1280 tokens
outo: Bash sed -i 's/pivot = xs\[0\]/pivot = xs[len(xs)\/\/2]/' quicksort.py
outo: let me run the tests now...
outo: Bash python -c "from quicksort import qsort; ..."
outo: tests pass. Here is the final code.
------finish------                             ← finish marker
[full final answer]                             ← plain stdout
```

### Session management (`--name`, `--continue`)

Without an explicit name a fresh one is generated each call and stored in `settings.toml`'s `session` field — but only for a **plain** chat. A delegated run (`--role`, `--alarm-session`, `--return`, `--bg`) is a guest: it writes its own session file under its own generated name and never repoints your active session. To carry a conversation across calls combine `--name` with `--continue`.

```bash
# Name the session explicitly (persisted to settings.session)
miniouto chat "gather the requirements" --name feature-x

# Continue the same session (previous history is prepended to the next prompt)
miniouto chat "based on the above, design the schema" --name feature-x --continue
miniouto chat "now write the ORM models" --name feature-x --continue

# Short form: -c
miniouto chat "next step?" -c --name feature-x

# --continue alone falls back to settings.session (or "default") when --name is omitted
miniouto chat "keep going" --continue
```

> **Note**: `chat_cmd` unconditionally calls `settings.update(session=...)` on every invocation. Every chat call overwrites the active session.

### Output modes (verbose / `--answer-only` / `--with-session`)

Output verbosity has three levels. `--answer-only` and `--with-session` are **mutually exclusive** (using both yields `✗ --answer-only and --with-session are mutually exclusive.` + exit 1).

```bash
# (1) verbose (default) — session marker + spinner + loop events + finish marker + answer
miniouto chat "suggest a refactor"

# (2) --answer-only / -a — answer body only (suppresses marker, events, spinner)
miniouto chat "respond as JSON" --answer-only > result.json
miniouto chat "one-line summary" -a

# (3) --with-session — session marker + answer only (use when you need to attribute answers)
miniouto chat "handle this" --with-session
# Output:
# ------feature-x------
# [answer body]
```

Exact differences across the three modes:

| Element | verbose (default) | `--with-session` | `--answer-only` |
|---|:---:|:---:|:---:|
| `------{session}------` marker | ✓ | ✓ | ✗ |
| Spinner | ✓ | ✗ | ✗ |
| Loop events (tool calls / intermediate responses) | ✓ | ✗ | ✗ |
| `------finish------` marker | ✓ | ✗ | ✗ |
| Final answer | ✓ | ✓ | ✓ |

> Use `--answer-only` when a script wants just the answer; use `--with-session` when you run several sessions in parallel and need to tell which answer belongs to which. Parsing recipes are in [`automation.md`](./automation.md#3-parsing-chat-output-by-mode).

### Runtime overrides (`--pm`, `--style`)

Apply a different provider/model/reasoning/style for just this call without touching the saved settings or the session's pin.

```bash
# Provider + model for this call only (both must already be registered)
miniouto chat "hello" --pm "openai/gpt-5.5"

# Reasoning effort rides along as a ':' suffix
miniouto chat "needs heavy reasoning" --pm "anthropic/claude-opus-4.5:high"

# A model name may itself contain '/', and the reasoning is split off the LAST ':'
miniouto chat "deep dive" --pm "together/meta-llama/Llama-4-70B:medium"

# Reasoning omitted (or a trailing ':') leaves it to the session pin / default
miniouto chat "just a quick classification" --pm "openai/gpt-5.5-mini:"

# Use a different style just for this call
miniouto chat "review this code" --style default
miniouto chat "orchestrate this for me" --style coding-pro

# Combinable
miniouto chat "summarize" --pm "anthropic/claude-sonnet-4.5" --style coding
```

`--pm 'PROVIDER/MODEL[:REASONING]'` is the **only** provider/model/reasoning override `chat` has; `--provider`, `--model`, `--reasoning`, `--subagent-provider` and `--subagent-model` were removed in favour of it. A missing `/`, or an empty provider or model, is rejected before any resolution, so a typo costs no model call.

Provider/model/reasoning resolution order (first match wins, per key — see [`README.md`](../README.md#model-resolution)):

1. `--pm` (this call only)
2. this session's pin, `session-config/<session>.toml` — frozen at the session's first resolution
3. the global default: `settings.provider` + the provider record's `default_model` / `reasoning_effort`
4. error — no provider configured / no model can be inferred

Pin a long-running session so a model switch in another process can't move it, and start a fresh one on something else with `--pm`.

### Generation parameters (`--max-tokens`, `--temperature`)

```bash
# Cap output tokens explicitly (default pulls the model's real cap from lma, floor 16384)
miniouto chat "keep it short" --max-tokens 256

# Sampling temperature
miniouto chat "10 creative ideas" --temperature 0.9
miniouto chat "strict facts only" --temperature 0.0

# Combinations
miniouto chat "summarize" --max-tokens 512 --temperature 0.3
```

> When `--max-tokens` is omitted, `core/context.py:get_max_output_tokens` queries the lma `/model` endpoint for the model's real cap and uses it, with a 16384-token floor (prevents Anthropic's 1024 default from silently truncating long tool calls, e.g. heredoc file writes, mid-stream). A per-provider `max_output_tokens` / `max_context_window` override is set when the provider is added (the TUI `+ add custom…` wizard asks for them) or by hand in `providers.toml`, for endpoints lma has no data on.

---

## `miniouto provider` — provider management

Three groups: catalog browse/add (`providers`, `models`, `add`), management of registered providers (`list`, `remove`, `default`), and manual config (`custom add`).

### Catalog browsing

```bash
# All catalog providers (≈144 from lma.blp.sh)
miniouto provider providers
# Columns: Name | SDK | API URL | miniouto format | Addable?
# Only Addable? = ✓ providers can be added (those with a sdk_to_format mapping)

# Models for a specific provider (fuzzy-matched name)
miniouto provider models Anthropic
miniouto provider models anthropic       # case-insensitive
miniouto provider models "open ai"       # space/hyphen-insensitive
```

### Adding from the catalog

```bash
# Default — uses the first model the catalog lists as default_model
miniouto provider add Anthropic --api-key sk-ant-...

# Explicit default model
miniouto provider add OpenAI --api-key sk-... --default-model gpt-5.5

# Same name already exists → yellow warning, overwrites in place
# ! Provider anthropic already exists; overwriting.
```

### Adding a custom provider

```bash
# All flags specified
miniouto provider custom add \
  --name openai \
  --format openai \
  --base-url https://api.openai.com/v1 \
  --api-key sk-... \
  --default-model gpt-5.5

# Omit API key → read from env at call time (coreouto provider default behavior)
miniouto provider custom add --name openai --format openai

# OpenAI Responses API
miniouto provider custom add \
  --name openai-responses \
  --format openai-response \
  --base-url https://api.openai.com/v1 \
  --default-model gpt-5.5

# Google Gemini
miniouto provider custom add \
  --name gemini \
  --format google \
  --api-key AIza... \
  --default-model gemini-2.5-pro
```

### Management

```bash
# List (active provider marked with ●)
miniouto provider list
# Columns: Name | Type(custom/catalog) | Format | Base URL | Default Model | Default

# Switch the default
miniouto provider default openai

# Remove
miniouto provider remove my-local
```

---

## `miniouto style` — style management

A style is a markdown system prompt at `~/.miniouto/style/<name>.md`. It declares one `<outo>` body plus zero or more **role** bodies — each `<name>...</name>` top-level tag is one role, and the tag name IS the role name you pass to `miniouto chat "<brief>" --style <style> --role <tag>` (e.g. `editor`, `file-picker`). A style with no named tags is still fully delegating: `Bash` is always registered, so the `chat` form works — it just has no roles to select. Format details in [`styles.md`](./styles.md).

```bash
# Five bundles are pre-installed: default, coding, coding-pro, coding-work, coding-ultra
miniouto style list
#   - coding
#   - coding-pro
#   - coding-ultra
#   - coding-work
#   - default ●          ← active style

# Switch the active style
miniouto style set coding

# Print a style's contents
miniouto style show coding

# Fetch styles from a remote repo's /style-md/ directory
# (GitHub / GitLab / HTML directory index auto-detected)
miniouto style add https://github.com/myorg/my-styles
miniouto style add https://gitlab.com/myorg/my-styles
miniouto style add https://example.com/styles/    # HTML directory index

# Override the style name (rare)
miniouto style add https://github.com/myorg/repo --name custom-prompt

# Refresh everything
# 1) Force-overwrite installed files whose names match bundled templates with latest bundled content
# 2) Re-fetch every repo URL recorded in style_repos.toml
miniouto style update
```

> `style update` always overwrites installed files whose names match a bundled template with the latest bundled content. To preserve a customized bundled style, copy it to a different name before editing.

---

## `miniouto skill` — inspecting skills

Skills live at `./skills/<name>/SKILL.md` (project-local) or `~/.agents/skills/<name>/SKILL.md` (NOT under `~/.miniouto/` — the Anthropic-compatible path); both are default roots, project-local first. The `skill` command is **read-only**; to add a skill, just create the directory and file. See [`skills.md`](./skills.md) and [`automation.md`](./automation.md#6-auto-registering-skills).

```bash
# List installed skills (hidden=true skills are excluded)
miniouto skill list
# Available Skills
# ┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
# ┃ Name        ┃ Description                          ┃
# ┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
# │ git-master  │ Git operations: atomic commits, reb… │
# │ frontend    │ Frontend design and QA skill         │
# └─────────────┴───────────────────────────────────────┘

# Show the full contents of a skill (hidden skills can still be shown by name)
miniouto skill show git-master
# Name: git-master
# Description: ...
# License: MIT
# Allowed Tools: Bash, Read, Write, Edit
#
# [full SKILL.md body]
```

Skills are listed (name + description, plus their on-disk location) wherever a style's prompt places the `{skills}` token — in the outo body and in each role body alike — on every chat call; the agent reads the full SKILL.md via Bash when a task matches. A style that names no skills token tells the model about none. See [`styles.md`](./styles.md) § Dynamic tokens.

---

## TUI mode (`miniouto` with no args)

```bash
miniouto          # no args → enters the TUI
```

A Textual-based interactive UI. Key bindings:

| Action | How |
|---|---|
| Command palette | `Ctrl+P` (new session / pick session / change model/provider/style/theme / clear log) |
| Clear log | `Ctrl+L` |
| Quit | `Ctrl+C` |
| Submit input | `Enter` |
| Cycle chip focus | `Tab` / `Shift+Tab` |
| Open a chip | click it, or focus + `Enter` |
| Close a modal | `Esc` |

The chips in the bottom panel (two left/right-anchored pairs, `outo`/`style` then `subagent`/`session`):

- **outo** — click → the provider settings popup: three rows, `Provider` / `Model` / `Reasoning`, each opening its own picker. The `Provider` row lists registered providers plus two sentinels (`+ add from catalog…`, `+ add custom…`); `Model` fetches the catalog list for a catalog provider and falls back to a free-text id for a hand-added endpoint; `Reasoning` lists the levels lma reports for the resolved model. **Picking a Provider or a Model resets Reasoning** to that model's default — an effort level belongs to a model — and Reasoning is one more click away.
- **subagent** — the same popup, editing the `subagent_provider` / `subagent_model` / `subagent_reasoning` trio; empty still reads `same as outo`, which is what a delegated run inherits.
- **style** — click → list of installed styles.
- **session** — click → the session picker, grouped by the session's creation directory, plus `+ new session…`.

Every provider/model/reasoning edit writes **two** places: this session's pin (`session-config/<session>.toml`, so the edit survives a later default change) and the global default (so other processes and the next session follow the newest choice). Per-session resolution is `overrides > session pin > global default`, and a session freezes its pin the first time it resolves — which is what lets several miniouto processes run side by side on different providers without redirecting each other.

> There is no standalone provider/model editor any more, and none that edits a provider's context/output caps: those are set when the provider is added, or by hand in `providers.toml`.

### Theme

The TUI boots on **`miniouto`**, the harness's own dark theme, comfort-tuned for long sessions: a lifted blue-black `#0a0e14` background (still darker than Textual's stock dark) with surfaces stepping up to `#1d2530`, and a **three-layer text hierarchy** so the agent's final answer is the thing your eye lands on — **answer** `#c5d0d8` at 12.33:1 on the background (painted only by the answer row, so it is the brightest layer on screen), **body** `#a9bac5` at 9.69:1 (up from a flat 8.76:1 and no longer the gray it was), and **muted** `#7b8490` at 5.11:1 for event, tool and thinking rows. The semantic colors are desaturated one notch, and a `#2fc2ff` accent is used for the focus rules, block cursors, scrollbar, splash logo and tool-row toggle glyph. Nothing to configure.

To use a different one, either pick it at runtime (`Ctrl+P` → **Theme**, which offers `miniouto` alongside the stock Textual themes and writes the choice to `settings.theme`), or set the key by hand — the next launch uses it:

```toml
# ~/.miniouto/settings.toml
theme = "gruvbox"      # any registered Textual theme name
```

A name no registered theme matches falls back to `miniouto` instead of failing the launch.

The Header shows the current working directory on the left and `miniouto v<version>` on the right (the same version also appears under the splash logo). Tool calls (Bash / Image / Video / Audio / Computer / continue_session) render as **collapsible boxes** in the chat log: each box starts as a single dimmed `▶ <summary>` header line and expands on a plain click anywhere on the box (glyph, header text, expanded body, output) or `Enter` to show the full command and, once the call finishes, its output (tool rows cap the display at ~4000 chars). The click is told apart from a text-selection by the gesture: dragging over the text selects it and never toggles the box, and double-clicking no longer selects the whole row. Errors gain a red border-left and a subtle `✗` marker on the header. Delegation is a `Bash` call to `miniouto chat "<brief>" --style <style> --role <tag>`, so a delegated run has **no per-invocation row or detail screen** — it is a separate process with its own transcript. What reaches this chat is its **completion alarm**: a user message starting with `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]` plus the result-file path under `~/.miniouto/subagent-results/`, which the model reads (e.g. with `cat`). The alarm is delivered even when outo's turn already ended — the TUI polls the session's inbox every second and auto-starts a turn whose prompt is the alarm text, so the model wakes up and continues with the result. You can post into the same channel yourself mid-turn or from a script: `miniouto message insert --session <name> "text"`. **Long turns hand off too:** there is no implicit summarization — at ~80% context a system alarm message (`[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]` + `Context budget: ...`) asks the model to write summary documents under `./.miniouto/summary/<session>/` and call `continue_session`, and the conversation then continues in a fresh `cont-*` session. In the TUI the marker `------summary → <new>------` closes the old conversation on screen, and the continuation is delivered automatically; `miniouto chat` does the same inside the same invocation. Other TUI details: the log column is capped at 150 cells (the header chips sit in two left/right-anchored pairs and render their values whole, never truncated), the TUI **pins** its session name (minted at first dispatch, or set by the session picker / "new session") so an external change to `settings.toml`'s `session` can't redirect a live conversation, and `PgUp` / `PgDn` / `Home` / `End` scroll it instantly (a jump-to-bottom button sits below).

---

## Pipeline / scripting recipes

`--answer-only` / `--with-session` unlock clean UNIX pipelines.

```bash
# Save the answer to a file
miniouto chat "summarize this code report" -a > summary.txt

# Receive JSON and parse with jq (steer the style to emit JSON)
miniouto chat "serialize this as JSON: ..." -a | jq '.items[0].name'

# Run several sessions in parallel, distinguishable in the output
for task in summarize translate classify; do
  miniouto chat "$task this" --name "$task" --with-session >> results.txt &
done
wait

# Multi-step workflow continuing a session
miniouto chat "step 1: analyze" --name pipeline -a > step1.txt
miniouto chat "step 2: design from step 1" --name pipeline -c -a > step2.txt
miniouto chat "step 3: code from step 2" --name pipeline -c -a > step3.txt
```

## Waking a session from its inbox

A `chat` run **never waits**: it answers its prompt, prints one `------finish------`, and exits, even with background runs of its own still going. Messages that arrive afterwards wait in `~/.miniouto/inbox/<session>/` for the next **wakeup**, and there are exactly two of them: the TUI's 1 s poll, and the explicit CLI one:

```bash
# Anything can insert, from any process
miniouto message insert --session pipeline "the deploy finished"

# Wake the session: the queued texts become the turn's prompt.
# -c is mandatory (history is never loaded implicitly), and no prompt may be given.
miniouto chat --alarms -c --name pipeline

# Empty inbox → one line, exit 0, no turn at all
$ miniouto chat --alarms -c --name pipeline
no pending messages in session 'pipeline'.
```

That makes an alarm a **wakeup**, not a wait: the producing side never needs a live process to answer, and monitoring is a matter of peeking (`miniouto message list --session <s>`) and polling the `spawn-logs/<id>.json` liveness markers. The insert / observe / wake recipes, the polling loops, and how to pull a transcript out of a session file are in [`automation.md`](./automation.md) § 3 and § 4.

Non-interactive automation (CI, Docker, cron, Python API embedding) is covered in [`automation.md`](./automation.md).
