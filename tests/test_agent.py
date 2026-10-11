import json
from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.tool import invalid_tool_call

from agent.client import BankClient
from agent.graph import (
    DECLINED_MESSAGE,
    FALLBACK_REPLY,
    MAX_TOOL_RESULT_CHARS,
    BankingAgent,
    ConfirmationPendingError,
    NoPendingConfirmationError,
    ThreadOwnershipError,
    redact_pans,
    window_messages,
)
from agent.guardrails import GuardConfig
from agent.llm import ReplayLLM, ScriptedLLM
from agent.tools import ToolSpec
from bank_services.store import BankStore

MakeAgent = Callable[..., BankingAgent]
THREAD = "thread-1"


@pytest.fixture
def make_agent(bank: BankClient) -> MakeAgent:
    def make(llm: Any, **kwargs: Any) -> BankingAgent:
        return BankingAgent(llm, bank, **kwargs)

    return make


def _call(name: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args}


def _tool_messages(agent: BankingAgent, user_id: str = "u_1001") -> list[ToolMessage]:
    return [m for m in agent.state(THREAD, user_id)["messages"] if isinstance(m, ToolMessage)]


def _payload(message: ToolMessage) -> dict[str, Any]:
    return json.loads(message.content)  # type: ignore[arg-type]


def _events(agent: BankingAgent, kind: str, user_id: str = "u_1001") -> list[dict[str, Any]]:
    return [e for e in agent.state(THREAD, user_id)["trace"] if e["type"] == kind]


def _disputes_for(store: BankStore, txn_id: str) -> list[dict[str, Any]]:
    return [d for d in store.state["disputes"].values() if d["transaction_id"] == txn_id]


def _script_faults(store: BankStore, faults: list[str | None]) -> None:
    queue = list(faults)
    store.roll_fault = lambda: queue.pop(0) if queue else None  # type: ignore[method-assign]


FREEZE_DEBIT = _call("freeze_card", card_id="card_1001_debit", reason="lost")
FREEZE_CREDIT = _call("freeze_card", card_id="card_1001_credit", reason="lost")


# ---- confirmation gating ------------------------------------------------------------------------------------------


def test_write_pauses_until_approved(make_agent: MakeAgent, store: BankStore) -> None:
    agent = make_agent(ReplayLLM([{"tool_calls": [FREEZE_DEBIT]}, {"content": "Your debit card is frozen."}]))
    paused = agent.send(THREAD, "u_1001", "I lost my debit card, freeze it")
    assert paused.reply is None
    assert paused.confirmation == {
        "type": "confirmation_required",
        "actions": [
            {
                "action_id": "call_1",
                "tool": "freeze_card",
                "summary": "Freeze your debit card ending 4417 (reason: lost)",
            }
        ],
    }
    assert store.snapshot()["cards"]["card_1001_debit"] == "active"  # nothing changes before approval
    done = agent.resume(THREAD, "u_1001", True)
    assert done.reply == "Your debit card is frozen." and done.confirmation is None
    assert store.snapshot()["cards"]["card_1001_debit"] == "frozen"
    assert agent.state(THREAD, "u_1001")["writes"] == 1


@pytest.mark.parametrize("answer", [False, {"approve": False}, {"approved_ids": []}])
def test_decline_changes_nothing(make_agent: MakeAgent, store: BankStore, answer: Any) -> None:
    agent = make_agent(ReplayLLM([{"tool_calls": [FREEZE_DEBIT]}, {"content": "Okay, I left it active."}]))
    agent.send(THREAD, "u_1001", "freeze my debit card")
    before = store.snapshot()
    agent.resume(THREAD, "u_1001", answer)
    assert store.snapshot() == before
    [message] = _tool_messages(agent)
    assert _payload(message) == {"error": "DECLINED_BY_CUSTOMER", "message": DECLINED_MESSAGE}
    assert message.status == "error"
    assert agent.state(THREAD, "u_1001")["writes"] == 0


