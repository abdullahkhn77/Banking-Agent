"""Runtime settings, read from environment variables (no secrets in code)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from agent.client import SERVICES

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    llm_base_url: str = "http://localhost:8000/v1"
    llm_model: str = "bank-agent-llm"
    llm_api_key: str = "EMPTY"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 512
    llm_timeout_s: float = 60.0
    bank_base_url: str | None = None  # None: run the bank services in-process
    service_urls: dict[str, str] = field(default_factory=dict)  # per-service overrides, e.g. {"cards": "http://..."}
    http_retries: int = 3
    http_backoff_s: float = 0.2
    guardrails_enabled: bool = True
    max_writes_per_session: int = 6
    max_agent_steps: int = 10

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        defaults = cls()
        return cls(
            llm_base_url=env.get("LLM_BASE_URL", defaults.llm_base_url),
            llm_model=env.get("LLM_MODEL", defaults.llm_model),
            llm_api_key=env.get("LLM_API_KEY", defaults.llm_api_key),
            llm_temperature=float(env.get("LLM_TEMPERATURE", defaults.llm_temperature)),
            llm_max_tokens=int(env.get("LLM_MAX_TOKENS", defaults.llm_max_tokens)),
            llm_timeout_s=float(env.get("LLM_TIMEOUT_S", defaults.llm_timeout_s)),
            bank_base_url=env.get("BANK_BASE_URL") or None,
            service_urls={
                name: env[f"{name.upper()}_SERVICE_URL"] for name in SERVICES if env.get(f"{name.upper()}_SERVICE_URL")
            },
            http_retries=int(env.get("HTTP_RETRIES", defaults.http_retries)),
            http_backoff_s=float(env.get("HTTP_BACKOFF_S", defaults.http_backoff_s)),
            guardrails_enabled=_parse_bool(env.get("GUARDRAILS_ENABLED"), defaults.guardrails_enabled),
            max_writes_per_session=int(env.get("MAX_WRITES_PER_SESSION", defaults.max_writes_per_session)),
            max_agent_steps=int(env.get("MAX_AGENT_STEPS", defaults.max_agent_steps)),
        )


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise ValueError(f"expected a boolean (true/false), got {value!r}")
