# Automation

A reference for setting up and driving miniouto in non-interactive environments (CI, containers, cron, pipelines, Python scripts). Every example reflects the actual behavior of the current source code.

- [1. Post-install auto-setup](#1-post-install-auto-setup)
- [2. Relocating storage with `MINIOUTO_HOME`](#2-relocating-storage-with-miniouto_home)
- [3. Alarm monitoring & wakeup](#3-alarm-monitoring--wakeup)
- [4. Extracting history from session files](#4-extracting-history-from-session-files)
- [5. Parsing chat output by mode](#5-parsing-chat-output-by-mode)
- [6. Auto-registering providers](#6-auto-registering-providers)
- [7. Auto-registering styles](#7-auto-registering-styles)
- [8. Auto-registering skills](#8-auto-registering-skills)
- [9. Session automation](#9-session-automation)
- [10. Non-interactive automation patterns (CI / Docker / cron)](#10-non-interactive-automation-patterns-ci--docker--cron)
- [11. lma cache control](#11-lma-cache-control)

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
├── settings.toml          ← created on first settings change (the GLOBAL DEFAULT provider)
├── session-config/        ← per-session pinned provider trio (one <session>.toml each)
├── style/                 ← ensure_dirs() auto-copies the five bundles
│   ├── default.md
│   ├── coding.md
│   ├── coding-pro.md
│   ├── coding-work.md
│   └── coding-ultra.md
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

- If an installed file **shares a name** with one of the three bundled `.md` files and the contents differ → overwrite with the bundled content.
- User-created styles whose names **don't match** a bundle (`my-custom.md`, etc.) are left untouched.
- After a miniouto version upgrade, the next run automatically refreshes the bundled styles to the latest.

> To preserve a customized bundled style, **copy it to a different name** before editing. Example: `cp ~/.miniouto/style/coding.md ~/.miniouto/style/my-coding.md`.

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
- Skills are **not** affected by `MINIOUTO_HOME`. Skills are always read from the two default roots — the invocation directory's `./skills/` and `~/.agents/skills/` (an independent Anthropic-compatible path). There is no official option to relocate them; a style can point `{skills:…}` at other directories.
- Pointing at an already-populated directory reuses the existing files. Pointing at an empty directory causes `ensure_dirs()` to build the skeleton fresh.

---

## 3. Alarm monitoring & wakeup

**The model: alarms are wakeups, not waits.** A message in a session's inbox is a wakeup for whoever is awake. `miniouto chat "prompt"` runs its turn, prints its answer behind exactly one `------finish------`, and exits immediately. It does **not** wait for background runs it started, and it does **not** wait for further messages to arrive. Anything that lands afterwards stays in `~/.miniouto/inbox/<session>/` until the next wakeup.

> Waiting could only ever have been satisfied by the markers this one run happened to start. Any producer anywhere can `miniouto message insert` into any inbox, so a closed wait is the wrong shape. The old follow-up chain (its turn/wall/poll bounds and its banner) is gone.

There are exactly three primitives, and nothing is locked in. Any tool that can run a shell can take part.

### INSERT — from anywhere

```bash
miniouto message insert --session <session> "text"
```

- `--session` is **required** (`miniouto message insert/list/clear`).
- Words are joined with single spaces, so no shell quoting gymnastics. `--stdin` reads the body from standard input instead.
- `--kind user` (default) or `--kind alarm`; `--source <label>` is a free-form origin tag stored with the message (`cli`, `tui`, `chat`, `summary`, `context`, …).
- The command prints the message id (the filename stem) and exits 0.

```bash
# a script, a cron job, another agent, a webhook handler — all identical
miniouto message insert --session mysession "the deploy finished: $(cat /tmp/status)"
git log --oneline -1 | miniouto message insert --session mysession --source ci
curl -s https://hooks.example/notify | miniouto message insert --session mysession --stdin
```

### OBSERVE — non-destructive

```bash
miniouto message list --session <session>          # peek; consumes nothing
ls ~/.miniouto/inbox/<session>/                    # the same queue as plain files
[ -n "$(miniouto message list --session <session>)" ] && echo busy   # a poll loop's condition
```

`message list` prints one line per queued message, oldest first: `<ts>  <kind>  <source>  <first 60 chars>`, and it **never unlinks anything** (`drain` is the delivery primitive, and only a running turn calls it). So it is safe to poll in a tight loop. `--session` defaults to `settings.session`; an empty session is an error, never a guess.

> An empty inbox prints **nothing at all** and exits 0, so `message list` doubles as a loop's emptiness test — no placeholder line, no text matching.

> A file that will not parse (truncated write, a stray editor save) is renamed `<stem>.bad` by the next drain and skipped, never raised on. `*.json` is the right glob for a loop: a leftover `.bad` file is not a message, and counting it would keep your loop awake forever.

Under the hood a message is one JSON file — `kind`, `text`, `ts`, `source`, `meta` — renamed into place with `os.replace`, so a reader sees a message either not at all or complete.

**Background-run liveness** is a separate on-disk fact: one marker per detached `--bg` run at `~/.miniouto/spawn-logs/<id>.json`.

```bash
cat ~/.miniouto/spawn-logs/<id>.json
jq -r '"\(.id)  \(.status)  alarm=\(.alarm_session)  session=\(.session)"' \
  ~/.miniouto/spawn-logs/*.json
```

| Field | Meaning |
|---|---|
| `status` | `running` → `done` / `error` / `stopped` |
| `alarm_session` | the session that **receives** the completion message |
| `session` | the run's **own** session, where its transcript lives (never the same name for `--bg`) |
| `pid` | the detached process; `null` means "not known yet", and unknown reads as alive |
| `return_path`, `role`, `style`, `log` | what the run promised, and where its stdio went |

The read API is `core/spawn_state.py:pending_spawns(session)` — a pure filter that returns markers still claiming `running` **and** still backed by a live pid, so a crashed run drops out immediately instead of lingering forever:

```python
from miniouto.core import spawn_state
for marker in spawn_state.pending_spawns("mysession"):
    print(marker.id, marker.role, marker.return_path, marker.log)
```

A pid is not a handle: if the OS recycles it, that marker stays pending for as long as the unrelated process lives. Liveness is a promise, not a proof — a SIGKILLed run never stamps its marker.

### WAKE — process the queue

```bash
miniouto chat --alarms -c --name <session>
```

- `--alarms` takes **no prompt**: the queued messages *are* the prompt.
- `-c` / `--continue` is **mandatory**. History is never loaded implicitly; asking for it is the operator's decision.
- The named session must **exist**, or there is nothing to wake.
- All three checks run **before** any model call and before any session file is written, so a malformed wakeup costs nothing and leaves the queue intact.
- The drained texts, joined with a blank line, become the turn's user message.
- An **empty inbox is a no-op, not a turn**: one line on stdout, exit 0, no session marker, no finish marker, no model call.

Exact output of an empty wakeup (verified):

```
$ miniouto chat --alarms -c --name wake-demo
no pending messages in session 'wake-demo'.
$ echo $?
0
```

The exact rejections (all exit 1, all pre-flight):

```
✗ --alarms takes no prompt argument — the queued messages are the prompt.
✗ a wakeup continues an existing session — pass --continue
✗ nothing to wake: session 'nope-not-here' does not exist
✗ nothing to wake: no session to continue — pass --name <session>
```

A successful wakeup reads like any other run — marker, loop events, one finish marker, the answer:

```
------wake-demo------
outo: iter 1
------finish------
woken answer
```

`--pm`, `--role`, `--style`, `--use-subagent-provider`, `--answer-only` are all orthogonal to `--alarms`: a wakeup can run on a different provider, as a role, or print only its answer.

### Tying the three together

Because an empty wakeup is free, the loop can be a plain `until` over the inbox directory. **One consumer per session** is the design rule: two live processes bound to one session steal each other's messages, so give each its own session name or its own `MINIOUTO_HOME`.

```bash
#!/usr/bin/env bash
set -euo pipefail

SESSION="${1:?usage: supervise.sh <session>}"
export MINIOUTO_HOME="${MINIOUTO_HOME:-$HOME/.miniouto}"
INBOX="$MINIOUTO_HOME/inbox/$SESSION"

# Wake the session whenever its queue stops being empty, forever.
while :; do
  until [ -n "$(ls -A "$INBOX"/*.json 2>/dev/null)" ]; do
    sleep 30
  done
  # `message list` for a peek, or `miniouto chat --alarms -c` to consume it.
  miniouto message list --session "$SESSION"
  miniouto chat --alarms -c --name "$SESSION" >> "$INBOX.log" 2>&1
done
```

An equivalent that wakes on a fixed tick instead of watching the directory, so it also picks up a queue that another process created from a different `$MINIOUTO_HOME` prefix view (this is the cron shape below, written as a loop):

```bash
while :; do
  n=$(ls -A "$INBOX"/*.json 2>/dev/null | wc -l)
  if [ "$n" -gt 0 ]; then
    miniouto chat --alarms -c --name "$SESSION"
  fi
  sleep 30
done
```

`miniouto message list --session "$SESSION"` is a fine thing to log or eyeball at the top of that loop, and since it is silent on an empty inbox it is also a legitimate emptiness test: `[ -n "$(miniouto message list --session "$SESSION")" ]`. The `ls` condition above stays worth preferring when another process may hold a different `$MINIOUTO_HOME` prefix view of the same directory.

As a cron entry, one wakeup per tick — the "nothing to do" case is a no-op, so an over-frequent schedule is harmless:

```cron
*/2 * * * * MINIOUTO_HOME=/srv/miniouto /usr/local/bin/miniouto chat --alarms -c --name oncall >> /var/log/oncall-wake.log 2>&1
```

Two lanes worth keeping separate: a **wakeup lane** (this section — consume the queue) and a **liveness lane** (poll `spawn-logs/*.json` so you know which detached runs are still going). Neither blocks the other, and neither blocks a chat run.

---

## 4. Extracting history from session files

Everything a session ever did is on disk at `~/.miniouto/sessions/<name>.json`, as plain JSON. Nothing needs a running harness to read it.

### The schema

Schema v2 (`storage/sessions.py`) has two sections, and which one you want depends on the question.

| Section | What it is | Use it for |
|---|---|---|
| `history` | coreouto `Message` dicts — **system messages excluded**, media flattened to text placeholders, `tool_calls` / `tool_call_id` / `name` preserved. Rewritten in full at the end of every turn, and snapshotted mid-turn at every loop iteration. | The transcript the model actually saw. Feed it back, diff it, count it. |
| `turns` | display records: `ts`, `user`, `assistant`, optional `status`, optional `events` | What the human watched: the prompt, the final answer, and every loop event. |

Envelope:

```json
{
  "version": 2,
  "session": "my-session",
  "updated": "2026-10-15T11:27:51Z",
  "cwd": "/home/luke/miniouto",
  "history": [ { "role": "...", "content": "..." } ],
  "turns": [ { "ts": "...", "user": "...", "assistant": "...", "events": [ ... ] } ]
}
```

`cwd` is stamped on the **first** turn only: a session belongs to the directory it was created in. `turns[].status` is `running` / `done` / `interrupted`; a turn still `running` on disk means its writer died mid-turn (a `status` key is only written when it is *not* `done`, so its absence means done).

Loop events (`turns[].events[]`) are `LoopEvent` dicts: `actor`, `kind`, `text`, plus optional `tool_name`, `subagent_id`, `subagent_name`, `detail`, `msg_ref`. The `kind` values you'll actually filter on:

| `kind` | Source | In `history`? |
|---|---|---|
| `thinking` | the model's reasoning | **no** — providers never put thinking in a `Message`, so it lives only here |
| `response` | an assistant message body | yes |
| `tool` | a tool call (`tool_name` + `text` summary) | yes, as `tool_calls` |
| `tool_result` | its result (`tool_name` + `text`) | yes, as a `role: "tool"` message |
| `context` | the context monitor's occupancy note | no |
| `alarm` / `user` | a delivered inbox message (`detail: "inbox:<id>"`) | yes, folded into the user message |
| `handoff` | a summarization hand-off marker | no |

Every recipe below was executed against a real session file (`jq` 1.7). `$S` is the file:

```bash
S=~/.miniouto/sessions/my-session.json
```

**Full transcript, in order** (user and assistant text from the restorable history):

```bash
jq -r '.history[]
         | select(.role == "user" or .role == "assistant")
         | select((.content // "") != "")
         | "\(.role): \(.content)"' "$S"
```

```
user: how many doc files are there?
assistant: There are 9 doc files.
user: and now the logs
assistant: There are 9 doc files.
```

The `select` on a non-empty `content` is what drops tool-call-only assistant messages (they carry the call in `tool_calls`, not in `content`).

**Thinking only** (from `turns`, because thinking never enters `history`):

```bash
jq -r '.turns[].events[] | select(.kind == "thinking") | .text' "$S"
```

```
I should list the files first.
```

**The final answer of a run** (the last turn that produced one; a bare `--alarms` no-op never appends a turn, so it can't be mistaken for one):

```bash
jq -r '[.turns[] | select((.assistant // "") != "")] | last | .assistant' "$S"
```

```
There are 9 doc files.
```

**Tool calls with their results**, in emission order (`tool` then its `tool_result`, pairing them further with the shared `msg_ref`):

```bash
jq -r '.turns[].events[]
         | select(.kind == "tool" or .kind == "tool_result")
         | "\(.kind)  \(.tool_name // "-")  \((.text // "") | split("\n")[0])"' "$S"
```

```
tool  Bash  Bash ls docs
tool_result  Bash  README.md
```

Full results, not truncated:

```bash
jq -r '.turns[].events[] | select(.kind == "tool_result") | "[\(.tool_name)] \(.text)"' "$S"
```

**Handoff marker turns** — a summarization transition, as a TUI session records it (`cli/tui.py:_persist_handoff_marker` appends a trailing turn holding one `handoff` event):

```bash
jq -r '.turns[] | .events[]? | select(.kind == "handoff") | .text' "$S"
```

```
------summary → cont-ab12cd------
summary: /tmp/sum
```

**Turn-level summary** (when did what, and did it finish):

```bash
jq -r '.turns[] | "\(.ts)  \(.status // "done")  user=\((.user // "")[0:40])  answer=\((.assistant // "")[0:40])"' "$S"
```

**Everything as JSON lines**, for a program that wants to consume rather than read:

```bash
jq -c '.turns[] | {ts, user, assistant, events}' "$S"
jq -c '.history[]' "$S"
```

**Session inventory** — name, directory, and recency, without going through `load()` (which would write):

```bash
for f in ~/.miniouto/sessions/*.json; do
  jq -r --arg f "$f" '"\(.session)\t\(.cwd // "")\t\(.updated)"' "$f"
done | sort -k3 -r
```

### Writing history back

The `history` list is plain JSON, so injecting a few-shot prefix is a two-line edit — see [§ Session automation](#9-session-automation). Each entry must validate as a coreouto `Message` (`role`, `content`, optional `tool_calls` / `tool_call_id` / `name`); invalid entries degrade to plain text instead of failing. Never add a `role: "system"` entry: coreouto prepends a fresh system prompt on every call. Editing `turns` is display-only.

---

## 5. Parsing chat output by mode

`miniouto chat` offers three output modes. Automation should use **`--answer-only`** or **`--with-session`** to keep parsing simple. (Usage details in [`usage.md`](./usage.md#output-modes-verbose---answer-only----with-session).)

### stdout composition per mode

| Mode | stdout contents |
|---|---|
| verbose (default) | `------{session}------\n` + spinner/loop events (rich console) + `------finish------\n` + answer |
| `--with-session` | `------{session}------\n` + answer |
| `--answer-only` / `-a` | **answer body only** |

`------finish------` is a property of the **run**, not of a turn: it is printed **exactly once**, at the true end, and nothing follows it but the final answer. The process exits immediately after that answer, however much is still queued in the inbox. A run that summarized mid-work prints its hand-off markers *before* it, and a delegated run prints its completion line before it too:

```
------my-session------
------summary → cont-4f2a90------
summary: /proj/.miniouto/summary/my-session
------cont-4f2a90------
outo: iter 1
----- chat<9ab31c> complete ----- result: /home/luke/.miniouto/subagent-results/9ab31c-2026-10-15-11-27.md
------finish------
The refactor is done.
```

(There is no `------follow-up N------` banner: a chat never chains on its own. To process queued messages, wake the session — see [§ Alarm monitoring & wakeup](#3-alarm-monitoring--wakeup).)

An empty `--alarms` wakeup is the one case with **no** finish marker: it prints `no pending messages in session '<s>'.` and exits 0 without running a turn.

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
    provider="anthropic",         # override; else this session's pin, else the global default
    model="claude-sonnet-4",      # override; else the pin's model, else the provider's default_model
    max_tokens=2048,
    temperature=0.0,
    continue_session=False,
)
answer: str = run_chat(opts, sink=NullSink())   # return value = final answer string
data = json.loads(answer)
```

> Resolution order for every provider/model/reasoning field is `override > this session's pin > the global default` (`session-config/<session>.toml`, frozen at the session's first resolution). Pin a long batch session so a later global default change can't move it mid-batch; `ChatOptions` fields are the CLI `--pm`'s library equivalent.
>
> `run_chat` always returns the final answer string. Pass `NullSink` to suppress loop events, spinner, and markers. The user message and assistant reply are automatically appended to `~/.miniouto/sessions/<session>.json`.

---

## 6. Auto-registering providers

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
style = "coding"
session = "default"
```

> Writing the files directly means the configuration is complete without ever running miniouto. The next `miniouto chat` reads it as-is. Note that `ensure_dirs()` must run at least once so the directory exists — a single `miniouto status` is enough.
>
> `settings.toml` is the **global default**; a session that already froze its own `session-config/<session>.toml` pin ignores later edits here. For a specific run (or a specific session you intend to keep), pass `chat --pm 'PROVIDER/MODEL[:REASONING]'` instead — it wins over both.

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

## 7. Auto-registering styles

A style is a file at `~/.miniouto/style/<name>.md`. Three registration methods.

### Method A — write the file directly

```bash
mkdir -p ~/.miniouto/style
cat > ~/.miniouto/style/my-prompt.md <<'EOF'
<outo>
You are a code review expert. ...
</outo>

<editor>
You are an editor. Edit the file the brief names, then report back.
</editor>

<reviewer>
You are a reviewer. Read the diff, flag problems, never edit.
</reviewer>
EOF
```

The `<outo>...</outo>` tag is required (without it, the entire file is used as the outo prompt). Each **top-level** `<name>...</name>` block is one **role** for `miniouto chat "<brief>" --style <style> --role <tag>` — the tag name IS the role name. A legacy `<subagent>...</subagent>` block becomes a role named `"subagent"`. **Zero named tags is legal**: `chat` without `--role` runs the outo prompt, and `--role <tag>` with no match errors before the turn. Delegation itself (`Bash` → `miniouto chat --role`) is always available regardless of the style's tags. Format details in [`styles.md`](./styles.md).

To activate:

```bash
miniouto style set my-prompt
# or write style = "my-prompt" directly into settings.toml
```

> Warning: if the file name matches one of the five bundles (default, coding, coding-pro, coding-work, coding-ultra), the next `ensure_dirs()` call **overwrites** it with the bundled content. Custom styles must use a different name.

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

## 8. Auto-registering skills

Skills live outside `~/.miniouto/`, in one of two default roots, listed **project-local first**: `./skills/<name>/SKILL.md` relative to the directory you invoked miniouto from, then `~/.agents/skills/<name>/SKILL.md` (the **Anthropic-compatible path**). Changing `MINIOUTO_HOME` does not move skills. Check a shared skill into a repo's `skills/` and anyone who clones it gets it automatically.

### Directory layout

```
./skills/                # project-local, listed FIRST
└── my-skill/
    └── SKILL.md         ← YAML frontmatter + markdown body

~/.agents/skills/        # global, listed SECOND
└── my-skill/
    └── SKILL.md
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

**Just create the directory and file — the next chat call picks it up automatically.** There is no registration command. On every `build_runtime()` call each style body is expanded, and the `{skills}` token (defaulting to `./skills/` then `~/.agents/skills/`; `{skills:<dir>…}` names other roots) renders a catalog (name + description per skill, plus the on-disk location) wherever the style placed it — in the outo body and in each role body alike; the agent reads the full SKILL.md via Bash on demand. A style that names no skills token tells the model about none.

### Auto-registration script example

```bash
# Ensure the skills directory exists (global root; use ./skills for project-local)
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

> Because there's no registration command, **removal is just deleting the directory**: `rm -rf ~/.agents/skills/my-skill` (or `rm -rf ./skills/my-skill`). Once the directory is gone, the skill automatically drops out of the prompt on the next chat call.

---

## 9. Session automation

A session is a file at `~/.miniouto/sessions/<name>.json` — a schema-v2 envelope with two sections: `history` (restorable model context: raw coreouto `Message` dicts, system messages excluded, full loop transcript including tool calls/results) and `turns` (display log: user/assistant text + `LoopEvent` dicts including thinking). The full schema, and a set of copy-paste `jq` recipes for pulling a transcript, the thinking stream, the final answer and tool activity out of it, are in [§ Extracting history from session files](#4-extracting-history-from-session-files).

### Continuing a session programmatically

`--continue` (`-c`) prepends a session's existing `history` to the next prompt, and it is **never implied** — a plain `chat` starts fresh every call, and a wakeup must spell it out. This is the backbone of multi-step workflow automation:

```bash
SESSION="refactor-$(date +%Y%m%d)"

# Step 1
miniouto chat "analyze this file: $FILE" --name "$SESSION" -a > /tmp/step1.txt

# Step 2 (step 1's analysis kept as context)
miniouto chat "based on the analysis, plan the refactor" --name "$SESSION" -c -a > /tmp/step2.txt

# Step 3
miniouto chat "execute the plan" --name "$SESSION" -c
```

### Waking a session instead of chaining turns

`chat` never waits: it answers its prompt, prints one `------finish------`, and exits. So a multi-step workflow is driven by the *caller*, not by the run:

```bash
SESSION="oncall-$(date +%Y%m%d)"

# Kick off the session
miniouto chat "watch the deploy log" --name "$SESSION" --bg \
  --alarm-session "$SESSION" --return /tmp/deploy-report.md

# Something happens; the agent is told without anybody polling it
miniouto message insert --session "$SESSION" "deploy 4f2a90 finished"

# A separate process (or cron tick) wakes the session up. It exits when done.
miniouto chat --alarms -c --name "$SESSION"

# And again, for whatever arrived while that turn ran
miniouto chat --alarms -c --name "$SESSION"
```

An empty queue makes the second call a free no-op, so "wake until quiet" needs no bookkeeping:

```bash
INBOX="${MINIOUTO_HOME:-$HOME/.miniouto}/inbox/$SESSION"
while [ -n "$(ls -A "$INBOX"/*.json 2>/dev/null)" ]; do
  miniouto chat --alarms -c --name "$SESSION"
done
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

## 10. Non-interactive automation patterns (CI / Docker / cron)

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

## 11. lma cache control

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
