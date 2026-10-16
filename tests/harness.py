"""Shared harness: a REAL `core.chat.run_chat` turn against a scripted provider.

The unit milestones each stub one seam. The cross-milestone seams only
exist in the wiring — `build_runtime`'s hook registration, coreouto's own
loop, the alarm delivery hook, `_RecordingSink`'s persister bridge and the
session file — so the tests that cover them must run the real thing. This
harness does exactly that and stops at one boundary: the provider.

Nothing here touches the network:

- the provider is a `coreouto` provider double, so no SDK client is built;
- the lma model lookups are answered from the installed provider's stored
  caps (`max_context_window` / `max_output_tokens`), with `lma.get_model`
  stubbed to "no data" for the reasoning resolver;
- every timing is event-gated (a `threading.Event` the test owns) or a
  bounded poll — never a fixed sleep.

`install()` writes the real storage (provider TOML, style file, settings)
and swaps only `build_coreouto_provider`, so `resolve_runtime_from_settings`
and `build_runtime` run their production code paths.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import coreouto as co
import pytest

from miniouto.core import chat as chat_mod
from miniouto.core import inbox
from miniouto.core import lma as lma_module
from miniouto.core import runtime as rt
from miniouto.core.error_rules import default_error_handling
from miniouto.core.events import LoopEvent
from miniouto.storage import providers as provider_store
from miniouto.storage import sessions as session_store
from miniouto.storage import settings as settings_store
from miniouto.storage import styles as style_store

PROVIDER_NAME = "e2e-provider"
STYLE_NAME = "e2e-style"
MODEL_A = "model-a"
MODEL_B = "model-b"

# Tokens in the style file. The provider double tells the two agents apart
# by the system prompt coreouto prepends, which is the only per-agent
# marker that survives into `create()`.
OUTO_TOKEN = "OUTO-SYSTEM-TOKEN"
PERSONA_TOKEN = "PERSONA-SYSTEM-TOKEN"

STYLE = (
    f"<outo>{OUTO_TOKEN} you are outo, the top level agent</outo>\n"
    f"<editor>{PERSONA_TOKEN} you are the editor persona</editor>\n"
)


# ── the provider double ─────────────────────────────────────────────────────


@dataclass
class Request:
    """One `create()` call: who is asking, what they were sent."""

    actor: str
    model: str
    index: int
    messages: list[co.Message]
    on_stream_text: Callable[[str], Awaitable[None]] | None = None

    def user_texts(self) -> list[str]:
        return [m.content for m in self.messages if m.role == "user" and isinstance(m.content, str)]

    async def stream(self, text: str) -> None:
        """Emit one live delta, as a streaming provider would."""

        if self.on_stream_text is None:
            raise AssertionError("coreouto did not pass the stream callbacks in")
        await self.on_stream_text(text)


Handler = Callable[[Request], Awaitable[co.LLMResponse]]


class ScriptedProvider:
    """A `coreouto` provider whose every response the test decides.

    `handler` is awaited once per `create()` call and returns the
    `LLMResponse` for that call. Each call is recorded (a deep copy of the
    live message list) so a test can assert what the model was actually
    sent, and `error_handling` carries miniouto's real 400/422 rules so the
    history-repair path is reachable without an SDK.

    `_stream` is set because every real provider has it: coreouto then
    passes the ON_STREAM_* callbacks into `create()`.
    """

    _stream = True

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[Request] = []
        self.error_handling = default_error_handling("openai")

    # coreouto Provider protocol ────────────────────────────────────────────

    async def create(
        self,
        messages: list[co.Message],
        *,
        model: str,
        tools: list[Any] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> co.LLMResponse:
        actor = self._actor_of(messages)
        request = Request(
            actor=actor,
            model=model,
            index=sum(1 for r in self.requests if r.actor == actor),
            messages=[m.model_copy(deep=True) for m in messages],
            on_stream_text=kwargs.get("_on_stream_text"),
        )
        self.requests.append(request)
        return await self.handler(request)

    def format_assistant_message(self, response: co.LLMResponse) -> co.Message:
        if isinstance(response.raw, dict) and response.raw.get("poisoned"):
            # `arguments: null` — the one payload shape every provider 400s
            # on. Real `ToolCall` objects, validation skipped: everything
            # downstream (pairing, the cut, the dump) must still see a
            # well-formed object, so a plain dict here breaks `_repair_pairing`
            # and a validated ToolCall cannot hold None at all.
            return co.Message.model_construct(
                role="assistant",
                content=response.content or "",
                tool_calls=[
                    co.ToolCall.model_construct(id=tc.id, name=tc.name, arguments=None)
                    for tc in response.tool_calls
                ],
                tool_call_id=None,
                name=None,
            )
        return co.Message(
            role="assistant",
            content=response.content or "",
            tool_calls=list(response.tool_calls) or None,
        )

    def format_tool_result(self, tool_call: co.ToolCall, result: co.ToolResult) -> co.Message:
        return co.Message(
            role="tool",
            content=result.content or "",
            tool_call_id=tool_call.id,
            name=tool_call.name,
        )

    # helpers ────────────────────────────────────────────────────────────────

    def _actor_of(self, messages: list[co.Message]) -> str:
        for m in messages:
            if m.role == "system" and isinstance(m.content, str):
                return "subagent" if PERSONA_TOKEN in m.content else "outo"
        return "outo"

    def models(self, actor: str = "outo") -> list[str]:
        return [r.model for r in self.requests if r.actor == actor]

    def for_actor(self, actor: str) -> list[Request]:
        return [r for r in self.requests if r.actor == actor]


# ── installation ───────────────────────────────────────────────────────────


def _provider(model: str) -> provider_store.Provider:
    # Both cap overrides are set so `core.context` never asks lma for the
    # window or the output cap; the reasoning resolver gets `{}` from the
    # stubbed `lma.get_model`.
    return provider_store.Provider(
        name=PROVIDER_NAME,
        api_format="openai",
        base_url="http://127.0.0.1:9/v1",
        api_key="k",
        default_model=model,
        max_context_window=200_000,
        max_output_tokens=4_096,
    )


def install(
    monkeypatch: pytest.MonkeyPatch,
    handler: Handler,
    *,
    session: str,
    model: str = MODEL_A,
    style: str = STYLE,
    after_tool_call: Callable[..., Any] | None = None,
) -> ScriptedProvider:
    """Install a fake runtime and return the scripted provider driving it.

    `after_tool_call` registers an extra coreouto AFTER_TOOL_CALL hook on
    every turn's freshly built runtime — the seam a test needs to release a
    gated background job at exactly the right point in the loop.
    """

    provider = ScriptedProvider(handler)
    provider_store.upsert(_provider(model))
    style_store.write(STYLE_NAME, style, overwrite=True)
    settings_store.save(
        settings_store.Settings(provider=PROVIDER_NAME, style=STYLE_NAME, session=session)
    )
    monkeypatch.setattr(lma_module, "get_model", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        rt,
        "build_coreouto_provider",
        lambda p: co.register_provider(p.name, provider),
    )
    if after_tool_call is not None:
        real_build_runtime = chat_mod.build_runtime

        def build_and_hook(*args: Any, **kwargs: Any) -> co.Agent:
            agent = real_build_runtime(*args, **kwargs)
            co.register_hook(co.AFTER_TOOL_CALL, after_tool_call)
            return agent

        monkeypatch.setattr(chat_mod, "build_runtime", build_and_hook)
    return provider


def set_default_model(model: str) -> None:
    """Edit the provider's default model the way the TUI editor does."""

    provider_store.upsert(_provider(model))


