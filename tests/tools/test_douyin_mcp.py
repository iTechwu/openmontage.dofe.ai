from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tools.analysis.douyin_mcp import (
    MCP_ENV,
    _MAX_RETRIES,
    _download_url,
    _parse_tos_envelope,
    _parse_tos_url,
    _resolve_tos_url,
    _run_coro,
    _should_retry,
    download_via_mcp,
)


# ---------------------------------------------------------------- parsing


def test_parse_success_envelope_extracts_tos_url():
    envelope = json.dumps(
        {
            "tosUrl": "https://bucket.volces.com/x.mp4?X-Amz-Sig=1",
            "downloadUrl": "https://bucket.volces.com/x.mp4?X-Amz-Sig=1",
            "expiresAt": "2026-08-20T12:34:56+00:00",
            "awemeId": "73456789012",
            "cached": False,
        }
    )
    assert _parse_tos_url(envelope) == "https://bucket.volces.com/x.mp4?X-Amz-Sig=1"


def test_parse_success_envelope_falls_back_to_download_url():
    envelope = json.dumps({"downloadUrl": "https://bucket.volces.com/y.mp4?X=1"})
    assert _parse_tos_url(envelope) == "https://bucket.volces.com/y.mp4?X=1"


def test_parse_returns_none_on_invalid_or_missing_url():
    assert _parse_tos_url("not-json") is None
    assert _parse_tos_url("[]") is None
    assert _parse_tos_url(json.dumps({"nourl": 1})) is None


def test_parse_tos_envelope_extracts_identity_best_effort():
    env = _parse_tos_envelope(
        json.dumps(
            {
                "tosUrl": "https://b/x.mp4?S=1",
                "title": "出动！宝马四芒星",
                "awemeId": "73456789012",
                "cached": False,
            }
        )
    )
    assert env == {
        "tos_url": "https://b/x.mp4?S=1",
        "title": "出动！宝马四芒星",
        "aweme_id": "73456789012",
    }
    # title/aweme_id absent -> empty strings, URL still usable.
    env_default = _parse_tos_envelope(json.dumps({"downloadUrl": "https://b/y.mp4"}))
    assert env_default["tos_url"] == "https://b/y.mp4"
    assert env_default["title"] == ""
    assert env_default["aweme_id"] == ""
    assert _parse_tos_envelope("not-json") is None


# ---------------------------------------------------------------- retry policy


def test_should_retry_only_marks_explicit_retryable_codes():
    assert _should_retry("IDEMPOTENCY_IN_PROGRESS")
    assert _should_retry("DOUYIN_VIDEO_DOWNLOAD_FAILED")
    assert _should_retry("DOUYIN_TOS_UPLOAD_FAILED")
    assert _should_retry("TOS_PRESIGN_FAILED")
    assert not _should_retry("PROVIDER_ERROR")
    assert not _should_retry("VALIDATION_ERROR")
    assert not _should_retry("IDEMPOTENCY_CONFLICT")


@pytest.mark.parametrize(
    ("responses", "expected_keys"),
    [
        # IN_PROGRESS -> success: retries reuse the SAME key (base), then the
        # final successful attempt also carries that same base key.
        (
            [("IDEMPOTENCY_IN_PROGRESS", True), (None, False)],
            ["base", "base"],
        ),
        # Concrete failure (download failed) -> success: the retry mints a
        # FRESH key (never replays the failed attempt under the same key).
        (
            [("DOUYIN_VIDEO_DOWNLOAD_FAILED", True), (None, False)],
            ["k0", "k1"],
        ),
    ],
)
def test_resolve_tos_url_idempotency_key_strategy(
    monkeypatch, responses, expected_keys
):
    """IN_PROGRESS reuses one key; concrete failures get fresh keys per retry."""
    seen_keys: list[str] = []

    async def fake_call(_url, _tool, arguments):
        seen_keys.append(arguments["idempotencyKey"] if arguments else "")
        result = responses[len(seen_keys) - 1]
        code, is_error = result
        if is_error:
            text = json.dumps({"error": {"code": code, "message": "x"}})
            return text, True, code
        text = json.dumps({"tosUrl": "https://b/v.mp4"})
        return text, False, None

    monkeypatch.setattr("tools.analysis.douyin_mcp._call_tool_once", fake_call)
    tos = asyncio.run(_resolve_tos_url("http://x", "https://v.douyin.com/abc/"))

    assert tos["tos_url"] == "https://b/v.mp4"
    assert len(seen_keys) == len(expected_keys)
    if expected_keys[0] == "base":
        # IN_PROGRESS path: all attempts reuse the exact same bare base key.
        assert len(set(seen_keys)) == 1
    else:
        # concrete-failure path: each retry gets a distinct (suffixed) key.
        assert len(set(seen_keys)) == 2


def test_resolve_tos_url_gives_up_after_max_retries(monkeypatch):
    """Non-retryable (or persistently failing) code stops after _MAX_RETRIES."""

    async def fake_call(_url, _tool, arguments):
        return (
            json.dumps({"error": {"code": "VALIDATION_ERROR"}}),
            True,
            "VALIDATION_ERROR",
        )

    monkeypatch.setattr("tools.analysis.douyin_mcp._call_tool_once", fake_call)
    num_sleeps = {"n": 0}
    monkeypatch.setattr(
        "tools.analysis.douyin_mcp._sleep", lambda _s: num_sleeps.__setitem__("n", num_sleeps["n"] + 1)
    )

    result = asyncio.run(_resolve_tos_url("http://x", "https://v.douyin.com/abc/"))
    assert result is None
    # VALIDATION_ERROR is not retryable -> exactly one attempt, no sleeps.
    assert num_sleeps["n"] == 0


