"""Media helpers for the dofe gateway: local-file → data URI, URL checks, log redaction.

The gateway accepts local images as ``data:<mime>;base64,<b64>`` in an
``image_url.url`` slot (verified, dev-guide §2.3/§5.6). Video/audio files are
too large for inline data URIs; callers must supply public https URLs.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from urllib.parse import urlparse

# Local images are the only media inlined as a data URI. Cap matches the
# gateway-accepted ceiling measured in P0 (dev-guide §5.6).
MAX_DATA_URI_BYTES = 5 * 1024 * 1024

_EXTENSION_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# Matches a full inline data URI (prefix + base64 payload) so the entire
# multi-MB image body can be redacted from a log line, not just its prefix.
_DATA_URI_RE = re.compile(r"data:[^;]*;base64,[A-Za-z0-9+/=\s]*")


def guess_image_mime(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    mime = _EXTENSION_MIME.get(suffix)
    if mime is None:
        raise ValueError(
            f"Unsupported image extension {suffix!r} for dofe data URI. "
            f"Use one of: {', '.join(sorted(_EXTENSION_MIME))}."
        )
    return mime


def file_to_data_uri(path: str | Path, *, max_bytes: int = MAX_DATA_URI_BYTES) -> str:
    """Read a local image file and return a ``data:<mime>;base64,<b64>`` string.

    Raises ``ValueError`` for missing files, unsupported types, or files over
    the ``max_bytes`` ceiling (clear error rather than a 413 from the gateway).
    """

    image_path = Path(path)
    if not image_path.is_file():
        raise FileNotFoundError(f"Image not found: {image_path}")
    mime = guess_image_mime(image_path)
    raw = image_path.read_bytes()
    if not raw:
        raise ValueError(f"Image file is empty: {image_path}")
    if len(raw) > max_bytes:
        raise ValueError(
            f"Image {image_path.name} is {len(raw)} bytes; dofe data URI limit is "
            f"{max_bytes} bytes. Provide a public https URL instead."
        )
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def is_https_url(value: str | None) -> bool:
    """True only for an absolute https URL (gateway artifacts must be https)."""

    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def resolve_image_source(url: str | None = None, path: str | Path | None = None) -> str:
    """Return an https URL or a data URI for one image input.

    A supplied https URL is passed through verbatim; a local path is inlined as
    a data URI. Anything else raises ``ValueError``.
    """

    if url:
        if not is_https_url(url):
            raise ValueError(f"dofe image URL must be https: {url!r}")
        return url
    if path:
        return file_to_data_uri(path)
    raise ValueError("dofe image input requires an https URL or a local file path")


def sanitize_for_log(value: Any, *, limit: int = 500) -> str:
    """Render a value safe for logs/errors: redact data URIs, truncate.

    The dofe request body can carry a multi-MB base64 image; never let one reach
    a log line or a ToolResult error (dev-guide §6.3).
    """

    text = "" if value is None else str(value)
    text = _DATA_URI_RE.sub("data:<redacted>", text)
    if len(text) > limit:
        text = text[:limit] + "…<truncated>"
    return text
