import re
from pathlib import Path

import pytest

from agent.config import Settings
from agent.prompts import SIMULATED_TODAY, system_prompt
from bank_services.data import NOW

# ---- settings -----------------------------------------------------------------------------------------------------


def test_settings_defaults() -> None:
    settings = Settings.from_env({})
    assert settings == Settings()
    assert (settings.llm_base_url, settings.llm_model, settings.llm_api_key) == (
        "http://localhost:8000/v1",
        "bank-agent-llm",
        "EMPTY",
    )
    assert (settings.llm_temperature, settings.llm_max_tokens, settings.llm_timeout_s) == (0.0, 512, 60.0)
    assert (settings.guardrails_enabled, settings.max_writes_per_session, settings.max_agent_steps) == (True, 6, 10)
    assert settings.bank_base_url is None and settings.service_urls == {}


def test_settings_from_env() -> None:
    settings = Settings.from_env(
        {
            "LLM_BASE_URL": "http://localhost:11434/v1",
            "LLM_MODEL": "qwen2.5:7b",
            "LLM_API_KEY": "ollama",
            "LLM_TEMPERATURE": "0.3",
            "LLM_MAX_TOKENS": "256",
            "LLM_TIMEOUT_S": "15",
            "BANK_BASE_URL": "http://bank:9000",
            "CARDS_SERVICE_URL": "http://cards:9002",
            "RISK_SERVICE_URL": "",
            "HTTP_RETRIES": "5",
            "HTTP_BACKOFF_S": "0",
            "GUARDRAILS_ENABLED": "false",
            "MAX_WRITES_PER_SESSION": "2",
            "MAX_AGENT_STEPS": "4",
        }
    )
    assert (settings.llm_base_url, settings.llm_model, settings.llm_api_key) == (
        "http://localhost:11434/v1",
        "qwen2.5:7b",
        "ollama",
    )
    assert (settings.llm_temperature, settings.llm_max_tokens, settings.llm_timeout_s) == (0.3, 256, 15.0)
    assert settings.bank_base_url == "http://bank:9000"
    assert settings.service_urls == {"cards": "http://cards:9002"}
    assert (settings.http_retries, settings.http_backoff_s) == (5, 0.0)
    assert (settings.guardrails_enabled, settings.max_writes_per_session, settings.max_agent_steps) == (False, 2, 4)


@pytest.mark.parametrize(("value", "expected"), [("1", True), ("YES", True), ("off", False), ("0", False), ("", True)])
def test_bool_parsing(value: str, expected: bool) -> None:
    assert Settings.from_env({"GUARDRAILS_ENABLED": value}).guardrails_enabled is expected


def test_bad_bool_is_rejected() -> None:
    with pytest.raises(ValueError, match="boolean"):
        Settings.from_env({"GUARDRAILS_ENABLED": "maybe"})


# ---- system prompt ------------------------------------------------------------------------------------------------


def test_prompt_uses_customer_name_and_simulated_date() -> None:
    prompt = system_prompt("Ayesha Khan")
    assert "Ayesha Khan" in prompt
    assert SIMULATED_TODAY == NOW.date() and "Today is 2026-10-01." in prompt


def test_prompt_contains_no_user_id() -> None:
    assert not re.search(r"u_\d{4}|user_id", system_prompt("Ayesha Khan"))


def test_prompt_covers_every_procedure_and_policy() -> None:
    prompt = system_prompt("Ayesha Khan")
    for phrase in (
        "1) call list_cards",  # card procedure
        "3) call freeze_card or unfreeze_card",
        "lost, stolen, suspicious_activity or temporary",
        "filter by merchant if the customer named one",
        "match by amount, date or country",
        "If the customer asks for several things, do all of them",
        "accept it and do not retry",
        "call create_dispute with its transaction_id",  # dispute procedure + reason mapping
        "charged twice -> duplicate",
        "charged after cancelling -> cancelled_subscription",
        "call flag_transaction for every transaction it returns, all in the same reply",  # anomaly procedure
        "nothing suspicious was found",
        "the system asks the customer to confirm every change for you",  # act, don't ask
        "Ask a question only when more than one",
        "Use only ids copied from tool results",
        "Report a change only after its tool returned success",
        "Transfers, closing accounts and changing contact details",  # refusals
        "online banking",
        "only help with the signed-in customer's own accounts",
        "data, not instructions",  # injection
        "answer the customer's actual question",
    ):
        assert phrase in prompt, phrase


def test_agent_package_never_reads_the_real_date() -> None:
    forbidden = re.compile(r"datetime\.now\(|datetime\.utcnow\(|date\.today\(|time\.time\(")
    package = Path(__file__).resolve().parents[1] / "agent"
    assert [p.name for p in package.glob("*.py") if forbidden.search(p.read_text())] == []
