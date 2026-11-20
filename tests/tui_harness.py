"""Helpers shared by the app-level TUI tests (and by the M9 test port).

The app layer is exercised through its REAL surfaces — ``run_test`` boots a
headless ``ChatTUI``, ``pilot`` presses keys and clicks, and assertions read the
composited screen or the node tree. That means the same three things are needed
over and over: a booted app with a pinned session of the test's own, a way to
read what the terminal would actually show, and a ``run_chat`` double.

Two rules for everything here:

* **Read the DISPLAY, not the model.** :func:`painted_line` goes through
  ``screen._compositor.render_strips()`` so it sees exactly what the terminal
  gets, including the crop at the screen edge — the layout bugs worth catching
  are the ones visible on screen.
* **Pin the session.** ``ChatTUI`` mints a ``tui-…`` session on its first turn
  and persists turn records, so a test that did not pin its own name would
  share provider config and inbox with every other test in the run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from miniouto.cli.tui import ChatTUI
from miniouto.cli.tui import app as app_module
from miniouto.cli.tui.modals import Modal
from miniouto.storage import session_config
from miniouto.storage import sessions as session_store
from miniouto.storage import settings as settings_store

BOOT_SIZE = (120, 32)
"""The pty-baseline screen size — the layout the owner approved."""


# ── booting ─────────────────────────────────────────────────────────────────


def pin_session(app: ChatTUI, name: str) -> str:
    """Stand ``app`` on a session of this test's own and publish it.

    Mirrors the production pin: routing reads ``_session_name``, and
    ``settings.session`` is the "active session" every surface (and a later
    ``miniouto status``) reads as the seed a mint copies from.
    """

    session_store.create(name)
    app._session_name = name
    app._session_assigned = True
    app._chat_started = True
    settings_store.update(session=name)
    session_config.clear(name)
    return name


# ── reading the screen ──────────────────────────────────────────────────────


def painted_line(app: ChatTUI, y: int) -> str:
    """The composited screen line ``y`` — what the terminal actually receives."""

    return app.screen._compositor.render_strips()[y].text


def painted(app: ChatTUI) -> list[str]:
    """Every composited screen line, right-stripped, top to bottom."""

    return [strip.text.rstrip() for strip in app.screen._compositor.render_strips()]


def composed(app: ChatTUI, width: int, height: int) -> list[str]:
    """Compose a frame and flatten it to plain text lines (no compositor needed)."""

    app.compose(width, height)
    return ["".join(piece[0] for piece in line).rstrip() for line in app.compose(width, height)]


def row_text(row: Any) -> str:
    """A row's painted text, as its own render cache spells it."""

    return "\n".join(strip.text.rstrip() for strip in row._render_cache.lines)


def plain_of(node: Any) -> str:
    """``node.render().plain`` without importing rich at every call site."""

    return str(node.render().plain)


# ── the turn driver double ──────────────────────────────────────────────────


class _FakeChatProcess:
    """A chat child that speaks the CLI's stdout contract, in-process.

    The TUI runs its turn as ``python -m miniouto chat <prompt> --name S -c``
    now, so the double replaces the SPAWN: ``argv`` is inspected exactly as the
    real wrapper inspects it, and the "child" then performs what the real CLI
    would — begin/finish the turn on the session file, print the session
    marker, raw answer deltas and the finish marker — through the same
    parsing path the app uses. What a test asserts on (the prompt the TUI
    handed over, the session it routed on, ``-c``) is provably what the real
    argv would have carried.
    """

    def __init__(
        self,
        argv: list[str],
        *,
        turns: list[dict[str, Any]] | None = None,
        answer: str = "",
        stderr_text: str = "",
    ) -> None:
        self.argv = argv
        self._turns = turns
        self._answer = answer
        self.returncode: int | None = None
        self.stdout = _FakeStreamReader(self._render())
        self.stderr = _FakeStreamReader(stderr_text.encode("utf-8"))

    def _parse(self) -> tuple[str, str, bool]:
        """``(prompt, session, continue_flag)`` out of the chat argv."""

        prompt = ""
        session = ""
        continue_session = False
        positional: list[str] = []
        index = 0
        only_positional = False
        args = self.argv[self.argv.index("chat") + 1 :]
        while index < len(args):
            arg = args[index]
            if not only_positional:
                if arg == "--":
                    only_positional = True
                    index += 1
                    continue
                if arg == "--name":
                    session = args[index + 1]
                    index += 2
                    continue
                if arg in ("-c", "--continue"):
                    continue_session = True
                    index += 1
                    continue
                if arg.startswith("-"):
                    index += 1
                    continue
            positional.append(arg)
            index += 1
        prompt = " ".join(positional)
        return prompt, session, continue_session

    def _render(self) -> bytes:
        prompt, session, _continue = self._parse()
        # The alarm wakeup has no prompt argument: the drain is the CLI's, and
        # the DRAINED TEXT is what the turn's `user` becomes — exactly as the
        # real `chat --alarms` builds its prompt.
        if "--alarms" in self.argv:
            from miniouto.core import inbox as inbox_api

            messages = inbox_api.drain(session)
            prompt = "\n\n".join(m.text for m in messages)
        if self._turns is not None:
            self._turns.append(
                {
                    "prompt": prompt,
                    "session": session,
                    "continue": _continue,
                    # The real child is `python -m miniouto chat`: diagnostics
                    # never go to ITS stderr, which is what the flag asserted.
                    "quiet_diagnostics": True,
                }
            )
        session_store.begin_turn(session, prompt)
        session_store.finish_turn(session, assistant=self._answer)
        self.returncode = 0
        out = f"------{session}------\n"
        if self._answer:
            out += self._answer
            if not self._answer.endswith("\n"):
                out += "\n"
        out += "------finish------\n"
        return out.encode("utf-8")

    def send_signal(self, _sig: int) -> None:  # pragma: no cover - stop path
        self.returncode = 130

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


