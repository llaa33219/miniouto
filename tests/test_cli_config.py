"""The `config` command group: show (read-only) and set (the trio write).

`config set` is the CLI form of the TUI settings popup's `_persist_trio`
(invariant 12): every trio edit writes BOTH the session pin and the global
default, the outo trio writes the paired settings keys through `save`, and
the provider record's `default_model` follows the pick while its
`reasoning_effort` stays. `config show` is the read side — it must reproduce
the pin + merged-subagent interpretation and never freeze a pin.
"""

from __future__ import annotations

import uuid

import pytest
import yaml
from typer.testing import CliRunner

from miniouto.cli import config as config_mod
from miniouto.storage import providers as provider_store
from miniouto.storage import session_config
from miniouto.storage import settings as settings_store

runner = CliRunner()


def _session() -> str:
    return f"config-cli-{uuid.uuid4().hex[:8]}"


def _load(output: str) -> dict:
    """Parse the command's stdout as exactly one YAML document."""

    docs = list(yaml.safe_load_all(output))
    assert len(docs) == 1, f"expected exactly one YAML document, got {len(docs)}"
    assert isinstance(docs[0], dict)
    return docs[0]


@pytest.fixture
def _clean(tmp_path, monkeypatch):
    """One test, one isolated store: settings, providers, and the pin directory.

    The shared test home accumulates state across tests (see
    test_cli_yaml.py's status test for the same trick), and `config set`
    writes the GLOBAL settings, so each test points every store at its own
    scratch directory instead.
    """

    monkeypatch.setattr(settings_store, "SETTINGS_FILE", tmp_path / "settings.toml")
    monkeypatch.setattr(provider_store, "PROVIDERS_FILE", tmp_path / "providers.toml")
    monkeypatch.setattr(session_config, "SESSION_CONFIG_DIR", tmp_path / "session-config")


def _install(name: str, default_model: str = "", **kwargs) -> None:
    kwargs.setdefault("api_format", "openai")
    provider_store.upsert(
        provider_store.Provider(name=name, default_model=default_model, **kwargs)
    )


# ── config show ───────────────────────────────────────────────────────────────


def test_show_reports_pin_and_effective_for_an_unpinned_session(_clean) -> None:
    # Given: a global default, no pin for the session
    _install("prov-a", default_model="m-a")
    settings_store.save(settings_store.Settings(provider="prov-a", model="m-a"))

    session = _session()

    # When: the config is shown
    result = runner.invoke(config_mod.app, ["show", "--session", session])

    # Then: the pin is empty (nothing stored), the effective trio follows the
    # global default, and no pin file was frozen by the read
    assert result.exit_code == 0
    doc = _load(result.output)
    assert doc["session"] == session
    assert list(doc) == ["session", "pin", "effective"]
    assert list(doc["pin"]) == [
        "provider", "model", "reasoning",
        "subagent_provider", "subagent_model", "subagent_reasoning", "style",
    ]
    assert doc["pin"] == {key: "" for key in doc["pin"]}
    assert list(doc["effective"]) == [
        "provider", "model", "reasoning", "style",
        "subagent_provider", "subagent_model", "subagent_reasoning",
    ]
    assert doc["effective"]["provider"] == "prov-a"
    assert doc["effective"]["model"] == "m-a"
    assert session_config.read(session) is None  # the read froze nothing


