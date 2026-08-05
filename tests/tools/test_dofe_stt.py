from __future__ import annotations

import json

from tools.analysis.dofe_stt import DofeSpeechToText


def test_dofe_stt_uses_openspeech_and_preserves_native_cost(monkeypatch, tmp_path):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.delenv("DOFE_STT_MODEL", raising=False)
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
    assert captured["payload"]["model"] == "openspeech-auc"
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
    local_audio = tmp_path / "audio.wav"
    local_audio.write_bytes(b"RIFF")

    result = DofeSpeechToText().execute({"audio_url": str(local_audio)})

    assert not result.success
    assert "provider-accessible" in result.error


def test_dofe_stt_stages_audio_path_before_submitting_openspeech(monkeypatch, tmp_path):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
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
