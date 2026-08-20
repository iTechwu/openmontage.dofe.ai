from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from lib.checkpoint import init_project
from openmontage.deterministic_video_executor import (
    PIPELINE_NAME,
    DeterministicVideoSmokeExecutor,
)
from openmontage.pipeline_executor import PipelineExecutionResult, StageAssignment


class FallbackExecutor:
    def __init__(self) -> None:
        self.called = False

    def execute(self, assignment, *, credential=None, cancellation_requested=None):
        self.called = True
        return PipelineExecutionResult(
            status="in_progress",
            checkpoint={"status": "in_progress"},
            assignment_path=assignment.project_dir / "fallback.json",
        )


def _assignment(tmp_path: Path, *, pipeline: str = PIPELINE_NAME) -> StageAssignment:
    project_id = "deterministic-smoke"
    project_dir = init_project(
        project_id,
        title="Deterministic smoke",
        pipeline_type=pipeline,
        pipeline_dir=tmp_path,
    )
    return StageAssignment(
        job_id=project_id,
        project_id=project_id,
        projects_dir=tmp_path,
        project_dir=project_dir,
        pipeline=pipeline,
        pipeline_version="1.0",
        stage="compose",
        stage_attempt=1,
        director_skill="skills/pipelines/deterministic-video-smoke/compose-director.md",
        request={
            "brief": {"title": "Smoke", "durationSeconds": 1},
            "output": {"container": "mp4", "resolution": "320x240", "fps": 24},
        },
        attribution={},
        job_snapshot={"stages": [{"code": "compose"}]},
        local_inputs=(),
    )


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_deterministic_executor_renders_and_checkpoints_mp4(tmp_path: Path) -> None:
    fallback = FallbackExecutor()
    assignment = _assignment(tmp_path)
    executor = DeterministicVideoSmokeExecutor(fallback)

    result = executor.execute(assignment)

    output = assignment.project_dir / "renders" / "final.mp4"
    report = result.checkpoint["artifacts"]["render_report"]["outputs"][0]
    assert result.status == "completed"
    assert output.is_file() and output.stat().st_size > 0
    assert report["path"] == "renders/final.mp4"
    assert report["codec"] == "h264"
    assert report["resolution"] == "320x240"
    assert fallback.called is False
    assert executor.requires_model_credential(assignment) is False


def test_deterministic_executor_delegates_other_pipelines(tmp_path: Path) -> None:
    fallback = FallbackExecutor()
    assignment = _assignment(tmp_path, pipeline="animated-explainer")
    executor = DeterministicVideoSmokeExecutor(fallback)

    result = executor.execute(assignment)

    assert result.status == "in_progress"
    assert fallback.called is True
    assert executor.requires_model_credential(assignment) is True
