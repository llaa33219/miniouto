"""supervised_run leaves no pending task on the (persistent) turn loop.

The supervisor runs a `trip.wait()` waiter alongside the attempt. On a loop that
lives for the whole process (the TUI's persistent turn loop), a waiter that is
cancelled but NOT awaited stays pending and later surfaces as "Task was destroyed
but it is pending!". The finally block must cancel AND await every owned task.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading

from miniouto.core import chat, runtime


def _pending_except_current() -> list[asyncio.Task]:
    current = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if not t.done() and t is not current]


def test_supervised_run_leaves_no_pending_task() -> None:
    async def scenario() -> int:
        async def start(prompt, history):
            await asyncio.sleep(0.01)
            return "answer"

        result = await runtime.supervised_run(
            start, "hi", None,
            live_key="outo",
            on_wakeup=lambda *a: None,
            cancel_event=threading.Event(),
            restart_on_repetition="outo",
        )
        assert result == "answer"
        # After supervised_run returns, no task it owned may still be pending.
        return len(_pending_except_current())

    leftover = chat._run_turn_coro(scenario())
    assert leftover == 0, f"{leftover} task(s) left pending by supervised_run"


def test_supervised_run_cancel_path_leaves_no_pending_task() -> None:
    async def scenario() -> str:
        # Pre-set the cancel event: the supervisor's first poll cancels the
        # attempt and raises LoopCancelledError; the finally must still clean up.
        cancel = threading.Event()
        cancel.set()

        async def start(prompt, history):
            await asyncio.sleep(3)
            return "should not reach"

        with contextlib.suppress(runtime.LoopCancelledError):
            await runtime.supervised_run(
                start, "hi", None,
                live_key="outo",
                on_wakeup=lambda *a: None,
                cancel_event=cancel,
                restart_on_repetition="outo",
            )
        await asyncio.sleep(0)
        return "ok" if not _pending_except_current() else "pending"

    assert chat._run_turn_coro(scenario()) == "ok"
