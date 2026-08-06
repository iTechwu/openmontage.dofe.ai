from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

from lib.checkpoint import init_project, read_checkpoint, write_checkpoint
from openmontage.artifact_bridge import ArtifactDownload
from openmontage.contracts import (
    ArtifactJobInput,
    ArtifactMetadata,
    JobAttribution,
    JobCreateRequest,
    JobEventType,
    JobStatus,
    PublishedArtifact,
    StageStatus,
)
from openmontage.job_service import JobService
from openmontage.job_worker import JobWorker
from openmontage.pipeline_executor import (
    PipelineExecutionError,
    PipelineExecutionResult,
    StageAssignment,
)
from tools.dofe.delegation import DelegatedModelCredential
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
        self.credentials: list[DelegatedModelCredential | None] = []

    def execute(
        self,
        assignment: StageAssignment,
        *,
        credential: DelegatedModelCredential | None = None,
    ) -> PipelineExecutionResult:
        self.assignments.append(assignment)
        self.credentials.append(credential)
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


class FakeArtifactBridge:
    def __init__(self, *, input_content: bytes = b"source-video") -> None:
        self.input_content = input_content
        self.download_calls: list[dict] = []
        self.upload_calls: list[dict] = []

    def download_input(self, **kwargs) -> ArtifactDownload:
        self.download_calls.append(kwargs)
        destination_dir = Path(kwargs["destination_dir"])
        destination_dir.mkdir(parents=True, exist_ok=True)
        path = destination_dir / "source.mp4"
        path.write_bytes(self.input_content)
        return ArtifactDownload(
            path=path,
            artifact=ArtifactMetadata(
                artifact_id=kwargs["artifact_id"],
                file_name="source.mp4",
                media_type="video/mp4",
                size_bytes=len(self.input_content),
                sha256=hashlib.sha256(self.input_content).hexdigest(),
            ),
        )

    def upload_output(self, **kwargs) -> PublishedArtifact:
        self.upload_calls.append(kwargs)
        path = Path(kwargs["path"])
        content = path.read_bytes()
        return PublishedArtifact(
            job_id=kwargs["job_id"],
            employee_artifact_id="eart-final-1",
            employee_id=kwargs["attribution"].employee_id,
            role=kwargs["role"],
            file_name=path.name,
            media_type="video/mp4",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            published_at="2026-08-05T14:30:00Z",
        )


class FakeModelCredentialBridge:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def issue(self, **kwargs) -> DelegatedModelCredential:
        self.calls.append(kwargs)
        return DelegatedModelCredential(
            api_key="delegated-api-key",
            models_base_url="https://models.test/api",
            delegation_id="delegation-1",
            external_job_id=kwargs["job_id"],
            pipeline_stage=kwargs["stage"],
            runtime_credential_id="runtime-credential-1",
            expires_at="2026-08-06T09:00:01Z",
        )

