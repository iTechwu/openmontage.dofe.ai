from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.checkpoint import init_project, read_checkpoint, write_checkpoint
from openmontage.contracts import JobAttribution, JobCreateRequest, JobStatus, StageStatus
from openmontage.job_service import JobService
from openmontage.job_worker import JobWorker
from openmontage.pipeline_executor import (
    PipelineExecutionError,
    PipelineExecutionResult,
    StageAssignment,
)
from tests.contracts.test_phase0_contracts import sample_artifact


NOW = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)


def _attribution() -> JobAttribution:
    return JobAttribution(
        workspace_id="ws-worker",
        employee_id="employee-worker",
        runtime_id="runtime-worker",
        root_task_id="task-worker",
        conversation_id="conversation-worker",
        source_invocation_id="invocation-worker",
        trace_id="trace-worker",
    )


def _request(*, request_id: str, workflow: str = "animated-explainer") -> JobCreateRequest:
    return JobCreateRequest(
        client_request_id=request_id,
        workflow=workflow,
        input={"type": "text", "inlineText": "Explain checkpoint recovery"},
        brief={"title": "Checkpoint recovery", "durationSeconds": 30},
        output={"container": "mp4", "resolution": "1080x1920"},
        budget={"maxAmount": "20.00", "currency": "CNY"},
    )


class FakeExecutor:
    def __init__(self, outcomes: list[str | Exception]):
        self.outcomes = deque(outcomes)
        self.assignments: list[StageAssignment] = []

    def execute(self, assignment: StageAssignment) -> PipelineExecutionResult:
        self.assignments.append(assignment)
        if not self.outcomes:
            raise AssertionError("executor should not have been called")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        approval_required = assignment.pipeline == "framework-smoke"
        artifacts = (
            {"research_brief": sample_artifact("research_brief")}
            if assignment.stage == "research" and outcome in {"completed", "awaiting_human"}
            else {}
        )
        write_checkpoint(
            assignment.projects_dir,
            assignment.project_id,
            assignment.stage,
            outcome,
            artifacts,
            pipeline_type=assignment.pipeline,
            human_approval_required=approval_required,
            human_approved=approval_required and outcome == "completed",
            error="Agent stage failed" if outcome == "failed" else None,
        )
        checkpoint = read_checkpoint(
            assignment.projects_dir,
            assignment.project_id,
            assignment.stage,
        )
        assert checkpoint is not None
        return PipelineExecutionResult(
            status=outcome,
            checkpoint=checkpoint,
            assignment_path=assignment.project_dir / "assignment.json",
        )


def _worker(
    service: JobService,
    executor: FakeExecutor,
    projects_dir: Path,
    *,
    max_executor_attempts: int = 3,
) -> JobWorker:
    return JobWorker(
        service,
        executor,
        projects_dir=projects_dir,
        worker_id="test-worker",
        lease_duration=timedelta(minutes=5),
        retry_delay=timedelta(0),
        max_executor_attempts=max_executor_attempts,
    )


def test_worker_executes_one_stage_and_updates_job_state(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(request_id="stage"), _attribution())
    executor = FakeExecutor(["completed"])

    result = _worker(service, executor, tmp_path / "projects").run_once(now=NOW)

    restored = service.get_job(job.job_id)
    assert result is not None
    assert result.outcome == "stage_completed"
    assert result.stage == "research"
    assert restored.status == JobStatus.RUNNING
    assert restored.stages[0].status == StageStatus.SUCCEEDED
    assert restored.stages[0].attempt == 1
    assert len(executor.assignments) == 1


