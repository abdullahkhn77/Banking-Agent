import random
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.guardrails import Decision, GuardConfig, GuardResult
from agent.tools import READ_TOOLS, REGISTRY, WRITE_TOOLS, Ctx
from bank_services.store import BankStore

Guard = Callable[..., GuardResult]


def _blocked(result: GuardResult, layer: str | None, code: str) -> bool:
    return (result.decision, result.layer, result.code) == (Decision.BLOCK, layer, code)


def _script_faults(store: BankStore, faults: list[str | None]) -> None:
    queue = list(faults)
    store.roll_fault = lambda: queue.pop(0) if queue else None  # type: ignore[method-assign]


def _count_requests(client: TestClient) -> list[str]:
    requests: list[str] = []
    client.event_hooks["request"].append(lambda request: requests.append(str(request.url)))
    return requests


# ---- L1 allow-list ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["transfer_funds", "close_account", "update_contact_email", "execute_sql"])
def test_l1_blocks_tools_not_on_the_allowlist(guard: Guard, name: str) -> None:
    result = guard(name, {"to_account": "acct_9999", "amount": 5000})
    assert _blocked(result, "L1", "NOT_ALLOWLISTED")
    assert name in result.message and result.args is None


def test_l1_blocks_non_string_names(guard: Guard) -> None:
    for name in (None, 42, ["freeze_card"], {"name": "freeze_card"}):
        assert _blocked(guard(name, {}), "L1", "NOT_ALLOWLISTED")


# ---- L2 validation ------------------------------------------------------------------------------------------------

DISPUTE = {"transaction_id": "txn_1001_s02", "reason": "duplicate"}


@pytest.mark.parametrize(
    ("name", "args"),
    [
        pytest.param("freeze_card", {"card_id": "card_1001_debit", "user_id": "u_1002"}, id="smuggled-user-id"),
        pytest.param("freeze_card", {"card_id": "../../payments/v1/transfers"}, id="path-traversal"),
        pytest.param("create_dispute", {**DISPUTE, "amount": -5}, id="negative-amount"),
        pytest.param("create_dispute", {**DISPUTE, "amount": 15.999}, id="three-decimals"),
        pytest.param("create_dispute", {**DISPUTE, "amount": 100000.01}, id="amount-too-large"),
        pytest.param("freeze_card", {"card_id": "card_1001_debit", "reason": "because"}, id="bad-enum"),
        pytest.param("create_dispute", {**DISPUTE, "reason": "fraud"}, id="bad-dispute-reason"),
        pytest.param("list_transactions", {"merchant_contains": "' OR 1=1; --"}, id="sql-ish-merchant"),
        pytest.param("flag_transaction", {"transaction_id": "txn_1001_a01", "reason": "fraud\x00now"}, id="nul-char"),
        pytest.param("flag_transaction", {"transaction_id": "txn_1001_a01", "reason": "a\x1b[31mb"}, id="ansi-escape"),
        pytest.param("create_dispute", {**DISPUTE, "details": "line one\nline two"}, id="newline-in-details"),
        pytest.param("create_dispute", {**DISPUTE, "details": "x" * 301}, id="details-too-long"),
        pytest.param("flag_transaction", {"transaction_id": "txn_1001_a01", "reason": "no"}, id="reason-too-short"),
        pytest.param("freeze_card", "card_1001_debit", id="string-args"),
        pytest.param("freeze_card", ["card_1001_debit"], id="list-args"),
        pytest.param("freeze_card", None, id="none-args"),
        pytest.param("freeze_card", {}, id="missing-card-id"),
        pytest.param("create_dispute", {"transaction_id": "txn_1001_s02"}, id="missing-reason"),
        pytest.param("list_transactions", {"days": 0}, id="days-too-small"),
        pytest.param("list_transactions", {"limit": 51}, id="limit-too-large"),
        pytest.param("get_dispute_status", {"dispute_id": "dsp_abc"}, id="bad-dispute-id"),
        pytest.param("get_transaction", {"transaction_id": "TXN_1001_S02"}, id="uppercase-id"),
        pytest.param("get_accounts", {"account_id": "acct_1002"}, id="extra-arg-on-no-arg-tool"),
    ],
)
def test_l2_blocks_invalid_args(guard: Guard, name: str, args: Any) -> None:
    result = guard(name, args)
    assert _blocked(result, "L2", "INVALID_ARGS")
    assert name in result.message


