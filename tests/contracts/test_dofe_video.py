"""Contract tests for the dofe_video provider + the DOFE_ENABLED video-selector switch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.base_tool import ToolResult, ToolStatus
from tools.video.dofe_video import DofeVideo


@pytest.fixture(autouse=True)
def _isolate_canonical_gateway_key(monkeypatch):
    monkeypatch.delenv("DOFE_MODEL_API_KEY", raising=False)


def test_registry_discovers_dofe_video(monkeypatch, isolated_tool_registry):
    monkeypatch.delenv("DOFE_API_KEY", raising=False)
    isolated_tool_registry.discover("tools")
    tool = isolated_tool_registry.get("dofe_video")
    assert tool is not None
    assert tool.capability == "video_generation"
    assert tool.provider == "dofe"
    assert tool.get_status() == ToolStatus.UNAVAILABLE


def test_status_available_with_key(monkeypatch):
    monkeypatch.setenv("DOFE_API_KEY", "test-key")
    assert DofeVideo().get_status() == ToolStatus.AVAILABLE


def test_text_content_block_has_no_role():
    payload = DofeVideo()._build_payload({"prompt": "x", "operation": "text_to_video"}, "seedance-2.0-fast")
    assert "role" not in payload["content"][0]


def test_default_model_is_seedance(monkeypatch):
    monkeypatch.delenv("DOFE_VIDEO_MODEL", raising=False)
    assert DofeVideo().resolve_model({"operation": "text_to_video"}) == "seedance-2.0-fast"


def test_fallback_tools_declared():
    fallback = DofeVideo().fallback_tools
    assert any("video" in name for name in fallback)


def _fake_execute(self, inputs):
    return ToolResult(
        success=True,
        data={"output_path": "out.mp4", "provider": self.provider},
        artifacts=["out.mp4"],
    )


def test_video_selector_switch_on_picks_dofe(monkeypatch, isolated_tool_registry):
    monkeypatch.setenv("DOFE_ENABLED", "true")
    monkeypatch.setenv("DOFE_API_KEY", "test-key")
    isolated_tool_registry.discover("tools")
    monkeypatch.setattr(DofeVideo, "execute", _fake_execute)

    result = isolated_tool_registry.get("video_selector").execute({"prompt": "a thing"})
    assert result.success
    assert result.data["selected_provider"] == "dofe"
    assert result.data["selected_tool"] == "dofe_video"
