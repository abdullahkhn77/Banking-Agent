import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.tool import invalid_tool_call
from langchain_openai.chat_models.base import _convert_message_to_dict

from agent.config import Settings
from agent.llm import OpenAICompatLLM, ReplayLLM, ScriptedLLM, to_openai_messages
from agent.tools import openai_tools

TOOLS = openai_tools()


def _completion(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }


def _mock_openai(reply: dict[str, Any], requests: list[dict[str, Any]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json=_completion(reply))

    return httpx.Client(transport=httpx.MockTransport(handler))


TOOL_CALL_REPLY = {
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {"id": "call_a", "type": "function", "function": {"name": "list_cards", "arguments": "{}"}},
        {"id": "call_b", "type": "function", "function": {"name": "freeze_card", "arguments": '{"card_id": '}},
    ],
}


# ---- OpenAICompatLLM ----------------------------------------------------------------------------------------------


def test_openai_compat_request_and_response(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []
    llm = OpenAICompatLLM(
        base_url="http://llm.test/v1",
        model="test-model",
        temperature=0.0,
        max_tokens=512,
        http_client=_mock_openai(TOOL_CALL_REPLY, requests),
    )
    reply = llm.invoke([SystemMessage(content="sys"), HumanMessage(content="hi")], TOOLS)

    [request] = requests
    assert request["url"] == "http://llm.test/v1/chat/completions"
    body = request["body"]
    assert (body["model"], body["temperature"], body["max_completion_tokens"]) == ("test-model", 0.0, 512)
    assert body["tool_choice"] == "auto" and body["tools"] == TOOLS
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]

    assert [c["name"] for c in reply.tool_calls] == ["list_cards"]
    assert [c["name"] for c in reply.invalid_tool_calls] == ["freeze_card"]  # unparseable JSON is kept, not dropped
    assert reply.usage_metadata is not None
    assert (reply.usage_metadata["input_tokens"], reply.usage_metadata["output_tokens"]) == (120, 30)


def test_openai_compat_records_every_request(tmp_path: Path) -> None:
    record = tmp_path / "nested" / "llm_requests.jsonl"
    llm = OpenAICompatLLM(
        base_url="http://llm.test/v1",
        model="m",
        record_path=record,
        http_client=_mock_openai({"role": "assistant", "content": "hello"}, []),
    )
    history = [SystemMessage(content="sys"), HumanMessage(content="hi")]
    threads = [threading.Thread(target=llm.invoke, args=(history, TOOLS)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = record.read_text().splitlines()
    assert len(lines) == 8
    for line in lines:
        assert json.loads(line) == {"messages": to_openai_messages(history), "tools": TOOLS}


def test_bound_model_is_cached_per_tool_list() -> None:
    llm = OpenAICompatLLM(base_url="http://llm.test/v1", model="m")
    assert llm._bind(TOOLS) is llm._bind(list(TOOLS))
    assert llm._bind(TOOLS[:2]) is not llm._bind(TOOLS)


def test_from_settings() -> None:
    settings = Settings(llm_base_url="http://x/v1", llm_model="qwen", llm_temperature=0.2, llm_max_tokens=64)
    chat = OpenAICompatLLM.from_settings(settings).chat
    assert (chat.model_name, chat.temperature, chat.max_tokens, chat.max_retries) == ("qwen", 0.2, 64, 2)
    assert str(chat.openai_api_base) == "http://x/v1"


# ---- message conversion -------------------------------------------------------------------------------------------


def test_to_openai_messages_matches_what_chatopenai_sends() -> None:
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="hi"),
        AIMessage(
            content="",
            tool_calls=[{"name": "list_cards", "args": {}, "id": "c1"}],
            invalid_tool_calls=[invalid_tool_call(name="freeze_card", args='{"card_id": ', id="c2", error="bad")],
        ),
        ToolMessage(content='{"cards": []}', tool_call_id="c1"),
        ToolMessage(content='{"error": "INVALID_ARGS"}', tool_call_id="c2"),
        AIMessage(content="Done."),
    ]
    ours = to_openai_messages(messages)
    theirs = [_convert_message_to_dict(m) for m in messages]
    for mine, wire in zip(ours, theirs, strict=True):
        assert mine == {k: v for k, v in wire.items() if k in mine or v is not None}
    assert [c["id"] for c in ours[2]["tool_calls"]] == ["c1", "c2"]  # malformed call is kept


# ---- deterministic stand-ins --------------------------------------------------------------------------------------


def test_replay_llm_returns_steps_in_order_then_closes() -> None:
    llm = ReplayLLM(
        [{"tool_calls": [{"name": "list_cards"}, {"name": "list_disputes", "args": {}}]}, {"content": "ok"}]
    )
    first = llm.invoke([HumanMessage(content="a")], TOOLS)
    assert [(c["name"], c["args"], c["id"]) for c in first.tool_calls] == [
        ("list_cards", {}, "call_1"),
        ("list_disputes", {}, "call_2"),
    ]
    assert llm.invoke([], TOOLS).content == "ok"
    assert llm.invoke([], TOOLS).content == ReplayLLM.CLOSING
    assert len(llm.calls) == 3 and llm.calls[0][0].content == "a"


def test_scripted_llm_applies_its_policy() -> None:
    llm = ScriptedLLM(lambda messages: AIMessage(content=f"seen {len(messages)}"))
    assert llm.invoke([HumanMessage(content="a"), HumanMessage(content="b")], TOOLS).content == "seen 2"
    assert len(llm.calls) == 1


@pytest.mark.parametrize("cls", [ReplayLLM, ScriptedLLM])
def test_stand_ins_document_that_they_never_produce_metrics(cls: type) -> None:
    assert "never produces reported metrics" in (cls.__doc__ or "") or "red team only" in (cls.__doc__ or "")
