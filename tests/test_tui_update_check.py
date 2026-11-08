"""PyPI update checks: offline-safe lookup, PEP 440 comparison, and TUI display."""

from __future__ import annotations

import asyncio
from importlib.metadata import PackageNotFoundError
from typing import Any

import httpx
import pytest

from miniouto.cli.tui import ChatTUI
from miniouto.core import update_check

_CHECK_FOR_UPDATES = ChatTUI._check_for_updates


@pytest.mark.parametrize(
    ("current", "latest"),
    [
        ("0.11.6", "0.11.6"),
        ("0.12.0", "0.11.6"),
        ("0.11.6", None),
        ("0.11.6", "not-a-version"),
        ("0.12.0", "0.12.0rc1"),
    ],
)
def test_no_update_warning_unless_pypi_is_strictly_newer(
    current: str, latest: str | None
) -> None:
    assert update_check.update_notice(current, latest) is None


def test_update_warning_contains_versions_and_upgrade_command() -> None:
    notice = update_check.update_notice("0.11.5", "0.11.6")

    assert notice is not None
    assert "v0.11.5" in notice
    assert "v0.11.6" in notice
    assert update_check.UPGRADE_COMMAND in notice


def test_a_final_release_is_newer_than_its_release_candidate() -> None:
    assert update_check.update_notice("0.12.0rc1", "0.12.0") is not None


def test_pypi_lookup_uses_the_json_api_and_short_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        calls.append((url, timeout))
        return httpx.Response(
            200, request=httpx.Request("GET", url), json={"info": {"version": "0.11.6"}}
        )

    monkeypatch.setattr(update_check.httpx, "get", fake_get)

    assert update_check.latest_pypi_version() == "0.11.6"
    assert calls == [(update_check.PYPI_URL, 3.0)]


@pytest.mark.parametrize("status_code", [403, 500])
def test_pypi_http_errors_are_silent(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        return httpx.Response(status_code, request=httpx.Request("GET", url))

    monkeypatch.setattr(update_check.httpx, "get", fake_get)

    assert update_check.latest_pypi_version() is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="missing-info"),
        pytest.param({"info": {}}, id="missing-version"),
        pytest.param({"info": {"version": 6}}, id="non-string-version"),
        pytest.param(None, id="non-object-json"),
    ],
)
def test_malformed_pypi_responses_are_silent(
    monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    monkeypatch.setattr(
        update_check.httpx,
        "get",
        lambda url, *, timeout: httpx.Response(
            200, request=httpx.Request("GET", url), json=payload
        ),
    )

    assert update_check.latest_pypi_version() is None


def test_current_version_prefers_installed_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_check, "version", lambda name: "0.11.6")
    assert update_check.current_version() == "0.11.6"

    def not_installed(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(update_check, "version", not_installed)
    assert update_check.current_version() == update_check.__version__


def _run(body: Any) -> None:
    asyncio.run(body())


def _rows(app: ChatTUI) -> list[str]:
    return [row.render().plain for row in app._window.rows]


def test_tui_warns_after_an_older_installed_version_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ChatTUI, "_check_for_updates", _CHECK_FOR_UPDATES)
    monkeypatch.setattr(update_check, "current_version", lambda: "0.11.5")
    monkeypatch.setattr(update_check, "latest_pypi_version", lambda: "0.11.6")

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            warning = "update available: miniouto v0.11.5 → v0.11.6"
            assert sum(warning in row for row in _rows(app)) == 1
            assert any(update_check.UPGRADE_COMMAND in row for row in _rows(app))

            # A theme refresh rebuilds the logo, not the update check. The
            # existing warning stays visible without a duplicate or a refetch.
            app._show_logo()
            await pilot.pause()
            assert sum("update available:" in row for row in _rows(app)) == 1

    _run(body)


def test_tui_stays_silent_when_the_installed_version_is_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ChatTUI, "_check_for_updates", _CHECK_FOR_UPDATES)
    monkeypatch.setattr(update_check, "current_version", lambda: "0.11.6")
    monkeypatch.setattr(update_check, "latest_pypi_version", lambda: "0.11.6")

    async def body() -> None:
        app = ChatTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert not any("update available:" in row for row in _rows(app))
            assert app._update_notice is None
            assert len(app.workers) == 0

    _run(body)
