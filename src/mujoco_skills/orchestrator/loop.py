"""The provider-neutral co-authoring loop.

Request -> model -> (tool calls -> execute -> feed back)* -> final text. Knows
nothing about OpenAI or HTTP: the provider yields tool calls, `execute` runs
them. Non-streaming and one-shot; a REPL can reuse `run` by passing prior
`history`.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from mujoco_skills.orchestrator.prompt import SYSTEM_PROMPT
from mujoco_skills.orchestrator.providers.base import LLMProvider
from mujoco_skills.orchestrator.schema import Message, ToolSpec

DEFAULT_MAX_ITERS = 12


def run(
    user_msg: str,
    provider: LLMProvider,
    tools: list[ToolSpec],
    execute: Callable[[str, dict], dict],
    *,
    history: list[Message] | None = None,
    max_iters: int = DEFAULT_MAX_ITERS,
    terminal_tools: set[str] | None = None,
    on_event: Callable[[str, object], None] | None = None,
) -> list[Message]:
    """Drive one request to completion. Returns the full message history.

    `on_event(kind, payload)` is an optional hook for CLI/logging:
      "assistant_text", "tool_call", "tool_result", "max_iters".
    """
    msgs: list[Message] = history or [Message("system", SYSTEM_PROMPT)]
    msgs.append(Message("user", user_msg))

    for _ in range(max_iters):
        reply = provider.chat(msgs, tools)
        msgs.append(reply)

        if not reply.tool_calls:
            if on_event and reply.content:
                on_event("assistant_text", reply.content)
            return msgs  # model is done

        # Run every requested tool call and feed each result back by id.
        for call in reply.tool_calls:
            if on_event:
                on_event("tool_call", call)
            result = execute(call.name, call.arguments)
            if on_event:
                on_event("tool_result", (call.name, result))
            msgs.append(
                Message("tool", content=json.dumps(result), tool_call_id=call.id)
            )
            # A terminal tool call that errored must not end the loop: the
            # error is fed back to the model (above) so it can retry instead
            # of silently losing the real failure reason.
            is_error = isinstance(result, dict) and bool(result.get("error"))
            if terminal_tools and call.name in terminal_tools and not is_error:
                return msgs

    if on_event:
        on_event("max_iters", max_iters)
    return msgs
