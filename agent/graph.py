"""LangGraph banking agent with guardrails and human confirmation of every state-changing action.

    START -> agent --(tool calls?)--> guard --(pending writes?)--> confirm (interrupt) --> execute --> agent
               \\--(no tool calls)--> END       \\--(none pending)----------------------> execute

Every proposed tool call goes through the guardrail engine (L1-L5). Writes that pass are paused with ``interrupt()``
(L6) and, once approved, execute with the arguments validated at proposal time. Blocked or declined calls come back
to the model as error ToolMessages so it can recover. The session never crashes: model failures and step-limit hits
end the turn with a fixed fallback reply.
"""

from __future__ import annotations

import json
import operator
import re
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from agent.client import BankClient, ServiceError, retry_scope
from agent.guardrails import Decision, GuardConfig, GuardrailEngine
from agent.llm import LLMBackend, message_text
from agent.prompts import SIMULATED_TODAY, system_prompt
from agent.tools import AGENT_ALLOWLIST, REGISTRY, Ctx, openai_tools

FALLBACK_REPLY = (
    "I couldn't complete that safely, so I've stopped here. A human agent can follow up with you about this request."
)
DECLINED_MESSAGE = "The customer declined. The action was NOT performed. Do not retry."
RESUME_KEY = "customer_answer"
HISTORY_WINDOW = 40
MAX_TOOL_RESULT_CHARS = 6000
RECURSION_LIMIT = 60
PAN_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")  # 13-19 digits, optional single space/dash between

_STATUS_FOR_DECISION = {Decision.ALLOW: "approved", Decision.CONFIRM: "pending", Decision.BLOCK: "blocked"}


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    batch: list[dict[str, Any]]  # current tool-call batch with per-call status
    writes: int  # successful state-changing actions this session
    steps: int  # LLM calls this turn; reset on each new user message
    trace: Annotated[list[dict[str, Any]], operator.add]  # append-only event log


# ---- helpers ------------------------------------------------------------------------------------------------------


def redact_pans(text: str) -> str:
    return PAN_PATTERN.sub("[REDACTED]", text)


def window_messages(messages: Sequence[BaseMessage], size: int = HISTORY_WINDOW) -> list[BaseMessage]:
    """The last ``size`` messages, starting at a HumanMessage so no ToolMessage is cut off from its AIMessage.

    If the current turn alone is longer than ``size``, the whole current turn is kept.
    """
    humans = [i for i, message in enumerate(messages) if isinstance(message, HumanMessage)]
    if not humans:
        return list(messages)
    cutoff = max(0, len(messages) - size)
    start = next((i for i in humans if i >= cutoff), humans[-1])
    return list(messages[start:])


def proposed_calls(message: AIMessage) -> list[dict[str, Any]]:
    """Valid and malformed tool calls in the order ChatOpenAI sends them back to the API.

    Malformed calls keep their raw (unparseable) argument string, which fails L2 validation as INVALID_ARGS.
    """
    calls = [{"id": c["id"], "name": c["name"], "args": c["args"]} for c in message.tool_calls]
    calls += [{"id": c["id"], "name": c.get("name"), "args": c.get("args")} for c in message.invalid_tool_calls]
    return calls


def _error_content(code: str | None, message: str) -> str:
    return json.dumps({"error": code or "ERROR", "message": message}, ensure_ascii=False)


def _normalize_reply(reply: AIMessage) -> AIMessage:
    """Give every tool call an id, and redact card numbers from final text replies."""
    update: dict[str, Any] = {}
    if any(not c.get("id") for c in reply.tool_calls):
        update["tool_calls"] = [{**c, "id": c.get("id") or f"call_{uuid.uuid4().hex[:12]}"} for c in reply.tool_calls]
    if any(not c.get("id") for c in reply.invalid_tool_calls):
        update["invalid_tool_calls"] = [
            {**c, "id": c.get("id") or f"call_{uuid.uuid4().hex[:12]}"} for c in reply.invalid_tool_calls
        ]
    if not reply.tool_calls and not reply.invalid_tool_calls:
        text = message_text(reply.content)
        redacted = redact_pans(text)
        if redacted != reply.content:
            update["content"] = redacted
    return reply.model_copy(update=update) if update else reply


