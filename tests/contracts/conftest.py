"""Shared fixtures for contract tests."""

from __future__ import annotations

import pytest

from tools.tool_registry import ToolRegistry


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
