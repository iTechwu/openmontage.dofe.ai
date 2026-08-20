"""Credential-free local renderer for the deterministic video smoke pipeline."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from lib.checkpoint import read_checkpoint, write_checkpoint
from openmontage.pipeline_executor import (
    PipelineExecutionCancelled,
    PipelineExecutionError,
    PipelineExecutionResult,
    PipelineExecutor,
    StageAssignment,
)
from tools.dofe.delegation import DelegatedModelCredential


PIPELINE_NAME = "deterministic-video-smoke"
_RESOLUTION = re.compile(r"^(?P<width>[1-9][0-9]*)x(?P<height>[1-9][0-9]*)$")
_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


class DeterministicVideoSmokeExecutor:
    """Route one health-check pipeline to ffmpeg and delegate every other pipeline."""

    def __init__(
        self,
        fallback: PipelineExecutor,
        *,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
    ) -> None:
        self.fallback = fallback
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

    def requires_model_credential(self, assignment: StageAssignment) -> bool:
        if assignment.pipeline == PIPELINE_NAME:
            return False
        resolver = getattr(self.fallback, "requires_model_credential", None)
        return bool(resolver(assignment)) if callable(resolver) else True

    def execute(
        self,
        assignment: StageAssignment,
        *,
        credential: DelegatedModelCredential | None = None,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> PipelineExecutionResult:
        if assignment.pipeline != PIPELINE_NAME:
            return self.fallback.execute(
                assignment,
                credential=credential,
                cancellation_requested=cancellation_requested,
            )
        if assignment.stage != "compose":
            raise PipelineExecutionError(
                f"{PIPELINE_NAME} only supports the compose stage"
            )
        if credential is not None:
            raise PipelineExecutionError(
                f"{PIPELINE_NAME} must execute without a model credential"
            )
        if cancellation_requested is not None and cancellation_requested():
            raise PipelineExecutionCancelled()

        width, height, fps, duration = _validated_output(assignment.request)
        assignment_path = _write_assignment(assignment)
        render_path = assignment.project_dir / "renders" / "final.mp4"
        render_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        video_filter = (
            "drawbox=x='mod(t*180,iw+260)-260':y=ih-96:w=260:h=10:"
            "color=0x2DD4BF:t=fill,"
            f"drawtext=fontfile={_FONT_PATH}:text='OpenMontage READY':"
            "fontcolor=white:fontsize=56:x=(w-text_w)/2:y=(h-text_h)/2"
        )
        _run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x0B1F3A:s={width}x{height}:r={fps}:d={duration}",
                "-vf",
                video_filter,
                "-t",
                _number(duration),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-an",
                str(render_path),
            ],
            timeout=90,
        )
        if cancellation_requested is not None and cancellation_requested():
            raise PipelineExecutionCancelled()

        probe = _probe(self.ffprobe, render_path)
        _verify_probe(
            probe,
            width=width,
            height=height,
            fps=fps,
            duration=duration,
        )
        stream = probe["streams"][0]
        actual_duration = float(probe["format"]["duration"])
        render_report = {
            "version": "1.0",
            "outputs": [
                {
                    "path": str(render_path.relative_to(assignment.project_dir)),
                    "format": "mp4",
                    "codec": str(stream["codec_name"]),
                    "resolution": f"{width}x{height}",
                    "fps": float(fps),
                    "duration_seconds": actual_duration,
                    "file_size_bytes": render_path.stat().st_size,
                    "platform_target": "agentspace-openmontage-smoke",
                }
            ],
            "render_time_seconds": max(0.0, time.monotonic() - started),
            "warnings": [],
            "verification_notes": [
                "Rendered locally without external media or model calls.",
                "Validated with ffprobe before checkpoint completion.",
            ],
            "render_grammar": "screen-demo",
            "metadata": {
                "deterministic": True,
                "pixel_format": str(stream["pix_fmt"]),
            },
        }
        write_checkpoint(
            assignment.projects_dir,
            assignment.project_id,
            assignment.stage,
            "completed",
            {"render_report": render_report},
            pipeline_type=assignment.pipeline,
            checkpoint_policy="guided",
        )
        checkpoint = read_checkpoint(
            assignment.projects_dir,
            assignment.project_id,
            assignment.stage,
        )
        if checkpoint is None:
            raise PipelineExecutionError("Deterministic render checkpoint was not persisted")
        return PipelineExecutionResult(
            status="completed",
            checkpoint=checkpoint,
            assignment_path=assignment_path,
        )


def _validated_output(request: dict) -> tuple[int, int, int, float]:
    output = request.get("output")
    brief = request.get("brief")
    if not isinstance(output, dict) or not isinstance(brief, dict):
        raise PipelineExecutionError("Deterministic render request is missing output or brief")
    resolution = output.get("resolution") or "1280x720"
    match = _RESOLUTION.fullmatch(str(resolution))
    if match is None:
        raise PipelineExecutionError("Deterministic render resolution is invalid")
    width = int(match.group("width"))
    height = int(match.group("height"))
    fps = output.get("fps") or 30
    duration = brief.get("durationSeconds") or 6
    if (
        width > 1920
        or height > 1080
        or width % 2
        or height % 2
        or not isinstance(fps, int)
        or isinstance(fps, bool)
        or not 1 <= fps <= 60
        or not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not 0 < float(duration) <= 30
    ):
        raise PipelineExecutionError(
            "Deterministic render is limited to even dimensions up to 1920x1080, "
            "1-60 fps, and 0-30 seconds"
        )
    return width, height, fps, float(duration)


def _write_assignment(assignment: StageAssignment) -> Path:
    directory = assignment.project_dir / ".openmontage" / "assignments"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{assignment.stage}-attempt-{assignment.stage_attempt}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(assignment.to_wire(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _run(command: list[str], *, timeout: float) -> None:
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise PipelineExecutionError(
            f"Deterministic ffmpeg render failed: {str(detail)[-500:]}"
        ) from exc


def _probe(ffprobe: str, path: Path) -> dict:
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,pix_fmt,width,height,r_frame_rate:format=duration",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        value = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise PipelineExecutionError("Deterministic ffprobe validation failed") from exc
    if not isinstance(value, dict):
        raise PipelineExecutionError("Deterministic ffprobe returned an invalid document")
    return value


def _verify_probe(
    probe: dict,
    *,
    width: int,
    height: int,
    fps: int,
    duration: float,
) -> None:
    streams = probe.get("streams")
    format_value = probe.get("format")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(format_value, dict):
        raise PipelineExecutionError("Deterministic render has no single video stream")
    stream = streams[0]
    if not isinstance(stream, dict):
        raise PipelineExecutionError("Deterministic render stream metadata is invalid")
    rate = str(stream.get("r_frame_rate", "0/1")).split("/", 1)
    actual_fps = float(rate[0]) / float(rate[1])
    actual_duration = float(format_value.get("duration", 0))
    if (
        stream.get("codec_name") != "h264"
        or stream.get("pix_fmt") != "yuv420p"
        or stream.get("width") != width
        or stream.get("height") != height
        or abs(actual_fps - fps) > 0.01
        or abs(actual_duration - duration) > max(0.1, 1 / fps)
    ):
        raise PipelineExecutionError("Deterministic render failed media contract validation")


def _number(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)
