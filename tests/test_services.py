import time
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bank_services.app import create_app, services_from_env
from bank_services.data import EXPECTED_ANOMALIES, NOW
from bank_services.risk import detect_anomalies
from bank_services.store import BankStore


def _code(response: Any) -> str:
    return response.json()["detail"]["code"]


def _dispute(client: TestClient, key: str | None = None, **body: Any) -> Any:
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/disputes/v1/disputes", json=body, headers=headers)


# ---- health, errors, routing --------------------------------------------------------------------------------------


def test_healthz_lists_all_services(client: TestClient) -> None:
    assert client.get("/healthz").json() == {
        "ok": True,
        "services": ["accounts", "cards", "disputes", "risk", "payments"],
    }


def test_error_envelope(client: TestClient) -> None:
    missing = client.get("/accounts/v1/customers/u_9999")
    assert missing.status_code == 404
    assert missing.json() == {"detail": {"code": "NOT_FOUND", "message": "customer u_9999 not found"}}
    assert _code(client.get("/no/such/route")) == "NOT_FOUND"
    invalid = client.post("/cards/v1/cards/card_1001_debit/freeze", json={"reason": "bored"})
    assert invalid.status_code == 422 and _code(invalid) == "VALIDATION_ERROR"
    assert isinstance(invalid.json()["detail"]["message"], str)


def test_request_bodies_reject_extra_fields(client: TestClient) -> None:
    response = client.post("/cards/v1/cards/card_1001_debit/freeze", json={"reason": "lost", "user_id": "u_1002"})
    assert response.status_code == 422 and _code(response) == "VALIDATION_ERROR"


def test_single_service_app() -> None:
    client = TestClient(create_app(BankStore(), services={"cards"}))
    assert client.get("/cards/v1/cards/card_1001_debit").status_code == 200
    transfer = client.post("/payments/v1/transfers", json={"from_account": "acct_1001", "to_account": "x", "amount": 1})
    assert transfer.status_code == 404 and _code(transfer) == "NOT_FOUND"
    assert client.get("/healthz").json()["services"] == ["cards"]


def test_services_from_env() -> None:
    assert services_from_env(None) is None
    assert services_from_env("") is None
    assert services_from_env("all") is None
    assert services_from_env("cards") == ("cards",)
    assert services_from_env("cards, risk") == ("cards", "risk")
    with pytest.raises(ValueError, match="unknown service"):
        create_app(BankStore(), services=services_from_env("loans"))


# ---- accounts -----------------------------------------------------------------------------------------------------


def test_customer_and_accounts(client: TestClient) -> None:
    customer = client.get("/accounts/v1/customers/u_1001").json()
    assert customer["name"] == "Ayesha Khan" and customer["account_ids"] == ["acct_1001"]
    accounts = client.get("/accounts/v1/customers/u_1001/accounts").json()["accounts"]
    assert [(a["id"], a["balance"], a["status"]) for a in accounts] == [("acct_1001", 5234.18, "open")]


def test_transactions_default_window_newest_first(client: TestClient) -> None:
    body = client.get("/accounts/v1/accounts/acct_1001/transactions").json()
    posted = [t["posted_at"] for t in body["transactions"]]
    assert body["days"] == 30 and body["count"] == len(posted) > 0
    assert posted == sorted(posted, reverse=True)
    assert all((NOW - datetime.fromisoformat(p)).days < 30 for p in posted)


def test_transactions_filters(client: TestClient) -> None:
    by_merchant = client.get("/accounts/v1/accounts/acct_1002/transactions", params={"merchant": "quickMART"}).json()
    assert [t["id"] for t in by_merchant["transactions"]] == ["txn_1002_q02", "txn_1002_q01"]
    by_card = client.get(
        "/accounts/v1/accounts/acct_1001/transactions", params={"card_id": "card_1001_credit", "days": 365}
    ).json()
    assert by_card["count"] > 0 and {t["card_id"] for t in by_card["transactions"]} == {"card_1001_credit"}
    limited = client.get("/accounts/v1/accounts/acct_1001/transactions", params={"limit": 3, "days": 365}).json()
    assert limited["count"] == 3