def _worker(
    service: JobService,
    executor: FakeExecutor,
    projects_dir: Path,
    *,
    max_executor_attempts: int = 3,
    artifact_bridge: FakeArtifactBridge | None = None,
    model_credential_bridge: FakeModelCredentialBridge | None = None,
) -> JobWorker:
    return JobWorker(
        service,
        executor,
        projects_dir=projects_dir,
        worker_id="test-worker",
        lease_duration=timedelta(minutes=5),
        retry_delay=timedelta(0),
        max_executor_attempts=max_executor_attempts,
        artifact_bridge=artifact_bridge,
        model_credential_bridge=model_credential_bridge,
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


def test_worker_fetches_and_scopes_a_delegated_model_credential_per_stage(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(request_id="delegated-stage"), _attribution())
    executor = FakeExecutor(["completed"])
    credentials = FakeModelCredentialBridge()

    _worker(
        service,
        executor,
        tmp_path / "projects",
        model_credential_bridge=credentials,
    ).run_once(now=NOW)

    assert credentials.calls == [{
        "job_id": job.job_id,
        "stage": "research",
        "stage_attempt": 1,
        "attribution": job.attribution,
    }]
    assert executor.credentials[0] is not None
    assert executor.credentials[0].pipeline_stage == "research"


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


def test_worker_downloads_artifact_input_once_and_reuses_verified_receipt(
    tmp_path: Path,
) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    request = _request(request_id="artifact-input").model_copy(
        update={"input": ArtifactJobInput(type="artifact", artifact_id="attachment-1")}
    )
    job = service.create_job(request, _attribution())
    executor = FakeExecutor(
        [PipelineExecutionError("retry after download"), "completed"]
    )
    bridge = FakeArtifactBridge()
    worker = _worker(
        service,
        executor,
        tmp_path / "projects",
        artifact_bridge=bridge,
    )

    worker.run_once(now=NOW)
    worker.run_once(now=NOW + timedelta(seconds=1))

    assert len(bridge.download_calls) == 1
    assert len(executor.assignments) == 2
    assert executor.assignments[0].local_inputs == executor.assignments[1].local_inputs
    prepared = executor.assignments[1].local_inputs[0]
    assert prepared["artifactId"] == "attachment-1"
    assert prepared["sha256"] == hashlib.sha256(b"source-video").hexdigest()
    assert not Path(prepared["path"]).is_absolute()


def _complete_all_stages(
    service: JobService,
    job_id: str,
    projects_dir: Path,
    final_video: Path,
) -> None:
    job = service.get_job(job_id)
    init_project(
        job_id,
        title="Checkpoint recovery",
        pipeline_type=job.workflow.name,
        pipeline_dir=projects_dir,
    )
    canonical = {
        "research": "research_brief",
        "proposal": "proposal_packet",
        "script": "script",
        "scene_plan": "scene_plan",
        "assets": "asset_manifest",
        "edit": "edit_decisions",
        "compose": "render_report",
        "publish": "publish_log",
    }
    for stage in job.stages:
        service.start_stage(job_id, stage.code)
        if stage.approval_required:
            service.request_stage_approval(job_id, stage.code, reason="test approval")
            service.resolve_stage_approval(job_id, stage.code, approved=True)
        artifact_name = canonical[stage.code]
        if stage.code == "compose":
            artifact = {
                "version": "1.0",
                "outputs": [
                    {
                        "path": str(final_video),
                        "format": "mp4",
                        "resolution": "1080x1920",
                        "duration_seconds": 30,
                    }
                ],
            }
        elif stage.code == "publish":
            artifact = {
                "version": "1.0",
                "entries": [
                    {
                        "platform": "local",
                        "status": "exported",
                        "export_path": str(final_video),
                        "timestamp": "2026-08-05T14:20:00Z",
                    }
                ],
            }
        else:
            artifact = sample_artifact(artifact_name)
            if stage.code == "edit":
                artifact["render_runtime"] = "remotion"
        write_checkpoint(
            projects_dir,
            job_id,
            stage.code,
            "completed",
            {artifact_name: artifact},
            pipeline_type=job.workflow.name,
            human_approval_required=stage.approval_required,
            human_approved=stage.approval_required,
        )
        service.complete_stage(job_id, stage.code)


def test_worker_uploads_final_mp4_persists_artifact_then_completes_job(
    tmp_path: Path,
) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(request_id="final-output"), _attribution())
    projects_dir = tmp_path / "projects"
    final_video = projects_dir / job.job_id / "renders" / "final.mp4"
    final_video.parent.mkdir(parents=True, exist_ok=True)
    final_video.write_bytes(b"final-video")
    _complete_all_stages(service, job.job_id, projects_dir, final_video)
    bridge = FakeArtifactBridge()

    result = _worker(
        service,
        FakeExecutor([]),
        projects_dir,
        artifact_bridge=bridge,
    ).run_once(now=NOW)

    restored = service.get_job(job.job_id)
    assert result is not None and result.outcome == "job_completed"
    assert restored.status == JobStatus.SUCCEEDED
    assert [artifact.employee_artifact_id for artifact in restored.artifacts] == ["eart-final-1"]
    assert bridge.upload_calls[0]["path"] == final_video.resolve()
    assert bridge.upload_calls[0]["role"] == "final_video"
    assert [event.event_type for event in service.list_events(job.job_id)][-2:] == [
        JobEventType.ARTIFACT_PUBLISHED,
        JobEventType.JOB_COMPLETED,
    ]


def test_worker_does_not_upload_final_artifact_twice(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(request_id="existing-output"), _attribution())
    projects_dir = tmp_path / "projects"
    final_video = projects_dir / job.job_id / "renders" / "final.mp4"
    final_video.parent.mkdir(parents=True, exist_ok=True)
    final_video.write_bytes(b"final-video")
    _complete_all_stages(service, job.job_id, projects_dir, final_video)
    service.publish_artifact(
        job.job_id,
        PublishedArtifact(
            job_id=job.job_id,
            employee_artifact_id="eart-existing",
            employee_id=job.attribution.employee_id,
            role="final_video",
            file_name="final.mp4",
            media_type="video/mp4",
            size_bytes=11,
            sha256=hashlib.sha256(b"final-video").hexdigest(),
            published_at="2026-08-05T14:25:00Z",
        ),
    )
    bridge = FakeArtifactBridge()

    result = _worker(
        service,
        FakeExecutor([]),
        projects_dir,
        artifact_bridge=bridge,
    ).run_once(now=NOW)

    assert result is not None and result.outcome == "job_completed"
    assert bridge.upload_calls == []


def test_worker_rejects_final_video_outside_job_workspace(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(request_id="unsafe-output"), _attribution())
    projects_dir = tmp_path / "projects"
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside-video")
    _complete_all_stages(service, job.job_id, projects_dir, outside)
    bridge = FakeArtifactBridge()

    result = _worker(
        service,
        FakeExecutor([]),
        projects_dir,
        artifact_bridge=bridge,
    ).run_once(now=NOW)

    assert result is not None and result.outcome == "job_failed"
    assert service.get_job(job.job_id).status == JobStatus.FAILED
    assert bridge.upload_calls == []
