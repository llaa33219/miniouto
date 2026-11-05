"""Provider construction helpers built on top of coreouto.

Dispatches by `api_format` to one of the four coreouto-builtin provider
classes: `openai`, `openai-response`, `anthropic`, `google`.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

import coreouto as co

from ..storage.providers import SOURCE_MODELSLIST, Provider
from ..tools import multimodal
from .error_rules import default_error_handling

SUPPORTED_FORMATS = ("openai", "openai-response", "anthropic", "google")

# Per-request stall timeout, in seconds — passed to the coreouto provider
# constructor's `timeout` param (coreouto >= 0.11), which forwards it to
# the SDK client (google's millisecond HttpOptions conversion is handled
# inside coreouto). Semantics are the SDKs' httpx-level timeout: the
# request dies only when no bytes arrive for this long — an actively
# streaming response is never cut. A timeout surfaces as an SDK exception
# matched by coreouto's TIMEOUT_ERRORS preset (see core/error_rules.py),
# and anything it can't catch is covered by the loop watchdog in
# core/chat.py.
API_STALL_TIMEOUT_SECONDS = 1800  # 30 minutes

# modelslist publishes exact wire-protocol base URLs. ``typesafe`` is not an
# LLM protocol supported by coreouto and is deliberately omitted.
DIALECT_TO_FORMAT: dict[str, str] = {
    "openai": "openai",
    "openai-responses": "openai-response",
    "anthropic": "anthropic",
    "google": "google",
}


@dataclass(frozen=True)
class CatalogDialect:
    """One supported provider endpoint advertised by modelslist."""

    dialect: str
    api_format: str
    base_url: str


def catalog_compatibility(catalog_provider: dict[str, Any]) -> list[CatalogDialect]:
    """Return hostable formats in the catalog's advertised priority order."""

    formats = catalog_provider.get("formats")
    if not isinstance(formats, list):
        return []
    options: list[CatalogDialect] = []
    seen: set[str] = set()
    for entry in formats:
        if not isinstance(entry, dict):
            continue
        dialect = entry.get("format")
        base_url = entry.get("baseUrl")
        api_format = DIALECT_TO_FORMAT.get(dialect) if isinstance(dialect, str) else None
        if (
            api_format is None
            or dialect in seen
            or not isinstance(base_url, str)
            or not base_url.strip()
        ):
            continue
        options.append(CatalogDialect(dialect, api_format, base_url.strip()))
        seen.add(dialect)
    return options


def resolve_catalog_dialect(
    catalog_provider: dict[str, Any],
) -> CatalogDialect | None:
    """Choose modelslist's first hostable format; preserve its exact URL."""

    options = catalog_compatibility(catalog_provider)
    return options[0] if options else None


def add_provider_from_catalog(
    catalog_provider: dict[str, Any],
    *,
    api_key: str,
    default_model: str = "",
    reasoning_effort: str | None = None,
) -> Provider:
    """Build a persisted provider solely from modelslist metadata."""

    selected = resolve_catalog_dialect(catalog_provider)
    if selected is None:
        name = str(catalog_provider.get("name") or catalog_provider.get("slug") or "?")
        raise ValueError(f"modelslist provider {name!r} has no supported API format")
    slug = catalog_provider.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        raise ValueError("modelslist provider has no usable slug")
    return Provider(
        name=slug.strip(),
        api_format=selected.api_format,
        base_url=selected.base_url,
        api_key=api_key,
        default_model=default_model,
        source=SOURCE_MODELSLIST,
        reasoning_effort=reasoning_effort,
        catalog_dialect=selected.dialect,
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
    # A provider that refuses a multimodal tool result (openai's chat
    # completions carries no image blocks; openai-response carries no
    # video/audio) raises from `format_tool_result` — which coreouto calls
    # OUTSIDE the tool-call wrapper, so the exception would end the whole
    # turn. The wrapper turns that refusal into a text tool result, so the
    # model is told instead of the run dying.
    multimodal.wrap_provider(instance)

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
            timeout=API_STALL_TIMEOUT_SECONDS,
            error_handling=error_handling,
        )

    if api_format == "openai-response":
        from coreouto.providers.openai_response import OpenAIResponseProvider

        url = base_url or None
        if url and url.rstrip("/").endswith("/responses"):
            url = url.rstrip("/")[: -len("/responses")]
        return OpenAIResponseProvider(
            api_key=api_key, base_url=url, stream=True,
            timeout=API_STALL_TIMEOUT_SECONDS,
            error_handling=error_handling,
        )

    if api_format == "anthropic":
        from coreouto.providers.anthropic import AnthropicProvider

        url = base_url or None
        if url and url.rstrip("/").endswith("/messages"):
            url = url.rstrip("/")[: -len("/messages")]
        if url and url.rstrip("/").endswith("/v1"):
            url = url.rstrip("/")[:-3]
        return AnthropicProvider(
            api_key=api_key, base_url=url, stream=True,
            timeout=API_STALL_TIMEOUT_SECONDS,
            error_handling=error_handling,
        )

    if api_format == "google":
        from coreouto.providers.google import GoogleProvider

        http_options: dict[str, Any] | None = None
        if base_url:
            # google-genai appends api_version to base_url itself. The legacy catalog's
            # gemini-generate SDK base already includes that version, so split
            # it back out instead of producing /v1beta/v1beta/... .
            from urllib.parse import urlsplit, urlunsplit

            parsed = urlsplit(base_url)
            path = parsed.path.rstrip("/")
            root, separator, version = path.rpartition("/")
            http_options = {"base_url": base_url}
            if separator and version.startswith("v") and version[1:].isalnum():
                http_options = {
                    "base_url": urlunsplit((parsed.scheme, parsed.netloc, root, "", "")),
                    "api_version": version,
                }
        return GoogleProvider(
            api_key=api_key, http_options=http_options, stream=True,
            timeout=API_STALL_TIMEOUT_SECONDS,
            error_handling=error_handling,
        )

    raise ValueError(f"Unhandled api_format: {api_format!r}")


def clear_coreouto_state() -> None:
    co.clear_providers()
    co.clear_agent_presets()
    co.clear_tools()
    co.clear_hooks()


def reset_subagent_registration() -> None:
    """Remove stale registry entries left over from a prior run."""

    with contextlib.suppress(Exception):
        co.clear_tools()


def provider_kwargs(provider_config: dict[str, Any], passthrough: dict[str, Any]) -> dict[str, Any]:
    """Bundle normalized + passthrough kwargs for AgentConfig."""

    return {
        "provider_config": dict(provider_config),
        "provider_passthrough": dict(passthrough),
    }