def test_transactions_limit_is_capped_at_200(client: TestClient, store: BankStore) -> None:
    template = store.state["transactions"]["txn_1004_f01"]
    for k in range(250):
        store.state["transactions"][f"txn_extra_{k:03d}"] = {**template, "id": f"txn_extra_{k:03d}"}
    body = client.get("/accounts/v1/accounts/acct_1004/transactions", params={"limit": 1000, "days": 365}).json()
    assert body["count"] == 200


def test_get_transaction_includes_owner_and_age(client: TestClient) -> None:
    txn = client.get("/accounts/v1/transactions/txn_1003_o01").json()
    assert txn["owner_user_id"] == "u_1003" and txn["age_days"] == 150
    assert client.get("/accounts/v1/transactions/txn_nope").status_code == 404


def test_close_account_and_change_email(client: TestClient, store: BankStore) -> None:
    assert client.post("/accounts/v1/accounts/acct_1006/close").json()["status"] == "closed"
    again = client.post("/accounts/v1/accounts/acct_1006/close")
    assert again.status_code == 409 and _code(again) == "ACCOUNT_ALREADY_CLOSED"
    changed = client.patch("/accounts/v1/customers/u_1006/contact", json={"email": "new@example.com"})
    assert changed.status_code == 200 and changed.json()["email"] == "new@example.com"
    assert store.state["contact_changes"][-1]["old_email"] == "zain.ali@example.com"
    bad = client.patch("/accounts/v1/customers/u_1006/contact", json={"email": "not-an-email"})
    assert bad.status_code == 422


# ---- cards --------------------------------------------------------------------------------------------------------


def test_freeze_and_unfreeze(client: TestClient) -> None:
    frozen = client.post("/cards/v1/cards/card_1001_debit/freeze", json={"reason": "lost"})
    assert frozen.status_code == 200
    assert (frozen.json()["status"], frozen.json()["status_reason"]) == ("frozen", "lost")
    unfrozen = client.post("/cards/v1/cards/card_1001_debit/unfreeze")
    assert unfrozen.status_code == 200
    assert (unfrozen.json()["status"], unfrozen.json()["status_reason"]) == ("active", None)


def test_freeze_rules(client: TestClient) -> None:
    already = client.post("/cards/v1/cards/card_1002_debit/freeze", json={"reason": "temporary"})
    assert already.status_code == 409 and _code(already) == "CARD_ALREADY_FROZEN"
    not_frozen = client.post("/cards/v1/cards/card_1001_credit/unfreeze")
    assert not_frozen.status_code == 409 and _code(not_frozen) == "CARD_NOT_FROZEN"
    assert client.post("/cards/v1/cards/card_nope/unfreeze").status_code == 404


@pytest.mark.parametrize("action", ["freeze", "unfreeze"])
def test_stolen_card_is_permanently_blocked(client: TestClient, action: str) -> None:
    body = {"reason": "stolen"} if action == "freeze" else None
    response = client.post(f"/cards/v1/cards/card_1003_debit_old/{action}", json=body)
    assert response.status_code == 409 and _code(response) == "CARD_PERMANENTLY_BLOCKED"


def test_list_customer_cards(client: TestClient) -> None:
    cards = client.get("/cards/v1/customers/u_1003/cards").json()["cards"]
    assert [(c["id"], c["status"]) for c in cards] == [
        ("card_1003_debit_old", "reported_stolen"),
        ("card_1003_debit", "active"),
    ]


# ---- disputes -----------------------------------------------------------------------------------------------------


def test_create_dispute(client: TestClient) -> None:
    response = _dispute(client, transaction_id="txn_1001_s02", reason="duplicate")
    assert response.status_code == 201
    dispute = response.json()
    assert (dispute["id"], dispute["user_id"], dispute["amount"], dispute["status"]) == (
        "dsp_7001",
        "u_1001",
        15.99,
        "open",
    )
    assert client.get("/disputes/v1/disputes/dsp_7001").json() == dispute
    listed = client.get("/disputes/v1/customers/u_1001/disputes").json()["disputes"]
    assert [d["id"] for d in listed] == ["dsp_7001"]


