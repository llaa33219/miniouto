"""Regression tests for the five reported issues.

Each test names the failure it locks down:

1. the provider picker must OPEN even with no provider configured, so the
   first provider can be registered at all;
2. an outo provider/model edit must not reset the subagent;
3. the LAST change must become the global default;
4. a provider that refuses a multimodal payload must return a tool
   result, not end the turn;
5. the `screen` action must say it is a TEXT RENDER, so a model that
   cannot receive images does not mistake the art for the screen.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import coreouto as co
import pytest

from miniouto.cli import tui as tui_module
from miniouto.core import runtime as rt
from miniouto.storage import providers as provider_store
from miniouto.storage import session_config
from miniouto.storage import settings as settings_store
from miniouto.tools import multimodal


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    """Every test owns the whole registry and the settings.

    These tests move the GLOBAL default (that is what issues 2 and 3 are
    about), so they must not leave it moved for the next test — and they
    must not run against a registry another test left behind.
    """

    saved_settings = settings_store.load()
    saved_providers = provider_store.load_all()
    saved_pin = session_config.read("issue-session")

    # The registry is emptied for the duration: the "no providers yet"
    # test needs an empty one, and the others need a known pair.
    for name in list(provider_store.load_all()):
        provider_store.remove(name)
    for name in ("issue-session", "issue-fresh", "issue1-session"):
        session_config.clear(name)

    yield

    for name in ("issue-x", "issue-y"):
        provider_store.remove(name)
    for name in ("issue-session", "issue-fresh", "issue1-session"):
        session_config.clear(name)
    for record in saved_providers.values():
        provider_store.upsert(record)
    settings_store.save(saved_settings)
    if saved_pin is not None:
        session_config.set_fields(
            "issue-session",
            provider=saved_pin.provider,
            model=saved_pin.model,
            reasoning=saved_pin.reasoning,
            subagent_provider=saved_pin.subagent_provider,
            subagent_model=saved_pin.subagent_model,
            subagent_reasoning=saved_pin.subagent_reasoning,
            style=saved_pin.style,
        )

# ── issue 1: the provider picker opens with no providers ─────────────────────


def test_the_provider_picker_opens_with_no_provider_configured() -> None:
    """The "add" entries are the only way to create the first provider.

    With an empty registry the picker used to report "No providers
    configured" and return — leaving no way to add one from the TUI.
    """

    from miniouto.cli.tui import (
        OUTO_TARGET,
        ChatTUI,
        ProviderSettingsPopup,
        SelectionModal,
    )

    async def body() -> None:
        assert provider_store.load_all() == {}, "the fixture must start empty"
        app = ChatTUI()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._session_name = "issue1-session"
            settings_store.update(session="issue1-session")
            session_config.clear("issue1-session")
            app._chat_started = True

            app._open_provider_settings(OUTO_TARGET)
            await pilot.pause()
            assert isinstance(app.screen, ProviderSettingsPopup)

            # The Provider row opens the picker...
            await pilot.click(app.screen.query("SettingRow")[0], offset=(2, 0))
            await pilot.pause()
            assert isinstance(app.screen, SelectionModal), "the picker did not open"

            # ...and the picker offers the two ways to add a provider.
            labels = [
                str(item.query_one("Label").render())
                for item in app.screen.query("ListItem")
            ]
            assert any("add from catalog" in label for label in labels), labels
            assert any("add custom" in label for label in labels), labels

    asyncio.run(body())


def _model_of(pm: str) -> str:
    """The MODEL part of a `--pm` value (`provider/model[:reasoning]`)."""

    return pm.split("/", 1)[1].split(":", 1)[0]


# ── issues 2 and 3: the outo trio and the global default ────────────────────


@pytest.fixture
def two_providers() -> None:
    provider_store.upsert(
        provider_store.Provider(name="issue-x", api_format="openai", default_model="x-model")
    )
    provider_store.upsert(
        provider_store.Provider(name="issue-y", api_format="openai", default_model="y-model")
    )
    settings_store.update(provider="issue-x")
    session_config.clear("issue-session")
    yield
    provider_store.remove("issue-x")
    provider_store.remove("issue-y")
    session_config.clear("issue-session")


def test_an_outo_model_edit_does_not_move_the_subagent(two_providers: None) -> None:
    """An outo edit must not drag the subagent to a different model.

    The subagent inherits outo's provider and was resolved from the
    PROVIDER RECORD's `default_model` — the very value an outo model
    edit rewrote. The delegation command's `--pm` therefore changed
    with outo's model, and every delegated run restarted on a new
    model: the "an outo change resets the subagent" symptom.
    """

    # The session already ran once, so it has a pin: the subagent was
    # running this model.
    session_config.set_fields(
        "issue-session", provider="issue-x", model="x-model", reasoning="high"
    )
    # The subagent has its OWN settings, so the outo edit must not
    # reach it.
    settings_store.save(
        settings_store.Settings(
            provider="issue-x",
            model="x-model",
            style="default",
            subagent_provider="issue-y",
            subagent_model="y-model",
        )
    )
    before_model = rt._effective_subagent_pm("issue-session").split("/")[1].split(":")[0]
    assert before_model == "y-model"

    tui_module._persist_trio(
        "issue-session", tui_module.OUTO_TARGET, ("issue-x", "x-model-2", "low")
    )

    after_model = rt._effective_subagent_pm("issue-session").split("/")[1].split(":")[0]
    assert after_model == before_model, "the subagent's model moved with outo's model edit"
    # And the subagent's own settings are untouched.
    stored = settings_store.load()
    assert (stored.subagent_provider, stored.subagent_model) == ("issue-y", "y-model")


def test_an_outo_model_edit_does_not_move_the_subagent_without_a_pin(
    two_providers: None,
) -> None:
    """The same holds for a session that has not pinned anything yet.

    Its subagent has its own settings in the GLOBAL default, so the
    outo edit must not reach those either.
    """

    session_config.clear("issue-session")
    settings_store.save(
        settings_store.Settings(
            provider="issue-x",
            model="x-model",
            style="default",
            subagent_provider="issue-y",
            subagent_model="y-model",
        )
    )
    before = rt._effective_subagent_pm("issue-session")
    assert before == "issue-y/y-model"

    tui_module._persist_trio(
        "issue-session", tui_module.OUTO_TARGET, ("issue-x", "x-model-2", "low")
    )

    assert _model_of(rt._effective_subagent_pm("issue-session")) == _model_of(before)


def test_an_outo_model_edit_does_not_move_a_subagent_on_its_own_provider(
    two_providers: None,
) -> None:
    """A subagent with a provider of its own never followed outo."""

    settings_store.save(
        settings_store.Settings(
            provider="issue-x",
            model="x-model",
            style="default",
            subagent_provider="issue-y",
            subagent_model="y-model",
        )
    )
    session_config.clear("issue-session")
    before = rt._effective_subagent_pm("issue-session")
    assert before == "issue-y/y-model"

    tui_module._persist_trio(
        "issue-session", tui_module.OUTO_TARGET, ("issue-x", "x-model-2", "low")
    )

    assert _model_of(rt._effective_subagent_pm("issue-session")) == _model_of(before)


def test_an_outo_edit_never_moves_a_configured_subagent(two_providers: None) -> None:
    """A subagent with its OWN settings is untouched by an outo edit.

    Three shapes, one contract: the subagent's provider, model and
    reasoning do not follow outo.

    * its own provider AND model — the fully configured case;
    * its own provider, no model — it took that provider's endpoint
      default, which outo's edit used to rewrite;
    * outo's own provider, no model — the endpoint default on the
      SHARED provider, which outo's edit used to rewrite.
    """

    shapes = (
        ("own provider and model", "issue-y", "y-model"),
        ("own provider, no model", "issue-y", ""),
        ("outo's provider, no model", "issue-x", ""),
    )
    for label, sub_provider, sub_model in shapes:
        # A fresh session for every shape: the pin left by the previous
        # shape would answer for this one.
        session_config.clear("issue-session")
        settings_store.save(
            settings_store.Settings(
                provider="issue-x",
                model="x-model",
                style="default",
                subagent_provider=sub_provider,
                subagent_model=sub_model,
            )
        )
        session_config.set_fields(
            "issue-session", provider="issue-x", model="x-model"
        )

        before = rt._effective_subagent_pm("issue-session")
        tui_module._persist_trio(
            "issue-session", tui_module.OUTO_TARGET, ("issue-x", "x-model-2", "low")
        )
        after = rt._effective_subagent_pm("issue-session")

        assert _model_of(after) == _model_of(before), (
            f"{label}: the subagent's model moved with outo's edit "
            f"({before!r} -> {after!r})"
        )


def test_same_as_outo_follows_outo_live(two_providers: None) -> None:
    """"Same as outo" is a LIVE link, not a snapshot.

    The empty subagent keys mean "whatever outo runs" — so an outo edit
    must reach the subagent. Freezing it here would be the opposite of
    what those keys say.
    """

    session_config.clear("issue-session")
    settings_store.save(
        settings_store.Settings(provider="issue-x", model="x-model", style="default")
    )
    session_config.set_fields("issue-session", provider="issue-x", model="x-model")

    before = rt._effective_subagent_pm("issue-session")
    assert _model_of(before) == "x-model"

    tui_module._persist_trio(
        "issue-session", tui_module.OUTO_TARGET, ("issue-x", "x-model-2", "low")
    )

    after = rt._effective_subagent_pm("issue-session")
    assert _model_of(after) == "x-model-2", (
        f"same as outo did not follow outo's new model ({before!r} -> {after!r})"
    )


def test_a_named_provider_always_carries_a_model(two_providers: None) -> None:
    """There is no "provider but no model" state.

    Every trio key is either set or "same as outo" — a middle state
    would leave every reader guessing which model that provider means,
    and a later write of the model alone would pair the two halves of
    two different providers.
    """

    settings_store.save(
        settings_store.Settings(
            provider="issue-y",
            model="",
            style="default",
            subagent_provider="issue-x",
            subagent_model="",
        )
    )
    stored = settings_store.load()
    assert stored.provider == "issue-y" and stored.model == "y-model"
    assert stored.subagent_provider == "issue-x" and stored.subagent_model == "x-model"

    # The same holds for a session pin.
    session_config.set_fields("issue-session", provider="issue-y", model="")
    pinned = session_config.read("issue-session")
    assert pinned is not None
    assert pinned.provider == "issue-y" and pinned.model == "y-model"


def test_clearing_the_model_alone_still_hands_it_back_to_the_default(
    two_providers: None,
) -> None:
    """Clearing a key keeps its documented meaning.

    Only a provider that JUST CHANGED is completed; clearing the model
    on its own means "follow the default again", which is the whole
    point of an empty pin key.
    """

    session_config.set_fields("issue-session", provider="issue-x", model="x-model")
    session_config.set_fields("issue-session", model="")
    pinned = session_config.read("issue-session")
    assert pinned is not None
    assert pinned.provider == "issue-x"
    assert pinned.model == "", "clearing the model must hand it back to the default"


def test_the_chip_shows_the_subagent_trio_from_the_global_default(
    two_providers: None,
) -> None:
    """The chip must not show "same as outo" for a configured subagent.

    A session pin that names only outo's trio leaves the subagent keys
    empty — and those empty keys mean "whatever the subagent default
    says", not "nothing is configured". Reading the pin ALONE therefore
    showed "same as outo" while the subagent was in fact configured
    (the display/config mismatch reported).
    """

    settings_store.save(
        settings_store.Settings(
            provider="issue-x",
            model="x-model",
            style="default",
            subagent_provider="issue-y",
            subagent_model="y-model",
            subagent_reasoning="high",
        )
    )
    # The pin names ONLY outo — the subagent trio lives in the default.
    session_config.set_fields("issue-session", provider="issue-x", model="x-model")

    display = tui_module._trio_display(
        tui_module.SUBAGENT_TARGET, tui_module._effective_config("issue-session")
    )
    assert display == ("issue-y", "y-model", "high"), (
        f"the chip showed {display} while the subagent is configured as y-model/high"
    )


def test_an_outo_edit_does_not_disturb_the_subagent_display(
    two_providers: None,
) -> None:
    """The subagent chip stays correct after an outo edit."""

    settings_store.save(
        settings_store.Settings(
            provider="issue-x",
            model="x-model",
            style="default",
            subagent_provider="issue-y",
            subagent_model="y-model",
            subagent_reasoning="high",
        )
    )
    session_config.set_fields("issue-session", provider="issue-x", model="x-model")

    tui_module._persist_trio(
        "issue-session", tui_module.OUTO_TARGET, ("issue-x", "x-model-2", "low")
    )

    display = tui_module._trio_display(
        tui_module.SUBAGENT_TARGET, tui_module._effective_config("issue-session")
    )
    assert display == ("issue-y", "y-model", "high")


def test_a_fresh_session_follows_the_last_change(two_providers: None) -> None:
    """Reopening must not ignore the last edit.

    The last outo edit is the new global default, so a session with no
    pin of its own starts there.
    """

    settings_store.save(
        settings_store.Settings(provider="issue-x", model="x-model", style="default")
    )
    session_config.set_fields("issue-session", provider="issue-x", model="x-model")

    tui_module._persist_trio(
        "issue-session", tui_module.OUTO_TARGET, ("issue-y", "y-model", "high")
    )

    fresh = session_config.effective("issue-fresh", default=rt.default_provider_config)
    assert (fresh.provider, fresh.model) == ("issue-y", "y-model")


def test_the_last_change_becomes_the_global_default(two_providers: None) -> None:
    """The newest outo pick is what a fresh session resolves."""

    tui_module._persist_trio(
        "issue-session", tui_module.OUTO_TARGET, ("issue-x", "x-model-2", "low")
    )

    default = rt.default_provider_config()
    assert default.provider == "issue-x"
    assert default.model == "x-model-2"
    assert default.reasoning == "low"

    # And a session with no pin of its own starts there.
    fresh = session_config.effective("issue-fresh", default=rt.default_provider_config)
    assert (fresh.provider, fresh.model) == ("issue-x", "x-model-2")


def test_switching_the_provider_switches_the_model_with_it(two_providers: None) -> None:
    """Provider and model are one paired choice.

    `settings.update(provider=...)` without a model left the previous
    provider's model behind — outo would then have run a model the new
    provider has never heard of.
    """

    settings_store.save(
        settings_store.Settings(provider="issue-x", model="x-model", style="default")
    )
    settings_store.update(provider="issue-y")

    default = rt.default_provider_config()
    assert default.provider == "issue-y"
    assert default.model == "y-model"


# ── issue 4: a refused multimodal payload must not end the turn ──────────────


class _RefusingProvider:
    """A provider double that refuses multimodal tool results.

    Mirrors coreouto's OpenAI provider, which raises from
    `format_tool_result` when a tool result carries blocks.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def create(
        self,
        messages: list[Any],
        *,
        model: str,
        tools: list[Any] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> co.LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return co.LLMResponse(
                content="",
                tool_calls=[
                    co.ToolCall(id="c1", name="Image", arguments={"file_path": "/tmp/x.png"})
                ],
                finish_reason="tool_calls",
            )
        return co.LLMResponse(
            content="Continued without the image.", tool_calls=[], finish_reason="stop"
        )

    def format_assistant_message(self, response: co.LLMResponse) -> co.Message:
        return co.Message(
            role="assistant", content=response.content, tool_calls=response.tool_calls or []
        )

    def format_tool_result(self, tool_call: co.ToolCall, result: co.ToolResult) -> co.Message:
        if result.blocks is not None:
            raise ValueError(
                "OpenAI Chat Completions does not support multimodal tool results "
                "(image block detected)."
            )
        return co.Message(
            role="tool",
            tool_call_id=tool_call.id,
            content=result.content,
            name=tool_call.name,
        )


def test_a_refused_multimodal_payload_becomes_a_tool_result() -> None:
    """The refusal is a tool RESULT, not a dead turn.

    The provider raises from `format_tool_result`, which coreouto calls
    outside the tool-call wrapper — so the exception used to end the whole
    turn with the model never learning why.
    """

    provider = _RefusingProvider()
    multimodal.wrap_provider(provider)

    tool_call = co.ToolCall(id="c1", name="Image", arguments={})
    result = co.ToolResult(
        tool_call_id="c1",
        blocks=[
            co.TextBlock(text="Image at /tmp/x.png (10 bytes, image/png)."),
            co.ImageBlock(data=b"\x89PNG", mime_type="image/png"),
        ],
    )

    message = provider.format_tool_result(tool_call, result)
    assert message.role == "tool"
    assert "could not return the image" in message.content
    # The model is told what to do instead of retrying the same call.
    assert "PROVIDER / MODEL" in message.content
    assert "Do NOT retry" in message.content


def test_a_refused_multimodal_payload_keeps_the_loop_alive() -> None:
    """The agent loop CONTINUES after the refusal."""

    provider = _RefusingProvider()
    multimodal.wrap_provider(provider)
    co.register_provider("issue-refuser", provider)

    def handler(file_path: str) -> list:
        return [
            co.TextBlock(text="Image at /tmp/x.png (10 bytes, image/png)."),
            co.ImageBlock(data=b"\x89PNG", mime_type="image/png"),
        ]

    co.register_tool("Image", description="view an image")(handler)

    async def body() -> None:
        config = co.AgentConfig(
            name="issue",
            provider="issue-refuser",
            model="m",
            system_prompt="you are outo",
            tools=["Image"],
            max_iterations=5,
        )
        response = await co.Agent(config).call("show me the image")
        assert response.content == "Continued without the image."
        assert provider.calls == 2, "the loop died instead of continuing"
        tool_messages = [m for m in response.messages if m.role == "tool"]
        assert tool_messages, "the refusal was not returned as a tool result"

    asyncio.run(body())


# ── issue 5: the `screen` action must label itself ──────────────────────────


def test_the_screen_result_says_it_is_a_text_render() -> None:
    """The art must not be mistakable for a picture of the screen.

    A model that reads the block characters as if they were the screen
    misjudges what is on it — and the cells are an approximation of
    screen pixels, not the coordinates a click needs.
    """

    from miniouto.tools import registry

    description = registry._computer_description()
    assert "TEXT RENDERING of the screen, NOT a picture" in description

    # And the screenshot refusal tells the model not to fall back to it.
    refusal = registry._SCREENSHOT_UNSUPPORTED
    assert "Do NOT switch to the `screen` action" in refusal
    assert "COARSE TEXT RENDERING" in refusal
