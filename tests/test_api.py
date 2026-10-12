"""Phase 7 API: auth, sessions, errors, rate limiting, degraded-LLM, per-request retry accounting."""

import threading
import time

import jwt
import pytest
from langchain_core.messages import AIMessage, BaseMessage

from agent.llm import ScriptedLLM
from api.server import ApiConfig
from tests.conftest_api import SECRET, auth, build, freeze_llm

# ---- auth ---------------------------------------------------------------------------------------------------------


def test_missing_token_is_401() -> None:
    client, _, _ = build()
    assert client.post("/v1/sessions").status_code == 401


@pytest.mark.parametrize(
    "header",
    ["", "Bearer ", "Token abc", "Bearer not.a.jwt"],
)
def test_malformed_auth_is_401(header: str) -> None:
    client, _, _ = build()
    assert client.post("/v1/sessions", headers={"Authorization": header}).status_code == 401


def test_rejects_alg_none() -> None:
    client, _, _ = build()
    bad = jwt.encode({"sub": "u_1001", "exp": int(time.time()) + 300}, "", algorithm="none")
    assert client.post("/v1/sessions", headers={"Authorization": f"Bearer {bad}"}).status_code == 401


def test_rejects_wrong_secret() -> None:
    client, _, _ = build()
    assert (
        client.post("/v1/sessions", headers=auth("u_1001", secret="the-wrong-secret-the-wrong-secret!")).status_code
        == 401
    )


