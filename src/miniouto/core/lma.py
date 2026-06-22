"""lma (llm-model-api) REST client for https://lma.blp.sh."""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

LMA_BASE_URL = "https://lma.blp.sh"
CACHE_TTL_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 15.0

# A cached None is meaningful: it means "lma returned 404 for this key"
# and we should not re-hit the endpoint on every turn.
_CACHE: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    fetched_at, payload = entry
    if time.time() - fetched_at > CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return payload


def _cache_set(key: str, payload: Any) -> None:
    _CACHE[key] = (time.time(), payload)


def clear_cache() -> None:
    _CACHE.clear()


def list_providers() -> list[dict[str, Any]]:
    payload = _cache_get("providers")
    if payload is not None:
        return payload
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
        r = client.get(f"{LMA_BASE_URL}/provider")
        r.raise_for_status()
        data = r.json()
    providers = list(data.get("providers") or [])
    _cache_set("providers", providers)
    return providers


def list_models(provider_name: str) -> list[dict[str, Any]]:
    """Returns [] on 404 (provider not in lma)."""
    key = f"models:{(provider_name or '').lower()}"
    payload = _cache_get(key)
    if payload is not None:
        return payload
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
        r = client.get(
            f"{LMA_BASE_URL}/model-list",
            params={"provider-name": provider_name},
        )
        if r.status_code == 404:
            _cache_set(key, [])
            return []
        r.raise_for_status()
        data = r.json()
    models = list(data.get("models") or [])
    _cache_set(key, models)
    return models


def get_model(model_name: str, provider_name: str | None = None) -> dict[str, Any] | None:
    """Return the first matching model's info dict, or None.

    Scoped by `provider_name` when given. Raises `httpx.HTTPError` on
    transport failure.
    """
    key = f"model:{((provider_name or '').lower())}:{(model_name or '').lower()}"
    payload = _cache_get(key)
    if payload is not None:
        return payload
    params: dict[str, str] = {"model-name": model_name}
    if provider_name:
        params["provider-name"] = provider_name
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
        r = client.get(f"{LMA_BASE_URL}/model", params=params)
        if r.status_code == 404:
            _cache_set(key, None)
            return None
        r.raise_for_status()
        data = r.json()
    models = data.get("models") or []
    result = models[0] if models else None
    _cache_set(key, result)
    return result


def find_provider(name: str) -> dict[str, Any] | None:
    """Return the lma provider dict matching `name`, or None.

    lma's `/provider` does not expose search, so we do a normalized
    substring match locally. Fails soft (returns None) if lma is
    unreachable.
    """
    try:
        providers = list_providers()
    except Exception:
        return None
    target = _normalize(name)
    if not target:
        return None
    for p in providers:
        if _normalize(p.get("name", "")) == target:
            return p
    for p in providers:
        pn = _normalize(p.get("name", ""))
        if target in pn or pn in target:
            return p
    return None


_NORMALIZE_RE = re.compile(r"[\s\-_.]+")


def _normalize(s: str) -> str:
    return _NORMALIZE_RE.sub("", (s or "").lower())


def slugify(name: str) -> str:
    """`"OpenAI"` → `"openai"`, `"GitHub Copilot"` → `"github-copilot"`.

    Lowercases and collapses runs of any non-alphanumeric character to
    a single `-`, matching the form users have historically typed in
    `provider add --name`.
    """
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")