def test_show_reports_a_raw_pin_with_default_inherited_subagent(_clean) -> None:
    # Given: a session pinning outo's trio only, and a global subagent default
    _install("prov-a", default_model="m-a")
    _install("prov-b", default_model="m-b")
    session = _session()
    session_config.set_fields(session, provider="prov-a", model="m-a", reasoning="high")
    settings_store.save(
        settings_store.Settings(
            provider="prov-b",
            model="m-b",
            subagent_provider="prov-b",
            subagent_model="m-b",
            subagent_reasoning="low",
            style="coding-work",
        )
    )

    # When: the config is shown
    result = runner.invoke(config_mod.app, ["show", "--session", session])

    # Then: pin keeps the RAW stored values, effective merges ONLY the
    # subagent trio from the default (the outo trio stays as the pin has it)
    assert result.exit_code == 0
    doc = _load(result.output)
    assert doc["pin"]["provider"] == "prov-a"
    assert doc["pin"]["subagent_provider"] == ""
    assert doc["effective"]["provider"] == "prov-a"
    assert doc["effective"]["model"] == "m-a"
    assert doc["effective"]["reasoning"] == "high"
    assert doc["effective"]["subagent_provider"] == "prov-b"
    assert doc["effective"]["subagent_model"] == "m-b"
    assert doc["effective"]["subagent_reasoning"] == "low"
    # The STYLE stays as the pin has it: `_merged_config` fills ONLY the
    # subagent trio from the default, so a pin without a style key reads ""
    # (the runtime's own pin-or-global resolution answers at turn time).
    assert doc["effective"]["style"] == ""


def test_show_requires_a_session() -> None:
    # When: show runs without --session
    result = runner.invoke(config_mod.app, ["show"])

    # Then: a usage error, not a half document
    assert result.exit_code != 0
    assert result.stdout == ""


# ── config set --target: the trio write ───────────────────────────────────────


def test_set_outo_trio_writes_the_pin_the_settings_and_the_record(_clean) -> None:
    # Given: two configured providers
    _install("prov-a", default_model="m-a", reasoning_effort="low")
    _install("prov-b", default_model="m-b")
    settings_store.save(settings_store.Settings(provider="prov-a", model="m-a"))
    session = _session()

    # When: the outo trio is set for that session
    result = runner.invoke(
        config_mod.app,
        [
            "set", "--session", session, "--target", "outo",
            "--provider", "prov-b", "--model", "m-b", "--reasoning", "high",
        ],
    )

    # Then: one ✓ line, and the trio write landed in all three places
    assert result.exit_code == 0
    assert "✓" in result.output
    pinned = session_config.read(session)
    assert pinned is not None
    assert (pinned.provider, pinned.model, pinned.reasoning) == ("prov-b", "m-b", "high")
    s = settings_store.load()
    assert (s.provider, s.model, s.reasoning) == ("prov-b", "m-b", "high")
    # the record's default_model follows the pick; reasoning_effort does NOT
    assert provider_store.get("prov-b").default_model == "m-b"
    assert provider_store.get("prov-b").reasoning_effort is None
    assert provider_store.get("prov-a").reasoning_effort == "low"


def test_set_outo_trio_with_empty_reasoning_clears_it(_clean) -> None:
    # Given: a pin and settings carrying a reasoning effort
    _install("prov-a", default_model="m-a")
    session = _session()
    session_config.set_fields(session, provider="prov-a", model="m-a", reasoning="high")
    settings_store.save(settings_store.Settings(provider="prov-a", reasoning="high"))

    # When: the trio is re-written with an empty reasoning
    result = runner.invoke(
        config_mod.app,
        [
            "set", "--session", session, "--target", "outo",
            "--provider", "prov-a", "--model", "m-a", "--reasoning", "",
        ],
    )

    # Then: the stored reasoning is CLEARED everywhere (save, not update)
    assert result.exit_code == 0
    assert session_config.read(session).reasoning == ""
    assert settings_store.load().reasoning == ""


def test_set_subagent_trio_writes_the_pin_and_the_subagent_settings(_clean) -> None:
    # Given: an outo default and a second provider for the subagent
    _install("prov-a", default_model="m-a")
    _install("prov-b", default_model="m-b")
    settings_store.save(settings_store.Settings(provider="prov-a", model="m-a"))
    session = _session()

    # When: the subagent trio is set for that session
    result = runner.invoke(
        config_mod.app,
        [
            "set", "--session", session, "--target", "subagent",
            "--provider", "prov-b", "--model", "m-b", "--reasoning", "low",
        ],
    )

    # Then: the pin and the three subagent_* settings carry the trio, and the
    # outo half (settings + record) is untouched
    assert result.exit_code == 0
    pinned = session_config.read(session)
    assert (
        pinned.subagent_provider, pinned.subagent_model, pinned.subagent_reasoning
    ) == ("prov-b", "m-b", "low")
    s = settings_store.load()
    assert (s.subagent_provider, s.subagent_model, s.subagent_reasoning) == (
        "prov-b", "m-b", "low"
    )
    assert (s.provider, s.model) == ("prov-a", "m-a")
    assert provider_store.get("prov-a").default_model == "m-a"


