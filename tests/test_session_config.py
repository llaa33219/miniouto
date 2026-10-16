"""The per-session provider pin: freeze-at-first-use, seeding, tolerance.

Provider settings used to be global only, so a model switch in one process
silently redirected every other process. `storage/session_config.py` pins
the six provider keys per session instead. What matters is not the file
format but the contract around it: a session keeps the settings it was
using, a freshly minted session can inherit them, and a broken pin never
takes a run down.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from miniouto.core import runtime as rt
from miniouto.storage import providers as provider_store
from miniouto.storage import session_config
from miniouto.storage import settings as settings_store
from miniouto.storage.session_config import ProviderConfig


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    """Each test owns its session names; pins outlive the test otherwise."""

    names = (
        "sc-freeze",
        "sc-other",
        "sc-seeded",
        "sc-new",
        "sc-source",
        "sc-corrupt",
        "sc-cleared",
    )
    for name in names:
        session_config.clear(name)
    yield
    for name in names:
        session_config.clear(name)


def _install(name: str, default_model: str) -> None:
    provider_store.upsert(
        provider_store.Provider(
            name=name,
            api_format="openai",
            base_url="http://127.0.0.1:9/v1",
            api_key="k",
            default_model=default_model,
        )
    )


def _default(model: str = "default-model") -> Callable[[], ProviderConfig]:
    return lambda: ProviderConfig(provider="probe", model=model)


def test_the_pin_freezes_at_first_use_and_a_later_default_cannot_move_it() -> None:
    # Given: a session that has never been configured, and a default
    session_config.effective("sc-freeze", default=_default("model-a"))

    # When: the global default changes afterwards, as a TUI model switch does
    frozen = session_config.effective("sc-freeze", default=_default("model-b"))

    # Then: the session keeps what it was using
    assert frozen.model == "model-a"
    assert session_config.read("sc-freeze") == ProviderConfig(
        provider="probe", model="model-a"
    )


def test_a_session_with_no_pin_follows_the_current_default() -> None:
    # Given/When: a brand-new session resolves against a changed default
    config = session_config.effective("sc-cleared", default=_default("model-b"))

    # Then: it takes the default AND freezes it for next time
    assert config.model == "model-b"
    assert session_config.read("sc-cleared") == ProviderConfig(
        provider="probe", model="model-b"
    )


def test_set_fields_moves_only_the_session_it_names() -> None:
    # Given: two sessions, both pinned to the same default
    session_config.effective("sc-freeze", default=_default("model-a"))
    session_config.effective("sc-other", default=_default("model-a"))

    # When: one of them is reconfigured
    updated = session_config.set_fields("sc-freeze", model="pinned-model")

    # Then: the named session takes the new value...
    assert updated.model == "pinned-model"
    assert session_config.effective("sc-freeze").model == "pinned-model"
    # ...and the untouched session is exactly as it was
    assert session_config.effective("sc-other").model == "model-a"


def test_clearing_a_key_hands_that_key_back_to_the_default() -> None:
    # Given: a session whose model was pinned
    session_config.set_fields("sc-freeze", provider="probe", model="pinned-model")

    # When: the pin entry is emptied
    session_config.set_fields("sc-freeze", model="")

    # Then: the key reads as unset, so the default applies again
    assert session_config.read("sc-freeze") == ProviderConfig(provider="probe")


def test_seed_from_copies_the_pin_without_overwriting_an_existing_one() -> None:
    # Given: a source session with a pinned model, and a target that has
    # its own pin already
    session_config.set_fields("sc-source", provider="probe", model="source-model")
    session_config.set_fields("sc-seeded", provider="other", model="target-model")

    # When: a freshly minted session inherits the source's settings...
    session_config.seed_from("sc-new", "sc-source")
    # ...and a second mint asks again for a session that already has one
    session_config.seed_from("sc-seeded", "sc-source")

    # Then: the new session copies the pin, and the pre-existing pin stands
    assert session_config.read("sc-new") == ProviderConfig(
        provider="probe", model="source-model"
    )
    assert session_config.read("sc-seeded").model == "target-model"


def test_seeding_from_a_source_without_a_pin_seeds_nothing() -> None:
    # Given/When: a mint from a session that was never configured
    session_config.seed_from("sc-new", "sc-source")

    # Then: the new session is left for its own first-use freeze
    assert session_config.read("sc-new") is None


def test_a_corrupt_pin_is_ignored_rather_than_raised_over() -> None:
    # Given: a pin file that is not valid TOML
    path = session_config.path_for("sc-corrupt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not = = toml", encoding="utf-8")

    # When/Then: reading treats it as absent, and the default takes over
    assert session_config.read("sc-corrupt") is None
    config = session_config.effective("sc-corrupt", default=_default("model-a"))
    assert config.model == "model-a"


def test_the_default_computed_by_runtime_mirrors_the_global_settings() -> None:
    # Given: settings naming a provider, with a subagent override
    _install("probe", "probe-model")
    settings_store.save(
        settings_store.Settings(
            provider="probe",
            subagent_provider="probe",
            subagent_model="sub-model",
            subagent_reasoning="high",
        )
    )

    # When: the runtime computes the default a pin freezes
    default = rt.default_provider_config()

    # Then: the provider's default_model and reasoning_effort come from
    # the resolved provider record, exactly as the pre-pin chain did
    assert default.provider == "probe"
    assert default.model == "probe-model"
    assert default.subagent_model == "sub-model"
    assert default.subagent_reasoning == "high"
