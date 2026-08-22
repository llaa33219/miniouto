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

The three bundled templates live in `src/miniouto/default_style/`. They are seeded into `~/.miniouto/style/` by `storage/paths.ensure_dirs`. Bundled styles are **force-refreshed**: every `ensure_dirs()` call overwrites any installed file whose name matches a bundled template with the current bundled content (written only when the content differs, to avoid needless disk churn). To customize a bundled style, copy it to a new name (e.g. `cp default.md mydefault.md`) — files whose names do not match a bundled template are never touched. Repo-added styles (via `style add`) are refreshed on demand with `style update`.

| File | Size | Persona | Orchestrator? |
|---|---|---|---|
| `default.md` | ~4 KB | "**outo**" — minimal, sparse | No (deliberately) |
| `coding.md` | ~14 KB | "**coding expert**" orchestrator | **Aggressive** |
| `pro.md` | ~25 KB | "**pro**" — senior staff engineer orchestrator | **Aggressive** (with a delegate-vs-DIY decision framework) |

`coding.md` and `pro.md` include explicit guidance on when and how to delegate via `call_subagent`; `default.md` is deliberately minimal.

### `default.md` — minimal fallback

The original "outo" prompt. Short, opinionated, deliberately sparse. Used when no style is set, or when `miniouto style set default` is run.

Key points:
- 7 outo operating principles: be brief, lead with the answer, finish with text + no tool call (or use `continue_loop`), treat tool results as loop input, match delegation scope to task size, never invent outputs, match the user's language, pass paths correctly when delegating.
- 10 subagent principles: treat the brief as the whole spec (no clarifying questions), be terse, prefer targeted `sed`/Python edits over full-file rewrites, read first, return useful extracted output (not full dumps), plan + execute + synthesize for multi-step work, use `call_subagent` only when the subtask deserves its own context, finish with text + no tool call, tool results are loop input, match brief language, surface errors verbatim.

### `coding.md`: coding expert orchestrator

A professional coding expert orchestrator focused on flawless, systematic,
production-grade software work. It requires the following behaviors:

- **Delegation-first**: delegate any task that is even slightly complex,
  including multi-file, multi-step, non-obvious, investigative, planned, or
  higher-risk work, through `call_subagent(task)`.
- **Parallel calls**: issue independent `call_subagent` invocations in the
  same turn, rather than handling independent subtasks sequentially.
- **Project onboarding**: on first contact, survey the directory layout,
  project state, build and test configuration, README, and any `AGENT.md`,
  `AGENTS.md`, `CURSOR.md`, or `CLAUDE.md` files before doing the work.
- **Plan-file lifecycle**: write planned work to exactly
  `./.miniouto/plans/<name>.md`, keep it updated with progress and revisions,
  and delete it only after the task is fully complete and verified.
- **Ask the user**: stop and ask a plain-text final question when a required
  design or intent decision cannot be resolved from evidence. Never guess.
- **Quality and verification**: hold subagents to the same professional bar,
  read before editing, follow project conventions, and report only results
  confirmed by real build, lint, typecheck, test, or execution runs.

### `pro.md`: senior staff engineer orchestrator

The most complete bundled style. Persona: "**pro**, a senior staff engineer"
that is "a teammate, not a tutor." It requires the following behaviors:

- **12 core operating principles**: lead with the answer, match depth to the
  task, delegate by default, parallelize independent work, verify with real
  evidence, surgical minimal changes, read before editing, preserve user work,
  no fabrication, match the user's language, stop and ask on material
  decisions, loop until done or hard-blocked.
- **Delegate-vs-DIY decision framework**: a signal table (one quick read → do
  it yourself; multi-file/multi-step/investigative → `call_subagent`; two or
  more independent subtasks → parallel calls in the same turn), with an
  explicit ban on stringing together many small direct actions to avoid
  delegation.
- **Five-stage execution loop**: EXPLORE → PLAN → EXECUTE → VERIFY → LOOP,
  with a project onboarding sweep for first contact.
- **Plan-file lifecycle**: same `./.miniouto/plans/<name>.md` convention as
  `coding.md`, plus optional `CHECKPOINTS.md` and `DECISIONS.md` companions
  (mirrors Karpathy's PLAN / EXPERIMENTS / NOTES pattern).
- **6-section delegation brief**: every `call_subagent(task)` must carry TASK,
  EXPECTED OUTCOME, REQUIRED TOOLS, MUST DO, MUST NOT DO, CONTEXT — the
  subagent has no conversation history, so the brief is its entire spec.
- **Definition of done**: an explicit checklist (build/lint/typecheck/test
  exit 0, minimal diff, plan file updated, subagent claims confirmed by
  reading changed files).
- **Status update format**: Checkpoint / Verified / Changed / Remaining /
  Blocked — vague updates like "working on it" are forbidden.
- **Hard blocks**: no sudo or system-level changes (must hand the command to
  the user), no mass or destructive operations without authorization, no
  fabrication, no silent scope expansion, no commit/push/publish without
  explicit instruction, no silent destruction of user work.
- Unlike the other two bundles, `pro.md` has **no Web access section** — it
  relies on the skill catalog and plain `curl` judgment.

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

Every bundled template includes a **Skills — MANDATORY first check** section in both its `<outo>` and `<subagent>` halves (immediately before the tools list). It instructs the agent to scan the skill catalog injected into its context (a name + one-line description listing, sourced from `~/.agents/skills/`) before starting any task, and — when a skill matches the task's domain — to `cat` that skill's SKILL.md (and any files it references) and follow it as the primary workflow, taking precedence over the style's default workflow. It also tells the agent to name the matching skill in delegation briefs so the subagent follows it too. Keep this section when authoring a custom style; it is what makes installed skills actually get used.

### Web access guidance in bundled styles

The `default.md` and `coding.md` templates include a **Web access (search & fetch)** section in both their `<outo>` and `<subagent>` halves (`pro.md` does not — see its section above). It is **skill-first**: the agent must check whether an available skill covers the web interaction (browser automation, scraping, search, platform-specific APIs) and follow that skill when one applies. Only when no skill applies does it fall back to `curl` via Bash — searching the web via DuckDuckGo's HTML endpoint (`https://html.duckduckgo.com/html/?q=...`) — no JavaScript, parseable with `grep`/`sed`/`awk`. If you author a custom style and want the agent to fetch real pages instead of guessing content, copy this section from any bundled template.

### Export / share a style

Just `cp ~/.miniouto/style/<name>.md some/path.md`. The file is fully self-contained.

---

## Known issues

1. **`continue_loop` tool is referenced but not registered.** All bundled styles mention it as the way to "send progress to the user while still planning more tool calls." The coreouto integration would need a no-op tool registered in `tools/registry.py` and added to `core/runtime.ALL_TOOLS`. Models currently improvise (often emitting a tool-shaped message with no actual call), which can confuse some coreouto versions.
2. **Style override semantics for `<subagent>` are asymmetric.** Missing `<subagent>` → uses `_fallback_style("subagent")` (hardcoded minimal prompt). Missing `<outo>` → uses the whole document. If you author a style without `<subagent>` and rely on the fallback, check `core/runtime.py:_fallback_style("subagent")` to confirm what the subagent actually receives.