def test_set_subagent_trio_with_empty_values_inherits_outo_again(_clean) -> None:
    # Given: a session with a subagent trio of its own
    _install("prov-a", default_model="m-a")
    _install("prov-b", default_model="m-b")
    session = _session()
    session_config.set_fields(
        session,
        subagent_provider="prov-b",
        subagent_model="m-b",
        subagent_reasoning="low",
    )
    settings_store.save(
        settings_store.Settings(
            provider="prov-a", subagent_provider="prov-b", subagent_model="m-b"
        )
    )

    # When: the subagent trio is re-written with empty values
    result = runner.invoke(
        config_mod.app,
        [
            "set", "--session", session, "--target", "subagent",
            "--provider", "", "--model", "", "--reasoning", "",
        ],
    )

    # Then: the pin keys and the settings keys are cleared — the subagent
    # follows outo again
    assert result.exit_code == 0
    pinned = session_config.read(session)
    assert (
        pinned.subagent_provider, pinned.subagent_model, pinned.subagent_reasoning
    ) == ("", "", "")
    s = settings_store.load()
    assert (s.subagent_provider, s.subagent_model, s.subagent_reasoning) == ("", "", "")


# ── config set: the usage rules ───────────────────────────────────────────────


def test_set_requires_at_least_one_action() -> None:
    # When: set runs with no action flag
    result = runner.invoke(config_mod.app, ["set"])

    # Then: a stderr ✗ with a non-zero exit, nothing on stdout
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "at least one of --target, --theme, or --active-session" in result.output


def test_set_rejects_trio_flags_without_a_target() -> None:
    # When: provider flags are passed with no --target
    result = runner.invoke(
        config_mod.app, ["set", "--provider", "prov-a", "--model", "m-a", "--reasoning", ""]
    )

    # Then: a usage error naming the missing target
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "--target" in result.output


def test_set_rejects_a_partial_trio() -> None:
    # Given: a configured provider
    _install("prov-a", default_model="m-a")
    session = _session()

    # When: the trio is named without --reasoning
    result = runner.invoke(
        config_mod.app,
        ["set", "--session", session, "--target", "outo", "--provider", "prov-a", "--model", "m-a"],
    )

    # Then: a usage error — the trio is one paired write
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "--reasoning" in result.output


def test_set_rejects_an_unknown_target(_clean) -> None:
    # When: a target outside outo|subagent is named
    result = runner.invoke(
        config_mod.app,
        ["set", "--session", "s", "--target", "both", "--provider", "p", "--model", "m",
         "--reasoning", ""],
    )

    # Then: a usage error
    assert result.exit_code == 1
    assert "outo" in result.output and "subagent" in result.output


def test_set_target_requires_a_session(_clean) -> None:
    # When: a trio write names no session
    result = runner.invoke(
        config_mod.app,
        ["set", "--target", "outo", "--provider", "p", "--model", "m", "--reasoning", ""],
    )

    # Then: a usage error — the pin write needs its session
    assert result.exit_code == 1
    assert "--session" in result.output


def test_set_rejects_an_unconfigured_provider(_clean) -> None:
    # Given: a session and no provider by that name
    session = _session()

    # When: the trio names a provider that is not configured
    result = runner.invoke(
        config_mod.app,
        [
            "set", "--session", session, "--target", "outo",
            "--provider", "nope", "--model", "m", "--reasoning", "",
        ],
    )

    # Then: a ✗ error, exit 1, nothing written
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "not configured" in result.output
    assert session_config.read(session) is None


# ── config set --theme / --active-session ─────────────────────────────────────


def test_set_theme_writes_the_setting(_clean) -> None:
    # Given: the default theme
    # When: the theme is set to a registered name
    result = runner.invoke(config_mod.app, ["set", "--theme", "nord"])

    # Then: the setting carries it (the TUI's theme setter writes any name)
    assert result.exit_code == 0
    assert settings_store.load().theme == "nord"


