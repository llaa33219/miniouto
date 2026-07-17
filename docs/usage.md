# Usage Examples

A cookbook of real-world examples for every CLI command — flags, output shapes, and combination patterns shown as code blocks. For exact flag/behavior specs see [`cli.md`](./cli.md); for storage layout see [`storage.md`](./storage.md); for non-interactive automation patterns see [`automation.md`](./automation.md).

- [Getting started (first-time setup)](#getting-started-first-time-setup)
- [`miniouto status` — inspect current configuration](#miniouto-status--inspect-current-configuration)
- [`miniouto chat` — single-shot turn](#miniouto-chat--single-shot-turn)
  - [Basic usage](#basic-usage)
  - [Session management (`--name`, `--continue`)](#session-management---name---continue)
  - [Output modes (verbose / `--answer-only` / `--with-session`)](#output-modes-verbose---answer-only----with-session)
  - [Runtime overrides (`--provider`, `--model`, `--style`)](#runtime-overrides---provider---model---style)
  - [Generation parameters (`--max-tokens`, `--temperature`)](#generation-parameters---max-tokens---temperature)
- [`miniouto provider` — provider management](#miniouto-provider--provider-management)
- [`miniouto style` — style management](#miniouto-style--style-management)
- [`miniouto skill` — inspecting skills](#miniouto-skill--inspecting-skills)
- [TUI mode (`miniouto` with no args)](#tui-mode-miniouto-with-no-args)
- [Pipeline / scripting recipes](#pipeline--scripting-recipes)

---

## Getting started (first-time setup)

The first `miniouto` invocation (any subcommand) runs `storage/paths.py:ensure_dirs()`, which creates the `~/.miniouto/` skeleton and seeds the six bundled styles automatically. After that you register one provider, set it as default, and you're ready to chat.

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

# 5. Pick a style (all six bundles are already installed)
miniouto style list
miniouto style set claude

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
Active style:     claude
Session:          default
Storage:          /home/luke/.miniouto
Providers:        anthropic, openai, my-local
Styles:           claude, codebuff, codex, default, oh-my-opencode, opencode
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

Without an explicit name a fresh one is generated each call and stored in `settings.toml`'s `session` field. To carry a conversation across calls combine `--name` with `--continue`.

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

### Runtime overrides (`--provider`, `--model`, `--style`)

Apply a different provider/model/style for just this call without touching the saved settings.

```bash
# Use a different model just for this call
miniouto chat "needs heavy reasoning" --model claude-opus-4
miniouto chat "just a quick classification" --model gpt-5.5-mini

# Use a different provider just for this call (must already be registered)
miniouto chat "hello" --provider openai

# Use a different style just for this call
miniouto chat "review this code" --style codex
miniouto chat "orchestrate this for me" --style oh-my-opencode

# Combinable
miniouto chat "summarize" --provider anthropic --model claude-sonnet-4 --style claude
```

Model resolution order (first match wins, see [`README.md`](../README.md#model-resolution)):

1. `chat --model <name>` (this call only)
2. `settings.model` (the TUI model picker clears this — effectively legacy)
3. `provider.default_model`
4. error — no model can be inferred

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

> When `--max-tokens` is omitted, `core/context.py:get_max_output_tokens` queries the lma `/model` endpoint for the model's real cap and uses it, with a 16384-token floor (prevents Anthropic's 1024 default from silently truncating long tool calls, e.g. heredoc file writes, mid-stream). A per-provider override can be set in the TUI custom-model editor for providers lma has no data on.

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

A style is a markdown system prompt at `~/.miniouto/style/<name>.md`. The `<outo>...</outo>` and (optional) `<subagent>...</subagent>` tags split the two agents' prompts. Format details in [`styles.md`](./styles.md).

```bash
# Six bundles are pre-installed: default, claude, codex, opencode, oh-my-opencode, codebuff
miniouto style list
#   - claude
#   - codebuff
#   - codex
#   - default ●          ← active style
#   - oh-my-opencode
#   - opencode

# Switch the active style
miniouto style set claude

# Print a style's contents
miniouto style show claude

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

Skills live at `~/.agents/skills/<name>/SKILL.md` (NOT under `~/.miniouto/` — the Anthropic-compatible path). The `skill` command is **read-only**; to add a skill, just create the directory and file. See [`skills.md`](./skills.md) and [`automation.md`](./automation.md#6-auto-registering-skills).

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

Skills are automatically prepended to both the outo and subagent system prompts on every chat call (`core/runtime.py:_load_active_skills`).

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

The three chips in the bottom panel:

- **model** — click → if the provider is catalog (`source == "lma"`) a model-list selection modal opens; if custom, a free-text input modal. Saving updates `provider.default_model` and clears any legacy `settings.model` override.
- **provider** — click → list of registered providers plus two sentinels: `+ add from catalog…` and `+ add custom…`.
- **style** — click → list of installed styles.

> There is no session chip in the TUI. Change sessions via `Ctrl+P` → "Pick session" / "New session".

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

Non-interactive automation (CI, Docker, cron, Python API embedding) is covered in [`automation.md`](./automation.md).
