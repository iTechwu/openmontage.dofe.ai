"""Contract tests for the dofe_image provider + the DOFE_ENABLED image-selector switch.

Covers (dev-guide §8.2): registry discovery, status gating, the text-block-no-role
invariant, fallback declaration, and the four selector-switch scenarios.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.base_tool import ToolResult, ToolStatus
from tools.graphics.dofe_image import DofeImage
from tools.graphics.flux_image import FluxImage


@pytest.fixture(autouse=True)
def _isolate_canonical_gateway_key(monkeypatch):
    monkeypatch.delenv("DOFE_MODEL_API_KEY", raising=False)


# --------------------------------------------------------- discovery & status

def test_registry_discovers_dofe_image(monkeypatch, isolated_tool_registry):
    monkeypatch.delenv("DOFE_API_KEY", raising=False)
    isolated_tool_registry.discover("tools")
    tool = isolated_tool_registry.get("dofe_image")
    assert tool is not None
    assert tool.capability == "image_generation"
    assert tool.provider == "dofe"
    assert tool.get_status() == ToolStatus.UNAVAILABLE


def test_status_available_with_key(monkeypatch):
    monkeypatch.setenv("DOFE_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_IMAGE_MODEL", "catalog-image")
    monkeypatch.setattr(
        "tools.dofe.status.DofeClient.list_models",
        lambda _self: [{"id": "catalog-image"}],
    )
    assert DofeImage().get_status() == ToolStatus.AVAILABLE


def test_status_unavailable_without_catalog_selection(monkeypatch):
    monkeypatch.setenv("DOFE_API_KEY", "test-key")
    monkeypatch.delenv("DOFE_IMAGE_MODEL", raising=False)
    assert DofeImage().get_status() == ToolStatus.UNAVAILABLE


def test_status_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("DOFE_API_KEY", raising=False)
    assert DofeImage().get_status() == ToolStatus.UNAVAILABLE


# --------------------------------------------------------------- invariants

def test_text_content_block_has_no_role():
    payload = DofeImage()._build_payload({"prompt": "x"}, "seedream-5.0")
    assert "role" not in payload["content"][0]


def test_fallback_tools_declared():
    assert "flux_image" in DofeImage().fallback_tools


def test_schema_exposes_image_edit_support():
    tool = DofeImage()
    assert tool.supports["image_edit"] is True
    assert tool.supports["multi_reference_edit"] is True
    assert "image_path" in tool.input_schema["properties"]
    assert "image_paths" in tool.input_schema["properties"]


# ------------------------------------------------------- selector switch matrix

def _fake_execute(self, inputs):
    return ToolResult(
        success=True,
        data={"output_path": "out.png", "provider": self.provider},
        artifacts=["out.png"],
    )


def test_image_selector_switch_on_picks_dofe(monkeypatch, isolated_tool_registry):
    monkeypatch.setenv("DOFE_ENABLED", "true")
    monkeypatch.setenv("DOFE_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_IMAGE_MODEL", "catalog-image")
    monkeypatch.setattr(
        "tools.dofe.status.DofeClient.list_models",
        lambda _self: [{"id": "catalog-image"}],
    )
    isolated_tool_registry.discover("tools")
    monkeypatch.setattr(DofeImage, "execute", _fake_execute)

    result = isolated_tool_registry.get("image_selector").execute({"prompt": "a thing"})
    assert result.success
    assert result.data["selected_provider"] == "dofe"
    assert result.data["selected_tool"] == "dofe_image"


def test_image_selector_switch_off_runs_original_scoring(monkeypatch, isolated_tool_registry):
    # DOFE_ENABLED unset -> original scoring logic; an explicit preference is honored.
    monkeypatch.delenv("DOFE_ENABLED", raising=False)
    monkeypatch.setenv("DOFE_API_KEY", "test-key")
    monkeypatch.setenv("FAL_KEY", "test-fal")
    isolated_tool_registry.discover("tools")
    monkeypatch.setattr(FluxImage, "execute", _fake_execute)

    result = isolated_tool_registry.get("image_selector").execute(
        {"prompt": "a thing", "preferred_provider": "flux"}
    )
    assert result.success
    assert result.data["selected_provider"] == "flux"


def test_image_selector_switch_beats_explicit_direct_provider(monkeypatch, isolated_tool_registry):
    monkeypatch.setenv("DOFE_ENABLED", "true")
    monkeypatch.setenv("DOFE_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_IMAGE_MODEL", "catalog-image")
    monkeypatch.setenv("FAL_KEY", "test-fal")
    monkeypatch.setattr(
        "tools.dofe.status.DofeClient.list_models",
        lambda _self: [{"id": "catalog-image"}],
    )
    isolated_tool_registry.discover("tools")
    monkeypatch.setattr(DofeImage, "execute", _fake_execute)

    result = isolated_tool_registry.get("image_selector").execute(
        {"prompt": "a thing", "preferred_provider": "flux"}
    )
    assert result.success
    assert result.data["selected_provider"] == "dofe"
    assert result.data["selected_tool"] == "dofe_image"


def test_image_selector_switch_on_but_unavailable_fails_closed(monkeypatch, isolated_tool_registry):
    # Switch on but no key -> dofe unavailable -> no direct-provider fallback.
    monkeypatch.setenv("DOFE_ENABLED", "true")
    monkeypatch.delenv("DOFE_API_KEY", raising=False)
    monkeypatch.setenv("FAL_KEY", "test-fal")
    isolated_tool_registry.discover("tools")
    monkeypatch.setattr(FluxImage, "execute", _fake_execute)

    result = isolated_tool_registry.get("image_selector").execute({"prompt": "a thing"})
    assert not result.success
    assert "direct-provider fallback is disabled" in result.error
