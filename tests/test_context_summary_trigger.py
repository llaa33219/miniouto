"""The context monitor asks for a summary; nothing summarizes implicitly.

The decision this file locks: miniouto never compacts a conversation on its
own. When a turn has consumed `CONTEXT_SUMMARY_TRIGGER_PCT` of the window, the
monitor inserts ONE request into the session inbox (core/summary.py owns the
text), the existing BEFORE_LLM_CALL delivery hook hands it to the model as a
user message, and the model decides: write the summary documents, then call
`continue_session`.

Two properties matter equally, and they pull in opposite directions: the
request must arrive before the window runs out, and it must arrive exactly
once — a per-iteration message would train the model to ignore the alarm.
"""

from __future__ import annotations

import coreouto as co
import pytest

import harness
from miniouto.core import chat as chat_mod
from miniouto.core import context, inbox
from miniouto.core import runtime as rt

SESSION = "ctx-trigger"
WINDOW = 1_000


def _usage(prompt_tokens: int) -> co.Usage:
    return co.Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=1,
        total_tokens=prompt_tokens + 1,
    )


def _response(prompt_tokens: int | None) -> co.LLMResponse:
    return co.LLMResponse(content="ok", usage=None if prompt_tokens is None else _usage(prompt_tokens))


def _monitor(monkeypatch: pytest.MonkeyPatch, window: int | None = WINDOW):
    monkeypatch.setattr(context, "get_context_window", lambda *a, **k: window)
    return context.make_context_monitor(model="m", session=SESSION, provider_name="p")


def _drained() -> list[inbox.InboxMessage]:
    return inbox.drain(SESSION)


# ── the trigger ──────────────────────────────────────────────────────────────


def test_it_stays_silent_below_the_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a monitor whose window is 1000 tokens
    hook = _monitor(monkeypatch)
    assert hook is not None

    # When: an iteration reports 79% of the window
    hook(iteration=1, messages=[co.Message(role="user", content="hi")], response=_response(790))

    # Then: the model is told nothing — the request is a courtesy, not noise
    assert _drained() == []


def test_at_the_threshold_it_inserts_exactly_one_summary_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a monitor whose window is 1000 tokens
    hook = _monitor(monkeypatch)
    assert hook is not None

    # When: an iteration lands exactly on the threshold
    hook(iteration=1, messages=[co.Message(role="user", content="hi")], response=_response(800))

    # Then: one alarm-kind inbox message, carrying summary.py's header and
    # stamped so a receiver can tell it apart from a delegated run's alarm
    (message,) = _drained()
    assert message.kind == inbox.ALARM_KIND
    assert message.source == context.SUMMARY_SOURCE
    assert message.text.splitlines()[0] == inbox.ALARM_HEADER
    assert message.text.splitlines()[1].startswith("Context budget: 80%")