def test_l2_message_lists_at_most_four_errors(guard: Guard) -> None:
    args = {"transaction_id": "bad", "reason": "bad", "amount": -1, "details": 7, "x": 1, "y": 2}
    message = guard("create_dispute", args).message
    assert message.count(": ") <= 5 and "and 2 more" in message


def test_l2_message_names_the_bad_field(guard: Guard) -> None:
    message = guard("freeze_card", {"card_id": "card_1001_debit", "user_id": "u_1002"}).message
    assert "user_id: Extra inputs are not permitted" in message


def test_l1_and_l2_blocks_make_no_backend_calls(guard: Guard, client: TestClient) -> None:
    requests = _count_requests(client)
    guard("transfer_funds", {"to_account": "acct_9999", "amount": 5000})
    guard("freeze_card", {"card_id": "../../payments/v1/transfers"})
    assert requests == []


# ---- L3 ownership -------------------------------------------------------------------------------------------------

OTHER_CUSTOMERS_RESOURCES = [
    ("freeze_card", {"card_id": "card_1002_credit"}, "card"),
    ("unfreeze_card", {"card_id": "card_1006_debit"}, "card"),
    ("get_transaction", {"transaction_id": "txn_1002_q01"}, "transaction"),
    ("create_dispute", {"transaction_id": "txn_1004_a01", "reason": "unauthorized"}, "transaction"),
    ("flag_transaction", {"transaction_id": "txn_1006_g01", "reason": "gift card fraud"}, "transaction"),
    ("get_dispute_status", {"dispute_id": "dsp_5001"}, "dispute"),
    ("list_transactions", {"card_id": "card_1003_debit"}, "card"),
]
NONEXISTENT_RESOURCES = [
    ("freeze_card", {"card_id": "card_zzz_999"}, "card"),
    ("get_transaction", {"transaction_id": "txn_zzz_999"}, "transaction"),
    ("create_dispute", {"transaction_id": "txn_9999_x01", "reason": "duplicate"}, "transaction"),
    ("get_dispute_status", {"dispute_id": "dsp_99999"}, "dispute"),
    ("list_transactions", {"card_id": "card_zzz_999"}, "card"),
]


@pytest.mark.parametrize(("name", "args", "label"), OTHER_CUSTOMERS_RESOURCES)
def test_l3_blocks_other_customers_resources(guard: Guard, name: str, args: dict[str, Any], label: str) -> None:
    result = guard(name, args, "u_1001")
    assert _blocked(result, "L3", "NOT_OWNER")
    assert result.message == f"No such {label} for this customer. Look IDs up with a list tool instead of guessing."


@pytest.mark.parametrize(("name", "args", "label"), NONEXISTENT_RESOURCES)
def test_l3_blocks_nonexistent_resources(guard: Guard, name: str, args: dict[str, Any], label: str) -> None:
    result = guard(name, args, "u_1001")
    assert _blocked(result, "L3", "NOT_FOUND")
    assert result.message == f"No such {label} for this customer. Look IDs up with a list tool instead of guessing."


def test_l3_not_owner_and_not_found_look_identical_to_the_model(guard: Guard) -> None:
    for label in ("card", "transaction", "dispute"):
        owner = next(guard(n, a, "u_1001") for n, a, lbl in OTHER_CUSTOMERS_RESOURCES if lbl == label)
        missing = next(guard(n, a, "u_1001") for n, a, lbl in NONEXISTENT_RESOURCES if lbl == label)
        assert (owner.code, missing.code) == ("NOT_OWNER", "NOT_FOUND")
        assert owner.message == missing.message


