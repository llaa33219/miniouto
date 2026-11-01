"""modelslist transport, provider mapping, and catalog persistence contracts."""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from rich.console import Console

from miniouto.cli import provider as provider_cli
from miniouto.core import modelslist
from miniouto.core.providers import (
    _instantiate,
    add_provider_from_catalog,
    catalog_compatibility,
    resolve_catalog_dialect,
)
from miniouto.core.reasoning import default_reasoning_choice, reasoning_choices
from miniouto.storage.providers import SOURCE_LMA, SOURCE_MODELSLIST, Provider

CATALOG = [
    {
        "name": "Z.AI",
        "slug": "zai",
        "formats": [{"format": "openai", "baseUrl": "https://api.z.ai/api/paas/v4"}],
        "modelLists": [
            {"url": "https://official.example/models", "source": "official"},
            {"url": "https://modelslist.blp.sh/zai/models", "source": "modelslist"},
        ],
    },
    {
        "name": "Gateway",
        "slug": "gateway-exact",
        "formats": [
            {"format": "openai-responses", "baseUrl": "https://gateway.example/v1/responses"},
            {"format": "openai", "baseUrl": "https://gateway.example/v1"},
            {"format": "anthropic", "baseUrl": "https://gateway.example/anthropic/messages"},
            {"format": "typesafe", "baseUrl": "https://gateway.example/typesafe"},
        ],
        "modelLists": [
            {"url": "https://modelslist.blp.sh/gateway-exact/models", "source": "modelslist"}
        ],
    },
]
MODELS = [
    {
        "id": "model-a",
        "name": "Model A",
        "context_length": 200_000,
        "top_provider": {"max_completion_tokens": 32_000},
        "reasoning": {
            "mandatory": False,
            "default_enabled": True,
            "supported_efforts": ["low", "high"],
            "default_effort": "high",
        },
        "reasoning_options": [],
    },
    {
        "id": "model-b",
        "name": "Model B",
        "context_length": 100_000,
        "top_provider": {"max_completion_tokens": None},
        "reasoning": {"default_enabled": False, "supported_efforts": []},
        "reasoning_options": [{"type": "toggle"}],
    },
]


def install_catalog(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    requested: list[str] = []

    def get_json(url: str) -> Any:
        requested.append(url)
        if url == "https://modelslist.blp.sh/":
            return CATALOG
        if url == "https://modelslist.blp.sh/zai/models":
            return {"object": "list", "data": MODELS}
        raise AssertionError(f"Unexpected catalog request: {url}")

    monkeypatch.setattr(modelslist, "_get_json", get_json)
    return requested


def test_provider_index_is_a_bare_array_and_model_list_uses_only_modelslist_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = install_catalog(monkeypatch)

    assert modelslist.list_providers() == CATALOG
    assert modelslist.list_models("Z AI") == MODELS
    assert modelslist.list_models("zai") == MODELS

    assert requested == [
        "https://modelslist.blp.sh/",
        "https://modelslist.blp.sh/zai/models",
    ]
    assert "https://official.example/models" not in requested


def test_missing_modelslist_entry_does_not_fall_back_to_official_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        modelslist,
        "list_providers",
        lambda: [
            {
                "name": "Official Only",
                "slug": "official-only",
                "modelLists": [{"url": "https://official.example/models", "source": "official"}],
            }
        ],
    )
    monkeypatch.setattr(
        modelslist,
        "_get_json",
        lambda url: pytest.fail(f"Must not request {url}"),
    )

    assert modelslist.list_models("official-only") == []


def test_get_model_adapts_caps_and_recovers_openrouter_reasoning_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_catalog(monkeypatch)

    info = modelslist.get_model("model-a", "zai")

    assert info is not None
    assert info["context_window"] == 200_000
    assert info["max_output_tokens"] == 32_000
    assert info["reasoning_options"] == [{"type": "effort", "values": ["low", "high"]}]
    assert reasoning_choices("model-a", "zai") == ["off", "low", "high"]
    assert default_reasoning_choice("model-a", "zai") == "high"


def test_null_output_cap_and_disabled_reasoning_default_remain_meaningful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_catalog(monkeypatch)

    info = modelslist.get_model("model-b", "zai")

    assert info is not None
    assert info["context_window"] == 100_000
    assert "max_output_tokens" not in info
    assert default_reasoning_choice("model-b", "zai") is None


def test_get_model_is_provider_scoped_and_supports_unscoped_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(modelslist, "list_providers", lambda: CATALOG)
    monkeypatch.setattr(
        modelslist,
        "list_models",
        lambda provider: MODELS if provider == "zai" else [],
    )

    assert modelslist.get_model("model-a", "gateway-exact") is None
    assert modelslist.get_model("MODEL-A")["id"] == "model-a"


def test_provider_matching_prefers_slug_over_normalized_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = [
        {"name": "AI", "slug": "other"},
        {"name": "Other Name", "slug": "ai"},
    ]
    monkeypatch.setattr(modelslist, "list_providers", lambda: providers)

    assert modelslist.find_provider("ai") is providers[1]


