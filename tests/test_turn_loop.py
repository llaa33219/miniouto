"""The persistent turn loop (core.chat._run_turn_coro).

A turn's async httpx/AsyncOpenAI connection pools must not outlive a CLOSED
event loop: the old ``asyncio.run()`` per turn created and closed a fresh loop,
so the second turn tripped "Event loop is closed" in httpcore's connection pool
and the traceback painted over the TUI. ``_run_turn_coro`` runs every turn on
ONE long-lived loop, so clients stay valid across turns. These tests pin that.
"""

from __future__ import annotations

import asyncio
import threading

from miniouto.core import chat


def test_the_turn_loop_is_reused_across_turns() -> None:
    # Given/When: two turns run on the persistent loop
    async def grab_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    chat._run_turn_coro(grab_loop())
    first = chat._get_turn_loop()
    chat._run_turn_coro(grab_loop())
    second = chat._get_turn_loop()

    # Then: it is the SAME loop, and it is not closed between turns
    assert first is second
    assert not first.is_closed()


def test_the_turn_loop_is_not_the_callers_loop() -> None:
    # The turn loop runs on its own daemon thread, distinct from any loop the
    # caller (e.g. the TUI's asyncio loop) is running.
    async def grab_thread() -> int:
        return threading.get_ident()

    turn_thread = chat._run_turn_coro(grab_thread())
    assert turn_thread != threading.get_ident()


def test_run_turn_coro_returns_the_coroutine_result() -> None:
    async def compute() -> int:
        await asyncio.sleep(0)
        return 42

    assert chat._run_turn_coro(compute()) == 42


def test_run_turn_coro_propagates_exceptions() -> None:
    async def boom() -> None:
        raise ValueError("turn failed")

    try:
        chat._run_turn_coro(boom())
    except ValueError as exc:
        assert str(exc) == "turn failed"
    else:
        raise AssertionError("the exception did not propagate")


def test_an_async_client_survives_across_turns() -> None:
    # The actual bug: an async client created in turn 1 must still be usable in
    # turn 2 (its pool is bound to the loop). With a closed loop this raises
    # "Event loop is closed"; with the persistent loop it does not.
    import httpx

    holder: dict[str, httpx.AsyncClient] = {}

    async def turn_one() -> None:
        holder["client"] = httpx.AsyncClient()

    async def turn_two() -> None:
        # The client from turn 1 is still bound to a LIVE loop; touching its
        # pool must not raise "Event loop is closed".
        client = holder["client"]
        assert not client.is_closed
        await client.aclose()

    chat._run_turn_coro(turn_one())
    chat._run_turn_coro(turn_two())  # must not raise
