"""`{cwd}` and `{skills}`: the tokens that replaced the implicit prompt layers.

The harness no longer prepends a cwd preamble or a skill catalog — a style
body places both itself, so it also decides WHICH skill roots the model is
told about (`{skills:~/.claude/skills}`, or the default pair, or none at all).
These lock the grammar: resolution order, path handling, ordering across
roots, and the silent-skip rules.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from miniouto.core import context, reasoning
from miniouto.core import runtime as rt
from miniouto.storage import providers as provider_store
from miniouto.storage import skills as skill_store
from miniouto.storage import styles as style_store

SESSION = "token-session"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        context, "get_max_output_tokens", lambda model, provider_name=None: 4_096
    )
    monkeypatch.setattr(
        reasoning,
        "resolve_reasoning_passthrough",
        lambda fmt, model, provider_name, choice: {},
    )


def _skill(root: Path, name: str, description: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n", encoding="utf-8"
    )
    return directory


def _local_and_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path]:
    """Point the two default roots at tmp dirs; return them (local, global)."""

    cwd = tmp_path / "project"
    cwd.mkdir()
    global_root = tmp_path / "global-skills"
    global_root.mkdir()
    monkeypatch.setattr(skill_store, "SKILLS_DIR", global_root)
    monkeypatch.setattr(rt, "INVOCATION_CWD", cwd)
    return cwd / "skills", global_root


def _entries(expanded: str) -> list[str]:
    return [line for line in expanded.splitlines() if line.startswith("- ")]


def _expand(text: str) -> str:
    return rt.expand_prompt_tokens(text, session=SESSION, style="probe")


# ── {cwd} ────────────────────────────────────────────────────────────────────


def test_cwd_expands_to_the_invocation_directory() -> None:
    # Given: a body naming the token
    # When: the body is expanded
    expanded = _expand("you are in {cwd}")

    # Then: the absolute invocation path is literal in the prompt
    assert expanded == f"you are in {rt.INVOCATION_CWD}"
    assert "{cwd}" not in expanded


def test_cwd_is_absolute() -> None:
    # Given: a body naming the token
    # When: the body is expanded
    expanded = _expand("{cwd}")

    # Then: what the model reads resolves relative paths on its own
    assert Path(expanded).is_absolute()


# ── {skills}: the default roots (local ./skills first, then ~/.agents/skills) ──


def test_the_bare_token_lists_the_local_root_then_the_global_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: the project-local ./skills root and the global root, each with one
    local, global_root = _local_and_global(monkeypatch, tmp_path)
    _skill(local, "alpha", "do the alpha thing")
    _skill(global_root, "beta", "do the beta thing")

    # When: the bare token is expanded
    expanded = _expand("{skills}")

    # Then: both roots are catalogued, local first, each entry naming its root
    assert expanded.startswith("# Available Skills")
    assert f"{local}/<name>/" in expanded
    assert f"{global_root}/<name>/" in expanded
    assert _entries(expanded) == [
        f"- alpha: do the alpha thing  ({local})",
        f"- beta: do the beta thing  ({global_root})",
    ]
    # ...and never the bodies
    assert "body" not in expanded


def test_the_default_token_skips_a_missing_local_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: no ./skills in the project, only the global root
    local, global_root = _local_and_global(monkeypatch, tmp_path)
    _skill(global_root, "alpha", "the alpha thing")

    # When: the bare token is expanded
    expanded = _expand("{skills}")

    # Then: the global root still renders, alone and without an origin suffix
    assert "- alpha: the alpha thing" in expanded
    assert f"({global_root})" not in expanded
    assert str(local) not in expanded


def test_the_default_token_lists_a_name_installed_in_both_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: the same skill name under both default roots
    local, global_root = _local_and_global(monkeypatch, tmp_path)
    _skill(local, "shared", "the local copy")
    _skill(global_root, "shared", "the global copy")

    # When: the bare token is expanded
    entries = _entries(_expand("{skills}"))

    # Then: both copies are listed, each naming the root it came from
    assert len(entries) == 2
    assert local.as_posix() in entries[0] and "the local copy" in entries[0]
    assert global_root.as_posix() in entries[1] and "the global copy" in entries[1]


def test_an_empty_argument_is_the_default_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: a skill in the local default root
    local, _ = _local_and_global(monkeypatch, tmp_path)
    _skill(local, "alpha", "the alpha thing")

    # When: the bare-argument spelling is expanded
    expanded = _expand("{skills:}")

    # Then: it means the same thing as `{skills}`
    assert expanded == _expand("{skills}")


def test_the_catalog_does_not_inject_skill_bodies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: a skill whose body says something unique
    local, _ = _local_and_global(monkeypatch, tmp_path)
    _skill(local, "alpha", "the alpha thing")

    # When: the token is expanded
    expanded = _expand("{skills}")

    # Then: only name + description travel; the model reads SKILL.md via Bash
    assert "- alpha: the alpha thing" in expanded
    assert expanded.count("\nbody") == 0


# ── {skills:...}: explicit roots ────────────────────────────────────────────


def test_an_alternate_root_is_listed(tmp_path: Path) -> None:
    # Given: one skill directory outside ~/.agents/skills
    root = tmp_path / "claude-skills"
    _skill(root, "solo", "the solo thing")

    # When: that root is named by the token
    expanded = _expand(f"{{skills:{root}}}")

    # Then: it is catalogued, naming its own directory
    assert "- solo: the solo thing" in expanded
    assert f"{root}/<name>/" in expanded


def test_a_home_relative_root_is_expanded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: a fake home holding one alternate skills root
    home = tmp_path / "home"
    root = home / ".claude" / "skills"
    _skill(root, "solo", "the solo thing")
    monkeypatch.setenv("HOME", str(home))

    # When: the token names it with a tilde
    expanded = _expand("{skills:~/.claude/skills}")

    # Then: `~` expanded to that root
    assert "- solo: the solo thing" in expanded


def test_a_relative_root_resolves_against_the_invocation_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: a skill directory relative to the invocation cwd
    _skill(tmp_path / "skills", "local", "the local thing")
    monkeypatch.setattr(rt, "INVOCATION_CWD", tmp_path)

    # When: the token names it relatively
    expanded = _expand("{skills:./skills}")

    # Then: it resolved against INVOCATION_CWD
    assert "- local: the local thing" in expanded
    assert f"{tmp_path / 'skills'}" in expanded


def test_several_roots_merge_in_the_order_listed(tmp_path: Path) -> None:
    # Given: two roots, each holding skills that sort in the opposite order
    first = tmp_path / "first"
    second = tmp_path / "second"
    _skill(first, "zulu", "from first")
    _skill(first, "alpha", "from first")
    _skill(second, "mike", "from second")

    # When: the token lists them in one order...
    expanded = _expand(f"{{skills:{first},{second}}}")
    lines = [line for line in expanded.splitlines() if line.startswith("- ")]

    # Then: every entry of the first root precedes every entry of the second
    assert [line.split(":")[0] for line in lines] == [
        "- alpha",
        "- zulu",
        "- mike",
    ]
    # ...and reversing the list reverses the catalog, so the style states a
    # precedence rather than getting an arbitrary merge
    reversed_lines = [
        line
        for line in _expand(f"{{skills:{second},{first}}}").splitlines()
        if line.startswith("- ")
    ]
    assert [line.split(":")[0] for line in reversed_lines] == ["- mike", "- alpha", "- zulu"]


def test_a_merged_entry_names_the_root_it_came_from(tmp_path: Path) -> None:
    # Given: the same skill name installed under two roots
    first = tmp_path / "first"
    second = tmp_path / "second"
    _skill(first, "shared", "the first copy")
    _skill(second, "shared", "the second copy")

    # When: both roots are catalogued
    expanded = _expand(f"{{skills:{first},{second}}}")
    lines = [line for line in expanded.splitlines() if line.startswith("- ")]

    # Then: both copies are listed, each naming its own distinct path
    assert len(lines) == 2
    assert any(first.as_posix() in line and "the first copy" in line for line in lines)
    assert any(second.as_posix() in line and "the second copy" in line for line in lines)


def test_a_single_root_entry_carries_no_origin_suffix(tmp_path: Path) -> None:
    # Given: one root holding one skill
    root = tmp_path / "only"
    _skill(root, "solo", "the solo thing")

    # When: it is catalogued alone
    expanded = _expand(f"{{skills:{root}}}")

    # Then: the note in the header already names the root, so the entry line
    # stays exactly `- <name>: <description>`
    assert "- solo: the solo thing" in expanded
    assert f"- solo: the solo thing ({root})" not in expanded


def test_a_missing_root_is_skipped_silently(tmp_path: Path) -> None:
    # Given: one real root and one that does not exist
    real = tmp_path / "real"
    _skill(real, "alpha", "the alpha thing")

    # When: both are named, missing one first
    expanded = _expand(f"{{skills:{tmp_path / 'ghost'},{real}}}")

    # Then: only the real root is catalogued, with no notice about the other
    assert "- alpha: the alpha thing" in expanded
    assert "ghost" not in expanded


def test_a_root_with_no_usable_skills_contributes_nothing(tmp_path: Path) -> None:
    # Given: a directory holding no SKILL.md, plus a hidden skill
    empty = tmp_path / "empty"
    empty.mkdir()
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "alpha").mkdir()
    (hidden / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: hidden\nhidden: true\n---\nb\n", encoding="utf-8"
    )

    # When: they are the only roots named
    expanded = _expand(f"{{skills:{empty},{hidden}}}")

    # Then: the token expands to nothing at all — not even a header
    assert expanded == ""


def test_every_root_missing_expands_to_the_empty_string(tmp_path: Path) -> None:
    # Given: only non-existent roots
    # When: the token is expanded
    expanded = _expand(f"{{skills:{tmp_path / 'a'},{tmp_path / 'b'}}}")

    # Then: nothing is inserted where the token stood
    assert expanded == ""


# ── unknown tokens ───────────────────────────────────────────────────────────


def test_a_token_that_only_looks_like_skills_stays_literal() -> None:
    # Given: prose braces adjacent to the real token's namespace
    # When: the body is expanded
    expanded = _expand("{skillsx} {skills_dir} {nope}")

    # Then: none of them is a token — the regex is anchored on the colon or
    # the closing brace, so nothing is silently swallowed
    assert expanded == "{skillsx} {skills_dir} {nope}"


# ── through build_runtime, in the outo body AND a role body ──────────────────


def test_the_new_tokens_expand_in_the_outo_body_and_in_a_role_body() -> None:
    # Given: a style placing both tokens in each body
    provider_store.upsert(
        provider_store.Provider(
            name="probe",
            api_format="openai",
            base_url="http://127.0.0.1:9/v1",
            api_key="k",
            default_model="m",
            max_context_window=200_000,
        )
    )
    style_store.write(
        "probe",
        "<outo>outo cwd={cwd}\n{skills}</outo>\n"
        "<editor>editor cwd={cwd}\n{skills}</editor>\n",
        overwrite=True,
    )
    config = rt.RuntimeConfig(
        provider_name="probe", model="m", style_name="probe", session=SESSION
    )

    # When: both modes build
    outo_prompt = str(rt.build_runtime(config).config.system_prompt)
    role_prompt = str(
        rt.build_runtime(dataclasses.replace(config, role="editor")).config.system_prompt
    )

    # Then: role mode reuses the one token pass, so both bodies expand
    assert f"outo cwd={rt.INVOCATION_CWD}" in outo_prompt
    assert f"editor cwd={rt.INVOCATION_CWD}" in role_prompt
    assert "{cwd}" not in outo_prompt
    assert "{cwd}" not in role_prompt
    assert "{skills}" not in outo_prompt
    assert "{skills}" not in role_prompt
