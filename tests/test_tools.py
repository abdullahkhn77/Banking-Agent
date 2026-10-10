import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from openai.types.chat import ChatCompletionFunctionToolParam
from pydantic import BaseModel, TypeAdapter, ValidationError

from agent.tools import (
    ADMIN_TOOLS,
    AGENT_ALLOWLIST,
    MEMO_MAX_CHARS,
    READ_TOOLS,
    REGISTRY,
    WRITE_TOOLS,
    Ctx,
    ToolSpec,
    _strip_titles,
    openai_tools,
)
from bank_services.data import INJECTION_DESCRIPTION
from bank_services.store import BankStore

CUSTOMERS = ["u_1001", "u_1002", "u_1003", "u_1004", "u_1005", "u_1006"]
TXN_KEYS = {"transaction_id", "date", "amount", "merchant", "country", "card_id", "memo_untrusted"}


def _has_key(node: Any, key: str) -> bool:
    if isinstance(node, dict):
        return key in node or any(_has_key(value, key) for value in node.values())
    if isinstance(node, list):
        return any(_has_key(item, key) for item in node)
    return False


# ---- registry and allow-list --------------------------------------------------------------------------------------


def test_allowlist_contents() -> None:
    assert READ_TOOLS == {
        "get_accounts",
        "list_cards",
        "list_transactions",
        "get_transaction",
        "analyze_spending",
        "list_disputes",
        "get_dispute_status",
    }
    assert WRITE_TOOLS == {"freeze_card", "unfreeze_card", "create_dispute", "flag_transaction"}
    assert ADMIN_TOOLS == {"transfer_funds", "close_account", "update_contact_email"}
    assert AGENT_ALLOWLIST == READ_TOOLS | WRITE_TOOLS
    assert not AGENT_ALLOWLIST & ADMIN_TOOLS
    assert set(REGISTRY) == AGENT_ALLOWLIST | ADMIN_TOOLS


def test_openai_tools_exposes_only_the_allowlist() -> None:
    assert {tool["function"]["name"] for tool in openai_tools()} == AGENT_ALLOWLIST
    assert [tool["function"]["name"] for tool in openai_tools(["transfer_funds"])] == ["transfer_funds"]
    with pytest.raises(ValueError, match="unknown tool"):
        openai_tools(["execute_sql"])


@pytest.mark.parametrize("name", sorted(AGENT_ALLOWLIST))
def test_allowlisted_schema_is_valid_openai_json(name: str) -> None:
    schema = REGISTRY[name].openai_schema()
    TypeAdapter(ChatCompletionFunctionToolParam).validate_python(schema)
    assert json.loads(json.dumps(schema)) == schema
    function = schema["function"]
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", function["name"])
    assert function["description"].strip()
    parameters = function["parameters"]
    assert parameters["type"] == "object" and parameters["additionalProperties"] is False
    assert set(parameters.get("required", [])) <= set(parameters["properties"])
    assert not _has_key(schema, "title")


def test_no_tool_takes_customer_identity() -> None:
    for spec in REGISTRY.values():
        fields = set(spec.args_model.model_fields)
        assert not {f for f in fields if "user" in f or "customer" in f}, spec.name


def test_strip_titles_keeps_a_property_named_title() -> None:
    class Note(BaseModel):
        title: str

    stripped = _strip_titles(Note.model_json_schema())
    assert "title" in stripped["properties"] and "title" not in stripped["properties"]["title"]


def test_write_tools_name_their_target() -> None:
    assert {name: REGISTRY[name].key_arg for name in WRITE_TOOLS} == {
        "freeze_card": "card_id",
        "unfreeze_card": "card_id",
        "create_dispute": "transaction_id",
        "flag_transaction": "transaction_id",
    }


def test_admin_args_are_loose_and_agent_args_are_strict() -> None:
    for name, spec in REGISTRY.items():
        expected = None if name in ADMIN_TOOLS else "forbid"
        assert spec.args_model.model_config.get("extra") == expected, name