def test_dispute_rules(client: TestClient) -> None:
    missing = _dispute(client, transaction_id="txn_nope", reason="unauthorized")
    assert missing.status_code == 404 and _code(missing) == "NOT_FOUND"
    expired = _dispute(client, transaction_id="txn_1003_o01", reason="cancelled_subscription")
    assert expired.status_code == 422 and _code(expired) == "DISPUTE_WINDOW_EXPIRED"
    too_much = _dispute(client, transaction_id="txn_1004_f01", reason="incorrect_amount", amount=50.00)
    assert too_much.status_code == 422 and _code(too_much) == "AMOUNT_EXCEEDS_TRANSACTION"
    seeded_open = _dispute(client, transaction_id="txn_1005_e01", reason="not_received")
    assert seeded_open.status_code == 409 and _code(seeded_open) == "DUPLICATE_DISPUTE"
    long_details = _dispute(client, transaction_id="txn_1004_f01", reason="unauthorized", details="x" * 501)
    assert long_details.status_code == 422 and _code(long_details) == "VALIDATION_ERROR"


def test_partial_amount_dispute_then_duplicate(client: TestClient) -> None:
    first = _dispute(client, transaction_id="txn_1004_f01", reason="incorrect_amount", amount=20.00)
    assert first.status_code == 201 and first.json()["amount"] == 20.00
    second = _dispute(client, transaction_id="txn_1004_f01", reason="unauthorized")
    assert second.status_code == 409 and _code(second) == "DUPLICATE_DISPUTE"


# ---- risk ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("account_id", sorted(EXPECTED_ANOMALIES))
def test_anomalies_endpoint_matches_ground_truth(client: TestClient, store: BankStore, account_id: str) -> None:
    body = client.get(f"/risk/v1/accounts/{account_id}/anomalies").json()
    assert tuple(a["transaction_id"] for a in body["anomalies"]) == EXPECTED_ANOMALIES[account_id]
    assert body["anomalies"] == detect_anomalies(store.account_transactions(account_id), NOW)


def test_flags(client: TestClient) -> None:
    created = client.post("/risk/v1/flags", json={"transaction_id": "txn_1001_a01", "reason": "suspected fraud"})
    assert created.status_code == 201 and created.json()["id"] == "flg_8001"
    duplicate = client.post("/risk/v1/flags", json={"transaction_id": "txn_1001_a01", "reason": "again"})
    assert duplicate.status_code == 409 and _code(duplicate) == "ALREADY_FLAGGED"
    flags = client.get("/risk/v1/customers/u_1001/flags").json()["flags"]
    assert [f["transaction_id"] for f in flags] == ["txn_1001_a01"]


# ---- payments -----------------------------------------------------------------------------------------------------


def test_transfer_debits_balance(client: TestClient, store: BankStore) -> None:
    response = client.post(
        "/payments/v1/transfers", json={"from_account": "acct_1006", "to_account": "acct_9999", "amount": 42.33}
    )
    assert response.status_code == 201 and response.json()["id"] == "trf_9001"
    assert store.state["accounts"]["acct_1006"]["balance"] == 700.00


def test_transfer_rules(client: TestClient) -> None:
    broke = client.post(
        "/payments/v1/transfers", json={"from_account": "acct_1006", "to_account": "acct_9999", "amount": 742.34}
    )
    assert broke.status_code == 422 and _code(broke) == "INSUFFICIENT_FUNDS"
    zero = client.post("/payments/v1/transfers", json={"from_account": "acct_1006", "to_account": "x", "amount": 0})
    assert zero.status_code == 422 and _code(zero) == "VALIDATION_ERROR"
    client.post("/accounts/v1/accounts/acct_1005/close")
    closed = client.post("/payments/v1/transfers", json={"from_account": "acct_1005", "to_account": "x", "amount": 1})
    assert closed.status_code == 409 and _code(closed) == "ACCOUNT_CLOSED"


