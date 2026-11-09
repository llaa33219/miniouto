# Style Documents

A "style" in miniouto is a **Markdown system prompt** that defines the persona, operating principles, and tool usage rules for the agent. Styles are stored at `~/.miniouto/style/<name>.md` (seeded from the bundled `src/miniouto/default_style/` on first run).

The active style is selected via `miniouto style set <name>` (or per-call via `miniouto chat --style <name>`).

## File structure

A style document is plain Markdown with a **top-level XML structure**: one optional `<outo>` block (always the outo prompt) and zero or more **named role** blocks (each `<tag>` is one role, selected by `miniouto chat "<brief>" --style <style> --role <tag>`). The tag name IS the role name.

```markdown
# Optional top-level title or notes (ignored by the parser)

<outo>
You are outo. You have full host access. …
[main agent persona + operating principles + tools list]
</outo>

<editor>
You are an editor. Read the brief, edit the file, report back. …
[editor persona + operating principles + whitelisted tools]
</editor>

<file-picker>
You are a file picker. Locate the right files. Do not edit. …
[file-picker persona + operating principles]
</file-picker>
```

Nothing is concatenated onto the tags. The system prompt the model sees **is** the chosen body with its tokens expanded — the harness prepends no cwd preamble and no skill catalog, so where (and whether) the working directory and the skill listing appear is entirely the style author's decision. A body that names no tokens tells the model nothing about either.

So the final outo prompt the model sees, top to bottom, is:

1. The `<outo>` section content (or the whole document if `<outo>` is missing), token-expanded per turn.

A role run (`miniouto chat "<brief>" --style <style> --role <tag>`) is the same story with that tag's body as the base. It is assembled by the **same** `build_runtime` call and through the same `expand_prompt_tokens` pass, so there is no second prompt-assembly path to drift. **Role bodies need their own tokens**: expansion is per body, so a role that must know the working directory or advertise skills carries `{cwd}` / `{skills}` itself. Every bundled style puts both in every body it defines, outo and role alike.

**Legacy compat**: an old-style `<subagent>...</subagent>` block is parsed as a single role named `"subagent"`. Existing style files keep working unchanged — `--role subagent` runs them.

## `parse_style(content) -> tuple[str, list[SubagentSpec]]`

From `storage/styles.py`. Parses a style document with a single sequential scan over its top level:

```python
@dataclass(frozen=True)
class SubagentSpec:
    name: str      # the tag name, e.g. "editor" or "subagent"
    prompt: str    # the tag body (this role's system prompt)

def parse_style(content: str) -> tuple[str, list[SubagentSpec]]: ...
```

Rules:

- `<outo>...</outo>` is extracted as the first element of the returned tuple. If absent, the **whole document** is used as the outo prompt.
- Each **top-level** `<name>...</name>` pair outside `<outo>` becomes one `SubagentSpec`; the tag name IS the role name that `--role` takes. Tag names match `[A-Za-z][A-Za-z0-9_-]*` (hyphens allowed: `file-picker`, `researcher-docs`).
- `outo` is reserved — always the outo prompt, never a role.
- A `<subagent>...</subagent>` block is parsed as a role named `"subagent"` (legacy compat).
- **Zero role tags is legal**: `chat` with no `--role` runs the outo prompt, and `--role <tag>` on a style with no matching tag **errors before the turn** (listing the available roles, or `(none)`), so a typo costs no model call.
- The parse is **top-level only**: a `<name>...</name>` pair nested inside another tag body is treated as content, not as a role.
- Stray prose outside any tag is ignored.
- Duplicate tag names at the top level raise `ValueError`.
- `parse_style` returns bodies **verbatim** — tokens included. Expansion happens later, at prompt assembly (below).

## Dynamic tokens

Five tokens are substituted into every style body at system-prompt assembly, by `core/runtime.py:expand_prompt_tokens`:

| Token | Expands to |
|---|---|
| `{cwd}` | the directory miniouto was invoked from — `paths_runtime.INVOCATION_CWD`, captured at import, so it is stable for the whole process |
| `{skills}` | the lazy-load skill catalog for the **default roots**: the invocation directory's `./skills/` **first**, then `~/.agents/skills` |
| `{skills:<dir>[,<dir>…]}` | the same catalog merged from the named roots (an empty argument — bare `{skills}` or `{skills:}` — means the two default roots above), comma-separated, in the order listed. `~` is expanded; a relative path resolves against `INVOCATION_CWD`, so a style can point at `./skills`. Missing or unreadable roots are skipped silently; entries are ordered by root order then name; a name present in two roots appears twice, each line naming its own root; nothing found → the empty string |
| `{this_session}` | this run's own session name — `runtime.session`, or `"default"` when it is empty |
| `{style}` | the resolved style name — `runtime.style_name` |
| `{subagent_pm}` | the effective subagent provider config in `--pm` form — `PROVIDER/MODEL` or `PROVIDER/MODEL:REASONING`. Resolved from the **subagent trio** (`session_config`'s `subagent_provider` / `subagent_model` / `subagent_reasoning`, else the `settings.subagent_*` global default, falling back to outo's own provider/model/reasoning per key). Left **literal** when the trio does not resolve |

Six spellings, five tokens: `{skills}` is the no-argument form of `{skills:…}`, and a bare `{skills:}` means the same thing — both default to the **two** default roots, local before global.

Three representative configurations:

| Write this | To get |
|---|---|
| `{skills}` | both default roots, project-local `./skills/` first, then `~/.agents/skills` |
| `{skills:~/.claude/skills}` | a Claude-style skills directory, listed **instead of** the defaults |
| `{skills:./docs/skills,~/.agents/skills}` | the two defaults with an extra project-local root spliced in front |

Rules:

- Substitution happens per body: **both** the `<outo>` body **and** every role body, on every turn (nothing is persisted).
- **Order matters.** The parameterized `{skills…}` regex pass runs **first** (the catalog text itself contains braces), then the plain `str.replace` passes: `{cwd}`, then `{this_session}` and `{style}`, then `{subagent_pm}`. `{subagent_pm}` is only resolved when the token is still present after the earlier passes.
- **A body with no tokens simply has no cwd and no skills.** There is no compatibility fallback and no implicit layer — the layout is yours.
- **Any other `{...}` text is left verbatim.** There is no escape mechanism, and an unknown token is not an error — so `{brief}` or a JSON-looking brace in your prose is safe.
- **The model never sees the token syntax.** By the time the prompt reaches the model, the recipe line reads as a fully literal, copy-ready command. That is the point: the delegated command line is run through `Bash`, so it must be the same command every time it is typed, from any agent, in any session.

A style author therefore writes the recipe with tokens, and expansion makes it concrete per run. Given a session `default` and the style `coding-work`, this line in `coding-work.md`:

```
miniouto chat "<self-contained brief>" --style {style} --role <tag> --bg --pm {subagent_pm} --alarm-session {this_session} --return
```

reaches the model as:

```
miniouto chat "<self-contained brief>" --style coding-work --role <tag> --bg --pm minimax/MiniMax-M3.1:high --alarm-session default --return
```

`<tag>` is **not** a token — the model picks a role name from the style's own roster.

These tokens are what replaced the old ambient bare-flag defaults. A bare `--alarm-session` used to mean "my session" through `settings.session`, and a missing `--style` fell back to whatever the default style happened to be — so the same Bash command meant different things depending on who ran it. Now both values are mandatory on the delegation surface (`--role` requires `--style`; `--alarm-session` requires a session name), and the tokens are how a bundled style still teaches a literal, self-contained command. See [`cli.md`](./cli.md) § `miniouto chat` for the flag contract.

**Precedence.** Values in the CURRENT assembled prompt beat anything in history. The prompt is rebuilt from the style file on every turn, so `{style}` and `{this_session}` are current by construction — but any command literal written into an earlier turn of the transcript is stale the moment the style changes (TUI style picker, `miniouto style set`). Models imitate historical commands, so the bundled styles carry the canonical sentence: take the delegation values **never from commands in earlier turns**. Style authors should keep that sentence wherever they teach a delegation command, next to the recipe itself.

## The tools section (required)

Every style should include a **Tools available** section that lists the tools the agent can call:

| Tool | Purpose |
|---|---|
| `Bash(command, *, cwd=None)` | Run a shell command — the ONLY file-manipulation tool (read via `cat`/`grep`, write via heredoc/`tee`, edit via `sed -i`/Python, delete via `rm`). 1-hour hard timeout (`BASH_TIMEOUT_SECONDS`). (`env` is accepted by the underlying `bash()` function but is **not** exposed through the model-facing schema — the registered `_bash_handler` does not accept it.) |
| `Image(file_path)` / `Video(file_path)` / `Audio(file_path)` | View/listen to a media file (multimodal blocks). |
| *(delegation is not a tool)* | There is **no** `call_subagent` tool — the word appears nowhere in the codebase. Delegation is a **Bash command**: `miniouto chat "<self-contained brief>" --style <style> --role <tag> --return` runs a nested run of the harness in its own process and prints its answer inline; add `--bg --alarm-session <session>` to fire and forget, in which case the answer arrives later as a user message starting with `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]` naming a file under `~/.miniouto/subagent-results/`. One brief per command; several commands in one Bash call = several concurrent runs. `Bash` is always registered, so **the delegation surface is never conditional on this style's tags** — what a style controls is only which role bodies exist. |
| `Computer(action="screen", chars=…, color=…)` | (outo runs only) Dump the current virtual screen as Unicode half-block art to `~/.miniouto/computer-screens/*.art` and return a short text preview — the text-only channel that works where `screenshot` cannot. Render it with `miniouto computer show`. |