def test_set_theme_accepts_an_unregistered_name(_clean) -> None:
    # Given: the boot theme default
    # When: the theme is set to a name no registered theme answers to
    result = runner.invoke(config_mod.app, ["set", "--theme", "custom-name"])

    # Then: the write succeeds — the TUI stores the name and boot falls back
    # (the same tolerance `_boot_theme` applies to a stale key)
    assert result.exit_code == 0
    assert settings_store.load().theme == "custom-name"


def test_set_active_session_writes_the_pointer(_clean) -> None:
    # Given: no session file by that name
    # When: the active-session pointer is published
    result = runner.invoke(config_mod.app, ["set", "--active-session", "tui-future"])

    # Then: the pointer is written — it need not name an existing session
    assert result.exit_code == 0
    assert settings_store.load().session == "tui-future"


def test_set_combines_target_theme_and_active_session(_clean) -> None:
    # Given: a configured provider
    _install("prov-a", default_model="m-a")
    session = _session()

    # When: one invocation writes the trio, the theme and the pointer
    result = runner.invoke(
        config_mod.app,
        [
            "set", "--session", session, "--target", "outo",
            "--provider", "prov-a", "--model", "m-a", "--reasoning", "high",
            "--theme", "dracula", "--active-session", "work",
        ],
    )

    # Then: all three landed, and the ✓ line names them
    assert result.exit_code == 0
    s = settings_store.load()
    assert (s.provider, s.model, s.reasoning) == ("prov-a", "m-a", "high")
    assert s.theme == "dracula"
    assert s.session == "work"
    assert session_config.read(session).provider == "prov-a"
    assert "theme" in result.output and "active-session" in result.output


# ── style set --session (the picker's pin write) ──────────────────────────────


def test_style_set_with_session_pins_the_style_to_that_session(_clean) -> None:
    # Given: the bundled styles are installed under the isolated home
    from miniouto.cli import style as style_mod
    from miniouto.storage import paths
    from miniouto.storage import styles as style_store

    paths.ensure_dirs()
    assert style_store.read("coding") is not None
    session = _session()

    # When: a style is set with --session
    result = runner.invoke(style_mod.app, ["set", "coding", "--session", session])

    # Then: the session's pin carries the style, and it is the default too
    assert result.exit_code == 0
    assert session_config.read(session).style == "coding"
    assert settings_store.load().style == "coding"


def test_style_set_without_session_leaves_the_pins_alone(_clean) -> None:
    # Given: the bundled styles and a session with no pin
    from miniouto.cli import style as style_mod
    from miniouto.storage import paths

    paths.ensure_dirs()
    session = _session()

    # When: a style is set with no --session
    result = runner.invoke(style_mod.app, ["set", "coding"])

    # Then: the default moved and the named session froze nothing from THIS
    # command (a session with no pin still follows the default)
    assert result.exit_code == 0
    assert settings_store.load().style == "coding"
    assert session_config.read(session) is None or session_config.read(session).style == ""


# ── provider edit ─────────────────────────────────────────────────────────────


def test_provider_edit_updates_the_record_partially(_clean) -> None:
    # Given: a custom provider with caps and a stored key
    from miniouto.cli import provider as provider_mod

    _install(
        "local",
        default_model="m-1",
        base_url="http://127.0.0.1:9/v1",
        api_key="k",
        max_context_window=8192,
        max_output_tokens=4096,
    )

    # When: only the model and the key are edited
    result = runner.invoke(
        provider_mod.app,
        ["edit", "local", "--default-model", "m-2", "--api-key", "k2"],
    )

    # Then: the named fields changed and the rest kept their stored values
    assert result.exit_code == 0
    record = provider_store.get("local")
    assert record.default_model == "m-2"
    assert record.api_key == "k2"
    assert record.base_url == "http://127.0.0.1:9/v1"
    assert record.max_context_window == 8192
    assert record.max_output_tokens == 4096


