from datetime import datetime, timedelta
from typing import Any

import pytest

from bank_services.data import EXPECTED_ANOMALIES, NOW, build_seed
from bank_services.risk import detect_anomalies


def _txn(txn_id: str, at: datetime, amount: float, merchant: str = "Local Shop", country: str = "US") -> dict[str, Any]:
    return {"id": txn_id, "posted_at": at.isoformat(), "amount": amount, "merchant": merchant, "country": country}


def _baseline(amount: float = 20.0, count: int = 10) -> list[dict[str, Any]]:
    """Ordinary domestic daytime spending, one charge per day at different merchants."""
    return [
        _txn(f"base_{k:02d}", (NOW - timedelta(days=k + 1)).replace(hour=13), amount, f"Merchant {k}")
        for k in range(count)
    ]


def _ids(anomalies: list[dict[str, Any]]) -> list[str]:
    return [a["transaction_id"] for a in anomalies]


def _account_txns(account_id: str) -> list[dict[str, Any]]:
    return [t for t in build_seed()["transactions"].values() if t["account_id"] == account_id]


# ---- ground truth on the seed -------------------------------------------------------------------------------------


@pytest.mark.parametrize("account_id", sorted(EXPECTED_ANOMALIES))
def test_seed_matches_expected_anomalies(account_id: str) -> None:
    assert tuple(_ids(detect_anomalies(_account_txns(account_id), NOW))) == EXPECTED_ANOMALIES[account_id]


def test_no_background_transaction_is_flagged() -> None:
    for account_id in EXPECTED_ANOMALIES:
        flagged = _ids(detect_anomalies(_account_txns(account_id), NOW))
        assert [txn_id for txn_id in flagged if "_n" in txn_id] == []


def test_planted_reasons_and_scores() -> None:
    by_id = {
        a["transaction_id"]: (a["reasons"], a["risk_score"])
        for account_id in EXPECTED_ANOMALIES
        for a in detect_anomalies(_account_txns(account_id), NOW)
    }
    assert by_id["txn_1001_a01"] == (["FOREIGN", "LARGE_AMOUNT", "ODD_HOUR_HIGH"], 1.0)
    assert by_id["txn_1004_a01"] == (["LARGE_AMOUNT"], 0.5)
    for i in range(1, 6):
        assert by_id[f"txn_1002_v0{i}"] == (["VELOCITY"], 0.6)
    for i in range(1, 5):
        assert by_id[f"txn_1006_g0{i}"] == (["FOREIGN", "ODD_HOUR_HIGH", "VELOCITY"], 1.0)


def test_output_shape() -> None:
    anomaly = detect_anomalies(_account_txns("acct_1001"), NOW)[0]
    assert anomaly == {
        "transaction_id": "txn_1001_a01",
        "merchant": "Elektro-Mag Bucuresti",
        "amount": 2450.00,
        "posted_at": "2026-09-29T03:12:00+00:00",
        "country": "RO",
        "reasons": ["FOREIGN", "LARGE_AMOUNT", "ODD_HOUR_HIGH"],
        "risk_score": 1.0,
    }


# ---- each rule in isolation ---------------------------------------------------------------------------------------

DAYTIME = (NOW - timedelta(days=3)).replace(hour=14, minute=0)
NIGHT = (NOW - timedelta(days=3)).replace(hour=3, minute=0)


def test_clean_baseline_has_no_anomalies() -> None:
    assert detect_anomalies(_baseline(), NOW) == []


def test_empty_input() -> None:
    assert detect_anomalies([], NOW) == []


def test_large_amount() -> None:
    result = detect_anomalies([*_baseline(), _txn("big", DAYTIME, 1500.0)], NOW)
    assert [(a["transaction_id"], a["reasons"], a["risk_score"]) for a in result] == [("big", ["LARGE_AMOUNT"], 0.5)]


def test_large_amount_needs_1000_minimum() -> None:
    assert detect_anomalies([*_baseline(amount=5.0), _txn("big", DAYTIME, 999.99)], NOW) == []


def test_large_amount_needs_8x_median() -> None:
    # median is 200, so 1500 < 8 x 200 = 1600 is not large
    assert detect_anomalies([*_baseline(amount=200.0), _txn("big", DAYTIME, 1500.0)], NOW) == []
    result = detect_anomalies([*_baseline(amount=200.0), _txn("big", DAYTIME, 1600.0)], NOW)
    assert _ids(result) == ["big"]


