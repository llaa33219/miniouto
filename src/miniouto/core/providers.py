"""Provider construction helpers built on top of coreouto.

Dispatches by `api_format` to one of the four coreouto-builtin provider
classes: `openai`, `openai-response`, `anthropic`, `google`.
"""

from __future__ import annotations

import contextlib
from typing import Any

import coreouto as co

from ..storage.providers import Provider

SUPPORTED_FORMATS = ("openai", "openai-response", "anthropic", "google")


def build_coreouto_provider(provider: Provider) -> None:
    """Register a miniouto Provider as a coreouto provider under its name."""

    fmt = provider.api_format
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported api_format: {fmt!r}. Supported: {', '.join(SUPPORTED_FORMATS)}."
        )

    api_key = provider.api_key or None
    instance = _instantiate(fmt, api_key, provider.base_url)

    co.register_provider(provider.name, instance)


def _instantiate(api_format: str, api_key: str | None, base_url: str) -> Any:
    if api_format == "openai":
        from coreouto.providers.openai import OpenAIProvider

        return OpenAIProvider(api_key=api_key, base_url=base_url or None)

    if api_format == "openai-response":
        from coreouto.providers.openai_response import OpenAIResponseProvider

        return OpenAIResponseProvider(api_key=api_key, base_url=base_url or None)

    if api_format == "anthropic":
        from coreouto.providers.anthropic import AnthropicProvider

        return AnthropicProvider(api_key=api_key, base_url=base_url or None)

    if api_format == "google":
        from coreouto.providers.google import GoogleProvider

        http_options: dict[str, Any] | None = None
        if base_url:
            http_options = {"base_url": base_url}
        return GoogleProvider(api_key=api_key, http_options=http_options)

    raise ValueError(f"Unhandled api_format: {api_format!r}")


def clear_coreouto_state() -> None:
    co.clear_providers()
    co.clear_agent_presets()
    co.clear_tools()
    co.clear_hooks()


def reset_subagent_registration() -> None:
    """Remove any stale `call_subagent` tool left over from a prior run."""

    with contextlib.suppress(Exception):
        co.clear_tools()


def provider_kwargs(provider_config: dict[str, Any], passthrough: dict[str, Any]) -> dict[str, Any]:
    """Bundle normalized + passthrough kwargs for AgentConfig."""

    return {
        "provider_config": dict(provider_config),
        "provider_passthrough": dict(passthrough),
    }
