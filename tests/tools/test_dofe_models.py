"""Unit tests for dofe alias resolution (dev-guide §3.2/§3.3, §8.1)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.dofe import config as dofe_config
from tools.dofe.errors import DofeModelUnavailableError
from tools.dofe.models import (
    CAPABILITY_ENV,
    catalog_model_ids,
    resolve_alias,
    validate_catalog_alias,
)


def _clear_dofe_env(monkeypatch):
    for var in (
        "DOFE_VIDEO_MODEL", "DOFE_IMAGE_MODEL", "DOFE_TTS_MODEL", "DOFE_MUSIC_MODEL", "DOFE_AVATAR_MODEL", "DOFE_STT_MODEL",
        "DOFE_MODEL_TEXT_TO_VIDEO", "DOFE_MODEL_IMAGE_TO_VIDEO", "DOFE_MODEL_REFERENCE_TO_VIDEO",
    ):
        monkeypatch.delenv(var, raising=False)


def test_models_are_never_invented_when_not_configured(monkeypatch):
    _clear_dofe_env(monkeypatch)
    for op in ("text_to_video", "image_to_video", "reference_to_video"):
        assert resolve_alias("video", op) is None
    assert resolve_alias("image", "generate") is None
    assert resolve_alias("stt", "transcribe") is None
    assert resolve_alias("tts", "generate") is None


def test_music_and_avatar_default_none(monkeypatch):
    _clear_dofe_env(monkeypatch)
    assert resolve_alias("music", "generate") is None
    assert resolve_alias("avatar", "generate") is None


def test_explicit_beats_env_and_default(monkeypatch):
    _clear_dofe_env(monkeypatch)
    monkeypatch.setenv("DOFE_VIDEO_MODEL", "kling-v3")
    assert resolve_alias("video", "text_to_video", explicit="my-alias") == "my-alias"


def test_capability_env_is_selected_candidate(monkeypatch):
    _clear_dofe_env(monkeypatch)
    monkeypatch.setenv("DOFE_VIDEO_MODEL", "kling-v3")
    assert resolve_alias("video", "text_to_video") == "kling-v3"


def test_per_operation_video_env(monkeypatch):
    _clear_dofe_env(monkeypatch)
    monkeypatch.setenv("DOFE_MODEL_IMAGE_TO_VIDEO", "hailuo")
    # image_to_video picks the per-op override...
    assert resolve_alias("video", "image_to_video") == "hailuo"
    # ...while other operations remain unconfigured instead of guessing.
    assert resolve_alias("video", "text_to_video") is None


def test_catalog_ids_are_exact_and_ignore_malformed_entries(monkeypatch):
    _clear_dofe_env(monkeypatch)
    models = [{"id": "seedance-2.0-fast"}, {"id": ""}, {}, "bad"]
    assert catalog_model_ids(models) == ("seedance-2.0-fast",)
    assert validate_catalog_alias("seedance-2.0-fast", models) == "seedance-2.0-fast"
    with pytest.raises(DofeModelUnavailableError, match="not returned by GET /v1/models"):
        validate_catalog_alias("seedance-2-0-fast", models)


def test_resolve_strips_whitespace(monkeypatch):
    _clear_dofe_env(monkeypatch)
    assert resolve_alias("image", "generate", explicit="  seedream-5.0  ") == "seedream-5.0"


def test_gateway_config_prefers_canonical_model_variables(monkeypatch):
    monkeypatch.setenv("DOFE_API_KEY", "legacy-key")
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "canonical-key")
    monkeypatch.setenv("DOFE_BASE_URL", "https://legacy.example/api")
    monkeypatch.setenv("DOFE_MODEL_BASE_URL", "https://ixicai.cn/api/")

    assert dofe_config.dofe_api_key() == "canonical-key"
    assert dofe_config.dofe_base_url() == "https://ixicai.cn/api"


def test_gateway_config_accepts_legacy_aliases(monkeypatch):
    monkeypatch.delenv("DOFE_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("DOFE_MODEL_BASE_URL", raising=False)
    monkeypatch.setenv("DOFE_API_KEY", "legacy-key")
    monkeypatch.setenv("DOFE_BASE_URL", "https://legacy.example/api/")

    assert dofe_config.dofe_api_key() == "legacy-key"
    assert dofe_config.dofe_base_url() == "https://legacy.example/api"


def test_user_setup_docs_list_every_dofe_model_selector_and_tool():
    for relative_path in ("README.md", "README_zh-CN.md", "docs/PROVIDERS.md"):
        content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        for environment_name in CAPABILITY_ENV.values():
            assert environment_name in content, f"{relative_path} omits {environment_name}"

    provider_docs = (PROJECT_ROOT / "docs/PROVIDERS.md").read_text(encoding="utf-8")
    for tool_name in (
        "dofe_image",
        "dofe_video",
        "dofe_tts",
        "dofe_music",
        "dofe_avatar",
        "dofe_stt",
    ):
        assert tool_name in provider_docs