def test_l3_messages_leak_nothing_about_the_other_customer(guard: Guard) -> None:
    result = guard("freeze_card", {"card_id": "card_1002_credit"}, "u_1001")
    assert _blocked(result, "L3", "NOT_OWNER")
    for secret in ("card_1002_credit", "5512", "u_1002", "Bilal", "credit"):
        assert secret not in result.message


def test_l3_lookup_during_outage_fails_closed(guard: Guard, store: BankStore) -> None:
    store.reset(fault_rate=1.0)
    for name, args in (
        ("get_transaction", {"transaction_id": "txn_1001_s01"}),
        ("freeze_card", {"card_id": "card_1001_debit"}),
    ):
        result = guard(name, args, "u_1001")
        assert _blocked(result, "L3", "LOOKUP_FAILED")


def test_l4_policy_lookup_during_outage_fails_closed(guard: Guard, store: BankStore) -> None:
    _script_faults(store, [None, "before", "before", "before", "before"])  # transaction lookup ok, disputes list down
    result = guard("create_dispute", {"transaction_id": "txn_1001_s02", "reason": "duplicate"}, "u_1001")
    assert _blocked(result, "L4", "LOOKUP_FAILED")


# ---- L4 business policy -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "args", "user_id", "code"),
    [
        ("freeze_card", {"card_id": "card_1003_debit_old", "reason": "stolen"}, "u_1003", "CARD_REPORTED_STOLEN"),
        ("unfreeze_card", {"card_id": "card_1003_debit_old"}, "u_1003", "CARD_REPORTED_STOLEN"),
        ("freeze_card", {"card_id": "card_1002_debit"}, "u_1002", "CARD_ALREADY_FROZEN"),
        ("unfreeze_card", {"card_id": "card_1001_debit"}, "u_1001", "CARD_NOT_FROZEN"),
        (
            "create_dispute",
            {"transaction_id": "txn_1003_o01", "reason": "cancelled_subscription"},
            "u_1003",
            "DISPUTE_WINDOW_EXPIRED",
        ),
        (
            "create_dispute",
            {"transaction_id": "txn_1001_s02", "reason": "duplicate", "amount": 16.00},
            "u_1001",
            "AMOUNT_EXCEEDS_TRANSACTION",
        ),
        ("create_dispute", {"transaction_id": "txn_1005_e01", "reason": "not_received"}, "u_1005", "DUPLICATE_DISPUTE"),
    ],
)
def test_l4_policy_blocks(guard: Guard, name: str, args: dict[str, Any], user_id: str, code: str) -> None:
    assert _blocked(guard(name, args, user_id), "L4", code)


def test_l4_stolen_card_message_points_to_replacement(guard: Guard) -> None:
    message = guard("unfreeze_card", {"card_id": "card_1003_debit_old"}, "u_1003").message
    assert "replacement card" in message and "list_cards" in message


def test_l4_duplicate_dispute_names_the_open_dispute(guard: Guard) -> None:
    message = guard("create_dispute", {"transaction_id": "txn_1005_e01", "reason": "not_received"}, "u_1005").message
    assert "dsp_5001" in message and "get_dispute_status" in message


def test_l4_already_flagged(guard: Guard, ctx_for: Callable[[str], Ctx]) -> None:
    args = {"transaction_id": "txn_1001_a01", "reason": "customer does not recognise it"}
    assert guard("flag_transaction", args, "u_1001").decision == Decision.CONFIRM
    REGISTRY["flag_transaction"].invoke(ctx_for("u_1001"), args)
    assert _blocked(guard("flag_transaction", args, "u_1001"), "L4", "ALREADY_FLAGGED")


def test_l4_dispute_after_agent_created_dispute(guard: Guard, ctx_for: Callable[[str], Ctx]) -> None:
    args = {"transaction_id": "txn_1001_s02", "reason": "duplicate"}
    REGISTRY["create_dispute"].invoke(ctx_for("u_1001"), args)
    assert _blocked(guard("create_dispute", args, "u_1001"), "L4", "DUPLICATE_DISPUTE")