class _FakeStreamReader:
    """The slice of asyncio's StreamReader the reader task touches.

    ``read`` hands out the data ONCE and EOF after — the semantics the
    incremental reader loop relies on (`while chunk := await read(4096)`).
    """

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, _n: int = -1) -> bytes:
        data, self._data = self._data, b""
        return data


async def _fake_create_subprocess_exec(*argv: str, **_kwargs: Any) -> _FakeChatProcess:
    return _FakeChatProcess(list(argv), turns=_CURRENT_TURNS[0], answer=_CURRENT_ANSWER[0])


_CURRENT_TURNS: list[list[dict[str, Any]] | None] = [None]
_CURRENT_ANSWER: list[str] = [""]


def stub_run_chat(
    monkeypatch: Any,
    turns: list[dict[str, Any]],
    *,
    answer: str = "",
    on_session: Any = None,
    on_prompt: Any = None,
) -> None:
    """Replace the chat SUBPROCESS with a recorder honouring the CLI contract.

    The turn is ``[sys.executable, -m, miniouto, chat, <prompt>, --name, S,
    -c]``: the double intercepts the spawn, records the argv's meaning (the
    prompt the TUI handed over, the session it routed on, ``-c``) and writes
    the session record the real CLI writes, so a test can assert on routing
    as well as on the prompt. stdout follows the measured contract (session
    marker, raw answer, ``------finish------``).
    """

    _CURRENT_TURNS[0] = _RecordingTurns(turns, on_session, on_prompt)
    _CURRENT_ANSWER[0] = answer
    # The spawn seam lives on the bridge module — one patch target for every
    # chat child the TUI starts.
    monkeypatch.setattr(
        app_module.bridge, "asyncio_create_subprocess_exec", _fake_create_subprocess_exec
    )


class _RecordingTurns(list):  # type: ignore[type-arg]
    """The ``turns`` list, mirroring each record through an optional callback.

    ``on_session`` (the navigation suites' flattened form) fires per turn.
    """

    def __init__(
        self,
        inner: list[dict[str, Any]],
        on_session: Any,
        on_prompt: Any = None,
    ) -> None:
        super().__init__()
        self._inner = inner
        self._on_session = on_session
        self._on_prompt = on_prompt

    def append(self, item: dict[str, Any]) -> None:
        self._inner.append(item)
        if self._on_session is not None:
            self._on_session(item["session"])
        if self._on_prompt is not None:
            self._on_prompt(item)

    def __len__(self) -> int:
        return len(self._inner)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self._inner[index]


def stub_chat_output(
    monkeypatch: Any,
    *,
    answer: str = "",
    turns: list[dict[str, Any]] | None = None,
) -> None:
    """Alias of :func:`stub_run_chat` for tests that only feed output."""

    stub_run_chat(monkeypatch, turns if turns is not None else [], answer=answer)


async def settle(pilot: Any, *, want: int, turns: list[Any], rounds: int = 300) -> None:
    """Pump the app until ``want`` turns have been dispatched."""

    for _ in range(rounds):
        if len(turns) >= want:
            return
        await pilot.pause(0.01)
    raise AssertionError(f"expected {want} dispatch(es), got {turns}")


async def settle_until(pilot: Any, predicate: Callable[[], bool], rounds: int = 300) -> None:
    """Pump the app until ``predicate`` holds (or fail loudly)."""

    for _ in range(rounds):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("condition never became true")


def clear_modals(app: ChatTUI) -> None:
    """Pop every pushed screen, leaving the base screen on top."""

    for _ in range(len(app.screens)):
        if not isinstance(app.screen, Modal):
            return
        app.pop_screen(None)


def chip_rows(app: ChatTUI) -> dict[str, Any]:
    """The panel's chip nodes, by kind."""

    return dict(app._panel.chips)


def chip_values(app: ChatTUI) -> dict[str, str]:
    """The four chips' painted labels (``label: value``), by kind."""

    return {kind: chip.render().plain for kind, chip in app._panel.chips.items()}


__all__ = [
    "BOOT_SIZE",
    "chip_rows",
    "chip_values",
    "clear_modals",
    "composed",
    "painted",
    "painted_line",
    "pin_session",
    "plain_of",
    "row_text",
    "settle",
    "settle_until",
    "stub_chat_output",
    "stub_run_chat",
]