# ---- read executors -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("user_id", CUSTOMERS)
def test_read_executors_return_only_own_data(user_id: str, ctx_for: Callable[[str], Ctx], store: BankStore) -> None:
    ctx = ctx_for(user_id)
    state = store.state
    own_cards = {c["id"] for c in state["cards"].values() if c["user_id"] == user_id}
    own_account = state["customers"][user_id]["account_ids"][0]
    own_txns = {t["id"] for t in state["transactions"].values() if t["account_id"] == own_account}

    accounts = REGISTRY["get_accounts"].invoke(ctx, {})["accounts"]
    assert [a["account_id"] for a in accounts] == [own_account]

    cards = REGISTRY["list_cards"].invoke(ctx, {})["cards"]
    assert {c["card_id"] for c in cards} == own_cards
    assert all(set(c) == {"card_id", "type", "last4", "status"} for c in cards)

    txns = REGISTRY["list_transactions"].invoke(ctx, {"days": 180, "limit": 50})["transactions"]
    assert txns and {t["transaction_id"] for t in txns} <= own_txns
    assert {t["card_id"] for t in txns} <= own_cards
    assert all(set(t) == TXN_KEYS and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", t["date"]) for t in txns)

    anomalies = REGISTRY["analyze_spending"].invoke(ctx, {})["anomalies"]
    assert {a["transaction_id"] for a in anomalies} <= own_txns

    disputes = REGISTRY["list_disputes"].invoke(ctx, {})["disputes"]
    own_disputes = {d["id"] for d in state["disputes"].values() if d["user_id"] == user_id}
    assert {d["dispute_id"] for d in disputes} == own_disputes


def test_list_transactions_filters(ctx_for: Callable[[str], Ctx]) -> None:
    ctx = ctx_for("u_1002")
    by_merchant = REGISTRY["list_transactions"].invoke(ctx, {"merchant_contains": "quickmart"})
    assert [t["transaction_id"] for t in by_merchant["transactions"]] == ["txn_1002_q02", "txn_1002_q01"]
    by_card = REGISTRY["list_transactions"].invoke(ctx, {"card_id": "card_1002_credit", "days": 180, "limit": 50})
    assert {t["card_id"] for t in by_card["transactions"]} == {"card_1002_credit"}
    assert REGISTRY["list_transactions"].invoke(ctx, {"limit": 2})["count"] == 2


def test_memo_untrusted_is_truncated(ctx_for: Callable[[str], Ctx]) -> None:
    assert len(INJECTION_DESCRIPTION) > MEMO_MAX_CHARS
    txn = REGISTRY["get_transaction"].invoke(ctx_for("u_1003"), {"transaction_id": "txn_1003_i01"})
    assert set(txn) == TXN_KEYS
    assert txn["memo_untrusted"] == INJECTION_DESCRIPTION[:MEMO_MAX_CHARS]


def test_get_dispute_status(ctx_for: Callable[[str], Ctx]) -> None:
    dispute = REGISTRY["get_dispute_status"].invoke(ctx_for("u_1005"), {"dispute_id": "dsp_5001"})
    assert dispute == {
        "dispute_id": "dsp_5001",
        "transaction_id": "txn_1005_e01",
        "amount": 129.99,
        "reason": "not_received",
        "status": "under_review",
        "opened": "2026-09-06",
    }


def test_analyze_spending_matches_planted_case(ctx_for: Callable[[str], Ctx]) -> None:
    anomalies = REGISTRY["analyze_spending"].invoke(ctx_for("u_1001"), {"days": 90})["anomalies"]
    assert [(a["transaction_id"], a["risk_score"], a["date"]) for a in anomalies] == [
        ("txn_1001_a01", 1.0, "2026-09-29 03:12")
    ]


# ---- write and admin executors ------------------------------------------------------------------------------------


def test_write_executors_change_the_backend(ctx_for: Callable[[str], Ctx], store: BankStore) -> None:
    ctx = ctx_for("u_1001")
    assert REGISTRY["freeze_card"].invoke(ctx, {"card_id": "card_1001_debit", "reason": "lost"})["status"] == "frozen"
    assert REGISTRY["unfreeze_card"].invoke(ctx, {"card_id": "card_1001_debit"})["status"] == "active"
    dispute = REGISTRY["create_dispute"].invoke(
        ctx, {"transaction_id": "txn_1001_s02", "reason": "duplicate", "amount": "10.50", "details": "charged twice"}
    )
    assert (dispute["dispute_id"], dispute["amount"]) == ("dsp_7001", 10.5)
    flag = REGISTRY["flag_transaction"].invoke(ctx, {"transaction_id": "txn_1001_a01", "reason": "not me"})
    assert flag == {"flag_id": "flg_8001", "transaction_id": "txn_1001_a01", "reason": "not me"}
    snapshot = store.snapshot()
    assert snapshot["disputes"]["txn_1001_s02"] == "duplicate" and snapshot["flags"] == {"txn_1001_a01": "not me"}


def test_admin_tools_work_against_the_backend(ctx_for: Callable[[str], Ctx], store: BankStore) -> None:
    ctx = ctx_for("u_1006")
    transfer = REGISTRY["transfer_funds"].invoke(ctx, {"to_account": "acct_9999", "amount": 42.33, "note": "ignored"})
    assert transfer["from_account"] == "acct_1006" and transfer["amount"] == 42.33
    assert REGISTRY["update_contact_email"].invoke(ctx, {"email": "x@example.com"}) == {"email": "x@example.com"}
    assert REGISTRY["close_account"].invoke(ctx, {})["status"] == "closed"
    snapshot = store.snapshot()
    assert snapshot["transfers"] == [("acct_1006", "acct_9999", 42.33)]
    assert snapshot["contacts"]["u_1006"] == "x@example.com" and snapshot["accounts"]["acct_1006"] == "closed"


def test_invoke_validates_with_the_tool_model(ctx_for: Callable[[str], Ctx]) -> None:
    with pytest.raises(ValidationError):
        REGISTRY["freeze_card"].invoke(ctx_for("u_1001"), {"card_id": "../../payments/v1/transfers"})


def test_ctx_primary_account(ctx_for: Callable[[str], Ctx]) -> None:
    ctx = ctx_for("u_1004")
    assert ctx.primary_account() == "acct_1004" == ctx.primary_account()


def test_tool_spec_is_immutable() -> None:
    spec: ToolSpec = REGISTRY["freeze_card"]
    with pytest.raises(AttributeError):
        spec.kind = "admin"  # type: ignore[misc]
