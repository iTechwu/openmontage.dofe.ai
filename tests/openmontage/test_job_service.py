from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from openmontage.contracts import (
    JobAttribution,
    JobCreateRequest,
    JobEventType,
    JobStatus,
    StageStatus,
)
from openmontage.job_service import JobConflictError, JobService, JobStateError


def _attribution() -> JobAttribution:
    return JobAttribution(
        workspace_id="ws-1",
        employee_id="employee-1",
        runtime_id="runtime-1",
        root_task_id="task-1",
        conversation_id="conversation-1",
        source_invocation_id="invocation-1",
        trace_id="trace-1",
    )


def _request(*, title: str = "Product video") -> JobCreateRequest:
    return JobCreateRequest(
        client_request_id="request-1",
        workflow="animated-explainer",
        input={"type": "text", "inlineText": "Explain the product"},
        brief={"title": title, "durationSeconds": 30},
        output={"container": "mp4", "resolution": "1080x1920"},
        budget={"maxAmount": "20.00", "currency": "CNY"},
    )


def _service(db_path: Path) -> JobService:
    return JobService(db_path)


def test_create_job_persists_manifest_snapshot_and_created_event(tmp_path: Path) -> None:
    service = _service(tmp_path / "jobs.sqlite3")

    job = service.create_job(_request(), _attribution())

    assert job.status == JobStatus.QUEUED
    assert job.workflow.name == "animated-explainer"
    assert job.workflow.version == "2.0"
    assert [stage.code for stage in job.stages] == [
        "research",
        "proposal",
        "script",
        "scene_plan",
        "assets",
        "edit",
        "compose",
        "publish",
    ]
    assert all(stage.status == StageStatus.PENDING for stage in job.stages)
    assert job.last_sequence == 1

    events = service.list_events(job.job_id)
    assert [event.sequence for event in events] == [1]
    assert events[0].event_type == JobEventType.JOB_CREATED
    assert events[0].payload["workflow"] == {
        "name": "animated-explainer",
        "version": "2.0",
    }


def test_create_job_is_idempotent_for_the_same_workspace_and_request(tmp_path: Path) -> None:
    service = _service(tmp_path / "jobs.sqlite3")

    first = service.create_job(_request(), _attribution())
    second = service.create_job(_request(), _attribution())

    assert second.job_id == first.job_id
    assert [event.sequence for event in service.list_events(first.job_id)] == [1]


def test_create_job_rejects_an_idempotency_key_with_a_different_request(tmp_path: Path) -> None:
    service = _service(tmp_path / "jobs.sqlite3")
    service.create_job(_request(), _attribution())

    with pytest.raises(JobConflictError, match="client_request_id"):
        service.create_job(_request(title="Different video"), _attribution())


def test_stage_transition_and_event_replay_survive_service_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    service = _service(db_path)
    job = service.create_job(_request(), _attribution())

    running = service.start_stage(job.job_id, "research")
    completed = service.complete_stage(job.job_id, "research")

    assert running.status == JobStatus.RUNNING
    assert running.stages[0].status == StageStatus.RUNNING
    assert completed.stages[0].status == StageStatus.SUCCEEDED
    assert completed.last_sequence == 3

    restarted = _service(db_path)
    restored = restarted.get_job(job.job_id)
    replay = restarted.list_events(job.job_id, after_sequence=1)

    assert restored.stages[0].status == StageStatus.SUCCEEDED
    assert restored.last_sequence == 3
    assert [event.sequence for event in replay] == [2, 3]
    assert [event.event_type for event in replay] == [
        JobEventType.STAGE_STARTED,
        JobEventType.STAGE_COMPLETED,
    ]


