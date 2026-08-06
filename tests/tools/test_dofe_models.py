"""Unit tests for dofe alias resolution (dev-guide §3.2/§3.3, §8.1)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.dofe import config as dofe_config
from tools.dofe.models import resolve_alias


def _clear_dofe_env(monkeypatch):
    for var in (
        "DOFE_VIDEO_MODEL", "DOFE_IMAGE_MODEL", "DOFE_TTS_MODEL", "DOFE_MUSIC_MODEL", "DOFE_AVATAR_MODEL", "DOFE_STT_MODEL",
        "DOFE_MODEL_TEXT_TO_VIDEO", "DOFE_MODEL_IMAGE_TO_VIDEO", "DOFE_MODEL_REFERENCE_TO_VIDEO",
    ):
        monkeypatch.delenv(var, raising=False)


def test_video_defaults_all_seedance(monkeypatch):
    _clear_dofe_env(monkeypatch)
    for op in ("text_to_video", "image_to_video", "reference_to_video"):
        assert resolve_alias("video", op) == "seedance-2.0-fast"
    assert resolve_alias("stt", "transcribe") == "openspeech-auc"


def test_image_default_and_tts_requires_configuration(monkeypatch):
    _clear_dofe_env(monkeypatch)
    assert resolve_alias("image", "generate") == "seedream-5.0"
    assert resolve_alias("tts", "generate") is None


def test_music_and_avatar_default_none(monkeypatch):
    _clear_dofe_env(monkeypatch)
    assert resolve_alias("music", "generate") is None
    assert resolve_alias("avatar", "generate") is None


def test_explicit_beats_env_and_default(monkeypatch):
    _clear_dofe_env(monkeypatch)
    monkeypatch.setenv("DOFE_VIDEO_MODEL", "kling-v3")
    assert resolve_alias("video", "text_to_video", explicit="my-alias") == "my-alias"


def test_capability_env_beats_default(monkeypatch):
    _clear_dofe_env(monkeypatch)
    monkeypatch.setenv("DOFE_VIDEO_MODEL", "kling-v3")
    assert resolve_alias("video", "text_to_video") == "kling-v3"


def test_per_operation_video_env(monkeypatch):
    _clear_dofe_env(monkeypatch)
    monkeypatch.setenv("DOFE_MODEL_IMAGE_TO_VIDEO", "hailuo")
    # image_to_video picks the per-op override...
    assert resolve_alias("video", "image_to_video") == "hailuo"
    # ...while other operations fall through to the default.
    assert resolve_alias("video", "text_to_video") == "seedance-2.0-fast"


def test_exact_match_not_normalized(monkeypatch):
    _clear_dofe_env(monkeypatch)
    # seedance-2.0-fast and seedance-2-0-fast are distinct aliases on the gateway; the
    # resolver never normalizes one into the other.
    assert resolve_alias("video", "text_to_video", explicit="seedance-2-0") == "seedance-2-0"
    assert resolve_alias("video", "text_to_video") == "seedance-2.0-fast"
    assert resolve_alias("video", "text_to_video") != resolve_alias("video", "text_to_video", explicit="seedance-2-0")


def test_resolve_strips_whitespace(monkeypatch):
    _clear_dofe_env(monkeypatch)
    assert resolve_alias("image", "generate", explicit="  seedream-5.0  ") == "seedream-5.0"


def test_gateway_config_prefers_canonical_model_variables(monkeypatch):
    monkeypatch.setenv("DOFE_API_KEY", "legacy-key")
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "canonical-key")
    monkeypatch.setenv("DOFE_BASE_URL", "https://legacy.example/api")
    monkeypatch.setenv("DOFE_MODEL_BASE_URL", "https://model.local.dofe.ai/api/")

    assert dofe_config.dofe_api_key() == "canonical-key"
    assert dofe_config.dofe_base_url() == "https://model.local.dofe.ai/api"


def test_gateway_config_accepts_legacy_aliases(monkeypatch):
    monkeypatch.delenv("DOFE_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("DOFE_MODEL_BASE_URL", raising=False)
    monkeypatch.setenv("DOFE_API_KEY", "legacy-key")
    monkeypatch.setenv("DOFE_BASE_URL", "https://legacy.example/api/")

    assert dofe_config.dofe_api_key() == "legacy-key"
    assert dofe_config.dofe_base_url() == "https://legacy.example/api"
