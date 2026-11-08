"""Best-effort PyPI update lookup for the TUI.

This module deliberately does not own a timer or any UI state. The TUI calls
it once per launch, off the UI thread; an unavailable or malformed response
simply means there is no update notice to show.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import httpx
from packaging.version import InvalidVersion, Version

from .. import __version__

PYPI_URL = "https://pypi.org/pypi/miniouto/json"
HTTP_TIMEOUT_SECONDS = 3.0
UPGRADE_COMMAND = "uv tool upgrade miniouto"


def current_version() -> str:
    """Read the installed distribution version, with a source-tree fallback."""

    try:
        return version("miniouto")
    except PackageNotFoundError:
        return __version__


def latest_pypi_version() -> str | None:
    """Return PyPI's advertised version, or None when it cannot be checked."""

    try:
        response = httpx.get(PYPI_URL, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        latest = response.json()["info"]["version"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    return latest if isinstance(latest, str) else None


def update_notice(current: str, latest: str | None) -> str | None:
    """Build an upgrade warning only for a valid, strictly newer release."""

    if latest is None:
        return None
    try:
        if Version(latest) <= Version(current):
            return None
    except InvalidVersion:
        return None
    return (
        f"update available: miniouto v{current} → v{latest}. "
        f"Upgrade with `{UPGRADE_COMMAND}`."
    )
