"""Contract tests for the dofe_music provider (dev-guide §8.2)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.base_tool import ToolStatus
from tools.audio.dofe_music import DofeMusic


@pytest.fixture(autouse=True)
def _isolate_canonical_gateway_key(monkeypatch):
    monkeypatch.delenv("DOFE_MODEL_API_KEY", raising=False)


def test_registry_discovers_dofe_music(monkeypatch, isolated_tool_registry):
    monkeypatch.delenv("DOFE_API_KEY", raising=False)
    isolated_tool_registry.discover("tools")
    tool = isolated_tool_registry.get("dofe_music")
    assert tool is not None
    assert tool.capability == "music_generation"
    assert tool.provider == "dofe"
    assert tool.get_status() == ToolStatus.UNAVAILABLE


def test_status_gating(monkeypatch):
    tool = DofeMusic()
    monkeypatch.delenv("DOFE_API_KEY", raising=False)
    assert tool.get_status() == ToolStatus.UNAVAILABLE
    monkeypatch.setenv("DOFE_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_MUSIC_MODEL", "music-test")
    assert tool.get_status() == ToolStatus.AVAILABLE


def test_text_content_block_has_no_role():
    payload = DofeMusic()._build_payload({"prompt": "x"}, "some-music-alias")
    assert "role" not in payload["content"][0]
    assert payload["endpointKind"] == "music_async"
    assert payload["params"]["operation"] == "generate"
    assert payload["params"]["musicMode"] == "inspiration"


def test_custom_music_mode_is_mapped():
    payload = DofeMusic()._build_payload(
        {"prompt": "night drive", "music_mode": "custom"},
        "suno-v5-5",
    )

    assert payload["params"]["musicMode"] == "custom"


def test_default_model_none_until_configured(monkeypatch):
    monkeypatch.delenv("DOFE_MUSIC_MODEL", raising=False)
    assert DofeMusic().resolve_model({}) is None


def test_fallback_tools_declared():
    assert "music_gen" in DofeMusic().fallback_tools
