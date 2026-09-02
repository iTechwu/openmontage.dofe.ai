"""Media-reference validation for client-submitted stage artifacts (plan §7, §12).

Media always lives on CI under ``projects/<job_id>/``; a client artifact may
only *reference* it — by project-relative path, never by absolute path, never
across Jobs, never through a symlink that escapes the project, and never as
inline binary. This module walks submitted artifact JSON and enforces that
contract before a checkpoint is written.

Validation rules:

1. A string value whose suffix is a known media extension is a media
   reference. It must be a relative path inside the project directory; when
   ``require_exists`` is set (``completed`` / ``awaiting_human`` submissions)
   the referenced file must exist.
2. Any path-ish string (media reference or a ``*_path`` / ``*_file`` field)
   must not be absolute and must not contain ``..`` segments.
3. When a dict carries a ``sha256`` field next to media references, each
   existing referenced file's SHA-256 must match.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

MEDIA_EXTENSIONS = frozenset({
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
    ".wav", ".mp3", ".m4a", ".flac",
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".srt",
})

# Keys whose string values are treated as paths even without a media suffix.
_PATH_KEY_SUFFIXES = ("_path", "_file", "_paths", "_files")
_PATH_KEY_EXACT = {"path", "file", "file_path", "filepath"}


class MediaReferenceError(ValueError):
    """Raised when an artifact references media outside the allowed contract."""


def _looks_like_media(value: str) -> bool:
    return Path(value).suffix.lower() in MEDIA_EXTENSIONS


def _is_path_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _PATH_KEY_EXACT or lowered.endswith(_PATH_KEY_SUFFIXES)


def _validate_one_reference(
    value: str,
    project_dir: Path,
    *,
    require_exists: bool,
    context: str,
) -> str | None:
    """Validate a single reference; return the normalized relative path.

    Returns ``None`` for non-reference strings (e.g. URLs, prose).
    """
    if "://" in value:
        # URLs are not filesystem references (source links, provider pages).
        return None
    if "\x00" in value:
        raise MediaReferenceError(
            f"{context}: media references must not contain NUL bytes: {value!r}"
        )
    if value.startswith("/") or (len(value) > 1 and value[1] == ":"):
        raise MediaReferenceError(
            f"{context}: absolute paths are not allowed in client artifacts: {value!r}; "
            "use a project-relative path"
        )
    parts = Path(value).parts
    if ".." in parts:
        raise MediaReferenceError(
            f"{context}: path traversal is not allowed in client artifacts: {value!r}"
        )
    resolved = (project_dir / value).resolve()
    if resolved != project_dir and project_dir not in resolved.parents:
        raise MediaReferenceError(
            f"{context}: reference escapes the project directory: {value!r}"
        )
    if require_exists and not resolved.is_file():
        raise MediaReferenceError(
            f"{context}: referenced media file does not exist on CI: {value!r}"
        )
    return str(Path(value))


def _verify_sha256(relative: str, expected: str, project_dir: Path, context: str) -> None:
    target = (project_dir / relative).resolve()
    if project_dir not in target.parents:
        raise MediaReferenceError(
            f"{context}: reference escapes the project directory: {relative!r}"
        )
    if not target.is_file():
        raise MediaReferenceError(
            f"{context}: cannot verify sha256, file missing: {relative!r}"
        )
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != expected.lower():
        raise MediaReferenceError(
            f"{context}: sha256 mismatch for {relative!r}: artifact declares "
            f"{expected}, file on CI hashes to {digest}"
        )


def validate_media_references(
    node: Any,
    project_dir: str | Path,
    *,
    require_exists: bool,
    _context: str = "artifact",
) -> list[str]:
    """Walk artifact JSON and validate every media reference it contains.

    Returns the sorted list of validated project-relative media paths. Raises
    ``MediaReferenceError`` on the first contract violation.
    """
    root = Path(project_dir).resolve()
    found: list[str] = []

    def walk(value: Any, context: str) -> None:
        if isinstance(value, dict):
            media_in_dict: list[str] = []
            for key, item in value.items():
                if isinstance(item, str) and (_looks_like_media(item) or (_is_path_key(str(key)) and item)):
                    # Existence is enforced for media references only; a plain
                    # path-key string gets form validation (relative, no
                    # traversal, inside the project) but may name a file the
                    # CI tooling is about to create (e.g. renders/final.mp4
                    # declared before compose runs).
                    validated = _validate_one_reference(
                        item, root,
                        require_exists=require_exists and _looks_like_media(item),
                        context=f"{context}.{key}",
                    )
                    if validated is not None and _looks_like_media(validated):
                        media_in_dict.append(validated)
                        found.append(validated)
                elif isinstance(item, (dict, list)):
                    walk(item, f"{context}.{key}")
            expected_hash = value.get("sha256")
            if isinstance(expected_hash, str) and expected_hash.strip():
                for relative in media_in_dict:
                    _verify_sha256(relative, expected_hash.strip(), root, context)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, str) and _looks_like_media(item):
                    validated = _validate_one_reference(
                        item, root, require_exists=require_exists,
                        context=f"{context}[{index}]",
                    )
                    if validated is not None:
                        found.append(validated)
                elif isinstance(item, (dict, list)):
                    walk(item, f"{context}[{index}]")

    walk(node, _context)
    return sorted(set(found))