def test_rejects_missing_exp() -> None:
    client, _, _ = build()
    tok = jwt.encode({"sub": "u_1001"}, SECRET, algorithm="HS256")  # no exp
    assert client.post("/v1/sessions", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_rejects_missing_sub() -> None:
    client, _, _ = build()
    tok = jwt.encode({"exp": int(time.time()) + 300}, SECRET, algorithm="HS256")
    assert client.post("/v1/sessions", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_rejects_expired_token() -> None:
    client, _, _ = build()
    assert client.post("/v1/sessions", headers=auth("u_1001", ttl=-10)).status_code == 401


def test_config_from_env_refuses_without_secret() -> None:
    with pytest.raises(RuntimeError, match="AUTH_SECRET"):
        ApiConfig.from_env({})
    # DEV_MODE lets it start with a fixed dev secret
    assert ApiConfig.from_env({"DEV_MODE": "1"}).auth_secret
    assert ApiConfig.from_env({"AUTH_SECRET": "x" * 32}).auth_secret == "x" * 32


# ---- session flow -------------------------------------------------------------------------------------------------


def test_full_freeze_flow() -> None:
    client, store, _ = build(freeze_llm())
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    r = client.post(
        f"/v1/sessions/{sid}/messages", json={"text": "lost my debit card, freeze it"}, headers=auth("u_1001")
    )
    body = r.json()
    assert body["status"] == "awaiting_confirmation" and body["degraded"] is False
    assert body["confirmation"]["actions"][0]["tool"] == "freeze_card"
    assert store.snapshot()["cards"]["card_1001_debit"] == "active"  # not yet
    done = client.post(f"/v1/sessions/{sid}/confirm", json={"approve": True}, headers=auth("u_1001")).json()
    assert done["status"] == "complete" and "frozen" in done["reply"]
    assert store.snapshot()["cards"]["card_1001_debit"] == "frozen"


def test_get_session_reports_turns_and_pending() -> None:
    client, _, _ = build(freeze_llm())
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    client.post(f"/v1/sessions/{sid}/messages", json={"text": "freeze my debit card"}, headers=auth("u_1001"))
    info = client.get(f"/v1/sessions/{sid}", headers=auth("u_1001")).json()
    assert info["turns"] == 1 and info["confirmation"] is not None and "actions" in info["confirmation"]


# ---- ownership and error codes ------------------------------------------------------------------------------------


def test_other_users_session_is_404_same_as_missing() -> None:
    client, _, _ = build(freeze_llm())
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    owned_other = client.get(f"/v1/sessions/{sid}", headers=auth("u_1002"))
    missing = client.get("/v1/sessions/deadbeef", headers=auth("u_1002"))
    assert owned_other.status_code == missing.status_code == 404
    assert owned_other.json() == missing.json()  # indistinguishable: can't probe sessions


def test_message_while_pending_is_409() -> None:
    client, _, _ = build(freeze_llm())
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    client.post(f"/v1/sessions/{sid}/messages", json={"text": "freeze it"}, headers=auth("u_1001"))
    r = client.post(f"/v1/sessions/{sid}/messages", json={"text": "another"}, headers=auth("u_1001"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CONFIRMATION_PENDING"


def test_confirm_with_nothing_pending_is_409() -> None:
    client, _, _ = build(ReplayLLMProxy())
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    client.post(f"/v1/sessions/{sid}/messages", json={"text": "hi"}, headers=auth("u_1001"))
    r = client.post(f"/v1/sessions/{sid}/confirm", json={"approve": True}, headers=auth("u_1001"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOTHING_PENDING"


def test_validation_errors_422() -> None:
    client, _, _ = build()
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    assert client.post(f"/v1/sessions/{sid}/messages", json={"text": ""}, headers=auth("u_1001")).status_code == 422
    assert (
        client.post(f"/v1/sessions/{sid}/messages", json={"text": "x" * 2001}, headers=auth("u_1001")).status_code
        == 422
    )
    assert client.post(f"/v1/sessions/{sid}/confirm", json={}, headers=auth("u_1001")).status_code in (409, 422)


# ---- rate limiting ------------------------------------------------------------------------------------------------


def test_rate_limit_returns_429() -> None:
    client, _, _ = build(config=ApiConfig(auth_secret=SECRET, rate_limit=3, rate_window_s=60))
    codes = [client.post("/v1/sessions", headers=auth("u_1001")).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429 and codes[4] == 429
    # a different user is unaffected
    assert client.post("/v1/sessions", headers=auth("u_1002")).status_code == 200


# ---- degraded LLM + readiness -------------------------------------------------------------------------------------


def test_llm_outage_returns_200_degraded_not_503() -> None:
    def policy(_: list[BaseMessage]) -> AIMessage:
        raise TimeoutError("llm down")

    client, store, _ = build(ScriptedLLM(policy))
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    r = client.post(f"/v1/sessions/{sid}/messages", json={"text": "freeze my card"}, headers=auth("u_1001"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "complete" and body["degraded"] is True and body["reply"]  # fallback reply returned


def test_readyz_503_when_llm_unreachable() -> None:
    client, _, _ = build(readiness=lambda: {"bank": True, "llm": False})
    assert client.get("/readyz").status_code == 503
    assert client.get("/healthz").status_code == 200


def test_readyz_200_when_all_ready() -> None:
    client, _, _ = build(readiness=lambda: {"bank": True, "llm": True})
    assert client.get("/readyz").status_code == 200


# ---- per-session concurrency and per-request retry accounting -----------------------------------------------------


def test_concurrent_requests_on_one_session_conflict() -> None:
    import time as _t

    def slow(messages: list[BaseMessage]) -> AIMessage:
        _t.sleep(0.3)
        return AIMessage(content="done")

    client, _, _ = build(ScriptedLLM(slow))
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    results: list[int] = []

    def hit() -> None:
        results.append(
            client.post(f"/v1/sessions/{sid}/messages", json={"text": "hi"}, headers=auth("u_1001")).status_code
        )

    threads = [threading.Thread(target=hit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 409]  # one ran, one got SESSION_BUSY


def test_retry_accounting_is_per_request_across_concurrent_sessions() -> None:
    """Two threads share ONE BankClient; the contextvar must attribute retries to the right request.

    A mock transport makes the disputes endpoint fail twice then succeed (2 retries), and the cards endpoint always
    succeed (0 retries). Driven concurrently in retry_scope contexts; each scope must see only its own retries.
    """
    import httpx

    from agent.client import BankClient, retry_scope

    def handler(request: httpx.Request) -> httpx.Response:
        if "/disputes/" in request.url.path:
            handler.dispute_hits += 1  # type: ignore[attr-defined]
            if handler.dispute_hits <= 2:  # type: ignore[attr-defined]
                return httpx.Response(503, json={})
            return httpx.Response(201, json={"id": "dsp_7001"})
        return httpx.Response(200, json={"ok": True})

    handler.dispute_hits = 0  # type: ignore[attr-defined]
    bank = BankClient(httpx.Client(transport=httpx.MockTransport(handler), base_url="http://t"), backoff_s=0)
    out: dict[str, int] = {}
    started = threading.Barrier(2)

    def dispute() -> None:
        started.wait()
        with retry_scope() as retries:
            bank.post("/disputes/v1/disputes", {"transaction_id": "t", "reason": "duplicate"})
            out["dispute"] = retries()

    def read() -> None:
        started.wait()
        with retry_scope() as retries:
            for _ in range(5):
                bank.get("/cards/v1/cards/card_1001_debit")
            out["read"] = retries()

    ta, tb = threading.Thread(target=dispute), threading.Thread(target=read)
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    assert out["dispute"] == 2 and out["read"] == 0  # isolated despite a shared client and lifetime retry_count
    assert bank.retry_count == 2  # lifetime total still counts the 2 real retries


class ReplayLLMProxy:
    """A one-shot plain-reply LLM (no tool calls) for the 'nothing pending' test."""

    def invoke(self, messages: list[BaseMessage], tools: list[dict]) -> AIMessage:
        return AIMessage(content="Hello, how can I help?")
