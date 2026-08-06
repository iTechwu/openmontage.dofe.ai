from __future__ import annotations

import pytest

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
