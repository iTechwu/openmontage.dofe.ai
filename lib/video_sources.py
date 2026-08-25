"""URL normalization and safety checks for reference-video ingestion."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests


_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_DOUYIN_ID_RE = re.compile(r"/(?:share/)?video/(\d+)")
_TRAILING_SHARE_PUNCTUATION = ")]}>，。！？；：、"
_DOUYIN_SHORT_HOSTS = {"v.douyin.com", "iesdouyin.com", "www.iesdouyin.com", "m.douyin.com"}
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "Chrome/131.0 Mobile Safari/537.36"
)


class VideoSourceError(ValueError):
    """Raised when a reference URL is invalid or unsafe to fetch."""


def extract_video_url(value: str) -> str:
    """Extract the first HTTP(S) URL from a bare URL or pasted share text."""
    match = _URL_RE.search(value.strip())
    if not match:
        raise VideoSourceError("Expected an http:// or https:// video URL")
    return match.group(0).rstrip(_TRAILING_SHARE_PUNCTUATION)


def detect_video_platform(value: str) -> str:
    """Return the platform label used by analysis artifacts."""
    try:
        url = extract_video_url(value)
    except VideoSourceError:
        return "local_file"
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if host == "douyin.com" or host.endswith(".douyin.com") or host.endswith(".iesdouyin.com"):
        return "douyin"
    if "youtube.com" in host and "/shorts/" in path:
        return "shorts"
    if host == "youtu.be" or "youtube.com" in host:
        return "youtube"
    if "instagram.com" in host:
        return "instagram"
    if "tiktok.com" in host:
        return "tiktok"
    if "vimeo.com" in host:
        return "vimeo"
    if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return "twitter"
    return "other_url"


def validate_public_http_url(url: str, *, resolve_dns: bool = True) -> None:
    """Reject non-HTTP and private-network targets before a server-side fetch.

    ``OPENMONTAGE_ALLOW_PRIVATE_URLS`` (1/true/yes/on) opts into accepting
    private/non-global targets (e.g. localhost, 127.0.0.1, 172.x, a
    ``host.docker.internal`` service). This is an intentional escape hatch for
    internal deployments where the DSH harness serves a reference video on the
    same host and OpenMontage must fetch it; the default stays strict.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise VideoSourceError("Only absolute HTTP(S) video URLs are supported")
    allow_private = os.environ.get("OPENMONTAGE_ALLOW_PRIVATE_URLS", "").strip().lower() in {
        "1", "true", "yes", "y", "on",
    }
    host = parsed.hostname.lower().rstrip(".")
    if (host == "localhost" or host.endswith(".localhost")) and not allow_private:
        raise VideoSourceError("Localhost video URLs are not allowed")
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global and not allow_private:
        raise VideoSourceError("Private or non-global video URL targets are not allowed")
    if not resolve_dns or literal_ip is not None:
        return
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(host, parsed.port or 443)}
    except socket.gaierror as exc:
        raise VideoSourceError(f"Could not resolve video URL host: {host}") from exc
    if not addresses:
        raise VideoSourceError(f"Could not resolve video URL host: {host}")
    for address in addresses:
        if not ipaddress.ip_address(address).is_global and not allow_private:
            raise VideoSourceError("Video URL resolves to a private or non-global address")


def _douyin_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    match = _DOUYIN_ID_RE.search(parsed.path)
    if match:
        return match.group(1)
    for key in ("modal_id", "aweme_id", "item_id"):
        values = parse_qs(parsed.query).get(key)
        if values and values[0].isdigit():
            return values[0]
    return None


def resolve_douyin_short_url(url: str, *, max_redirects: int = 6) -> str:
    """Resolve a Douyin short/share URL one validated redirect at a time."""
    current = url
    session = requests.Session()
    headers = {"User-Agent": _BROWSER_USER_AGENT}
    for _ in range(max_redirects + 1):
        validate_public_http_url(current)
        response = session.get(
            current,
            headers=headers,
            allow_redirects=False,
            stream=True,
            timeout=(5, 15),
        )
        try:
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("location")
                if not location:
                    raise VideoSourceError("Douyin redirect did not include a target URL")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            video_id = _douyin_video_id(str(response.url)) or _douyin_video_id(current)
            if video_id:
                return f"https://www.douyin.com/video/{video_id}"
            return current
        finally:
            response.close()
    raise VideoSourceError("Douyin short URL exceeded the redirect limit")


def normalize_video_url(value: str) -> str:
    """Normalize share text and Douyin variants into a downloader-ready URL."""
    url = extract_video_url(value)
    validate_public_http_url(url)
    platform = detect_video_platform(url)
    if platform != "douyin":
        return url
    video_id = _douyin_video_id(url)
    if video_id:
        return f"https://www.douyin.com/video/{video_id}"
    host = (urlparse(url).hostname or "").lower()
    if host in _DOUYIN_SHORT_HOSTS:
        return resolve_douyin_short_url(url)
    raise VideoSourceError("Could not find a Douyin video ID in the supplied URL")


def resolve_cookie_file(explicit_path: str | None) -> str | None:
    """Validate an optional Netscape-format cookie file path."""
    if not explicit_path:
        return None
    path = Path(explicit_path).expanduser().resolve()
    if not path.is_file():
        raise VideoSourceError(f"Cookie file not found: {path}")
    return str(path)


BROWSER_USER_AGENT = _BROWSER_USER_AGENT
