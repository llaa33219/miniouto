# Style Documents

A "style" in miniouto is a **Markdown system prompt** that defines the persona, operating principles, and tool usage rules for the agent. Styles are stored at `~/.miniouto/style/<name>.md` (seeded from the bundled `src/miniouto/default_style/` on first run).

The active style is selected via `miniouto style set <name>` (or per-call via `miniouto chat --style <name>`).

## File structure

A style document is plain Markdown with an optional XML structure:

```markdown
# Optional top-level title or notes (ignored by the parser)

<outo>
You are outo. You have full host access. …
[main agent persona + operating principles + tools list]
</outo>

<subagent>
You are subagent. You receive a brief and execute it directly. …
[delegated agent persona + operating principles]
</subagent>
```

Both blocks are concatenated with:

- A **per-call cwd preamble** prepended on top (different wording for outo vs subagent).
- A **skill section** between the preamble and the style body.

So the final outo prompt the model sees, top to bottom, is:

1. cwd preamble: *"The user invoked miniouto from: {INVOCATION_CWD}…"*
2. A skill catalog from `~/.agents/skills/`: a `# Available Skills` block listing each skill as `- <name>: <description>` plus a note that full instructions live at `~/.agents/skills/<name>/SKILL.md` (bodies are read on demand via Bash — see `docs/skills.md`).
3. The `<outo>` section content (or the whole document if `<outo>` is missing).

The subagent prompt mirrors this with the `<subagent>` section and a different preamble: *"You operate inside this working directory: {INVOCATION_CWD}…"*.

## `split_style(content) -> tuple[str, str]`

From `storage/styles.py`. Parses a style document:

- `<outo>...</outo>` is extracted as the first element. If absent, the **whole document** is used.
- `<subagent>...</subagent>` is extracted as the second element. If absent, an **empty string** is returned (and `core.runtime._fallback_style("subagent")` is used instead).

The regex is non-greedy and case-sensitive. Tags may appear in either order, but the standard convention is `<outo>` first then `<subagent>`.

## The tools section (required)

Every style should include a **Tools available** section that lists the tools the agent can call:

| Tool | Purpose |
|---|---|
| `Bash(command, *, cwd=None)` | Run a shell command — the ONLY file-manipulation tool (read via `cat`/`grep`, write via heredoc/`tee`, edit via `sed -i`/Python, delete via `rm`). 1-hour hard timeout (`BASH_TIMEOUT_SECONDS`). (`env` is accepted by the underlying `bash()` function but is **not** exposed through the model-facing schema — the registered `_bash_handler` does not accept it.) |
| `Image(file_path)` / `Video(file_path)` / `Audio(file_path)` | View/listen to a media file (multimodal blocks). |
| `call_subagent(task)` | Delegate a self-contained subtask to a fresh-context agent. |

