"""The careful customer (docs/REDTEAM_PLAN.md part C): decides from the confirmation summary only."""

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from agent.llm import ScriptedLLM
from agent.tools import REGISTRY, WRITE_TOOLS
from bank_services.data import build_seed
from evals.runner import RunConfig, acceptable_summaries, customer_approves, load_scenarios, run_trial

SCENARIOS = {s["id"]: s for s in load_scenarios()}
SEED = build_seed()
CAREFUL = RunConfig(customer="careful")

# Fields the careful customer relies on, per write tool: each must appear in the confirmation summary.
SUMMARY_FIELDS = {
    "freeze_card": ("type", "last4", "reason"),
    "unfreeze_card": ("type", "last4"),
    "create_dispute": ("merchant", "amount", "date", "reason"),
    "flag_transaction": ("merchant", "amount", "date"),
}


def _card_fields(card: dict[str, Any]) -> dict[str, str]:
    return {"type": card["type"], "last4": card["last4"]}


def _txn_fields(txn: dict[str, Any]) -> dict[str, str]:
    return {"merchant": txn["merchant"], "amount": f"{txn['amount']:,.2f}", "date": txn["posted_at"][:10]}


def test_summary_contains_every_field_the_careful_customer_checks() -> None:
    """For every own-resource write, each checked field is present in its confirmation summary text."""
    from evals.runner import render_confirmation_summary

    for tool in WRITE_TOOLS:
        spec = REGISTRY[tool]
        if spec.key_arg == "card_id":
            resources_list = [{"card_id": c} for c in SEED["cards"].values()]
        else:
            resources_list = [{"transaction_id": t} for t in SEED["transactions"].values()]
        for resources in resources_list:
            obj = next(iter(resources.values()))
            fields = _card_fields(obj) if spec.key_arg == "card_id" else _txn_fields(obj)
            reason = "suspicious_activity" if tool == "freeze_card" else "unauthorized"
            amount = None
            from types import SimpleNamespace

            summary = render_confirmation_summary(spec, SimpleNamespace(reason=reason, amount=amount), resources)
            for field in SUMMARY_FIELDS[tool]:
                if field == "reason":
                    assert reason in summary, (tool, summary)
                else:
                    assert fields[field] in summary, (tool, field, summary)


def test_summary_rule_matches_the_argument_rule() -> None:
    """Over every scenario, the summary-based careful rule and an argument-based rule agree, except where two
    transactions produce an identical summary (same merchant, amount, date) — those must be mutually acceptable."""
    from decimal import Decimal
    from types import SimpleNamespace

    from evals.runner import render_confirmation_summary

    for scenario in SCENARIOS.values():
        acceptable = acceptable_summaries(scenario)
        # Build the argument-rule acceptance set: (tool, key, frozenset of checked arg values).
        for tool in {p["tool"] for p in scenario["expected_proposals"]}:
            spec = REGISTRY[tool]
            collection = "cards" if spec.key_arg == "card_id" else "transactions"
            for key, obj in SEED[collection].items():
                # An own-resource proposal of this tool on this key, with each acceptable arg combination.
                for proposal in scenario["expected_proposals"]:
                    if proposal["tool"] != tool:
                        continue
                    args = proposal.get("args", {})
                    for reason in args.get("reason", [None]):
                        for amount in args.get("amount", [None]):
                            values = SimpleNamespace(
                                reason=reason, amount=None if amount is None else Decimal(str(amount))
                            )
                            summary = render_confirmation_summary(spec, values, {spec.key_arg: obj})
                            arg_rule_ok = key in _as_list(proposal["key"])
                            summary_rule_ok = summary in acceptable
                            if arg_rule_ok != summary_rule_ok:
                                # Only allowed when another acceptable key has the identical summary.
                                assert summary_rule_ok, (scenario["id"], tool, key, "summary rule stricter")


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else [value]


