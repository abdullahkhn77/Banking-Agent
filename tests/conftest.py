import statistics
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.client import BankClient
from agent.guardrails import GuardConfig, GuardrailEngine, GuardResult
from agent.tools import Ctx
from bank_services.app import create_app
from bank_services.data import build_seed
from bank_services.store import BankStore

# Collected during the run and printed in the terminal summary (test diagnostics, not reported metrics).
GUARD_CHECKS: list[tuple[bool, GuardResult]] = []  # (guards enabled?, result)
RUN_COUNTS: dict[str, int] = {}


@pytest.fixture
def store() -> BankStore:
    return BankStore()


@pytest.fixture
def client(store: BankStore) -> Iterator[TestClient]:
    with TestClient(create_app(store)) as test_client:
        yield test_client


@pytest.fixture
def bank(client: TestClient) -> BankClient:
    return BankClient(client, backoff_s=0)


@pytest.fixture
def ctx_for(bank: BankClient) -> Callable[[str], Ctx]:
    return lambda user_id: Ctx(user_id, bank)


@pytest.fixture
def guard(bank: BankClient) -> Callable[..., GuardResult]:
    """Run one guard check for a customer and record its latency."""

    def check(
        name: Any, args: Any, user_id: str = "u_1001", writes_so_far: int = 0, config: GuardConfig | None = None
    ) -> GuardResult:
        engine = GuardrailEngine(config)
        result = engine.check(name, args, Ctx(user_id, bank), writes_so_far)
        GUARD_CHECKS.append((engine.config.enabled, result))
        return result

    return check


@pytest.fixture
def record_count() -> Callable[[str, int], None]:
    def record(label: str, count: int) -> None:
        RUN_COUNTS[label] = RUN_COUNTS.get(label, 0) + count

    return record


def _latency_line(label: str, results: list[GuardResult]) -> str:
    latencies = [r.latency_ms for r in results]
    p95 = statistics.quantiles(latencies, n=20, method="inclusive")[-1] if len(latencies) > 1 else latencies[0]
    return f"{label}: n={len(latencies)}, median={statistics.median(latencies):.3f} ms, p95={p95:.3f} ms"


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    seed = build_seed()
    counts = ", ".join(f"{name}={len(seed[name])}" for name in ("customers", "accounts", "cards", "transactions"))
    terminalreporter.section("bank seed")
    terminalreporter.write_line(counts)

    enabled = [result for is_enabled, result in GUARD_CHECKS if is_enabled]
    if enabled:
        terminalreporter.section("guardrail check latency, guards enabled (in-process TestClient, includes lookups)")
        terminalreporter.write_line(_latency_line("all", enabled))
        groups = {
            "allow": lambda r: r.decision == "allow",
            "confirm": lambda r: r.decision == "confirm",
            "block at L1/L2 (no lookups)": lambda r: r.decision == "block" and r.layer in ("L1", "L2"),
            "block at L3-L5 (with lookups)": lambda r: r.decision == "block" and r.layer in ("L3", "L4", "L5"),
        }
        for label, matches in groups.items():
            subset = [r for r in enabled if matches(r)]
            if subset:
                terminalreporter.write_line(_latency_line(label, subset))
        disabled_count = len(GUARD_CHECKS) - len(enabled)
        terminalreporter.write_line(f"(plus {disabled_count} checks with guards disabled, excluded: no-op path)")
    for label, count in RUN_COUNTS.items():
        terminalreporter.write_line(f"{label}: {count}")
