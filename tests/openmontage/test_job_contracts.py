from __future__ import annotations

import pytest
from pydantic import ValidationError

from openmontage.contracts import (
    JobAttribution,
    JobEvent,
    JobEventType,
    JobStatus,
    StageStatus,
    WorkflowDefinition,
    validate_job_transition,
    validate_stage_transition,
)


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


def test_workflow_definition_uses_manifest_version_stage_order_and_approval_policy() -> None:
    workflow = WorkflowDefinition.from_pipeline("animated-explainer")

    assert workflow.name == "animated-explainer"
    assert workflow.version == "2.0"
    assert [stage.code for stage in workflow.stages] == [
        "research",
        "proposal",
        "script",
        "scene_plan",
        "assets",
        "edit",
        "compose",
        "publish",
    ]
    assert workflow.stages[1].approval_required is True
    assert workflow.stages[5].approval_required is False
    assert workflow.stages[1].label_code == "openmontage.stage.proposal"


def test_job_event_serializes_the_versioned_camel_case_envelope() -> None:
    event = JobEvent.create(
        event_id="evt-1",
        event_type=JobEventType.STAGE_STARTED,
        job_id="job-1",
        sequence=2,
        attribution=_attribution(),
        payload={"stage": "proposal", "status": StageStatus.RUNNING.value},
        occurred_at="2026-08-05T10:00:00Z",
    )

    assert event.to_wire() == {
        "schemaVersion": 1,
        "eventId": "evt-1",
        "eventType": "openmontage.stage.started",
        "occurredAt": "2026-08-05T10:00:00Z",
        "jobId": "job-1",
        "sequence": 2,
        "workspaceId": "ws-1",
        "employeeId": "employee-1",
        "runtimeId": "runtime-1",
        "rootTaskId": "task-1",
        "conversationId": "conversation-1",
        "sourceInvocationId": "invocation-1",
        "traceId": "trace-1",
        "payload": {"stage": "proposal", "status": "RUNNING"},
    }


def test_job_event_rejects_non_positive_sequences() -> None:
    with pytest.raises(ValidationError):
        JobEvent.create(
            event_id="evt-1",
            event_type=JobEventType.JOB_CREATED,
            job_id="job-1",
            sequence=0,
            attribution=_attribution(),
            payload={},
        )


def test_job_event_rejects_unknown_schema_versions() -> None:
    with pytest.raises(ValidationError):
        JobEvent.model_validate(
            {
                "schemaVersion": 2,
                "eventId": "evt-1",
                "eventType": "openmontage.job.created",
                "occurredAt": "2026-08-05T10:00:00Z",
                "jobId": "job-1",
                "sequence": 1,
                **_attribution().to_wire(),
                "payload": {},
            }
        )


@pytest.mark.parametrize(
    ("previous", "next_status"),
    [
        (StageStatus.PENDING, StageStatus.RUNNING),
        (StageStatus.RUNNING, StageStatus.WAITING_APPROVAL),
        (StageStatus.WAITING_APPROVAL, StageStatus.RUNNING),
        (StageStatus.RUNNING, StageStatus.SUCCEEDED),
        (StageStatus.PENDING, StageStatus.SKIPPED),
    ],
)
def test_stage_transition_accepts_declared_lifecycle_edges(
    previous: StageStatus,
    next_status: StageStatus,
) -> None:
    validate_stage_transition(previous, next_status)


@pytest.mark.parametrize(
    ("previous", "next_status"),
    [
        (StageStatus.PENDING, StageStatus.SUCCEEDED),
        (StageStatus.SUCCEEDED, StageStatus.RUNNING),
        (StageStatus.CANCELLED, StageStatus.RUNNING),
    ],
)
def test_stage_transition_rejects_skips_and_terminal_regressions(
    previous: StageStatus,
    next_status: StageStatus,
) -> None:
    with pytest.raises(ValueError, match="Invalid stage transition"):
        validate_stage_transition(previous, next_status)


def test_job_transition_requires_cancel_confirmation_before_cancelled() -> None:
    validate_job_transition(JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED)
    validate_job_transition(JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED)

    with pytest.raises(ValueError, match="Invalid job transition"):
        validate_job_transition(JobStatus.RUNNING, JobStatus.CANCELLED)


def test_job_terminal_state_cannot_regress() -> None:
    with pytest.raises(ValueError, match="Invalid job transition"):
        validate_job_transition(JobStatus.SUCCEEDED, JobStatus.RUNNING)
