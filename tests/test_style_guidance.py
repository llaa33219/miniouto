"""Operational guidance shared by bundled styles, and the coding exception."""

from __future__ import annotations

import pytest

from miniouto.storage.paths import BUNDLED_STYLE_DIR
from miniouto.storage.styles import parse_style

UPDATED_STYLES = ("default", "coding-pro", "coding-work", "coding-ultra")
DDG_SEARCH = "curl -sL 'https://html.duckduckgo.com/html/?q=URL_ENCODED_QUERY' -A 'Mozilla/5.0'"


def _body(name: str, role: str | None = None) -> str:
    path = BUNDLED_STYLE_DIR / f"{name}.md"
    outo, specs = parse_style(path.read_text(encoding="utf-8"))
    if role is None:
        return outo
    return next(spec.prompt for spec in specs if spec.name == role)


@pytest.mark.parametrize("style", UPDATED_STYLES)
def test_non_coding_orchestrators_recommend_curl_duckduckgo_html(style: str) -> None:
    body = _body(style)

    assert DDG_SEARCH in body
    assert "DuckDuckGo HTML" in body
    assert "skill" in body  # existing skill precedence must survive


@pytest.mark.parametrize("style", UPDATED_STYLES)
def test_non_coding_orchestrators_yield_when_only_bg_results_remain(style: str) -> None:
    body = _body(style)

    assert "independent work" in body
    assert "wait for its completion message" in body
    assert "END YOUR TURN" in body or "end your turn" in body
    assert "`sleep`" in body
    assert "read the result file" in body or "read the named result file" in body
    assert "verify" in body


def test_default_nested_agent_also_yields_instead_of_polling() -> None:
    body = _body("default", "subagent")

    assert "independent work" in body
    assert "end your turn" in body
    assert "Never poll" in body
    assert DDG_SEARCH in body


@pytest.mark.parametrize(
    ("style", "role"),
    [("coding-work", "researcher"), ("coding-ultra", "researcher-web")],
)
def test_web_researchers_have_the_actual_curl_recipe(style: str, role: str) -> None:
    assert DDG_SEARCH in _body(style, role)


def test_coding_style_is_explicitly_not_part_of_the_web_or_waiting_update() -> None:
    body = _body("coding")

    assert DDG_SEARCH not in body
    assert "wait for its completion message" not in body


@pytest.mark.parametrize("style", ("coding-pro", "coding-work", "coding-ultra"))
def test_every_role_knows_bg_yielding_does_not_mean_its_own_work_is_done(
    style: str,
) -> None:
    path = BUNDLED_STYLE_DIR / f"{style}.md"
    _, specs = parse_style(path.read_text(encoding="utf-8"))
    assert specs

    for spec in specs:
        assert "no independent work left" in spec.prompt, spec.name
        assert "end your turn" in spec.prompt, spec.name
        assert "never poll" in spec.prompt, spec.name
        assert "`sleep`" in spec.prompt, spec.name
