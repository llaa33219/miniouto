"""Skill discovery and loading from ~/.agents/skills/."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SKILLS_DIR = Path.home() / ".agents" / "skills"


@dataclass
class Skill:
    name: str
    description: str
    content: str
    license: str | None = None
    allowed_tools: str | None = None
    hidden: bool = False


def list_skills() -> list[Skill]:
    """Discover all skills from ~/.agents/skills/."""

    if not SKILLS_DIR.is_dir():
        return []

    skills: list[Skill] = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        skill = _parse_skill(skill_md)
        if skill and not skill.hidden:
            skills.append(skill)
    return skills


def get_skill(name: str) -> Skill | None:
    """Get a skill by name."""

    skill_md = SKILLS_DIR / name / "SKILL.md"
    if not skill_md.exists():
        return None
    return _parse_skill(skill_md)


def get_skill_content(name: str) -> str | None:
    """Get the raw content of a skill."""

    skill = get_skill(name)
    return skill.content if skill else None


def _parse_skill(path: Path) -> Skill | None:
    """Parse a SKILL.md file into a Skill object."""

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
    )


def _extract_field(frontmatter: str, field: str) -> str | None:
    """Extract a field value from YAML frontmatter."""

    pattern = rf"^{field}:\s*(.+)$"
    match = re.search(pattern, frontmatter, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()
