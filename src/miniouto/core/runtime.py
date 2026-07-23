"""Build a runtime: register providers, presets, subagent tool, and resolve outo."""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import coreouto as co

from ..paths_runtime import INVOCATION_CWD
from ..storage import providers as provider_store
from ..storage import settings as settings_store
from ..storage import skills as skill_store
from ..storage import styles as style_store
from ..tools import registry as tool_registry
from .providers import build_coreouto_provider, clear_coreouto_state

ALL_TOOLS = ["Bash", "Image", "Video", "Audio", "call_subagent"]

# Tracks how deep we are inside a `call_subagent` invocation. 0 = outo (or
# after `build_runtime` has just been called), >=1 = inside a subagent.
# Read by `core.chat` dispatchers to decide whether a tool call came
# from outo (no prefix) or a nested subagent (`subagent-<id>:` prefix).
# Mutated only by the wrapper installed around `call_subagent`'s handler.
_SUBAGENT_DEPTH: ContextVar[int] = ContextVar("miniouto_subagent_depth", default=0)

# Stable per-invocation id (6 hex chars) of the innermost active subagent
# call. Set alongside _SUBAGENT_DEPTH so every hook fired inside that
# subagent's loop (tool calls, thinking, iterations) can be attributed to
# one specific invocation — with parallel subagents this is the only way
# to tell them apart. ContextVars are copied per asyncio task, so
# concurrent `call_subagent` handlers each see their own id.
_SUBAGENT_ID: ContextVar[str | None] = ContextVar("miniouto_subagent_id", default=None)

# Lifecycle observer for subagent invocations: callable(phase, sid, text)
# where phase is "start" or "end". Set per-turn by `core.chat.run_chat`
# (the hooks are global, so this module-level slot is the bridge between
# the wrapped handler and the active turn's sink). None outside a turn.
_SUBAGENT_OBSERVER: Callable[[str, str, str], None] | None = None


def current_subagent_depth() -> int:
    """Return how many subagent layers we're currently nested inside."""

    return _SUBAGENT_DEPTH.get()


def current_subagent_id() -> str | None:
    """Return the innermost active subagent invocation id, or None for outo."""

    return _SUBAGENT_ID.get()


def set_subagent_observer(observer: Callable[[str, str, str], None] | None) -> None:
    """Install (or clear, with None) the subagent lifecycle observer."""

    global _SUBAGENT_OBSERVER
    _SUBAGENT_OBSERVER = observer


def _notify_subagent(phase: str, sid: str, text: str) -> None:
    observer = _SUBAGENT_OBSERVER
    if observer is not None:
        # an observer must never break the subagent loop
        with contextlib.suppress(Exception):
            observer(phase, sid, text)


