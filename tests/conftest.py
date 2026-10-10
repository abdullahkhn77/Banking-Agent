from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from bank_services.app import create_app
from bank_services.data import build_seed
from bank_services.store import BankStore


@pytest.fixture
def store() -> BankStore:
    return BankStore()


@pytest.fixture
def client(store: BankStore) -> Iterator[TestClient]:
    with TestClient(create_app(store)) as test_client:
        yield test_client


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    seed = build_seed()
    counts = ", ".join(f"{name}={len(seed[name])}" for name in ("customers", "accounts", "cards", "transactions"))
    terminalreporter.section("bank seed")
    terminalreporter.write_line(counts)
