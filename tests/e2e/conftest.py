"""E2E tests require a live cluster — see tests/conftest.py preflight."""

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.e2e)
