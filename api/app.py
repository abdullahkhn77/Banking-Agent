"""Host entrypoint for the agent API: builds the app from env and the in-process bank, for `uvicorn api.app:app`.

    AUTH_SECRET=... LLM_BASE_URL=... LLM_MODEL=... uvicorn api.app:app --port 8080

The bank services run in-process (TestClient) unless BANK_BASE_URL is set. Readiness pings the LLM /models endpoint.
Containerising this is Phase 8; here it is used for local verification and the demo.
"""

from __future__ import annotations

import os

import httpx

from agent.client import BankClient
from agent.config import Settings
from agent.graph import BankingAgent
from agent.llm import OpenAICompatLLM
from api.audit import AuditLog
from api.server import ApiConfig, create_api


def _bank_client(settings: Settings) -> BankClient:
    if settings.bank_base_url or settings.service_urls:
        http: httpx.Client = httpx.Client(base_url=settings.bank_base_url or "http://localhost:9000", timeout=10.0)
    else:
        from fastapi.testclient import TestClient

        from bank_services.app import create_app
        from bank_services.store import BankStore

        http = TestClient(create_app(BankStore()))
    return BankClient(http, settings.service_urls, retries=settings.http_retries, backoff_s=settings.http_backoff_s)


def _readiness(settings: Settings, client: BankClient) -> dict[str, bool]:
    checks = {}
    try:
        client.get("/accounts/v1/customers/u_1001")
        checks["bank"] = True
    except Exception:
        checks["bank"] = False
    try:
        host = settings.llm_base_url.rstrip("/")
        httpx.get(f"{host}/models", timeout=2).raise_for_status()
        checks["llm"] = True
    except Exception:
        checks["llm"] = False
    return checks


def build_app():  # noqa: ANN201 (FastAPI app)
    settings = Settings.from_env()
    config = ApiConfig.from_env(os.environ)
    client = _bank_client(settings)
    agent = BankingAgent(OpenAICompatLLM.from_settings(settings), client, max_steps=settings.max_agent_steps)
    audit = AuditLog(path=os.environ.get("AUDIT_LOG_PATH"))
    return create_api(agent, config, audit=audit, readiness=lambda: _readiness(settings, client))


app = build_app()