def _unwrap_answer(resume_value: Any) -> Any:
    """The facade wraps every answer in ``{RESUME_KEY: answer}``.

    LangGraph 1.2 crashes on ``Command(resume=None)``, and it treats a dict whose keys look like interrupt-id hashes
    as a resume map, so raw customer input is never passed to it directly.
    """
    if isinstance(resume_value, Mapping) and set(resume_value) == {RESUME_KEY}:
        return resume_value[RESUME_KEY]
    return resume_value


def _approved_ids(answer: Any, pending_ids: list[str]) -> set[str]:
    """Parse a confirmation answer. Anything unrecognised is a decline (fail safe)."""
    if isinstance(answer, bool):
        return set(pending_ids) if answer else set()
    if isinstance(answer, Mapping):
        if set(answer) == {"approve"} and isinstance(answer["approve"], bool):
            return set(pending_ids) if answer["approve"] else set()
        ids = answer.get("approved_ids")
        if set(answer) == {"approved_ids"} and isinstance(ids, list | tuple) and all(isinstance(i, str) for i in ids):
            return set(ids) & set(pending_ids)
    return set()


# ---- graph --------------------------------------------------------------------------------------------------------


def build_graph(
    llm: LLMBackend,
    client: BankClient,
    guard_config: GuardConfig | None = None,
    max_steps: int = 10,
    today: date = SIMULATED_TODAY,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    engine = GuardrailEngine(guard_config)
    # The model is offered the same tools with guardrails on or off, so an ablation changes enforcement only.
    tools = openai_tools(AGENT_ALLOWLIST)

    def agent(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        steps = state.get("steps", 0)
        if steps >= max_steps:
            return {"messages": [AIMessage(content=FALLBACK_REPLY)], "trace": [{"type": "step_limit", "steps": steps}]}
        customer_name = config["configurable"].get("customer_name") or "the customer"
        messages = [SystemMessage(content=system_prompt(customer_name, today)), *window_messages(state["messages"])]
        started = time.perf_counter()
        try:
            reply = _normalize_reply(llm.invoke(messages, tools))
        except Exception as exc:  # timeouts, connection errors, bad responses: never crash the session
            event = {
                "type": "llm",
                "latency_s": time.perf_counter() - started,
                "input_tokens": 0,
                "output_tokens": 0,
                "n_tool_calls": 0,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }
            return {"messages": [AIMessage(content=FALLBACK_REPLY)], "steps": steps + 1, "trace": [event]}
        usage = reply.usage_metadata or {}
        event = {
            "type": "llm",
            "latency_s": time.perf_counter() - started,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "n_tool_calls": len(reply.tool_calls) + len(reply.invalid_tool_calls),
        }
        return {"messages": [reply], "steps": steps + 1, "trace": [event]}

    def route_after_agent(state: AgentState) -> Literal["guard", "__end__"]:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and (last.tool_calls or last.invalid_tool_calls):
            return "guard"
        return END

    def guard(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        ctx = Ctx(config["configurable"]["user_id"], client)
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        writes = state.get("writes", 0)
        batch: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        pending = 0
        for call in proposed_calls(last):
            # Count writes already pending in this batch so one batch cannot exceed the L5 budget.
            result = engine.check(call["name"], call["args"], ctx, writes + pending)
            status = _STATUS_FOR_DECISION[result.decision]
            pending += status == "pending"
            batch.append(
                {
                    "id": call["id"],
                    "name": call["name"],
                    "args": call["args"] if result.decision == Decision.BLOCK else result.args,
                    "status": status,
                    "code": result.code,
                    "layer": result.layer,
                    "message": result.message,
                    "summary": result.summary,
                }
            )
            events.append(
                {
                    "type": "tool_call",
                    "id": call["id"],
                    "name": call["name"],
                    "args": call["args"],
                    "decision": str(result.decision),
                    "code": result.code,
                    "layer": result.layer,
                }
            )
        return {"batch": batch, "trace": events}

    def route_after_guard(state: AgentState) -> Literal["confirm", "execute"]:
        return "confirm" if any(entry["status"] == "pending" for entry in state["batch"]) else "execute"

    def confirm(state: AgentState) -> dict[str, Any]:
        pending = [entry for entry in state["batch"] if entry["status"] == "pending"]
        resume_value = interrupt(
            {
                "type": "confirmation_required",
                "actions": [{"action_id": e["id"], "tool": e["name"], "summary": e["summary"]} for e in pending],
            }
        )
        pending_ids = [entry["id"] for entry in pending]
        approved = _approved_ids(_unwrap_answer(resume_value), pending_ids)
        batch = [
            {**entry, "status": "approved" if entry["id"] in approved else "declined"}
            if entry["status"] == "pending"
            else entry
            for entry in state["batch"]
        ]
        event = {
            "type": "confirmation",
            "requested": pending_ids,
            "approved": [i for i in pending_ids if i in approved],
        }
        return {"batch": batch, "trace": [event]}

    def execute(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        ctx = Ctx(config["configurable"]["user_id"], client)
        writes = state.get("writes", 0)
        messages: list[ToolMessage] = []
        events: list[dict[str, Any]] = []
        for entry in state["batch"]:
            ok = False
            if entry["status"] == "approved":
                content, ok, event = _run(entry, ctx, client)
                events.append(event)
                if ok and event["kind"] in ("write", "admin"):
                    writes += 1
            elif entry["status"] == "blocked":
                content = _error_content(entry["code"], entry["message"])
            else:  # declined, or anything unexpected: never execute
                content = _error_content("DECLINED_BY_CUSTOMER", DECLINED_MESSAGE)
            messages.append(
                ToolMessage(
                    content=content,
                    tool_call_id=entry["id"],
                    name=entry["name"] if isinstance(entry["name"], str) else None,
                    status="success" if ok else "error",
                )
            )
        return {"messages": messages, "writes": writes, "trace": events}

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("guard", guard)
    graph.add_node("confirm", confirm)
    graph.add_node("execute", execute)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, ["guard", END])
    graph.add_conditional_edges("guard", route_after_guard, ["confirm", "execute"])
    graph.add_edge("confirm", "execute")
    graph.add_edge("execute", "agent")
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def _run(entry: dict[str, Any], ctx: Ctx, client: BankClient) -> tuple[str, bool, dict[str, Any]]:
    """Execute one approved call with its stored args. Returns (ToolMessage content, ok, trace event)."""
    name = entry["name"]
    spec = REGISTRY[name]
    started = time.perf_counter()
    error: str | None = None
    detail: str | None = None
    # Count retries for THIS execution only (contextvar-isolated), so concurrent sessions sharing a client cannot
    # corrupt each other's retry accounting (Phase 7 fix of the Phase 3 limitation).
    with retry_scope() as request_retries:
        # A non-dict or schema-invalid args can only reach here with guardrails disabled; the tool's own model still
        # rejects them (guardrails off = no L1-L6, tool schemas still enforced).
        if not isinstance(entry["args"], Mapping):
            error = "INVALID_ARGS"
            content = _error_content(error, f"Invalid arguments for {name}: arguments must be a JSON object")
        else:
            try:
                result = spec.invoke(ctx, entry["args"])
                content = json.dumps(result, ensure_ascii=False, default=str)[:MAX_TOOL_RESULT_CHARS]
            except ServiceError as exc:
                error = exc.code
                content = _error_content(exc.code, exc.message)
            except ValidationError as exc:
                error = "INVALID_ARGS"
                problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:4])
                content = _error_content(error, f"Invalid arguments for {name}: {problems}")
            except Exception as exc:
                error = "TOOL_ERROR"
                detail = type(exc).__name__
                content = _error_content(
                    error, "The tool failed unexpectedly. Check the current state with a read tool before replying."
                )
        retries = request_retries()
    event: dict[str, Any] = {
        "type": "execution",
        "id": entry["id"],
        "name": name,
        "kind": spec.kind,
        "ok": error is None,
        "latency_s": time.perf_counter() - started,
        "retries": retries,
    }
    if error is not None:
        event["error"] = error
    if detail is not None:
        event["exception"] = detail
    return content, error is None, event


# ---- facade -------------------------------------------------------------------------------------------------------


class AgentSessionError(Exception):
    """A session-level rule was violated by the caller."""


class ThreadOwnershipError(AgentSessionError):
    """The thread belongs to a different user."""


class ConfirmationPendingError(AgentSessionError):
    """A confirmation is pending; the caller must resume() first."""


class NoPendingConfirmationError(AgentSessionError):
    """resume() was called but nothing is waiting for confirmation."""


@dataclass(frozen=True)
class TurnResult:
    reply: str | None  # final assistant text, or None while a confirmation is pending
    confirmation: dict[str, Any] | None  # the interrupt payload when the turn is waiting for the customer
    latency_s: float


class BankingAgent:
    """Session facade over the graph. Enforces:

    (a) a thread is pinned to the user_id that started it;
    (b) send() is refused while a confirmation is pending;
    (c) resume() is refused when nothing is pending.

    Thread ownership is kept in memory, matching the default in-memory checkpointer.
    """

    def __init__(
        self,
        llm: LLMBackend,
        client: BankClient,
        guard_config: GuardConfig | None = None,
        max_steps: int = 10,
        today: date = SIMULATED_TODAY,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> None:
        self.client = client
        self.graph = build_graph(llm, client, guard_config, max_steps, today, checkpointer)
        self._owners: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._lock = threading.Lock()

    def send(self, thread_id: str, user_id: str, text: str) -> TurnResult:
        config = self._config(thread_id, user_id)
        if self._pending(config) is not None:
            raise ConfirmationPendingError("a confirmation is pending; call resume() first")
        started = time.perf_counter()
        self.graph.invoke({"messages": [HumanMessage(content=text)], "steps": 0}, config)
        return self._result(config, started)

    def resume(self, thread_id: str, user_id: str, answer: Any) -> TurnResult:
        config = self._config(thread_id, user_id)
        if self._pending(config) is None:
            raise NoPendingConfirmationError("nothing is waiting for confirmation")
        started = time.perf_counter()
        self.graph.invoke(Command(resume={RESUME_KEY: answer}), config)
        return self._result(config, started)

    def state(self, thread_id: str, user_id: str) -> dict[str, Any]:
        config = self._config(thread_id, user_id)
        return dict(self.graph.get_state(config).values)

    def pending(self, thread_id: str, user_id: str) -> dict[str, Any] | None:
        """The pending confirmation payload for this thread, or None (enforces thread ownership)."""
        return self._pending(self._config(thread_id, user_id))

    def _config(self, thread_id: str, user_id: str) -> RunnableConfig:
        with self._lock:
            owner = self._owners.setdefault(thread_id, user_id)
            if owner != user_id:
                raise ThreadOwnershipError(f"thread {thread_id!r} belongs to a different user")
            name = self._names.get(user_id)
        if name is None:
            name = self._customer_name(user_id)
            with self._lock:
                self._names[user_id] = name
        return {
            "configurable": {"thread_id": thread_id, "user_id": user_id, "customer_name": name},
            "recursion_limit": RECURSION_LIMIT,
        }

    def _customer_name(self, user_id: str) -> str:
        try:
            return str(self.client.get(f"/accounts/v1/customers/{user_id}")["name"])
        except Exception:
            return "the customer"

    def _pending(self, config: RunnableConfig) -> dict[str, Any] | None:
        interrupts = self.graph.get_state(config).interrupts
        return interrupts[0].value if interrupts else None

    def _result(self, config: RunnableConfig, started: float) -> TurnResult:
        latency = time.perf_counter() - started
        pending = self._pending(config)
        if pending is not None:
            return TurnResult(reply=None, confirmation=pending, latency_s=latency)
        messages = self.graph.get_state(config).values.get("messages", [])
        last = messages[-1] if messages else None
        reply = message_text(last.content) if isinstance(last, AIMessage) else None
        return TurnResult(reply=reply, confirmation=None, latency_s=latency)
