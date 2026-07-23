# Automation

A reference for setting up and driving miniouto in non-interactive environments (CI, containers, cron, pipelines, Python scripts). Every example reflects the actual behavior of the current source code.

- [1. Post-install auto-setup](#1-post-install-auto-setup)
- [2. Relocating storage with `MINIOUTO_HOME`](#2-relocating-storage-with-miniouto_home)
- [3. Parsing chat output by mode](#3-parsing-chat-output-by-mode)
- [4. Auto-registering providers](#4-auto-registering-providers)
- [5. Auto-registering styles](#5-auto-registering-styles)
- [6. Auto-registering skills](#6-auto-registering-skills)
- [7. Session automation](#7-session-automation)
- [8. Non-interactive automation patterns (CI / Docker / cron)](#8-non-interactive-automation-patterns-ci--docker--cron)
- [9. lma cache control](#9-lma-cache-control)

---

## 1. Post-install auto-setup

miniouto needs **no init/setup command after install**. The first `miniouto` invocation (any subcommand) runs `storage/paths.py:ensure_dirs()`, which creates the directory skeleton and seeds the bundled styles.

### What triggers `ensure_dirs()`

`ensure_dirs()` is called from several entry points:

| Call site | Trigger |
|---|---|
| `cli/__init__.py:_root` callback | **Every** `miniouto <subcommand>` run (root callback always runs before the subcommand) |
| `storage/settings.py:load` / `save` | Any read/write of `settings.toml` |
| `storage/providers.py:load_all` | Any read of `providers.toml` |
| `storage/styles.py` (most functions) | Any style read/write |
| `cli/provider.py:add_custom` etc. | Before adding a custom provider |

In other words, a single `miniouto status` creates the entire `~/.miniouto/` structure.

### Auto-created structure

```
~/.miniouto/
├── providers.toml         ← created on first provider registration
├── settings.toml          ← created on first settings change
├── style/                 ← ensure_dirs() auto-copies the six bundles
│   ├── default.md
│   ├── claude.md
│   ├── codex.md
│   ├── opencode.md
│   ├── oh-my-opencode.md
│   └── codebuff.md
├── style_repos.toml       ← created on first `style add` (absent initially)
├── sessions/              ← empty directory
└── logs/                  ← empty directory (currently unused, reserved)
```

### Bundled-style force-refresh mechanism

`ensure_dirs()` **force-refreshes bundled styles** (`storage/paths.py`):

```python
# storage/paths.py:ensure_dirs() core logic
if BUNDLED_STYLE_DIR.is_dir():
    for src in BUNDLED_STYLE_DIR.glob("*.md"):
        target = STYLE_DIR / src.name
        bundled_text = src.read_text(encoding="utf-8")
        if not target.exists() or target.read_text(encoding="utf-8") != bundled_text:
            target.write_text(bundled_text, encoding="utf-8")
```

Behavior summary:

- If an installed file **shares a name** with one of the six bundled `.md` files and the contents differ → overwrite with the bundled content.
- User-created styles whose names **don't match** a bundle (`my-custom.md`, etc.) are left untouched.
- After a miniouto version upgrade, the next run automatically refreshes the bundled styles to the latest.

> To preserve a customized bundled style, **copy it to a different name** before editing. Example: `cp ~/.miniouto/style/claude.md ~/.miniouto/style/my-claude.md`.

---

## 2. Relocating storage with `MINIOUTO_HOME`

The storage root is controlled by a single env var, `MINIOUTO_HOME`. The only definition site is `storage/paths.py:ROOT`:

```python
ROOT = Path(os.environ.get("MINIOUTO_HOME") or Path.home() / ".miniouto").expanduser()
```

If `MINIOUTO_HOME` is set, that path is used; otherwise `~/.miniouto/`. `.expanduser()` is applied, so `~` expansion is supported.

### Examples

```bash
# 1. Test / scratch environment (avoids polluting main config)
export MINIOUTO_HOME=/tmp/miniouto-test
miniouto status                                  # creates /tmp/miniouto-test/ structure
miniouto provider custom add --name openai --format openai --api-key sk-...
miniouto chat "hello"
unset MINIOUTO_HOME

# 2. Per-project profile separation
MINIOUTO_HOME=~/work/.miniouto-work miniouto chat "..."
MINIOUTO_HOME=~/personal/.miniouto-me miniouto chat "..."

# 3. Docker / container with a volume mount point
docker run -e MINIOUTO_HOME=/data/miniouto -v miniouto-data:/data ...
```

### Caveats

- `MINIOUTO_HOME` is evaluated **once, at process start (import time)**. Changing it mid-process does not affect `paths.ROOT`.
- Skills are **not** affected by `MINIOUTO_HOME`. Skills are always read from `~/.agents/skills/` (an independent Anthropic-compatible path). There is no official option to relocate skills.
- Pointing at an already-populated directory reuses the existing files. Pointing at an empty directory causes `ensure_dirs()` to build the skeleton fresh.

---

## 3. Parsing chat output by mode

`miniouto chat` offers three output modes. Automation should use **`--answer-only`** or **`--with-session`** to keep parsing simple. (Usage details in [`usage.md`](./usage.md#output-modes-verbose---answer-only----with-session).)

### stdout composition per mode

| Mode | stdout contents |
|---|---|
| verbose (default) | `------{session}------\n` + spinner/loop events (rich console) + `------finish------\n` + answer |
| `--with-session` | `------{session}------\n` + answer |
| `--answer-only` / `-a` | **answer body only** |

> The spinner and loop events go to stdout via rich console, but in non-verbose modes `ConsoleEventSink(quiet=True)` suppresses all of them. So `--answer-only` / `--with-session` leave only the answer and (optionally) the session marker on stdout.

> Failure diagnostics (`✗ {ExceptionType}`, last tool calls, traceback) always go to **stderr** (`core/chat.py:_dump_failure_diagnostics`, `_fail_console = Console(stderr=True)`). They never pollute stdout parsing.

### Exit codes

| Situation | Exit code |
|---|:---:|
| Normal completion | `0` |
| Exception inside `run_chat` (provider error, tool failure, etc.) | `1` (traceback to stderr, then re-raised → Typer exits 1) |
| `--answer-only` and `--with-session` used together | `1` |
| Typer argument parsing error | `2` |

### Parsing examples

**Bash — answer only:**

```bash
answer=$(miniouto chat "respond as JSON" -a)
echo "$answer" | jq '.'
```

**Bash — split session and answer with `--with-session`:**

```bash
output=$(miniouto chat "handle this" --with-session)
session=$(echo "$output" | head -n1 | sed 's/^------\(.*\)------$/\1/')
answer=$(echo "$output" | tail -n +2)
echo "session=$session"
echo "answer=$answer"
```

**Python — via subprocess:**

```python
import subprocess, json

def ask(prompt: str, *, session: str | None = None, continue_session: bool = False) -> str:
    cmd = ["miniouto", "chat", prompt, "--answer-only"]
    if session:
        cmd += ["--name", session]
    if continue_session:
        cmd += ["--continue"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"miniouto failed (exit {result.returncode}):\n{result.stderr}")
    return result.stdout

# One-shot
print(ask("what is 1 plus 1?"))

# Continue a session
ask("gather the requirements", session="feature-x")
plan = ask("design based on the above", session="feature-x", continue_session=True)
```

**Python — call the library directly (no CLI overhead):**

Importing `core.chat.run_chat` skips the CLI and returns the answer as a `str`.

```python
from miniouto.core.chat import ChatOptions, run_chat
from miniouto.core.events import NullSink  # suppresses all output

opts = ChatOptions(
    prompt="respond as JSON",
    session="my-batch",
    provider="anthropic",         # override (falls back to settings.provider)
    model="claude-sonnet-4",      # override
    max_tokens=2048,
    temperature=0.0,
    continue_session=False,
)
answer: str = run_chat(opts, sink=NullSink())   # return value = final answer string
data = json.loads(answer)
```

> `run_chat` always returns the final answer string. Pass `NullSink` to suppress loop events, spinner, and markers. The user message and assistant reply are automatically appended to `~/.miniouto/sessions/<session>.json`.

---

## 4. Auto-registering providers

Three ways to set up a provider non-interactively. Pick based on context.

### Method A — write the TOML file directly (simplest, no dependencies)

Write TOML tables directly into `~/.miniouto/providers.toml`. The schema matches the `storage/providers.py:Provider` dataclass:

```toml
# ~/.miniouto/providers.toml

[anthropic]
api_format = "anthropic"
base_url = ""                  # empty → use the provider SDK's default endpoint
api_key = "sk-ant-..."         # empty → read from env at call time
default_model = "claude-sonnet-4"
source = "lma"                 # "lma" (catalog) or "custom"

[my-local-vllm]
api_format = "openai"
base_url = "http://localhost:8000/v1"
api_key = "dummy"
default_model = "llama-3.1-70b"
source = "custom"

[openai-proxy]
api_format = "openai-response"
base_url = "https://my-proxy.example.com/v1"
api_key = ""                   # uses OPENAI_API_KEY env at call time
default_model = "gpt-5.5"
source = "custom"
```

Required field: `api_format` (one of `openai`, `openai-response`, `anthropic`, `google`). The rest are optional and default to `""` or `"custom"`.

Set the active provider separately in `settings.toml`:

```toml
# ~/.miniouto/settings.toml
provider = "anthropic"
style = "claude"
session = "default"
```

> Writing the files directly means the configuration is complete without ever running miniouto. The next `miniouto chat` reads it as-is. Note that `ensure_dirs()` must run at least once so the directory exists — a single `miniouto status` is enough.

### Method B — CLI commands (`provider add` / `provider custom add`)

Invoke the CLI from a script. Catalog add auto-fills base_url and format from lma:

```bash
# From the catalog (lma lookup auto-infers base_url and format)
miniouto provider add Anthropic --api-key "$ANTHROPIC_API_KEY"
miniouto provider add OpenAI --api-key "$OPENAI_API_KEY" --default-model gpt-5.5

# Manual config
miniouto provider custom add \
  --name my-local \
  --format openai \
  --base-url http://localhost:8000/v1 \
  --api-key dummy \
  --default-model llama-3.1-70b

# Set default
miniouto provider default anthropic
```

> The CLI is **not** idempotent: repeating `provider add` with the same name produces a yellow warning and **overwrites**. For scripts that need idempotency, Method A (direct TOML) is safer.

### Method C — Python API (inside a program)

```python
from miniouto.storage import providers as provider_store
from miniouto.storage import settings as settings_store
from miniouto.core.providers import add_provider_from_lma

# Build from catalog metadata (includes sdk→format mapping)
p = add_provider_from_lma(
    name="anthropic",
    api_key="sk-ant-...",
    sdk="anthropic",
    api=None,
    default_model="claude-sonnet-4",
)
provider_store.upsert(p)

# Or construct a Provider object directly
p = provider_store.Provider(
    name="my-local",
    api_format="openai",
    base_url="http://localhost:8000/v1",
    api_key="dummy",
    default_model="llama-3.1-70b",
    source="custom",
)
provider_store.upsert(p)

# Set as active
settings_store.update(provider="my-local")
```

### API key management (security)

Leaving `api_key` as `""` makes miniouto/coreouto follow the provider SDK's default behavior, which typically reads from an environment variable:

| Format | Default env var |
|---|---|
| `openai` / `openai-response` | `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `google` | `GOOGLE_API_KEY` (or `GEMINI_API_KEY`) |

For CI / containers, the recommended pattern is to leave `api_key` empty in `providers.toml` and inject the secret via env:

```yaml
# GitHub Actions example
env:
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  MINIOUTO_HOME: /tmp/miniouto-ci
steps:
  - run: |
      mkdir -p /tmp/miniouto-ci
      cat > /tmp/miniouto-ci/providers.toml <<'EOF'
      [anthropic]
      api_format = "anthropic"
      api_key = ""
      default_model = "claude-sonnet-4"
      source = "custom"
      EOF
      cat > /tmp/miniouto-ci/settings.toml <<'EOF'
      provider = "anthropic"
      style = "default"
      EOF
  - run: miniouto chat "summarize the PR" -a
```

---

## 5. Auto-registering styles

A style is a file at `~/.miniouto/style/<name>.md`. Three registration methods.

### Method A — write the file directly

```bash
mkdir -p ~/.miniouto/style
cat > ~/.miniouto/style/my-prompt.md <<'EOF'
<outo>
You are a code review expert. ...
</outo>

<subagent>
You are the subagent. Perform the brief concisely and accurately.
</subagent>
EOF
```

The `<outo>...</outo>` tag is required (without it, the entire file is used as the outo prompt). `<subagent>...</subagent>` is optional — when absent, the minimal built-in prompt from `core/runtime.py:_fallback_style("subagent")` is used. Format details in [`styles.md`](./styles.md).

To activate:

```bash
miniouto style set my-prompt
# or write style = "my-prompt" directly into settings.toml
```

> Warning: if the file name matches one of the six bundles (default, claude, codex, opencode, oh-my-opencode, codebuff), the next `ensure_dirs()` call **overwrites** it with the bundled content. Custom styles must use a different name.

### Method B — fetch from a remote repo (`style add`)

Pulls every `.md` in a git repo's `/style-md/` directory. GitHub, GitLab, and HTML directory indexes are auto-detected (`storage/styles.py:_fetch_dir`):

```bash
# GitHub
miniouto style add https://github.com/myorg/my-styles

# GitLab
miniouto style add https://gitlab.com/myorg/my-styles

# raw directory index (nginx autoindex, etc.)
miniouto style add https://example.com/styles/
```

On success the repo URL is recorded in `~/.miniouto/style_repos.toml` so later `style update` calls can re-fetch it:

```toml
# ~/.miniouto/style_repos.toml (auto-generated)
repos = ["https://github.com/myorg/my-styles"]
```

### Method C — refresh everything (`style update`)

```bash
miniouto style update
```

Performs two steps:

1. **Force re-seed bundled styles** — copies `default_style/*.md` from the package into `~/.miniouto/style/` (overwrites any installed file with a matching name).
2. **Re-fetch recorded repos** — iterates every URL in `style_repos.toml` and re-runs `add_from_repo`. A per-repo failure skips just that repo and continues with the rest (exit code 0).

Running this from cron or a batch script automates bundle upgrades and repo refresh.

### Changing the active style (direct TOML)

Change the `style` field in `settings.toml`:

```toml
style = "my-prompt"
```

Or via CLI:

```bash
miniouto style set my-prompt
```

---

## 6. Auto-registering skills

Skills live at `~/.agents/skills/<name>/SKILL.md` (NOT under `~/.miniouto/` — the **Anthropic-compatible path**). Changing `MINIOUTO_HOME` does not move skills.

### Directory layout

```
~/.agents/skills/
└── my-skill/
    └── SKILL.md       ← YAML frontmatter + markdown body
```

Minimal `SKILL.md` example:

```markdown
---
name: my-skill
description: Auto-run the project's linter/formatter
license: MIT
allowed_tools: Bash, Read, Write
hidden: false
---

This skill auto-detects and runs the project's linters/formatters.

## Supported formatters
- Python: ruff, black
- JS/TS: prettier, eslint

## Usage
When the user says "format this" ...
```

### Frontmatter fields

| Field | Required | Description |
|---|:---:|---|
| `name` | ✓ | Skill identifier (used by `skill show <name>`) |
| `description` | ✓ | One-line description (shown in `skill list`) |
| `license` | | License string (shown only in `skill show`) |
| `allowed_tools` | | Comma-separated tool list (informational, not enforced at runtime) |
| `hidden` | | `true` hides it from `skill list` (still queryable via `skill show <name>`) |

### Auto-detection

**Just create the directory and file — the next chat call picks it up automatically.** There is no registration command. `core/runtime.py:_load_active_skills` scans `~/.agents/skills/` on every `build_runtime()` call and joins the body of every visible skill with `---` separators, prepended to both the outo and subagent system prompts.

### Auto-registration script example

```bash
# Ensure the skills directory exists
mkdir -p ~/.agents/skills

# Distribute a skill (git clone, tar, cp, ...)
git clone https://github.com/myorg/my-skill ~/.agents/skills/my-skill

# Or create one from a file
mkdir -p ~/.agents/skills/project-conventions
cat > ~/.agents/skills/project-conventions/SKILL.md <<'EOF'
---
name: project-conventions
description: Coding conventions for this project
---
1. Functions under 50 lines
2. Every public API has a docstring
3. ...
EOF

# Verify
miniouto skill list
miniouto skill show project-conventions
```

> Because there's no registration command, **removal is just deleting the directory**: `rm -rf ~/.agents/skills/my-skill`. Once the directory is gone, the skill automatically drops out of the prompt on the next chat call.

---

## 7. Session automation

A session is a file at `~/.miniouto/sessions/<name>.json` — a schema-v2 envelope with two sections: `history` (restorable model context: raw coreouto `Message` dicts, system messages excluded, full loop transcript including tool calls/results) and `turns` (display log: user/assistant text + `LoopEvent` dicts including thinking). See `docs/storage.md` § `sessions/<name>.json` for the full schema.

### Continuing a session programmatically

`--continue` (`-c`) prepends a session's existing `history` to the next prompt. This is the backbone of multi-step workflow automation:

```bash
SESSION="refactor-$(date +%Y%m%d)"

# Step 1
miniouto chat "analyze this file: $FILE" --name "$SESSION" -a > /tmp/step1.txt

# Step 2 (step 1's analysis kept as context)
miniouto chat "based on the analysis, plan the refactor" --name "$SESSION" -c -a > /tmp/step2.txt

# Step 3
miniouto chat "execute the plan" --name "$SESSION" -c
```

### Manipulating session files directly

The `history` section is a plain list of coreouto `Message` dicts, so you can read and write it directly:

```python
import json
from pathlib import Path

session_path = Path.home() / ".miniouto/sessions/my-session.json"
data = json.loads(session_path.read_text())

# Extract all user messages
user_msgs = [m["content"] for m in data["history"] if m["role"] == "user"]

# Inject pre-existing history (e.g. few-shot examples)
data["history"].insert(0, {"role": "user", "content": "system context: ..."})
data["history"].insert(1, {"role": "assistant", "content": "understood."})
session_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
```

> Note: when loading such a hand-edited file via `--continue`, each `history` entry must be a valid coreouto `Message` dict (`role`, `content`, optional `tool_calls` / `tool_call_id` / `name`) — `core/chat.py:_load_coreouto_history` runs every entry through `co.Message.model_validate` (invalid entries degrade to plain text messages instead of failing). Do not add a `role: "system"` entry — coreouto prepends a fresh system prompt on every call. Editing `turns` is optional; it only affects what the TUI displays.

---

## 8. Non-interactive automation patterns (CI / Docker / cron)

### Docker

```dockerfile
FROM python:3.12-slim

# Install miniouto
RUN pip install miniouto

# Pin the storage root inside the container
ENV MINIOUTO_HOME=/data/miniouto

# Copy config (api_key comes from env)
COPY providers.toml settings.toml /data/miniouto/

VOLUME ["/data/miniouto/sessions"]

ENTRYPOINT ["miniouto"]
CMD ["chat", "hello"]
```

```bash
docker build -t miniouto .
docker run --rm \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  miniouto chat "summarize the PR" -a
```

### GitHub Actions

```yaml
name: ai-review
on: [pull_request]
jobs:
  review:
    runs-on: ubuntu-latest
    env:
      MINIOUTO_HOME: /tmp/miniouto
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    steps:
      - uses: actions/checkout@v4
      - run: pip install miniouto
      - name: Configure miniouto
        run: |
          mkdir -p $MINIOUTO_HOME
          cat > $MINIOUTO_HOME/providers.toml <<'EOF'
          [anthropic]
          api_format = "anthropic"
          api_key = ""
          default_model = "claude-sonnet-4"
          source = "custom"
          EOF
          echo 'provider = "anthropic"' > $MINIOUTO_HOME/settings.toml
          echo 'style = "default"' >> $MINIOUTO_HOME/settings.toml
      - name: Initialize (ensure_dirs + seed styles)
        run: miniouto status
      - name: Run review
        run: |
          miniouto chat "review this PR's diff: $(git diff main...HEAD)" -a > review.md
      - uses: actions/upload-artifact@v4
        with:
          name: review
          path: review.md
```

### cron (scheduled batch)

```cron
# Nightly log summary at 23:00
0 23 * * * MINIOUTO_HOME=/srv/miniouto /usr/local/bin/miniouto chat \
    "$(cat /var/log/app/*.log | tail -1000) summarize today's logs" \
    --name daily-summary-$(date +\%Y\%m\%d) -a \
    >> /var/log/summaries.log 2>&1
```

### Embedding as a Python library (no CLI subprocess overhead)

```python
from miniouto.core.chat import ChatOptions, run_chat
from miniouto.core.events import NullSink

def batch_process(items: list[str]) -> list[str]:
    results = []
    for item in items:
        opts = ChatOptions(
            prompt=f"classify this item: {item}",
            session="batch-classify",
            continue_session=False,        # each item independent
            max_tokens=64,
            temperature=0.0,
        )
        answer = run_chat(opts, sink=NullSink())
        results.append(answer.strip())
    return results
```

---

## 9. lma cache control

The catalog commands (`provider providers`, `provider models`, `provider add`) and context-window calculation (`get_max_output_tokens`) call `https://lma.blp.sh` via `core/lma.py`. Responses are cached in `core/lma.py:_CACHE` for 10 minutes (matching lma's server-side TTL).

### Cache characteristics

- **TTL**: `CACHE_TTL_SECONDS = 600` (10 minutes)
- **Keys**: `"providers"`, `f"models:{provider.lower()}"`, `f"model:{provider.lower()}:{model.lower()}"`
- **A cached `None` is meaningful**: when lma returns 404, the fact that "this model/provider is not in lma" is cached for 10 minutes to prevent redundant requests.
- **Transport errors are not cached**: a transient `httpx.HTTPError` propagates without calling `_cache_set`, so a momentary network blip doesn't pollute the cache for 10 minutes.

### Cache invalidation

There is **no CLI command** to clear the cache. It's only possible from Python:

```python
from miniouto.core import lma
lma.clear_cache()    # empties the entire _CACHE dict
```

In CI / batch jobs that need fresh catalog data, simply start a new process (the cache is in-memory only, not on disk). To force-refresh within a single Python process, call `lma.clear_cache()` then re-query.

> If catalog command results look frozen for 10 minutes, it's the cache. For fresh data, run in a new process or call `lma.clear_cache()`.
