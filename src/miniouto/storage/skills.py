"""Skill discovery and loading from the two default roots.

Defaults are **project-local first, then global**: `./skills/` relative to the
directory miniouto was invoked from, then `~/.agents/skills/` (the Anthropic
convention). Every default lookup searches both, in that order; a named root
looks at that root alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..paths_runtime import INVOCATION_CWD

SKILLS_DIR = Path.home() / ".agents" / "skills"
LOCAL_SKILLS_DIRNAME = "skills"


def default_roots(cwd: Path | None = None) -> list[Path]:
    """The default skill roots, local first: `./skills/` then `~/.agents/skills/`."""

    base = Path(cwd) if cwd is not None else INVOCATION_CWD
    return [base / LOCAL_SKILLS_DIRNAME, SKILLS_DIR]


@dataclass
class Skill:
    name: str
    description: str
    content: str
    license: str | None = None
    allowed_tools: str | None = None
    hidden: bool = False
    directory: Path = SKILLS_DIR


def list_skills(directory: Path | None = None) -> list[Skill]:
    """Discover skills, in root order.

    `None` means the default roots — `./skills/` of the invocation directory
    first, then `~/.agents/skills/` — so a name installed in both lists twice,
    each `Skill` carrying the root it came from. An explicit `directory` reads
    that root alone. A missing or unreadable root contributes nothing rather
    than raising: the `{skills:...}` style token accepts user-supplied roots,
    and one bogus path must not fail the turn that expanded it.
    """

    roots = default_roots() if directory is None else [Path(directory)]
    found: list[Skill] = []
    for root in roots:
        found.extend(_skills_in(root))
    return found


def get_skill(name: str) -> Skill | None:
    """Get a skill by name, searching the default roots in order."""

    for root in default_roots():
        skill_md = root / name / "SKILL.md"
        if not skill_md.exists():
            continue
        skill = _parse_skill(skill_md, root)
        if skill is not None:
            return skill
    return None


def _skills_in(root: Path) -> list[Skill]:
    """Skills visible in one root; `[]` when it is missing or unreadable."""

    if not root.is_dir():
        return []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    skills: list[Skill] = []
    for skill_dir in entries:
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        skill = _parse_skill(skill_md, root)
        if skill and not skill.hidden:
            skills.append(skill)
    return skills


def get_skill_content(name: str) -> str | None:
    """Get the raw content of a skill."""

    skill = get_skill(name)
    return skill.content if skill else None


def _parse_skill(path: Path, directory: Path = SKILLS_DIR) -> Skill | None:
    """Parse a SKILL.md file into a Skill object rooted at `directory`."""

    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return None

    frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
    if not frontmatter_match:
        return None

    frontmatter = frontmatter_match.group(1)
    content = raw[frontmatter_match.end():].strip()

    name = _extract_field(frontmatter, "name")
    description = _extract_field(frontmatter, "description")
    if not name or not description:
        return None

    license_val = _extract_field(frontmatter, "license")
    allowed_tools = _extract_field(frontmatter, "allowed-tools")
    hidden = _extract_field(frontmatter, "hidden") == "true"

    return Skill(
        name=name,
        description=description,
        content=content,
        license=license_val,
        allowed_tools=allowed_tools,
        hidden=hidden,
        directory=directory,
    )


def _extract_field(frontmatter: str, field: str) -> str | None:
    """Extract a field value from YAML frontmatter."""

    pattern = rf"^{field}:\s*(.+)$"
    match = re.search(pattern, frontmatter, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()
