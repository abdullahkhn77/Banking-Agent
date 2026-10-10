"""Rule-based anomaly detector. Pure function: no I/O, no clock, no shared state."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from bank_services.data import HOME_COUNTRY

LARGE_AMOUNT_MIN = 1000.0
LARGE_AMOUNT_MEDIAN_MULTIPLE = 8.0
VELOCITY_MIN_CHARGES = 4
VELOCITY_WINDOW = timedelta(minutes=15)  # inclusive: last - first <= 15 min
ODD_HOURS = range(0, 5)  # 00:00-04:59 UTC
ODD_HOUR_MIN_AMOUNT = 200.0

WEIGHTS: dict[str, float] = {"LARGE_AMOUNT": 0.5, "FOREIGN": 0.3, "VELOCITY": 0.6, "ODD_HOUR_HIGH": 0.3}


def detect_anomalies(txns: Iterable[Mapping[str, Any]], now: datetime, days: int = 90) -> list[dict[str, Any]]:
    """Score one account's transactions posted in the ``days`` days before ``now``.

    The window's median amount is the spending baseline. Returns one entry per transaction that triggers at least
    one rule, sorted by risk score (descending), then transaction id.
    """
    start = now - timedelta(days=days)
    window = [(txn, datetime.fromisoformat(txn["posted_at"])) for txn in txns]
    window = [(txn, posted) for txn, posted in window if start <= posted <= now]
    if not window:
        return []

    median = statistics.median(txn["amount"] for txn, _ in window)
    reasons: dict[str, set[str]] = defaultdict(set)
    for txn, posted in window:
        amount = txn["amount"]
        if amount >= LARGE_AMOUNT_MIN and amount >= LARGE_AMOUNT_MEDIAN_MULTIPLE * median:
            reasons[txn["id"]].add("LARGE_AMOUNT")
        if txn["country"] != HOME_COUNTRY:
            reasons[txn["id"]].add("FOREIGN")
        if posted.hour in ODD_HOURS and amount >= ODD_HOUR_MIN_AMOUNT:
            reasons[txn["id"]].add("ODD_HOUR_HIGH")
    for txn_id in _velocity_bursts(window):
        reasons[txn_id].add("VELOCITY")

    by_id = {txn["id"]: txn for txn, _ in window}
    anomalies = [
        {
            "transaction_id": txn_id,
            "merchant": by_id[txn_id]["merchant"],
            "amount": by_id[txn_id]["amount"],
            "posted_at": by_id[txn_id]["posted_at"],
            "country": by_id[txn_id]["country"],
            "reasons": sorted(rules),
            "risk_score": round(min(1.0, sum(WEIGHTS[rule] for rule in rules)), 2),
        }
        for txn_id, rules in reasons.items()
    ]
    anomalies.sort(key=lambda a: (-a["risk_score"], a["transaction_id"]))
    return anomalies


def _velocity_bursts(window: list[tuple[Mapping[str, Any], datetime]]) -> set[str]:
    """Ids of every charge in a run of >= VELOCITY_MIN_CHARGES charges at one merchant within VELOCITY_WINDOW."""
    by_merchant: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for txn, posted in window:
        by_merchant[txn["merchant"]].append((posted, txn["id"]))

    flagged: set[str] = set()
    for charges in by_merchant.values():
        charges.sort()
        first = 0
        for last in range(len(charges)):
            while charges[last][0] - charges[first][0] > VELOCITY_WINDOW:
                first += 1
            if last - first + 1 >= VELOCITY_MIN_CHARGES:
                flagged.update(txn_id for _, txn_id in charges[first : last + 1])
    return flagged
