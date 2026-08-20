from __future__ import annotations

from pathlib import Path

import pytest

from tools.analysis.douyin_mcp import MCP_ENV, McpDownload
from tools.analysis.video_downloader import VideoDownloader


def _fail_if_called(message):
    def fail(*_args, **_kwargs):
        raise AssertionError(message)

    return fail


@pytest.mark.parametrize(
    ("dl_format", "expected_success"),
    [
        ("video", True),
        ("audio_only", True),
        ("metadata_only", True),
        ("subtitles_only", False),
    ],
)
def test_douyin_formats_never_use_yt_dlp(
    monkeypatch, tmp_path, dl_format, expected_success
):
    monkeypatch.setenv("OPENMONTAGE_YTDLP_COOKIES", "/missing/yt-dlp-cookies.txt")
    monkeypatch.delenv(MCP_ENV, raising=False)
    monkeypatch.setattr(
        "tools.analysis.video_downloader.normalize_video_url",
        lambda _value: "https://www.douyin.com/video/7667931266800454975",
    )
    monkeypatch.setattr(
        VideoDownloader,
        "_extract_metadata",
        _fail_if_called("Douyin metadata must not be extracted with yt-dlp"),
    )
    monkeypatch.setattr(
        VideoDownloader,
        "_download_video",
        _fail_if_called("Douyin video must not be downloaded with yt-dlp"),
    )
    monkeypatch.setattr(
        VideoDownloader,
        "_download_audio",
        _fail_if_called("Douyin audio must not be downloaded with yt-dlp"),
    )
    monkeypatch.setattr(
        VideoDownloader,
        "_download_subtitles",
        _fail_if_called("Douyin subtitles must not be downloaded with yt-dlp"),
    )

    def fake_extract_audio(_self, _video_path, output_dir):
        audio_path = output_dir / "reference_audio.wav"
        audio_path.write_bytes(b"audio")
        return str(audio_path)

    monkeypatch.setattr(VideoDownloader, "_extract_audio_track", fake_extract_audio)

    class FakeDouyin:
        def extract(self, _url):
            return {
                "title": "Public video",
                "duration": 5,
                "play_url": "https://example.com/video.mp4",
            }

        def download(self, _metadata, output_path):
            output_path.write_bytes(b"video")
            return str(output_path)

    monkeypatch.setattr("tools.analysis.douyin.DouyinShareClient", FakeDouyin)
    result = VideoDownloader().execute(
        {
            "url": "https://www.douyin.com/video/7667931266800454975",
            "output_dir": str(tmp_path),
            "format": dl_format,
        }
    )
    assert result.success is expected_success
    assert result.data["platform"] == "douyin"
    assert result.data["metadata"]["extractor"] == "douyin_public_share"
    if expected_success:
        assert result.data["resolved_url"].endswith("7667931266800454975")
    if dl_format in {"video", "audio_only"}:
        assert (tmp_path / "reference_video.mp4").is_file()


def test_douyin_audio_only_fails_when_audio_extraction_fails(monkeypatch, tmp_path):
    # This test asserts the local audio-extraction failure path only; it must not
    # reach the real MCP server. Without de-env'ing MCP, a populated .env would
    # trigger a real download_via_mcp call (a stat_miss archive ~90s).
    monkeypatch.delenv(MCP_ENV, raising=False)
    monkeypatch.setattr(
        "tools.analysis.video_downloader.normalize_video_url",
        lambda _value: "https://www.douyin.com/video/7667931266800454975",
    )
    monkeypatch.setattr(VideoDownloader, "_extract_audio_track", lambda *_a, **_k: None)

    class FakeDouyin:
        def extract(self, _url):
            return {"title": "Public video", "duration": 5, "play_url": "https://example.com"}

        def download(self, _metadata, output_path):
            output_path.write_bytes(b"video")
            return str(output_path)

    monkeypatch.setattr("tools.analysis.douyin.DouyinShareClient", FakeDouyin)
    result = VideoDownloader().execute(
        {
            "url": "https://www.douyin.com/video/7667931266800454975",
            "output_dir": str(tmp_path),
            "format": "audio_only",
        }
    )

    assert not result.success
    assert "audio extraction failed" in result.error.lower()


def test_douyin_mcp_disabled_still_uses_local_download(monkeypatch, tmp_path):
    """When MCP is not configured, local DouyinShareClient still downloads."""
    monkeypatch.delenv(MCP_ENV, raising=False)
    monkeypatch.setattr(
        "tools.analysis.video_downloader.normalize_video_url",
        lambda _value: "https://www.douyin.com/video/7667931266800454975",
    )

    def fake_extract_audio(_self, _video_path, output_dir):
        audio_path = output_dir / "reference_audio.wav"
        audio_path.write_bytes(b"audio")
        return str(audio_path)

    monkeypatch.setattr(VideoDownloader, "_extract_audio_track", fake_extract_audio)

    class FakeDouyin:
        def extract(self, _url):
            return {"title": "Public video", "duration": 5, "play_url": "https://example"}

        def download(self, _metadata, output_path):
            output_path.write_bytes(b"video")
            return str(output_path)

    monkeypatch.setattr("tools.analysis.douyin.DouyinShareClient", FakeDouyin)
    result = VideoDownloader().execute(
        {
            "url": "https://www.douyin.com/video/7667931266800454975",
            "output_dir": str(tmp_path),
            "format": "video",
        }
    )
    assert result.success
    assert (tmp_path / "reference_video.mp4").is_file()