# ── the session inbox ───────────────────────────────────────────────────────


def queued_alarms(session: str) -> int:
    """How many completion messages are waiting, WITHOUT draining them.

    A test that needs "the run has finished but nobody has read the message
    yet" cannot use `inbox.drain()`: that is the delivery, and it unlinks.
    `inbox.pending_count` is the non-destructive peek at the session's inbox
    directory — the same thing a caller would look at on disk.
    """

    return inbox.pending_count(session)


def wait_for_alarm(session: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if queued_alarms(session):
            return
        time.sleep(0.005)
    raise AssertionError("no completion message landed in the inbox")


async def wait_for_alarm_async(session: str, timeout: float = 10.0) -> None:
    """The awaitable twin of `wait_for_alarm`, for use inside a hook."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if queued_alarms(session):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("no completion message landed in the inbox")


# ── the caller's sink ───────────────────────────────────────────────────────


class CaptureSink:
    """A real `EventSink` that remembers everything it was handed.

    Like the console sink, `remove_events` is append-only: `events` is
    "what the user watched happen", `pruned` is "what the record later
    retracted". That difference is the point of the prune milestone, so
    this sink must not collapse it.
    """

    def __init__(self) -> None:
        self.events: list[LoopEvent] = []
        self.deltas: list[tuple[str, str]] = []
        self.answers: list[tuple[str, str]] = []
        self.pruned: list[list[str]] = []

    def begin_working(self) -> None:
        pass

    def update_activity(self, text: str) -> None:
        pass

    def end_working(self) -> None:
        pass

    def emit_loop_event(self, event: LoopEvent) -> None:
        self.events.append(event)

    def emit_final_answer(self, content: str, session_name: str) -> None:
        self.answers.append((content, session_name))

    def emit_stream_delta(self, kind: str, text: str) -> None:
        self.deltas.append((kind, text))

    def remove_events(self, msg_refs: list[str]) -> None:
        self.pruned.append(list(msg_refs))

    # helpers ────────────────────────────────────────────────────────────────

    def of_kind(self, kind: str) -> list[LoopEvent]:
        return [e for e in self.events if e.kind == kind]


# ── the session file ────────────────────────────────────────────────────────


def session_blob(session: str) -> dict[str, Any]:
    return json.loads(session_store.path_for(session).read_text(encoding="utf-8"))


def turn_events(session: str, index: int = -1) -> list[dict[str, Any]]:
    return session_blob(session)["turns"][index]["events"]


def history(session: str) -> list[dict[str, Any]]:
    return session_blob(session)["history"]


def user_history(session: str) -> list[str]:
    return [
        m["content"]
        for m in history(session)
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