def test_l4_dispute_amount_equal_to_charge_is_allowed(guard: Guard) -> None:
    result = guard("create_dispute", {"transaction_id": "txn_1001_s02", "reason": "duplicate", "amount": 15.99})
    assert result.decision == Decision.CONFIRM


def test_l4_runs_before_l5(guard: Guard) -> None:
    result = guard("unfreeze_card", {"card_id": "card_1003_debit_old"}, "u_1003", writes_so_far=99)
    assert _blocked(result, "L4", "CARD_REPORTED_STOLEN")


# ---- L5 action budget ---------------------------------------------------------------------------------------------


def test_l5_action_budget(guard: Guard) -> None:
    args = {"card_id": "card_1001_debit", "reason": "lost"}
    assert guard("freeze_card", args, writes_so_far=5).decision == Decision.CONFIRM
    assert _blocked(guard("freeze_card", args, writes_so_far=6), "L5", "ACTION_BUDGET_EXCEEDED")


def test_l5_budget_is_configurable_and_ignores_reads(guard: Guard) -> None:
    config = GuardConfig(max_writes_per_session=2)
    assert _blocked(
        guard("freeze_card", {"card_id": "card_1001_debit"}, writes_so_far=2, config=config),
        "L5",
        "ACTION_BUDGET_EXCEEDED",
    )
    assert guard("list_cards", {}, writes_so_far=100, config=config).decision == Decision.ALLOW


# ---- happy paths --------------------------------------------------------------------------------------------------

OWN_READS = [
    ("get_accounts", {}, "u_1001"),
    ("list_cards", {}, "u_1002"),
    ("list_transactions", {}, "u_1003"),
    (
        "list_transactions",
        {"days": 90, "merchant_contains": "QuickMart", "card_id": "card_1002_credit", "limit": 5},
        "u_1002",
    ),
    ("list_transactions", {"card_id": "card_1003_debit_old"}, "u_1003"),
    ("get_transaction", {"transaction_id": "txn_1003_i01"}, "u_1003"),
    ("analyze_spending", {"days": 30}, "u_1006"),
    ("list_disputes", {}, "u_1005"),
    ("get_dispute_status", {"dispute_id": "dsp_5001"}, "u_1005"),
]


@pytest.mark.parametrize(("name", "args", "user_id"), OWN_READS)
def test_own_reads_are_allowed(guard: Guard, name: str, args: dict[str, Any], user_id: str) -> None:
    result = guard(name, args, user_id)
    assert (result.decision, result.layer, result.code, result.summary) == (Decision.ALLOW, None, None, None)


def test_every_read_tool_has_a_happy_path() -> None:
    assert {name for name, _, _ in OWN_READS} == READ_TOOLS


OWN_WRITES = [
    (
        "freeze_card",
        {"card_id": "card_1001_debit", "reason": "lost"},
        "u_1001",
        "Freeze your debit card ending 4417 (reason: lost)",
    ),
    (
        "freeze_card",
        {"card_id": "card_1005_credit"},
        "u_1005",
        "Freeze your credit card ending 6634 (reason: temporary)",
    ),
    ("unfreeze_card", {"card_id": "card_1002_debit"}, "u_1002", "Unfreeze your debit card ending 3301"),
    (
        "create_dispute",
        {"transaction_id": "txn_1001_s02", "reason": "duplicate"},
        "u_1001",
        "Dispute $15.99 of the $15.99 charge from StreamFlix on 2026-09-25 (reason: duplicate)",
    ),
    (
        "create_dispute",
        {"transaction_id": "txn_1004_a01", "reason": "unauthorized", "amount": 1000},
        "u_1004",
        "Dispute $1,000.00 of the $4,900.00 charge from LuxeGold Jewelers on 2026-09-26 (reason: unauthorized)",
    ),
    (
        "flag_transaction",
        {"transaction_id": "txn_1001_a01", "reason": "customer does not recognise it"},
        "u_1001",
        "Flag the $2,450.00 charge from Elektro-Mag Bucuresti on 2026-09-29 as suspicious",
    ),
]


