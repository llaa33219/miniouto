# Style Documents

A "style" in miniouto is a **Markdown system prompt** that defines the persona, operating principles, and tool usage rules for the agent. Styles are stored at `~/.miniouto/style/<name>.md` (seeded from the bundled `src/miniouto/default_style/` on first run).

The active style is selected via `miniouto style set <name>` (or per-call via `miniouto chat --style <name>`).

## File structure

A style document is plain Markdown with a **top-level XML structure**: one optional `<outo>` block (always the outo prompt) and zero or more named subagent blocks (each `<tag>` is one subagent). The tag name IS the subagent name.

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

Tags are then concatenated with:

- A **per-call cwd preamble** prepended on top (different wording for outo vs subagent).
- A **skill section** between the preamble and the style body.

So the final outo prompt the model sees, top to bottom, is:

1. cwd preamble: *"The user invoked miniouto from: {INVOCATION_CWD}…"*
2. A skill catalog from `~/.agents/skills/`: a `# Available Skills` block listing each skill as `- <name>: <description>` plus a note that full instructions live at `~/.agents/skills/<name>/SKILL.md` (bodies are read on demand via Bash — see `docs/skills.md`).
3. The `<outo>` section content (or the whole document if `<outo>` is missing).

Each named subagent prompt mirrors this with its own tag body and a different preamble: *"You operate inside this working directory: {INVOCATION_CWD}…"*.

**Legacy compat**: an old-style `<subagent>...</subagent>` block is parsed as a single subagent named `"subagent"`. Existing style files keep working unchanged; actor labels for those invocations stay `subagent-<6hex>`.

## `parse_style(content) -> tuple[str, list[SubagentSpec]]`

From `storage/styles.py`. Parses a style document with a single sequential scan over its top level:

```python
@dataclass(frozen=True)
class SubagentSpec:
    name: str      # the tag name, e.g. "editor" or "subagent"
    prompt: str    # the tag body (this subagent's system prompt)

def parse_style(content: str) -> tuple[str, list[SubagentSpec]]: ...
```

Rules:

- `<outo>...</outo>` is extracted as the first element of the returned tuple. If absent, the **whole document** is used as the outo prompt.
- Each **top-level** `<name>...</name>` pair outside `<outo>` becomes one `SubagentSpec`; the tag name IS the subagent name. Tag names match `[A-Za-z][A-Za-z0-9_-]*` (hyphens allowed: `file-picker`, `researcher-docs`).
- `outo` is reserved — always the outo prompt, never a subagent.
- A `<subagent>...</subagent>` block is parsed as a subagent named `"subagent"` (legacy compat).
- **Zero subagent tags → zero `SubagentSpec`s → the `call_subagent` tool is NOT registered at all** (outo-only style = no delegation surface). There is no fallback subagent prompt anymore.
- The parse is **top-level only**: a `<name>...</name>` pair nested inside another tag body is treated as content, not as a subagent.
- Stray prose outside any tag is ignored.
- Duplicate tag names at the top level raise `ValueError`.

## The tools section (required)

Every style should include a **Tools available** section that lists the tools the agent can call:

| Tool | Purpose |
|---|---|
| `Bash(command, *, cwd=None)` | Run a shell command — the ONLY file-manipulation tool (read via `cat`/`grep`, write via heredoc/`tee`, edit via `sed -i`/Python, delete via `rm`). 1-hour hard timeout (`BASH_TIMEOUT_SECONDS`). (`env` is accepted by the underlying `bash()` function but is **not** exposed through the model-facing schema — the registered `_bash_handler` does not accept it.) |
| `Image(file_path)` / `Video(file_path)` / `Audio(file_path)` | View/listen to a media file (multimodal blocks). |
| `call_subagent(name="", task="", tasks=None, briefs=None)` | Delegate one or more self-contained briefs to subagent(s). `name=""` resolves to `"subagent"` if declared, else the sole subagent if exactly one, else an error. `task` is a single brief; `tasks` is N parallel briefs to the SAME `name`; `briefs` is `list[{"name", "task"}]` for mixed-name fan-out (e.g. an editor + a reviewer spawned concurrently in one invocation). All briefs merge into one concurrent pool and return ONE numbered tool result. **Only registered when the active style declares ≥1 named subagent.** |