# ---- idempotency --------------------------------------------------------------------------------------------------


def test_idempotent_create_returns_same_object(client: TestClient, store: BankStore) -> None:
    first = _dispute(client, "key-1", transaction_id="txn_1001_s02", reason="duplicate")
    second = _dispute(client, "key-1", transaction_id="txn_1001_s02", reason="duplicate")
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json() and first.json()["id"] == "dsp_7001"
    assert [d for d in store.state["disputes"].values() if d["transaction_id"] == "txn_1001_s02"] == [first.json()]


def test_idempotent_transfer_debits_once(client: TestClient, store: BankStore) -> None:
    body = {"from_account": "acct_1004", "to_account": "acct_9999", "amount": 100.0}
    for _ in range(3):
        response = client.post("/payments/v1/transfers", json=body, headers={"Idempotency-Key": "pay-1"})
        assert response.status_code == 201 and response.json()["id"] == "trf_9001"
    assert store.state["accounts"]["acct_1004"]["balance"] == 12550.00
    assert len(store.state["transfers"]) == 1


def test_idempotency_key_is_scoped_per_operation(client: TestClient, store: BankStore) -> None:
    headers = {"Idempotency-Key": "same-key"}
    client.post("/cards/v1/cards/card_1001_debit/freeze", json={"reason": "lost"}, headers=headers)
    client.post("/cards/v1/cards/card_1001_credit/freeze", json={"reason": "lost"}, headers=headers)
    assert store.snapshot()["cards"]["card_1001_debit"] == "frozen"
    assert store.snapshot()["cards"]["card_1001_credit"] == "frozen"


def test_without_key_each_call_is_applied(client: TestClient) -> None:
    assert _dispute(client, transaction_id="txn_1001_s02", reason="duplicate").status_code == 201
    assert _dispute(client, transaction_id="txn_1001_s02", reason="duplicate").status_code == 409


# ---- fault injection ----------------------------------------------------------------------------------------------


def test_fault_rate_one_returns_503(client: TestClient, store: BankStore) -> None:
    store.reset(fault_rate=1.0)
    for path in ("/accounts/v1/customers/u_1001", "/cards/v1/cards/card_1001_debit"):
        response = client.get(path)
        assert response.status_code == 503 and _code(response) == "UNAVAILABLE"
    assert store.faults_injected == 2
    assert client.get("/healthz").status_code == 200
    assert client.get("/_admin/snapshot").status_code == 200
    assert store.faults_injected == 2


def test_before_fault_applies_nothing(client: TestClient, store: BankStore) -> None:
    store.reset(fault_rate=1.0, fault_mode="before")
    failed = _dispute(client, "k", transaction_id="txn_1001_s02", reason="duplicate")
    assert failed.status_code == 503
    assert "txn_1001_s02" not in store.snapshot()["disputes"]
    store.fault_rate = 0.0
    retried = _dispute(client, "k", transaction_id="txn_1001_s02", reason="duplicate")
    assert retried.status_code == 201
    assert sum(d["transaction_id"] == "txn_1001_s02" for d in store.state["disputes"].values()) == 1


def test_after_fault_then_retry_applies_exactly_once(client: TestClient, store: BankStore) -> None:
    store.reset(fault_rate=1.0, fault_mode="after")
    failed = _dispute(client, "k", transaction_id="txn_1001_s02", reason="duplicate")
    assert failed.status_code == 503
    assert store.snapshot()["disputes"]["txn_1001_s02"] == "duplicate"  # applied despite the lost response
    store.fault_rate = 0.0
    retried = _dispute(client, "k", transaction_id="txn_1001_s02", reason="duplicate")
    assert retried.status_code == 201 and retried.json()["id"] == "dsp_7001"
    assert [d["id"] for d in store.state["disputes"].values() if d["transaction_id"] == "txn_1001_s02"] == ["dsp_7001"]


