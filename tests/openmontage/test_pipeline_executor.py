from __future__ import annotations

import json
import os
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
from tools.dofe.delegation import DelegatedModelCredential


WRITE_CHECKPOINT_SCRIPT = r"""
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

prompt = sys.stdin.read()
prefix = "OPENMONTAGE_ASSIGNMENT_PATH="
assignment_line = next(line for line in prompt.splitlines() if line.startswith(prefix))
assignment_path = pathlib.Path(json.loads(assignment_line[len(prefix):]))
assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
pathlib.Path(sys.argv[1]).write_text(prompt, encoding="utf-8")
if len(sys.argv) > 2:
    pathlib.Path(sys.argv[2]).write_text(json.dumps({
        key: os.environ.get(key)
        for key in ["DOFE_MODEL_API_KEY", "DOFE_DELEGATION_ID", "DOFE_EXTERNAL_JOB_ID", "DOFE_PIPELINE_STAGE", "OPENAI_BASE_URL", "OPENMONTAGE_SERVICE_TOKEN", "OPENMONTAGE_EVENT_SIGNING_SECRET", "FAL_KEY"]
    }), encoding="utf-8")
if len(sys.argv) > 4:
    pathlib.Path(sys.argv[3]).write_text(sys.argv[4], encoding="utf-8")
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


def test_executor_injects_delegation_only_into_the_stage_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENMONTAGE_SERVICE_TOKEN", "must-not-reach-agent")
    monkeypatch.setenv("OPENMONTAGE_EVENT_SIGNING_SECRET", "must-not-reach-agent")
    monkeypatch.setenv("FAL_KEY", "must-not-reach-agent")
    job, projects_dir, project_dir = _job(tmp_path)
    environment_capture = tmp_path / "environment.json"
    project_argument_capture = tmp_path / "project-argument.txt"
    executor = AgentCommandPipelineExecutor(
        [
            sys.executable,
            "-c",
            WRITE_CHECKPOINT_SCRIPT,
            str(tmp_path / "prompt.txt"),
            str(environment_capture),
            str(project_argument_capture),
            "{project_dir}",
        ],
        timeout_seconds=5,
    )
    assignment = StageAssignment.from_job(
        job,
        stage="research",
        stage_attempt=1,
        projects_dir=projects_dir,
    )
    credential = DelegatedModelCredential(
        api_key="delegated-api-key",
        models_base_url="https://models.test/api",
        delegation_id="delegation-1",
        external_job_id=job.job_id,
        pipeline_stage="research",
        runtime_credential_id="runtime-credential-1",
        expires_at="2099-08-06T09:00:01Z",
    )

    result = executor.execute(assignment, credential=credential)

    environment = json.loads(environment_capture.read_text())
    assert environment["DOFE_MODEL_API_KEY"] == "delegated-api-key"
    assert environment["DOFE_DELEGATION_ID"] == "delegation-1"
    assert environment["DOFE_PIPELINE_STAGE"] == "research"
    assert environment["OPENAI_BASE_URL"].startswith("http://127.0.0.1:")
    assert environment["OPENAI_BASE_URL"].endswith("/api/v1")
    assert environment["OPENMONTAGE_SERVICE_TOKEN"] is None
    assert environment["OPENMONTAGE_EVENT_SIGNING_SECRET"] is None
    assert environment["FAL_KEY"] is None
    assert project_argument_capture.read_text() == str(project_dir)
    assert "delegated-api-key" not in result.assignment_path.read_text()
    assert os.environ.get("DOFE_MODEL_API_KEY") != "delegated-api-key"


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
