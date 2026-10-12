"""Shared helpers for the Phase 7 API tests."""

from __future__ import annotations

import time
from typing import Any

import jwt
from fastapi.testclient import TestClient

from agent.client import BankClient
from agent.graph import BankingAgent
from agent.llm import LLMBackend, ReplayLLM
from api.audit import AuditLog
from api.server import ApiConfig, create_api
from bank_services.app import create_app
from bank_services.store import BankStore

SECRET = "test-secret-test-secret-test-secret!"  # >= 32 bytes, silences the PyJWT short-key warning


def token(user: str, ttl: int = 300, secret: str = SECRET, **extra: Any) -> str:
    claims = {"sub": user, "exp": int(time.time()) + ttl, **extra}
    return jwt.encode(claims, secret, algorithm="HS256")


def auth(user: str, **kw: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(user, **kw)}"}


def build(
    llm: LLMBackend | None = None,
    store: BankStore | None = None,
    config: ApiConfig | None = None,
    audit: AuditLog | None = None,
    readiness: Any = None,
) -> tuple[TestClient, BankStore, BankingAgent]:
    store = store or BankStore()
    bank = BankClient(TestClient(create_app(store)), backoff_s=0)
    agent = BankingAgent(llm or ReplayLLM([{"content": "hello"}]), bank)
    api = create_api(agent, config or ApiConfig(auth_secret=SECRET), audit=audit, readiness=readiness)
    return TestClient(api), store, agent


def freeze_llm() -> ReplayLLM:
    return ReplayLLM(
        [
            {"tool_calls": [{"name": "list_cards", "args": {}}]},
            {"tool_calls": [{"name": "freeze_card", "args": {"card_id": "card_1001_debit", "reason": "lost"}}]},
            {"content": "Your debit card ending 4417 is now frozen."},
        ]
    )
