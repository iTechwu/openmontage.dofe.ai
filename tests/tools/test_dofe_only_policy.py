from __future__ import annotations

import pytest

from tools.base_tool import (
    BaseTool,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)
from tools.dofe.config import model_api_policy_error
from tools.tool_registry import ToolRegistry


class DirectImageApi(BaseTool):
    name = "direct_image_api"
    capability = "image_generation"
    provider = "openai"
    runtime = ToolRuntime.API
    resource_profile = ResourceProfile(vram_mb=1, network_required=True)

    def __init__(self) -> None:
        self.called = False

    def execute(self, _inputs):
        self.called = True
        return ToolResult(success=True)


class DofeImageApi(BaseTool):
    name = "dofe_image_api"
    capability = "image_generation"
    provider = "dofe"
    runtime = ToolRuntime.API

    def execute(self, _inputs):
        return ToolResult(success=True)


class SupportingStockApi(BaseTool):
    name = "supporting_stock_api"
    tier = ToolTier.SOURCE
    capability = "image_generation"
    provider = "pexels"
    runtime = ToolRuntime.API
    resource_profile = ResourceProfile(network_required=True)

    def execute(self, _inputs):
        return ToolResult(success=True)


def test_dofe_only_policy_blocks_direct_model_api_before_execution(monkeypatch) -> None:
    monkeypatch.setenv("DOFE_ENABLED", "true")
    tool = DirectImageApi()

    result = tool.execute({})

    assert result.success is False
    assert "direct model API tool" in (result.error or "")
    assert tool.called is False


@pytest.mark.parametrize(
    "capability",
    ["analysis", "avatar", "image_generation", "music_generation", "tts", "video_generation"],
)
def test_dofe_only_policy_covers_every_remote_model_capability(
    monkeypatch,
    capability: str,
) -> None:
    monkeypatch.setenv("DOFE_ENABLED", "true")
    tool = DirectImageApi()
    tool.capability = capability

    assert model_api_policy_error(tool) is not None


def test_dofe_only_policy_keeps_dofe_and_supporting_apis_available(monkeypatch) -> None:
    monkeypatch.setenv("DOFE_ENABLED", "true")

    assert DofeImageApi().execute({}).success is True
    assert SupportingStockApi().execute({}).success is True


def test_registry_hides_direct_model_apis_when_dofe_is_enabled(monkeypatch) -> None:
    monkeypatch.setenv("DOFE_ENABLED", "true")
    registry = ToolRegistry()
    registry.register(DirectImageApi())
    registry.register(DofeImageApi())
    registry.register(SupportingStockApi())

    assert registry.get("direct_image_api") is None
    assert registry.list_all() == ["dofe_image_api", "supporting_stock_api"]
    assert [tool.name for tool in registry.get_by_capability("image_generation")] == [
        "dofe_image_api",
        "supporting_stock_api",
    ]
    assert registry.gpu_required_tools() == []
    assert registry.network_required_tools() == ["supporting_stock_api"]


def test_direct_model_api_policy_is_limited_to_dofe_mode(monkeypatch) -> None:
    monkeypatch.delenv("DOFE_ENABLED", raising=False)
    tool = DirectImageApi()

    assert model_api_policy_error(tool) is None
    assert tool.execute({}).success is True
    assert tool.called is True


class FallsBackToDirect(BaseTool):
    name = "falls_back_to_direct"
    capability = "video_post"
    provider = "dofe"
    runtime = ToolRuntime.LOCAL
    fallback = "direct_image_api"

    def execute(self, _inputs):
        return ToolResult(success=True)


def test_registry_hides_direct_model_apis_from_every_public_view(monkeypatch) -> None:
    monkeypatch.setenv("DOFE_ENABLED", "true")
    registry = ToolRegistry()
    registry.register(DirectImageApi())
    registry.register(DofeImageApi())
    registry.register(SupportingStockApi())
    registry.register(FallsBackToDirect())

    assert "direct_image_api" not in registry.support_envelope()

    image_catalog_names = [
        item["name"] for item in registry.capability_catalog().get("image_generation", [])
    ]
    assert "direct_image_api" not in image_catalog_names
    assert "dofe_image_api" in image_catalog_names
    assert "supporting_stock_api" in image_catalog_names

    provider_catalog = registry.provider_catalog()
    assert "openai" not in provider_catalog
    assert "dofe" in provider_catalog
    assert "pexels" in provider_catalog

    # Query-style views must likewise hide the direct model tool. Discovery has
    # already populated the registry from the real tool tree at this point, so
    # assert absence rather than an exact list.
    assert registry.get_by_provider("openai") == []
    assert "direct_image_api" not in [t.name for t in registry.get_by_tier(ToolTier.CORE)]
    assert "direct_image_api" not in [
        t.name for t in registry.get_by_status(ToolStatus.AVAILABLE)
    ]
    assert "direct_image_api" not in [
        t.name for t in registry.get_by_stability(ToolStability.EXPERIMENTAL)
    ]
    assert "direct_image_api" not in registry.list_all()

    # A fallback that resolves to a hidden direct model tool must resolve to None.
    assert registry.find_fallback("falls_back_to_direct") is None
