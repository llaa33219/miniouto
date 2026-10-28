"""Provider construction helpers built on top of coreouto.

Dispatches by `api_format` to one of the four coreouto-builtin provider
classes: `openai`, `openai-response`, `anthropic`, `google`.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

import coreouto as co

from ..storage.providers import SOURCE_LMA, Provider
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

# `None` in the second tuple slot means "do not pin a base_url".
_SDK_TO_FORMAT: dict[str, tuple[str, str | None]] = {
    "openai": ("openai", None),
    "openai-responses": ("openai-response", None),
    "openai-compatible": ("openai", None),
    "anthropic": ("anthropic", None),
    "google-generative-ai": ("google", None),
    "google-vertex": ("google", None),
}

# lma's wire-protocol ids -> coreouto's provider formats. In particular,
# typesafe-systemone is intentionally absent: it is not an LLM wire protocol
# miniouto can host.
DIALECT_TO_FORMAT: dict[str, str] = {
    "openai-chat": "openai",
    "openai-responses": "openai-response",
    "anthropic-messages": "anthropic",
    "gemini-generate": "google",
}


@dataclass(frozen=True)
class CatalogDialect:
    """One usable endpoint advertised by lma's keyless dialect probe."""

    dialect: str
    api_format: str
    base_url: str
    confidence: str  # "supported" = confirmed; "likely" = tentative


def catalog_compatibility(catalog_provider: dict[str, Any]) -> list[CatalogDialect]:
    """Return hostable dialects, confirmed first, in deterministic order.

    A dialect occurs in at most one lma confidence group. Be defensive about
    that upstream contract anyway: supported wins if malformed data repeats
    an id in both groups, and unrelated/non-string entries never reach a
    provider constructor.
    """

    dialects = catalog_provider.get("dialects")
    if not isinstance(dialects, dict):
        return []
    options: list[CatalogDialect] = []
    seen: set[str] = set()
    for confidence in ("supported", "likely"):
        entries = dialects.get(confidence)
        if not isinstance(entries, dict):
            continue
        for dialect, api_format in DIALECT_TO_FORMAT.items():
            if dialect in seen:
                continue
            base_url = entries.get(dialect)
            if not isinstance(base_url, str) or not base_url.strip():
                continue
            options.append(
                CatalogDialect(dialect, api_format, base_url.strip(), confidence)
            )
            seen.add(dialect)
    return options


def resolve_catalog_dialect(
    sdk: str | None,
    api: str | None,
    dialects: dict[str, Any] | None,
) -> CatalogDialect | None:
    """Choose the default endpoint, retaining the SDK mapping as a preference.

    Confirmed endpoints always outrank tentative ones. Within either
    confidence group a protocol matching the provider's declared SDK wins;
    this also recovers entries whose native SDK miniouto cannot host directly.
    """

    options = catalog_compatibility({"dialects": dialects})
    if not options:
        return None
    native_format, _ = sdk_to_format(sdk, api)
    for confidence in ("supported", "likely"):
        group = [option for option in options if option.confidence == confidence]
        native = next((option for option in group if option.api_format == native_format), None)
        if native is not None:
            return native
        if group:
            return group[0]
    return None  # unreachable: options is non-empty and has only these groups


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
    reasoning_effort: str | None = None,
    dialects: dict[str, Any] | None = None,
) -> Provider:
    """Build a Provider from lma metadata. Raises ValueError on unsupported SDK."""

    selected = resolve_catalog_dialect(sdk, api, dialects)
    if selected is not None:
        fmt, base_url = selected.api_format, selected.base_url
    else:
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
        reasoning_effort=reasoning_effort,
        catalog_dialect=selected.dialect if selected is not None else "",
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
            timeout=API_STALL_TIMEOUT_SECONDS,
            error_handling=error_handling,
        )

    if api_format == "openai-response":
        from coreouto.providers.openai_response import OpenAIResponseProvider

        return OpenAIResponseProvider(
            api_key=api_key, base_url=base_url or None, stream=True,
            timeout=API_STALL_TIMEOUT_SECONDS,
            error_handling=error_handling,
        )

    if api_format == "anthropic":
        from coreouto.providers.anthropic import AnthropicProvider

        url = base_url or None
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
            # google-genai appends api_version to base_url itself. lma's
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
