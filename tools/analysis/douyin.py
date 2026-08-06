"""Cookie-free downloader for public Douyin share pages.

Douyin's public mobile share page embeds a structured JSON payload. This module
parses that payload and downloads the public play URL without routing through
yt-dlp.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests

from lib.video_sources import BROWSER_USER_AGENT, VideoSourceError, validate_public_http_url


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self._inside_script = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self._inside_script = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._inside_script:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._inside_script:
            self.scripts.append("".join(self._parts))
            self._inside_script = False
            self._parts = []


def _video_id(url: str) -> str:
    candidate = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
    if not candidate.isdigit():
        raise VideoSourceError("Could not extract the Douyin video ID")
    return candidate


def parse_douyin_share_page(html: str) -> dict[str, Any]:
    """Parse the SSR JSON payload from a public Douyin mobile share page."""
    parser = _ScriptCollector()
    parser.feed(html)
    prefix = "window._ROUTER_DATA = "
    script = next((item.strip() for item in parser.scripts if item.strip().startswith(prefix)), None)
    if script is None:
        raise VideoSourceError("Douyin share page did not expose its public video payload")
    try:
        router_data = json.loads(script[len(prefix):].rstrip(";"))
        loader_data = router_data["loaderData"]
        page_data = next(
            value
            for key, value in loader_data.items()
            if key.endswith("/page") and isinstance(value, dict) and "videoInfoRes" in value
        )
        item = page_data["videoInfoRes"]["item_list"][0]
        video = item["video"]
        play_url = video["play_addr"]["url_list"][0]
    except (KeyError, IndexError, StopIteration, TypeError, json.JSONDecodeError) as exc:
        raise VideoSourceError("Douyin share payload did not contain a playable video") from exc

    # The public non-watermarked endpoint uses the same signed video id.
    play_url = play_url.replace("/playwm/", "/play/")
    author = item.get("author") or {}
    statistics = item.get("statistics") or {}
    created = item.get("create_time")
    upload_date = ""
    if isinstance(created, (int, float)):
        upload_date = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y%m%d")
    return {
        "id": str(item.get("aweme_id") or ""),
        "title": str(item.get("desc") or "Douyin reference video"),
        "duration": round(float(video.get("duration") or 0) / 1000, 3),
        "uploader": str(author.get("nickname") or author.get("unique_id") or ""),
        "upload_date": upload_date,
        "description": str(item.get("desc") or "")[:500],
        "view_count": int(statistics.get("play_count") or 0),
        "like_count": int(statistics.get("digg_count") or 0),
        "resolution": f"{int(video.get('width') or 0)}x{int(video.get('height') or 0)}",
        "fps": 0,
        "play_url": play_url,
    }


class DouyinShareClient:
    """Fetch metadata and media through Douyin's public mobile share page."""

    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def extract(self, canonical_url: str) -> dict[str, Any]:
        video_id = _video_id(canonical_url)
        share_url = f"https://www.iesdouyin.com/share/video/{video_id}/"
        validate_public_http_url(share_url)
        response = self.session.get(
            share_url,
            headers={"User-Agent": BROWSER_USER_AGENT},
            timeout=(5, 30),
        )
        response.raise_for_status()
        data = parse_douyin_share_page(response.text)
        data["canonical_url"] = canonical_url
        return data

    def download(self, metadata: dict[str, Any], output_path: Path) -> str:
        play_url = str(metadata["play_url"])
        validate_public_http_url(play_url)
        max_bytes = 2_000 * 1024 * 1024
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.session.get(
            play_url,
            headers={"User-Agent": BROWSER_USER_AGENT, "Referer": "https://www.iesdouyin.com/"},
            allow_redirects=True,
            stream=True,
            timeout=(10, 120),
        ) as response:
            response.raise_for_status()
            length = int(response.headers.get("content-length") or 0)
            if length and length > max_bytes:
                raise VideoSourceError("Douyin video exceeds the 2000 MB download safety limit")
            written = 0
            with output_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise VideoSourceError("Douyin video exceeds the 2000 MB download safety limit")
                    handle.write(chunk)
        return str(output_path)
