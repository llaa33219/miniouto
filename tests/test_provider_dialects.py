"""Catalog dialect resolution, persistence, and CLI presentation."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from miniouto.cli import provider as provider_cli
from miniouto.core import lma as catalog_api
from miniouto.core.providers import (
    _instantiate,
    add_provider_from_lma,
    catalog_compatibility,
    resolve_catalog_dialect,
)
from miniouto.storage.providers import SOURCE_LMA, Provider

NATIVE_OPENAI = {
    "sdk": "openai-compatible",
    "api": "https://native.example/v1",
}


def test_supported_dialect_wins_and_supplies_its_exact_base_url() -> None:
    metadata = {
        **NATIVE_OPENAI,
        "dialects": {
            "supported": {"anthropic-messages": "https://gateway.example/anthropic"},
            "likely": {"openai-chat": "https://gateway.example/v1"},
        },
    }

    selected = resolve_catalog_dialect(**metadata)

    assert selected is not None
    assert selected.dialect == "anthropic-messages"
    assert selected.api_format == "anthropic"
    assert selected.base_url == "https://gateway.example/anthropic"
    assert selected.confidence == "supported"


def test_native_supported_mapping_is_preferred_over_another_confirmed_dialect() -> None:
    selected = resolve_catalog_dialect(
        sdk="openai-compatible",
        api="https://native.example/v1",
        dialects={
            "supported": {
                "openai-chat": "https://native.example/v1",
                "anthropic-messages": "https://native.example",
            },
            "likely": {},
        },
    )

    assert selected is not None
    assert selected.dialect == "openai-chat"


def test_only_likely_dialect_is_explicitly_marked_likely() -> None:
    selected = resolve_catalog_dialect(
        sdk="unknown-sdk",
        api=None,
        dialects={
            "supported": {},
            "likely": {"openai-responses": "https://gateway.example/v1"},
        },
    )

    assert selected is not None
    assert (selected.api_format, selected.base_url, selected.confidence) == (
        "openai-response",
        "https://gateway.example/v1",
        "likely",
    )


def test_missing_or_unknown_dialects_fall_back_to_legacy_sdk_mapping() -> None:
    assert resolve_catalog_dialect(**NATIVE_OPENAI, dialects=None) is None
    assert (
        resolve_catalog_dialect(
            **NATIVE_OPENAI,
            dialects={"supported": {"typesafe-systemone": "https://ignored.example"}},
        )
        is None
    )
    provider = add_provider_from_lma(
        name="legacy",
        api_key="secret",
        **NATIVE_OPENAI,
        dialects={"likely": {"typesafe-systemone": "https://ignored.example"}},
    )
    assert (provider.api_format, provider.base_url, provider.catalog_dialect) == (
        "openai",
        "https://native.example/v1",
        "",
    )


def test_add_from_catalog_persists_the_chosen_dialect() -> None:
    provider = add_provider_from_lma(
        name="gateway",
        api_key="secret",
        **NATIVE_OPENAI,
        dialects={"supported": {"openai-responses": "https://gateway.example/v1"}},
    )

    assert provider.source == SOURCE_LMA
    assert provider.catalog_dialect == "openai-responses"
    assert provider.api_format == "openai-response"
    assert provider.base_url == "https://gateway.example/v1"
    restored = Provider.from_dict("gateway", provider.to_dict())
    assert restored.catalog_dialect == "openai-responses"


def test_compatibility_options_include_both_confidence_groups_and_skip_unknown_data() -> None:
    options = catalog_compatibility(
        {
            "dialects": {
                "supported": {"openai-chat": "https://gateway.example/v1"},
                "likely": {
                    "anthropic-messages": "https://gateway.example",
                    "typesafe-systemone": "https://ignored.example",
                    "openai-chat": "https://duplicate.example",
                },
                "broken": {"google-generate": 42},
            }
        }
    )

    assert [(o.dialect, o.confidence, o.base_url) for o in options] == [
        ("openai-chat", "supported", "https://gateway.example/v1"),
        ("anthropic-messages", "likely", "https://gateway.example"),
    ]


def test_catalog_list_shows_confidence_and_effective_url(monkeypatch) -> None:
    output = StringIO()
    monkeypatch.setattr(provider_cli, "console", Console(file=output, width=240))
    monkeypatch.setattr(
        provider_cli.catalog_api,
        "list_providers",
        lambda: [
            {
                "name": "Gateway",
                **NATIVE_OPENAI,
                "dialects": {
                    "supported": {"anthropic-messages": "https://gateway.example/anthropic"},
                    "likely": {"openai-chat": "https://gateway.example/v1"},
                },
            }
        ],
    )

    provider_cli.providers_cmd()

    rendered = output.getvalue()
    assert "anthropic-messages (supported)" in rendered
    assert "openai-chat (likely)" in rendered
    assert "https://gateway.example/anthropic" in rendered
    assert "typesafe-systemone" not in rendered


def test_configured_list_shows_the_saved_dialect_and_url(monkeypatch) -> None:
    output = StringIO()
    monkeypatch.setattr(provider_cli, "console", Console(file=output, width=200))
    monkeypatch.setattr(
        provider_cli.provider_store,
        "load_all",
        lambda: {
            "gateway": Provider(
                name="gateway",
                source=SOURCE_LMA,
                api_format="anthropic",
                base_url="https://gateway.example/anthropic",
                catalog_dialect="anthropic-messages",
            )
        },
    )

    provider_cli.list_cmd()

    rendered = output.getvalue()
    assert "anthropic-messages" in rendered
    assert "https://gateway.example/anthropic" in rendered



def test_cli_add_uses_dialects_on_the_initial_provider_construction(
    monkeypatch,
) -> None:
    metadata = {
        "name": "Gateway",
        **NATIVE_OPENAI,
        "dialects": {
            "supported": {"anthropic-messages": "https://gateway.example/anthropic"},
            "likely": {"openai-chat": "https://gateway.example/v1"},
        },
    }
    saved: list[Provider] = []
    monkeypatch.setattr(provider_cli.catalog_api, "find_provider", lambda _name: metadata)
    monkeypatch.setattr(provider_cli.provider_store, "get", lambda _name: None)
    monkeypatch.setattr(provider_cli.provider_store, "upsert", saved.append)
    monkeypatch.setattr(provider_cli, "console", Console(file=StringIO(), width=200))

    provider_cli.add_cmd("Gateway", api_key="secret", default_model="model", reasoning=None)

    assert len(saved) == 1
    assert saved[0].catalog_dialect == "anthropic-messages"
    assert saved[0].api_format == "anthropic"
    assert saved[0].base_url == "https://gateway.example/anthropic"



def test_find_provider_resolves_a_stored_slug_with_parenthesized_alias(
    monkeypatch,
) -> None:
    metadata = {
        "name": "MiniMax Token Plan (minimax.io)",
        "dialects": {"supported": {"anthropic-messages": "https://api.minimax.io/anthropic"}},
    }
    monkeypatch.setattr(catalog_api, "list_providers", lambda: [metadata])

    assert catalog_api.find_provider_by_slug("minimax-token-plan-minimax-io") is metadata


def test_stored_slug_lookup_does_not_change_user_facing_fuzzy_matching(
    monkeypatch,
) -> None:
    providers = [{"name": "302.AI"}, {"name": "ai&"}]
    monkeypatch.setattr(catalog_api, "list_providers", lambda: providers)

    assert catalog_api.find_provider("ai") is providers[0]
    assert catalog_api.find_provider_by_slug("ai") is providers[1]


def test_openai_dialect_base_url_is_used_verbatim_by_the_sdk() -> None:
    provider = _instantiate("openai", "test-key", "https://gateway.example/api/gateway")

    assert provider._client._prepare_url("/chat/completions") == (
        "https://gateway.example/api/gateway/chat/completions"
    )


def test_google_dialect_version_is_not_appended_twice() -> None:
    provider = _instantiate(
        "google", "test-key", "https://gateway.example/google/v1beta"
    )
    client = provider._client._api_client

    assert client._http_options.base_url == "https://gateway.example/google"
    assert client._http_options.api_version == "v1beta"
    assert client._build_request("post", "models/m:streamGenerateContent", {}).url == (
        "https://gateway.example/google/v1beta/models/m:streamGenerateContent"
    )