def test_the_reported_percentage_is_the_measured_one(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a monitor whose window is 1000 tokens
    hook = _monitor(monkeypatch)
    assert hook is not None

    # When: usage overshoots the threshold by a wide margin
    hook(iteration=1, messages=[], response=_response(850))

    # Then: the model is told 85%, not the threshold it happened to cross
    (message,) = _drained()
    assert message.text.splitlines()[0] == inbox.ALARM_HEADER
    assert message.text.splitlines()[1].startswith("Context budget: 85%")
    assert "1000" in message.text and "850" in message.text


def test_it_fires_once_per_turn_even_as_usage_keeps_growing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a monitor that already fired
    hook = _monitor(monkeypatch)
    assert hook is not None
    hook(iteration=1, messages=[], response=_response(800))

    # When: three more iterations report a full window
    for iteration in (2, 3, 4):
        hook(iteration=iteration, messages=[], response=_response(WINDOW))

    # Then: the turn asked exactly once — a repeated alarm is one the model
    # learns to skim
    assert len(_drained()) == 1


def test_a_fresh_turn_gets_its_own_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: the previous turn's monitor, which already fired
    first = _monitor(monkeypatch)
    assert first is not None
    first(iteration=1, messages=[], response=_response(800))

    # When: the next turn builds its own monitor (as build_runtime does)
    second = _monitor(monkeypatch)
    assert second is not None
    second(iteration=1, messages=[], response=_response(800))

    # Then: the new turn is warned too — the guard is per turn, not per session
    assert len(_drained()) == 2


def test_without_a_window_the_monitor_is_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: no lma answer and no provider override for the window
    monkeypatch.setattr(context, "get_context_window", lambda *a, **k: None)

    # When / Then: no hook exists, because there is no threshold to measure
    assert context.make_context_monitor(model="m", session=SESSION, provider_name="p") is None


def test_the_estimate_stands_in_when_the_provider_reports_no_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a provider that returns no usage at all, and a window of 1000
    hook = _monitor(monkeypatch)
    assert hook is not None
    messages = [co.Message(role="user", content="x" * 4_000)]

    # When: an iteration reports nothing
    hook(iteration=1, messages=messages, response=_response(None))

    # Then: the live transcript is measured instead (4000 chars / 4 = 1000)
    (message,) = _drained()
    assert message.text.splitlines()[0] == inbox.ALARM_HEADER
    assert message.text.splitlines()[1].startswith("Context budget: 100%")


# ── nothing summarizes implicitly any more ────────────────────────────────────

# The deleted compaction machinery, named indirectly so this guard survives a
# repo-wide grep for the stale symbols it is here to forbid.
_REMOVED = ("make_" + "summarize_hook", "SUMMARIZE_" + "THRESHOLD")


def test_the_implicit_summarizer_is_gone() -> None:
    # Given / When / Then: the compaction hook and its threshold no longer
    # exist — the module keeps the caps lookups and the trigger, nothing else
    assert not hasattr(context, _REMOVED[0])
    assert not hasattr(context, _REMOVED[1])
    assert context.DEFAULT_MAX_OUTPUT_TOKENS == 16_384


def test_no_hook_rewrites_the_live_message_list(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a runtime built for a session with plenty of headroom
    harness.install(monkeypatch, lambda _req: _response(10), session=SESSION)
    rt.build_runtime(
        rt.RuntimeConfig(
            provider_name=harness.PROVIDER_NAME, model="m", style_name=harness.STYLE_NAME,
            session=SESSION,
        )
    )
    messages = [
        co.Message(role="user", content="go"),
        co.Message(role="assistant", content="sure"),
        co.Message(role="user", content="go on"),
    ]
    before = list(messages)
    payload = {
        "iteration": 1,
        "messages": messages,
        "response": _response(10),
        "name": "Bash",
        "arguments": {},
        "result": co.ToolResult(tool_call_id="c1", content="ok"),
        "status_code": None,
        "error_message": "",
        "reaction": "retry",
    }

    # When: every hook that is handed the message list fires on it, repeatedly
    for _ in range(3):
        for event in (co.BEFORE_LLM_CALL, co.ON_ITERATION):
            for hook in co.get_hooks(event):
                hook(**payload)

    # Then: the transcript the model sees is byte-identical afterwards — the
    # only thing this harness writes into a message list is a delivered
    # request, and that arrives through the inbox like any other message
    assert messages == before


def test_continue_session_is_advertised_to_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a freshly built runtime
    harness.install(monkeypatch, lambda _req: _response(10), session=SESSION)
    rt.build_runtime(
        rt.RuntimeConfig(
            provider_name=harness.PROVIDER_NAME, model="m", style_name=harness.STYLE_NAME,
            session=SESSION,
        )
    )

    # Then: the preset carries the tool the summary request tells it to call
    assert "continue_session" in rt.BASE_TOOLS
    assert "continue_session" in co.get_agent_preset("outo").tools


# ── end to end: the request reaches the model as a user message ─────────────


def test_a_full_turn_delivers_the_request_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a real turn against a window small enough that the scripted
    # provider's own usage crosses the threshold on its first response
    provider_store_window = 1_000
    answers = iter(["done", "done", "done"])

    async def handler(req: harness.Request) -> co.LLMResponse:
        if req.actor == "outo" and req.index == 0:
            return co.LLMResponse(
                content="working",
                usage=_usage(provider_store_window),
                tool_calls=[co.ToolCall(id="c1", name="Bash", arguments={"command": "true"})],
            )
        return co.LLMResponse(content=next(answers), usage=_usage(10))

    provider = harness.install(monkeypatch, handler, session=SESSION)
    _shrink_window(provider_store_window)
    sink = harness.CaptureSink()

    # When: the turn runs
    answer = chat_mod.run_chat(
        chat_mod.ChatOptions(prompt="build the thing", session=SESSION), sink
    )

    # Then: the request is one of the user messages the model was actually
    # sent, and it was delivered exactly once
    assert answer == "done"
    asked = [t for r in provider.for_actor("outo") for t in r.user_texts()]
    requests = [t for t in asked if t.startswith(inbox.ALARM_HEADER)]
    assert len(requests) == 1
    assert requests[0].splitlines()[0] == inbox.ALARM_HEADER
    assert requests[0].splitlines()[1].startswith("Context budget: 100%")
    assert "continue_session" in requests[0]
    assert inbox.pending_count(SESSION) == 0


def _shrink_window(window: int) -> None:
    from miniouto.storage import providers as provider_store

    current = provider_store.get(harness.PROVIDER_NAME)
    assert current is not None
    provider_store.upsert(
        provider_store.Provider(**{**current.__dict__, "max_context_window": window})
    )
