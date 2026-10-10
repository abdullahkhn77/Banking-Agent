import importlib

import pytest

PACKAGES = ["agent", "api", "bank_services", "evals", "monitoring", "scripts", "serving"]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports(name: str) -> None:
    assert importlib.import_module(name) is not None