There are deliberately **no** Write/Edit/Delete tools — file work goes through Bash (see `docs/tools.md` § "Why Bash is the only file tool"). All bundled styles describe the tools with **identical behavior summaries** (so the model doesn't see inconsistent tool docs across styles). The summaries come from `tools/registry.py`'s `_<name>_description` strings.

## The loop behavior section (required)

Every style should also include a **Loop behavior** section with three rules:

1. **Termination**: when you've finished, your final message is plain text with no tool call.
2. **Mid-loop progress**: to send a progress update to the user while still planning more tool calls, include the text ALONGSIDE a tool call in the same turn — a response carrying text AND a tool call keeps the loop going and shows that text. No special tool exists for this; do not invent one.
3. **Tool results are loop input**: the result of a tool call is fed back to the model as the next iteration's input — it is **not** the user's response.

This mirrors coreouto's termination policy exactly: text + no tool call = final answer; text + tool call = keep going. An earlier draft referenced a `continue_loop` tool that was never registered; the model then invented calls that never executed. That reference has been removed from every style, the docs, and `runtime._fallback_style`.

---

## Bundled templates

The five bundled templates live in `src/miniouto/default_style/`. They are seeded into `~/.miniouto/style/` by `storage/paths.ensure_dirs`. Bundled styles are **force-refreshed**: every `ensure_dirs()` call overwrites any installed file whose name matches a bundled template with the current bundled content (written only when the content differs, to avoid needless disk churn). To customize a bundled style, copy it to a new name (e.g. `cp default.md mydefault.md`) — files whose names do not match a bundled template are never touched. Repo-added styles (via `style add`) are refreshed on demand with `style update`.

**Rename history (0.8.1)**: `pro.md` → `coding-pro.md` and `ultra.md` → `coding-ultra.md`. `ensure_dirs()` removes an old-name seeded copy only while it still matches the renamed bundle byte-for-byte (a user-customized old file survives as a regular user style) and repoints `settings.style` to the new name when it referenced the old one.

| File | Size | Persona | Orchestrator? |
|---|---|---|---|
| `default.md` | ~15 KB | "**outo**" — minimal, sparse | No (deliberately) |
| `coding.md` | ~4.7 KB | "**outo**" — expert coding agent, minimal ruleset | Light (delegation for non-trivial work) |
| `coding-pro.md` | ~38 KB | "**pro**" — senior staff engineer orchestrator | **Aggressive** (with a delegate-vs-DIY decision framework) |
| `coding-work.md` | ~49 KB | "**coding-work**" — pragmatic senior engineer orchestrator | **Aggressive but proportionate** (one subagent per slice, one reviewer when warranted) |
| `coding-ultra.md` | ~90 KB | "**ultra**" — relentless MAX-mode execution orchestrator | **Aggressive** (layered subagent fan-out, best-of-N editors, multi-focus review) |

`coding-pro.md`, `coding-work.md`, and `coding-ultra.md` include explicit guidance on when and how to delegate, taught entirely as **Bash recipes** (`miniouto chat "<brief>" --style {style} --role <tag> --pm {subagent_pm} [--bg --alarm-session {this_session}] --return` — the tokens expand per run, see [Dynamic tokens](#dynamic-tokens)); `default.md` and `coding.md` compress the same mechanic to one or two lines.

**Note on which provider a delegated run uses**: bundled styles teach the recipe with `--pm {subagent_pm}`, so the command the model copies carries the subagent provider/model/reasoning **spelled out** — literal, idempotent, and not re-resolved against whatever the default says at the moment the command is typed. `--use-subagent-provider` remains available and does the same job by *resolving at call time*: it resolves the subagent trio **pin-first against the delegated run's own session** (`session-config/<session>.toml`, which it inherits copy-if-absent from the caller via `seed_from`), else the `settings.subagent_*` global default. Prefer `--pm` in a generated command; reach for `--use-subagent-provider` when the point is "whatever the subagent profile currently is".

**Note on mechanics**: each bundled orchestrator style declares its roster as top-level named tags (one tag per role). The model delegates by running `miniouto chat "<brief>" --style <style> --role <tag>` through `Bash`, and that tag's body becomes the delegated run's system prompt — a per-role lean prompt in its own process. A mixed-role layer is just **several such commands issued together** (an editor, then a reviewer), each carrying exactly one brief; each run reports through its own completion alarm. See each style file for its actual roster.

### `default.md` — minimal fallback

The original "outo" prompt. Short, opinionated, deliberately sparse. Used when no style is set, or when `miniouto style set default` is run.

Key points:
- 7 outo operating principles: be brief, lead with the answer, finish with text + no tool call (progress updates are text ALONGSIDE a tool call), treat tool results as loop input, match delegation scope to task size, never invent outputs, match the user's language, pass paths correctly when delegating.
- A single legacy `<subagent>` block (named `"subagent"`, selected with `--role subagent`): 10 principles — treat the brief as the whole spec (no clarifying questions), be terse, prefer targeted `sed`/Python edits over full-file rewrites, read first, return useful extracted output (not full dumps), plan + execute + synthesize for multi-step work, spawn a nested run only when the subtask deserves its own context, finish with text + no tool call, tool results are loop input, match brief language, surface errors verbatim.

### `coding.md`: expert coding agent, minimal

A deliberately terse style (~1.6 KB) — an "expert coding agent" persona with
only the load-bearing rules. Both halves are a short ruleset:

- **Identity**: "expert coding agent. Be precise, minimal, and verified."
- **Tools**: one line each — Bash as the ONLY file tool (with the
  read/create/edit/delete command forms), media viewers, Computer,
  and `miniouto chat "<brief>" --style <style> --role <tag>` with one
  self-contained brief per command (several commands run as separate,
  independent processes).
- **Seven rules** (outo half): follow matching skills; read before editing
  and follow project conventions; smallest change; verify with real
  commands and never fabricate; fetch web content instead of recalling it;
  ask before destructive/ambiguous moves; match the user's language.
- **Six rules** (single legacy `<subagent>` block, named `"subagent"`): same core — brief is the whole spec, read first, Bash-only file work, verified reporting (files changed, commands + output, blockers), no unauthorized destructive actions, match the brief's language.

### `coding-pro.md`: senior staff engineer orchestrator

A senior staff engineer orchestrator. Persona: "**pro**, a senior staff engineer"
that is "a teammate, not a tutor" — "you do the work, you show the evidence, you
do not narrate." Its signature is **explicit**: it names the destination before
starting, the files before touching them, and the command that proves it before
claiming it. It asks **once, cleanly, up front**, then carries the work to the
end without asking again.

The prompt is an ordered stack; each section answers one question:

1. **Environment** — `{cwd}` and `{skills}`.
2. **Project rules come first — AGENTS.md**: on every turn, check for an
   instruction file (repo root, `./.agents/`, `./docs/`, nested copies), read
   it in the same response, and treat it as **overriding** the style when in
   conflict. First contact runs one batched sweep (layout, state, real
   build/lint/test commands, instructions, conventions, verification surface);
   later turns refresh only what may have changed.
3. **Skills — mandatory first check**: scan the skill listing, follow a
   matching skill, name it in any brief that covers the same domain.
4. **Operating rules** (12): lead with the answer, read before editing,
   surgical changes, verify with real evidence, never fabricate, preserve
   user work, match the user's language, depth scales with the task, ask
   once clearly, loop until done or truly blocked, one objective per unit
   of work, convergence not repetition.
5. **Match the deliverable to the request** — "explain" → answer;
   "look into" → findings report then **stop** (an investigation is not
   implementation authorization, and authorization does not carry across
   turns); "what do you think" → judgment; "implement/fix" → shipped work;
   "refactor" → proposal first when the blast radius is unclear.
6. **Size the work before you start** — S/M/L tiers, each with its procedure.
7. **The confirm gate** — a proposal (destination, changes, verification,
   risk) and ONE question; after a go-ahead, no re-asking.
- **Subagent roster (top-level tags)**: file-picker / thinker / editor / reviewer / validator — each a named tag in the file, loaded as that subagent's own system prompt.
- **Web search and fetching**: `curl` with DuckDuckGo HTML is recommended;
  a matching web-access skill still takes precedence.

### `coding-work.md`: pragmatic senior engineer orchestrator

A pragmatic senior-software-engineer orchestrator positioned between
`coding-pro.md` and `coding-ultra.md`. Persona: "**coding-work**, a pragmatic
senior software engineer" that "ships, then tells the user what it shipped and
the evidence." It is not a ceremonial agent and not a nervous one: **the
machinery and the verbosity both scale with the work** — over-procedure on small
work is as much a failure as under-procedure on large work.

The prompt is an ordered stack; each section answers one question:

- **Environment** — `{cwd}` and `{skills}` (with explicit root order:
  `./skills/` → `./.agents/skills/` → `~/.agents/skills/`).
- **Project rules come first — AGENTS.md**: same sweep as `coding-pro.md`
  (repo root, `./.agents/`, `./docs/`, nested copies; re-check mtime on
  later turns), then scan the skills list. First contact runs one batched
  sweep; later turns refresh only what may have changed.
- **Skills — first check**: same rule as `coding-pro.md`, with the explicit
  root order for duplicate skill names (project-local beats global).
- **The size ladder**: T0 (trivial) → T1 (small) → T2 (medium) → T3 (large).
  Each rung sets the machinery AND the length of the reply — T0 gets one
  command and one line back; T3 gets a full report. The two failure modes
  are named: T3 ceremony on a T0 task, and a T3 task shipped in a T0
  paragraph.
- **Ask or decide?**: ask only when readings produce **different
  deliverables**, no convention settles it, and it is **not easily
  reversible**; decide everything else (naming, formatting, library choice,
  API shape, test placement, equivalent implementations) and log it.
- **Subagent roster (top-level tags)**: file-picker / code-searcher / directory-lister / researcher / thinker / editor / reviewer / validator — each a named tag in the file. Role-specific behavior is encoded in each tag's body: a file-picker does not edit; an editor does not review its own work; a validator does not change code to make a check pass.
- **Web search and fetching**: `curl` with DuckDuckGo HTML is recommended;
  the researcher role carries the same executable search recipe.

### `coding-ultra.md`: MAX-mode execution orchestrator

The largest bundled style. Persona: "**ultra**, a relentless execution
engine" — it does not stop, does not negotiate, and does not ask the user
mid-task; it picks reasonable defaults, documents them, and loops until the
task is **verifiably complete** or hits a **true hard block** (soft blocks
are decided autonomously). It requires the following behaviors:

- **Startup step**: same AGENTS.md-first read as `coding-pro.md`, **plus**
  `./.miniouto/docs/` — if that directory exists, read its `INDEX.md` and the
  docs relevant to the task before acting.
- **Zero-excuse protocol** (load-bearing): never stop early, never ask
  permission mid-loop, never ask low-stakes design questions (pick the most
  defensible option, log it in the plan's DECISIONS section), never report
  partial success as success, never give up on a verification or subagent
  failure (re-brief and respawn), never fabricate progress, never expand
  scope without a reason.
- **MAX-mode operating principle**: spawn more subagents than you think you
  need — at least 3 file-picker subagents on the first context-gathering
  layer (plus searcher / glob / researchers), a second context pass with
  different prompts, 2–3 **best-of-N editors** with materially different
  strategies (pick or synthesize), 3–5 **multi-focus reviewers** (security /
  performance / edge cases / test coverage / API design), a thinker or
  deep-thinker for non-obvious design decisions, a validator, and a verifier.
  Stated cost: 5–8x more tokens in exchange for meaningfully better output.
- **PARALLEL TOOL CALLS mechanics**: an expanded version of `coding-pro.md`'s
  section — every layer's N subagents go out as N `miniouto chat --role` commands,
  each with one brief, issued together (mixed-role layers like an editor
  + a reviewer and same-role layers like three file-pickers are both just
  several calls), with a concrete worked example (a six-call delegation batch for
  "add rate limiting to the auth endpoints"), a mandatory self-check after
  every layer, and the "Handling results" contract (never wait, never poll,
  never re-ask; read the file the alarm names).
- **Subagent roster (top-level tags)**: file-picker, code-searcher, glob-matcher, directory-lister, researcher-docs, researcher-web, commander, thinker, deep-thinker, generate-plan, editor, code-reviewer, validator, verifier, context-pruner, doc-reviewer, doc-bootstrapper — each a named tag in the file. Per-role tool whitelists and expected output shapes live in each tag's body.
- **Canonical layer sequence (0–9)**: project onboarding → heavy context
  gathering → second-pass context → deep thinking + plan generation →
  best-of-N editors → multi-focus review → validation → final verification →
  **documentation sync** → one-sentence final report. Layers are load-bearing
  and ordered; validation failure loops back to the editor layer.
- **The `.miniouto/docs/` documentation system**: a persistent project doc
  tree (INDEX, architecture/, decisions/ ADRs, api/, setup/, runbooks/,
  changelog/, session-notes/, plans/, reports/) that the orchestrator owns
  and keeps in lockstep with the code — Layer 8 sync is **non-skippable** on
  any non-trivial task. Includes a `docs` keyword trigger (Korean 포함:
  `문서화`, `문서 정리`…) for doc-only sessions and a bootstrap flow when the
  tree does not exist yet.
- **Worktree by default**: non-trivial changes happen in an isolated git
  worktree under `./.miniouto/worktrees/<task>/`; the user decides when to
  merge. Checkpoint commits after every verified milestone; a boulder state
  JSON (`BOULDER.json`) tracks milestones for long-running, interruptible
  work.
- **Domain probes**: when intent is ambiguous, dispatch 2–3 parallel thinker
  probes on plausible interpretations and synthesize, instead of stopping to
  ask.
- **Three-strike rule**: third failure of the same approach ⇒ switch strategy
  entirely; only after three *structurally different* failures is it a true
  hard block.
- **6-section delegation brief** at the top of every brief (the role name is selected via `name=`, not as a `[ROLE: …]` tag inside the brief text), a subagent retry protocol (fresh spawn in a new layer, never chained), and a self-correction protocol (recognize serialization, single-subagent layers, skipped doc sync, etc. and fix them).
- **Hard blocks**: the same absolute prohibitions as `coding-pro.md` (no
  sudo, no mass delete, no fabrication, no silent scope expansion, no
  unprompted commit/push/publish, no silent destruction of user work).
- **The deliverable follows the request type**: zero-excuse execution targets the *requested* deliverable — investigations complete as findings reports (Layer 9), and authorization does not carry across turns.
- **Execution know-how**: convergence as the layer-advance signal (never re-run a search a searcher returned); Layer 7 exercises the real surface; hypothesis-driven debugging with a `deep-thinker` consult after two failed fixes; one objective per brief.
- **Web search and fetching**: `curl` with DuckDuckGo HTML is recommended;
  researcher-web carries the same executable search recipe.

---

## Editing / creating styles

### Edit an existing style

```bash
$EDITOR ~/.miniouto/style/default.md   # or your active style
```

Changes take effect on the **next** chat turn. The TUI caches the active style name only; the prompt is rebuilt on each `build_runtime` call.

### Create a new style

```bash
cp ~/.miniouto/style/default.md ~/.miniouto/style/mystyle.md
$EDITOR ~/.miniouto/style/mystyle.md
miniouto style set mystyle
```

### Pull styles from a git repo

```bash
miniouto style add https://github.com/owner/repo
```

Fetches `https://api.github.com/repos/owner/repo/contents/style-md` (or GitLab equivalent) and copies every `*.md` into `~/.miniouto/style/`. Existing files with the same name are overwritten. The repo URL is recorded in `~/.miniouto/style_repos.toml` so it can be re-fetched later.

URL shapes accepted:
- `https://github.com/owner/repo` (auto-resolves to `/style-md/`)
- `https://github.com/owner/repo/tree/main/style-md`
- `https://gitlab.com/owner/repo` (auto-resolves to `/style-md/`)
- `https://gitlab.com/owner/repo/tree/main/style-md`
- Any URL whose directory listing exposes `<a href="*.md">` links (raw HTML fallback)

### Refresh all styles

```bash
miniouto style update
```

Re-seeds all bundled styles from the miniouto package (same force-refresh that `ensure_dirs()` does), then re-fetches every repo previously added via `style add` from `~/.miniouto/style_repos.toml`. Same-name files are overwritten in place. Styles you created by hand (no matching bundled template and no recorded repo) are left untouched. Per-repo failures are reported individually but do not abort the rest.

### Skills guidance in bundled styles

The larger templates (`default.md`, `coding-pro.md`, `coding-work.md`, `coding-ultra.md`) include a **Skills — MANDATORY first check** section in their `<outo>` half (immediately before the tools list) and a mirror section in each named subagent's tag body as applicable. It instructs the agent to scan the skill catalog injected into its context (a name + one-line description listing, sourced from the default roots — the project's `./skills/` and `~/.agents/skills/`) before starting any work, and — when a skill matches the work's domain — to `cat` that skill's SKILL.md (and any files it references) and follow it as the primary workflow, taking precedence over the style's default workflow. It also tells the agent to name the matching skill in the delegation brief so the subagent follows it too. The minimal `coding.md` compresses this to a single rule ("if a listed skill matches the work, read its SKILL.md and follow it") in each role's prompt. Keep some form of this guidance when authoring a custom style; it is what makes installed skills actually get used.

### Web access guidance in bundled styles

Every bundled orchestrator except `coding.md` recommends `curl` via Bash with DuckDuckGo's HTML endpoint (`https://html.duckduckgo.com/html/?q=...`) for web search — no JavaScript, parseable with `grep`/`sed`/`awk`. `default.md` includes the full **Web access (search & fetch)** recipe in its `<outo>` half and in its single legacy `<subagent>` half; `coding-pro.md`, `coding-work.md`, and `coding-ultra.md` include a concise orchestrator-level section, with the executable curl recipe also present in the web researcher roles of the latter two. Web access remains **skill-first**: a matching skill (browser automation, scraping, search, platform-specific APIs) takes precedence over raw `curl`. `coding.md` is deliberately unchanged by this guidance. Never guess content from an unfetched page; surface fetch failures.

### Background waiting guidance in bundled styles

The same four orchestrators distinguish independent work from waiting. After a `--bg` delegation with `--alarm-session`, do independent work while it is useful. When none remains, say you will wait for the completion message and **end the turn**, rather than polling status files, logs, the inbox, or the result file (or using `sleep`). A TUI inbox poll or `chat --alarms -c` wakes the session; then read the named result and verify it. Nested role prompts have the same exception to their ordinary final-message contract. `coding.md` is unchanged.

### Export / share a style

Just `cp ~/.miniouto/style/<name>.md some/path.md`. The file is fully self-contained.

---

## Known issues

1. **There is no `continue_loop` tool, by design.** coreouto's termination policy already covers progress updates: text AND a tool call keeps the loop going, text with NO tool call ends it. An earlier draft referenced a `continue_loop` tool that was never registered, which made the model invent calls that never executed — that reference has been removed everywhere (styles, docs, `_fallback_style`).
2. **A style with no named tags can still delegate.** `Bash` — and therefore a delegated `miniouto chat --role <tag>` run — is always registered, so an outo-only style has a working delegation surface; it just has **no roles to select**, and `--role <tag>` against it fails before the turn with `Available roles: (none)`. Add a top-level `<name>` body for each role you want `--role` to be able to pick.