There are deliberately **no** Write/Edit/Delete tools — file work goes through Bash (see `docs/tools.md` § "Why Bash is the only file tool"). All bundled styles describe the tools with **identical behavior summaries** (so the model doesn't see inconsistent tool docs across styles). The summaries come from `tools/registry.py`'s `_<name>_description` strings.

## The loop behavior section (required)

Every style should also include a **Loop behavior** section with three rules:

1. **Termination**: when you've finished, your final message is plain text with no tool call.
2. **`continue_loop`**: to send a progress update to the user while still planning more tool calls, emit a tool call to `continue_loop` (a no-op tool shipped by some styles). This avoids "text-only mid-loop" messages that the model sometimes improvises.
3. **Tool results are loop input**: the result of a tool call is fed back to the model as the next iteration's input — it is **not** the user's response.

The `continue_loop` tool is referenced in styles but not actually wired into the tool registry by default. If you want the model to use it, register a no-op `continue_loop` tool in `tools/registry.py` and add it to `core/runtime.ALL_TOOLS`.

---

## Bundled templates

The five bundled templates live in `src/miniouto/default_style/`. They are seeded into `~/.miniouto/style/` by `storage/paths.ensure_dirs`. Bundled styles are **force-refreshed**: every `ensure_dirs()` call overwrites any installed file whose name matches a bundled template with the current bundled content (written only when the content differs, to avoid needless disk churn). To customize a bundled style, copy it to a new name (e.g. `cp default.md mydefault.md`) — files whose names do not match a bundled template are never touched. Repo-added styles (via `style add`) are refreshed on demand with `style update`.

**Rename history (0.8.1)**: `pro.md` → `coding-pro.md` and `ultra.md` → `coding-ultra.md`. `ensure_dirs()` removes an old-name seeded copy only while it still matches the renamed bundle byte-for-byte (a user-customized old file survives as a regular user style) and repoints `settings.style` to the new name when it referenced the old one.

| File | Size | Persona | Orchestrator? |
|---|---|---|---|
| `default.md` | ~4 KB | "**outo**" — minimal, sparse | No (deliberately) |
| `coding.md` | ~1.6 KB | "**outo**" — expert coding agent, minimal ruleset | Light (delegation for non-trivial work) |
| `coding-pro.md` | ~32 KB | "**pro**" — senior staff engineer orchestrator | **Aggressive** (with a delegate-vs-DIY decision framework) |
| `coding-work.md` | ~38 KB | "**coding-work**" — pragmatic senior engineer orchestrator | **Aggressive but proportionate** (one subagent per slice, one reviewer when warranted) |
| `coding-ultra.md` | ~81 KB | "**ultra**" — relentless MAX-mode execution orchestrator | **Aggressive** (layered subagent fan-out, best-of-N editors, multi-focus review) |

`coding-pro.md`, `coding-work.md`, and `coding-ultra.md` include explicit guidance on when and how to delegate via `call_subagent`; `default.md` and `coding.md` are deliberately minimal.

### `default.md` — minimal fallback

The original "outo" prompt. Short, opinionated, deliberately sparse. Used when no style is set, or when `miniouto style set default` is run.

Key points:
- 7 outo operating principles: be brief, lead with the answer, finish with text + no tool call (or use `continue_loop`), treat tool results as loop input, match delegation scope to task size, never invent outputs, match the user's language, pass paths correctly when delegating.
- 10 subagent principles: treat the brief as the whole spec (no clarifying questions), be terse, prefer targeted `sed`/Python edits over full-file rewrites, read first, return useful extracted output (not full dumps), plan + execute + synthesize for multi-step work, use `call_subagent` only when the subtask deserves its own context, finish with text + no tool call, tool results are loop input, match brief language, surface errors verbatim.

### `coding.md`: expert coding agent, minimal

A deliberately terse style (~1.6 KB) — an "expert coding agent" persona with
only the load-bearing rules. Both halves are a short ruleset:

- **Identity**: "expert coding agent. Be precise, minimal, and verified."
- **Tools**: one line each — Bash as the ONLY file tool (with the
  read/create/edit/delete command forms), media viewers, Computer
  (outo-only), and `call_subagent` with self-contained briefs (parallel for
  independent tasks).
- **Seven rules** (outo half): follow matching skills; read before editing
  and follow project conventions; smallest change; verify with real
  commands and never fabricate; fetch web content instead of recalling it;
  ask before destructive/ambiguous moves; match the user's language.
- **Six rules** (subagent half): same core — brief is the whole spec, read
  first, Bash-only file work, verified reporting (files changed, commands
  + output, blockers), no unauthorized destructive actions, match the
  brief's language.

### `coding-pro.md`: senior staff engineer orchestrator

A senior staff engineer orchestrator. Persona: "**pro**, a senior staff engineer"
that is "a teammate, not a tutor." It requires the following behaviors:

- **Startup step — read AGENTS.md before anything else**: on every turn, check
  for an `AGENTS.md` / `AGENT.md` / `CLAUDE.md` / `CURSOR.md` / `.cursorrules`
  / `GEMINI.md` (repo root, `./.agents/`, `./docs/`, or nested copies), read
  them in the same response, and treat their content as **overriding** the
  style when in conflict (project rules win). Immediately after, scan the
  skills list and follow any matching skill (see below).
- **12 core operating principles**: lead with the answer, match depth to the
  task, delegate by default, parallelize independent work **as a single
  batched tool call**, verify with real evidence, surgical minimal changes,
  read before editing, preserve user work, no fabrication, match the user's
  language, stop and ask on material decisions, loop until done or
  hard-blocked.
- **PARALLEL TOOL CALLS — the actual mechanics**: a dedicated section teaching
  the exact parallelism mechanic — when spawning N independent subagents,
  emit ALL N `call_subagent` tool_use blocks in a **single assistant
  response** (not one per turn, not interleaved with text or Bash calls),
  with worked wrong/right patterns and a self-check ("count the tool_use
  blocks; fewer than N means you serialized"). Includes when you *cannot*
  batch (true data dependencies sequence across turns) and a mirror rule for
  the subagent half (batch independent reads/commands of its own).
- **Delegate-vs-DIY decision framework**: a signal table (one quick read → do
  it yourself; multi-file/multi-step/investigative → `call_subagent`; two or
  more independent subtasks → one batched parallel call), with an explicit
  ban on stringing together many small direct actions to avoid delegation.
- **Five-stage execution loop**: EXPLORE → PLAN → EXECUTE → VERIFY → LOOP,
  with a project onboarding sweep for first contact.
- **Plan-file lifecycle**: work that needs a plan goes to
  `./.miniouto/plans/<name>.md`, plus optional `CHECKPOINTS.md` and
  `DECISIONS.md` companions (mirrors Karpathy's PLAN / EXPERIMENTS / NOTES
  pattern).
- **6-section delegation brief**: every `call_subagent(task)` must carry TASK,
  EXPECTED OUTCOME, REQUIRED TOOLS, MUST DO, MUST NOT DO, CONTEXT — the
  subagent has no conversation history, so the brief is its entire spec.
- **Definition of done**: an explicit checklist (build/lint/typecheck/test
  exit 0, minimal diff, plan file updated, subagent claims confirmed by
  reading changed files, parallel batches actually emitted as N tool_use
  blocks in one response, skills list actually scanned).
- **Status update format**: Checkpoint / Verified / Changed / Remaining /
  Blocked — vague updates like "working on it" are forbidden.
- **Hard blocks**: no sudo or system-level changes (must hand the command to
  the user), no mass or destructive operations without authorization, no
  fabrication, no silent scope expansion, no commit/push/publish without
  explicit instruction, no silent destruction of user work.
- Like `coding-ultra.md` (and unlike `default.md`/`coding.md`), `coding-pro.md`
  has **no Web access section** — it relies on the skill catalog and plain
  `curl` judgment.

### `coding-work.md`: pragmatic senior engineer orchestrator

A pragmatic senior-software-engineer orchestrator (~38 KB) positioned between
`coding-pro.md` and `coding-ultra.md`: the autonomy of the heaviest execution
styles (never stop early, never ask permission mid-loop, decide soft blocks
yourself, three-strike rule) with **proportionate machinery** — one focused
subagent per slice, one reviewer only when the change warrants it, no
documentation empire, no worktrees for everyday patches, no best-of-N editor
tournaments. Persona: "**coding-work**, a pragmatic senior software engineer"
that "ships, then tells the user what it shipped and the evidence." It
requires the following behaviors:

- **Startup step — read AGENTS.md before anything else**: same sweep as
  `coding-pro.md` (repo root, `./.agents/`, `./docs/`, nested copies;
  re-check mtime on later turns), then scan the skills list.
- **Autonomy protocol (load-bearing)**: eight rules — never stop early, never
  ask permission mid-loop, decide soft blocks yourself (naming, library
  choice, API shape — pick the defensible default, log it in the plan),
  never report partial success as success, never give up on a failure
  (re-brief and respawn a failed subagent once, then take the slice over),
  three-strike rule, never fabricate progress, never expand scope. Defines
  the only four **true hard blocks** (missing credentials, unauthorized
  destructive action, contradictory instructions, unreachable verification
  surface) — everything else is decided autonomously.
- **PARALLEL TOOL CALLS mechanics**: the same wrong/right patterns and
  self-check as `coding-pro.md`, mirrored in the subagent half for its own
  batched reads.
- **Five-phase workflow**: EXPLORE (parallel role-tagged onboarding sweep on
  first contact) → PLAN (`./.miniouto/plans/<name>.md` with soft-block
  decisions logged) → EXECUTE (one `editor` subagent per slice; parallel
  editors only for genuinely independent slices, never overlapping edit
  ownership) → REVIEW (one `reviewer` subagent with a **single** focus area
  for risky changes — auth, money, concurrency, public API, migrations;
  self-review for routine ones) → VERIFY (real commands, read the diff
  yourself, loop).
- **Subagent roster**: eight named roles — file-picker, code-searcher,
  directory-lister, researcher (fetch real sources with `curl`), thinker,
  editor (the only role that modifies files), reviewer (one focus area,
  severity-tagged findings), validator — with a matching **Role-specific
  behavior** section in the subagent half ("a file-picker does not edit; an
  editor does not review its own work; a validator does not change code to
  make a check pass").
- **6-section delegation brief with a `[ROLE: …]` tag** at the top, plus a
  subagent retry protocol (one respawn with a sharper brief, then take the
  slice over).
- **Status update format, definition of done, hard blocks, loop behavior**:
  the same discipline as `coding-pro.md` (Checkpoint / Verified / Changed /
  Remaining / Blocked; nothing is "done" until the real commands pass; no
  sudo, no mass delete, no fabrication, no silent scope expansion, no
  unprompted commit/push/publish).
- Like `coding-pro.md` and `coding-ultra.md`, it has **no Web access
  section** — its researcher role covers external lookups via the skill
  catalog and Bash `curl` judgment.

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
  section — every layer's N subagents go out as N `call_subagent` tool_use
  blocks in ONE assistant response, with a concrete worked example (a
  six-block spawn batch for "add rate limiting to the auth endpoints") and a
  mandatory self-check after every layer.
- **Subagent roster**: named, reusable roles with whitelisted tools and
  expected output shapes — file-picker, code-searcher, glob-matcher,
  directory-lister, researcher-docs, researcher-web, commander, thinker,
  deep-thinker, generate-plan, editor, code-reviewer, validator, verifier,
  context-pruner, doc-reviewer, doc-bootstrapper.
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
  tasks.
- **Domain probes**: when intent is ambiguous, dispatch 2–3 parallel thinker
  probes on plausible interpretations and synthesize, instead of stopping to
  ask.
- **Three-strike rule**: third failure of the same approach ⇒ switch strategy
  entirely; only after three *structurally different* failures is it a true
  hard block.
- **6-section delegation brief with a role tag** (`[ROLE: …]`), a subagent
  retry protocol (fresh spawn in a new layer, never chained), and a
  self-correction protocol (recognize serialization, single-subagent layers,
  skipped doc sync, etc. and fix them).
- **Hard blocks**: the same absolute prohibitions as `coding-pro.md` (no
  sudo, no mass delete, no fabrication, no silent scope expansion, no
  unprompted commit/push/publish, no silent destruction of user work).
- Like `coding-pro.md`, `coding-ultra.md` has **no Web access section** — its
  researcher-docs / researcher-web roles rely on the skill catalog and Bash
  `curl` judgment.

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

The larger templates (`default.md`, `coding-pro.md`, `coding-work.md`, `coding-ultra.md`) include a **Skills — MANDATORY first check** section in both their `<outo>` and `<subagent>` halves (immediately before the tools list). It instructs the agent to scan the skill catalog injected into its context (a name + one-line description listing, sourced from `~/.agents/skills/`) before starting any task, and — when a skill matches the task's domain — to `cat` that skill's SKILL.md (and any files it references) and follow it as the primary workflow, taking precedence over the style's default workflow. It also tells the agent to name the matching skill in delegation briefs so the subagent follows it too. The minimal `coding.md` compresses this to a single rule ("if a listed skill matches the task, read its SKILL.md and follow it") in each half. Keep some form of this guidance when authoring a custom style; it is what makes installed skills actually get used.

### Web access guidance in bundled styles

The `default.md` template includes a full **Web access (search & fetch)** section in both its `<outo>` and `<subagent>` halves (`coding-pro.md`, `coding-work.md`, and `coding-ultra.md` do not; `coding.md` compresses it to one rule). It is **skill-first**: the agent must check whether an available skill covers the web interaction (browser automation, scraping, search, platform-specific APIs) and follow that skill when one applies. Only when no skill applies does it fall back to `curl` via Bash — searching the web via DuckDuckGo's HTML endpoint (`https://html.duckduckgo.com/html/?q=...`) — no JavaScript, parseable with `grep`/`sed`/`awk`. If you author a custom style and want the agent to fetch real pages instead of guessing content, copy this section from `default.md`.

### Export / share a style

Just `cp ~/.miniouto/style/<name>.md some/path.md`. The file is fully self-contained.

---

## Known issues

1. **`continue_loop` tool is referenced but not registered.** All bundled styles mention it as the way to "send progress to the user while still planning more tool calls." The coreouto integration would need a no-op tool registered in `tools/registry.py` and added to `core/runtime.ALL_TOOLS`. Models currently improvise (often emitting a tool-shaped message with no actual call), which can confuse some coreouto versions.
2. **Style override semantics for `<subagent>` are asymmetric.** Missing `<subagent>` → uses `_fallback_style("subagent")` (hardcoded minimal prompt). Missing `<outo>` → uses the whole document. If you author a style without `<subagent>` and rely on the fallback, check `core/runtime.py:_fallback_style("subagent")` to confirm what the subagent actually receives.