def test_provider_edit_clears_caps_with_zero(_clean) -> None:
    # Given: a custom provider with caps set
    from miniouto.cli import provider as provider_mod

    _install("local", max_context_window=8192, max_output_tokens=4096)

    # When: both caps are cleared with 0
    result = runner.invoke(
        provider_mod.app,
        ["edit", "local", "--max-context-window", "0", "--max-output-tokens", "0"],
    )

    # Then: the overrides are None again — "follow modelslist"
    assert result.exit_code == 0
    record = provider_store.get("local")
    assert record.max_context_window is None
    assert record.max_output_tokens is None


def test_provider_edit_clears_reasoning_with_an_empty_value(_clean) -> None:
    # Given: a provider with a stored reasoning preference
    from miniouto.cli import provider as provider_mod

    _install("local", default_model="m-1", reasoning_effort="high")

    # When: reasoning is set to ""
    result = runner.invoke(provider_mod.app, ["edit", "local", "--reasoning", ""])

    # Then: the preference is cleared to auto (None)
    assert result.exit_code == 0
    assert provider_store.get("local").reasoning_effort is None


def test_provider_edit_on_a_missing_name_fails(_clean) -> None:
    # Given: no provider by that name
    from miniouto.cli import provider as provider_mod

    # When: it is edited
    result = runner.invoke(provider_mod.app, ["edit", "nope", "--default-model", "m"])

    # Then: a ✗ on stderr with exit 1
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "not configured" in result.output


def test_provider_edit_compatibility_on_a_custom_record_fails(_clean) -> None:
    # Given: a hand-added provider (custom source)
    from miniouto.cli import provider as provider_mod

    _install("local", default_model="m-1")

    # When: --compatibility names a dialect
    result = runner.invoke(provider_mod.app, ["edit", "local", "--compatibility", "openai"])

    # Then: a ✗ on stderr with exit 1 — catalog-only flow
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "catalog" in result.output


def test_provider_edit_compatibility_rewrites_the_catalog_record(
    _clean, monkeypatch
) -> None:
    # Given: a catalog-source provider whose catalog entry offers a second
    # dialect with a different URL
    from miniouto.cli import provider as provider_mod

    _install(
        "kimi",
        default_model="m-1",
        base_url="https://api.moonshot.cn/v1",
        api_format="openai",
        source="modelslist",
        catalog_dialect="openai",
    )
    monkeypatch.setattr(
        provider_mod.catalog_api,
        "find_provider_by_slug",
        lambda slug, base_url="": {
            "name": "Kimi",
            "slug": "kimi",
            "formats": [
                {"format": "openai", "baseUrl": "https://api.moonshot.cn/v1"},
                {"format": "anthropic", "baseUrl": "https://api.moonshot.cn/anthropic"},
            ],
        },
    )

    # When: the compatibility dialect is switched
    result = runner.invoke(provider_mod.app, ["edit", "kimi", "--compatibility", "anthropic"])

    # Then: format, base URL and dialect all moved to the chosen mapping
    assert result.exit_code == 0
    record = provider_store.get("kimi")
    assert record.api_format == "anthropic"
    assert record.base_url == "https://api.moonshot.cn/anthropic"
    assert record.catalog_dialect == "anthropic"


def test_provider_edit_compatibility_with_an_unknown_dialect_fails(
    _clean, monkeypatch
) -> None:
    # Given: a catalog-source provider offering one dialect
    from miniouto.cli import provider as provider_mod

    _install("kimi", source="modelslist", catalog_dialect="openai")
    monkeypatch.setattr(
        provider_mod.catalog_api,
        "find_provider_by_slug",
        lambda slug, base_url="": {
            "name": "Kimi",
            "slug": "kimi",
            "formats": [{"format": "openai", "baseUrl": "https://api.moonshot.cn/v1"}],
        },
    )

    # When: a dialect the catalog does not offer is named
    result = runner.invoke(provider_mod.app, ["edit", "kimi", "--compatibility", "google"])

    # Then: a ✗ listing what IS available, exit 1
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "openai" in result.output


# ── provider custom add caps flags ────────────────────────────────────────────