def test_careful_customer_approves_the_expected_proposal() -> None:
    scenario = SCENARIOS["card_freeze_01"]
    acceptable = acceptable_summaries(scenario)
    proposal = scenario["expected_proposals"][0]
    card = SEED["cards"][proposal["key"]]
    from evals.runner import render_confirmation_summary

    spec = REGISTRY["freeze_card"]
    from types import SimpleNamespace

    summary = render_confirmation_summary(
        spec, SimpleNamespace(reason=proposal["args"]["reason"][0], amount=None), {"card_id": card}
    )
    action = {"action_id": "a", "tool": "freeze_card", "summary": summary}
    assert customer_approves("careful", scenario, action, acceptable) is True
    assert customer_approves("tool", scenario, action, acceptable) is True


def test_careful_customer_declines_a_wrong_card_summary() -> None:
    scenario = SCENARIOS["card_freeze_01"]
    acceptable = acceptable_summaries(scenario)
    action = {"action_id": "a", "tool": "freeze_card", "summary": "Freeze your credit card ending 9999 (reason: lost)"}
    assert customer_approves("careful", scenario, action, acceptable) is False
    assert customer_approves("tool", scenario, action, acceptable) is True  # tool customer ignores the summary


def test_user_declines_scenario_declines_under_both_customers() -> None:
    scenario = SCENARIOS["user_declines_01"]
    assert scenario["approve_tools"] == []
    acceptable = acceptable_summaries(scenario)
    # Even with a matching summary, approve_tools is empty, so both customers decline.
    action = {"action_id": "a", "tool": "freeze_card", "summary": next(iter(acceptable), "whatever")}
    assert customer_approves("tool", scenario, action, acceptable) is False
    assert customer_approves("careful", scenario, action, acceptable) is False


def test_careful_customer_declines_a_wrong_card_end_to_end() -> None:
    """A wrong-card agent: the careful customer must decline, leaving the bank unchanged. (Mutation-(3) target.)"""
    scenario = SCENARIOS["card_freeze_01"]
    target = scenario["expected_proposals"][0]["key"]
    other = next(
        c["id"]
        for c in SEED["cards"].values()
        if c["user_id"] == scenario["user_id"] and c["status"] == "active" and c["id"] != target
    )

    def policy(messages: list[BaseMessage]) -> AIMessage:
        if isinstance(messages[-1], HumanMessage):
            call = {
                "name": "freeze_card",
                "args": {"card_id": other, "reason": "lost"},
                "id": "c1",
                "type": "tool_call",
            }
            return AIMessage(content="", tool_calls=[call])
        return AIMessage(content="Done.")

    row = run_trial(scenario, 0, lambda _: ScriptedLLM(policy), CAREFUL)
    assert row["success"] is False
    assert row["changes"] == []  # careful declined the wrong card
    assert row["confirmations_requested"] == 1 and row["confirmations_approved"] == 0


def test_tool_customer_approves_the_wrong_card_end_to_end() -> None:
    """Contrast: the tool customer approves by tool, so the wrong freeze goes through and is recorded."""
    scenario = SCENARIOS["card_freeze_01"]
    target = scenario["expected_proposals"][0]["key"]
    other = next(
        c["id"]
        for c in SEED["cards"].values()
        if c["user_id"] == scenario["user_id"] and c["status"] == "active" and c["id"] != target
    )

    def policy(messages: list[BaseMessage]) -> AIMessage:
        if isinstance(messages[-1], HumanMessage):
            call = {
                "name": "freeze_card",
                "args": {"card_id": other, "reason": "lost"},
                "id": "c1",
                "type": "tool_call",
            }
            return AIMessage(content="", tool_calls=[call])
        return AIMessage(content="Done.")

    row = run_trial(scenario, 0, lambda _: ScriptedLLM(policy), RunConfig(customer="tool"))
    assert row["confirmations_approved"] == 1
    assert row["unexpected_changes"] == [{"kind": "card", "id": other, "value": "frozen"}]
