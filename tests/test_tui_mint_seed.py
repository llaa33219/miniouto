"""The mint's seed source: a pre-mint popup edit must reach the minted session.

A provider-settings change made BEFORE the first message lands on this TUI's
fallback pin (``tui-pending-…`` — what ``_pinned_session`` hands the popup
before a session exists). The popup's write is ``config set --session
tui-pending-…``, so the pin AND the global default both move; but the mint used
to seed the fresh ``tui-…`` name from the PUBLISHED active session's pin —
some other, stale session — silently freezing the OLD model over the user's
last choice. These tests lock the two-part rule:

1. a pre-mint pin on the pending name IS the seed source (the user's latest
   choice for THIS conversation): whole pin, copy-if-absent, CLI lane;
2. with no pre-mint edit, the published active session stays the source (the
   intended "continue the conversation you were in" default).

Everything runs headless through ``run_test`` with the chat child stubbed; the
config writes go through the REAL bridge CLI children, exactly as the popup
does, on the shared isolated ``MINIOUTO_HOME`` the suite boots with.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import tui_harness
from miniouto.cli.tui import ChatTUI
from miniouto.cli.tui import app as app_module
from miniouto.storage import providers as provider_store
from miniouto.storage import session_config
from miniouto.storage import settings as settings_store

BOOT = tui_harness.BOOT_SIZE


@pytest.fixture(autouse=True)
def _restore_settings() -> Any:
    """Put ``settings.toml`` back after every test in this module.

    The trio writes these tests make (through the real CLI lane) rewrite the
    GLOBAL default and publish the active session — that IS the behaviour
    under test — but the settings file is shared, so an unrestored write would
    decide the boot theme (and the seed source) of every test that runs after.
    """

    saved = settings_store.load()
    yield
    settings_store.save(saved)


@pytest.fixture
def two_providers() -> None:
    """Two providers with different default models: `mint-old` / `mint-new`.

    The pair every test below switches between: the old model the published
    session froze, and the new one the pre-mint popup edit picks. Removed at
    teardown so the shared store stays clean.
    """

    for name, model in (("mint-old", "old-model"), ("mint-new", "new-model")):
        provider_store.upsert(
            provider_store.Provider(name=name, api_format="openai", default_model=model)
        )
    yield
    provider_store.remove("mint-old")
    provider_store.remove("mint-new")


def _publish_old_session() -> None:
    """Yesterday's conversation: published active session with an old-model pin."""

    settings_store.save(
        settings_store.Settings(
            provider="mint-old", model="old-model", session="mint-old-session"
        )
    )
    session_config.set_fields("mint-old-session", provider="mint-old", model="old-model")


def _pre_mint_edit(app: ChatTUI, target: str, provider: str, model: str, reasoning: str) -> None:
    """Make one popup-shaped trio edit on this TUI's fallback pin (CLI lane)."""

    ok, message = app_module.bridge.config_set_trio(
        app._pinned_session(), target, provider, model, reasoning
    )
    assert ok, message


async def _first_turn(app: ChatTUI, prompt: str = "hello") -> None:
    """Dispatch the first message, which mints this TUI's own session."""

    await app._dispatch(prompt)
    assert app._session_name is not None
    assert app._session_name.startswith("tui-"), app._session_name
    assert not app._session_name.startswith("tui-pending-"), app._session_name


def _minted_pin(app: ChatTUI) -> session_config.ProviderConfig:
    """Read the minted session's pin, failing loudly when nothing was seeded."""

    pinned = session_config.read(app._session_name)
    assert pinned is not None, "the mint seeded nothing"
    return pinned


