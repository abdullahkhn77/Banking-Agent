"""Hard rule: the prompt, tool schemas and model-facing messages contain no scenario-specific seed content."""

import re
from pathlib import Path

import pytest

from bank_services.data import build_seed

ROOT = Path(__file__).resolve().parents[1]
# Every file that defines text the model can see: system prompt, tool/argument descriptions, guardrail and
# execution error messages.
MODEL_FACING_SOURCES = ["agent/prompts.py", "agent/tools.py", "agent/guardrails.py", "agent/graph.py"]


def _forbidden_terms() -> list[str]:
    seed = build_seed()
    terms: set[str] = set()
    terms.update(seed["customers"], seed["accounts"], seed["cards"], seed["transactions"], seed["disputes"])
    terms.update(card["last4"] for card in seed["cards"].values())
    for customer in seed["customers"].values():
        terms.add(customer["name"])
        terms.update(customer["name"].split())
    planted = [t for t in seed["transactions"].values() if not re.search(r"_n\d{3}$", t["id"])]
    for txn in seed["transactions"].values():
        terms.add(txn["merchant"])
        terms.add(txn["merchant"].split()[0])
    amounts = [t["amount"] for t in planted] + [a["balance"] for a in seed["accounts"].values()]
    amounts += [d["amount"] for d in seed["disputes"].values()]
    for amount in amounts:
        terms.update({f"{amount:.2f}", f"{amount:,.2f}"})
    terms.update({"Romania", "Nigeria"})
    return sorted(terms)


def _hits(text: str) -> list[str]:
    return [term for term in _forbidden_terms() if re.search(rf"(?<![\w.]){re.escape(term)}(?![\w])", text)]


def test_the_leakage_check_detects_seed_content() -> None:
    assert _hits("call list_cards, e.g. card_1001_debit") == ["card_1001_debit"]
    assert _hits("The StreamFlix charge of $15.99") == ["15.99", "StreamFlix"]
    assert _hits("use a placeholder such as card_XXXX_debit or $NN.NN") == []


@pytest.mark.parametrize("source", MODEL_FACING_SOURCES)
def test_model_facing_text_has_no_seed_content(source: str) -> None:
    assert _hits((ROOT / source).read_text(encoding="utf-8")) == []
