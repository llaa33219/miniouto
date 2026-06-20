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
2. All active skills from `~/.agents/skills/`, formatted as `# Skill: <name>\n\n<content>` joined by `\n\n---\n\n`.
3. The `<outo>` section content (or the whole document if `<outo>` is missing).

The subagent prompt mirrors this with the `<subagent>` section and a different preamble: *"You operate inside this working directory: {INVOCATION_CWD}…"*.

## `split_style(content) -> tuple[str, str]`

From `storage/styles.py`. Parses a style document:

- `<outo>...</outo>` is extracted as the first element. If absent, the **whole document** is used.
- `<subagent>...</subagent>` is extracted as the second element. If absent, an **empty string** is returned (and `core.runtime._fallback_style("subagent")` is used instead).

The regex is non-greedy and case-sensitive. Tags may appear in either order, but the standard convention is `<outo>` first then `<subagent>`.

## The five tools section (required)

Every style should include a **Tools available** section that lists the five tools the agent can call:

| Tool | Purpose |
|---|---|
| `Write(file_path, content)` | Create a new file (refuses overwrite). |
| `Edit(file_path, edits)` | Apply search/replace ops to an existing file. |
| `Delete(file_path)` | Remove a file or empty directory. |
| `Bash(command, *, timeout_seconds=60, cwd=None, env=None)` | Run a shell command. |
| `call_subagent(task)` | Delegate a self-contained subtask to a fresh-context agent. |

