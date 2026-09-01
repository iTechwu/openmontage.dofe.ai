"""Unit tests for media-reference validation (plan §7, §12, §16.3)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from openmontage.media_references import (
    MEDIA_EXTENSIONS,
    MediaReferenceError,
    validate_media_references,
)


def _write(project: Path, relative: str, content: bytes = b"x") -> Path:
    target = project / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def test_accepts_relative_media_path_inside_project(tmp_path: Path) -> None:
    project = tmp_path / "job"
    _write(project, "assets/images/a.png")

    found = validate_media_references(
        {"asset": {"path": "assets/images/a.png"}},
        project,
        require_exists=True,
    )
    assert found == ["assets/images/a.png"]


def test_ignores_urls(tmp_path: Path) -> None:
    found = validate_media_references(
        {"source": {"url": "https://example.com/video.mp4"}},
        tmp_path / "job",
        require_exists=True,
    )
    assert found == []


def test_rejects_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(MediaReferenceError, match="absolute"):
        validate_media_references(
            {"path": "/etc/passwd.png"}, tmp_path / "job", require_exists=True
        )


def test_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(MediaReferenceError, match="traversal"):
        validate_media_references(
            {"path": "../other/assets/x.png"}, tmp_path / "job", require_exists=True
        )


def test_rejects_missing_file_when_required(tmp_path: Path) -> None:
    with pytest.raises(MediaReferenceError, match="does not exist"):
        validate_media_references(
            {"path": "assets/missing.png"}, tmp_path / "job", require_exists=True
        )
    # Not required: no error.
    assert validate_media_references(
        {"path": "assets/missing.png"}, tmp_path / "job", require_exists=False
    ) == ["assets/missing.png"]


def test_rejects_symlink_escaping_project(tmp_path: Path) -> None:
    project = tmp_path / "job"
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"secret")
    link = project / "assets" / "link.png"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    with pytest.raises(MediaReferenceError):
        validate_media_references(
            {"path": "assets/link.png"}, project, require_exists=True
        )


def test_verifies_sha256_sibling(tmp_path: Path) -> None:
    project = tmp_path / "job"
    _write(project, "assets/images/a.png", b"real bytes")
    good = hashlib.sha256(b"real bytes").hexdigest()

    validate_media_references(
        {"asset": {"path": "assets/images/a.png", "sha256": good}},
        project,
        require_exists=True,
    )
    with pytest.raises(MediaReferenceError, match="sha256 mismatch"):
        validate_media_references(
            {"asset": {"path": "assets/images/a.png", "sha256": "0" * 64}},
            project,
            require_exists=True,
        )


def test_walks_nested_lists_and_dicts(tmp_path: Path) -> None:
    project = tmp_path / "job"
    _write(project, "assets/video/clip.mp4")
    _write(project, "assets/audio/narration.wav")

    found = validate_media_references(
        {
            "manifest": {
                "assets": [
                    {"path": "assets/video/clip.mp4"},
                    {"path": "assets/audio/narration.wav"},
                ]
            }
        },
        project,
        require_exists=True,
    )
    assert found == ["assets/audio/narration.wav", "assets/video/clip.mp4"]


def test_media_extensions_include_common_formats() -> None:
    assert {".mp4", ".wav", ".mp3", ".png", ".srt"} <= MEDIA_EXTENSIONS
