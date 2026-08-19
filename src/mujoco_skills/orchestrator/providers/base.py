"""The provider-neutral LLM interface the loop depends on.

The loop only ever calls `chat(messages, tools) -> Message`; everything
provider-specific (SDK init, tool-call parsing, tool-result message shape) lives
behind an implementation of this Protocol. OpenAI is implemented today; Claude
and Gemini can be added later without touching the loop.
"""

from __future__ import annotations

from typing import Protocol

from mujoco_skills.orchestrator.schema import Message, ToolSpec


class LLMProvider(Protocol):
    def chat(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        """Send the conversation + tool specs, return one assistant Message.

        The returned message either carries `tool_calls` (the loop should run
        them and continue) or only `content` (the loop should terminate).
        """
        ...

    def force_tool(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_name: str,
    ) -> Message:
        """Require one named structured tool call."""
        ...