def test_resolve_tos_url_retries_retryable_then_succeeds(monkeypatch):
    """A retryable code loops up to _MAX_RETRIES then tries a fresh attempt."""
    calls = {"n": 0}

    async def fake_call(_url, _tool, arguments):
        calls["n"] += 1
        if calls["n"] < 2:
            return (
                json.dumps({"error": {"code": "TOS_PRESIGN_FAILED"}}),
                True,
                "TOS_PRESIGN_FAILED",
            )
        return json.dumps({"tosUrl": "https://b/v2.mp4"}), False, None

    monkeypatch.setattr("tools.analysis.douyin_mcp._call_tool_once", fake_call)
    sleeps = {"n": 0}
    monkeypatch.setattr("tools.analysis.douyin_mcp._sleep", lambda _s: sleeps.__setitem__("n", sleeps["n"] + 1))

    tos = asyncio.run(_resolve_tos_url("http://x", "https://v.douyin.com/abc/"))
    assert tos["tos_url"] == "https://b/v2.mp4"
    assert calls["n"] == 2
    assert sleeps["n"] == 1


# ---------------------------------------------------------------- download


def test_download_url_streams_to_file(tmp_path):
    bytes_written = []

    class Resp:
        headers = {"content-length": "5"}

        def raise_for_status(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size=0):
            yield b"video"

    class FakeSession:
        def get(self, url, **kwargs):
            bytes_written.append(url)
            return Resp()

    out = _download_url(FakeSession(), "https://bucket.volces.com/v.mp4", tmp_path / "reference_video.mp4")
    assert out == str(tmp_path / "reference_video.mp4")
    assert (tmp_path / "reference_video.mp4").read_bytes() == b"video"


def test_download_via_mcp_returns_none_when_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv(MCP_ENV, raising=False)
    assert download_via_mcp("https://v.douyin.com/abc/", tmp_path) is None


def test_download_via_mcp_returns_path_on_success(tmp_path, monkeypatch):
    monkeypatch.setenv(MCP_ENV, "http://127.0.0.1:8000/mcp/viral-video")
    async def fake_resolve(*a, **k):
        return {
            "tos_url": "https://bucket.volces.com/v.mp4?X=1",
            "title": "出动！宝马四芒星",
            "aweme_id": "73456789012",
        }

    monkeypatch.setattr("tools.analysis.douyin_mcp._resolve_tos_url", fake_resolve)

    class Resp:
        headers = {"content-length": "5"}

        def raise_for_status(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size=0):
            yield b"video"

    class FakeSession:
        def get(self, url, **kwargs):
            return Resp()

    monkeypatch.setattr("tools.analysis.douyin_mcp.requests.Session", FakeSession)
    out = download_via_mcp("https://v.douyin.com/abc/", tmp_path)
    assert out.path == str(tmp_path / "reference_video.mp4")
    assert out.title == "出动！宝马四芒星"
    assert out.aweme_id == "73456789012"
    assert (tmp_path / "reference_video.mp4").read_bytes() == b"video"


def test_download_via_mcp_returns_none_when_resolve_fails(tmp_path, monkeypatch):
    monkeypatch.setenv(MCP_ENV, "http://127.0.0.1:8000/mcp/viral-video")
    monkeypatch.setattr(
        "tools.analysis.douyin_mcp._resolve_tos_url", lambda *a, **k: None
    )
    assert download_via_mcp("https://v.douyin.com/abc/", tmp_path) is None
    assert not (tmp_path / "reference_video.mp4").exists()


def test_download_via_mcp_swallows_transport_errors(tmp_path, monkeypatch):
    monkeypatch.setenv(MCP_ENV, "http://127.0.0.1:8000/mcp/viral-video")

    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("tools.analysis.douyin_mcp._resolve_tos_url", boom)
    assert download_via_mcp("https://v.douyin.com/abc/", tmp_path) is None


# ---------------------------------------------------------------- event-loop bridging


def test_run_coro_from_sync_context():
    """Outside any running loop, _run_coro behaves like asyncio.run."""
    async def probe():
        return "ok"

    # Guard: no loop should be running here.
    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False
    assert running is False
    assert _run_coro(probe()) == "ok"


def test_run_coro_from_running_loop():
    """Inside an active asyncio loop (MCP server scenario), _run_coro still works
    instead of raising RuntimeError."""
    async def run_from_loop():
        async def probe():
            return "inside-loop"

        return _run_coro(probe())

    result = asyncio.run(run_from_loop())
    assert result == "inside-loop"


def test_download_via_mcp_works_inside_running_loop(monkeypatch, tmp_path):
    """Multi-worker scenario: called from an active loop it must not raise and
    must fall back correctly rather than crashing the loop."""
    monkeypatch.setenv(MCP_ENV, "http://127.0.0.1:9/mcp/viral-video" )

    async def call_from_loop():
        # Real transport would fail to connect; it must return None (fallback),
        # never raise / crash the running loop.
        return download_via_mcp("https://v.douyin.com/abc/", tmp_path)

    result = asyncio.run(call_from_loop())
    assert result is None
    assert not (tmp_path / "reference_video.mp4").exists()