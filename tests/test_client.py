from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent import client as client_module
from agent.client import BankClient, ServiceError
from bank_services.store import BankStore

Reply = tuple[int, Any] | Exception


def _mock_http(replies: list[Reply], seen: list[httpx.Request]) -> httpx.Client:
    """httpx client whose transport records every request and answers from ``replies`` (then 200 {})."""
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = queue.pop(0) if queue else (200, {})
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://default.test")


def _script_faults(store: BankStore, faults: list[str | None]) -> None:
    """Replace the store's random fault roll with a fixed sequence (then no faults)."""
    queue = list(faults)
    store.roll_fault = lambda: queue.pop(0) if queue else None  # type: ignore[method-assign]


def _error_body(code: str, message: str) -> dict[str, Any]:
    return {"detail": {"code": code, "message": message}}


# ---- routing ------------------------------------------------------------------------------------------------------


def test_routes_by_service_prefix() -> None:
    seen: list[httpx.Request] = []
    bank = BankClient(
        _mock_http([], seen),
        service_urls={"cards": "http://cards.test:9002/", "accounts": "http://accounts.test:9001"},
    )
    bank.get("/cards/v1/cards/card_1001_debit")
    bank.get("/accounts/v1/customers/u_1001")
    bank.get("/risk/v1/accounts/acct_1001/anomalies")
    assert [str(r.url) for r in seen] == [
        "http://cards.test:9002/cards/v1/cards/card_1001_debit",
        "http://accounts.test:9001/accounts/v1/customers/u_1001",
        "http://default.test/risk/v1/accounts/acct_1001/anomalies",
    ]


def test_unroutable_paths_and_bad_config_are_rejected() -> None:
    bank = BankClient(_mock_http([], []))
    for path in ("/loans/v1/x", "cards/v1/cards/x", "/", ""):
        with pytest.raises(ValueError, match="cannot route"):
            bank.get(path)
    with pytest.raises(ValueError, match="unknown service"):
        BankClient(_mock_http([], []), service_urls={"loans": "http://x"})


def test_none_params_are_dropped() -> None:
    seen: list[httpx.Request] = []
    BankClient(_mock_http([], seen)).get("/accounts/v1/accounts/a/transactions", days=30, merchant=None, card_id=None)
    assert seen[0].url.query == b"days=30"


def test_works_with_testclient_and_per_service_urls(client: TestClient) -> None:
    bank = BankClient(client, service_urls={"cards": "http://testserver"}, backoff_s=0)
    assert bank.get("/cards/v1/cards/card_1001_debit")["last4"] == "4417"
    assert bank.get("/accounts/v1/customers/u_1001")["name"] == "Ayesha Khan"


# ---- idempotency keys ---------------------------------------------------------------------------------------------


def test_reads_send_no_idempotency_key_and_writes_send_one() -> None:
    seen: list[httpx.Request] = []
    bank = BankClient(_mock_http([], seen))
    bank.get("/cards/v1/cards/x")
    bank.post("/cards/v1/cards/x/freeze", {"reason": "lost"})
    bank.patch("/accounts/v1/customers/u/contact", {"email": "a@example.com"})
    assert "idempotency-key" not in seen[0].headers
    assert seen[1].headers["idempotency-key"] and seen[2].headers["idempotency-key"]
    assert seen[1].headers["idempotency-key"] != seen[2].headers["idempotency-key"]


def test_write_retries_reuse_the_same_key() -> None:
    seen: list[httpx.Request] = []
    bank = BankClient(_mock_http([(503, {}), (500, {}), (201, {"id": "dsp_7001"})], seen), backoff_s=0)
    assert bank.post("/disputes/v1/disputes", {"transaction_id": "t", "reason": "duplicate"}) == {"id": "dsp_7001"}
    keys = {r.headers["idempotency-key"] for r in seen}
    assert len(seen) == 3 and len(keys) == 1
    assert bank.retry_count == 2


# ---- errors and retries -------------------------------------------------------------------------------------------