def test_custom_add_accepts_caps_flags(_clean) -> None:
    # Given: an isolated store
    from miniouto.cli import provider as provider_mod

    # When: a custom provider is added with caps
    result = runner.invoke(
        provider_mod.app,
        [
            "custom", "add", "--name", "local",
            "--max-context-window", "131072", "--max-output-tokens", "32768",
        ],
    )

    # Then: the record carries both overrides
    assert result.exit_code == 0
    record = provider_store.get("local")
    assert record.max_context_window == 131072
    assert record.max_output_tokens == 32768


def test_custom_add_without_caps_keeps_the_old_shape(_clean) -> None:
    # Given: an isolated store
    from miniouto.cli import provider as provider_mod

    # When: a custom provider is added with no caps flags
    result = runner.invoke(provider_mod.app, ["custom", "add", "--name", "local"])

    # Then: no overrides — the pre-flag behavior is unchanged
    assert result.exit_code == 0
    record = provider_store.get("local")
    assert record.max_context_window is None
    assert record.max_output_tokens is None


# ── config seed: the mint's copy-if-absent pin (TUI defect 10) ────────────────


def test_seed_copies_the_whole_pin_including_style(_clean) -> None:
    # Given: a source session pinning the outo trio AND a style
    _install("prov-a", default_model="m-a")
    session_config.set_fields(
        "seed-src",
        provider="prov-a",
        model="m-a",
        reasoning="high",
        style="coding",
    )
    # And: global defaults that DIFFER (the invariant-12 hazard)
    _install("prov-b", default_model="m-b")
    settings_store.save(settings_store.Settings(provider="prov-b", model="m-b"))

    # When: a fresh session is seeded from it
    result = runner.invoke(
        config_mod.app, ["seed", "--source", "seed-src", "--target", "seed-new"]
    )

    # Then: the WHOLE pin copied — trios AND the style key
    assert result.exit_code == 0
    pinned = session_config.read("seed-new")
    assert pinned is not None
    assert pinned.provider == "prov-a"
    assert pinned.model == "m-a"
    assert pinned.reasoning == "high"
    assert pinned.style == "coding"


def test_seed_leaves_the_global_default_alone(_clean) -> None:
    # Given: a source pin and a global default that must not move
    _install("prov-a", default_model="m-a")
    _install("prov-b", default_model="m-b")
    session_config.set_fields("seed-src-2", provider="prov-a", model="m-a")
    before = settings_store.load()
    assert before.provider == ""

    # When: the seed runs
    result = runner.invoke(
        config_mod.app, ["seed", "--source", "seed-src-2", "--target", "seed-new-2"]
    )

    # Then: the global default is UNTOUCHED — a seed copies, it never
    # rewrites the installation's default (invariant 12; the old per-trio
    # `config set` seeding rewrote it on every mint)
    assert result.exit_code == 0
    assert settings_store.load() == before


def test_seed_is_copy_if_absent(_clean) -> None:
    # Given: a target that already carries its own pin
    _install("prov-a", default_model="m-a")
    _install("prov-c", default_model="m-c")
    session_config.set_fields("seed-src-3", provider="prov-a", model="m-a")
    session_config.set_fields("seed-taken", provider="prov-c", model="m-c")

    # When: the seed runs onto it
    result = runner.invoke(
        config_mod.app, ["seed", "--source", "seed-src-3", "--target", "seed-taken"]
    )

    # Then: the target kept its own pin
    assert result.exit_code == 0
    pinned = session_config.read("seed-taken")
    assert (pinned.provider, pinned.model) == ("prov-c", "m-c")


def test_seed_from_an_unpinned_source_seeds_nothing(_clean) -> None:
    # Given: a source with no pin at all
    # When: the seed runs
    result = runner.invoke(
        config_mod.app, ["seed", "--source", "seed-empty", "--target", "seed-fresh"]
    )

    # Then: nothing was written and the ✓ says so
    assert result.exit_code == 0
    assert "nothing to seed" in result.output
    assert session_config.read("seed-fresh") is None


def test_seed_rejects_a_self_seed(_clean) -> None:
    result = runner.invoke(config_mod.app, ["seed", "--source", "x", "--target", "x"])
    assert result.exit_code == 1
    assert "must differ" in result.output
