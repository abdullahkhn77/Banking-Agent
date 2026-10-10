"""Thread-safe in-memory state for the simulated bank."""

from __future__ import annotations

import copy
import random
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from bank_services.data import NOW, build_seed

Fault = Literal["before", "after"]
FaultMode = Literal["random", "before", "after"]
FAULT_MODES: tuple[str, ...] = ("random", "before", "after")
Response = tuple[int, dict[str, Any]]


class BankStore:
    """Bank state built from ``build_seed()`` plus fault-injection and idempotency bookkeeping.

    Every read or write of ``state`` must happen while holding ``lock``.
    """

    def __init__(
        self, fault_rate: float = 0.0, latency_ms: int = 0, seed: int = 0, fault_mode: FaultMode = "random"
    ) -> None:
        self.lock = threading.RLock()
        self.now: datetime = NOW
        self.reset(fault_rate=fault_rate, latency_ms=latency_ms, seed=seed, fault_mode=fault_mode)

    def reset(
        self, fault_rate: float = 0.0, latency_ms: int = 0, seed: int = 0, fault_mode: FaultMode = "random"
    ) -> None:
        """Rebuild the seed state, clear idempotency records, and reconfigure fault injection."""
        if not 0.0 <= fault_rate <= 1.0:
            raise ValueError(f"fault_rate must be in [0, 1], got {fault_rate}")
        if latency_ms < 0:
            raise ValueError(f"latency_ms must be >= 0, got {latency_ms}")
        if fault_mode not in FAULT_MODES:
            raise ValueError(f"fault_mode must be one of {FAULT_MODES}, got {fault_mode!r}")
        with self.lock:
            self.state: dict[str, Any] = build_seed()
            self.fault_rate = fault_rate
            self.latency_ms = latency_ms
            self.fault_mode: FaultMode = fault_mode
            self.faults_injected = 0
            self._rng = random.Random(seed)
            self._idempotency: dict[tuple[str, str], Response] = {}

    def roll_fault(self) -> Fault | None:
        """Decide whether the current request fails, and whether before or after applying its change."""
        with self.lock:
            if self.fault_rate <= 0.0 or self._rng.random() >= self.fault_rate:
                return None
            fault: Fault = self._rng.choice(("before", "after")) if self.fault_mode == "random" else self.fault_mode
            self.faults_injected += 1
            return fault

    def next_id(self, kind: str, prefix: str) -> str:
        with self.lock:
            value = self.state["counters"][kind]
            self.state["counters"][kind] = value + 1
            return f"{prefix}_{value}"

    def txn_age_days(self, txn: dict[str, Any]) -> int:
        """Whole days between the transaction's posting time and ``now`` (rounded down)."""
        return (self.now - datetime.fromisoformat(txn["posted_at"])).days

    def owner_of_account(self, account_id: str) -> str | None:
        with self.lock:
            account = self.state["accounts"].get(account_id)
            return account["user_id"] if account else None

    def account_transactions(self, account_id: str) -> list[dict[str, Any]]:
        with self.lock:
            return [t for t in self.state["transactions"].values() if t["account_id"] == account_id]

    def run_idempotent(self, scope: str, key: str | None, operation: Callable[[], Response]) -> Response:
        """Run ``operation`` once per (scope, key); a repeated key returns the stored response unchanged.

        Server errors (5xx) are not stored, so a retry after a failure runs the operation again.
        """
        with self.lock:
            if key is not None and (scope, key) in self._idempotency:
                return copy.deepcopy(self._idempotency[(scope, key)])
            status, body = operation()
            if key is not None and status < 500:
                self._idempotency[(scope, key)] = (status, copy.deepcopy(body))
            return status, body

    def snapshot(self) -> dict[str, Any]:
        """Compact copy of the state-changing parts of the bank, used to diff end state in evals."""
        with self.lock:
            s = self.state
            return {
                "cards": {card_id: card["status"] for card_id, card in s["cards"].items()},
                "disputes": {d["transaction_id"]: d["reason"] for d in s["disputes"].values()},
                "flags": {f["transaction_id"]: f["reason"] for f in s["flags"].values()},
                "transfers": [(t["from_account"], t["to_account"], t["amount"]) for t in s["transfers"].values()],
                "accounts": {account_id: account["status"] for account_id, account in s["accounts"].items()},
                "contacts": {user_id: customer["email"] for user_id, customer in s["customers"].items()},
            }


STORE = BankStore()