def test_running_stage_reports_fact_based_progress(tmp_path: Path) -> None:
    service = _service(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    service.start_stage(job.job_id, "research")

    progressed = service.update_stage_progress(
        job.job_id,
        "research",
        completed_units=2,
        total_units=5,
        label_code="openmontage.research.sources",
    )

    assert progressed.stages[0].progress == {
        "completedUnits": 2,
        "totalUnits": 5,
        "labelCode": "openmontage.research.sources",
    }
    assert service.list_events(job.job_id)[-1].event_type == JobEventType.STAGE_PROGRESSED


def test_stage_progress_rejects_impossible_units_without_writes(tmp_path: Path) -> None:
    service = _service(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    service.start_stage(job.job_id, "research")

    with pytest.raises(JobStateError, match="completed_units"):
        service.update_stage_progress(
            job.job_id,
            "research",
            completed_units=6,
            total_units=5,
            label_code="openmontage.research.sources",
        )

    assert service.get_job(job.job_id).last_sequence == 2


def test_start_stage_rejects_skipping_a_predecessor_without_partial_writes(tmp_path: Path) -> None:
    service = _service(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())

    with pytest.raises(JobStateError, match="predecessor"):
        service.start_stage(job.job_id, "proposal")

    restored = service.get_job(job.job_id)
    assert restored.status == JobStatus.QUEUED
    assert restored.last_sequence == 1
    assert all(stage.status == StageStatus.PENDING for stage in restored.stages)
    assert len(service.list_events(job.job_id)) == 1


def test_concurrent_stage_start_has_one_winner_and_a_contiguous_event_stream(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    service = _service(db_path)
    job = service.create_job(_request(), _attribution())
    barrier = Barrier(4)

    def start_once() -> str:
        worker = _service(db_path)
        barrier.wait()
        try:
            worker.start_stage(job.job_id, "research")
        except JobStateError:
            return "rejected"
        return "started"

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: start_once(), range(4)))

    assert results.count("started") == 1
    assert results.count("rejected") == 3
    assert [event.sequence for event in service.list_events(job.job_id)] == [1, 2]


def test_approval_stage_cannot_complete_until_approval_is_resolved(tmp_path: Path) -> None:
    service = _service(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    service.start_stage(job.job_id, "research")
    service.complete_stage(job.job_id, "research")
    service.start_stage(job.job_id, "proposal")

    waiting = service.request_stage_approval(job.job_id, "proposal", reason="Approve plan")
    assert waiting.status == JobStatus.WAITING_APPROVAL
    assert waiting.stages[1].status == StageStatus.WAITING_APPROVAL

    with pytest.raises(JobStateError, match="approval"):
        service.complete_stage(job.job_id, "proposal")

    approved = service.resolve_stage_approval(job.job_id, "proposal", approved=True)
    completed = service.complete_stage(job.job_id, "proposal")

    assert approved.status == JobStatus.RUNNING
    assert completed.stages[1].status == StageStatus.SUCCEEDED
    assert [event.event_type for event in service.list_events(job.job_id)][-3:] == [
        JobEventType.JOB_WAITING_APPROVAL,
        JobEventType.APPROVAL_RESOLVED,
        JobEventType.STAGE_COMPLETED,
    ]


def test_approval_command_is_idempotent_across_service_restarts(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    service = _service(db_path)
    job = service.create_job(_request(), _attribution())
    service.start_stage(job.job_id, "research")
    service.complete_stage(job.job_id, "research")
    service.start_stage(job.job_id, "proposal")
    waiting = service.request_stage_approval(job.job_id, "proposal", reason="Approve plan")

    first = service.resolve_stage_approval(
        job.job_id,
        "proposal",
        approved=True,
        expected_sequence=waiting.last_sequence,
        idempotency_key="approve-proposal-4",
    )
    repeated = _service(db_path).resolve_stage_approval(
        job.job_id,
        "proposal",
        approved=True,
        expected_sequence=waiting.last_sequence,
        idempotency_key="approve-proposal-4",
    )

    assert repeated.last_sequence == first.last_sequence
    assert repeated.stages[1].approval_status == "APPROVED"
    assert [event.sequence for event in service.list_events(job.job_id)] == [1, 2, 3, 4, 5, 6]


def test_rejected_approval_emits_resolved_and_terminal_failed_events(tmp_path: Path) -> None:
    service = _service(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    service.start_stage(job.job_id, "research")
    service.complete_stage(job.job_id, "research")
    service.start_stage(job.job_id, "proposal")
    service.request_stage_approval(job.job_id, "proposal", reason="Approve plan")

    failed = service.resolve_stage_approval(job.job_id, "proposal", approved=False)

    assert failed.status == JobStatus.FAILED
    assert failed.stages[1].status == StageStatus.FAILED
    assert [event.event_type for event in service.list_events(job.job_id)][-2:] == [
        JobEventType.APPROVAL_RESOLVED,
        JobEventType.JOB_FAILED,
    ]


def test_fail_job_emits_a_terminal_event_with_safe_error_fields(tmp_path: Path) -> None:
    service = _service(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    service.start_stage(job.job_id, "research")

    failed = service.fail_job(
        job.job_id,
        code="OPENMONTAGE_MODEL_UPSTREAM_FAILED",
        message="Image generation failed",
        retryable=True,
    )

    event = service.list_events(job.job_id)[-1]
    assert failed.status == JobStatus.FAILED
    assert failed.stages[0].status == StageStatus.FAILED
    assert event.event_type == JobEventType.JOB_FAILED
    assert event.payload == {
        "stage": "research",
        "status": "FAILED",
        "error": {
            "code": "OPENMONTAGE_MODEL_UPSTREAM_FAILED",
            "message": "Image generation failed",
            "retryable": True,
        },
    }


def test_cancellation_uses_requested_then_confirmed_terminal_states(tmp_path: Path) -> None:
    service = _service(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    service.start_stage(job.job_id, "research")

    requested = service.request_cancel(job.job_id)
    cancelled = service.confirm_cancel(job.job_id)

    assert requested.status == JobStatus.CANCEL_REQUESTED
    assert cancelled.status == JobStatus.CANCELLED
    assert cancelled.stages[0].status == StageStatus.CANCELLED
    assert [event.event_type for event in service.list_events(job.job_id)][-2:] == [
        JobEventType.JOB_CANCEL_REQUESTED,
        JobEventType.JOB_CANCELLED,
    ]


def test_completion_requires_every_stage_to_be_terminal_success(tmp_path: Path) -> None:
    service = _service(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())

    with pytest.raises(JobStateError, match="all stages"):
        service.complete_job(job.job_id)

    assert service.get_job(job.job_id).status == JobStatus.QUEUED
    assert len(service.list_events(job.job_id)) == 1
