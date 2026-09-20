"""Thin wrapper over the Anthropic SDK. Tool-use only, so every response is
structured -- Claude never replies with free text here. The API key is passed in by
the caller (api/services), never read from settings here: finrecur/ never imports
api/."""

from typing import Protocol

import anthropic

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1500


class ToolCaller(Protocol):
    """What explain()/propose() actually need -- lets tests pass a fake without
    subclassing the real SDK-backed client."""

    def call_tool(
        self, system: str, user_content: str, tool_name: str, tool_schema: dict
    ) -> dict | None: ...


class ClaudeClient:
    def __init__(self, api_key: str):
        self._client = anthropic.Anthropic(api_key=api_key)

    def call_tool(
        self, system: str, user_content: str, tool_name: str, tool_schema: dict
    ) -> dict | None:
        response = self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=[
                {
                    "name": tool_name,
                    "description": tool_schema.get("description", tool_name),
                    "input_schema": {k: v for k, v in tool_schema.items() if k != "description"},
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user_content}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return block.input
        return None