All bundled styles describe these with **identical behavior summaries** (so the model doesn't see inconsistent tool docs across styles). The summaries come from `tools/registry.py`'s `_<name>_description` strings.

## The loop behavior section (required)

Every style should also include a **Loop behavior** section with three rules:

1. **Termination**: when you've finished, your final message is plain text with no tool call.
2. **`continue_loop`**: to send a progress update to the user while still planning more tool calls, emit a tool call to `continue_loop` (a no-op tool shipped by some styles). This avoids "text-only mid-loop" messages that the model sometimes improvises.
3. **Tool results are loop input**: the result of a tool call is fed back to the model as the next iteration's input — it is **not** the user's response.

The `continue_loop` tool is referenced in styles but not actually wired into the tool registry by default. If you want the model to use it, register a no-op `continue_loop` tool in `tools/registry.py` and add it to `core/runtime.ALL_TOOLS`.

---

## Bundled templates

All six bundled templates live in `src/miniouto/default_style/`. They are seeded into `~/.miniouto/style/` on first run by `storage/paths.ensure_dirs` — only if a file of the same name doesn't already exist (your edits survive reinstalls).

| File | Size | Persona | Orchestrator? | Sub-roles |
|---|---|---|---|---|
| `default.md` | ~4 KB | "**outo**" — minimal, sparse | No (deliberately) | n/a |
| `claude.md` | ~15 KB | Claude Code-style | Yes (mild) | Explore / Plan / General-purpose |
| `codex.md` | ~17 KB | OpenAI Codex CLI-style | Yes | File picker / Code searcher / Researcher / Editor / Code reviewer / Basher |
| `opencode.md` | ~10 KB | OpenCode-style | No | (delegates ad-hoc) |
| `oh-my-opencode.md` | ~13 KB | "**Sisyphus**" | **Aggressive** | Explorer / Researcher / Planner / Advisor / Reviewer / Editor / Basher |
| `codebuff.md` | ~11 KB | "**Buffy**" | Yes | File picker / Code searcher / Researcher / Editor / Code reviewer / Basher |

The orchestration styles (claude/codex/codebuff/oh-my-opencode) include explicit guidance on when and how to delegate via `call_subagent`. `default.md` and `opencode.md` are more minimal.

### `default.md` — minimal fallback

The original "outo" prompt. Short, opinionated, deliberately sparse. Used when no style is set, or when `miniouto style set default` is run.

Key points:
- 7 outo operating principles: be brief, lead with the answer, finish with text + no tool call (or use `continue_loop`), treat tool results as loop input, match delegation scope to task size, never invent outputs, match the user's language, pass paths correctly when delegating.
- 10 subagent principles: treat the brief as the whole spec (no clarifying questions), be terse, prefer Edit over Write, read first, return useful extracted output (not full dumps), plan + execute + synthesize for multi-step work, use `call_subagent` only when the subtask deserves its own context, finish with text + no tool call, tool results are loop input, match brief language, surface errors verbatim.

### `claude.md` — Claude Code-style

Long, structured, opinionated about communication style. Identity: "an interactive agent that helps users with software engineering tasks."

Sections include: harness, communication style, outcome-first communication, executing actions with care, code editing mandates, doing tasks, comment guidelines. Includes a description of a `claude.md` CWD memory file (**not actually wired up** — see Known issues below).

### `codex.md` — OpenAI Codex CLI-style

The longest style. Personality: "precise, safe, helpful." Strong emphasis on preamble messages before tool calls.

Includes: preamble messages culture, editing constraints (ASCII default, dirty worktree rules, never `git reset --hard`, never `git checkout --`), frontend tasks (anti-AI-slop rules on typography/color/motion/backgrounds), validating your work, presenting your work, file references (clickable paths with line/column).

### `opencode.md` — OpenCode-style

Concise. Identity: "an interactive CLI tool that helps users with software engineering tasks."

Tone-and-style section is the strongest part: extremely short answers (1–3 sentences preferred, fewer than 4 lines by default, one-word answers are best). Proactiveness rules are clear: proactive when asked, don't surprise the user.

### `oh-my-opencode.md` — Sisyphus orchestrator

The most aggressively orchestrator-focused. Critical identity constraint: "YOU ARE AN ORCHESTRATOR. YOU PLAN AND DELEGATE. YOU DO NOT WRITE CODE DIRECTLY (unless trivially simple)."

Seven sub-agent roles with explicit operating modes:
- **Explorer** (READ-ONLY) — search codebase, return ABSOLUTE paths.
- **Researcher** (READ-ONLY) — web/docs; date awareness required.
- **Planner** (READ-ONLY) — 5–7 word step headings.
- **Advisor** (READ-ONLY) — three-tier response (Essential / Expanded / Edge cases); confidence signal.
- **Reviewer** (READ-ONLY) — blockers only (max 3), APPROVE-biased.
- **Editor** — implementer; conventions, Edit-not-Write.
- **Basher** — shell runner.

Includes a Decision Framework (effort tag: Quick<1h / Short 1-4h / Medium 1-2d / Large 3d+) and AI-Slop Avoidance section.

### `codebuff.md` — Buffy orchestrator

"Buffy, a strategic assistant that orchestrates complex coding tasks through specialized sub-agents."

Same six sub-agent roles as codex (no Planner/Advisor). Stronger emphasis on quality-over-speed: "fewer, well-informed agents > many rushed ones."

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

Fetches `https://api.github.com/repos/owner/repo/contents/style-md` (or GitLab equivalent) and copies every `*.md` into `~/.miniouto/style/`. Existing files with the same name are overwritten.

URL shapes accepted:
- `https://github.com/owner/repo` (auto-resolves to `/style-md/`)
- `https://github.com/owner/repo/tree/main/style-md`
- `https://gitlab.com/owner/repo` (auto-resolves to `/style-md/`)
- `https://gitlab.com/owner/repo/tree/main/style-md`
- Any URL whose directory listing exposes `<a href="*.md">` links (raw HTML fallback)

### Export / share a style

Just `cp ~/.miniouto/style/<name>.md some/path.md`. The file is fully self-contained.

---

## Known issues

1. **CWD memory files are described but not implemented.** Four styles (`claude.md`, `codex.md`, `opencode.md`, `oh-my-opencode.md`) describe a `<style-name>.md` file in the user's CWD that the agent should read/write as persistent memory. **No such loader exists in miniouto.** If you want this feature, implement it in `core/runtime.py:_load_active_skills()` (or a sibling) and update the styles to match the actual file name.
2. **`continue_loop` tool is referenced but not registered.** All bundled styles mention it as the way to "send progress to the user while still planning more tool calls." The coreouto integration would need a no-op tool registered in `tools/registry.py` and added to `core/runtime.ALL_TOOLS`. Models currently improvise (often emitting a tool-shaped message with no actual call), which can confuse some coreouto versions.
3. **Style override semantics for `<subagent>` are asymmetric.** Missing `<subagent>` → uses `_fallback_style("subagent")` (hardcoded minimal prompt). Missing `<outo>` → uses the whole document. If you author a style without `<subagent>` and rely on the fallback, check `core/runtime.py:_fallback_style("subagent")` to confirm what the subagent actually receives.
