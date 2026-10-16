# Skills

Skills are **portable, project-shared context blocks** that a style's prompt advertises at runtime through the `{skills}` token. Unlike styles (which are per-installation and live in `~/.miniouto/`), skills live outside that tree, in one of **two default roots** — checked in this order, local first:

```
./skills/                                # project-local, relative to the directory
└── <skill-name>/                        #   you invoked miniouto from — FIRST
    └── SKILL.md

~/.agents/skills/                        # global, the Anthropic convention — SECOND
└── <skill-name>/
    └── SKILL.md
```

Both roots sit outside `~/.miniouto/`, so skills stay portable: a project can check its own `skills/` into version control next to the code it describes, and the global root keeps skills shared across every project and every other tool that follows the same convention.

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

The actual skill content goes here. This is the body the model reads on
demand — never injected into a prompt. The prompt only ever carries the
catalog entry (name + description) that the style's `{skills}` token
renders.
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
| `hidden` | no | bool (`true`/`false`) | If `true`, the skill is **not listed** by the `{skills}` catalog and is **excluded from `skill list`**. (`skill show <name>` still works on a hidden skill by explicit name.) Only the literal string `"true"` sets the field — anything else (including `"True"`, `"1"`) is treated as `false`. |

If `name` or `description` is missing, the skill is skipped (parsed as `None`).

### Body

Free-form Markdown. This is the actual prompt content. Skills typically describe:
- A specific project's architecture and conventions.
- A workflow or methodology the agent should follow.
- A coding style guide.
- A list of allowed tools or commands.

## How skills reach the prompt

Skills are **lazy-loaded**, and nothing is added to a prompt implicitly any more: the catalog appears exactly where a style body places `{skills}` or `{skills:<dir>…}`. The catalog is rendered by `core/runtime.py:_skills_catalog(roots)`:

```python
def _skills_catalog(roots: list[Path]) -> str:
    entries = [(skill, root) for root in roots for skill in skill_store.list_skills(root)]
    if not entries:
        return ""
    # "# Available Skills", a note that each skill lives in its own directory
    # under <root>/<name>/ and its SKILL.md should be read via Bash when a task
    # matches, then "- <name>: <description>" lines (each naming its root when
    # there is more than one).
```

The prompt shape is therefore:

- a `# Available Skills` heading;
- a short instruction telling the model where the skill directories live and that it should read the SKILL.md (and the files it references) via Bash when a task matches, so skill instructions take precedence over the default workflow;
- one `- <name>: <description>` line per skill.

The full SKILL.md body is NOT injected; the agent reads it on demand. If a body names no skills token, the model is told about no skills; if the roots yield nothing visible, the token expands to the empty string (no catalog block at all).

**Which directories are listed is (by default) two roots, and beyond that the style's choice.** `{skills}` (and `{skills:}`) reads the default roots — the invocation directory's `./skills/` **first**, then `~/.agents/skills` — so project-local skills take precedence in the catalog and a name installed in both is listed twice, each line naming its own root; `{skills:<dir>[,<dir>…]}` reads the named roots — `~` is expanded, a relative path resolves against the directory miniouto was invoked from, missing or unreadable roots are skipped silently, and entries are ordered by root order then name (a name present in two roots is listed twice, each line naming its own root). The underlying `storage/skills.py:list_skills(directory)` takes that directory, with `None` meaning **both** default roots — `default_roots()` resolves them to `[<invocation cwd>/skills, SKILLS_DIR]`. See `docs/styles.md` § Dynamic tokens.

## CLI

```bash
miniouto skill list                  # list visible skills (hidden skills are EXCLUDED)
miniouto skill show my-skill         # print name/description/license/tools + body
```

`miniouto skill list` shows a rich table titled `Available Skills` with `Name | Description | Root` (description truncated to 80 chars + "…", root dimmed) over the merged local+global set, so a name installed in both shows twice with the root that resolved it. Hidden skills never appear in this list — to view one, call `skill show <name>` explicitly (which does not filter on `hidden`).

## Adding a skill

```bash
mkdir -p ~/.agents/skills/my-skill
$EDITOR ~/.agents/skills/my-skill/SKILL.md
```

That's it. The skill is picked up on the next `miniouto` invocation. No "install" step.

To check a skill into a project's git repo, put it in the project's `skills/` — a default root, so it is discovered with no extra wiring:

```bash
cd ~/projects/myproject
mkdir -p skills
cp -r ~/.agents/skills/my-skill skills/
git add skills
```

Anyone who clones that repo gets the skill the next time they run miniouto **from that project directory**. (`.agents/skills/` works too, named explicitly: `{skills:./.agents/skills}`.)

## Removing a skill

```bash
rm -rf ~/.agents/skills/my-skill     # global
rm -rf ./skills/my-skill             # project-local
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
    directory: Path = SKILLS_DIR   # the root this entry was found under
```

| Function | Returns | Notes |
|---|---|---|
| `default_roots(cwd: Path \| None = None)` | `list[Path]` | the two default roots, LOCAL first: `[<cwd or INVOCATION_CWD>/skills, SKILLS_DIR]` |
| `list_skills(directory: Path \| None = None)` | `list[Skill]` | walks every subdir of each root, parses `SKILL.md`, **filters out hidden skills**, and stamps each `Skill.directory` with the root it came from. `None` = **both** default roots (local first, so a name in both lists twice); a named root is what a style's `{skills:<dir>}` token asks for and is read alone. A missing or unreadable root contributes nothing rather than raising |
| `get_skill(name)` | `Skill \| None` | searches the default roots in order (local wins), then parses it |
| `get_skill_content(name)` | `str \| None` | shorthand for `get_skill(name).content` |
| `_parse_skill(path, directory=SKILLS_DIR)` | `Skill \| None` | internal: regex frontmatter parser |
| `_skills_in(root)` | `list[Skill]` | internal: the one-root walk `list_skills` fans out over |
| `_extract_field(frontmatter, field)` | `str \| None` | internal: single-line `key: value` extractor |

> **Note:** `storage/skills.py` is NOT re-exported in `storage/__init__.py`'s `__all__`. The `core` and `cli` layers import it directly via `from ..storage import skills as skill_store`. Don't add it to `__all__` without auditing the import sites first.

## Why skills live outside `~/.miniouto/`

Skills are intended to be:

- **Project-shared** — checked into a repo's `skills/` (a default root, discovered automatically) or `.agents/skills/` (named explicitly with `{skills:./.agents/skills}`) — see Adding a skill above.
- **Cross-installation** — the same skill works regardless of which machine or virtualenv you're running miniouto from.
- **Cross-tool** — `~/.agents/skills/` is the Anthropic skill convention; other tools (Claude Code, etc.) read from the same location.

The per-installation config (providers, styles, settings, sessions) lives in `~/.miniouto/` because it's environment-specific. Skills are content, not configuration.
