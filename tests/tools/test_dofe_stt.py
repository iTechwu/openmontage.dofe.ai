from __future__ import annotations

import json

from tools.analysis.dofe_stt import DofeSpeechToText
from tools.base_tool import ToolStatus
from tools.dofe import DofeAPIError


def _catalog(monkeypatch, model="catalog-stt"):
    monkeypatch.setenv("DOFE_STT_MODEL", model)
    monkeypatch.setattr(
        "tools.analysis.dofe_stt.DofeClient.list_models",
        lambda _self: [{"id": model}],
    )


def test_dofe_stt_status_requires_catalog_selection(monkeypatch):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.delenv("DOFE_STT_MODEL", raising=False)
    assert DofeSpeechToText().get_status() == ToolStatus.UNAVAILABLE

    monkeypatch.setenv("DOFE_STT_MODEL", "catalog-stt")
    monkeypatch.setattr(
        "tools.dofe.status.DofeClient.list_models",
        lambda _self: [{"id": "catalog-stt"}],
    )
    assert DofeSpeechToText().get_status() == ToolStatus.AVAILABLE


def test_dofe_stt_uses_catalog_model_and_preserves_native_cost(monkeypatch, tmp_path):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    _catalog(monkeypatch)
    captured = {}

    def fake_submit(_self, payload, **kwargs):
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return {
            "task_id": "gen-stt-1",
            "status": "succeeded",
            "assets": [{"type": "document", "url": ""}],
            "text": "一段测试转写",
            "estimated_cost": "0.06400000",
            "final_cost": "0.06400000",
            "cost_currency": "CNY",
            "pricing_breakdown": {
                "billingUnit": "PER_MINUTE",
                "usage": {"durationSeconds": 300},
            },
        }

    monkeypatch.setattr(
        "tools.analysis.dofe_stt.DofeClient.submit_and_collect", fake_submit
    )
    output = tmp_path / "transcript.json"

    result = DofeSpeechToText().execute(
        {
            "audio_url": "https://media.example.test/audio.wav",
            "duration_seconds": 300,
            "output_path": str(output),
        }
    )

    assert result.success
    assert captured["payload"]["model"] == "catalog-stt"
    assert captured["payload"]["endpointKind"] == "speech_transcription_async"
    assert captured["payload"]["params"]["durationSeconds"] == 300
    assert result.cost_usd == 0.0
    assert result.cost_amount == 0.064
    assert result.cost_currency == "CNY"
    assert result.cost_source == "gateway_final"
    transcript = json.loads(output.read_text(encoding="utf-8"))
    assert transcript["full_text"] == "一段测试转写"
    assert transcript["word_count"] == 6
    assert transcript["character_count"] == 6


def test_dofe_stt_rejects_local_audio_without_gateway_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    _catalog(monkeypatch)
    local_audio = tmp_path / "audio.wav"
    local_audio.write_bytes(b"RIFF")

    result = DofeSpeechToText().execute({"audio_url": str(local_audio)})

    assert not result.success
    assert "provider-accessible" in result.error


def test_dofe_stt_stages_audio_path_before_submitting_catalog_model(monkeypatch, tmp_path):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    _catalog(monkeypatch)
    source = tmp_path / "audio.wav"
    source.write_bytes(b"RIFF")
    events = []

    def fake_upload(_self, path, **kwargs):
        events.append(("upload", str(path), kwargs["asset_type"]))
        return {
            "url": "tos://dofe-transcode/temp/generation-assets/id-audio.wav",
            "bucket": "dofe-transcode",
            "key": "temp/generation-assets/id-audio.wav",
            "sizeBytes": 4,
        }

    def fake_submit(_self, payload, **_kwargs):
        events.append(("submit", payload["content"][0]["part"]["audio_url"]["url"]))
        return {
            "task_id": "gen-stt-local-1",
            "status": "succeeded",
            "text": "本地音频转写",
            "estimated_cost": "0.01280000",
            "final_cost": "0.01280000",
            "cost_currency": "CNY",
        }

    monkeypatch.setattr("tools.analysis.dofe_stt.DofeMediaUploadClient.upload", fake_upload)
    monkeypatch.setattr("tools.analysis.dofe_stt.DofeClient.submit_and_collect", fake_submit)

    result = DofeSpeechToText().execute(
        {
            "audio_path": str(source),
            "duration_seconds": 60,
            "output_path": str(tmp_path / "transcript-local.json"),
        }
    )

    assert result.success
    assert events == [
        ("upload", str(source), "audio"),
        ("submit", "tos://dofe-transcode/temp/generation-assets/id-audio.wav"),
    ]
    assert result.data["source_asset"]["bucket"] == "dofe-transcode"


def test_dofe_stt_rejects_model_missing_from_catalog_before_submit(monkeypatch):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_STT_MODEL", "hidden-stt")
    monkeypatch.setattr(
        "tools.analysis.dofe_stt.DofeClient.list_models",
        lambda _self: [{"id": "visible-stt"}],
    )

    def fail_submit(*_args, **_kwargs):
        raise AssertionError("STT task must not be submitted for a hidden model")

    monkeypatch.setattr(
        "tools.analysis.dofe_stt.DofeClient.submit_and_collect", fail_submit
    )

    result = DofeSpeechToText().execute(
        {"audio_url": "https://media.example.test/audio.wav"}
    )

    assert not result.success
    assert "not returned by GET /v1/models" in result.error


def test_dofe_stt_persists_created_task_and_resumes_before_upload(monkeypatch, tmp_path):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    _catalog(monkeypatch)
    source = tmp_path / "audio.wav"
    source.write_bytes(b"RIFF")
    output = tmp_path / "transcript.json"
    calls = []

    def fake_upload(_self, path, **_kwargs):
        calls.append(("upload", str(path)))
        return {
            "url": "tos://dofe-transcode/temp/generation-assets/first.wav",
            "bucket": "dofe-transcode",
            "key": "temp/generation-assets/first.wav",
            "sizeBytes": 4,
        }

    def fail_after_create(_self, _payload, **kwargs):
        calls.append(("submit", kwargs.get("existing_task_id")))
        raise DofeAPIError("poll failed", details={"task_id": "gen-recover-1"})

    monkeypatch.setattr("tools.analysis.dofe_stt.DofeMediaUploadClient.upload", fake_upload)
    monkeypatch.setattr("tools.analysis.dofe_stt.DofeClient.submit_and_collect", fail_after_create)

    first = DofeSpeechToText().execute(
        {"audio_path": str(source), "duration_seconds": 60, "output_path": str(output)}
    )
    resume_path = output.with_suffix(".resume.json")

    assert not first.success
    assert json.loads(resume_path.read_text(encoding="utf-8")) == {
        "idempotency_key": DofeSpeechToText().idempotency_key(
            {"audio_path": str(source), "duration_seconds": 60, "output_path": str(output)}
        ),
        "task_id": "gen-recover-1",
    }

    def recover(_self, _payload, **kwargs):
        calls.append(("resume", kwargs.get("existing_task_id")))
        return {
            "task_id": "gen-recover-1",
            "status": "succeeded",
            "text": "恢复成功",
            "final_cost": "0.01",
            "cost_currency": "CNY",
        }

    monkeypatch.setattr("tools.analysis.dofe_stt.DofeClient.submit_and_collect", recover)
    second = DofeSpeechToText().execute(
        {"audio_path": str(source), "duration_seconds": 60, "output_path": str(output)}
    )

    assert second.success
    assert calls == [
        ("upload", str(source)),
        ("submit", None),
        ("resume", "gen-recover-1"),
    ]
    assert not resume_path.exists()
