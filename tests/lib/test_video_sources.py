from __future__ import annotations

import pytest

from lib import video_sources


def test_extracts_douyin_url_from_pasted_share_text():
    value = "7.28 复制打开抖音 https://v.douyin.com/AbCdEf/ 看视频！"
    assert video_sources.extract_video_url(value) == "https://v.douyin.com/AbCdEf/"
    assert video_sources.detect_video_platform(value) == "douyin"


def test_normalizes_direct_douyin_video(monkeypatch):
    monkeypatch.setattr(video_sources, "validate_public_http_url", lambda *_a, **_k: None)
    assert video_sources.normalize_video_url(
        "https://www.douyin.com/video/7667931266800454975?foo=bar"
    ) == "https://www.douyin.com/video/7667931266800454975"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/video.mp4",
        "http://10.0.0.2/video.mp4",
        "http://localhost/video.mp4",
        "file:///etc/passwd",
    ],
)
def test_rejects_private_or_non_http_targets(url):
    with pytest.raises(video_sources.VideoSourceError):
        video_sources.validate_public_http_url(url, resolve_dns=False)


def test_cookie_file_must_exist(tmp_path):
    with pytest.raises(video_sources.VideoSourceError, match="Cookie file not found"):
        video_sources.resolve_cookie_file(str(tmp_path / "missing.txt"))
