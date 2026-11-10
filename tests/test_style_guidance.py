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
    assert "END YOUR TURN" in body or "END THE TURN" in body or "end your turn" in body
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
        assert "no independent work remains" in spec.prompt, spec.name
        assert "end your turn" in spec.prompt, spec.name
        assert "NO tool call in that response" in spec.prompt, spec.name
        assert "never poll" in spec.prompt or "do not poll" in spec.prompt, spec.name
        assert "not your final task report" in spec.prompt, spec.name
        assert "never poll" in spec.prompt or "do not poll" in spec.prompt, spec.name
        assert "`sleep`" in spec.prompt, spec.name


RWE_STYLES = ("coding", "coding-pro", "coding-work", "coding-ultra")
RWE_DETAILED_STYLES = RWE_STYLES[1:]


@pytest.mark.parametrize("style", RWE_STYLES)
def test_coding_styles_require_rwe_for_text_files(style: str) -> None:
    body = _body(style)

    assert "rwe read" in body
    assert "rwe write" in body
    assert "rwe edit" in body
    assert "hex" in body
    assert "original" in body
    assert "re-read" in body.lower()


def test_minimal_coding_uses_ephemeral_rwe_without_persistent_install() -> None:
    for role in (None, "subagent"):
        body = _body("coding", role)
        assert "uvx --from read-write-edit-tools rwe" in body
        assert "uv tool install read-write-edit-tools" not in body
        assert "pip install" not in body
        assert "report the blocker" in body or "not a persistent install" in body


@pytest.mark.parametrize("style", RWE_DETAILED_STYLES)
def test_detailed_styles_include_rwe_bootstrap_and_safety_rules(style: str) -> None:
    body = _body(style)

    assert "command -v rwe" in body
    assert "uv tool install read-write-edit-tools" in body
    assert "python3 -m pip install --user read-write-edit-tools" in body
    assert "never `sudo`" in body
    assert "https://github.com/llaa33219/read-write-edit-tools" in body
    assert "1000 lines" in body
    assert "same original snapshot" in body
    assert "never overwrites" in body or "Fails if FILE exists" in body


@pytest.mark.parametrize("style", RWE_DETAILED_STYLES)
def test_detailed_editor_roles_can_install_and_use_rwe(style: str) -> None:
    body = _body(style, "editor")

    assert "rwe read" in body
    assert "rwe write" in body
    assert "rwe edit" in body
    assert "uv tool install read-write-edit-tools" in body
    assert "python3 -m pip install --user read-write-edit-tools" in body


@pytest.mark.parametrize("style", RWE_DETAILED_STYLES)
def test_each_detailed_role_has_a_self_contained_rwe_route(style: str) -> None:
    path = BUNDLED_STYLE_DIR / f"{style}.md"
    _, specs = parse_style(path.read_text(encoding="utf-8"))

    for spec in specs:
        assert "rwe read" in spec.prompt, spec.name
        if spec.name in {"editor", "doc-writer"}:
            assert "uv tool install read-write-edit-tools" in spec.prompt, spec.name
        else:
            assert "uvx --from read-write-edit-tools rwe" in spec.prompt, spec.name


@pytest.mark.parametrize("style", RWE_DETAILED_STYLES)
def test_background_waiting_branch_is_explicit_and_tool_free(style: str) -> None:
    body = _body(style)

    assert "There are exactly two valid branches" in body
    assert "Send the user a plain-text waiting response with NO tool call" in body
    assert "END THE TURN" in body
    assert "next edit, brief, review, validation, or synthesis depends" in body
    assert "This is an intentional **waiting response, not a final answer**" in body
    assert "alongside the real tool call" in body
    assert "polling or filler, never independent work" in body
    assert "Foreground delegation is different" in body
    assert "Foreground result:" in body
    assert "When one of several alarms arrives:" in body
    assert "A correct waiting assistant response contains text only, no tool call:" in body


@pytest.mark.parametrize("style", RWE_DETAILED_STYLES)
def test_loop_rules_reinforce_waiting_without_premature_completion(style: str) -> None:
    body = _body(style)

    assert "Background waiting" in body
    assert "intentional waiting, not the task's final answer" in body
    assert "Never attach a tool call to that waiting response" in body
    assert "continue_loop" not in body


@pytest.mark.parametrize("style", RWE_DETAILED_STYLES)
def test_background_delegation_is_the_default_and_foreground_is_the_exception(
    style: str,
) -> None:
    body = _body(style)

    assert "**`--bg` is the default and recommended way to delegate.**" in body
    assert "Use foreground delegation only for the specific exception" in body
    assert "Foreground is the exception, not the default" in body

    _, specs = parse_style((BUNDLED_STYLE_DIR / f"{style}.md").read_text(encoding="utf-8"))
    for spec in specs:
        assert "Delegation defaults to `--bg`" in spec.prompt, spec.name
        assert "foreground run only when you intentionally need its answer inline" in spec.prompt, spec.name


def test_coding_work_defers_optional_interpretation_questions_to_the_report() -> None:
    body = _body("coding-work")

    assert "## Ask or decide? — calibrated clarification" in body
    assert "Never ask automatically or reflexively just because the request is ambiguous" in body
    assert "Stop and ask only for consequential ambiguity" in body
    assert "Non-blocking choices belong in the completed-work report" in body
    assert "report what you delivered, then optionally ask **one** concise follow-up" in body
    assert "the user may ignore it and keep the delivered result" in body
    assert "ask mid-task only when work truly cannot proceed" in body


@pytest.mark.parametrize("style", ("coding-work", "coding-ultra"))
def test_no_question_instruction_has_only_extreme_emergency_exceptions(style: str) -> None:
    body = _body(style)

    assert "If the user says not to ask questions" in body
    assert "irreversibly destroy all or essentially all data" in body
    assert "expose sensitive credentials" in body
    assert "genuinely exhaustive, documented user-space attempts" in body
    assert "cannot be skipped and no other useful work can continue" in body or "unskippable step and no other useful work able to continue" in body
    assert "STOP immediately before the dangerous or impossible action" in body
    assert "Ask only the one unavoidable question" in body or "ask only the single unavoidable question" in body


def test_coding_ultra_asks_only_consequential_questions_before_work_starts() -> None:
    body = _body("coding-ultra")

    assert "Startup clarification — evidence first, not a mandatory question" in body
    assert "Never ask merely because the wording is ambiguous" in body
    assert "one concrete startup question only when all three hold" in body
    assert "Never ask after work starts" in body
    assert "do not append an optional" in body
    assert "Questions are startup-only and consequential" in body
    assert "ask once (there is no defensible default here)" not in body
    assert "The four true hard blocks" not in body


@pytest.mark.parametrize("style", ("coding-work", "coding-ultra"))
def test_roles_report_ambiguity_to_the_parent_instead_of_questioning_the_user(
    style: str,
) -> None:
    _, specs = parse_style((BUNDLED_STYLE_DIR / f"{style}.md").read_text(encoding="utf-8"))

    for spec in specs:
        assert "never ask the user questions" in spec.prompt, spec.name
        assert "the parent owns any user clarification" in spec.prompt, spec.name
        assert "Do not append optional questions to your report" in spec.prompt, spec.name
        assert "If the brief lacks a fact you need, ask" not in spec.prompt, spec.name
