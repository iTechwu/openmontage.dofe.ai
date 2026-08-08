"""Regression coverage for the tool registry's lazy-discovery contract.

Guards against the bug class where a caller forgets ``registry.discover()``,
calls ``registry.get(name)``, silently gets ``None``, and later crashes with a
misleading ``AttributeError: 'NoneType' object has no attribute 'execute'``.
"""

from __future__ import annotations

from tools.tool_registry import ToolRegistry


def test_get_lazily_discovers_without_prior_discover():
    """get() must resolve a registered tool even if discover() was never called."""
    registry = ToolRegistry()
    # No registry.discover() call here -- this is the regression.
    tool = registry.get("image_selector")
    assert tool is not None
    assert tool.name == "image_selector"
    # Discovery ran lazily and is now memoized.
    assert "tools" in registry._discovered_packages


def test_get_returns_none_for_genuinely_unknown_tool():
    """Lazy discovery must not fabricate tools that are not registered."""
    registry = ToolRegistry()
    assert registry.get("definitely_not_a_registered_tool_xyz") is None


def test_discovery_is_memoized_across_lookups():
    """The first get() triggers discovery; subsequent ones must not re-walk."""
    registry = ToolRegistry()
    registry.get("image_selector")
    assert "tools" in registry._discovered_packages
    # A second lookup resolves from the populated cache.
    again = registry.get("image_selector")
    assert again is registry._tools["image_selector"]


def test_ensure_discovered_reentrancy_is_noop():
    """A get() issued mid-discovery (module import calling the registry) must
    not recurse back into discover() before _discovered_packages is set."""
    registry = ToolRegistry()
    # Simulate being mid-walk for the default package.
    registry._discovering.add("tools")
    registry.ensure_discovered("tools")
    # Skipped: discovery did not complete, so nothing was registered.
    assert "tools" not in registry._discovered_packages
    assert registry._tools == {}