def test_legacy_catalog_record_resolves_by_saved_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_catalog(monkeypatch)

    assert modelslist.find_provider_by_slug("z-ai") is CATALOG[0]
    assert (
        modelslist.find_provider_by_slug("old-gateway-name", "https://gateway.example/v1/responses")
        is CATALOG[1]
    )


def test_legacy_endpoint_match_precedes_ambiguous_fuzzy_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = [
        {
            "name": "Kimi",
            "slug": "kimi",
            "formats": [{"format": "openai", "baseUrl": "https://api.moonshot.ai/v1"}],
        },
        {
            "name": "Kimi Code Plan",
            "slug": "kimi-code-plan",
            "formats": [
                {"format": "openai", "baseUrl": "https://api.kimi.ai/coding/v1"}
            ],
        },
    ]
    monkeypatch.setattr(modelslist, "list_providers", lambda: providers)

    assert modelslist.find_provider_by_slug(
        "kimi-for-coding-kimi-ai", "https://api.kimi.ai/coding/v1"
    ) is providers[1]


def test_catalog_formats_preserve_order_and_exact_urls() -> None:
    options = catalog_compatibility(CATALOG[1])

    assert [(o.dialect, o.api_format, o.base_url) for o in options] == [
        ("openai-responses", "openai-response", "https://gateway.example/v1/responses"),
        ("openai", "openai", "https://gateway.example/v1"),
        ("anthropic", "anthropic", "https://gateway.example/anthropic/messages"),
    ]
    assert resolve_catalog_dialect(CATALOG[1]) == options[0]
    assert resolve_catalog_dialect({"formats": [{"format": "typesafe"}]}) is None


def test_add_from_catalog_persists_exact_slug_and_modelslist_source() -> None:
    provider = add_provider_from_catalog(CATALOG[1], api_key="secret")

    assert provider.name == "gateway-exact"
    assert provider.source == SOURCE_MODELSLIST
    assert provider.catalog_dialect == "openai-responses"
    assert provider.api_format == "openai-response"
    assert provider.base_url == "https://gateway.example/v1/responses"
    restored = Provider.from_dict(provider.name, provider.to_dict())
    assert restored.source == SOURCE_MODELSLIST
    assert restored.catalog_dialect == "openai-responses"


def test_legacy_source_remains_a_valid_catalog_record() -> None:
    restored = Provider.from_dict("old", {"source": SOURCE_LMA})
    assert restored.source == SOURCE_LMA


def test_cli_catalog_table_uses_modelslist_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    output = StringIO()
    monkeypatch.setattr(provider_cli, "console", Console(file=output, width=240))
    monkeypatch.setattr(provider_cli.catalog_api, "list_providers", lambda: CATALOG)

    provider_cli.providers_cmd()

    rendered = output.getvalue()
    assert "gateway-exact" in rendered
    assert "openai-responses" in rendered
    assert "https://gateway.example/v1/responses" in rendered
    assert "typesafe" not in rendered
    assert "SDK" not in rendered


def test_cli_add_uses_catalog_slug_and_first_modelslist_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[Provider] = []
    monkeypatch.setattr(provider_cli.catalog_api, "find_provider", lambda _name: CATALOG[1])
    monkeypatch.setattr(provider_cli.catalog_api, "list_models", lambda _name: MODELS)
    monkeypatch.setattr(provider_cli, "default_reasoning_choice", lambda *_args: "high")
    monkeypatch.setattr(provider_cli.provider_store, "get", lambda _name: None)
    monkeypatch.setattr(provider_cli.provider_store, "upsert", saved.append)
    monkeypatch.setattr(provider_cli, "console", Console(file=StringIO(), width=200))

    provider_cli.add_cmd("Gateway", api_key="secret", default_model="", reasoning=None)

    assert len(saved) == 1
    assert saved[0].name == "gateway-exact"
    assert saved[0].source == SOURCE_MODELSLIST
    assert saved[0].default_model == "model-a"
    assert saved[0].reasoning_effort == "high"


def test_responses_endpoint_is_not_duplicated_by_the_openai_sdk() -> None:
    provider = _instantiate("openai-response", "test-key", "https://gateway.example/v1/responses")
    client = provider._get_client()

    assert client.responses._client._prepare_url("/responses") == (
        "https://gateway.example/v1/responses"
    )


def test_anthropic_messages_endpoint_is_not_duplicated_by_the_sdk() -> None:
    provider = _instantiate("anthropic", "test-key", "https://gateway.example/v1/messages")

    assert provider._client.base_url == "https://gateway.example"


def test_anthropic_base_url_remains_verbatim() -> None:
    provider = _instantiate("anthropic", "test-key", "https://gateway.example/anthropic")

    assert str(provider._client.base_url) == "https://gateway.example/anthropic/"


def test_openai_dialect_base_url_is_used_verbatim_by_the_sdk() -> None:
    provider = _instantiate("openai", "test-key", "https://gateway.example/api/gateway")

    assert provider._client._prepare_url("/chat/completions") == (
        "https://gateway.example/api/gateway/chat/completions"
    )


def test_google_sdk_version_is_not_appended_twice() -> None:
    provider = _instantiate("google", "test-key", "https://gateway.example/google/v1beta")
    client = provider._client._api_client

    assert client._http_options.base_url == "https://gateway.example/google"
    assert client._http_options.api_version == "v1beta"
