"""Video downloader tool with platform-specific routing.

Downloads video, audio, or subtitles from YouTube, Shorts, Instagram Reels,
TikTok, Douyin, and 1000+ other sites. Douyin uses its dedicated public-share
downloader; other platforms use yt-dlp. Designed for reference video analysis
at analysis quality (720p), not production quality.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolStability,
    ToolStatus,
    ToolTier,
    ToolRuntime,
)
from lib.video_sources import (
    BROWSER_USER_AGENT,
    VideoSourceError,
    detect_video_platform,
    normalize_video_url,
    resolve_cookie_file,
)


class VideoDownloader(BaseTool):
    name = "video_downloader"
    version = "0.1.0"
    tier = ToolTier.SOURCE
    capability = "source_ingest"
    provider = "yt-dlp"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["python:yt_dlp", "cmd:ffmpeg"]
    install_instructions = (
        "Install yt-dlp: pip install yt-dlp\n"
        "For YouTube support, also install a supported JS runtime: "
        "https://deno.land/#installation\n"
        "For restricted sites, pass cookie_file or set OPENMONTAGE_YTDLP_COOKIES. "
        "Public Douyin videos use the cookie-free dedicated share-page downloader."
    )
    agent_skills = ["video-download"]

    capabilities = [
        "download_video",
        "download_audio",
        "download_subtitles",
        "extract_metadata",
    ]

    best_for = [
        "downloading reference video from URL",
        "extracting audio from online video",
        "downloading subtitles from YouTube",
        "getting video metadata without downloading",
    ]

    not_good_for = [
        "downloading entire playlists",
        "downloading DRM-protected content",
    ]

    input_schema = {
        "type": "object",
        "required": ["url", "output_dir"],
        "properties": {
            "url": {"type": "string", "description": "Video URL to download"},
            "output_dir": {"type": "string", "description": "Directory for downloaded files"},
            "format": {
                "type": "string",
                "enum": ["video", "audio_only", "subtitles_only", "metadata_only"],
                "default": "video",
                "description": "What to download",
            },
            "max_resolution": {
                "type": "string",
                "enum": ["360p", "480p", "720p", "1080p"],
                "default": "720p",
                "description": "Maximum video resolution (for analysis, 720p is sufficient)",
            },
            "max_duration_seconds": {
                "type": "integer",
                "default": 600,
                "description": "Reject videos longer than this (safety limit)",
            },
            "cookie_file": {
                "type": "string",
                "description": "Optional Netscape-format cookies.txt path",
            },
        },
    }

    output_schema = {
        "type": "object",
        "properties": {
            "video_path": {"type": ["string", "null"]},
            "audio_path": {"type": ["string", "null"]},
            "subtitle_path": {"type": ["string", "null"]},
            "metadata": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "duration": {"type": "number"},
                    "uploader": {"type": "string"},
                    "upload_date": {"type": "string"},
                    "description": {"type": "string"},
                    "view_count": {"type": "integer"},
                    "like_count": {"type": "integer"},
                },
            },
            "platform": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=2000,
        network_required=True,
    )
    idempotency_key_fields = ["url", "format", "max_resolution"]
    side_effects = ["downloads media files to output_dir"]
    resume_support_value = "from_start"
    user_visible_verification = [
        "Check downloaded file plays correctly",
        "Verify resolution matches requested max",
    ]

    # --- Resolution mapping ---
    _RES_MAP = {
        "360p": 360,
        "480p": 480,
        "720p": 720,
        "1080p": 1080,
    }

    def _detect_platform(self, url: str) -> str:
        """Detect platform from URL."""
        return detect_video_platform(url)

    def _ydl_options(self, cookie_file: str | None) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 3,
            "fragment_retries": 3,
            "http_headers": {"User-Agent": BROWSER_USER_AGENT},
        }
        if cookie_file:
            options["cookiefile"] = cookie_file
        return options

    def _extract_metadata(self, url: str, cookie_file: str | None = None) -> dict:
        """Extract metadata without downloading."""
        import yt_dlp

        ydl_opts = self._ydl_options(cookie_file)
        ydl_opts["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info is None:
                    return {"error": "No info extracted", "title": "", "duration": 0}
                return {
                    "title": info.get("title", ""),
                    "duration": info.get("duration", 0),
                    "uploader": info.get("uploader", info.get("channel", "")),
                    "upload_date": info.get("upload_date", ""),
                    "description": (info.get("description", "") or "")[:500],
                    "view_count": info.get("view_count", 0),
                    "like_count": info.get("like_count", 0),
                    "resolution": f"{info.get('width', 0)}x{info.get('height', 0)}",
                    "fps": info.get("fps", 0),
                }
        except Exception as e:
            return {"error": str(e), "title": "", "duration": 0}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        raw_url = inputs["url"]
        output_dir = Path(inputs["output_dir"])
        dl_format = inputs.get("format", "video")
        max_res = inputs.get("max_resolution", "720p")
        max_duration = inputs.get("max_duration_seconds", 600)

        try:
            url = normalize_video_url(raw_url)
        except VideoSourceError as exc:
            return ToolResult(success=False, error=str(exc))

        platform = self._detect_platform(url)
        try:
            cookie_file = (
                None
                if platform == "douyin"
                else resolve_cookie_file(
                    inputs.get("cookie_file")
                    or os.environ.get("OPENMONTAGE_YTDLP_COOKIES")
                )
            )
        except VideoSourceError as exc:
            return ToolResult(success=False, error=str(exc))

        output_dir.mkdir(parents=True, exist_ok=True)
        start = time.time()

        douyin_client = None
        if platform == "douyin":
            try:
                from tools.analysis.douyin import DouyinShareClient

                douyin_client = DouyinShareClient()
                metadata = douyin_client.extract(url)
                metadata["extractor"] = "douyin_public_share"
            except Exception as exc:
                return ToolResult(
                    success=False,
                    error=f"Douyin extraction failed: {exc}",
                    data={"platform": platform, "resolved_url": url},
                    duration_seconds=round(time.time() - start, 2),
                )
        else:
            metadata = self._extract_metadata(url, cookie_file)

        # Check duration limit
        duration = metadata.get("duration", 0)
        if duration and duration > max_duration:
            return ToolResult(
                success=False,
                error=(
                    f"Video is {duration}s, exceeds max_duration_seconds={max_duration}. "
                    f"Increase the limit or use a shorter video."
                ),
                data={"metadata": metadata, "platform": platform},
            )

        if dl_format == "metadata_only":
            return ToolResult(
                success=True,
                data={
                    "video_path": None,
                    "audio_path": None,
                    "subtitle_path": None,
                    "metadata": metadata,
                    "platform": platform,
                    "source_url": raw_url,
                    "resolved_url": url,
                },
                duration_seconds=round(time.time() - start, 2),
            )

        video_path = None
        audio_path = None
        subtitle_path = None

        try:
            if dl_format == "video":
                if douyin_client is not None:
                    video_path = douyin_client.download(metadata, output_dir / "reference_video.mp4")
                    audio_path = self._extract_audio_track(video_path, output_dir)
                else:
                    video_path, audio_path = self._download_video(
                        url, output_dir, max_res, cookie_file
                    )
            elif dl_format == "audio_only":
                if douyin_client is not None:
                    video_path = douyin_client.download(metadata, output_dir / "reference_video.mp4")
                    audio_path = self._extract_audio_track(video_path, output_dir)
                    if audio_path is None:
                        return ToolResult(
                            success=False,
                            error="Douyin audio extraction failed after downloading the video.",
                            data={"metadata": metadata, "platform": platform},
                            artifacts=[video_path],
                            duration_seconds=round(time.time() - start, 2),
                        )
                else:
                    audio_path = self._download_audio(url, output_dir, cookie_file)
            elif dl_format == "subtitles_only":
                if douyin_client is not None:
                    return ToolResult(
                        success=False,
                        error=(
                            "The dedicated Douyin downloader does not provide subtitles; "
                            "download the video or audio and transcribe it instead."
                        ),
                        data={"metadata": metadata, "platform": platform},
                        duration_seconds=round(time.time() - start, 2),
                    )
                subtitle_path = self._download_subtitles(url, output_dir, cookie_file)
        except Exception as e:
            elapsed = time.time() - start
            return ToolResult(
                success=False,
                error=f"Download failed: {e}",
                data={"metadata": metadata, "platform": platform},
                duration_seconds=round(elapsed, 2),
            )

        elapsed = time.time() - start
        artifacts = [p for p in [video_path, audio_path, subtitle_path] if p]

        return ToolResult(
            success=True,
            data={
                "video_path": video_path,
                "audio_path": audio_path,
                "subtitle_path": subtitle_path,
                "metadata": metadata,
                "platform": platform,
                "source_url": raw_url,
                "resolved_url": url,
            },
            artifacts=artifacts,
            duration_seconds=round(elapsed, 2),
        )

    def _download_video(
        self, url: str, output_dir: Path, max_res: str, cookie_file: str | None = None
    ) -> tuple[str | None, str | None]:
        """Download video + extract audio track."""
        import yt_dlp

        height = self._RES_MAP.get(max_res, 720)
        video_out = str(output_dir / "reference_video.%(ext)s")

        ydl_opts = self._ydl_options(cookie_file)
        ydl_opts.update({
            "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
            "merge_output_format": "mp4",
            "outtmpl": video_out,
        })
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Find the downloaded video file
        video_path = self._find_downloaded(output_dir, "reference_video", ["mp4", "mkv", "webm"])

        # Extract audio separately for transcription
        audio_path = self._extract_audio_track(video_path, output_dir) if video_path else None

        return video_path, audio_path

    def _extract_audio_track(self, video_path: str, output_dir: Path) -> str | None:
        audio_out = output_dir / "reference_audio.wav"
        try:
            audio_cmd = [
                "ffmpeg", "-y", "-i", video_path, "-vn",
                "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(audio_out),
            ]
            self.run_command(audio_cmd, timeout=180)
            return str(audio_out) if audio_out.exists() else None
        except Exception:
            return None

    def _download_audio(
        self, url: str, output_dir: Path, cookie_file: str | None = None
    ) -> str | None:
        """Download audio only."""
        import yt_dlp

        audio_out = str(output_dir / "reference_audio.%(ext)s")
        ydl_opts = self._ydl_options(cookie_file)
        ydl_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
                "preferredquality": "0",
            }],
            "outtmpl": audio_out,
        })
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return self._find_downloaded(output_dir, "reference_audio", ["wav", "mp3", "m4a", "opus"])

    def _download_subtitles(
        self, url: str, output_dir: Path, cookie_file: str | None = None
    ) -> str | None:
        """Download subtitles only."""
        import yt_dlp

        sub_out = str(output_dir / "reference_subs.%(ext)s")
        ydl_opts = self._ydl_options(cookie_file)
        ydl_opts.update({
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en"],
            "subtitlesformat": "srt",
            "skip_download": True,
            "outtmpl": sub_out,
        })
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception:
            pass
        return self._find_downloaded(output_dir, "reference_subs", ["srt", "vtt", "ass"])

    def _find_downloaded(
        self, output_dir: Path, prefix: str, extensions: list[str]
    ) -> str | None:
        """Find a downloaded file by prefix and possible extensions."""
        for ext in extensions:
            candidates = list(output_dir.glob(f"{prefix}*.{ext}"))
            if candidates:
                return str(candidates[0])
        return None
