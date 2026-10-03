"""Lock the model-caps cache contract in src/miniouto/core/context.py.

The bug this file guards: `_fetch_model_caps` swallowed every lma exception and
then wrote the resulting `{}` into `_MODEL_CACHE`, so one transport blip
permanently pinned the (model, provider) key to "no caps" for the life of the
process — compaction disabled and the 16K output floor applied forever. Only a
fetch that completed without raising may be cached; a successful-but-empty
result still is (that means "lma has no info for this key").
"""

from __future__ import annotations

import pytest

from miniouto.core import context, lma


def test_transport_failure_does_not_poison_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a first lma fetch that raises, then a healthy one
    calls: list[str] = []

    def fake_get_model(model: str, provider: str | None = None) -> dict[str, int]:
        calls.append(model)
        if len(calls) == 1:
            raise RuntimeError("connection reset")
        return {"context_window": 100_000, "max_output_tokens": 8_192}

    monkeypatch.setattr(lma, "get_model", fake_get_model)

    # When: two consecutive cap lookups
    first = context.get_max_output_tokens("m", "p")
    second = context.get_max_output_tokens("m", "p")

    # Then: the failure is not cached — the second call refetches and uses it
    assert len(calls) == 2
    assert first == context.DEFAULT_MAX_OUTPUT_TOKENS
    assert second == 8_192


def test_successful_empty_result_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: lma answers successfully with no info for this key
    calls: list[str] = []

    def fake_get_model(model: str, provider: str | None = None) -> dict[str, int]:
        calls.append(model)
        return {}

    monkeypatch.setattr(lma, "get_model", fake_get_model)

    # When: the same key is looked up twice
    context.get_max_output_tokens("m", "p")
    context.get_max_output_tokens("m", "p")

    # Then: one fetch — a genuine "lma has no info" answer is cacheable
    assert len(calls) == 1


def test_clear_model_cache_empties_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a populated cache
    monkeypatch.setattr(
        lma, "get_model", lambda model, provider=None: {"max_output_tokens": 4_096}
    )
    assert context.get_max_output_tokens("m", "p") == 4_096
    assert context._MODEL_CACHE

    # When: the mirror of lma.clear_cache() runs
    context.clear_model_cache()

    # Then: the dict is empty
    assert context._MODEL_CACHE == {}
