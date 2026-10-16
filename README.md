# miniouto

<p align="center">
  <img src="logo.svg" alt="miniouto logo" width="800">
</p>

A minimal, file-driven CLI agent harness built on [coreouto](https://github.com/llaa33219/coreouto).

Three principles:
1. **Minimalism** — No bloat. Extend with styles.
2. **Automation-friendly** — Full CLI; TUI is optional.
3. **Fluidity** — Adapts to any environment. Delegation is just a `miniouto chat "<brief>" --style <style> --role <tag>` shell command in a separate process, so a brief fits any harness the same way.

## Preview

<p align="center">
  <img src="preview.gif" alt="miniouto preview" width="800">
</p>

## Install

```bash
uv tool install miniouto
# or
pip install miniouto
```

## License

[Apache License 2.0](./LICENSE)

## Quick start

```bash
# Either add a custom provider by hand:
miniouto provider custom add \
  --name openai \
  --api-key sk-... \
  --base-url https://api.openai.com/v1 \
  --default-model gpt-5.5            # optional; used when neither chat --pm nor the session pin names a model

# Or browse the lma (llm-model-api) catalog and add from there:
miniouto provider providers          # see all 144 catalog providers
miniouto provider add Anthropic --api-key sk-ant-...   # adds it with the first catalog model

miniouto provider default openai
miniouto style set default
miniouto chat "hello"
miniouto                              # TUI mode
```

## Computer use (GUI control)

The outo agent can operate GUI applications inside a virtual headless display (via [py-Vwayland](https://github.com/llaa33219/py-Vwayland)): launch apps, take screenshots, click, type, scroll, drag. Enabled automatically on Linux x86_64 (glibc ≥ 2.28) — no setup needed; the tool simply doesn't appear on unsupported platforms. Set `MINIOUTO_COMPUTER=0` to disable it explicitly. Requires a vision-capable model so the agent can see its screenshots.

Screen dumps are written to `~/.miniouto/computer-screens/` as Unicode `.art`: eight sub-pixels per terminal cell, each cell drawn as a solid `█`, a **BLOCK OCTANT** (Unicode 16.0) when it is two crisp solid regions, or **BRAILLE** dots when it is a blend, each in truecolour fg/bg. View one yourself with `miniouto computer show [file]` (add `--plain` for a luminance ramp). Old `halfblock-truecolor` dumps still render unchanged.

## Delegation

Delegation is a Bash command, not an in-process tool, and there is no separate spawn command — a delegated run *is* a `miniouto chat` run. The agent runs:

```bash
# background (fire and forget): detach, write the answer to a file, alarm on completion
miniouto chat "<brief>" --style <style> --role <tag> --bg --pm <provider>/<model> --alarm-session <session> --return

# foreground: same thing, answer inline in the tool result
miniouto chat "<brief>" --style <style> --role <tag> --pm <provider>/<model> --return
```

Each delegated run is its own process (with `--bg`, detached, so it survives the turn that started it) and is **mechanistically identical to an outo run** — same tools, same chain, same watchdog. Only four things differ, each one a flag: `--style <style> --role <tag>` selects a top-level `<tag>...</tag>` body from that style as the system prompt (omit `--role` to run the `<outo>` body), `--pm 'PROVIDER/MODEL[:REASONING]'` takes provider/model/reasoning outright, and bundled styles write it with the `{subagent_pm}` token so it always names the effective `subagent_*` trio (this run's session pin first, then the global default); `--use-subagent-provider` resolves that same trio at call time when a hand-written command carries no `--pm`, `--alarm-session <session>` names the session that receives the completion message, and `--return` writes the final answer as markdown (bare = `~/.miniouto/subagent-results/`; the file holds **only** the answer text). Every value is spelled out: `--role` **requires** `--style`, `--alarm-session` **requires** a session name, and `--pm` carries a concrete provider/model, because a command that reads `settings.style` or `settings.session` behind your back is not the same command twice. Bundled styles are taught with `{style}` / `{this_session}` / `{subagent_pm}` tokens that expand into literal values per run, so what the model copies is always self-contained. `--return` is the only flag whose value is optional.

When the run finishes it writes that file and inserts exactly one completion message into the target session's inbox. That message's first line is always `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]`, so the agent can tell it apart from a real one, and it names the run's own `Session: <name> — <path>` so the receiver can open that transcript. Every chat run writes its session file, delegated or not; `--answer-only` / `-a` suppresses stdout only. A delegated run never changes your **active** session, though: it mints its own `chat-`/`spawn-` session name, writes its transcript there, and leaves `settings.toml`'s `session` exactly where your plain `miniouto chat` left it.

## Alarms are wakeups

A chat run **never waits**. It answers its prompt, prints exactly one `------finish------`, and exits — even if background runs of its own are still going. Anything that arrives afterwards (a detached run's completion alarm, a cron job, another agent) waits in `~/.miniouto/inbox/<session>/` for the next wakeup, which is the TUI's 1 s poll or the explicit CLI door:

```bash
miniouto message insert --session <s> "something happened"   # from any process, any tool
miniouto message list --session <s>                          # non-destructive peek
miniouto chat --alarms -c --name <s>                         # wake: drain the queue, run one turn, exit
```

`--alarms` takes no prompt (the queued messages are the prompt), requires `-c` (history is never loaded implicitly), and requires the session to exist. An empty inbox is a no-op: one line, exit 0, no model call — so a monitor can poll it for free. Nothing here is locked in: any tool that can run a shell can insert, observe, or wake. Monitoring recipes and `jq` recipes for extracting a transcript from a session file are in [`docs/automation.md`](./docs/automation.md).

## Long runs: explicit summaries, session hand-off

There is **no implicit summarization** — nothing rewrites the conversation behind the model's back. When a turn has eaten ~80% of its context window, the harness sends one system message (`[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]` + `Context budget: {pct}% of this session's window is consumed ({used}/{window} tokens). Explicit summarization is required now.`, arriving as a user message) telling the agent to write summary documents into `./.miniouto/summary/<session>/` and call the `continue_session` tool. That ends the conversation and continues in a fresh `cont-*` session that reads those documents; a delegated run carries its contract (alarm session, result file, role, style) across, and its result file and completion alarm name the session it finally ended in. Summary documents live next to your code on purpose, so they can be reviewed and committed with the work they describe.

## Model resolution

The active provider, model and reasoning effort each come from the **first match in**:

1. `miniouto chat --pm 'PROVIDER/MODEL[:REASONING]'` — for that one call and nothing else. This is the **only** per-call override; the old `--provider` / `--model` / `--reasoning` / `--subagent-provider` / `--subagent-model` flags are gone. The provider is everything before the first `/` (a model name may contain slashes), and reasoning is split off the last `:`; both are optional parts, and a missing `/` is rejected before anything is resolved.
2. **this session's pin**, `~/.miniouto/session-config/<session>.toml` — frozen the first time that session resolves, so a model switch made in another process can never redirect a conversation already under way.
3. the global default — `settings.provider` plus the provider record's `default_model` / `reasoning_effort` (a legacy `settings.model` still reads at the bottom of that chain).
4. error — no provider configured, or no model can be inferred.

That middle tier is the point: **each session keeps the settings it started with, and only the default moves** (the most recent TUI change wins). A session minted on the fly — a delegated `chat-`/`spawn-` run, a summary hand-off's `cont-*` session, the TUI's "new session" — inherits the pin of the session it continues, copy-if-absent. So several miniouto processes can run on different providers at the same time without redirecting each other.

In the TUI, both provider chips open the same popup: three rows, `Provider` / `Model` / `Reasoning`, each with its own picker. Picking a provider or a model resets Reasoning to that model's default (an effort level belongs to a model), and every change writes **both** the session pin and the global default.

The **subagent** (a delegated run that asks for the subagent profile — `--use-subagent-provider`, or the `--pm` value the `{subagent_pm}` token renders) can run on a different provider/model, and resolves the same way, pin-first against its own session: `--pm` > the pin's `subagent_provider` > `settings.subagent_provider` (via `miniouto subagent set`) > inherit outo's provider; and `--pm` > the pin's `subagent_model` > `settings.subagent_model` > the subagent provider's `default_model` > inherit outo's model (only reachable when no subagent provider is set). A model-only setting (no subagent provider) is also valid — the run then uses outo's provider with that model. Reasoning resolves as: the `--pm` suffix > the pin's `subagent_reasoning` > `settings.subagent_reasoning` > the subagent provider's `reasoning_effort` > lma's default.

## Commands

| Command | Purpose |
| --- | --- |
| `miniouto chat "prompt"` | One-shot chat turn — also the delegation door (`--style`+`--role`, `--bg`, `--pm`, `--alarm-session <s>`, `--return`) and, with `--alarms -c`, the wakeup door |
| `miniouto` | TUI mode |
| `miniouto status` | Show current configuration |
| `miniouto message insert/list/clear` | Insert into or inspect a session inbox (`--session <s>`) |
| `miniouto computer show` | Render a captured `.art` screen dump |
| `miniouto provider providers/models/add` | Browse the lma catalog and add providers from it |
| `miniouto provider custom add` | Add a custom provider by hand (api_format, base_url, api_key, default_model) |
| `miniouto provider list/remove/default` | Manage saved providers |
| `miniouto style list/set/add/update/show` | Manage style documents (`update` re-seeds bundled + re-fetches all repo styles) |
| `miniouto skill list/show` | List/show skills from `./skills/` (project-local) + `~/.agents/skills/` |
| `miniouto subagent show/set/clear` | Manage the **global default** subagent provider/model/reasoning (empty = inherit outo's) |

### `chat` flags

| Flag | Effect |
| --- | --- |
| `--name` | Session name (persists to `~/.miniouto/settings.toml`) |
| `--pm 'PROVIDER/MODEL[:REASONING]'` | Override provider/model/reasoning for this call only. The one per-call provider override; wins over the session pin and the global default |
| `--style` | Override the active style for this call. **Required with `--role`** |
| `--role` | Run as this style role's system prompt (a `<tag>...</tag>` body) — what makes the turn a delegated one. Requires `--style` |
| `--use-subagent-provider` | Resolve provider/model/reasoning from the subagent trio (this session's pin, else the `subagent_*` settings default) instead of outo's. An explicit `--pm` still wins — and prefer `--pm` in generated commands, since a literal provider/model makes a copied command idempotent |
| `--max-tokens` | Cap output tokens |
| `--temperature` | Sampling temperature |
| `--alarm-session NAME` | Insert one completion alarm into that session's inbox on completion. The value is required — there is no bare form |
| `--return [PATH]` | Write the final answer as markdown to this path (bare = `subagent-results/`); a directory receives the generated filename |
| `--bg` | Detach the run into its own process and return immediately |
| `--continue` / `-c` | Prepend the session's previous history. Never implied — and mandatory with `--alarms` |
| `--alarms` | Wake a session from its queued messages: the inbox is drained and the joined texts become the turn's user message. No prompt argument; requires `-c` and an existing session; an empty inbox is a no-op |
| `--answer-only` / `-a` | Print only the final answer (no session marker, no loop events, no finish marker) |
| `--with-session` | Print only the session marker + final answer (no loop events, no finish marker) |

### Talking to a running turn

Anything can drop a message into a session's inbox from outside, and a turn that is *already running* picks it up at its next iteration. With nothing running, the message simply waits for the next wakeup (`miniouto chat --alarms -c`, or the TUI):

```bash
miniouto message insert --session mysession "actually, use tabs"
miniouto message list --session mysession        # peek: never consumes
miniouto message clear --session mysession
```

## Storage

Everything lives under `~/.miniouto/`:
- `providers.toml` — provider configs (one top-level TOML table per provider: `api_format`, `base_url`, `api_key`, `default_model`, `source`)
- `settings.toml` — the global **default** `provider`, `style`, `session`, `theme`, `subagent_provider`, `subagent_model`, `subagent_reasoning` (plus a legacy, read-only `model`)
- `session-config/<session>.toml` — that session's pinned `provider`, `model`, `reasoning` and `subagent_*`; an empty key means "follow the default"
- `style/<name>.md` — style documents
- `sessions/<name>.json` — conversation history
- `inbox/<session>/` — one JSON file per pending message for that session (alarms and `miniouto message insert`)
- `spawn-logs/` — background-run liveness markers (`<id>.json`) plus bg stdio logs (`<id>.log`)
- `computer-screens/` — `.art` Unicode screen dumps written by the Computer tool (`screen`; `screenshot` writes the same art as a best-effort sibling of its capture)
- `subagent-results/` — one markdown file per finished delegated run, holding the final answer (the completion alarm names the file)
- `logs/` — reserved (currently unused)

Summary documents are the one exception: they live **project-local** at `./.miniouto/summary/<session>/` (relative to the directory you invoked miniouto from), because a summary is a project artifact rather than harness state.

Skills live outside this tree, in one of two default roots: the project-local `./skills/<name>/SKILL.md` (relative to the directory you invoked miniouto from — check it into your repo and everyone gets it) and `~/.agents/skills/<name>/SKILL.md` (the Anthropic convention). A style advertises them to the model with the `{skills}` token in its body, which lists **both** defaults, project-local first; `{skills:~/.claude/skills}` lists whatever directories you name instead. The harness adds nothing implicitly: the system prompt is the style body with its tokens expanded, nothing else.

Override the root with the `MINIOUTO_HOME` environment variable.
