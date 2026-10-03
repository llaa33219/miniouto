"""Lock the session `cwd` stamping + directory-grouping contract.

A session belongs to the directory miniouto was launched from: `begin_turn`
stamps `paths_runtime.INVOCATION_CWD` exactly once (the first turn wins) and
later turns never overwrite it. `list_sessions_with_meta` reads the session
files directly (never `load()`, which force-writes the bundled styles via
`ensure_dirs()`) so the TUI picker can group by directory without writing.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from textual.app import App
from textual.widgets import Label, ListItem, ListView

from miniouto.cli.tui import SelectionModal
from miniouto.paths_runtime import INVOCATION_CWD
from miniouto.storage import sessions as session_store
from miniouto.storage.paths import SESSION_DIR


def _write(name: str, payload: dict[str, object]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    (SESSION_DIR / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_begin_turn_stamps_invocation_cwd_and_later_turns_do_not_overwrite() -> None:
    # Given: a brand-new session name
    name = "stamp-cwd"

    # When: a first turn begins
    session_store.begin_turn(name, "hello")

    # Then: the session records the invocation cwd
    assert session_store.load(name).cwd == str(INVOCATION_CWD)

    # When: a second turn begins (simulating a later launch from elsewhere by
    # forcing a different INVOCATION_CWD value on the module)
    import miniouto.paths_runtime as paths_runtime

    original = paths_runtime.INVOCATION_CWD
    paths_runtime.INVOCATION_CWD = original.parent / "somewhere-else"
    try:
        session_store.begin_turn(name, "again")
    finally:
        paths_runtime.INVOCATION_CWD = original

    # Then: the original (creating) cwd is preserved
    assert session_store.load(name).cwd == str(original)


def test_update_turn_events_round_trips_cwd() -> None:
    # Given: a session with one begun turn
    name = "roundtrip-cwd"
    session_store.begin_turn(name, "hello")

    # When: the running turn's events are rewritten (load -> mutate -> save)
    session_store.update_turn_events(name, [{"kind": "iteration", "iteration": 1}])

    # Then: cwd survives the load/mutate/save round trip
    assert session_store.load(name).cwd == str(INVOCATION_CWD)

    # And when the history snapshot is written too
    session_store.update_history(name, [{"role": "user", "content": "hello"}])
    assert session_store.load(name).cwd == str(INVOCATION_CWD)

    # And when the turn is finished
    session_store.finish_turn(name, assistant="done", status="done")
    assert session_store.load(name).cwd == str(INVOCATION_CWD)


def test_list_sessions_with_meta_groups_this_other_and_legacy_sessions() -> None:
    # Given: a session created here, a legacy session without cwd, and a
    # foreign-cwd session, with distinct mtimes
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    here = SESSION_DIR / "group-here.json"
    legacy = SESSION_DIR / "group-legacy.json"
    foreign = SESSION_DIR / "group-foreign.json"
    here.write_text(
        json.dumps({"version": 2, "session": "group-here", "cwd": str(INVOCATION_CWD)}),
        encoding="utf-8",
    )
    legacy.write_text(json.dumps({"version": 2, "session": "group-legacy"}), encoding="utf-8")
    foreign.write_text(
        json.dumps({"version": 2, "session": "group-foreign", "cwd": "/tmp/elsewhere"}),
        encoding="utf-8",
    )
    import os

    os.utime(here, (1000, 1000))
    os.utime(foreign, (2000, 2000))
    os.utime(legacy, (1500, 1500))

    # When: the lister runs
    metas = dict((name, cwd) for name, cwd, _mtime in session_store.list_sessions_with_meta())

    # Then: this-dir, legacy (empty) and foreign cwds are all reported
    assert metas["group-here"] == str(INVOCATION_CWD)
    assert metas["group-legacy"] == ""
    assert metas["group-foreign"] == "/tmp/elsewhere"

    # And the ordering is most-recently-written first
    names = [name for name, _cwd, _mtime in session_store.list_sessions_with_meta()]
    assert names.index("group-foreign") < names.index("group-legacy") < names.index("group-here")


def test_list_sessions_with_meta_tolerates_unparseable_files() -> None:
    # Given: a corrupt session file
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    (SESSION_DIR / "corrupt-abc.json").write_text("{not json", encoding="utf-8")

    # When/Then: the lister reports it with an empty cwd instead of raising
    metas = dict((name, cwd) for name, cwd, _mtime in session_store.list_sessions_with_meta())
    assert metas.get("corrupt-abc") == ""


def test_list_sessions_with_meta_never_calls_ensure_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a lister that would be poisoned by any storage bootstrap
    def _boom() -> None:
        raise AssertionError("list_sessions_with_meta must not touch ensure_dirs/load")

    monkeypatch.setattr(session_store, "ensure_dirs", _boom)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    (SESSION_DIR / "nowrite-xyz.json").write_text(
        json.dumps({"version": 2, "session": "nowrite-xyz", "cwd": "/tmp/x"}), encoding="utf-8"
    )

    # When: the lister runs
    metas = session_store.list_sessions_with_meta()

    # Then: it still returns metadata read straight from the files
    assert ("nowrite-xyz", "/tmp/x") in [(name, cwd) for name, cwd, _m in metas]


def test_load_tolerates_v1_migration_and_v2_file_without_cwd() -> None:
    # Given: a v1 flat-messages file and a v2 file with no "cwd" key
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    (SESSION_DIR / "legacy-v1.json").write_text(
        json.dumps(
            {
                "session": "legacy-v1",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "yo"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (SESSION_DIR / "novc-v2.json").write_text(
        json.dumps({"version": 2, "session": "novc-v2", "history": [], "turns": []}),
        encoding="utf-8",
    )

    # When/Then: both load without error, with cwd defaulting to ""
    assert session_store.load("legacy-v1").cwd == ""
    assert [t.assistant for t in session_store.load("legacy-v1").turns] == ["yo"]
    assert session_store.load("novc-v2").cwd == ""


def test_save_omits_cwd_when_empty_and_includes_it_when_set() -> None:
    # Given: a session with no cwd yet
    name = "sparse-cwd"
    session_store.create(name)

    # Then: the payload stays sparse (no "cwd" key)
    raw = json.loads((SESSION_DIR / f"{name}.json").read_text(encoding="utf-8"))
    assert "cwd" not in raw

    # When: a turn is begun (stamping cwd)
    session_store.begin_turn(name, "hi")

    # Then: "cwd" is serialized
    raw = json.loads((SESSION_DIR / f"{name}.json").read_text(encoding="utf-8"))
    assert raw["cwd"] == str(INVOCATION_CWD)


class _ModalApp(App[None]):
    def __init__(self, modal: SelectionModal) -> None:
        super().__init__()
        self._modal = modal

    def on_mount(self) -> None:
        self.push_screen(self._modal)


def _rows(modal: SelectionModal) -> list[tuple[str, str | None, bool]]:
    """(label, mapped value, is-header) for each row, top to bottom."""

    async def _run() -> list[tuple[str, str | None, bool]]:
        async with _ModalApp(modal).run_test() as pilot:
            out: list[tuple[str, str | None, bool]] = []
            for item in pilot.app.screen.query_one("#selection-list", ListView).query(ListItem):
                out.append(
                    (
                        str(item.query_one(Label).content),
                        modal._row_values.get(item),
                        "-header" in item.classes,
                    )
                )
            return out

    return asyncio.run(_run())


def test_selection_modal_renders_section_headers_and_preselects_correctly() -> None:
    # Given: a modal with two groups, the current value in the second group,
    # and a trailing extra option
    modal = SelectionModal(
        "Select session",
        [],
        current="sess-b",
        allow_none=False,
        extra_options=[("__new__", "+ new session…")],
        sections=[
            ("This directory", [("sess-a", "sess-a")]),
            ("Other directories", [("sess-b", "[proj] sess-b")]),
        ],
    )

    # When: the modal is mounted
    rows = _rows(modal)

    # Then: headers are display-only rows (no mapped value), options are not
    assert rows == [
        ("This directory", None, True),
        ("  sess-a", "sess-a", False),
        ("Other directories", None, True),
        ("● [proj] sess-b", "sess-b", False),
        ("  + new session…", "__new__", False),
    ]

    # And the pre-select row index is the real option row, not the position
    # of that option in the options list
    assert modal._row_index_by_value == {"sess-a": 1, "sess-b": 3, "__new__": 4}


def test_selection_modal_flat_rendering_is_unchanged_without_sections() -> None:
    # Given: a modal with no sections (style/provider/model pickers)
    modal = SelectionModal(
        "Select style",
        ["a", "b"],
        current="b",
        allow_none=False,
        extra_options=[("__new__", "+ new")],
    )

    # When: it is mounted
    rows = _rows(modal)

    # Then: a flat list, no headers, marker on the current option
    assert rows == [
        ("  a", "a", False),
        ("● b", "b", False),
        ("  + new", "__new__", False),
    ]
    assert modal._row_index_by_value == {"a": 0, "b": 1, "__new__": 2}
