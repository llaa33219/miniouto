"""`_dump_message` never fires a pydantic serializer warning, at the source.

coreouto's anthropic and openai-response providers build multimodal tool
results via `Message.model_construct(content=<provider WIRE dicts>)`,
bypassing validation. `Message.content`'s schema is
`str | list[ContentBlock | ToolCall]`, so a raw wire dict matches neither
and pydantic's serializer emits `PydanticSerializationUnexpectedValue`
UserWarnings on every `model_dump` — and `_dump_message` runs on every
ON_ITERATION history snapshot AND finish_turn. The fix is structural: a
message holding raw dicts in `content`/`tool_calls` is dumped by hand and
never reaches `model_dump`. These tests pin BOTH halves of the contract:
zero pydantic UserWarnings for any live shape, and — for the shapes that
already dumped cleanly — byte-identical output (the flattened history is
the invariant; `docs/storage.md`'s media rule depends on it).

The providers are exercised through their real `format_tool_result` (the
exact producer of the wire shape), never by hand-rolling wire dicts.
"""

from __future__ import annotations

import warnings
from typing import Any

import coreouto as co
import pytest
from coreouto.providers.anthropic import AnthropicProvider
from coreouto.providers.openai_response import OpenAIResponseProvider
from pydantic import BaseModel

from miniouto.core.chat import _dump_message

CAPTION = "cap\nImage saved to: /x.png\nSaved to: /x.art"


def _wire_tool_result(provider: Any, blocks: list[Any]) -> co.Message:
    """Build the message exactly as the live loop does: via the provider."""

    call = co.ToolCall(id="t1", name="Computer", arguments={"action": "screenshot"})
    result = co.ToolResult(tool_call_id="t1", blocks=blocks)
    return provider.format_tool_result(call, result)


def _captured(m: Any) -> tuple[dict[str, Any], list[warnings.WarningMessage]]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        dumped = _dump_message(m)
    return dumped, caught


def _pydantic_user_warnings(caught: list[warnings.WarningMessage]) -> list[str]:
    return [
        str(w.message)
        for w in caught
        if issubclass(w.category, UserWarning) and "Pydantic serializer warnings" in str(w.message)
    ]


@pytest.mark.parametrize(
    ("provider_cls", "flattened"),
    [
        (AnthropicProvider, "cap\nImage saved to: /x.png\nSaved to: /x.art\n"
                            "[image omitted from restored history: image/png]"),
        (OpenAIResponseProvider, "cap\nImage saved to: /x.png\nSaved to: /x.art\n"
                                 "[image omitted from restored history: image/png]"),
    ],
    ids=["anthropic", "openai-response"],
)
def test_wire_tool_result_dumps_without_warning(
    provider_cls: type, flattened: str
) -> None:
    """(a+b) The live wire message: no warning, flattened content, pairing kept."""

    live = _wire_tool_result(
        provider_cls(api_key="x"),
        [
            co.TextBlock(text=CAPTION),
            co.ImageBlock(data=b"\x89PNG", mime_type="image/png"),
        ],
    )
    assert all(isinstance(b, dict) for b in live.content)  # the wire shape itself
    dumped, caught = _captured(live)
    assert _pydantic_user_warnings(caught) == []
    assert dumped == {
        "role": "tool",
        "content": flattened,
        "tool_calls": None,
        "tool_call_id": "t1",
        "name": "Computer",
    }


def test_anthropic_media_url_blocks_flatten_to_placeholders() -> None:
    """The full anthropic media family (url form: no media_type anywhere)."""

    live = _wire_tool_result(
        AnthropicProvider(api_key="x"),
        [
            co.TextBlock(text="cap"),
            co.ImageBlock(url="https://e.com/x.png"),
            co.VideoBlock(url="https://e.com/v.mp4"),
        ],
    )
    dumped, caught = _captured(live)
    assert _pydantic_user_warnings(caught) == []
    assert dumped["content"] == (
        "cap\n[image omitted from restored history: unknown]\n"
        "[video omitted from restored history: unknown]"
    )


def test_openai_wire_text_survives_flattening() -> None:
    """The point of flattening openai-response wire: the text must not vanish.

    The old behavior stored `[{"type": "input_text", ...}]` raw — a record
    `Message.model_validate` rejects on reload, so `_load_coreouto_history`
    degraded it to `_flatten_media_blocks`, which knew none of the
    `input_*` types and dropped EVERYTHING. Now the same degrader sees a
    plain string and passes it through.
    """

    live = _wire_tool_result(OpenAIResponseProvider(api_key="x"), [co.TextBlock(text="the answer is 42")])
    dumped, caught = _captured(live)
    assert _pydantic_user_warnings(caught) == []
    assert dumped["content"] == "the answer is 42"


def test_tool_calls_as_dicts_no_warning_and_args_normalized() -> None:
    """(c) Dict `tool_calls`: dumped as-is, non-dict arguments coerced to {}."""

    live = co.Message.model_construct(
        role="assistant",
        content="",
        tool_calls=[
            {"id": "t1", "name": "Computer", "arguments": {"action": "screenshot"}},
            {"id": "t2", "name": "Bash", "arguments": None},
        ],
    )
    dumped, caught = _captured(live)
    assert _pydantic_user_warnings(caught) == []
    assert dumped["tool_calls"] == [
        {"id": "t1", "name": "Computer", "arguments": {"action": "screenshot"}},
        {"id": "t2", "name": "Bash", "arguments": {}},
    ]
    co.Message.model_validate(dumped)  # the record round-trips


