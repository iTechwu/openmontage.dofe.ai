"""Shared fixtures for contract tests."""

from __future__ import annotations

import pytest

from tools.tool_registry import ToolRegistry


@pytest.fixture(autouse=True)
def _dofe_switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``DOFE_ENABLED`` off for provider-contract tests.

    The developer/CI ``.env`` may enable ``DOFE_ENABLED=true``, which hides
    third-party remote model tools from the registry and blocks their
    execution. These tests exercise the direct-provider contract, which exists
    when the switch is off; the DoFe-only policy is covered separately by
    ``tests/tools/test_dofe_only_policy.py`` (which sets the variable itself).
    """
    monkeypatch.setenv("DOFE_ENABLED", "false")


@pytest.fixture()
def isolated_tool_registry(monkeypatch) -> ToolRegistry:
    """Provide a registry singleton replacement scoped to one test.

    Discovery must not reload the developer's real ``.env`` after a test has
    deliberately removed a credential to exercise an unavailable path.
    """
    test_registry = ToolRegistry()
    monkeypatch.setattr(test_registry, "_load_dotenv", lambda: None)
    monkeypatch.setattr("tools.tool_registry.registry", test_registry)
    return test_registry