def _wrap_subagent_handler(inner: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap `call_subagent`'s handler so depth + id ContextVars track it.

    coreouto's `BEFORE_TOOL_CALL` hook is global and does not carry agent
    context, so without this we cannot tell whether a Bash/Image/etc. call
    came from outo or from a subagent. Setting/resetting the ContextVars
    around the inner handler gives the hooks the information they need,
    and minting the id here (this wrapper runs exactly once per subagent
    invocation) gives each invocation a stable `subagent-<6hex>` label.
    """

    async def wrapped(task: str) -> str:
        sid = secrets.token_hex(3)  # 6 hex chars
        depth_token = _SUBAGENT_DEPTH.set(_SUBAGENT_DEPTH.get() + 1)
        id_token = _SUBAGENT_ID.set(sid)
        _notify_subagent("start", sid, task)
        try:
            result = await inner(task)
        except Exception as exc:
            _notify_subagent("end", sid, f"error: {type(exc).__name__}: {exc}")
            raise
        else:
            _notify_subagent("end", sid, result or "")
            return result
        finally:
            _SUBAGENT_ID.reset(id_token)
            _SUBAGENT_DEPTH.reset(depth_token)

    return wrapped


def _build_subagent_tool(
    preset_name: str,
    *,
    description: str,
    provider_config: dict[str, Any],
) -> Any:
    """Build the subagent tool with a non-empty provider_config.

    coreouto's `agent_as_tool` calls `preset.to_config()` and silently
    drops any provider_config we might want to inject — meaning the
    subagent runs with `max_tokens` unset, and Anthropic's 1024 hard
    default silently truncates any long tool call (e.g. a heredoc file
    write). We
    rebuild the same `Tool` shape here, but with our own Agent instance
    built from a config whose `provider_config` carries the cap.
    """

    preset = co.get_agent_preset(preset_name)
    config = preset.to_config()
    if provider_config:
        config.provider_config.update(provider_config)
    sub_agent = co.Agent(config)

    tool_name = f"call_{preset_name}"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The task description to pass to the sub-agent.",
            }
        },
        "required": ["task"],
    }

    async def handler(task: str) -> str:
        return (await sub_agent.call(task)).content

    return co.Tool(
        name=tool_name,
        description=description,
        parameters=parameters,
        handler=handler,
    )


@dataclass
class RuntimeConfig:
    provider_name: str
    model: str
    style_name: str
    subagent_model: str | None = None
    subagent_provider: str | None = None
    session: str | None = None


@dataclass
class ChatOverrides:
    provider: str | None = None
    model: str | None = None
    style: str | None = None


def build_runtime(
    runtime: RuntimeConfig,
    *,
    style_overrides: dict[str, str] | None = None,
    provider_config: dict[str, Any] | None = None,
    on_tool_call: Callable[[str, dict[str, Any]], None] | None = None,
    on_response: Callable[[str, bool], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_iteration: Callable[..., None] | None = None,
    on_provider_error: Callable[..., None] | None = None,
) -> co.Agent:
    """Construct the outo Agent with the active style and subagent wired in.

    `provider_config` is merged into the outo Agent's `provider_config` so that
    canonical settings (max_tokens, temperature, ...) flow through coreouto's
    normalizer without us having to mutate the config after construction.
    `on_tool_call` is called for every tool invocation (outo and subagent)
    with `(tool_name, arguments)`. Pass None to skip.
    `on_response` is called after each LLM response with `(content, has_tool_calls)`
    so the caller can stream intermediate model text. Pass None to skip.
    `on_thinking` is called after each LLM response that carries reasoning
    text (coreouto's ON_THINKING; providers never put thinking into history
    messages, so this hook is the only way to surface it). Pass None to skip.
    `on_iteration` is called after each agent-loop iteration with the same
    kwargs coreouto passes to ON_ITERATION (iteration, messages, response),
    letting the caller stream loop-progress signals. Pass None to skip.
    `on_provider_error` is called whenever an `error_handling` rule matches a
    provider exception (coreouto >= 0.10), with `status_code`, `error_message`,
    `reaction`, `reaction_message` kwargs. Rule-matched errors don't raise, so
    without this hook a retry or termination is invisible to the user.
    Pass None to skip.
    """

    clear_coreouto_state()

    provider = provider_store.get(runtime.provider_name)
    if provider is None:
        raise RuntimeError(
            f"Provider {runtime.provider_name!r} is not configured. "
            "Run `miniouto provider add` first."
        )
    build_coreouto_provider(provider)

    sub_provider_name = runtime.subagent_provider or runtime.provider_name
    sub_provider = provider_store.get(sub_provider_name)
    if sub_provider is None:
        raise RuntimeError(f"Subagent provider {sub_provider_name!r} is not configured.")
    build_coreouto_provider(sub_provider)

    tool_registry.register_all()

    outo_style, subagent_style = _resolve_both_styles(runtime.style_name, style_overrides)
    outo_prompt = _with_cwd("outo", outo_style)
    subagent_prompt = _with_cwd("subagent", subagent_style)

    co.register_agent_preset(
        "subagent",
        model=runtime.subagent_model or runtime.model,
        provider=sub_provider_name,
        system_prompt=subagent_prompt,
        tools=ALL_TOOLS,
        max_iterations=None,
    )

    co.register_agent_preset(
        "outo",
        model=runtime.model,
        provider=runtime.provider_name,
        system_prompt=outo_prompt,
        tools=ALL_TOOLS,
        max_iterations=None,
    )

    # Subagent runs the same model (or its override) and must share the
    # output-token cap, otherwise long tool calls it issues (heredoc file
    # writes) get truncated at
    # the provider's low default (1024 for Anthropic) and we end up with
    # a half-written file and an "I cut off mid-function" loop. We pull
    # the cap from the same lma endpoint as outo so it tracks the
    # subagent's model when one is configured.
    from .context import get_max_output_tokens

    subagent_model = runtime.subagent_model or runtime.model
    subagent_provider_config = dict(provider_config or {})
    subagent_provider_config.setdefault(
        "max_tokens", get_max_output_tokens(subagent_model, sub_provider_name)
    )

    subagent_tool = _build_subagent_tool(
        "subagent",
        description=_subagent_description(),
        provider_config=subagent_provider_config,
    )
    co.register_tool(subagent_tool.name, description=subagent_tool.description)(
        _wrap_subagent_handler(subagent_tool.handler)
    )

    if on_tool_call is not None:
        co.register_hook(co.BEFORE_TOOL_CALL, _make_tool_call_logger(on_tool_call))

    from .context import make_summarize_hook
    summarize_hook = make_summarize_hook(
        runtime.model, runtime.session or "default", runtime.provider_name
    )
    co.register_hook(co.ON_ITERATION, summarize_hook)

    if on_response is not None:
        co.register_hook(co.AFTER_LLM_CALL, _make_response_logger(on_response))

    if on_thinking is not None:
        co.register_hook(co.ON_THINKING, _make_thinking_logger(on_thinking))

    if on_iteration is not None:
        co.register_hook(co.ON_ITERATION, _make_iteration_logger(on_iteration))

    if on_provider_error is not None:
        co.register_hook(co.ON_PROVIDER_ERROR, _make_provider_error_logger(on_provider_error))

    outo_config = co.get_agent_preset("outo").to_config()
    if provider_config:
        outo_config.provider_config.update(provider_config)

    subagent_config = co.get_agent_preset("subagent").to_config()
    subagent_config.provider_config.update(subagent_provider_config)

    return co.Agent(outo_config)


def _make_tool_call_logger(callback: Callable[[str, dict[str, Any]], None]):
    def hook(*, name: str, arguments: dict[str, Any], **kwargs: Any) -> None:
        callback(name, arguments)

    return hook


def _make_provider_error_logger(callback: Callable[..., None]):
    """Build an ON_PROVIDER_ERROR hook that forwards the rule-match payload.

    coreouto fires ON_PROVIDER_ERROR after an `error_handling` rule matches
    a provider exception, before executing the reaction. Only the four
    renderable fields are forwarded — the raw exception and the full
    message list are also in the payload but are useless to a sink.
    """

    def hook(
        *,
        status_code: int | None,
        error_message: str,
        reaction: str,
        reaction_message: str,
        **kwargs: Any,
    ) -> None:
        callback(
            status_code=status_code,
            error_message=error_message,
            reaction=reaction,
            reaction_message=reaction_message,
        )

    return hook


def _make_iteration_logger(callback: Callable[..., None]):
    """Build an ON_ITERATION hook that forwards coreouto's kwargs verbatim.

    coreouto fires ON_ITERATION after each agent-loop iteration with
    `(iteration, messages, response)`. Forwarding them as-is lets the
    caller stream loop-progress signals without us binding the public
    hook signature to a particular coreouto version.
    """

    def hook(*, iteration: int, messages: Any, response: Any, **kwargs: Any) -> None:
        callback(iteration=iteration, messages=messages, response=response)

    return hook


def _make_response_logger(
    callback: Callable[[str, bool], None],
):
    """Build the AFTER_LLM_CALL hook that streams intermediate model text.

    `callback(content, has_tool_calls)` receives the full response text and
    a flag indicating whether the model emitted tool calls in this turn.
    Final responses (no tool calls) are flagged so the caller can skip them
    — the terminal answer is rendered separately by the sink.
    """

    def hook(*, response: Any, messages: Any, **kwargs: Any) -> None:
        if not response or not hasattr(response, "content") or not response.content:
            return
        tool_calls = getattr(response, "tool_calls", None) or []
        callback(response.content, bool(tool_calls))

    return hook


def _make_thinking_logger(callback: Callable[[str], None]):
    """Build the ON_THINKING hook that streams reasoning text.

    coreouto fires ON_THINKING once per LLM response that carries thinking
    (Anthropic extended thinking, OpenAI reasoning summaries). Fired inside
    subagent loops too, so the ContextVars above attribute it correctly.
    """

    def hook(*, thinking: str, **kwargs: Any) -> None:
        if thinking:
            callback(thinking)

    return hook


def _subagent_description() -> str:
    return (
        "Delegate a self-contained task to the subagent. The subagent "
        "has its own tool access (Bash/Image/Video/Audio) "
        "and a fresh context. Pass the full brief in the `task` argument. "
        "The tool blocks until the subagent terminates the loop (a turn "
        "with no tool calls) and returns the subagent's final text as the "
        "result."
    )


def _resolve_both_styles(
    style_name: str, overrides: dict[str, str] | None
) -> tuple[str, str]:
    """Return (outo_prompt, subagent_prompt) from the active style document.

    The style document is split at <subagent>...</subagent> tags. If no such
    tags exist, the entire document is the outo prompt and subagent uses a
    minimal built-in default. Active skills are prepended to both prompts.
    """

    raw = _read_raw_style(style_name, overrides)
    outo_part, subagent_part = style_store.split_style(raw)
    if not subagent_part:
        subagent_part = _fallback_style("subagent")

    skills_content = _load_active_skills()
    if skills_content:
        outo_part = skills_content + "\n\n" + outo_part
        subagent_part = skills_content + "\n\n" + subagent_part

    return outo_part, subagent_part


def _read_raw_style(name: str, overrides: dict[str, str] | None) -> str:
    if overrides and name in overrides:
        return overrides[name]
    content = style_store.read(name)
    if content is None:
        if name == "default":
            return style_store.builtin_default() or _fallback_style("outo")
        return _fallback_style(name)
    return content


def _load_active_skills() -> str:
    """Load content from all available skills."""

    skills = skill_store.list_skills()
    if not skills:
        return ""

    parts: list[str] = []
    for skill in skills:
        if skill.content:
            parts.append(f"# Skill: {skill.name}\n\n{skill.content}")

    return "\n\n---\n\n".join(parts)


def _with_cwd(role: str, body: str) -> str:
    """Prepend an absolute-cwd preamble to a style body at runtime.

    The preamble tells the model where the user invoked miniouto from so
    relative paths and bash commands resolve against the right directory.
    Not stored on disk — injected fresh each build_runtime call.
    """

    if role == "subagent":
        preamble = (
            f"You operate inside this working directory: {INVOCATION_CWD}\n"
            "All relative paths and shell commands resolve against it. "
            "Use absolute paths when in doubt.\n\n"
        )
    else:
        preamble = (
            f"The user invoked miniouto from: {INVOCATION_CWD}\n"
            "When delegating to the subagent, pass relative paths verbatim "
            "or absolute paths explicitly — the subagent's tools resolve "
            "against this directory.\n\n"
        )
    return preamble + body


def _fallback_style(name: str) -> str:
    if name == "subagent":
        return (
            "You are subagent. Execute the brief directly using your tools. "
            "To finish, respond with text and no tool call — that text becomes "
            "the final answer returned to the parent. Use the `continue_loop` "
            "tool if you need to send text to the parent while still planning "
            "more tool calls."
        )
    return (
        f"You are {name}. Use the call_subagent tool for non-trivial work. "
        "To finish, respond with text and no tool call — that text becomes "
        "the final answer returned to the user. Use the `continue_loop` tool "
        "if you need to share progress while still planning more tool calls."
    )


def resolve_runtime_from_settings(overrides: ChatOverrides | None = None) -> RuntimeConfig:
    s = settings_store.load()
    overrides = overrides or ChatOverrides()

    provider_name = overrides.provider or s.provider
    if not provider_name:
        raise RuntimeError(
            "No default provider set. Run `miniouto provider add` and "
            "`miniouto provider default <name>` first."
        )
    provider = provider_store.get(provider_name)
    if provider is None:
        raise RuntimeError(f"Provider {provider_name!r} is not configured.")

    model = overrides.model or s.model or provider.default_model
    if not model:
        raise RuntimeError(
            f"No model specified for provider {provider_name!r}. "
            "Pass --model on the chat command, or set a default via "
            "`miniouto provider add --default-model <name>`."
        )

    return RuntimeConfig(
        provider_name=provider_name,
        model=model,
        style_name=overrides.style or s.style or "default",
        session=s.session or "default",
    )
