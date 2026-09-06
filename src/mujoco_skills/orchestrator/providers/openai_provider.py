"""OpenAI Responses API implementation of ``LLMProvider``.

This file owns every OpenAI-specific detail: translating neutral messages,
function calls, and function outputs to and from the Responses API. Requires
``OPENAI_API_KEY`` in the environment.
"""

from __future__ import annotations

import json
import os

from openai import OpenAI

from mujoco_skills.orchestrator.schema import (
    Message,
    ToolCall,
    ToolSpec,
    to_openai_response,
)

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-terra")


def _empty_usage() -> dict[str, int]:
    return {
        "requests": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }


class OpenAIProvider:
    def __init__(self, model: str = DEFAULT_MODEL, client: OpenAI | None = None):
        self.model = model
        self.client = client or OpenAI()  # reads OPENAI_API_KEY from env
        # Reasoning responses can contain opaque reasoning items before their
        # function calls. Responses requires those output items to be replayed
        # with the subsequent function_call_output. Keep them private to this
        # provider so the provider-neutral Message contract stays small.
        self._response_outputs_by_call_id: dict[str, list[object]] = {}
        self.usage = _empty_usage()

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
            tool_choice={"type": "function", "name": tool_name},
        )

    def _chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        tool_choice: dict | None = None,
    ) -> Message:
        response_options = {}
        if self.model.startswith("gpt-5.6"):
            response_options["reasoning"] = {
                "effort": os.environ.get(
                    "ORCHESTRATOR_REASONING_EFFORT",
                    "medium",
                )
            }
        response = self.client.responses.create(
            model=self.model,
            input=self._to_response_input(messages),
            tools=[to_openai_response(tool) for tool in tools],
            **({"tool_choice": tool_choice} if tool_choice else {}),
            **response_options,
        )
        self._record_usage(response)
        reply = _from_response(response)
        if reply.tool_calls:
            outputs = list(response.output)
            for call in reply.tool_calls:
                self._response_outputs_by_call_id[call.id] = outputs
        return reply

    def _record_usage(self, response) -> None:
        """Accumulate usage across every Responses call in this provider run."""
        usage = _field(response, "usage")
        self.usage["requests"] += 1
        if usage is None:
            return
        input_details = _field(usage, "input_tokens_details")
        output_details = _field(usage, "output_tokens_details")
        increments = {
            "input_tokens": _field(usage, "input_tokens"),
            "cached_input_tokens": _field(input_details, "cached_tokens"),
            "cache_write_tokens": _field(input_details, "cache_write_tokens"),
            "output_tokens": _field(usage, "output_tokens"),
            "reasoning_tokens": _field(output_details, "reasoning_tokens"),
            "total_tokens": _field(usage, "total_tokens"),
        }
        for name, value in increments.items():
            if isinstance(value, int):
                self.usage[name] += value

    def _to_response_input(self, messages: list[Message]) -> list[object]:
        items: list[object] = []
        for message in messages:
            if message.role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": message.content or "",
                    }
                )
                continue

            if message.role == "assistant" and message.tool_calls:
                cached = self._cached_response_output(message.tool_calls)
                if cached is not None:
                    items.extend(cached)
                    continue
                if message.content:
                    items.append({"role": "assistant", "content": message.content})
                items.extend(
                    _tool_call_to_response_input(call) for call in message.tool_calls
                )
                continue

            items.append({"role": message.role, "content": message.content or ""})
        return items

    def _cached_response_output(self, calls: list[ToolCall]) -> list[object] | None:
        outputs = [self._response_outputs_by_call_id.get(call.id) for call in calls]
        if not outputs or any(output is None for output in outputs):
            return None
        first = outputs[0]
        if any(output is not first for output in outputs[1:]):
            return None
        return first


def _tool_call_to_response_input(call: ToolCall) -> dict:
    """Reconstruct a function-call input when no raw response item is cached."""
    return {
        "type": "function_call",
        "call_id": call.id,
        "name": call.name,
        "arguments": json.dumps(call.arguments),
    }


def _from_response(response) -> Message:
    """OpenAI Responses API result -> provider-neutral Message."""
    tool_calls = []
    for item in response.output:
        if _field(item, "type") != "function_call":
            continue
        tool_calls.append(
            ToolCall(
                id=_field(item, "call_id"),
                name=_field(item, "name"),
                arguments=_parse_args(_field(item, "arguments")),
            )
        )
    content = getattr(response, "output_text", None) or None
    return Message(
        role="assistant",
        content=content,
        tool_calls=tool_calls or None,
    )


def _field(value, name: str):
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _parse_args(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Surface the raw string so the caller/model can see what went wrong.
        return {"__raw_arguments__": raw}