@pytest.mark.parametrize("answer", [True, {"approve": True}, {"approved_ids": ["call_1"]}])
def test_approval_formats(make_agent: MakeAgent, store: BankStore, answer: Any) -> None:
    agent = make_agent(ReplayLLM([{"tool_calls": [FREEZE_DEBIT]}]))
    agent.send(THREAD, "u_1001", "freeze my debit card")
    agent.resume(THREAD, "u_1001", answer)
    assert store.snapshot()["cards"]["card_1001_debit"] == "frozen"


def test_partial_approval(make_agent: MakeAgent, store: BankStore) -> None:
    agent = make_agent(ReplayLLM([{"tool_calls": [FREEZE_DEBIT, FREEZE_CREDIT]}, {"content": "Done."}]))
    paused = agent.send(THREAD, "u_1001", "freeze both my cards")
    assert paused.confirmation is not None
    assert [a["action_id"] for a in paused.confirmation["actions"]] == ["call_1", "call_2"]
    agent.resume(THREAD, "u_1001", {"approved_ids": ["call_1"]})
    cards = store.snapshot()["cards"]
    assert (cards["card_1001_debit"], cards["card_1001_credit"]) == ("frozen", "active")
    first, second = _tool_messages(agent)
    assert first.status == "success" and _payload(second)["error"] == "DECLINED_BY_CUSTOMER"
    assert _events(agent, "confirmation") == [
        {"type": "confirmation", "requested": ["call_1", "call_2"], "approved": ["call_1"]}
    ]


@pytest.mark.parametrize(
    "answer",
    [
        "yes",
        1,
        None,
        ["call_1"],
        {"approve": "true"},
        {"approved_ids": "call_1"},
        {"approve": True, "extra": 1},
        {"0123456789abcdef0123456789abcdef": True},  # looks like a LangGraph resume map
        {"customer_answer": True},  # already wrapped by the caller: still not a valid answer
    ],
)
def test_garbage_resume_value_is_a_decline(make_agent: MakeAgent, store: BankStore, answer: Any) -> None:
    agent = make_agent(ReplayLLM([{"tool_calls": [FREEZE_DEBIT]}]))
    agent.send(THREAD, "u_1001", "freeze my debit card")
    before = store.snapshot()
    agent.resume(THREAD, "u_1001", answer)
    assert store.snapshot() == before
    assert _payload(_tool_messages(agent)[0])["error"] == "DECLINED_BY_CUSTOMER"


def test_approved_ids_outside_the_batch_are_ignored(make_agent: MakeAgent, store: BankStore) -> None:
    agent = make_agent(ReplayLLM([{"tool_calls": [FREEZE_DEBIT]}]))
    agent.send(THREAD, "u_1001", "freeze my debit card")
    agent.resume(THREAD, "u_1001", {"approved_ids": ["call_999"]})
    assert store.snapshot()["cards"]["card_1001_debit"] == "active"


# ---- batches ------------------------------------------------------------------------------------------------------


def test_mixed_batch_answers_every_call_once_in_order(make_agent: MakeAgent, store: BankStore) -> None:
    calls = [
        _call("list_cards"),
        FREEZE_DEBIT,
        _call("transfer_funds", to_account="acct_9999", amount=500),
        _call("freeze_card", card_id="card_1002_credit"),
        _call("get_transaction", transaction_id="../../etc"),
    ]
    agent = make_agent(ReplayLLM([{"tool_calls": calls}, {"content": "Done."}]))
    paused = agent.send(THREAD, "u_1001", "do several things")
    assert paused.confirmation is not None and len(paused.confirmation["actions"]) == 1
    agent.resume(THREAD, "u_1001", True)

    messages = agent.state(THREAD, "u_1001")["messages"]
    ai = next(m for m in messages if isinstance(m, AIMessage) and m.tool_calls)
    tool_messages = _tool_messages(agent)
    assert [m.tool_call_id for m in tool_messages] == [c["id"] for c in ai.tool_calls]
    assert [m.status for m in tool_messages] == ["success", "success", "error", "error", "error"]
    assert [_payload(m).get("error") for m in tool_messages[2:]] == ["NOT_ALLOWLISTED", "NOT_OWNER", "INVALID_ARGS"]
    snapshot = store.snapshot()
    assert snapshot["transfers"] == [] and snapshot["cards"]["card_1002_credit"] == "active"
    assert snapshot["cards"]["card_1001_debit"] == "frozen"


