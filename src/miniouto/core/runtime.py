"""Build a runtime: register providers, presets, subagent tool, and resolve outo."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import coreouto as co
from rich.console import Console

from ..paths_runtime import INVOCATION_CWD
from ..storage import providers as provider_store
from ..storage import settings as settings_store
from ..storage import skills as skill_store
from ..storage import styles as style_store
from ..tools import registry as tool_registry
from .providers import build_coreouto_provider, clear_coreouto_state

ALL_TOOLS = ["Write", "Edit", "Delete", "Bash", "call_subagent"]

_hook_console = Console(stderr=True, soft_wrap=False, highlight=False)


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
) -> co.Agent:
    """Construct the outo Agent with the active style and subagent wired in.

    `provider_config` is merged into the outo Agent's `provider_config` so that
    canonical settings (max_tokens, temperature, ...) flow through coreouto's
    normalizer without us having to mutate the config after construction.
    `on_tool_call` is called for every tool invocation (outo and subagent)
    with `(tool_name, arguments)`. Pass None to skip.
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
        max_iterations=40,
    )

    co.register_agent_preset(
        "outo",
        model=runtime.model,
        provider=runtime.provider_name,
        system_prompt=outo_prompt,
        tools=ALL_TOOLS,
    )

    subagent_tool = co.agent_as_tool("subagent", description=_subagent_description())
    co.register_tool(subagent_tool.name, description=subagent_tool.description)(
        subagent_tool.handler
    )

    if on_tool_call is not None:
        co.register_hook(co.BEFORE_TOOL_CALL, _make_tool_call_logger(on_tool_call))

    from .context import make_summarize_hook
    summarize_hook = make_summarize_hook(runtime.model, runtime.session or "default")
    co.register_hook(co.ON_ITERATION, summarize_hook)

    co.register_hook(co.AFTER_LLM_CALL, _make_response_logger())

    outo_config = co.get_agent_preset("outo").to_config()
    if provider_config:
        outo_config.provider_config.update(provider_config)
    return co.Agent(outo_config)


def _make_tool_call_logger(callback: Callable[[str, dict[str, Any]], None]):
    def hook(*, name: str, arguments: dict[str, Any], **kwargs: Any) -> None:
        callback(name, arguments)

    return hook


def _make_response_logger():
    def hook(*, response: Any, messages: Any, **kwargs: Any) -> None:
        if response and hasattr(response, "content") and response.content:
            content = response.content
            if len(content) > 200:
                content = content[:197] + "..."
            _hook_console.print(f"  outo: {content}", style="dim", markup=False)

    return hook


def _subagent_description() -> str:
    return (
        "Delegate a self-contained task to the subagent. The subagent "
        "has its own tool access (Write/Edit/Delete/Bash) and a fresh "
        "context. Pass the full brief in the `task` argument. The tool "
        "blocks until the subagent terminates the loop (a turn with no "
        "tool calls) and returns the subagent's final text as the result."
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

    model = overrides.model or provider.default_model
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
