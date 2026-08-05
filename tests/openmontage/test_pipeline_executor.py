from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lib.checkpoint import init_project
from openmontage.contracts import JobAttribution, JobCreateRequest
from openmontage.job_service import JobService
from openmontage.pipeline_executor import (
    AgentCommandPipelineExecutor,
    PipelineExecutionError,
    PipelineExecutionIncomplete,
    StageAssignment,
)


WRITE_CHECKPOINT_SCRIPT = r"""
import json
import pathlib
import sys
from datetime import datetime, timezone

prompt = sys.stdin.read()
prefix = "OPENMONTAGE_ASSIGNMENT_PATH="
assignment_line = next(line for line in prompt.splitlines() if line.startswith(prefix))
assignment_path = pathlib.Path(json.loads(assignment_line[len(prefix):]))
assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
pathlib.Path(sys.argv[1]).write_text(prompt, encoding="utf-8")
checkpoint = {
    "version": "1.0",
    "project_id": assignment["projectId"],
    "pipeline_type": assignment["pipeline"],
    "stage": assignment["stage"],
    "status": "in_progress",
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "checkpoint_policy": "guided",
    "human_approval_required": False,
    "human_approved": False,
    "artifacts": {},
    "metadata": {"partial_progress": {"completed_units": 1, "total_units": 3}},
}
checkpoint_path = pathlib.Path(assignment["projectDir"]) / f"checkpoint_{assignment['stage']}.json"
checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
"""


def _job(tmp_path: Path):
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(
        JobCreateRequest(
            client_request_id="executor-request",
            workflow="animated-explainer",
            input={"type": "text", "inlineText": "Explain executor leases"},
            brief={"title": "Executor leases", "durationSeconds": 30},
            output={"container": "mp4", "resolution": "1080x1920"},
            budget={"maxAmount": "20.00", "currency": "CNY"},
        ),
        JobAttribution(
            workspace_id="ws-executor",
            employee_id="employee-executor",
            runtime_id="runtime-executor",
            root_task_id="task-executor",
            conversation_id="conversation-executor",
            source_invocation_id="invocation-executor",
            trace_id="trace-executor",
        ),
    )
    projects_dir = tmp_path / "projects"
    project_dir = init_project(
        job.job_id,
        title="Executor leases",
        pipeline_type=job.workflow.name,
        pipeline_dir=projects_dir,
    )
    return job, projects_dir, project_dir


def test_executor_writes_assignment_invokes_real_argv_and_reads_checkpoint(
    tmp_path: Path,
) -> None:
    job, projects_dir, project_dir = _job(tmp_path)
    prompt_capture = tmp_path / "prompt.txt"
    assignment = StageAssignment.from_job(
        job,
        stage="research",
        stage_attempt=1,
        projects_dir=projects_dir,
    )
    executor = AgentCommandPipelineExecutor(
        [sys.executable, "-c", WRITE_CHECKPOINT_SCRIPT, str(prompt_capture)],
        timeout_seconds=5,
    )

    result = executor.execute(assignment)

    assignment_payload = json.loads(result.assignment_path.read_text(encoding="utf-8"))
    assert assignment_payload["jobId"] == job.job_id
    assert assignment_payload["projectId"] == job.job_id
    assert assignment_payload["projectDir"] == str(project_dir.resolve())
    assert assignment_payload["pipeline"] == "animated-explainer"
    assert assignment_payload["stage"] == "research"
    assert assignment_payload["stageAttempt"] == 1
    assert assignment_payload["directorSkill"] == "skills/pipelines/explainer/research-director.md"
    assert assignment_payload["attribution"]["employeeId"] == "employee-executor"
    assert result.status == "in_progress"
    assert result.checkpoint["metadata"]["partial_progress"] == {
        "completed_units": 1,
        "total_units": 3,
    }
    assert "Execute exactly one OpenMontage pipeline stage" in prompt_capture.read_text()


@pytest.mark.parametrize(
    "raw",
    [
        "codex exec --full-auto",
        "{}",
        "[]",
        '["codex", 7]',
        '["", "exec"]',
    ],
)
def test_executor_environment_requires_nonempty_json_argv(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("OPENMONTAGE_AGENT_EXECUTOR_JSON", raw)

    with pytest.raises(PipelineExecutionError, match="JSON argv"):
        AgentCommandPipelineExecutor.from_environment()


def test_executor_reports_nonzero_exit_without_accepting_a_checkpoint(tmp_path: Path) -> None:
    job, projects_dir, _ = _job(tmp_path)
    executor = AgentCommandPipelineExecutor(
        [sys.executable, "-c", "import sys; sys.stderr.write('agent failed'); sys.exit(7)"],
        timeout_seconds=5,
    )

    with pytest.raises(PipelineExecutionError, match="exit code 7"):
        executor.execute(
            StageAssignment.from_job(
                job,
                stage="research",
                stage_attempt=1,
                projects_dir=projects_dir,
            )
        )


def test_executor_reports_timeout(tmp_path: Path) -> None:
    job, projects_dir, _ = _job(tmp_path)
    executor = AgentCommandPipelineExecutor(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        timeout_seconds=0.01,
    )

    with pytest.raises(PipelineExecutionError, match="timed out"):
        executor.execute(
            StageAssignment.from_job(
                job,
                stage="research",
                stage_attempt=1,
                projects_dir=projects_dir,
            )
        )


def test_executor_requires_a_valid_stage_checkpoint(tmp_path: Path) -> None:
    job, projects_dir, _ = _job(tmp_path)
    executor = AgentCommandPipelineExecutor(
        [sys.executable, "-c", "pass"],
        timeout_seconds=5,
    )

    with pytest.raises(PipelineExecutionIncomplete, match="checkpoint"):
        executor.execute(
            StageAssignment.from_job(
                job,
                stage="research",
                stage_attempt=1,
                projects_dir=projects_dir,
            )
        )