def test_foreign() -> None:
    result = detect_anomalies([*_baseline(), _txn("abroad", DAYTIME, 25.0, country="GB")], NOW)
    assert [(a["transaction_id"], a["reasons"], a["risk_score"]) for a in result] == [("abroad", ["FOREIGN"], 0.3)]


def test_odd_hour_high() -> None:
    result = detect_anomalies([*_baseline(), _txn("night", NIGHT, 250.0)], NOW)
    assert [(a["transaction_id"], a["reasons"], a["risk_score"]) for a in result] == [("night", ["ODD_HOUR_HIGH"], 0.3)]


@pytest.mark.parametrize(
    ("hour", "minute", "amount", "flagged"),
    [
        (0, 0, 200.0, True),
        (4, 59, 250.0, True),
        (5, 0, 250.0, False),
        (3, 0, 199.99, False),
        (23, 30, 250.0, False),
    ],
)
def test_odd_hour_high_edges(hour: int, minute: int, amount: float, flagged: bool) -> None:
    at = (NOW - timedelta(days=3)).replace(hour=hour, minute=minute)
    result = detect_anomalies([*_baseline(), _txn("t", at, amount)], NOW)
    assert (_ids(result) == ["t"]) is flagged


def _burst(minutes: list[int], merchant: str = "Burst Store") -> list[dict[str, Any]]:
    start = (NOW - timedelta(days=2)).replace(hour=15, minute=0)
    return [_txn(f"b{k}", start + timedelta(minutes=m), 1.0, merchant) for k, m in enumerate(minutes)]


def test_velocity_four_charges_within_15_minutes() -> None:
    result = detect_anomalies([*_baseline(), *_burst([0, 5, 10, 15])], NOW)
    assert _ids(result) == ["b0", "b1", "b2", "b3"]
    assert all(a["reasons"] == ["VELOCITY"] and a["risk_score"] == 0.6 for a in result)


def test_velocity_four_charges_over_16_minutes_not_flagged() -> None:
    assert detect_anomalies([*_baseline(), *_burst([0, 5, 10, 16])], NOW) == []


def test_velocity_three_charges_not_flagged() -> None:
    assert detect_anomalies([*_baseline(), *_burst([0, 1, 2])], NOW) == []


def test_velocity_flags_every_charge_in_a_longer_burst() -> None:
    result = detect_anomalies([*_baseline(), *_burst([0, 1, 2, 3, 4, 40])], NOW)
    assert _ids(result) == ["b0", "b1", "b2", "b3", "b4"]


def test_velocity_counts_one_merchant_at_a_time() -> None:
    mixed = [*_burst([0, 1], "Shop A"), *_burst([2, 3], "Shop B")]
    for k, txn in enumerate(mixed):
        txn["id"] = f"m{k}"
    assert detect_anomalies([*_baseline(), *mixed], NOW) == []


# ---- window, scoring and ordering ---------------------------------------------------------------------------------


def test_transactions_outside_window_are_ignored() -> None:
    old = _txn("old", (NOW - timedelta(days=91)).replace(hour=14), 5000.0)
    assert detect_anomalies([*_baseline(), old], NOW, days=90) == []
    assert _ids(detect_anomalies([*_baseline(), old], NOW, days=120)) == ["old"]


def test_future_transactions_are_ignored() -> None:
    future = _txn("future", NOW + timedelta(hours=1), 5000.0, country="GB")
    assert detect_anomalies([*_baseline(), future], NOW) == []


def test_score_is_capped_and_sorted() -> None:
    txns = [
        *_baseline(),
        _txn("all_rules", NIGHT, 3000.0, country="RO"),  # 0.5 + 0.3 + 0.3 = 1.1 -> capped at 1.0
        _txn("foreign_night", NIGHT + timedelta(hours=1), 300.0, country="NG"),  # 0.3 + 0.3
        _txn("z_foreign", DAYTIME, 30.0, country="GB"),  # 0.3
        _txn("a_foreign", DAYTIME + timedelta(hours=1), 30.0, country="GB"),  # 0.3, sorts before z_foreign
    ]
    result = detect_anomalies(txns, NOW)
    assert [(a["transaction_id"], a["risk_score"]) for a in result] == [
        ("all_rules", 1.0),
        ("foreign_night", 0.6),
        ("a_foreign", 0.3),
        ("z_foreign", 0.3),
    ]
