"""Test-suite isolation.

Two jobs, both of which must happen *before* any test runs:

1. Point ``$MINIOUTO_HOME`` at a throwaway directory. ``storage.paths`` binds
   ``ROOT`` from that variable at import time, so a test that writes a session
   or a subagent result would otherwise land in the user's real ``~/.miniouto``.
   The assignment is module-level and comes first -- ``conftest.py`` is imported
   before any test module, so nothing under ``miniouto`` can have resolved
   ``ROOT`` yet.
2. Reset the module-level caches between tests, so one test's lma/model lookups
   or coreouto provider registrations never leak into the next. The repetition
   watchdog's counters and stream buffers are module state too, so they reset
   here as well.

``miniouto`` symbols are imported inside the fixture body on purpose: a
top-level import here would bind ``paths.ROOT`` at import time, which is
already true, and hides the ordering requirement. The lazy import keeps it
explicit and makes the env var above provably in place first.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

# Must stay the first executable statement in this module.
TEST_HOME: Path = Path(tempfile.mkdtemp(prefix="miniouto-test-home-"))
os.environ["MINIOUTO_HOME"] = str(TEST_HOME)


def _clear_model_cache(context: Any) -> None:
    """Drop ``context._MODEL_CACHE``, however the module exposes clearing it.

    ``clear_model_cache()`` is the intended API; the dict fallback keeps this
    working on revisions that still expose only the raw dict.
    """

    clear = getattr(context, "clear_model_cache", None)
    if callable(clear):
        clear()
        return
    cache: dict[Any, Any] = getattr(context, "_MODEL_CACHE", {})
    cache.clear()


@pytest.fixture(autouse=True)
def _isolate_module_state() -> None:
    """Reset every module-level cache before each test."""

    from miniouto.core import context, lma, providers, repetition

    _clear_model_cache(context)
    lma.clear_cache()
    providers.clear_coreouto_state()
    # The watchdog's counters AND its per-actor stream buffers: a test that
    # fed deltas must not leave a half-counted phrase behind for the next.
    repetition.reset()
