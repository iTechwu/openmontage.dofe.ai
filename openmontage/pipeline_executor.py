"""External Agent execution adapter for one checkpoint-backed pipeline stage."""

from __future__ import annotations

import json
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from lib.checkpoint import CheckpointValidationError, read_checkpoint
from lib.pipeline_loader import get_stage_skill, load_pipeline_readonly
from openmontage.contracts import JobSnapshot


ROOT = Path(__file__).resolve().parent.parent


class PipelineExecutionError(RuntimeError):
    """Raised when the configured external Agent cannot execute safely."""


class PipelineExecutionIncomplete(PipelineExecutionError):
    """Raised when an Agent exits without a valid checkpoint fact."""


@dataclass(frozen=True)
class StageAssignment:
    job_id: str
    project_id: str
    projects_dir: Path
    project_dir: Path
    pipeline: str
    pipeline_version: str
    stage: str
    stage_attempt: int
    director_skill: str | None
    request: dict[str, Any]
    attribution: dict[str, Any]
    job_snapshot: dict[str, Any]

    @classmethod
    def from_job(
        cls,
        job: JobSnapshot,
        *,
        stage: str,
        stage_attempt: int,
        projects_dir: str | Path,
    ) -> "StageAssignment":
        if stage_attempt < 1:
            raise PipelineExecutionError("stage_attempt must be greater than zero")
        if stage not in {item.code for item in job.stages}:
            raise PipelineExecutionError(
                f"Stage {stage!r} does not belong to Job workflow {job.workflow.name!r}"
            )
        base = Path(projects_dir).expanduser().resolve()
        manifest = load_pipeline_readonly(job.workflow.name)
        skill = get_stage_skill(manifest, stage)
        return cls(
            job_id=job.job_id,
            project_id=job.job_id,
            projects_dir=base,
            project_dir=base / job.job_id,
            pipeline=job.workflow.name,
            pipeline_version=job.workflow.version,
            stage=stage,
            stage_attempt=stage_attempt,
            director_skill=f"skills/{skill}.md" if skill else None,
            request=job.request.to_wire(),
            attribution=job.attribution.to_wire(),
            job_snapshot=job.to_wire(),
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "jobId": self.job_id,
            "projectId": self.project_id,
            "projectsDir": str(self.projects_dir),
            "projectDir": str(self.project_dir),
            "pipeline": self.pipeline,
            "pipelineVersion": self.pipeline_version,
            "stage": self.stage,
            "stageAttempt": self.stage_attempt,
            "directorSkill": self.director_skill,
            "request": self.request,
            "attribution": self.attribution,
            "jobSnapshot": self.job_snapshot,
        }


@dataclass(frozen=True)
class PipelineExecutionResult:
    status: str
    checkpoint: dict[str, Any]
    assignment_path: Path


class PipelineExecutor(Protocol):
    def execute(self, assignment: StageAssignment) -> PipelineExecutionResult:
        """Execute one stage and return the checkpoint-backed outcome."""


