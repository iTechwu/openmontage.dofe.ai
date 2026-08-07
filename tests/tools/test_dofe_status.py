from __future__ import annotations

import pytest

from tools.analysis.dofe_stt import DofeSpeechToText
from tools.audio.dofe_music import DofeMusic
from tools.audio.dofe_tts import DofeTTS
from tools.avatar.dofe_avatar import DofeAvatar
from tools.base_tool import ToolStatus
from tools.dofe.client import DofeClient
from tools.dofe.errors import DofeNetworkError
from tools.dofe.status import configured_model_is_visible
from tools.graphics.dofe_image import DofeImage
from tools.video.dofe_video import DofeVideo


def test_configured_model_status_requires_exact_tenant_visibility(monkeypatch):
    monkeypatch.setenv("DOFE_IMAGE_MODEL", "configured-image")
    monkeypatch.setattr(
        DofeClient,
        "list_models",
        lambda _self: [{"id": "different-image"}],
    )

    assert configured_model_is_visible("image", ("generate",)) is False


def test_configured_model_status_accepts_any_exact_operation_alias(monkeypatch):
    monkeypatch.setenv("DOFE_MODEL_TEXT_TO_VIDEO", "hidden-video")
    monkeypatch.setenv("DOFE_MODEL_REFERENCE_TO_VIDEO", "visible-video")
    monkeypatch.setattr(
        DofeClient,
        "list_models",
        lambda _self: [{"id": "visible-video"}],
    )

    assert configured_model_is_visible(
        "video",
        ("text_to_video", "image_to_video", "reference_to_video"),
    ) is True


def test_configured_model_status_fails_closed_on_catalog_error(monkeypatch):
    monkeypatch.setenv("DOFE_STT_MODEL", "catalog-stt")

    def fail(_self):
        raise DofeNetworkError("catalog offline")

    monkeypatch.setattr(DofeClient, "list_models", fail)

    assert configured_model_is_visible("stt", ("transcribe",)) is False


@pytest.mark.parametrize(
    ("tool_type", "environment_name"),
    [
        (DofeImage, "DOFE_IMAGE_MODEL"),
        (DofeVideo, "DOFE_VIDEO_MODEL"),
        (DofeTTS, "DOFE_TTS_MODEL"),
        (DofeMusic, "DOFE_MUSIC_MODEL"),
        (DofeAvatar, "DOFE_AVATAR_MODEL"),
        (DofeSpeechToText, "DOFE_STT_MODEL"),
    ],
)
def test_all_dofe_tool_statuses_reject_hidden_configured_models(
    monkeypatch,
    tool_type,
    environment_name,
):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv(environment_name, "hidden-model")
    monkeypatch.setattr(
        DofeClient,
        "list_models",
        lambda _self: [{"id": "visible-model"}],
    )

    assert tool_type().get_status() == ToolStatus.UNAVAILABLE