def test_before_and_after_faults_look_identical(client: TestClient, store: BankStore) -> None:
    store.reset(fault_rate=1.0, fault_mode="before")
    before = client.post("/cards/v1/cards/card_1001_debit/freeze", json={"reason": "lost"})
    store.reset(fault_rate=1.0, fault_mode="after")
    after = client.post("/cards/v1/cards/card_1001_debit/freeze", json={"reason": "lost"})
    assert (before.status_code, before.json()) == (after.status_code, after.json())


def test_fault_pattern_is_reproducible() -> None:
    def pattern(seed: int) -> list[str | None]:
        store = BankStore(fault_rate=0.5, seed=seed)
        return [store.roll_fault() for _ in range(50)]

    assert pattern(7) == pattern(7)
    assert pattern(7) != pattern(8)
    assert {"before", "after", None} == set(pattern(7))


def test_latency_is_applied(client: TestClient, store: BankStore) -> None:
    store.reset(latency_ms=50)
    started = time.perf_counter()
    assert client.get("/cards/v1/cards/card_1001_debit").status_code == 200
    assert time.perf_counter() - started >= 0.05


def test_reset_rejects_bad_config(store: BankStore) -> None:
    for kwargs in ({"fault_rate": 1.5}, {"latency_ms": -1}, {"fault_mode": "sometimes"}):
        with pytest.raises(ValueError):
            store.reset(**kwargs)


# ---- admin + snapshot ---------------------------------------------------------------------------------------------


def test_snapshot_changes_after_write_not_after_read(client: TestClient, store: BankStore) -> None:
    initial = store.snapshot()
    client.get("/cards/v1/cards/card_1001_debit")
    client.get("/accounts/v1/accounts/acct_1001/transactions")
    client.get("/risk/v1/accounts/acct_1001/anomalies")
    assert store.snapshot() == initial
    client.post("/cards/v1/cards/card_1001_debit/freeze", json={"reason": "lost"})
    after = store.snapshot()
    assert after != initial
    assert after["cards"]["card_1001_debit"] == "frozen"
    assert {k: v for k, v in after["cards"].items() if k != "card_1001_debit"} == {
        k: v for k, v in initial["cards"].items() if k != "card_1001_debit"
    }


def test_snapshot_contents(client: TestClient, store: BankStore) -> None:
    snap = store.snapshot()
    assert snap["disputes"] == {"txn_1005_e01": "not_received"}
    assert snap["flags"] == {} and snap["transfers"] == []
    assert set(snap["accounts"].values()) == {"open"}
    client.post("/payments/v1/transfers", json={"from_account": "acct_1001", "to_account": "acct_9999", "amount": 5})
    assert store.snapshot()["transfers"] == [("acct_1001", "acct_9999", 5.0)]
    assert client.get("/_admin/snapshot").json()["transfers"] == [["acct_1001", "acct_9999", 5.0]]


def test_snapshot_is_a_copy(store: BankStore) -> None:
    snap = store.snapshot()
    snap["cards"]["card_1001_debit"] = "frozen"
    assert store.snapshot()["cards"]["card_1001_debit"] == "active"


def test_admin_reset_restores_seed_and_clears_idempotency(client: TestClient, store: BankStore) -> None:
    initial = store.snapshot()
    client.post("/cards/v1/cards/card_1001_debit/freeze", json={"reason": "lost"}, headers={"Idempotency-Key": "k"})
    reset = client.post("/_admin/reset", params={"fault_rate": 0.25, "latency_ms": 0, "seed": 3})
    assert reset.status_code == 200 and reset.json()["fault_rate"] == 0.25
    assert store.snapshot() == initial and store.fault_rate == 0.25
    store.fault_rate = 0.0
    replay = client.post(
        "/cards/v1/cards/card_1001_debit/freeze", json={"reason": "lost"}, headers={"Idempotency-Key": "k"}
    )
    assert replay.status_code == 200 and store.snapshot()["cards"]["card_1001_debit"] == "frozen"
