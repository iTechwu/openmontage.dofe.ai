"""Contract tests for the dofe_tts provider + the DOFE_ENABLED tts-selector switch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.base_tool import ToolResult, ToolStatus
from tools.audio.dofe_tts import DofeTTS


@pytest.fixture(autouse=True)
def _isolate_canonical_gateway_key(monkeypatch):
    monkeypatch.delenv("DOFE_MODEL_API_KEY", raising=False)


def test_registry_discovers_dofe_tts(monkeypatch, isolated_tool_registry):
    monkeypatch.delenv("DOFE_API_KEY", raising=False)
    isolated_tool_registry.discover("tools")
    tool = isolated_tool_registry.get("dofe_tts")
    assert tool is not None
    assert tool.capability == "tts"
    assert tool.provider == "dofe"
    assert tool.get_status() == ToolStatus.UNAVAILABLE


def test_status_gating(monkeypatch):
    tool = DofeTTS()
    monkeypatch.delenv("DOFE_API_KEY", raising=False)
    assert tool.get_status() == ToolStatus.UNAVAILABLE
    monkeypatch.setenv("DOFE_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_TTS_MODEL", "tts-test")
    assert tool.get_status() == ToolStatus.AVAILABLE


def test_text_content_block_has_no_role():
    payload = DofeTTS()._build_payload({"text": "hello"}, "volcengine-tts")
    assert "role" not in payload["content"][0]
    assert payload["endpointKind"] == "speech_synthesis"


def test_fallback_tools_declared():
    assert "piper_tts" in DofeTTS().fallback_tools


def _fake_execute(self, inputs):
    return ToolResult(
        success=True,
        data={"output_path": "out.mp3", "provider": self.provider},
        artifacts=["out.mp3"],
    )


def test_tts_selector_switch_on_picks_dofe(monkeypatch, isolated_tool_registry):
    monkeypatch.setenv("DOFE_ENABLED", "true")
    monkeypatch.setenv("DOFE_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_TTS_MODEL", "tts-test")
    isolated_tool_registry.discover("tools")
    monkeypatch.setattr(DofeTTS, "execute", _fake_execute)

    result = isolated_tool_registry.get("tts_selector").execute({"text": "hello"})
    assert result.success
    assert result.data["selected_provider"] == "dofe"
    assert result.data["selected_tool"] == "dofe_tts"
