"""Context window monitoring and auto-summarization."""

from __future__ import annotations

from typing import Any

import httpx

CONTEXT_WINDOW_API = "https://lcw-api.blp.sh/context-window?model={model}"
CONTEXT_WINDOW_CACHE: dict[str, int] = {}
SUMMARIZE_THRESHOLD = 0.8


def get_context_window(model: str) -> int | None:
    """Get the context window size for a model from the API."""

    if model in CONTEXT_WINDOW_CACHE:
        return CONTEXT_WINDOW_CACHE[model]

    try:
        url = CONTEXT_WINDOW_API.format(model=model)
        with httpx.Client(timeout=10.0) as client:
            r = client.get(url)
            r.raise_for_status()
            data = r.json()
            if data.get("success") and data.get("data"):
                window = data["data"].get("contextWindow")
                if window:
                    CONTEXT_WINDOW_CACHE[model] = window
                    return window
    except Exception:
        pass

    return None


def make_summarize_hook(model: str, session_name: str) -> Any:
    """Create a hook that summarizes when context window is 80% full.

    Calls the LLM to produce a structured summary of the conversation:
    - What was done so far
    - What is currently in progress
    - What needs to be done next
    """

    from coreouto.contrib.hooks import auto_summarize_hook

    window = get_context_window(model)
    if not window:
        return lambda **kwargs: None

    threshold = int(window * SUMMARIZE_THRESHOLD)

    def summarizer(messages: list[Any]) -> list[Any]:
        if len(messages) <= 2:
            return messages

        system_msgs = [m for m in messages if m.role == "system"]
        other_msgs = [m for m in messages if m.role != "system"]

        existing_summary: str | None = None
        msgs_to_summarize: list[Any] = []

        for m in other_msgs:
            if m.role == "user" and m.content.startswith("[Summary"):
                existing_summary = m.content
            else:
                msgs_to_summarize.append(m)

        conversation_text: list[str] = []
        if existing_summary:
            conversation_text.append(f"Previous summary:\n{existing_summary}")

        for m in msgs_to_summarize:
            if m.role == "user" and m.content:
                conversation_text.append(f"User: {m.content}")
            elif m.role == "assistant" and m.content:
                conversation_text.append(f"Agent: {m.content}")
            elif m.role == "tool" and m.content:
                conversation_text.append(f"Tool: {m.content[:500]}")

        conversation = "\n".join(conversation_text)

        summary_prompt = (
            "Summarize the following conversation into three sections:\n"
            "1. DONE: What has been completed so far\n"
            "2. IN PROGRESS: What is currently being worked on\n"
            "3. NEXT: What needs to be done next\n\n"
            "Be concise but specific. Include file paths, command names, "
            "and concrete details.\n\n"
            f"Conversation:\n{conversation}"
        )

        from coreouto._types import Message

        try:
            import coreouto as co
            summary_agent = co.Agent(co.AgentConfig(
                name="summarizer",
                model=model,
                provider="",
                system_prompt="You are a conversation summarizer. Produce concise, structured summaries.",
                max_iterations=1,
            ))
            result = summary_agent.call_sync(summary_prompt)
            summary_content = f"[Summary]\n{result.content}"
        except Exception:
            summary_content = (
                "[Summary]\n"
                "Unable to generate LLM summary. Continuing with truncated context."
            )

        summary_msg = Message(role="user", content=summary_content)
        return [*system_msgs, summary_msg]

    hook = auto_summarize_hook(threshold=threshold, summarize_fn=summarizer)
    return hook
