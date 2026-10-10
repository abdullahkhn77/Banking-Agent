"""LLM backends for the agent.

``OpenAICompatLLM`` talks to an OpenAI-compatible endpoint (vLLM in production, e.g. Ollama locally). It is the only
backend whose behaviour may be reported as a model metric.

``ReplayLLM`` and ``ScriptedLLM`` are deterministic stand-ins for tests, harness checks and the red team only. They
NEVER produce reported model metrics.
"""

from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from agent.config import Settings


class LLMBackend(Protocol):
    def invoke(self, messages: list[BaseMessage], tools: list[dict[str, Any]]) -> AIMessage: ...


class OpenAICompatLLM:
    """ChatOpenAI against any OpenAI-compatible endpoint, with optional JSONL recording of every request.

    Recordings (one ``{"messages", "tools"}`` object per line, OpenAI wire format) are replayed in phase 6 as the
    real benchmark workload.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "EMPTY",
        temperature: float = 0.0,
        max_tokens: int = 512,
        timeout_s: float = 60.0,
        record_path: str | Path | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.chat = ChatOpenAI(
            base_url=base_url,
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout_s,
            max_retries=2,
            http_client=http_client,
        )
        self.record_path = Path(record_path) if record_path else None
        self._bound: dict[str, Runnable[Any, Any]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings, record_path: str | Path | None = None) -> OpenAICompatLLM:
        return cls(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            timeout_s=settings.llm_timeout_s,
            record_path=record_path,
        )

    def invoke(self, messages: list[BaseMessage], tools: list[dict[str, Any]]) -> AIMessage:
        if self.record_path is not None:
            self._record(messages, tools)
        result = self._bind(tools).invoke(messages)
        if not isinstance(result, AIMessage):
            raise TypeError(f"expected AIMessage from the model, got {type(result).__name__}")
        return result

    def _bind(self, tools: list[dict[str, Any]]) -> Runnable[Any, Any]:
        key = json.dumps(tools, sort_keys=True)
        with self._lock:
            if key not in self._bound:
                self._bound[key] = self.chat.bind_tools(tools, tool_choice="auto") if tools else self.chat
            return self._bound[key]

    def _record(self, messages: list[BaseMessage], tools: list[dict[str, Any]]) -> None:
        assert self.record_path is not None
        line = json.dumps({"messages": to_openai_messages(messages), "tools": tools}, ensure_ascii=False, default=str)
        with self._lock:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            with self.record_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class ReplayLLM:
    """Returns pre-written steps in order. Tests and harness checks only; never produces reported metrics.

    A step is ``{"tool_calls": [{"name": ..., "args": {...}}]}`` or ``{"content": "..."}``. Past the last step it
    returns a polite closing message.
    """

    CLOSING = "Is there anything else I can help you with?"

    def __init__(self, steps: Sequence[dict[str, Any]]) -> None:
        self.steps = list(steps)
        self.calls: list[list[BaseMessage]] = []  # messages received on each call, for assertions
        self._next = 0
        self._ids = itertools.count(1)

    def invoke(self, messages: list[BaseMessage], tools: list[dict[str, Any]]) -> AIMessage:
        self.calls.append(list(messages))
        if self._next >= len(self.steps):
            return AIMessage(content=self.CLOSING)
        step = self.steps[self._next]
        self._next += 1
        if "tool_calls" in step:
            calls = [
                {
                    "name": call["name"],
                    "args": call.get("args", {}),
                    "id": call.get("id") or f"call_{next(self._ids)}",
                    "type": "tool_call",
                }
                for call in step["tool_calls"]
            ]
            return AIMessage(content=step.get("content", ""), tool_calls=calls)
        return AIMessage(content=step["content"])


class ScriptedLLM:
    """Arbitrary deterministic policy over the message history. Tests, harness checks and red team only."""

    def __init__(self, policy: Callable[[list[BaseMessage]], AIMessage]) -> None:
        self.policy = policy
        self.calls: list[list[BaseMessage]] = []

    def invoke(self, messages: list[BaseMessage], tools: list[dict[str, Any]]) -> AIMessage:
        self.calls.append(list(messages))
        return self.policy(list(messages))


def to_openai_messages(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """Convert to the OpenAI chat wire format, as ChatOpenAI sends it.

    Unlike ``langchain_core.messages.convert_to_openai_messages``, this keeps malformed (invalid) tool calls, which
    ChatOpenAI does send to the API.
    """
    converted: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            converted.append({"role": "system", "content": message_text(message.content)})
        elif isinstance(message, HumanMessage):
            converted.append({"role": "user", "content": message_text(message.content)})
        elif isinstance(message, AIMessage):
            calls = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": json.dumps(call["args"], ensure_ascii=False)},
                }
                for call in message.tool_calls
            ]
            calls += [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["args"] or ""},
                }
                for call in message.invalid_tool_calls
            ]
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": message_text(message.content) or (None if calls else ""),
            }
            if calls:
                entry["tool_calls"] = calls
            converted.append(entry)
        elif isinstance(message, ToolMessage):
            converted.append(
                {"role": "tool", "tool_call_id": message.tool_call_id, "content": message_text(message.content)}
            )
        else:
            raise TypeError(f"unsupported message type: {type(message).__name__}")
    return converted


def message_text(content: Any) -> str:
    """Plain text of a message content (a string or a list of content blocks)."""
    if isinstance(content, str):
        return content
    parts = [part.get("text", "") if isinstance(part, dict) else str(part) for part in content or []]
    return "".join(parts)
