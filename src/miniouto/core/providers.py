"""Provider construction helpers built on top of coreouto.

Dispatches by `api_format` to one of the four coreouto-builtin provider
classes: `openai`, `openai-response`, `anthropic`, `google`.
"""

from __future__ import annotations

import contextlib
from typing import Any

import coreouto as co

from ..storage.providers import SOURCE_LMA, Provider
from .error_rules import default_error_handling

SUPPORTED_FORMATS = ("openai", "openai-response", "anthropic", "google")

# `None` in the second tuple slot means "do not pin a base_url".
_SDK_TO_FORMAT: dict[str, tuple[str, str | None]] = {
    "openai": ("openai", None),
    "openai-responses": ("openai-response", None),
    "openai-compatible": ("openai", None),
    "anthropic": ("anthropic", None),
    "google-generative-ai": ("google", None),
    "google-vertex": ("google", None),
}


def sdk_to_format(sdk: str | None, api: str | None) -> tuple[str | None, str | None]:
    """Returns `(None, None)` when the SDK has no supported host."""

    if not sdk:
        return (None, None)
    if sdk in _SDK_TO_FORMAT:
        fmt, _ = _SDK_TO_FORMAT[sdk]
        return (fmt, api or None)
    if api:
        return ("openai", api)
    return (None, None)


def add_provider_from_lma(
    *,
    name: str,
    api_key: str,
    sdk: str | None,
    api: str | None,
    default_model: str = "",
) -> Provider:
    """Build a Provider from lma metadata. Raises ValueError on unsupported SDK."""

    fmt, base_url = sdk_to_format(sdk, api)
    if fmt is None:
        raise ValueError(
            f"Cannot map lma provider {name!r} (sdk={sdk!r}) to a supported "
            f"api_format. Supported SDKs: {', '.join(sorted(_SDK_TO_FORMAT))}, "
            "or any SDK with a non-empty api URL (OpenAI-compatible fallback)."
        )
    return Provider(
        name=name,
        api_format=fmt,
        base_url=base_url or "",
        api_key=api_key,
        default_model=default_model,
        source=SOURCE_LMA,
    )


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
    # `stream=True` is transport-only: coreouto reassembles the SSE
    # fragments into the same `LLMResponse`, so no caller changes. It is
    # required for Anthropic (SDK rejects non-streaming requests whose
    # max_tokens estimate exceeds ~10 minutes, which our 16K output floor
    # would trip) and harmless for OpenAI/Google.
    #
    # `error_handling` (coreouto >= 0.10) attaches the per-format
    # ErrorRule list so rate limits retry with backoff, auth failures
    # terminate with a clear message, and provider-side tool-call
    # rejections feed back as tool results instead of crashing the turn.
    error_handling = default_error_handling(api_format)
    if api_format == "openai":
        from coreouto.providers.openai import OpenAIProvider

        return OpenAIProvider(
            api_key=api_key, base_url=base_url or None, stream=True,
            error_handling=error_handling,
        )

    if api_format == "openai-response":
        from coreouto.providers.openai_response import OpenAIResponseProvider

        return OpenAIResponseProvider(
            api_key=api_key, base_url=base_url or None, stream=True,
            error_handling=error_handling,
        )

    if api_format == "anthropic":
        from coreouto.providers.anthropic import AnthropicProvider

        url = base_url or None
        if url and url.rstrip("/").endswith("/v1"):
            url = url.rstrip("/")[:-3]
        return AnthropicProvider(
            api_key=api_key, base_url=url, stream=True, error_handling=error_handling
        )

    if api_format == "google":
        from coreouto.providers.google import GoogleProvider

        http_options: dict[str, Any] | None = None
        if base_url:
            http_options = {"base_url": base_url}
        return GoogleProvider(
            api_key=api_key, http_options=http_options, stream=True,
            error_handling=error_handling,
        )

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
