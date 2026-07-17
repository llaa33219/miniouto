"""Default provider error-handling rules (coreouto >= 0.10).

coreouto 0.10 replaced the agent-level `retry_intervals` knob with
provider-level `error_handling`: a list of `ErrorRule`s matched against
each exception the provider SDK raises (by `status_code`/`code` int and
a `content_contains` substring of the rendered error, first match wins).
`build_coreouto_provider` attaches the list returned here to every
provider instance it registers, so the outo agent and the subagent
inherit the same behavior.

The per-format lists mirror coreouto's own recipes (examples 18-20 in
the coreouto repo), tuned to each SDK's exception hierarchy. Every match
fires coreouto's `ON_PROVIDER_ERROR` hook; `core.chat` forwards those
matches to the event sink as `provider:` loop events.

Rule ordering is significant: more specific `content_contains` matchers
must precede generic same-status fallbacks.
"""

from __future__ import annotations

import coreouto as co
from coreouto.contrib.error_presets import COMMON_HTTP_ERRORS

# OpenAI Chat Completions and the Responses API share the openai SDK's
# exception hierarchy, so one list covers both api_format values.
_OPENAI_RULES: list[co.ErrorRule] = [
    *COMMON_HTTP_ERRORS,
    co.ErrorRule(
        status_code=400,
        content_contains="context_length_exceeded",
        reaction="terminate",
        message="Context window exceeded. Reduce the conversation length or clear history.",
    ),
    co.ErrorRule(
        status_code=400,
        content_contains="invalid_schema",
        reaction="tool_result",
        message=(
            "Tool arguments failed schema validation. "
            "Check parameter types and required fields."
        ),
    ),
    co.ErrorRule(
        status_code=400,
        content_contains="tool",
        reaction="tool_result",
        message="Invalid tool call. Verify the tool name exists and try again.",
    ),
    co.ErrorRule(
        status_code=404,
        content_contains="model",
        reaction="terminate",
        message="Model not found. Check the model name in your config.",
    ),
    co.ErrorRule(
        status_code=422,
        reaction="tool_result",
        message=(
            "Request schema validation failed. "
            "The tool parameters don't match the declared schema."
        ),
    ),
]

_ANTHROPIC_RULES: list[co.ErrorRule] = [
    co.ErrorRule(
        status_code=429,
        reaction="retry",
        message="Anthropic rate limit — retrying.",
        retry_after=1.0,
        retry_backoff=2.0,
        retry_max=5,
    ),
    # 529 OverloadedError is Anthropic-specific; worth a longer initial backoff.
    co.ErrorRule(
        status_code=529,
        reaction="retry",
        message="Anthropic overloaded — retrying with longer backoff.",
        retry_after=5.0,
        retry_backoff=2.0,
        retry_max=3,
    ),
    co.ErrorRule(
        status_code=500,
        reaction="retry",
        message="Internal server error — retrying.",
        retry_after=2.0,
        retry_backoff=2.0,
        retry_max=3,
    ),
    co.ErrorRule(
        status_code=503,
        reaction="retry",
        message="Service unavailable — retrying.",
        retry_after=2.0,
        retry_backoff=2.0,
        retry_max=3,
    ),
    co.ErrorRule(
        status_code=401,
        reaction="terminate",
        message="Authentication failed. Check your Anthropic API key.",
    ),
    co.ErrorRule(
        status_code=403,
        reaction="terminate",
        message="Permission denied. Your API key may not have access to this model.",
    ),
    # Anthropic returns this when input + max_tokens exceeds the model's
    # context limit. Not recoverable mid-conversation.
    co.ErrorRule(
        status_code=400,
        content_contains="context",
        reaction="terminate",
        message="Context too long. Reduce conversation history or lower max_tokens.",
    ),
    co.ErrorRule(
        status_code=400,
        content_contains="tool",
        reaction="tool_result",
        message="Invalid tool call. Check the tool name and argument schema.",
    ),
    co.ErrorRule(
        status_code=413,
        reaction="terminate",
        message="Request body too large. Reduce the number of messages or content size.",
    ),
    co.ErrorRule(
        status_code=422,
        reaction="tool_result",
        message=(
            "Schema validation failed. "
            "The tool parameters don't match the declared input_schema."
        ),
    ),
]

_GOOGLE_RULES: list[co.ErrorRule] = [
    # Google's quota system can be aggressive — longer initial backoff.
    co.ErrorRule(
        status_code=429,
        reaction="retry",
        message="Google API quota exceeded — retrying.",
        retry_after=2.0,
        retry_backoff=2.0,
        retry_max=5,
    ),
    co.ErrorRule(
        status_code=500,
        reaction="retry",
        message="Google internal error — retrying.",
        retry_after=2.0,
        retry_backoff=2.0,
        retry_max=3,
    ),
    co.ErrorRule(
        status_code=503,
        reaction="retry",
        message="Google service unavailable — retrying.",
        retry_after=2.0,
        retry_backoff=2.0,
        retry_max=3,
    ),
    co.ErrorRule(
        status_code=401,
        reaction="terminate",
        message="Google API authentication failed. Check your API key or OAuth token.",
    ),
    co.ErrorRule(
        status_code=403,
        reaction="terminate",
        message="Permission denied. Your Google Cloud project may lack access to this model.",
    ),
    # google-genai collapses all 4xx into ClientError; distinguish by the
    # .status enum string inside the error message. First match wins, so
    # the specific content matchers precede the generic 400 fallback.
    co.ErrorRule(
        status_code=400,
        content_contains="tool",
        reaction="tool_result",
        message="Invalid tool call. Check function name and argument schema.",
    ),
    co.ErrorRule(
        status_code=400,
        content_contains="safety",
        reaction="terminate",
        message="Request rejected by Google's safety filter. Adjust your input.",
    ),
    co.ErrorRule(
        status_code=400,
        content_contains="precondition",
        reaction="terminate",
        message="Request violates a Google API precondition. Check model capabilities.",
    ),
    co.ErrorRule(
        status_code=400,
        reaction="tool_result",
        message="Invalid request (Google API). Check your tool arguments and request format.",
    ),
    co.ErrorRule(
        status_code=404,
        reaction="terminate",
        message="Model not found. Check the model name (e.g. 'gemini-2.0-flash').",
    ),
]


def default_error_handling(api_format: str) -> list[co.ErrorRule]:
    """Return the default `ErrorRule` list for a supported api_format.

    Unknown formats get the shared HTTP preset — `core.providers` only
    calls this with the four supported formats, so the fallback exists
    for direct callers.
    """

    if api_format in ("openai", "openai-response"):
        return _OPENAI_RULES
    if api_format == "anthropic":
        return _ANTHROPIC_RULES
    if api_format == "google":
        return _GOOGLE_RULES
    return list(COMMON_HTTP_ERRORS)