def test_douyin_mcp_failure_falls_back_to_local(monkeypatch, tmp_path):
    """When MCP is configured but fails, local DouyinShareClient is used."""
    monkeypatch.setenv(MCP_ENV, "http://127.0.0.1:8000/mcp/viral-video")

    def fake_mcp(_url, _out):
        return None  # MCP failed -> signal fallback

    monkeypatch.setattr(
        "tools.analysis.video_downloader.download_via_mcp", fake_mcp
    )
    monkeypatch.setattr(
        "tools.analysis.video_downloader.normalize_video_url",
        lambda _value: "https://www.douyin.com/video/7667931266800454975",
    )

    def fake_extract_audio(_self, _video_path, output_dir):
        audio_path = output_dir / "reference_audio.wav"
        audio_path.write_bytes(b"audio")
        return str(audio_path)

    monkeypatch.setattr(VideoDownloader, "_extract_audio_track", fake_extract_audio)

    called = {"local": False}

    class FakeDouyin:
        def extract(self, _url):
            return {"title": "Public video", "duration": 5, "play_url": "https://example"}

        def download(self, _metadata, output_path):
            called["local"] = True
            output_path.write_bytes(b"video")
            return str(output_path)

    monkeypatch.setattr("tools.analysis.douyin.DouyinShareClient", FakeDouyin)
    result = VideoDownloader().execute(
        {
            "url": "https://www.douyin.com/video/7667931266800454975",
            "output_dir": str(tmp_path),
            "format": "video",
        }
    )
    assert result.success
    assert called["local"] is True
    assert (tmp_path / "reference_video.mp4").is_file()


def test_douyin_mcp_success_bypasses_local_download(monkeypatch, tmp_path):
    """When MCP downloads successfully, the local DouyinShareClient is NOT touched."""
    monkeypatch.setenv(MCP_ENV, "http://127.0.0.1:8000/mcp/viral-video")

    def fake_mcp(_url, output_dir):
        out = Path(output_dir) / "reference_video.mp4"
        out.write_bytes(b"mcp-video")
        return McpDownload(path=str(out), title="", aweme_id="")

    monkeypatch.setattr(
        "tools.analysis.video_downloader.download_via_mcp", fake_mcp
    )
    monkeypatch.setattr(
        "tools.analysis.video_downloader.normalize_video_url",
        lambda _value: "https://www.douyin.com/video/7667931266800454975",
    )

    def fake_extract_audio(_self, _video_path, output_dir):
        audio_path = output_dir / "reference_audio.wav"
        audio_path.write_bytes(b"audio")
        return str(audio_path)

    monkeypatch.setattr(VideoDownloader, "_extract_audio_track", fake_extract_audio)

    local_hit = {"called": False}

    class FakeDouyin:
        def extract(self, _url):
            return {"title": "Public video", "duration": 5, "play_url": "https://example"}

        def download(self, _metadata, output_path):
            local_hit["called"] = True
            output_path.write_bytes(b"video")
            return str(output_path)

    monkeypatch.setattr("tools.analysis.douyin.DouyinShareClient", FakeDouyin)
    result = VideoDownloader().execute(
        {
            "url": "https://www.douyin.com/video/7667931266800454975",
            "output_dir": str(tmp_path),
            "format": "video",
        }
    )
    assert result.success
    assert local_hit["called"] is False  # MCP path won, local never invoked
    assert (tmp_path / "reference_video.mp4").read_bytes() == b"mcp-video"


def test_douyin_mcp_success_fills_metadata_when_extract_failed(monkeypatch, tmp_path):
    """When the local extract fails (metadata == {}) but MCP download succeeds,
    title/duration/resolution are patched in place. Regression for the empty-dict
    falsy gotcha (`metadata = metadata or {}` would mutate a throwaway dict)."""
    monkeypatch.setenv(MCP_ENV, "http://127.0.0.1:8000/mcp/viral-video")

    def fake_mcp(_url, output_dir):
        out = Path(output_dir) / "reference_video.mp4"
        out.write_bytes(b"mcp-video")
        return McpDownload(path=str(out), title="出动！宝马四芒星", aweme_id="7651959802171526438")

    monkeypatch.setattr(
        "tools.analysis.video_downloader.download_via_mcp", fake_mcp
    )
    monkeypatch.setattr(
        "tools.analysis.video_downloader.normalize_video_url",
        lambda _value: "https://www.douyin.com/video/7667931266800454975",
    )
    monkeypatch.setattr(
        VideoDownloader, "_extract_audio_track", lambda *_a, **_k: None
    )
    # Local extract raises -> metadata stays {} until MCP success fills it.
    monkeypatch.setattr(
        "tools.analysis.douyin.DouyinShareClient.extract", _fail_if_called("extract must not run")
    )
    monkeypatch.setattr(
        VideoDownloader,
        "_probe_duration_and_resolution",
        lambda _self, _p: (369.96, "1280x720"),
    )

    result = VideoDownloader().execute(
        {
            "url": "https://www.douyin.com/video/7667931266800454975",
            "output_dir": str(tmp_path),
            "format": "video",
        }
    )
    assert result.success
    metadata = result.data["metadata"]
    assert metadata["title"] == "出动！宝马四芒星"
    assert metadata["duration"] == 369.96
    assert metadata["resolution"] == "1280x720"
