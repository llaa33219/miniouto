"""Role mode: the system prompt and tool list of a SPAWNED run.

Delegation is cross-process now, so a spawned run is not a second agent —
it is the SAME runtime built with `RuntimeConfig.role` set. Exactly one
thing changes: the system prompt becomes one of the active style's named
subagent specs. The tool list, the preset, and every hook are identical.

The prompt is the interesting half. It used to be assembled in three
layers (per-call cwd preamble, skill catalog, the role's tag body); those two
implicit layers are gone and a style now places cwd and the skill catalog
itself with the `{cwd}` / `{skills}` tokens. Role mode must reuse that one
assembly path rather than invent a second one, because a drifted token pass is
invisible in a smoke test and only shows up as a spawned agent that cannot
find its files.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import coreouto as co
import pytest

from miniouto.core import context, reasoning
from miniouto.core import runtime as rt
from miniouto.storage import providers as provider_store
from miniouto.storage import session_config
from miniouto.storage import settings as settings_store
from miniouto.storage import styles as style_store

STYLE = (
    "<outo>outo body</outo>\n"
    "<editor>editor body</editor>\n"
    "<file-picker>file-picker body</file-picker>\n"
)


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


def _install_provider() -> str:
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
    style_store.write("probe", STYLE, overwrite=True)
    return "probe"


def _config(role: str | None = None) -> rt.RuntimeConfig:
    return rt.RuntimeConfig(
        provider_name="probe", model="m", style_name="probe", role=role, session="probe"
    )


def _build(role: str | None = None) -> co.Agent:
    return rt.build_runtime(_config(role))


def _prompt_of(agent: co.Agent) -> str:
    return str(agent.config.system_prompt)


def _tools_of(agent: co.Agent) -> list[str]:
    return list(agent.config.tools)


def test_the_role_prompt_is_the_roles_expanded_body() -> None:
    # Given: a style declaring two named roles
    _install_provider()

    # When: a runtime is built in role mode
    agent = _build("editor")
    prompt = _prompt_of(agent)

    # Then: the prompt is the role's own body and nothing else — no implicit
    # cwd preamble, no implicit skill catalog
    assert prompt == "editor body"


def test_the_role_prompt_is_byte_identical_to_the_assembled_spec() -> None:
    # Given: the same style read through the assembly helpers role mode uses
    _install_provider()

    # When: a role-mode runtime is built
    agent = _build("file-picker")

    # Then: its prompt is exactly what the style loader produces for that
    # spec — one assembly path, no drift between the two
    _, specs = rt._resolve_both_styles("probe", None)
    expected = next(s for s in specs if s.name == "file-picker")
    assert _prompt_of(agent) == rt.expand_prompt_tokens(
        expected.prompt, session="probe", style="probe"
    )


def test_an_unknown_role_names_the_available_roles() -> None:
    # Given: a style declaring two roles
    _install_provider()

    # When/Then: asking for a third fails before anything is registered, and
    # the message lists what the style DOES declare
    with pytest.raises(ValueError) as raised:
        _build("reviewer")
    message = str(raised.value)
    assert "Unknown role 'reviewer'" in message
    assert "editor" in message
    assert "file-picker" in message


def test_an_unknown_role_leaves_no_preset_registered() -> None:
    # Given: a previous turn's preset still in the registry
    _install_provider()
    _build()

    # When: a role-mode build fails
    with pytest.raises(ValueError):
        _build("reviewer")

    # Then: the failure happened BEFORE the clear+register window, so the
    # previous turn's preset is untouched rather than half-registered
    assert co.get_agent_preset("outo") is not None


def test_role_mode_gets_exactly_the_outo_tool_list() -> None:
    # Given: a style declaring two roles
    _install_provider()

    # When: a role-mode runtime is built and compared against the normal one
    role_tools = _tools_of(_build("editor"))
    _build()
    outo_tools = _tools_of(_build())

    # Then: the tool lists are identical, Computer included — a role run is
    # mechanically the same runtime, only the prompt differs
    assert role_tools == outo_tools


def test_normal_mode_still_gets_the_computer_tool() -> None:
    # Given: a platform where Computer registered
    _install_provider()
    if co.get_tool("Computer") is None:
        pytest.skip("Computer is not registered on this platform")

    # When: an ordinary (non-role) runtime is built
    agent = _build()

    # Then: it keeps the outo-only tool
    assert _tools_of(agent) == [*rt.BASE_TOOLS, "Computer"]


def test_call_subagent_is_never_registered_in_either_mode() -> None:
    # Given: a style declaring roles (the old trigger for the tool)
    _install_provider()

    # When: both modes build
    _build()
    assert co.get_tool("call_subagent") is None
    _build("editor")

    # Then: no tool by that name exists in any configuration
    assert co.get_tool("call_subagent") is None
    assert not hasattr(rt, "SUBAGENT_TOOL_NAME")


def test_resolve_subagent_still_resolves_the_settings_override() -> None:
    # Given: settings pinning a subagent provider and model, and no
    # per-call override
    provider_store.upsert(
        provider_store.Provider(
            name="helper",
            api_format="openai",
            base_url="http://127.0.0.1:9/v1",
            api_key="k",
            default_model="helper-model",
        )
    )
    settings_store.save(
        settings_store.Settings(subagent_provider="helper", subagent_reasoning="high")
    )

    # When: the spawn CLI's resolution helper runs
    resolved = rt._resolve_subagent(rt.ChatOverrides(), settings_store.load())

    # Then: the configured provider and its default model come back
    assert resolved == ("helper", "helper-model")


def test_resolve_subagent_prefers_the_per_call_override() -> None:
    # Given: settings pinning a provider, and an override naming another
    for name in ("helper", "other"):
        provider_store.upsert(
            provider_store.Provider(
                name=name,
                api_format="openai",
                base_url="http://127.0.0.1:9/v1",
                api_key="k",
                default_model=f"{name}-model",
            )
        )
    settings_store.save(settings_store.Settings(subagent_provider="helper"))

    # When: a run overrides both provider and model
    overrides = rt.ChatOverrides(subagent_provider="other", subagent_model="pinned")
    resolved = rt._resolve_subagent(overrides, settings_store.Settings())

    # Then: the override wins over both settings and the default model
    assert resolved == ("other", "pinned")


def test_resolve_subagent_reports_an_unconfigured_provider() -> None:
    # Given: an override naming a provider that was never added
    overrides = rt.ChatOverrides(subagent_provider="ghost")

    # When/Then: the failure names the fix
    with pytest.raises(RuntimeError, match="ghost"):
        rt._resolve_subagent(overrides, settings_store.Settings())


def test_role_mode_reaches_the_agent_config_not_just_the_preset() -> None:
    # Given: the same provider installed
    _install_provider()

    # When: a role-mode runtime is built
    agent: Any = _build("editor")

    # Then: the registered preset and the returned agent agree, so a spawned
    # process cannot run the outo prompt by accident
    assert co.get_agent_preset("outo").to_config().system_prompt == agent.config.system_prompt


TOKEN_STYLE = (
    "<outo>alarm into {this_session} using style {style}</outo>\n"
    "<editor>role alarms {this_session} / {style} / {nope}</editor>\n"
)


def test_tokens_expand_to_the_run_session_in_the_outo_body() -> None:
    # Given: a style whose outo body teaches a delegation command
    _install_provider()
    style_store.write("probe", TOKEN_STYLE, overwrite=True)

    # When: an outo runtime for session "probe" is built
    prompt = _prompt_of(_build())

    # Then: the token never reaches the model — the session name is literal
    assert "{this_session}" not in prompt
    assert "alarm into probe using style probe" in prompt


def test_tokens_expand_in_a_role_body_too() -> None:
    # Given: a style whose ROLE body also carries tokens, so a delegated run
    # that delegates further still teaches a literal command
    _install_provider()
    style_store.write("probe", TOKEN_STYLE, overwrite=True)

    # When: a role-mode runtime is built
    prompt = _prompt_of(_build("editor"))

    # Then: both tokens expanded...
    assert "role alarms probe / probe / {nope}" in prompt
    assert "{this_session}" not in prompt


def test_the_style_token_follows_the_resolved_style_name() -> None:
    # Given: a style that pins its own name in the outo body
    _install_provider()
    style_store.write(
        "probe", "<outo>run me under {style}</outo>\n<editor>e</editor>\n", overwrite=True
    )

    # When: a runtime is built against that style
    prompt = _prompt_of(_build())

    # Then: the expanded value is the --style / active default, not the
    # style document's title or the role
    assert "run me under probe" in prompt


def test_an_unknown_token_stays_literal() -> None:
    # Given: prose braces that are not tokens of ours
    _install_provider()
    style_store.write(
        "probe",
        "<outo>keep {nope} and {this_sessionx} and a lone { brace</outo>\n"
        "<editor>e</editor>\n",
        overwrite=True,
    )

    # When: a runtime is built
    prompt = _prompt_of(_build())

    # Then: nothing is substituted, stripped, or raised over — a `{` in
    # prose must survive prompt assembly untouched
    assert "keep {nope} and {this_sessionx} and a lone { brace" in prompt


# ── {subagent_pm}: the subagent trio spelled the way the model must copy it ──
#
# A style can only teach a self-contained delegation command if the
# subagent provider travels in the command line (invariant 5's Bash
# idempotency), so the token renders the EFFECTIVE trio in the `--pm`
# spelling rather than leaving the run to the ambient
# `--use-subagent-provider` resolution. Values are chosen so every
# fallback tier differs from the expected one: a test that passes whichever
# tier the implementation happens to read proves nothing.


PM_SESSION = "pm-token-session"


@pytest.fixture(autouse=True)
def _clear_pm_pin() -> Iterator[None]:
    session_config.clear(PM_SESSION)
    yield
    session_config.clear(PM_SESSION)


def test_subagent_pm_expands_to_the_pinned_subagent_trio() -> None:
    # Given: a pin whose subagent trio differs from the global default's AND
    # from the same pin's own outo trio
    _install_default("global-prov", "global-model")
    _install_default("pinned-prov", "pinned-model")
    _install_default("sub-prov", "sub-model")
    settings_store.save(
        _settings(PM_SESSION, provider="global-prov", subagent_provider="global-prov")
    )
    session_config.set_fields(
        PM_SESSION,
        provider="pinned-prov",
        model="pinned-model",
        subagent_provider="sub-prov",
        subagent_model="sub-model",
        subagent_reasoning="high",
    )

    # When: the token is expanded for that session
    expanded = rt.expand_prompt_tokens(
        "--pm {subagent_pm}", session=PM_SESSION, style="probe"
    )

    # Then: the pin's subagent trio, in the --pm spelling with the suffix
    assert expanded == "--pm sub-prov/sub-model:high"


def test_subagent_pm_falls_back_to_the_outo_trio_when_no_subagent_trio_is_set() -> None:
    # Given: a pin and a global default that set NO subagent key at all
    _install_default("global-prov", "global-model")
    _install_default("pinned-prov", "pinned-model")
    settings_store.save(_settings(PM_SESSION, provider="global-prov"))
    session_config.set_fields(PM_SESSION, provider="pinned-prov", model="pinned-model")

    # When: the token is expanded for that session
    expanded = rt.expand_prompt_tokens("{subagent_pm}", session=PM_SESSION, style="probe")

    # Then: the effective OUTO trio ("same as outo"), and no ":REASONING"
    # suffix because no reasoning level resolves anywhere
    assert expanded == "pinned-prov/pinned-model"


def test_subagent_pm_inherits_the_outo_reasoning_effort() -> None:
    # Given: no subagent reasoning anywhere, but outo's provider record
    # carries a default effort
    provider_store.upsert(
        provider_store.Provider(
            name="effort-prov",
            api_format="openai",
            base_url="http://127.0.0.1:9/v1",
            api_key="k",
            default_model="effort-model",
            reasoning_effort="high",
        )
    )
    settings_store.save(_settings(PM_SESSION, provider="effort-prov"))
    session_config.set_fields(PM_SESSION, model="pinned-model")

    # When: the token is expanded for that session
    expanded = rt.expand_prompt_tokens("{subagent_pm}", session=PM_SESSION, style="probe")

    # Then: the inherited outo effort rides along as the --pm suffix
    assert expanded == "effort-prov/pinned-model:high"


def test_subagent_pm_stays_literal_when_nothing_resolves() -> None:
    # Given: no provider configured anywhere and no pin
    settings_store.save(_settings(PM_SESSION))

    # When: the token is expanded for that session
    expanded = rt.expand_prompt_tokens(
        "delegate with --pm {subagent_pm} now", session=PM_SESSION, style="probe"
    )

    # Then: the token stays literal — a half-filled `--pm` would be a
    # malformed command for the model to copy, and the unknown-token rule
    # already covers the unresolved case
    assert expanded == "delegate with --pm {subagent_pm} now"


def test_subagent_pm_expands_in_both_prompt_bodies_when_built() -> None:
    # Given: a style whose outo AND role bodies both teach a literal --pm,
    # plus a session whose subagent trio is the global default
    _install_provider()
    style_store.write(
        "probe",
        "<outo>outo --pm {subagent_pm}</outo>\n<editor>role --pm {subagent_pm}</editor>\n",
        overwrite=True,
    )
    _install_default("sub-prov", "sub-model")
    settings_store.save(
        _settings(
            "probe",
            provider="probe",
            subagent_provider="sub-prov",
            subagent_model="sub-model",
        )
    )

    # When: both modes build
    outo_prompt = _prompt_of(_build())
    role_prompt = _prompt_of(_build("editor"))

    # Then: build_runtime expands the token in both, since role mode reuses
    # the one spec assembly path
    assert "outo --pm sub-prov/sub-model" in outo_prompt
    assert "role --pm sub-prov/sub-model" in role_prompt
    assert "{subagent_pm}" not in outo_prompt
    assert "{subagent_pm}" not in role_prompt


def test_a_token_free_body_is_byte_identical_to_the_raw_style_body() -> None:
    # Given: the token-free style this suite already uses
    _install_provider()

    # When: both modes build
    outo_prompt = _prompt_of(_build())
    role_prompt = _prompt_of(_build("editor"))

    # Then: the prompt is the raw style body, byte for byte — expansion is a
    # no-op, and nothing is prepended (a cwd preamble or a skill catalog
    # would show up as extra lines here)
    outo_body, specs = rt._resolve_both_styles("probe", None)
    assert outo_prompt == outo_body
    editor = next(s for s in specs if s.name == "editor")
    assert role_prompt == editor.prompt


# ── the session pin: overrides > pin > global default ───────────────────────
#
# Provider settings used to be global only, so a model switch in one process
# redirected every other one. `storage/session_config.py` pins them per
# session; these lock the two lanes that resolve through it. A pin freezes
# at first use, so every test here owns its session name (and clears it), and
# the provider names differ from the fixture's so a pin can never be confused
# with the global default.


PINNED = "pin-session"


@pytest.fixture(autouse=True)
def _clear_pin() -> Iterator[None]:
    session_config.clear(PINNED)
    yield
    session_config.clear(PINNED)


def _settings(session: str, **fields: str) -> settings_store.Settings:
    return settings_store.Settings(session=session, **fields)


def _install_default(name: str, model: str) -> None:
    provider_store.upsert(
        provider_store.Provider(
            name=name,
            api_format="openai",
            base_url="http://127.0.0.1:9/v1",
            api_key="k",
            default_model=model,
        )
    )


def test_a_session_pin_wins_over_the_global_default() -> None:
    # Given: the global default pointing at one provider/model, and the
    # session pinned to another
    _install_default("global-prov", "global-model")
    _install_default("pinned-prov", "pinned-model")
    settings_store.save(_settings(PINNED, provider="global-prov"))
    session_config.set_fields(PINNED, provider="pinned-prov", model="pinned-model")

    # When: the run resolves
    runtime = rt.resolve_runtime_from_settings()

    # Then: the session keeps what it was using, not what is global now
    assert (runtime.provider_name, runtime.model) == ("pinned-prov", "pinned-model")


def test_an_explicit_override_wins_over_the_session_pin() -> None:
    # Given: the session pinned to one provider/model
    _install_default("global-prov", "global-model")
    _install_default("override-prov", "override-model")
    settings_store.save(_settings(PINNED, provider="global-prov"))
    session_config.set_fields(PINNED, provider="pinned-prov", model="pinned-model")

    # When: the run overrides both
    runtime = rt.resolve_runtime_from_settings(
        rt.ChatOverrides(provider="override-prov", model="override-model")
    )

    # Then: the per-call choice wins
    assert (runtime.provider_name, runtime.model) == ("override-prov", "override-model")


def test_an_override_to_another_provider_drops_the_pinned_model() -> None:
    # Given: a session pinned to a provider and the model that goes with it
    _install_default("pinned-prov", "pinned-model")
    _install_default("other-prov", "other-model")
    settings_store.save(_settings(PINNED, provider="other-prov"))
    session_config.set_fields(PINNED, provider="pinned-prov", model="pinned-model")

    # When: the run moves outo to a different provider
    runtime = rt.resolve_runtime_from_settings(rt.ChatOverrides(provider="other-prov"))

    # Then: the pinned model — which belongs to the pinned provider — is
    # not carried over; the new provider's own default is used
    assert (runtime.provider_name, runtime.model) == ("other-prov", "other-model")


def test_a_changed_default_reaches_an_unpinned_session_and_then_freezes_it() -> None:
    # Given: a session that has never resolved, with one global default
    _install_default("global-prov", "model-a")
    settings_store.save(_settings(PINNED, provider="global-prov"))
    assert rt.resolve_runtime_from_settings().model == "model-a"

    # When: the default is edited afterwards, as the TUI model editor does
    _install_default("global-prov", "model-b")

    # Then: the running session is unaffected — its pin froze model-a
    assert rt.resolve_runtime_from_settings().model == "model-a"


def test_the_pinned_reasoning_reaches_the_runtime() -> None:
    # Given: a provider whose own reasoning_effort is set, and a session
    # pinned to a different effort
    _install_default("global-prov", "m")
    provider_store.upsert(
        provider_store.Provider(
            name="global-prov",
            api_format="openai",
            base_url="http://127.0.0.1:9/v1",
            api_key="k",
            default_model="m",
            reasoning_effort="low",
        )
    )
    settings_store.save(_settings(PINNED, provider="global-prov"))
    session_config.set_fields(PINNED, provider="global-prov", reasoning="high")

    # Then: the pin is what the runtime carries
    assert rt.resolve_runtime_from_settings().reasoning == "high"


def test_the_subagent_pin_wins_over_the_global_setting() -> None:
    # Given: settings naming one subagent provider, and the session pinned
    # to another
    for name in ("helper", "other"):
        _install_default(name, f"{name}-model")
    settings_store.save(_settings(PINNED, subagent_provider="helper"))
    session_config.set_fields(PINNED, subagent_provider="other")

    # When: the spawn CLI resolves against that session's pin
    resolved = rt._resolve_subagent(rt.ChatOverrides(), settings_store.load())

    # Then: the pinned provider wins, and its own default_model still fills
    # the model when the pin has none
    assert resolved == ("other", "other-model")


def test_a_subagent_override_wins_over_the_pin() -> None:
    # Given: a session pinned to a subagent provider, and an override
    for name in ("helper", "other"):
        _install_default(name, f"{name}-model")
    settings_store.save(_settings(PINNED, subagent_provider="helper"))
    session_config.set_fields(PINNED, subagent_provider="helper")

    # When: the run overrides the subagent provider
    resolved = rt._resolve_subagent(
        rt.ChatOverrides(subagent_provider="other"), settings_store.load()
    )

    # Then: the override wins
    assert resolved == ("other", "other-model")


def test_a_subagent_pin_that_clears_its_keys_falls_back_to_the_setting() -> None:
    # Given: a session whose subagent pin was explicitly cleared, and a
    # global subagent setting
    _install_default("helper", "helper-model")
    settings_store.save(_settings(PINNED, subagent_provider="helper"))
    session_config.set_fields(PINNED, subagent_provider="")

    # When: the run asks to use the subagent settings
    resolved = rt._resolve_subagent(rt.ChatOverrides(), settings_store.load())

    # Then: an unset key follows the global default
    assert resolved == ("helper", "helper-model")
