"""Unit tests for dofe media helpers (dev-guide §5.6, §6.3, §8.1)."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.dofe.media import file_to_data_uri, is_https_url, resolve_image_source, sanitize_for_log


def test_data_uri_png(tmp_path):
    path = tmp_path / "ref.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"data")
    uri = file_to_data_uri(path)
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(";base64,", 1)[1]).startswith(b"\x89PNG")


@pytest.mark.parametrize("name,mime", [("x.jpg", "image/jpeg"), ("x.jpeg", "image/jpeg"), ("x.webp", "image/webp")])
def test_data_uri_mime_by_extension(tmp_path, name, mime):
    path = tmp_path / name
    path.write_bytes(b"bytes")
    assert file_to_data_uri(path).startswith(f"data:{mime};base64,")


def test_data_uri_5mb_cap(tmp_path):
    path = tmp_path / "big.png"
    path.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="limit"):
        file_to_data_uri(path)


def test_data_uri_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        file_to_data_uri(tmp_path / "nope.png")


def test_data_uri_unsupported_extension(tmp_path):
    path = tmp_path / "ref.gif"
    path.write_bytes(b"bytes")
    with pytest.raises(ValueError, match="Unsupported image extension"):
        file_to_data_uri(path)


def test_is_https_url():
    assert is_https_url("https://cdn.test/x.png") is True
    assert is_https_url("http://cdn.test/x.png") is False
    assert is_https_url("ftp://cdn.test/x.png") is False
    assert is_https_url(None) is False
    assert is_https_url("not a url") is False


def test_resolve_image_source_url_passthrough():
    assert resolve_image_source(url="https://cdn.test/x.png") == "https://cdn.test/x.png"


def test_resolve_image_source_rejects_non_https():
    with pytest.raises(ValueError, match="https"):
        resolve_image_source(url="http://cdn.test/x.png")


def test_resolve_image_source_path_to_data_uri(tmp_path):
    path = tmp_path / "ref.png"
    path.write_bytes(b"\x89PNG")
    assert resolve_image_source(path=path).startswith("data:image/png;base64,")


def test_sanitize_redacts_data_uri():
    raw = "data:image/png;base64," + "A" * 5000
    out = sanitize_for_log(raw)
    assert "data:<redacted>" in out
    assert "AAAA" not in out  # the base64 payload is fully redacted, not just the prefix


def test_sanitize_truncates_long_text():
    out = sanitize_for_log("x" * 2000, limit=100)
    assert out.endswith("…<truncated>")
    assert len(out) < 200
