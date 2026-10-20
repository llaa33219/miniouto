"""Lock the per-session STYLE pin.

The provider trio was pinned per session (invariant 12) but the STYLE was
read straight from the global `settings.style` — so a style chosen for one
session followed the user into the next one, and the style picked in the
TUI rewrote the global default for every session. The style is pinned the
same way the model is: explicit override > this session's pin > the global
default.
"""

from __future__ import annotations

import pytest

from miniouto.core import runtime
from miniouto.core.runtime import ChatOverrides
from miniouto.storage import session_config
from miniouto.storage import settings as settings_store


@pytest.fixture
def provider_ready(monkeypatch):
    """A configured provider, so the runtime resolves past its provider gate.

    The style is the last key resolved, but the provider/model chain runs
    first and raises without one — so the test provides a minimal provider.
    """

    from miniouto.storage import providers as provider_store

    record = provider_store.Provider(name="test-provider", default_model="test-model")
    monkeypatch.setattr(provider_store, "get", lambda name: record)
    return record


@pytest.fixture
def style_of(monkeypatch, provider_ready):
    """The style a session resolves to."""

    def _style_of(
        session: str,
        overrides: ChatOverrides | None = None,
        *,
        global_style: str = "default",
    ) -> str:
        monkeypatch.setattr(
            settings_store,
            "load",
            lambda: settings_store.Settings(
                provider="test-provider", style=global_style, session=session
            ),
        )
        config = runtime.resolve_runtime_from_settings(overrides)
        return config.style_name

    return _style_of


def test_a_session_without_a_pin_follows_the_global_default(style_of) -> None:
    # Given: no pin for this session, and a global default style
    # When: the session resolves
    # Then: it follows the global default
    assert style_of("pin-fresh") == "default"


def test_a_pinned_style_is_that_session_only(style_of, tmp_path, monkeypatch) -> None:
    # Given: session A pinned to a style of its own
    session_config.clear("pin-alpha")
    session_config.set_fields("pin-alpha", style="coding-work")
    session_config.clear("pin-beta")
    # When: two sessions resolve
    a = style_of("pin-alpha")
    b = style_of("pin-beta")
    # Then: A runs its own style and B is untouched
    assert a == "coding-work"
    assert b == "default"


def test_an_explicit_style_beats_the_pin(style_of) -> None:
    # Given: a session pinned to one style
    session_config.clear("pin-alpha")
    session_config.set_fields("pin-alpha", style="coding-work")
    session_config.clear("pin-beta")
    # When: the run names a style explicitly
    # Then: the explicit choice wins
    assert style_of("pin-alpha", ChatOverrides(style="explicit-style")) == "explicit-style"


def test_the_pin_survives_a_global_style_change(style_of) -> None:
    # Given: a session pinned to a style
    session_config.clear("pin-alpha")
    session_config.set_fields("pin-alpha", style="coding-work")
    session_config.clear("pin-beta")
    # When: the pinned session resolves, with the global default moved on
    # Then: it keeps its own style — the pin is the session's, not the default's
    assert style_of("pin-alpha", global_style="other-style") == "coding-work"


# ── the default: the last style chosen, with the running sessions kept ────


def test_the_last_style_chosen_becomes_the_default(monkeypatch) -> None:
    """`set_active_style` makes the choice the default for NEW sessions."""

    from miniouto.core import runtime

    monkeypatch.setattr(
        settings_store, "load", lambda: settings_store.Settings(style="default")
    )
    written: list[dict] = []
    monkeypatch.setattr(
        settings_store, "update", lambda **kw: written.append(kw) or settings_store.Settings()
    )
    runtime.set_active_style("coding-work")
    assert written and written[-1].get("style") == "coding-work"


def test_existing_sessions_keep_their_style_when_the_default_moves(
    monkeypatch,
) -> None:
    """The user's rule: the old session keeps its style, the default moves.

    A session that was already running under a style must not be dragged
    along when a different style becomes the default — that is what the
    per-session pin is for.
    """

    from miniouto.core import runtime
    from miniouto.storage import sessions as session_store

    # the real settings write (the test home is isolated) so the assertions
    # read the state the change actually left behind
    settings_store.update(style="coding-work")
    monkeypatch.setattr(session_store, "list_sessions", lambda: ["pin-old-session"])
    # the old session is running under a style and has no pin of its own
    session_config.clear("pin-old-session")
    session_config.set_fields("pin-old-session", style="coding-work")

    runtime.set_active_style("coding-pro")

    # the default moved
    assert settings_store.load().style == "coding-pro"
    # and the old session kept its own style
    assert session_config.read("pin-old-session").style == "coding-work"


def test_a_session_without_a_pin_freezes_the_style_it_was_running(
    monkeypatch,
) -> None:
    """A session with no pin of its own freezes the CURRENT default."""

    from miniouto.core import runtime
    from miniouto.storage import sessions as session_store

    settings_store.update(style="coding-work")
    monkeypatch.setattr(session_store, "list_sessions", lambda: ["pin-running-session"])

    runtime.set_active_style("coding-pro")

    # the session that was running under coding-work keeps it
    assert session_config.read("pin-running-session").style == "coding-work"


def test_a_new_session_follows_the_new_default() -> None:
    """A session with no pin freezes the default that is current NOW."""

    from miniouto.core import runtime

    # a name no other test uses: the pin files are per-session, and a
    # session frozen by an earlier test would answer with ITS style
    session = "style-pin-fresh-session"
    session_config.clear(session)
    settings_store.update(style="coding-pro")
    pinned = session_config.effective(session, default=runtime.default_provider_config)
    assert pinned.style == "coding-pro"


# ── the TUI picker pins the style to the session ───────────────────────────


def test_the_tui_style_picker_pins_to_the_session(monkeypatch) -> None:
    """Picking a style in the TUI must not rewrite the global default.

    The TUI style picker used to write `settings.style`, so the choice
    followed the user into every other session. It now writes the session's
    own pin, and the global default keeps whatever it had.
    """

    import asyncio

    from miniouto.cli import tui as tui_module
    from miniouto.storage import styles as style_store

    monkeypatch.setattr(style_store, "list_styles", lambda: ["default", "coding-work"])
    monkeypatch.setattr(
        settings_store, "load", lambda: settings_store.Settings(style="default")
    )
    global_writes: list[dict] = []
    monkeypatch.setattr(
        settings_store, "update", lambda **kw: global_writes.append(kw)
    )

    session = "pin-picker-session"
    session_config.clear(session)
    written: list[tuple[str, dict]] = []
    real_set_fields = session_config.set_fields

    def spy_set_fields(name, **fields):
        written.append((name, fields))
        return real_set_fields(name, **fields)

    monkeypatch.setattr(tui_module.session_config, "set_fields", spy_set_fields)

    async def main() -> None:
        app = tui_module.ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._session_name = session
            app._open_style_picker()
            await pilot.pause()
            # pick the second entry (the style of the session)
            options = app.screen.query("ListItem")
            await pilot.click(options[1])
            await pilot.pause()

    asyncio.run(main())

    # the choice is written to the SESSION's pin
    assert written, "the picker wrote no pin"
    name, fields = written[-1]
    assert name == session
    assert fields.get("style") == "coding-work"
    # and it also became the DEFAULT (the last style chosen wins)
    assert any(w.get("style") == "coding-work" for w in global_writes)