class AgentCommandPipelineExecutor:
    """Invoke a configured Agent argv and reconcile its durable checkpoint."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = 3600,
    ) -> None:
        self.command = _validate_command(command)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise PipelineExecutionError("timeout_seconds must be greater than zero")
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> "AgentCommandPipelineExecutor":
        raw = os.environ.get("OPENMONTAGE_AGENT_EXECUTOR_JSON", "")
        try:
            command = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise PipelineExecutionError(
                "OPENMONTAGE_AGENT_EXECUTOR_JSON must be a nonempty JSON argv array"
            ) from exc
        if not isinstance(command, list):
            raise PipelineExecutionError(
                "OPENMONTAGE_AGENT_EXECUTOR_JSON must be a nonempty JSON argv array"
            )
        raw_timeout = os.environ.get("OPENMONTAGE_AGENT_TIMEOUT_SECONDS", "3600")
        try:
            timeout_seconds = float(raw_timeout)
        except ValueError as exc:
            raise PipelineExecutionError(
                "OPENMONTAGE_AGENT_TIMEOUT_SECONDS must be a positive number"
            ) from exc
        return cls(command, timeout_seconds=timeout_seconds)

    def execute(self, assignment: StageAssignment) -> PipelineExecutionResult:
        assignment.project_dir.mkdir(parents=True, exist_ok=True)
        assignment_dir = assignment.project_dir / ".openmontage" / "assignments"
        assignment_dir.mkdir(parents=True, exist_ok=True)
        assignment_path = assignment_dir / (
            f"{assignment.stage}-attempt-{assignment.stage_attempt}.json"
        )
        _atomic_write_json(assignment_path, assignment.to_wire())
        prompt = _stage_prompt(assignment, assignment_path)

        try:
            completed = subprocess.run(
                self.command,
                input=prompt,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PipelineExecutionError(
                f"External Agent executor timed out after {self.timeout_seconds:g} seconds"
            ) from exc
        except OSError as exc:
            raise PipelineExecutionError("External Agent executor could not be started") from exc

        _write_execution_log(
            assignment,
            stdout=completed.stdout,
            stderr=completed.stderr,
            return_code=completed.returncode,
        )
        if completed.returncode != 0:
            raise PipelineExecutionError(
                f"External Agent executor exited with exit code {completed.returncode}"
            )

        try:
            checkpoint = read_checkpoint(
                assignment.projects_dir,
                assignment.project_id,
                assignment.stage,
            )
        except (CheckpointValidationError, json.JSONDecodeError, OSError) as exc:
            raise PipelineExecutionError(
                "External Agent executor wrote an invalid stage checkpoint"
            ) from exc
        if checkpoint is None:
            raise PipelineExecutionIncomplete(
                "External Agent executor exited without a stage checkpoint"
            )
        if (
            checkpoint.get("project_id") != assignment.project_id
            or checkpoint.get("pipeline_type") != assignment.pipeline
            or checkpoint.get("stage") != assignment.stage
        ):
            raise PipelineExecutionError(
                "External Agent executor checkpoint identity does not match the assignment"
            )
        return PipelineExecutionResult(
            status=str(checkpoint["status"]),
            checkpoint=checkpoint,
            assignment_path=assignment_path,
        )


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)) or not command:
        raise PipelineExecutionError(
            "OPENMONTAGE_AGENT_EXECUTOR_JSON must be a nonempty JSON argv array"
        )
    if any(not isinstance(value, str) or not value.strip() for value in command):
        raise PipelineExecutionError(
            "OPENMONTAGE_AGENT_EXECUTOR_JSON must be a nonempty JSON argv array"
        )
    return tuple(command)


def _stage_prompt(assignment: StageAssignment, assignment_path: Path) -> str:
    skill_instruction = (
        f"Read `{assignment.director_skill}` before stage work."
        if assignment.director_skill
        else "Follow the stage contract declared in the pipeline manifest."
    )
    return "\n".join(
        [
            f"OPENMONTAGE_ASSIGNMENT_PATH={json.dumps(str(assignment_path))}",
            "Execute exactly one OpenMontage pipeline stage for the durable Job assignment above.",
            "Read `AGENT_GUIDE.md`, the assignment JSON, the named pipeline manifest, and "
            "`skills/meta/checkpoint-protocol.md` before acting.",
            skill_instruction,
            "Work only inside the assignment project directory and use the repository tool registry.",
            "Write an `in_progress` checkpoint before consequential work and refresh factual partial "
            "progress where available.",
            "Finish by writing exactly one valid checkpoint status: `completed`, `awaiting_human`, "
            "`in_progress`, or `failed`.",
            "At a human gate, write `awaiting_human` and stop. Do not execute a later stage.",
            "Do not print media bytes or secrets. The checkpoint is the execution result.",
        ]
    )


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_execution_log(
    assignment: StageAssignment,
    *,
    stdout: str,
    stderr: str,
    return_code: int,
) -> None:
    log_dir = assignment.project_dir / ".openmontage" / "executor"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{assignment.stage}-attempt-{assignment.stage_attempt}.json"
    _atomic_write_json(
        path,
        {
            "returnCode": return_code,
            "stdoutCharacters": len(stdout),
            "stderrCharacters": len(stderr),
        },
    )
