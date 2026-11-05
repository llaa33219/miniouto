"""Keep a multimodal tool failure from ending the turn.

A media tool (Image / Video / Audio) or a Computer screenshot hands the
model a payload the provider may not be able to carry. coreouto turns
that refusal into an EXCEPTION raised from the provider's
``format_tool_result`` — which happens OUTSIDE the tool-call wrapper, so
the exception propagates out of the agent loop and the whole turn dies
with the model never learning why.

This module wraps the provider's ``format_tool_result`` so the refusal
becomes an ordinary TEXT tool result instead: the turn continues, the
model is told what went wrong, and it can pick a next step (switch
provider, downsample, describe from text).

NO PRE-FLIGHT PREDICTION. Whether a payload is acceptable is the
PROVIDER's call, decided at the wire boundary — not something to guess
from a lookup table before the call. So nothing here inspects the
provider in advance: the real provider is asked, and only its actual
refusal is intercepted. A provider that accepts the payload passes
through untouched.

The one thing that IS known in advance is the failure's SHAPE: coreouto
signals it with ``ValueError`` (or ``TypeError``) raised from
``format_tool_result`` — a malformed payload, never a transport or
auth problem. Only those are converted; any other exception is left
alone, because hiding it would swallow a real bug.
"""

from __future__ import annotations

from typing import Any

import coreouto as co

#: The exception types coreouto's providers raise from
#: ``format_tool_result`` when the wire format cannot carry a payload.
#: openai: "OpenAI Chat Completions does not support multimodal tool
#: results"; openai-response: "does not support video/audio blocks in
#: tool results"; anthropic/google: a malformed-block ValueError.
_REJECTION_ERRORS = (ValueError, TypeError)


def _describe(exc: BaseException, *, tool_name: str, kind: str) -> str:
    """The tool-result text handed to the model when a payload is refused.

    Written for the MODEL: it states what happened, that the file itself
    was fine, and the concrete alternatives — so the agent picks a next
    step instead of retrying the same call forever.
    """

    return (
        f"The {tool_name} tool could not return the {kind} to the model: "
        f"{exc}\n"
        "The file was read successfully — this is a PROVIDER / MODEL "
        "limitation (this provider's wire format does not carry "
        f"{kind} payloads in a tool result), not a problem with the file.\n"
        "Do NOT retry the same call as-is. Instead: (a) if another provider "
        "or model is available for this session, switch to one that accepts "
        f"{kind} payloads and call the tool again; (b) if the tool has a "
        "text-only fallback for this kind, use it; (c) otherwise continue "
        f"with the information available in the text channel and say plainly "
        f"that the {kind} could not be shown."
    )


def _kind_of(tool_name: str) -> str:
    """The payload kind a tool name produces, for the refusal message."""

    return {
        "Image": "image",
        "Video": "video",
        "Audio": "audio",
        "Computer": "screenshot",
    }.get(tool_name, "media")


def wrap_provider(provider: Any) -> Any:
    """Return `provider` with a failure-tolerant ``format_tool_result``.

    The original method is kept as ``_format_tool_result``; the wrapper
    calls it and, if it refuses the payload, returns a plain text tool
    result carrying the explanation. Every other method is untouched —
    the provider is duck-typed in coreouto, so wrapping is done by
    attribute assignment on the instance rather than by subclassing a
    concrete provider class.
    """

    original = getattr(provider, "format_tool_result", None)
    if original is None or getattr(provider, "_miniouto_mm_wrapped", False):
        return provider

    def format_tool_result(tool_call: Any, result: Any) -> Any:
        try:
            return original(tool_call, result)
        except _REJECTION_ERRORS as exc:
            # The provider refused THIS payload. Hand the model a text
            # result instead of letting the exception end the turn.
            message = _describe(
                exc,
                tool_name=getattr(tool_call, "name", "") or "media",
                kind=_kind_of(getattr(tool_call, "name", "") or ""),
            )
            return co.Message(
                role="tool",
                tool_call_id=getattr(tool_call, "id", "") or "",
                content=message,
                name=getattr(tool_call, "name", "") or "",
            )

    provider._miniouto_mm_wrapped = True
    provider.format_tool_result = format_tool_result
    return provider