def test_worker_reconciles_completed_checkpoint_without_repeating_executor(
    tmp_path: Path,
) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(request_id="recover"), _attribution())
    projects_dir = tmp_path / "projects"
    init_project(
        job.job_id,
        title="Checkpoint recovery",
        pipeline_type=job.workflow.name,
        pipeline_dir=projects_dir,
    )
    write_checkpoint(
        projects_dir,
        job.job_id,
        "research",
        "completed",
        {"research_brief": sample_artifact("research_brief")},
        pipeline_type=job.workflow.name,
    )
    executor = FakeExecutor([])

    result = _worker(service, executor, projects_dir).run_once(now=NOW)

    assert result is not None
    assert result.outcome == "stage_reconciled"
    assert service.get_job(job.job_id).stages[0].status == StageStatus.SUCCEEDED
    assert executor.assignments == []


def test_worker_pauses_at_approval_and_resumes_with_latest_job_snapshot(
    tmp_path: Path,
) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(
        _request(request_id="approval", workflow="framework-smoke"),
        _attribution(),
    )
    executor = FakeExecutor(["awaiting_human", "completed"])
    worker = _worker(service, executor, tmp_path / "projects")

    waiting_result = worker.run_once(now=NOW)
    waiting = service.get_job(job.job_id)
    assert waiting_result is not None
    assert waiting_result.outcome == "waiting_approval"
    assert waiting.status == JobStatus.WAITING_APPROVAL

    service.resolve_stage_approval(job.job_id, "research", approved=True)
    completed_result = worker.run_once(now=NOW + timedelta(seconds=1))
    completed = service.get_job(job.job_id)

    assert completed_result is not None
    assert completed_result.outcome == "stage_completed"
    assert completed.stages[0].status == StageStatus.SUCCEEDED
    assert executor.assignments[1].job_snapshot["stages"][0]["approvalStatus"] == "APPROVED"


def test_worker_retries_executor_error_then_completes_same_running_stage(
    tmp_path: Path,
) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(request_id="retry"), _attribution())
    executor = FakeExecutor(
        [PipelineExecutionError("temporary agent failure"), "completed"]
    )
    worker = _worker(service, executor, tmp_path / "projects")

    first = worker.run_once(now=NOW)
    second = worker.run_once(now=NOW + timedelta(seconds=1))

    restored = service.get_job(job.job_id)
    assert first is not None and first.outcome == "retry_scheduled"
    assert second is not None and second.outcome == "stage_completed"
    assert restored.stages[0].status == StageStatus.SUCCEEDED
    assert restored.stages[0].attempt == 1
    assert len(executor.assignments) == 2


def test_worker_fails_job_after_bounded_executor_errors(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(request_id="bounded-failure"), _attribution())
    executor = FakeExecutor(
        [PipelineExecutionError("failure one"), PipelineExecutionError("failure two")]
    )
    worker = _worker(
        service,
        executor,
        tmp_path / "projects",
        max_executor_attempts=2,
    )

    worker.run_once(now=NOW)
    result = worker.run_once(now=NOW + timedelta(seconds=1))

    restored = service.get_job(job.job_id)
    assert result is not None and result.outcome == "job_failed"
    assert restored.status == JobStatus.FAILED
    assert restored.stages[0].status == StageStatus.FAILED


def test_worker_maps_failed_checkpoint_and_cancel_request_to_terminal_states(
    tmp_path: Path,
) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    failed_job = service.create_job(_request(request_id="checkpoint-failed"), _attribution())
    failed_executor = FakeExecutor(["failed"])

    failed_result = _worker(
        service,
        failed_executor,
        tmp_path / "projects",
    ).run_once(now=NOW)
    assert failed_result is not None and failed_result.outcome == "job_failed"
    assert service.get_job(failed_job.job_id).status == JobStatus.FAILED

    cancelled_job = service.create_job(_request(request_id="cancelled"), _attribution())
    service.request_cancel(cancelled_job.job_id)
    cancel_executor = FakeExecutor([])
    cancelled_result = _worker(
        service,
        cancel_executor,
        tmp_path / "projects",
    ).run_once(now=NOW + timedelta(seconds=1))
    assert cancelled_result is not None and cancelled_result.outcome == "job_cancelled"
    assert service.get_job(cancelled_job.job_id).status == JobStatus.CANCELLED
    assert cancel_executor.assignments == []