There are deliberately **no** Write/Edit/Delete tools — file work goes through Bash (see `docs/tools.md` § "Why Bash is the only file tool"). All bundled styles describe the tools with **identical behavior summaries** (so the model doesn't see inconsistent tool docs across styles). The summaries come from `tools/registry.py`'s `_<name>_description` strings.

## The loop behavior section (required)

Every style should also include a **Loop behavior** section with three rules:

1. **Termination**: when you've finished, your final message is plain text with no tool call.
2. **`continue_loop`**: to send a progress update to the user while still planning more tool calls, emit a tool call to `continue_loop` (a no-op tool shipped by some styles). This avoids "text-only mid-loop" messages that the model sometimes improvises.
3. **Tool results are loop input**: the result of a tool call is fed back to the model as the next iteration's input — it is **not** the user's response.

The `continue_loop` tool is referenced in styles but not actually wired into the tool registry by default. If you want the model to use it, register a no-op `continue_loop` tool in `tools/registry.py` and add it to `core/runtime.BASE_TOOLS`.

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

**Note on mechanics**: each bundled orchestrator style declares its roster as top-level named tags (one tag per role). At invocation time the model passes the role name via `call_subagent(name=...)` and the matching tag body is loaded as that subagent's system prompt — a per-role lean prompt replaces the old single shared subagent prompt. Mixed-role fan-out uses `call_subagent(briefs=[{"name": …, "task": …}, …])` (e.g. an editor and a reviewer concurrently in one invocation); same-name fan-out uses `call_subagent(tasks=[…])`. See each style file for its actual roster.

### `default.md` — minimal fallback

The original "outo" prompt. Short, opinionated, deliberately sparse. Used when no style is set, or when `miniouto style set default` is run.

Key points:
- 7 outo operating principles: be brief, lead with the answer, finish with text + no tool call (or use `continue_loop`), treat tool results as loop input, match delegation scope to task size, never invent outputs, match the user's language, pass paths correctly when delegating.
- A single legacy `<subagent>` block (named `"subagent"`): 10 principles — treat the brief as the whole spec (no clarifying questions), be terse, prefer targeted `sed`/Python edits over full-file rewrites, read first, return useful extracted output (not full dumps), plan + execute + synthesize for multi-step work, use `call_subagent` only when the subtask deserves its own context, finish with text + no tool call, tool results are loop input, match brief language, surface errors verbatim.

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
- **Six rules** (single legacy `<subagent>` block, named `"subagent"`): same core — brief is the whole spec, read first, Bash-only file work, verified reporting (files changed, commands + output, blockers), no unauthorized destructive actions, match the brief's language.

### `coding-pro.md`: senior staff engineer orchestrator

A senior staff engineer orchestrator. Persona: "**pro**, a senior staff engineer"
that is "a teammate, not a tutor." It requires the following behaviors:

- **Startup step — read AGENTS.md before anything else**: on every turn, check
  for an `AGENTS.md` / `AGENT.md` / `CLAUDE.md` / `CURSOR.md` / `.cursorrules`
  / `GEMINI.md` (repo root, `./.agents/`, `./.docs/`, or nested copies), read
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
  each named subagent's body (batch independent reads/commands of its own).
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
- **6-section delegation brief**: every `call_subagent(name=..., task=...)` (or each item in `briefs=[…]`) must carry TASK, EXPECTED OUTCOME, REQUIRED TOOLS, MUST DO, MUST NOT DO, CONTEXT — the subagent has no conversation history, so the brief is its entire spec.
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
- **Match the response to the request**: a deliverable-type table — "explain" → answer; "look into" → findings report then **stop** (an investigation is not implementation authorization, and authorization does not carry across turns); "what do you think" → judgment; "implement/fix" → shipped work.
- **Execution know-how**: destination / constraints / stopping-condition triad named before acting; delegated searches never duplicated (stop when sources converge); verification exercises the real surface (invoke the CLI, `curl` the endpoint, drive the TUI — build/lint/typecheck are necessary, not sufficient); hypothesis-driven debugging with a `thinker` consult after two failed fixes; one objective per brief (an "and also" in TASK = split it); final reports state residual risk.
- **Subagent roster (top-level tags)**: file-picker / thinker / editor / reviewer / validator — each a named tag in the file, loaded as that subagent's own system prompt.
- Like `coding-ultra.md` (and unlike `default.md`/`coding.md`), `coding-pro.md`
  has **no Web access section** — it relies on the skill catalog and plain
  `curl` judgment.

### `coding-work.md`: pragmatic senior engineer orchestrator

A pragmatic senior-software-engineer orchestrator (~38 KB) positioned between
`coding-pro.md` and `coding-ultra.md`: the autonomy of the heaviest execution
styles (never stop early, never ask permission mid-loop, decide soft blocks
yourself, three-strike rule) with **proportionate machinery** — the work is
**distributed by its scale**: what the orchestrator can read, understand, and
execute within a modest number of calls is done end-to-end itself (batched
parallel reads, direct edits, no subagent ceremony on small tasks);
large-scale understanding (unfamiliar codebase too broad to survey directly)
goes to parallel context subagents; large or multi-site changes go to one
focused editor subagent per independent slice — parallel spawns for
genuinely independent sites — with one reviewer when the change warrants it.
No documentation empire, no worktrees for everyday patches, no best-of-N
editor tournaments. Persona: "**coding-work**, a pragmatic senior software
engineer" that "ships, then tells the user what it shipped and the evidence."
It requires the following behaviors:

- **Startup step — read AGENTS.md before anything else**: same sweep as
  `coding-pro.md` (repo root, `./.agents/`, `./.docs/`, nested copies;
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
- **PARALLEL TOOL CALLS mechanics, two flavors**: Flavor 1 — batch your own
  independent reads/commands as N tool_use blocks in one response (the
  small-task case); Flavor 2 — parallel `call_subagent` blocks in one
  response for large splittable work (parallel context searches with
  different angles on a broad codebase, or one editor per independent site
  of a multi-site change). Ceremony on small tasks is the named
  anti-pattern; the self-check counts tool_use blocks after each batch.
- **Scale-based decision framework**: "read it, understand it, do it
  directly within a modest number of calls → do it yourself" is the first
  row; large-scale understanding, non-trivial execution, multi-site
  modification, and independent checking each map to their delegation
  action. The closing rule runs both directions — subagent ceremony on
  small tasks and hand-grinding oversized work are the same failure.
- **Five-phase workflow**: EXPLORE (direct batched-read exploration — the
  onboarding sweep runs as ONE batched block of read-only commands by the
  orchestrator itself; parallel context subagents when the tree is
  genuinely too broad to survey) → PLAN (`./.miniouto/plans/<name>.md` with
  soft-block decisions logged) → EXECUTE (read-and-do work goes direct;
  otherwise one `editor` subagent per slice — a multi-site change is one
  editor per independent site, emitted as one parallel batch, never
  overlapping edit ownership) → REVIEW (one `reviewer` subagent with a
  **single** focus area for risky changes — auth, money, concurrency,
  public API, migrations; self-review for routine ones) → VERIFY (real
  commands, read the diff yourself, loop).
- **Subagent roster (top-level tags)**: file-picker / code-searcher / directory-lister / researcher / thinker / editor / reviewer / validator — each a named tag in the file. Role-specific behavior is encoded in each tag's body: a file-picker does not edit; an editor does not review its own work; a validator does not change code to make a check pass.
- **6-section delegation brief** at the top of every brief (the role name is selected via `name=`, not as a `[ROLE: …]` tag inside the brief text), plus a subagent retry protocol (one respawn with a sharper brief, then take the slice over).
- **Status update format, definition of done, hard blocks, loop behavior**:
  the same discipline as `coding-pro.md` (Checkpoint / Verified / Changed /
  Remaining / Blocked; nothing is "done" until the real commands pass; no
  sudo, no mass delete, no fabrication, no silent scope expansion, no
  unprompted commit/push/publish).
- **Match the response to the request**: a deliverable-type table — "explain" → answer; "look into" → findings report then **stop** (an investigation is not implementation authorization, and authorization does not carry across turns); "what do you think" → judgment; "implement/fix" → shipped work.
- **Execution know-how**: destination / constraints / stopping-condition triad named before acting; delegated searches never duplicated (stop when sources converge); verification exercises the real surface (invoke the CLI, `curl` the endpoint, drive the TUI — build/lint/typecheck are necessary, not sufficient); hypothesis-driven debugging with a `thinker` consult after two failed fixes; one objective per brief (an "and also" in TASK = split it); final reports state residual risk.
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
  blocks in ONE assistant response (use `briefs=[…]` for mixed-role layers
  like an editor + a reviewer; use `tasks=[…]` for same-role layers like
  three file-pickers), with a concrete worked example (a six-block spawn
  batch for "add rate limiting to the auth endpoints") and a mandatory
  self-check after every layer.
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
  tasks.
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

The larger templates (`default.md`, `coding-pro.md`, `coding-work.md`, `coding-ultra.md`) include a **Skills — MANDATORY first check** section in their `<outo>` half (immediately before the tools list) and a mirror section in each named subagent's tag body as applicable. It instructs the agent to scan the skill catalog injected into its context (a name + one-line description listing, sourced from `~/.agents/skills/`) before starting any task, and — when a skill matches the task's domain — to `cat` that skill's SKILL.md (and any files it references) and follow it as the primary workflow, taking precedence over the style's default workflow. It also tells the agent to name the matching skill in delegation briefs so the subagent follows it too. The minimal `coding.md` compresses this to a single rule ("if a listed skill matches the task, read its SKILL.md and follow it") in each role's prompt. Keep some form of this guidance when authoring a custom style; it is what makes installed skills actually get used.

### Web access guidance in bundled styles

The `default.md` template includes a full **Web access (search & fetch)** section in its `<outo>` half and in its single legacy `<subagent>` half (`coding-pro.md`, `coding-work.md`, and `coding-ultra.md` do not; `coding.md` compresses it to one rule). It is **skill-first**: the agent must check whether an available skill covers the web interaction (browser automation, scraping, search, platform-specific APIs) and follow that skill when one applies. Only when no skill applies does it fall back to `curl` via Bash — searching the web via DuckDuckGo's HTML endpoint (`https://html.duckduckgo.com/html/?q=...`) — no JavaScript, parseable with `grep`/`sed`/`awk`. If you author a custom style and want the agent to fetch real pages instead of guessing content, copy this section from `default.md`.

### Export / share a style

Just `cp ~/.miniouto/style/<name>.md some/path.md`. The file is fully self-contained.

---

## Known issues

1. **`continue_loop` tool is referenced but not registered.** All bundled styles mention it as the way to "send progress to the user while still planning more tool calls." The coreouto integration would need a no-op tool registered in `tools/registry.py` and added to `core/runtime.BASE_TOOLS`. Models currently improvise (often emitting a tool-shaped message with no actual call), which can confuse some coreouto versions.
2. **Outo-only styles advertise no delegation surface.** A style with no `<name>` named-subagent tags (or with only a `<subagent>` tag and a typo elsewhere) yields zero `SubagentSpec`s, so `build_runtime` skips registering `call_subagent` entirely. The model simply has no way to delegate. If you want delegation, add at least one top-level named subagent tag.