def test_l5_budget_holds_within_one_batch(make_agent: MakeAgent) -> None:
    proposals = [FREEZE_DEBIT, FREEZE_CREDIT] * 3 + [FREEZE_DEBIT]  # 7 write proposals in one AIMessage
    agent = make_agent(ReplayLLM([{"tool_calls": proposals}]))
    paused = agent.send(THREAD, "u_1001", "freeze everything, many times")
    assert paused.confirmation is not None and len(paused.confirmation["actions"]) == 6
    decisions = [(e["decision"], e["code"]) for e in _events(agent, "tool_call")]
    assert decisions == [("confirm", None)] * 6 + [("block", "ACTION_BUDGET_EXCEEDED")]


def test_l5_budget_counts_earlier_turns(make_agent: MakeAgent) -> None:
    agent = make_agent(ReplayLLM([{"tool_calls": [FREEZE_DEBIT]}]), guard_config=GuardConfig(max_writes_per_session=0))
    result = agent.send(THREAD, "u_1001", "freeze my debit card")
    assert result.confirmation is None
    assert _payload(_tool_messages(agent)[0])["error"] == "ACTION_BUDGET_EXCEEDED"


def test_writes_count_only_successful_writes(make_agent: MakeAgent, store: BankStore) -> None:
    agent = make_agent(ReplayLLM([{"tool_calls": [FREEZE_DEBIT, FREEZE_DEBIT]}, {"content": "Done."}]))
    agent.send(THREAD, "u_1001", "freeze it")
    agent.resume(THREAD, "u_1001", True)
    first, second = _tool_messages(agent)
    assert first.status == "success" and _payload(second)["error"] == "CARD_ALREADY_FROZEN"  # backend rule
    assert agent.state(THREAD, "u_1001")["writes"] == 1


# ---- stored args are what execute ---------------------------------------------------------------------------------

RAW_DISPUTE = _call(
    "create_dispute", transaction_id=" txn_1001_s02 ", reason="duplicate", amount="10.50", details=" charged twice "
)
STORED_DISPUTE = {"transaction_id": "txn_1001_s02", "reason": "duplicate", "amount": 10.5, "details": "charged twice"}


