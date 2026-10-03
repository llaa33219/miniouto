"""Lock the clear+register window of `core.runtime.build_runtime`.

`clear_coreouto_state()` wipes coreouto's global provider/preset/tool/hook
registries. Anything slow that runs *inside* that window (style file I/O, lma
lookups for the output cap, lma lookups for reasoning options) leaves the
registries empty for as long as it takes — and a background subagent job
resolves its provider and tools by NAME at `call()` entry. So every slow
resolution must be hoisted above the clear, and the window itself must be pure
in-memory dict operations.

Also covered: a `run_chat` that dies during resolve/build must not leave the
previous turn's global hooks (which close over the previous sink/persister)
registered.
"""

from __future__ import annotations

import coreouto as co
import pytest

from miniouto.core import chat as chat_mod
from miniouto.core import context, providers, reasoning
from miniouto.core import runtime as rt
from miniouto.storage import providers as provider_store
from miniouto.storage import styles as style_store

STYLE = "<outo>outo body</outo>\n<editor>editor body</editor>\n"


def _install_provider() -> str:
    name = "probe"
    provider_store.upsert(
        provider_store.Provider(
            name=name,
            api_format="openai",
            base_url="http://127.0.0.1:9/v1",
            api_key="k",
            default_model="m",
            # Set so make_summarize_hook resolves the window from the
            # override instead of hitting lma.
            max_context_window=200_000,
        )
    )
    style_store.write("probe", STYLE, overwrite=True)
    return name


def test_slow_resolution_completes_before_the_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a style with a named subagent, and every slow step instrumented
    name = _install_provider()
    timeline: list[str] = []

    real_clear = providers.clear_coreouto_state

    def spy_clear() -> None:
        timeline.append("clear")
        real_clear()

    def spy_max_tokens(model: str, provider_name: str | None = None) -> int:
        timeline.append("max_tokens")
        return 4_096

    def spy_reasoning(fmt: str, model: str, provider_name: str, choice: str | None) -> dict:
        timeline.append(f"reasoning:{model}:{provider_name}")
        return {}

    monkeypatch.setattr(rt, "clear_coreouto_state", spy_clear)
    monkeypatch.setattr(context, "get_max_output_tokens", spy_max_tokens)
    monkeypatch.setattr(reasoning, "resolve_reasoning_passthrough", spy_reasoning)

    # When: a runtime is built
    rt.build_runtime(
        rt.RuntimeConfig(provider_name=name, model="m", style_name="probe", session="probe")
    )

    # Then: the style I/O and both lma lookups landed before the clear
    assert "clear" in timeline
    clear_at = timeline.index("clear")
    assert "max_tokens" in timeline[:clear_at]
    assert timeline.count("reasoning:m:probe") == 2


def test_registration_effects_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: the same instrumented setup
    name = _install_provider()
    monkeypatch.setattr(
        context, "get_max_output_tokens", lambda model, provider_name=None: 4_096
    )
    monkeypatch.setattr(
        reasoning,
        "resolve_reasoning_passthrough",
        lambda fmt, model, provider_name, choice: {},
    )

    # When: a runtime is built
    rt.build_runtime(
        rt.RuntimeConfig(provider_name=name, model="m", style_name="probe", session="probe")
    )

    # Then: provider, tools, both presets and the subagent tool all registered
    assert co.get_agent_preset("outo") is not None
    assert co.get_agent_preset("subagent") is not None
    assert co.get_tool("Bash") is not None
    assert co.get_tool(rt.SUBAGENT_TOOL_NAME) is not None


def test_failed_resolve_leaves_no_stale_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a previous turn's hooks still registered globally
    def stale_hook(**_kwargs: object) -> None:
        return None

    co.register_hook(co.BEFORE_LLM_CALL, stale_hook)
    assert co.get_hooks(co.BEFORE_LLM_CALL)

    calls: list[bool] = []
    real_clear = providers.clear_coreouto_state

    def spy_clear() -> None:
        calls.append(True)
        real_clear()

    def boom(_overrides: object) -> object:
        raise RuntimeError("Provider 'gone' is not configured.")

    monkeypatch.setattr(providers, "clear_coreouto_state", spy_clear)
    monkeypatch.setattr(chat_mod, "resolve_runtime_from_settings", boom)

    # When: a send fails during resolve/build
    with pytest.raises(RuntimeError, match="not configured"):
        chat_mod.run_chat(chat_mod.ChatOptions(prompt="hi"))

    # Then: coreouto state was cleared, so no sink/persister hooks linger
    assert calls == [True]
    assert co.get_hooks(co.BEFORE_LLM_CALL) == []
