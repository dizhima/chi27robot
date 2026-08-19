"""OpenAI implementation of LLMProvider.

This file owns every OpenAI-specific detail: translating neutral Messages to the
chat.completions wire format (and back), and how tool calls / tool results are
shaped. The neutral tool schema comes from schema.to_openai. Requires
OPENAI_API_KEY in the environment.
"""

from __future__ import annotations

import json
import os

from openai import OpenAI

from mujoco_skills.orchestrator.schema import Message, ToolCall, ToolSpec, to_openai

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-terra")


class OpenAIProvider:
    def __init__(self, model: str = DEFAULT_MODEL, client: OpenAI | None = None):
        self.model = model
        self.client = client or OpenAI()  # reads OPENAI_API_KEY from env

    def chat(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        return self._chat(messages, tools)

    def force_tool(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_name: str,
    ) -> Message:
        """Request one required structured call (used by ground and augment)."""
        return self._chat(
            messages,
            tools,
            tool_choice={"type": "function", "function": {"name": tool_name}},
        )

    def _chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        tool_choice: dict | None = None,
    ) -> Message:
        model_options = {}
        if self.model.startswith("gpt-5.6"):
            # Chat Completions requires reasoning disabled when GPT-5.6 uses
            # function tools. The Responses API can combine both, but that
            # migration is intentionally outside this checkpoint.
            model_options["reasoning_effort"] = os.environ.get(
                "ORCHESTRATOR_REASONING_EFFORT",
                "none",
            )
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[_to_wire(m) for m in messages],
            tools=[to_openai(t) for t in tools],
            **({"tool_choice": tool_choice} if tool_choice else {}),
            **model_options,
        )
        return _from_wire(resp.choices[0].message)


def _to_wire(m: Message) -> dict:
    """Neutral Message -> OpenAI chat message dict."""
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content or ""}
    if m.role == "assistant" and m.tool_calls:
        return {
            "role": "assistant",
            "content": m.content,  # may be None alongside tool_calls
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in m.tool_calls
            ],
        }
    return {"role": m.role, "content": m.content or ""}


def _from_wire(msg) -> Message:
    """OpenAI response message -> neutral Message."""
    tool_calls = None
    if msg.tool_calls:
        tool_calls = [
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                # arguments arrive as a JSON string; tolerate empty/malformed.
                arguments=_parse_args(tc.function.arguments),
            )
            for tc in msg.tool_calls
        ]
    return Message(role="assistant", content=msg.content, tool_calls=tool_calls)


def _parse_args(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Surface the raw string so the caller/model can see what went wrong.
        return {"__raw_arguments__": raw}