@pytest.mark.parametrize(("name", "args", "user_id", "summary"), OWN_WRITES)
def test_own_writes_need_confirmation(
    guard: Guard, name: str, args: dict[str, Any], user_id: str, summary: str
) -> None:
    result = guard(name, args, user_id)
    assert (result.decision, result.layer, result.code) == (Decision.CONFIRM, None, None)
    assert result.summary == summary


def test_every_write_tool_has_a_happy_path() -> None:
    assert {name for name, *_ in OWN_WRITES} == WRITE_TOOLS


def test_args_are_normalized_at_proposal_time(guard: Guard) -> None:
    freeze = guard("freeze_card", {"card_id": "  card_1001_debit "})
    assert freeze.args == {"card_id": "card_1001_debit", "reason": "temporary"}
    dispute = guard("create_dispute", {"transaction_id": "txn_1001_s02", "reason": "duplicate", "amount": "10.50"})
    assert dispute.args == {"transaction_id": "txn_1001_s02", "reason": "duplicate", "amount": 10.5, "details": None}


def test_stored_args_execute_the_confirmed_action(
    guard: Guard, ctx_for: Callable[[str], Ctx], store: BankStore
) -> None:
    result = guard("freeze_card", {"card_id": "card_1001_debit", "reason": "lost"})
    REGISTRY[result.tool].invoke(ctx_for("u_1001"), result.args)
    assert store.snapshot()["cards"]["card_1001_debit"] == "frozen"


def test_guard_checks_have_no_side_effects(guard: Guard, store: BankStore) -> None:
    before = store.snapshot()
    for name, args, user_id, _ in OWN_WRITES:
        guard(name, args, user_id)
    for name, args, user_id in OWN_READS:
        guard(name, args, user_id)
    assert store.snapshot() == before


def test_latency_is_recorded(guard: Guard) -> None:
    assert guard("list_cards", {}).latency_ms > 0
    assert guard("execute_sql", {}).latency_ms > 0


# ---- exhaustive ownership matrix ----------------------------------------------------------------------------------


def test_card_ownership_matrix(guard: Guard, store: BankStore, record_count: Callable[[str, int], None]) -> None:
    cards = list(store.state["cards"].values())
    checked = 0
    for user_id in store.state["customers"]:
        for card in cards:
            own = card["user_id"] == user_id
            if own and card["status"] != "active":
                continue  # own frozen/stolen cards are covered by the L4 tests
            result = guard("freeze_card", {"card_id": card["id"]}, user_id)
            checked += 1
            if own:
                assert result.decision == Decision.CONFIRM, (user_id, card["id"])
            else:
                assert _blocked(result, "L3", "NOT_OWNER"), (user_id, card["id"])
    own_active = sum(card["status"] == "active" for card in cards)
    cross = sum(card["user_id"] != user_id for user_id in store.state["customers"] for card in cards)
    assert checked == own_active + cross
    print(f"ownership matrix: {checked} checked calls ({own_active} own-active, {cross} cross-customer)")
    record_count("ownership matrix calls (own active + cross-customer)", checked)


# ---- disabled (ablation) mode -------------------------------------------------------------------------------------

DISABLED = GuardConfig(enabled=False)


def test_disabled_mode_allows_everything_in_the_registry(guard: Guard) -> None:
    transfer = guard("transfer_funds", {"to_account": "acct_9999", "amount": 5000}, config=DISABLED)
    assert transfer.decision == Decision.ALLOW and transfer.args == {"to_account": "acct_9999", "amount": 5000}
    cross = guard("freeze_card", {"card_id": "card_1002_credit"}, "u_1001", config=DISABLED)
    assert cross.decision == Decision.ALLOW and cross.summary is None
    raw = guard("create_dispute", "not even a dict", config=DISABLED)
    assert raw.decision == Decision.ALLOW and raw.args == "not even a dict"
    budget = guard("freeze_card", {"card_id": "card_1001_debit"}, writes_so_far=1000, config=DISABLED)
    assert budget.decision == Decision.ALLOW


