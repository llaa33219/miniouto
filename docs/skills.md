# Skills

Skills are **portable, project-shared context blocks** that are prepended to every style's prompt at runtime. Unlike styles (which are per-installation and live in `~/.miniouto/`), skills live in `~/.agents/skills/` — a separate location that follows the Anthropic skill convention. This lets you check skills into version control alongside the project they describe.

## Layout

```
~/.agents/skills/                        # NOT ~/.miniouto/
└── <skill-name>/
    └── SKILL.md
```

A skill is a single directory containing one `SKILL.md` file. The directory name is the skill's identifier.

## `SKILL.md` format

```markdown
---
name: my-skill                # required
description: Short description of what this skill does and when to use it. Required.
license: Apache-2.0           # optional
allowed-tools: Bash,Write     # optional, comma-separated
hidden: false                 # optional, default false
---

# Body

The actual skill content goes here. This is the text that gets prepended
to every style's prompt at runtime. Markdown is rendered into the system
prompt as-is.
```

### Frontmatter

The frontmatter is parsed by `storage/skills.py:_parse_skill` using a regex-based extractor (not a YAML library — kept minimal to avoid pulling in `pyyaml`). Each key is on a single line:

```
key: value
```

Multi-line values are not supported.

| Key | Required | Type | Purpose |
|---|---|---|---|
| `name` | yes | str | Skill identifier (should match directory name). |
| `description` | yes | str | Shown in `miniouto skill list` and `miniouto skill show`. |
| `license` | no | str | SPDX identifier or similar. |
| `allowed-tools` | no | str (comma-separated) | Hint to the orchestrator about which tools this skill needs. |
| `hidden` | no | bool (`true`/`false`) | If `true`, the skill is **not** prepended to prompts and is **excluded from `skill list`**. (`skill show <name>` still works on a hidden skill by explicit name.) Only the literal string `"true"` sets the field — anything else (including `"True"`, `"1"`) is treated as `false`. |

If `name` or `description` is missing, the skill is skipped (parsed as `None`).

### Body

Free-form Markdown. This is the actual prompt content. Skills typically describe:
- A specific project's architecture and conventions.
- A workflow or methodology the agent should follow.
- A coding style guide.
- A list of allowed tools or commands.

## How skills are loaded

In `core/runtime.py:_load_active_skills()`:

```python
def _load_active_skills() -> str:
    skills = skill_store.list_skills()        # already excludes hidden skills
    if not skills:
        return ""
    parts: list[str] = []
    for skill in skills:
        if skill.content:                     # skip empty-content skills
            parts.append(f"# Skill: {skill.name}\n\n{skill.content}")
    return "\n\n---\n\n".join(parts)
```

Every non-hidden skill (with non-empty content) is concatenated as `# Skill: <name>\n\n<content>` blocks, joined by `\n\n---\n\n`. The result is prepended to **both** the outo and subagent prompts.

If no skills are installed (or all are hidden / empty), `_load_active_skills` returns an empty string (no separator, no heading).

## CLI

```bash
miniouto skill list                  # list visible skills (hidden skills are EXCLUDED)
miniouto skill show my-skill         # print name/description/license/tools + body
```

`miniouto skill list` shows a rich table titled `Available Skills` with `Name | Description` (description truncated to 80 chars + "…"). Hidden skills never appear in this list — to view one, call `skill show <name>` explicitly (which does not filter on `hidden`).

## Adding a skill

```bash
mkdir -p ~/.agents/skills/my-skill
$EDITOR ~/.agents/skills/my-skill/SKILL.md
```

That's it. The skill is picked up on the next `miniouto` invocation. No "install" step.

To check a skill into a project's git repo:

```bash
cd ~/projects/myproject
mkdir -p .agents/skills
cp -r ~/.agents/skills/my-skill .agents/skills/
git add .agents/skills
```

(You'd then need to symlink or copy `.agents/skills/` into `~/.agents/skills/` to activate — there's no built-in discovery for project-local skills yet. Patches welcome.)

## Removing a skill

```bash
rm -rf ~/.agents/skills/my-skill
```

Takes effect on the next chat turn.

## API reference (`storage/skills.py`)

```python
SKILLS_DIR = Path.home() / ".agents" / "skills"   # NOT under ~/.miniouto/

@dataclass
class Skill:
    name: str
    description: str
    content: str
    license: str | None = None
    allowed_tools: str | None = None
    hidden: bool = False
```

| Function | Returns | Notes |
|---|---|---|
| `list_skills()` | `list[Skill]` | walks every subdir of `SKILLS_DIR`, parses `SKILL.md`, **filters out hidden skills** |
| `get_skill(name)` | `Skill \| None` | |
| `get_skill_content(name)` | `str \| None` | shorthand for `get_skill(name).content` |
| `_parse_skill(path)` | `Skill \| None` | internal: regex frontmatter parser |
| `_extract_field(frontmatter, field)` | `str \| None` | internal: single-line `key: value` extractor |

> **Note:** `storage/skills.py` is NOT re-exported in `storage/__init__.py`'s `__all__`. The `core` and `cli` layers import it directly via `from ..storage import skills as skill_store`. Don't add it to `__all__` without auditing the import sites first.

## Why skills live outside `~/.miniouto/`

Skills are intended to be:

- **Project-shared** — checked into a repo's `.agents/skills/` (when wired up — see Adding a skill above).
- **Cross-installation** — the same skill works regardless of which machine or virtualenv you're running miniouto from.
- **Cross-tool** — `~/.agents/skills/` is the Anthropic skill convention; other tools (Claude Code, etc.) read from the same location.

The per-installation config (providers, styles, settings, sessions) lives in `~/.miniouto/` because it's environment-specific. Skills are content, not configuration.
