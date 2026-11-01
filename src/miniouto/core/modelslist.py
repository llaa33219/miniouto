"""modelslist catalog client for https://modelslist.blp.sh.

Provider metadata comes from the catalog root. Model metadata is fetched only
from each provider's ``modelLists`` entry whose ``source`` is ``modelslist``;
official provider endpoints are advertised for other consumers, not used here.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

MODELSLIST_BASE_URL = "https://modelslist.blp.sh"
CACHE_TTL_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 15.0

_CACHE: dict[str, tuple[float, Any]] = {}
_MISSING = object()


def _cache_get(key: str) -> Any:
    entry = _CACHE.get(key)
    if entry is None:
        return _MISSING
    fetched_at, payload = entry
    if time.time() - fetched_at > CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return _MISSING
    return payload


def _cache_set(key: str, payload: Any) -> None:
    _CACHE[key] = (time.time(), payload)


def clear_cache() -> None:
    _CACHE.clear()


def _get_json(url: str) -> Any:
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def list_providers() -> list[dict[str, Any]]:
    """Return the curated provider index, preserving its advertised order."""

    cached = _cache_get("providers")
    if cached is not _MISSING:
        return cached
    data = _get_json(f"{MODELSLIST_BASE_URL}/")
    if not isinstance(data, list):
        raise ValueError("modelslist provider index must be a JSON array")
    providers = [entry for entry in data if isinstance(entry, dict)]
    _cache_set("providers", providers)
    return providers


def _modelslist_url(provider: dict[str, Any]) -> str | None:
    """Select only the catalog-owned list; never fall back to an official URL."""

    entries = provider.get("modelLists")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("source") != "modelslist":
            continue
        url = entry.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def _stored_base_url(name: str) -> str:
    """Read an old catalog record lazily, without a storage import cycle."""

    try:
        from ..storage import providers as provider_store

        record = provider_store.get(name)
        return record.base_url if record is not None else ""
    except Exception:
        return ""


def _endpoint_key(url: str) -> tuple[str, str]:
    """Normalize SDK-base/endpoint variants for legacy provider matching."""

    try:
        parsed = urlsplit(url.strip())
    except (AttributeError, ValueError):
        return ("", "")
    if not parsed.hostname:
        return ("", "")
    path = parsed.path.rstrip("/")
    for suffix in ("/responses", "/messages", "/chat/completions"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    # lma sometimes included the Anthropic SDK's automatically appended /v1.
    if path.endswith("/v1") and "/anthropic" in path:
        path = path[:-3]
    return (parsed.hostname.lower(), path)


def _matches_base_url(provider: dict[str, Any], base_url: str) -> bool:
    wanted = _endpoint_key(base_url)
    if not wanted[0]:
        return False
    formats = provider.get("formats")
    if not isinstance(formats, list):
        return False
    return any(
        isinstance(entry, dict)
        and isinstance(entry.get("baseUrl"), str)
        and _endpoint_key(entry["baseUrl"]) == wanted
        for entry in formats
    )


def _resolve_provider(name: str, base_url: str = "") -> dict[str, Any] | None:
    target = _normalize(name)
    if not target:
        return None
    providers = list_providers()
    # A slug is the stable public identifier; prefer it over fuzzy display-name
    # matches (Z.AI is the important slugify(name) != slug case).
    for provider in providers:
        if _normalize(str(provider.get("slug", ""))) == target:
            return provider
    for provider in providers:
        if _normalize(str(provider.get("name", ""))) == target:
            return provider
    # Existing lma-era records have names that do not necessarily occur in the
    # new curated catalog. Their saved endpoint is a safer identity than a
    # broader fuzzy match (for example, Kimi vs Kimi Code Plan).
    endpoint = base_url or _stored_base_url(name)
    if endpoint:
        for provider in providers:
            if _matches_base_url(provider, endpoint):
                return provider
    for provider in providers:
        candidate = _normalize(str(provider.get("name", "")))
        if target in candidate or candidate in target:
            return provider
    return None


def find_provider(name: str) -> dict[str, Any] | None:
    """Find a provider by slug or normalized display name; fail soft for UI."""

    try:
        return _resolve_provider(name)
    except Exception:
        return None


def find_provider_by_slug(slug: str, base_url: str = "") -> dict[str, Any] | None:
    """Resolve current slugs and endpoint-match legacy stored catalog records."""

    try:
        return _resolve_provider(slug, base_url)
    except Exception:
        return None


def list_models(provider_name: str) -> list[dict[str, Any]]:
    """Return models from the provider's modelslist-owned list, in source order."""

    key = f"models:{_normalize(provider_name)}"
    cached = _cache_get(key)
    if cached is not _MISSING:
        return cached
    provider = _resolve_provider(provider_name)
    if provider is None:
        _cache_set(key, [])
        return []
    url = _modelslist_url(provider)
    if url is None:
        _cache_set(key, [])
        return []
    try:
        data = _get_json(url)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        _cache_set(key, [])
        return []
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("modelslist model response must contain a data array")
    models = [entry for entry in entries if isinstance(entry, dict)]
    _cache_set(key, models)
    return models


def _model_info(model: dict[str, Any]) -> dict[str, Any]:
    """Adapt modelslist's model envelope to the cap/reasoning consumer shape."""

    info = dict(model)
    context_length = model.get("context_length")
    if isinstance(context_length, int) and not isinstance(context_length, bool):
        info["context_window"] = context_length
    top_provider = model.get("top_provider")
    if isinstance(top_provider, dict):
        max_output = top_provider.get("max_completion_tokens")
        if isinstance(max_output, int) and not isinstance(max_output, bool):
            info["max_output_tokens"] = max_output

    reasoning = model.get("reasoning")
    options = model.get("reasoning_options")
    if (not isinstance(options, list) or not options) and isinstance(reasoning, dict):
        efforts = reasoning.get("supported_efforts")
        if isinstance(efforts, list) and efforts:
            # OpenRouter advertises effort levels in `reasoning` while leaving
            # `reasoning_options` empty. Do not hide those controls in the UI.
            info["reasoning_options"] = [{"type": "effort", "values": efforts}]
    return info


def get_model(model_name: str, provider_name: str | None = None) -> dict[str, Any] | None:
    """Find a model by exact id (then case-insensitive id) in cached lists."""

    target = (model_name or "").strip()
    if not target:
        return None
    provider_keys = (
        [provider_name]
        if provider_name
        else [str(provider.get("slug", "")) for provider in list_providers()]
    )
    for provider_key in provider_keys:
        models = list_models(provider_key or "")
        match = next((model for model in models if model.get("id") == target), None)
        if match is None:
            match = next(
                (model for model in models if str(model.get("id", "")).lower() == target.lower()),
                None,
            )
        if match is not None:
            return _model_info(match)
    return None


_NORMALIZE_RE = re.compile(r"[\s\-_.]+")


def _normalize(value: str) -> str:
    return _NORMALIZE_RE.sub("", (value or "").lower())


def slugify(name: str) -> str:
    value = (name or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")
