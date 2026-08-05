"""Contract tests for the dofe_avatar provider (dev-guide §8.2)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.base_tool import ToolStatus
from tools.avatar.dofe_avatar import DofeAvatar


@pytest.fixture(autouse=True)
def _isolate_canonical_gateway_key(monkeypatch):
    monkeypatch.delenv("DOFE_MODEL_API_KEY", raising=False)


def test_registry_discovers_dofe_avatar(monkeypatch, isolated_tool_registry):
    monkeypatch.delenv("DOFE_API_KEY", raising=False)
    isolated_tool_registry.discover("tools")
    tool = isolated_tool_registry.get("dofe_avatar")
    assert tool is not None
    assert tool.capability == "avatar"
    assert tool.provider == "dofe"
    assert tool.get_status() == ToolStatus.UNAVAILABLE


def test_status_gating(monkeypatch):
    tool = DofeAvatar()
    monkeypatch.delenv("DOFE_API_KEY", raising=False)
    assert tool.get_status() == ToolStatus.UNAVAILABLE
    monkeypatch.setenv("DOFE_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_AVATAR_MODEL", "avatar-test")
    assert tool.get_status() == ToolStatus.AVAILABLE


def test_avatar_image_uses_allowed_role():
    # Avatar image role must stay within {reference, first_frame, last_frame}
    # (dev-guide §2.3 hard rule). Uses "reference" rather than "avatar".
    payload = DofeAvatar()._build_payload(
        {"image_url": "https://cdn.test/face.png", "audio_url": "https://cdn.test/voice.mp3"},
        "some-avatar-alias",
    )
    roles = [c.get("role") for c in payload["content"]]
    assert roles[0] == "reference"
    assert payload["endpointKind"] == "digital_human"


def test_requires_https_audio():
    with pytest.raises(ValueError, match="audio_url"):
        DofeAvatar()._build_payload(
            {"image_url": "https://cdn.test/face.png", "audio_url": "http://evil/voice.mp3"},
            "x",
        )


def test_rejects_local_audio_path():
    with pytest.raises(ValueError, match="public https URL"):
        DofeAvatar()._build_payload(
            {"image_url": "https://cdn.test/face.png", "audio_path": "/tmp/voice.mp3"}, "x",
        )


def test_default_model_none_until_configured(monkeypatch):
    monkeypatch.delenv("DOFE_AVATAR_MODEL", raising=False)
    assert DofeAvatar().resolve_model({}) is None


def test_fallback_tools_declared():
    assert "talking_head" in DofeAvatar().fallback_tools
