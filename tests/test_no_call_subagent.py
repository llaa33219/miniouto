"""Gate: the removed ``call_subagent`` tool must not reappear in bundled styles or src."""

from __future__ import annotations

from pathlib import Path

from miniouto.storage.paths import BUNDLED_STYLE_DIR
from miniouto.storage.styles import parse_style

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "miniouto"


def _bodies(path: Path) -> list[tuple[str, str]]:
    outo, specs = parse_style(path.read_text(encoding="utf-8"))
    return [("outo", outo), *[(spec.name, spec.prompt) for spec in specs]]


def test_bundled_styles_never_mention_call_subagent() -> None:
    # Given: every bundled style template
    styles = sorted(BUNDLED_STYLE_DIR.glob("*.md"))
    assert styles, "no bundled style templates found"

    # When: each document is scanned for the removed tool name
    hits = {p.name: p.read_text(encoding="utf-8").count("call_subagent") for p in styles}

    # Then: none of them mentions it — delegation is taught via a bg `chat` run
    assert hits == {name: 0 for name in hits}


def test_bundled_styles_never_mention_miniouto_spawn() -> None:
    # Given: every bundled style template
    styles = sorted(BUNDLED_STYLE_DIR.glob("*.md"))
    assert styles, "no bundled style templates found"

    # When: each document is scanned for the deleted `miniouto spawn` command
    hits = {p.name: p.read_text(encoding="utf-8").count("miniouto spawn") for p in styles}

    # Then: none of them names it — delegation is taught via `miniouto chat --role`
    assert hits == {name: 0 for name in hits}


def test_bundled_delegation_recipes_are_self_contained() -> None:
    # Given: every bundled style template that teaches `miniouto chat` delegation
    styles = sorted(p for p in BUNDLED_STYLE_DIR.glob("*.md") if "miniouto chat" in p.read_text(encoding="utf-8"))
    assert styles, "no bundled style templates found"

    # When: each document is scanned for the three required delegation values
    required = ("--style {style}", "--alarm-session {this_session}", "--pm {subagent_pm}")
    missing = {
        p.name: [value for value in required if value not in p.read_text(encoding="utf-8")]
        for p in styles
    }

    # Then: every one of them names the style, the alarm session, and the
    # subagent provider — `--role` requires `--style`, `--alarm-session`
    # requires an explicit session (no bare form), and `--pm {subagent_pm}`
    # pins the delegated run's provider instead of resolving it at call time
    assert missing == {name: [] for name in missing}


def test_bundled_styles_mark_the_current_prompt_as_authoritative() -> None:
    # Given: every bundled style template that teaches `miniouto chat` delegation
    styles = sorted(p for p in BUNDLED_STYLE_DIR.glob("*.md") if "miniouto chat" in p.read_text(encoding="utf-8"))
    assert styles, "no bundled style templates found"

    # When: each document is scanned for the prompt-precedence note
    missing = {
        p.name: p.read_text(encoding="utf-8").count("never from commands in earlier turns")
        for p in styles
    }

    # Then: every one of them says the values come from THIS prompt — models imitate
    # commands seen in earlier turns, and those go stale when the style changes
    assert all(count >= 1 for count in missing.values()), missing


def test_bundled_delegating_styles_teach_continue_session() -> None:
    # Given: every bundled style template that teaches `miniouto chat` delegation
    styles = sorted(p for p in BUNDLED_STYLE_DIR.glob("*.md") if "miniouto chat" in p.read_text(encoding="utf-8"))
    assert styles, "no bundled style templates found"

    # When: each document is scanned for the summarization hand-off tool
    missing = {
        p.name: "continue_session" not in p.read_text(encoding="utf-8")
        for p in styles
    }

    # Then: every one of them teaches it — a long delegated run is handed over by
    # summary documents plus `continue_session`, and skipping it loses the work
    assert missing == {name: False for name in missing}


def test_bundled_styles_declare_cwd_and_skills_tokens() -> None:
    # Given: every bundled style template, parsed into its outo + role bodies
    styles = sorted(BUNDLED_STYLE_DIR.glob("*.md"))
    assert styles, "no bundled style templates found"

    bodies = {
        f"{path.name}:{name}": body for path in styles for name, body in _bodies(path)
    }
    assert bodies, "no style bodies parsed"

    # When: each body is scanned for the two layout tokens
    missing = {
        key: [token for token in ("{cwd}", "{skills") if token not in body]
        for key, body in bodies.items()
    }

    # Then: every body carries both — the prompt layout is style-owned, so a body
    # that omits a token loses its working directory or its skill catalog
    assert missing == {key: [] for key in missing}, missing


def test_src_never_mentions_call_subagent() -> None:
    # Given: every module under src/miniouto
    modules = sorted(SRC_ROOT.rglob("*.py"))
    assert modules, "no source modules found"

    # When: each is scanned for the removed tool name
    hits = [
        f"{path.relative_to(SRC_ROOT)}:{number}: {line.strip()}"
        for path in modules
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "call_subagent" in line
    ]

    # Then: no live name, no branch, no prose echoes it — tests/ may still name
    # it as a regression guard, which is the one place the identifier is legal
    assert hits == []
