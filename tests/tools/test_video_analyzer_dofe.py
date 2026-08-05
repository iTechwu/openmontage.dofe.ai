from __future__ import annotations

from tools.analysis.transcriber import Transcriber
from tools.analysis.dofe_stt import DofeSpeechToText
from tools.analysis.video_analyzer import VideoAnalyzer
from tools.analysis.video_downloader import VideoDownloader
from tools.base_tool import ToolResult


def test_strict_dofe_routing_uses_airouter_stt_not_local_whisper(monkeypatch, tmp_path):
    stt_inputs = {}
    monkeypatch.setenv("DOFE_ENABLED", "true")
    monkeypatch.setattr(VideoAnalyzer, "_is_url", lambda _self, _source: True)
    monkeypatch.setattr(VideoAnalyzer, "_detect_platform", lambda _self, _source: "douyin")
    monkeypatch.setattr(
        VideoDownloader,
        "execute",
        lambda _self, _inputs: ToolResult(
            success=True,
            data={
                "metadata": {
                    "title": "reference",
                    "duration": 5,
                    "play_url": "https://media.example.test/reference.mp4",
                },
                "video_path": str(tmp_path / "reference.mp4"),
                "audio_path": str(tmp_path / "reference.wav"),
            },
        ),
    )
    monkeypatch.setattr(
        Transcriber,
        "execute",
        lambda _self, _inputs: (_ for _ in ()).throw(AssertionError("local Whisper was called")),
    )
    def execute_stt(_self, inputs):
        stt_inputs.update(inputs)
        return ToolResult(
            success=True,
            data={
                "full_text": "测试字幕",
                "segments": [],
                "language": "zh-CN",
                "word_count": 1,
                "billing": {
                    "amount": 0.0128,
                    "currency": "CNY",
                    "source": "gateway_final",
                    "is_final": True,
                },
                "source_asset": {
                    "url": "tos://dofe-transcode/temp/generation-assets/reference.wav",
                    "bucket": "dofe-transcode",
                    "key": "temp/generation-assets/reference.wav",
                },
            },
            cost_amount=0.0128,
            cost_currency="CNY",
            cost_source="gateway_final",
        )

    monkeypatch.setattr(DofeSpeechToText, "execute", execute_stt)

    result = VideoAnalyzer().execute(
        {
            "source": "https://www.douyin.com/video/7667931266800454975",
            "analysis_depth": "transcript_only",
            "output_dir": str(tmp_path / "analysis"),
        }
    )

    assert result.success
    assert "audio_upload_airouter_tos" in result.data["_analysis_meta"]["steps_completed"]
    assert "transcript_airouter_openspeech_auc" in result.data["_analysis_meta"]["steps_completed"]
    assert stt_inputs["audio_path"] == str(tmp_path / "reference.wav")
    assert result.data["narration_transcript"]["full_text"] == "测试字幕"
    assert result.data["_analysis_meta"]["billing"][0]["currency"] == "CNY"