def test_tool_calls_model_entries_still_dump_via_model_dump() -> None:
    """Model entries inside a dict-triggered message keep the dump semantics."""

    live = co.Message.model_construct(
        role="assistant",
        content="",
        tool_calls=[
            co.ToolCall(id="t1", name="Bash", arguments={"command": "ls"}),
            {"id": "t2", "name": "Computer", "arguments": None},
        ],
    )
    dumped, caught = _captured(live)
    assert _pydantic_user_warnings(caught) == []
    assert dumped["tool_calls"] == [
        {"id": "t1", "name": "Bash", "arguments": {"command": "ls"}},
        {"id": "t2", "name": "Computer", "arguments": {}},
    ]


def test_validated_google_real_blocks_unchanged() -> None:
    """(d) Validated real-block messages keep the model_dump + flatten path.

    Google's providers put REAL blocks in `content` (never wire dicts), so
    they must keep dumping through `model_dump` — no hand-built record —
    with media flattened to a placeholder exactly as before.
    """

    live = co.Message(
        role="tool",
        content=[co.TextBlock(text="cap"), co.ImageBlock(url="https://e.com/x.png")],
        tool_call_id="t1",
        name="Computer",
    )
    dumped, caught = _captured(live)
    assert _pydantic_user_warnings(caught) == []
    assert dumped == live.model_dump(mode="json") | {
        "content": "cap\n[image omitted from restored history: unknown]"
    }


def test_plain_str_content_unchanged() -> None:
    """(e) The ordinary case: same dict `model_dump` itself produces."""

    live = co.Message(role="user", content="hello")
    dumped, caught = _captured(live)
    assert _pydantic_user_warnings(caught) == []
    assert dumped == live.model_dump(mode="json")


def test_raising_model_dump_fallback_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """(f) A dump that RAISES (raw bytes) still degrades via the except path.

    Monkeypatch `model_dump` to raise so the fallback is reached directly,
    without needing a payload that happens to fail JSON mode.
    """

    live = co.Message(role="tool", content="all text", tool_call_id="t1", name="Bash")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise ValueError("forced serialization failure")

    monkeypatch.setattr(type(live), "model_dump", _boom)
    dumped, caught = _captured(live)
    assert _pydantic_user_warnings(caught) == []
    assert dumped == {
        "role": "tool",
        "content": "all text",
        "tool_call_id": "t1",
        "name": "Bash",
    }


def test_wire_shape_never_reaches_model_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    """The structural guarantee itself: a wire message is never `model_dump`ed.

    If the guard ever regresses, the warning comes back on every snapshot;
    this makes the regression loud even if a future pydantic stops warning.
    """

    live = _wire_tool_result(
        AnthropicProvider(api_key="x"),
        [
            co.TextBlock(text="cap"),
            co.ImageBlock(data=b"\x89PNG", mime_type="image/png"),
        ],
    )

    def _boom(self: Any, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("model_dump called on a validation-bypassed message")

    monkeypatch.setattr(type(live), "model_dump", _boom)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        dumped = _dump_message(live)
    assert _pydantic_user_warnings(caught) == []
    assert "model_dump" not in dumped.get("content", "")


def test_wire_dump_survives_session_roundtrip() -> None:
    """The wire dump must load back through `Message.model_validate`.

    History is reloaded by `_load_coreouto_history` via `model_validate`;
    a flat string content round-trips (the record shape every stored
    session already uses).
    """

    live = _wire_tool_result(
        AnthropicProvider(api_key="x"),
        [
            co.TextBlock(text=CAPTION),
            co.ImageBlock(data=b"\x89PNG", mime_type="image/png"),
        ],
    )
    dumped, _ = _captured(live)
    restored = co.Message.model_validate(dumped)
    assert restored.role == "tool"
    assert restored.tool_call_id == "t1"
    assert restored.content == (
        "cap\nImage saved to: /x.png\nSaved to: /x.art\n"
        "[image omitted from restored history: image/png]"
    )


def test_unrelated_user_warning_still_surfaces() -> None:
    """The fix is structural, not a filter: other warnings still pass through."""

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.warn("unrelated", UserWarning, stacklevel=2)
        _dump_message(co.Message(role="user", content="hello"))
    assert any(
        issubclass(w.category, UserWarning) and "unrelated" in str(w.message) for w in caught
    )


def test_model_dump_warning_still_surfaces_for_unguarded_shapes() -> None:
    """Guard coverage is about live shapes, not a global silencer.

    A bare model holding a mismatched value (no `_dump_message` guard
    applies — it is not a Message with wire content) still warns: proof
    that the fix never touched global warning behavior.
    """

    class _Odd(BaseModel):
        x: str

    odd = _Odd.model_construct(x=123)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        odd.model_dump(mode="json")
    assert _pydantic_user_warnings(caught), "pydantic must still warn for mismatched values"