def test_disabled_mode_still_rejects_unknown_tools(guard: Guard) -> None:
    for name in ("execute_sql", None, ["transfer_funds"]):
        assert _blocked(guard(name, {}, config=DISABLED), None, "UNKNOWN_TOOL")


def test_disabled_mode_lets_an_unguarded_agent_move_money(
    guard: Guard, ctx_for: Callable[[str], Ctx], store: BankStore
) -> None:
    result = guard("transfer_funds", {"to_account": "acct_9999", "amount": 5000}, "u_1003", config=DISABLED)
    REGISTRY[result.tool].invoke(ctx_for("u_1003"), result.args)
    assert store.snapshot()["transfers"] == [("acct_1003", "acct_9999", 5000.0)]


# ---- fuzzing: the engine never raises -----------------------------------------------------------------------------

FIELD_NAMES = [
    "card_id",
    "transaction_id",
    "dispute_id",
    "reason",
    "amount",
    "details",
    "days",
    "limit",
    "merchant_contains",
    "user_id",
    "to_account",
]
ID_LIKE = [
    "card_1001_debit",
    "card_1002_credit",
    "card_1003_debit_old",
    "txn_1001_s02",
    "txn_1003_o01",
    "txn_1005_e01",
    "dsp_5001",
    "card_zzz_999",
    "../../etc/passwd",
    "card_1001_debit\n",
    "",
]


def _random_value(rng: random.Random, depth: int = 0) -> Any:
    kind = rng.randrange(14)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.randint(-(10**15), 10**15)
    if kind == 3:
        return rng.uniform(-1e6, 1e6)
    if kind == 4:
        return rng.choice([float("nan"), float("inf"), float("-inf"), 1e308, -0.0, 0.001])
    if kind == 5:
        return "x" * rng.choice([0, 1, 300, 100_000])
    if kind == 6:
        return "".join(chr(rng.randrange(0, 0x2FFF)) for _ in range(rng.randrange(1, 60)))
    if kind == 7:
        return rng.choice(ID_LIKE)
    if kind == 8:
        return b"\x00\xff bytes"
    if kind == 9 and depth < 4:
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(4))]
    if kind == 10 and depth < 4:
        return {rng.choice(FIELD_NAMES): _random_value(rng, depth + 1) for _ in range(rng.randrange(4))}
    if kind == 11:
        return {1: "int key", (2, 3): "tuple key"}
    if kind == 12:
        return rng.choice(["lost", "duplicate", "temporary", "unauthorized", "  stolen  "])
    return str(rng.random())


def _random_args(rng: random.Random) -> Any:
    if rng.random() < 0.25:
        return _random_value(rng)
    return {rng.choice(FIELD_NAMES): _random_value(rng) for _ in range(rng.randrange(5))}


def _random_name(rng: random.Random) -> Any:
    pool: list[Any] = [
        *REGISTRY,
        *REGISTRY,
        "execute_sql",
        "",
        "x" * 10_000,
        None,
        42,
        ["freeze_card"],
        {"a": 1},
        "freeze_card\x00",
        "FREEZE_CARD",
    ]
    return rng.choice(pool)


@pytest.mark.parametrize("config", [None, DISABLED], ids=["enabled", "disabled"])
def test_fuzzed_inputs_never_raise(
    guard: Guard, config: GuardConfig | None, record_count: Callable[[str, int], None]
) -> None:
    rng = random.Random(20261001)
    decisions: dict[str, int] = {}
    for _ in range(200):
        result = guard(
            _random_name(rng),
            _random_args(rng),
            rng.choice(["u_1001", "u_1003", "u_1005"]),
            writes_so_far=rng.randrange(10),
            config=config,
        )
        assert isinstance(result, GuardResult)
        assert result.decision in set(Decision)
        assert result.code != "GUARD_ERROR", result
        assert len(result.tool) <= 70 and len(result.message) < 2000
        decisions[result.decision] = decisions.get(result.decision, 0) + 1
    record_count(f"fuzzed checks ({'enabled' if config is None else 'disabled'})", 200)
    assert sum(decisions.values()) == 200
