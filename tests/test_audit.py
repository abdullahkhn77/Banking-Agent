"""Audit log: redaction, no message text, and one tool_call+confirmation+execution triple per confirmed write."""

from __future__ import annotations

import io
import json

from api.audit import AuditLog
from tests.conftest_api import auth, build, freeze_llm


def _lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_pan_is_redacted_everywhere_in_a_field() -> None:
    buf = io.StringIO()
    log = AuditLog(sink=buf)
    log.emit("tool_call", "r", "s", "u_1001", args={"note": "card 4417 1234 5678 9012 and 4417-1234-5678-9012"})
    [rec] = _lines(buf)
    assert "[REDACTED]" in rec["args"]["note"]
    assert "4417 1234 5678 9012" not in json.dumps(rec)


def test_message_text_is_never_logged_only_its_length() -> None:
    buf = io.StringIO()
    client, _, _ = build(freeze_llm(), audit=AuditLog(sink=buf))
    secret_text = "freeze my card 4111111111111111 please"
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    client.post(f"/v1/sessions/{sid}/messages", json={"text": secret_text}, headers=auth("u_1001"))
    blob = buf.getvalue()
    assert secret_text not in blob and "freeze my card" not in blob
    user_msg = next(r for r in _lines(buf) if r["event"] == "user_message")
    assert user_msg["length"] == len(secret_text) and "text" not in user_msg


def test_every_event_carries_identity_fields() -> None:
    buf = io.StringIO()
    client, _, _ = build(freeze_llm(), audit=AuditLog(sink=buf))
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    client.post(f"/v1/sessions/{sid}/messages", json={"text": "freeze my debit card"}, headers=auth("u_1001"))
    client.post(f"/v1/sessions/{sid}/confirm", json={"approve": True}, headers=auth("u_1001"))
    for rec in _lines(buf):
        assert set(rec) >= {"ts", "event", "request_id", "session_id", "user_id"}
        assert rec["user_id"] == "u_1001" and rec["session_id"] == sid


def test_confirmed_write_has_exactly_one_triple() -> None:
    buf = io.StringIO()
    client, _, _ = build(freeze_llm(), audit=AuditLog(sink=buf))
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    client.post(f"/v1/sessions/{sid}/messages", json={"text": "freeze my debit card"}, headers=auth("u_1001"))
    client.post(f"/v1/sessions/{sid}/confirm", json={"approve": True}, headers=auth("u_1001"))
    recs = _lines(buf)
    freeze_calls = [r for r in recs if r["event"] == "tool_call" and r["tool"] == "freeze_card"]
    confirmations = [r for r in recs if r["event"] == "confirmation"]
    freeze_execs = [r for r in recs if r["event"] == "execution" and r["tool"] == "freeze_card"]
    assert len(freeze_calls) == 1 and len(confirmations) == 1 and len(freeze_execs) == 1
    assert freeze_calls[0]["decision"] == "confirm" and freeze_execs[0]["ok"] is True
    assert confirmations[0]["approved"] == confirmations[0]["requested"]  # the one action was approved


def test_tool_results_are_not_logged() -> None:
    buf = io.StringIO()
    client, _, _ = build(freeze_llm(), audit=AuditLog(sink=buf))
    sid = client.post("/v1/sessions", headers=auth("u_1001")).json()["session_id"]
    client.post(f"/v1/sessions/{sid}/messages", json={"text": "freeze my debit card"}, headers=auth("u_1001"))
    # the execution event records ok/latency/retries, never the tool's returned payload (e.g. status_reason/last4)
    execs = [r for r in _lines(buf) if r["event"] == "execution"]
    assert execs and all("result" not in r and "last4" not in r and "status_reason" not in r for r in execs)