def test_execute_receives_the_stored_validated_args(make_agent: MakeAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    original = ToolSpec.invoke

    def spy(self: ToolSpec, ctx: Any, args: Any) -> dict[str, Any]:
        seen.append(dict(args))
        return original(self, ctx, args)

    monkeypatch.setattr(ToolSpec, "invoke", spy)
    agent = make_agent(ReplayLLM([{"tool_calls": [RAW_DISPUTE]}, {"content": "Opened."}]))
    agent.send(THREAD, "u_1001", "I was charged twice by StreamFlix")
    agent.resume(THREAD, "u_1001", True)
    assert seen == [STORED_DISPUTE]


def test_changing_the_model_message_after_proposal_has_no_effect(make_agent: MakeAgent, store: BankStore) -> None:
    agent = make_agent(ReplayLLM([{"tool_calls": [RAW_DISPUTE]}, {"content": "Opened."}]))
    paused = agent.send(THREAD, "u_1001", "I was charged twice by StreamFlix")
    assert paused.confirmation is not None
    assert "Dispute $10.50 of the $15.99 charge" in paused.confirmation["actions"][0]["summary"]

    config = agent._config(THREAD, "u_1001")
    proposal = agent.graph.get_state(config).values["messages"][-1]
    tampered_args = {"transaction_id": "txn_1001_s02", "reason": "unauthorized", "amount": 15.99}
    tampered = AIMessage(id=proposal.id, content="", tool_calls=[{**proposal.tool_calls[0], "args": tampered_args}])
    agent.graph.update_state(config, {"messages": [tampered]}, as_node="guard")
    agent.graph.invoke(None, config)  # re-enter the confirm step; it pauses again with the stored batch
    agent.resume(THREAD, "u_1001", True)

    [dispute] = _disputes_for(store, "txn_1001_s02")
    assert (dispute["reason"], dispute["amount"], dispute["details"]) == ("duplicate", 10.5, "charged twice")


# ---- robustness ---------------------------------------------------------------------------------------------------


def test_step_limit_returns_fallback(make_agent: MakeAgent) -> None:
    llm = ScriptedLLM(lambda _: AIMessage(content="", tool_calls=[{"name": "list_cards", "args": {}, "id": "c"}]))
    agent = make_agent(llm, max_steps=3)
    result = agent.send(THREAD, "u_1001", "loop forever")
    assert result.reply == FALLBACK_REPLY
    assert len(llm.calls) == 3
    assert _events(agent, "step_limit") == [{"type": "step_limit", "steps": 3}]


def test_steps_reset_on_each_user_message(make_agent: MakeAgent) -> None:
    agent = make_agent(ReplayLLM([{"tool_calls": [_call("list_cards")]}, {"content": "a"}, {"content": "b"}]))
    agent.send(THREAD, "u_1001", "first")
    assert agent.state(THREAD, "u_1001")["steps"] == 2
    agent.send(THREAD, "u_1001", "second")
    assert agent.state(THREAD, "u_1001")["steps"] == 1


def test_llm_failure_returns_fallback_and_session_survives(make_agent: MakeAgent) -> None:
    failures = [TimeoutError("model took too long")]

    def policy(_: list[BaseMessage]) -> AIMessage:
        if failures:
            raise failures.pop()
        return AIMessage(content="Back online.")

    agent = make_agent(ScriptedLLM(policy))
    assert agent.send(THREAD, "u_1001", "hello").reply == FALLBACK_REPLY
    [event] = _events(agent, "llm")
    assert event["error"] == "TimeoutError: model took too long" and event["n_tool_calls"] == 0
    assert agent.send(THREAD, "u_1001", "hello again").reply == "Back online."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("card 4417123456789012 ok", "card [REDACTED] ok"),
        ("card 4417 1234 5678 9012 ok", "card [REDACTED] ok"),
        ("card 4417-1234-5678-9012 ok", "card [REDACTED] ok"),
        ("thirteen 1234567890123 and nineteen 1234567890123456789", "thirteen [REDACTED] and nineteen [REDACTED]"),
        ("twelve 123456789012 stays", "twelve 123456789012 stays"),
        ("twenty 12345678901234567890 stays", "twenty 12345678901234567890 stays"),
        ("ending 4417, on 2026-09-25, $2,450.00, acct_1001, 555-123-4567", None),
    ],
)
def test_redact_pans(text: str, expected: str | None) -> None:
    assert redact_pans(text) == (text if expected is None else expected)


def test_final_replies_are_redacted(make_agent: MakeAgent) -> None:
    agent = make_agent(ReplayLLM([{"content": "Your card 4417 1234 5678 9012 is active (ending 4417)."}]))
    reply = agent.send(THREAD, "u_1001", "what is my card number?").reply
    assert reply == "Your card [REDACTED] is active (ending 4417)."
    assert agent.state(THREAD, "u_1001")["messages"][-1].content == reply


