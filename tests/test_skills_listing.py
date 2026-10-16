"""`list_skills(None)` searches BOTH default roots, project-local first.

Every test points the roots at tmp dirs — the real `~/.agents/skills/` is never
read or written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from miniouto.storage import skills as skill_store


def _skill(root: Path, name: str, description: str, *, hidden: bool = False) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    front = f"---\nname: {name}\ndescription: {description}\n"
    if hidden:
        front += "hidden: true\n"
    (directory / "SKILL.md").write_text(f"{front}---\n\nbody\n", encoding="utf-8")


@pytest.fixture
def roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    local = project / "skills"
    global_root = tmp_path / "global-skills"
    global_root.mkdir()
    monkeypatch.setattr(skill_store, "SKILLS_DIR", global_root)
    monkeypatch.setattr(skill_store, "INVOCATION_CWD", project)
    return local, global_root


def test_the_default_roots_are_local_then_global() -> None:
    # Given: nothing configured
    # When: the default roots are resolved
    roots = skill_store.default_roots(Path("/srv/app"))

    # Then: the project's ./skills comes first, the global root second
    assert roots == [Path("/srv/app/skills"), skill_store.SKILLS_DIR]


def test_both_default_roots_are_listed_local_first(roots: tuple[Path, Path]) -> None:
    # Given: one skill in each default root
    local, global_root = roots
    _skill(local, "alpha", "the local one")
    _skill(global_root, "beta", "the global one")

    # When: the default listing is taken
    found = skill_store.list_skills()

    # Then: both, in root order
    assert [s.name for s in found] == ["alpha", "beta"]
    # ...and each carries the root it came from
    assert [s.directory for s in found] == [local, global_root]


def test_a_name_in_both_roots_is_listed_twice(roots: tuple[Path, Path]) -> None:
    # Given: the same skill name installed under both roots
    local, global_root = roots
    _skill(local, "shared", "the local copy")
    _skill(global_root, "shared", "the global copy")

    # When: the default listing is taken
    found = skill_store.list_skills()

    # Then: both copies are listed, each knowing its own root
    assert [(s.name, s.directory, s.description) for s in found] == [
        ("shared", local, "the local copy"),
        ("shared", global_root, "the global copy"),
    ]


def test_a_missing_local_root_leaves_the_global_listing_intact(
    roots: tuple[Path, Path],
) -> None:
    # Given: no ./skills in the project, one skill in the global root
    local, global_root = roots
    _skill(global_root, "beta", "the global one")

    # When: the default listing is taken
    found = skill_store.list_skills()

    # Then: only the global skill, and no error for the absent local root
    assert [s.name for s in found] == ["beta"]
    assert not local.exists()


def test_names_sort_within_a_root(roots: tuple[Path, Path]) -> None:
    # Given: two skills in one root, added in reverse alphabetical order
    local, _ = roots
    _skill(local, "zulu", "from local")
    _skill(local, "alpha", "from local")

    # When: the default listing is taken
    found = skill_store.list_skills()

    # Then: name order within the root
    assert [s.name for s in found] == ["alpha", "zulu"]


def test_hidden_skills_are_excluded_from_every_root(roots: tuple[Path, Path]) -> None:
    # Given: a visible and a hidden skill in each root
    local, global_root = roots
    _skill(local, "local-visible", "shown")
    _skill(local, "local-hidden", "not shown", hidden=True)
    _skill(global_root, "global-visible", "shown")
    _skill(global_root, "global-hidden", "not shown", hidden=True)

    # When: the default listing is taken
    found = skill_store.list_skills()

    # Then: only the visible ones
    assert [s.name for s in found] == ["local-visible", "global-visible"]


def test_an_explicit_root_is_read_alone(roots: tuple[Path, Path]) -> None:
    # Given: skills in both default roots plus an unrelated one
    local, global_root = roots
    _skill(local, "alpha", "the local one")
    _skill(global_root, "beta", "the global one")
    other = local.parent / "other"
    _skill(other, "solo", "the other one")

    # When: that unrelated root is named explicitly
    found = skill_store.list_skills(other)

    # Then: only its own skills come back
    assert [(s.name, s.directory) for s in found] == [("solo", other)]


def test_get_skill_prefers_the_local_copy(roots: tuple[Path, Path]) -> None:
    # Given: one name installed under both roots
    local, global_root = roots
    _skill(local, "shared", "the local copy")
    _skill(global_root, "shared", "the global copy")

    # When: the skill is fetched by name
    skill = skill_store.get_skill("shared")

    # Then: the local one wins, and says where it lives
    assert skill is not None
    assert (skill.description, skill.directory) == ("the local copy", local)


def test_get_skill_falls_back_to_the_global_root(roots: tuple[Path, Path]) -> None:
    # Given: a skill only in the global root
    _, global_root = roots
    _skill(global_root, "beta", "the global one")

    # When: the skill is fetched by name
    skill = skill_store.get_skill("beta")

    # Then: it is found globally
    assert skill is not None
    assert skill.directory == global_root