@pytest.fixture
def routing_stub(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the chat child, recording the session each turn routed on."""

    routed: list[str] = []
    tui_harness.stub_run_chat(monkeypatch, [], on_session=routed.append)
    # The idle poll's inbox listing is also a CLI child; nothing is queued in
    # these tests, so answer empty without a process.
    monkeypatch.setattr(app_module.bridge, "message_list", lambda session: [])
    return routed


# ── (a) THE BUG: a pre-mint edit must win over the stale published pin ─────


def test_a_pre_mint_popup_edit_reaches_the_minted_session(
    routing_stub: list[str], two_providers: None
) -> None:
    async def body() -> None:
        _publish_old_session()
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            # Given: the popup opened BEFORE the first message, so its write
            # lands on this TUI's fallback pin (tui-pending-…)
            pending = app._pinned_session()
            assert pending.startswith("tui-pending-"), pending
            _pre_mint_edit(app, "outo", "mint-new", "new-model", "")
            # When: the first message mints this TUI's own session
            await _first_turn(app)
            await pilot.pause()
            # Then: the minted pin carries the user's LAST choice — the new
            # provider AND the new model — not the published session's old pin
            pinned = _minted_pin(app)
            assert (pinned.provider, pinned.model) == ("mint-new", "new-model"), pinned
            # And: the turn routed on the minted session
            assert routing_stub == [app._session_name], routing_stub
            # And: the global default the popup also wrote stays the new one
            saved = settings_store.load()
            assert (saved.provider, saved.model) == ("mint-new", "new-model")
            # And: (e) the pending pin is left in place (a unique name; the
            # TUI never mutates storage to tidy it) — the minted pin is the
            # one that matters
            assert session_config.read(pending) is not None

    asyncio.run(body())


# ── (b) no pre-mint edit: the published session still seeds the mint ───────


def test_without_a_pre_mint_edit_the_published_session_still_seeds(
    routing_stub: list[str], two_providers: None
) -> None:
    async def body() -> None:
        _publish_old_session()
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            assert session_config.read(app._pinned_session()) is None
            # When: the first message mints, with NO pre-mint edit made
            await _first_turn(app)
            await pilot.pause()
            # Then: the minted pin seeds from the published active session —
            # continuing that conversation's config is the intended default
            pinned = _minted_pin(app)
            assert (pinned.provider, pinned.model) == ("mint-old", "old-model"), pinned
            # And: the global default is untouched by the seed
            saved = settings_store.load()
            assert (saved.provider, saved.model) == ("mint-old", "old-model")

    asyncio.run(body())


# ── (c) no published pin: the pre-mint edit alone reaches the mint ─────────


def test_with_no_published_pin_a_pre_mint_edit_still_reaches_the_mint(
    routing_stub: list[str], two_providers: None
) -> None:
    async def body() -> None:
        # Given: a published active session that carries NO pin — the same
        # shape as no published session at all (an empty source seeds nothing)
        settings_store.save(
            settings_store.Settings(provider="mint-old", model="old-model")
        )
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            _pre_mint_edit(app, "outo", "mint-new", "new-model", "")
            # When: the first message mints
            await _first_turn(app)
            await pilot.pause()
            # Then: the pre-mint choice is on the minted pin explicitly (it
            # used to arrive only by freezing the new default at first use)
            pinned = _minted_pin(app)
            assert (pinned.provider, pinned.model) == ("mint-new", "new-model"), pinned

    asyncio.run(body())


# ── (d) a pre-mint SUBAGENT edit carries over too (the whole pin) ──────────


def test_a_pre_mint_subagent_edit_carries_over_the_whole_pin(
    routing_stub: list[str], two_providers: None
) -> None:
    async def body() -> None:
        _publish_old_session()
        app = ChatTUI()
        async with app.run_test(size=BOOT) as pilot:
            await pilot.pause()
            # Given: the SUBAGENT chip's popup edited before the first message
            _pre_mint_edit(app, "subagent", "mint-new", "sub-model", "high")
            # When: the first message mints
            await _first_turn(app)
            await pilot.pause()
            # Then: the seed copied the WHOLE pending pin — the subagent trio
            # (all three keys); outo's own keys stay empty
            pinned = _minted_pin(app)
            assert (
                pinned.subagent_provider,
                pinned.subagent_model,
                pinned.subagent_reasoning,
            ) == ("mint-new", "sub-model", "high"), pinned
            assert (pinned.provider, pinned.model) == ("", ""), pinned

    asyncio.run(body())