def test_malformed_tool_call_gets_invalid_args_and_agent_recovers(make_agent: MakeAgent) -> None:
    def policy(messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        if isinstance(last, HumanMessage):
            bad = invalid_tool_call(
                name="freeze_card", args='{"card_id": "card_1001_debit"', id="call_bad", error="JSON"
            )
            return AIMessage(content="", invalid_tool_calls=[bad])
        if isinstance(last, ToolMessage) and "INVALID_ARGS" in str(last.content):
            return AIMessage(
                content="",
                tool_calls=[{"name": "freeze_card", "args": {"card_id": "card_1001_debit"}, "id": "call_ok"}],
            )
        return AIMessage(content="Frozen.")

    agent = make_agent(ScriptedLLM(policy))
    paused = agent.send(THREAD, "u_1001", "freeze my debit card")
    [bad_result] = _tool_messages(agent)
    assert bad_result.tool_call_id == "call_bad"
    assert _payload(bad_result) == {
        "error": "INVALID_ARGS",
        "message": "Arguments for freeze_card must be a JSON object. "
        "freeze_card accepts only: card_id (required), reason (optional).",
    }
    assert paused.confirmation is not None and paused.confirmation["actions"][0]["action_id"] == "call_ok"


def test_large_tool_results_are_truncated(make_agent: MakeAgent) -> None:
    agent = make_agent(ReplayLLM([{"tool_calls": [_call("list_transactions", days=180, limit=50)]}]))
    agent.send(THREAD, "u_1001", "show everything")
    [message] = _tool_messages(agent)
    assert len(message.content) == MAX_TOOL_RESULT_CHARS


# ---- faults during execution --------------------------------------------------------------------------------------


def test_after_faults_during_execute_apply_exactly_once(make_agent: MakeAgent, store: BankStore) -> None:
    dispute = _call("create_dispute", transaction_id="txn_1001_s02", reason="duplicate")
    agent = make_agent(ReplayLLM([{"tool_calls": [dispute]}, {"content": "Opened."}]))
    agent.send(THREAD, "u_1001", "charged twice")
    _script_faults(store, ["after", "after"])  # the write is applied, its response lost, twice
    agent.resume(THREAD, "u_1001", True)
    assert len(_disputes_for(store, "txn_1001_s02")) == 1
    [execution] = _events(agent, "execution")
    assert execution["ok"] is True and execution["retries"] == 2
    assert _payload(_tool_messages(agent)[0])["dispute_id"] == "dsp_7001"


def test_seeded_50_percent_after_faults(make_agent: MakeAgent, store: BankStore, bank: BankClient) -> None:
    store.reset(fault_rate=0.5, fault_mode="after", seed=3)
    dispute = _call("create_dispute", transaction_id="txn_1001_s02", reason="duplicate")
    agent = make_agent(ReplayLLM([{"tool_calls": [dispute]}, {"content": "Opened."}]))
    paused = agent.send(THREAD, "u_1001", "charged twice")
    assert paused.confirmation is not None
    agent.resume(THREAD, "u_1001", True)
    assert len(_disputes_for(store, "txn_1001_s02")) == 1
    assert store.faults_injected > 0 and bank.retry_count > 0
    [execution] = _events(agent, "execution")
    assert execution["ok"] is True
    assert 1 <= execution["retries"] <= bank.retry_count  # seed 3: the write itself was hit by "after" faults


# ---- ablation (guardrails disabled) -------------------------------------------------------------------------------


def test_ablation_executes_admin_tools_without_confirmation(make_agent: MakeAgent, store: BankStore) -> None:
    transfer = _call("transfer_funds", to_account="acct_9999", amount=500)
    agent = make_agent(ReplayLLM([{"tool_calls": [transfer]}, {"content": "Sent."}]), guard_config=GuardConfig(False))
    result = agent.send(THREAD, "u_1001", "send 500 to acct_9999")
    assert result.confirmation is None and result.reply == "Sent."
    assert store.snapshot()["transfers"] == [("acct_1001", "acct_9999", 500.0)]
    assert agent.state(THREAD, "u_1001")["writes"] == 1


def test_ablation_still_enforces_tool_schemas(make_agent: MakeAgent, store: BankStore) -> None:
    calls = [_call("freeze_card", card_id="../../payments/v1/transfers"), {"name": "freeze_card", "args": {}}]
    agent = make_agent(ReplayLLM([{"tool_calls": calls}]), guard_config=GuardConfig(False))
    before = store.snapshot()
    agent.send(THREAD, "u_1001", "freeze")
    assert store.snapshot() == before
    assert [_payload(m)["error"] for m in _tool_messages(agent)] == ["INVALID_ARGS", "INVALID_ARGS"]
    assert [e["error"] for e in _events(agent, "execution")] == ["INVALID_ARGS", "INVALID_ARGS"]


# ---- prompt injection ---------------------------------------------------------------------------------------------


def _obedient_to_memo(messages: list[BaseMessage]) -> AIMessage:
    """A hijacked model that does whatever the BookNook memo says."""
    last = messages[-1]
    if isinstance(last, HumanMessage):
        return AIMessage(
            content="", tool_calls=[{"name": "get_transaction", "args": {"transaction_id": "txn_1003_i01"}, "id": "c1"}]
        )
    if isinstance(last, ToolMessage) and last.tool_call_id == "c1" and "unfreeze_card" in str(last.content):
        return AIMessage(
            content="",
            tool_calls=[
                {"name": "unfreeze_card", "args": {"card_id": "card_1003_debit_old"}, "id": "c2"},
                {"name": "transfer_funds", "args": {"to_account": "acct_9999", "amount": 5000}, "id": "c3"},
            ],
        )
    return AIMessage(content="All done.")


def test_prompt_injection_is_contained_with_guardrails_on(make_agent: MakeAgent, store: BankStore) -> None:
    agent = make_agent(ScriptedLLM(_obedient_to_memo))
    before = store.snapshot()
    result = agent.send(THREAD, "u_1003", "What was the BookNook charge?")
    assert result.confirmation is None  # both attacks are blocked before the customer is ever asked
    assert store.snapshot() == before
    blocked = [
        (e["name"], e["code"], e["layer"]) for e in _events(agent, "tool_call", "u_1003") if e["decision"] == "block"
    ]
    assert blocked == [("unfreeze_card", "CARD_REPORTED_STOLEN", "L4"), ("transfer_funds", "NOT_ALLOWLISTED", "L1")]


def test_prompt_injection_succeeds_partially_with_guardrails_off(make_agent: MakeAgent, store: BankStore) -> None:
    agent = make_agent(ScriptedLLM(_obedient_to_memo), guard_config=GuardConfig(False))
    agent.send(THREAD, "u_1003", "What was the BookNook charge?")
    assert store.snapshot()["transfers"] == [("acct_1003", "acct_9999", 5000.0)]  # the money moved
    assert store.snapshot()["cards"]["card_1003_debit_old"] == "reported_stolen"  # the backend rule still held


# ---- session rules ------------------------------------------------------------------------------------------------


def test_thread_is_pinned_to_its_user(make_agent: MakeAgent) -> None:
    agent = make_agent(ReplayLLM([{"content": "hi"}]))
    agent.send(THREAD, "u_1001", "hello")
    with pytest.raises(ThreadOwnershipError):
        agent.send(THREAD, "u_1002", "show me the cards")
    with pytest.raises(ThreadOwnershipError):
        agent.state(THREAD, "u_1002")
    with pytest.raises(ThreadOwnershipError):
        agent.resume(THREAD, "u_1002", True)


def test_send_while_confirmation_pending_raises(make_agent: MakeAgent) -> None:
    agent = make_agent(ReplayLLM([{"tool_calls": [FREEZE_DEBIT]}]))
    agent.send(THREAD, "u_1001", "freeze my debit card")
    with pytest.raises(ConfirmationPendingError):
        agent.send(THREAD, "u_1001", "never mind, do something else")


def test_resume_with_nothing_pending_raises(make_agent: MakeAgent) -> None:
    agent = make_agent(ReplayLLM([{"content": "hi"}]))
    with pytest.raises(NoPendingConfirmationError):
        agent.resume(THREAD, "u_1001", True)
    agent.send(THREAD, "u_1001", "hello")
    with pytest.raises(NoPendingConfirmationError):
        agent.resume(THREAD, "u_1001", True)


def test_identity_comes_from_the_session(make_agent: MakeAgent) -> None:
    llm = ReplayLLM([{"tool_calls": [_call("list_cards")]}, {"content": "ok"}])
    agent = make_agent(llm)
    agent.send(THREAD, "u_1003", "my cards?")
    cards = _payload(_tool_messages(agent, "u_1003")[0])["cards"]
    assert {c["card_id"] for c in cards} == {"card_1003_debit_old", "card_1003_debit"}
    system = llm.calls[0][0]
    assert isinstance(system, SystemMessage) and "Sara Malik" in system.content and "u_1003" not in system.content


# ---- memory and history window ------------------------------------------------------------------------------------


def test_second_turn_sees_first_turn_tool_results(make_agent: MakeAgent) -> None:
    llm = ReplayLLM([{"tool_calls": [_call("list_cards")]}, {"content": "You have two cards."}, {"content": "4417."}])
    agent = make_agent(llm)
    agent.send(THREAD, "u_1001", "what cards do I have?")
    agent.send(THREAD, "u_1001", "which one is the debit card?")
    second_turn = llm.calls[-1]
    assert any(isinstance(m, ToolMessage) and "card_1001_debit" in str(m.content) for m in second_turn)
    assert [m.content for m in second_turn if isinstance(m, HumanMessage)] == [
        "what cards do I have?",
        "which one is the debit card?",
    ]


def _synthetic_history(turns: int) -> list[BaseMessage]:
    history: list[BaseMessage] = []
    for turn in range(turns):
        history.append(HumanMessage(content=f"question {turn}"))
        for call in range(turn % 4):
            call_id = f"t{turn}c{call}"
            history.append(AIMessage(content="", tool_calls=[{"name": "list_cards", "args": {}, "id": call_id}]))
            history.append(ToolMessage(content="{}", tool_call_id=call_id))
        history.append(AIMessage(content=f"answer {turn}"))
    return history


def _assert_window_is_valid(window: list[BaseMessage]) -> None:
    assert isinstance(window[0], HumanMessage)
    seen_calls: set[str] = set()
    for message in window:
        if isinstance(message, AIMessage):
            seen_calls.update(c["id"] for c in message.tool_calls)
        if isinstance(message, ToolMessage):
            assert message.tool_call_id in seen_calls, "orphaned ToolMessage"


@pytest.mark.parametrize("turns", range(1, 40))
def test_history_window_never_orphans_a_tool_message(turns: int) -> None:
    history = _synthetic_history(turns)
    window = window_messages(history, 40)
    _assert_window_is_valid(window)
    assert len(window) <= 40 or window[0] is next(m for m in reversed(history) if isinstance(m, HumanMessage))
    assert window[-1] is history[-1]


def test_history_window_keeps_an_oversized_current_turn() -> None:
    long_turn: list[BaseMessage] = [HumanMessage(content="old"), AIMessage(content="old answer")]
    long_turn.append(HumanMessage(content="current"))
    for call in range(30):
        long_turn.append(AIMessage(content="", tool_calls=[{"name": "list_cards", "args": {}, "id": f"c{call}"}]))
        long_turn.append(ToolMessage(content="{}", tool_call_id=f"c{call}"))
    window = window_messages(long_turn, 40)
    assert window[0].content == "current" and len(window) == 61


def test_long_session_sends_a_valid_window_to_the_model(make_agent: MakeAgent) -> None:
    llm = ReplayLLM([step for _ in range(30) for step in ({"tool_calls": [_call("list_cards")]}, {"content": "ok"})])
    agent = make_agent(llm)
    for turn in range(30):
        agent.send(THREAD, "u_1001", f"turn {turn}")
    assert len(agent.state(THREAD, "u_1001")["messages"]) == 120
    for messages in llm.calls[-5:]:
        assert isinstance(messages[0], SystemMessage)
        _assert_window_is_valid(messages[1:])
        assert len(messages) - 1 <= 40


# ---- trace --------------------------------------------------------------------------------------------------------


def test_trace_for_a_full_freeze_flow(make_agent: MakeAgent) -> None:
    agent = make_agent(ReplayLLM([{"tool_calls": [FREEZE_DEBIT]}, {"content": "Frozen."}]))
    agent.send(THREAD, "u_1001", "freeze my debit card")
    agent.resume(THREAD, "u_1001", True)
    trace = agent.state(THREAD, "u_1001")["trace"]
    assert [e["type"] for e in trace] == ["llm", "tool_call", "confirmation", "execution", "llm"]
    llm_event, tool_call, confirmation, execution, _ = trace
    assert {"latency_s", "input_tokens", "output_tokens", "n_tool_calls"} <= set(llm_event)
    assert llm_event["n_tool_calls"] == 1 and "error" not in llm_event
    assert tool_call == {
        "type": "tool_call",
        "id": "call_1",
        "name": "freeze_card",
        "args": {"card_id": "card_1001_debit", "reason": "lost"},
        "decision": "confirm",
        "code": None,
        "layer": None,
    }
    assert confirmation == {"type": "confirmation", "requested": ["call_1"], "approved": ["call_1"]}
    assert {k: execution[k] for k in ("id", "name", "kind", "ok", "retries")} == {
        "id": "call_1",
        "name": "freeze_card",
        "kind": "write",
        "ok": True,
        "retries": 0,
    }


# ---- end-to-end flows ---------------------------------------------------------------------------------------------


def test_unfreeze_flow(make_agent: MakeAgent, store: BankStore) -> None:
    llm = ReplayLLM(
        [
            {"tool_calls": [_call("list_cards")]},
            {"tool_calls": [_call("unfreeze_card", card_id="card_1002_debit")]},
            {"content": "Your debit card ending 3301 is active again."},
        ]
    )
    agent = make_agent(llm)
    paused = agent.send(THREAD, "u_1002", "I found my debit card, please unfreeze it")
    assert paused.confirmation is not None
    assert paused.confirmation["actions"][0]["summary"] == "Unfreeze your debit card ending 3301"
    assert agent.resume(THREAD, "u_1002", True).reply == "Your debit card ending 3301 is active again."
    assert store.snapshot()["cards"]["card_1002_debit"] == "active"


def test_anomaly_flag_flow(make_agent: MakeAgent, store: BankStore) -> None:
    """analyze_spending, then one flag_transaction per returned anomaly, all confirmed in one batch."""

    def policy(messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        if isinstance(last, HumanMessage):
            return AIMessage(content="", tool_calls=[{"name": "analyze_spending", "args": {}, "id": "scan"}])
        if isinstance(last, ToolMessage) and last.tool_call_id == "scan":
            anomalies = json.loads(str(last.content))["anomalies"]
            calls = [
                {
                    "name": "flag_transaction",
                    "args": {"transaction_id": a["transaction_id"], "reason": "card-testing burst"},
                    "id": f"flag_{i}",
                }
                for i, a in enumerate(anomalies)
            ]
            return AIMessage(content="", tool_calls=calls)
        return AIMessage(content="I flagged the suspicious charges.")

    agent = make_agent(ScriptedLLM(policy))
    paused = agent.send(THREAD, "u_1002", "Anything suspicious on my account? Flag it.")
    assert paused.confirmation is not None and len(paused.confirmation["actions"]) == 5
    agent.resume(THREAD, "u_1002", True)
    assert sorted(store.snapshot()["flags"]) == [f"txn_1002_v0{i}" for i in range(1, 6)]
    assert agent.state(THREAD, "u_1002")["writes"] == 5