def test_4xx_raises_immediately_without_retry() -> None:
    seen: list[httpx.Request] = []
    bank = BankClient(_mock_http([(404, _error_body("NOT_FOUND", "card x not found"))], seen), backoff_s=0)
    with pytest.raises(ServiceError) as caught:
        bank.get("/cards/v1/cards/x")
    assert (caught.value.status, caught.value.code, caught.value.message) == (404, "NOT_FOUND", "card x not found")
    assert len(seen) == 1 and bank.retry_count == 0


def test_4xx_with_unexpected_body() -> None:
    bank = BankClient(_mock_http([(400, "not the error format")], []), backoff_s=0)
    with pytest.raises(ServiceError) as caught:
        bank.get("/cards/v1/cards/x")
    assert (caught.value.status, caught.value.code) == (400, "HTTP_400")


def test_5xx_is_retried_then_raises_unavailable() -> None:
    seen: list[httpx.Request] = []
    bank = BankClient(_mock_http([(500, {})] * 10, seen), retries=3, backoff_s=0)
    with pytest.raises(ServiceError) as caught:
        bank.get("/cards/v1/cards/x")
    assert (caught.value.status, caught.value.code) == (503, "UNAVAILABLE")
    assert len(seen) == 4 and bank.retry_count == 3


def test_transport_errors_are_retried() -> None:
    seen: list[httpx.Request] = []
    flaky = BankClient(_mock_http([httpx.ConnectError("refused"), (200, {"ok": True})], seen), backoff_s=0)
    assert flaky.get("/cards/v1/cards/x") == {"ok": True} and flaky.retry_count == 1
    down = BankClient(_mock_http([httpx.ConnectError("refused")] * 4, []), retries=3, backoff_s=0)
    with pytest.raises(ServiceError) as caught:
        down.get("/cards/v1/cards/x")
    assert caught.value.code == "UNAVAILABLE" and "ConnectError" in caught.value.message


def test_backoff_is_exponential(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", sleeps.append)
    bank = BankClient(_mock_http([(503, {})] * 10, []), retries=3, backoff_s=0.2)
    with pytest.raises(ServiceError):
        bank.get("/cards/v1/cards/x")
    assert sleeps == pytest.approx([0.2, 0.4, 0.8])


# ---- against the real services ------------------------------------------------------------------------------------


def test_service_4xx_is_not_retried(bank: BankClient, store: BankStore) -> None:
    with pytest.raises(ServiceError) as caught:
        bank.get("/accounts/v1/customers/u_9999")
    assert (caught.value.status, caught.value.code) == (404, "NOT_FOUND")
    assert bank.retry_count == 0


def test_outage_is_retried_then_unavailable(bank: BankClient, store: BankStore) -> None:
    store.reset(fault_rate=1.0)
    with pytest.raises(ServiceError) as caught:
        bank.get("/cards/v1/cards/card_1001_debit")
    assert caught.value.code == "UNAVAILABLE"
    assert bank.retry_count == bank.retries and store.faults_injected == bank.retries + 1


@pytest.mark.parametrize("fault", ["after", "before"])
def test_faulted_write_then_retry_applies_exactly_once(bank: BankClient, store: BankStore, fault: str) -> None:
    _script_faults(store, [fault])
    dispute = bank.post("/disputes/v1/disputes", {"transaction_id": "txn_1001_s02", "reason": "duplicate"})
    assert dispute["id"] == "dsp_7001" and bank.retry_count == 1
    matching = [d["id"] for d in store.state["disputes"].values() if d["transaction_id"] == "txn_1001_s02"]
    assert matching == ["dsp_7001"]


def test_retries_of_a_transfer_debit_once(bank: BankClient, store: BankStore) -> None:
    _script_faults(store, ["after", "after"])
    bank.post("/payments/v1/transfers", {"from_account": "acct_1004", "to_account": "acct_9999", "amount": 100})
    assert store.state["accounts"]["acct_1004"]["balance"] == 12550.00
    assert len(store.state["transfers"]) == 1 and bank.retry_count == 2
