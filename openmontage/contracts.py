"""Versioned contracts for durable OpenMontage video jobs."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from lib.pipeline_loader import get_stage_order, list_pipelines, load_pipeline_readonly


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class WireModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        populate_by_name=True,
        use_enum_values=True,
    )

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json", exclude_none=True)


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class StageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class JobEventType(str, Enum):
    JOB_CREATED = "openmontage.job.created"
    STAGE_STARTED = "openmontage.stage.started"
    STAGE_PROGRESSED = "openmontage.stage.progressed"
    STAGE_COMPLETED = "openmontage.stage.completed"
    JOB_WAITING_APPROVAL = "openmontage.job.waiting_approval"
    APPROVAL_RESOLVED = "openmontage.approval.resolved"
    USAGE_UPDATED = "openmontage.usage.updated"
    ARTIFACT_PUBLISHED = "openmontage.artifact.published"
    JOB_COMPLETED = "openmontage.job.completed"
    JOB_FAILED = "openmontage.job.failed"
    JOB_CANCEL_REQUESTED = "openmontage.job.cancel_requested"
    JOB_CANCELLED = "openmontage.job.cancelled"


class JobAttribution(WireModel):
    workspace_id: str = Field(min_length=1)
    employee_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    root_task_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    source_invocation_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)


class JobCreateRequest(WireModel):
    schema_version: Literal[1] = 1
    client_request_id: str = Field(
        min_length=1,
        description=(
            "Idempotency key for one business submission; reuse it for retries and change it "
            "for a new Job."
        ),
    )
    workflow: str = Field(
        min_length=1,
        description=(
            "Pipeline manifest name from pipeline_defs; stage names such as compose are invalid."
        ),
    )
    input: dict[str, Any]
    brief: dict[str, Any]
    output: dict[str, Any]
    budget: dict[str, Any]


def job_submission_capability() -> dict[str, Any]:
    """Return the agent-facing submission contract from canonical runtime sources."""
    request_schema = JobCreateRequest.model_json_schema(by_alias=True)
    return {
        "workflow_field_is_pipeline": True,
        "workflow_stage_warning": "compose is a stage, not a workflow; use a pipeline name",
        "supported_workflows": sorted(list_pipelines()),
        "request_schema": request_schema,
        "request_example": {
            "schemaVersion": 1,
            "clientRequestId": "<unique-per-business-attempt>",
            "workflow": "animated-explainer",
            "input": {"type": "text", "inlineText": "Create a concise product video"},
            "brief": {"title": "Product video", "durationSeconds": 12},
            "output": {"container": "mp4", "resolution": "1280x720", "fps": 30},
            "budget": {"maxAmount": "1.00", "currency": "CNY"},
        },
        "required_fields": request_schema.get("required", []),
        "idempotency": "Reuse clientRequestId when retrying the same business submission; change it for a new Job.",
    }


class ApprovalStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    REQUIRED = "REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class StageDefinition(WireModel):
    code: str = Field(min_length=1)
    label_code: str = Field(min_length=1)
    approval_required: bool = False


class WorkflowDefinition(WireModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    stages: tuple[StageDefinition, ...]

    @classmethod
    def from_pipeline(cls, pipeline_type: str) -> "WorkflowDefinition":
        manifest = load_pipeline_readonly(pipeline_type)
        approval_by_stage = {
            stage["name"]: bool(stage.get("human_approval_default", False))
            for stage in manifest["stages"]
        }
        return cls(
            name=manifest["name"],
            version=manifest["version"],
            stages=tuple(
                StageDefinition(
                    code=stage,
                    label_code=f"openmontage.stage.{stage}",
                    approval_required=approval_by_stage[stage],
                )
                for stage in get_stage_order(manifest)
            ),
        )


class StageSnapshot(WireModel):
    code: str = Field(min_length=1)
    label_code: str = Field(min_length=1)
    approval_required: bool = False
    approval_status: ApprovalStatus = ApprovalStatus.NOT_REQUIRED
    status: StageStatus = StageStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    progress: dict[str, Any] | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class PublishedArtifact(WireModel):
    schema_version: Literal[1] = 1
    job_id: str = Field(min_length=1, max_length=256)
    employee_artifact_id: str = Field(min_length=1, max_length=256)
    employee_id: str = Field(min_length=1, max_length=256)
    role: str = Field(min_length=1, max_length=64)
    file_name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    published_at: datetime


class JobSnapshot(WireModel):
    schema_version: Literal[1] = 1
    job_id: str = Field(min_length=1)
    status: JobStatus
    workflow: WorkflowDefinition
    attribution: JobAttribution
    request: JobCreateRequest
    stages: tuple[StageSnapshot, ...]
    artifacts: tuple[PublishedArtifact, ...] = ()
    current_stage: str | None = None
    last_sequence: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime


class JobEvent(WireModel):
    schema_version: Literal[1] = 1
    event_id: str = Field(min_length=1)
    event_type: JobEventType
    occurred_at: datetime
    job_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    workspace_id: str = Field(min_length=1)
    employee_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    root_task_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    source_invocation_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    payload: dict[str, Any]

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        event_type: JobEventType,
        job_id: str,
        sequence: int,
        attribution: JobAttribution,
        payload: dict[str, Any],
        occurred_at: datetime | str | None = None,
    ) -> "JobEvent":
        return cls(
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at or datetime.now(timezone.utc),
            job_id=job_id,
            sequence=sequence,
            payload=payload,
            **attribution.model_dump(),
        )


class OutboxRecord(WireModel):
    event: JobEvent
    status: str
    delivery_attempts: int = Field(default=0, ge=0)
    next_attempt_at: datetime | None = None
    delivered_at: datetime | None = None
    last_error: str | None = None


class ArtifactMetadata(WireModel):
    artifact_id: str = Field(min_length=1, max_length=256)
    file_name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ArtifactReadGrant(WireModel):
    schema_version: Literal[1] = 1
    grant_id: str = Field(pattern=r"^om_ag_[A-Za-z0-9_-]{1,256}$")
    operation: Literal["READ"]
    download_url: str = Field(min_length=1, max_length=2048)
    token: str = Field(min_length=32, max_length=512)
    expires_at: datetime
    artifact: ArtifactMetadata


class OutputArtifactMetadata(WireModel):
    role: str = Field(min_length=1, max_length=64)
    file_name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ArtifactWriteGrant(WireModel):
    schema_version: Literal[1] = 1
    grant_id: str = Field(pattern=r"^om_ag_[A-Za-z0-9_-]{1,256}$")
    operation: Literal["WRITE"]
    upload_url: str = Field(min_length=1, max_length=2048)
    token: str = Field(min_length=32, max_length=512)
    expires_at: datetime
    artifact: OutputArtifactMetadata


_STAGE_TRANSITIONS: dict[StageStatus, frozenset[StageStatus]] = {
    StageStatus.PENDING: frozenset(
        {StageStatus.RUNNING, StageStatus.SKIPPED, StageStatus.CANCELLED}
    ),
    StageStatus.RUNNING: frozenset(
        {
            StageStatus.WAITING_APPROVAL,
            StageStatus.SUCCEEDED,
            StageStatus.FAILED,
            StageStatus.CANCELLED,
        }
    ),
    StageStatus.WAITING_APPROVAL: frozenset(
        {StageStatus.RUNNING, StageStatus.FAILED, StageStatus.CANCELLED}
    ),
    StageStatus.SUCCEEDED: frozenset(),
    StageStatus.FAILED: frozenset(),
    StageStatus.CANCELLED: frozenset(),
    StageStatus.SKIPPED: frozenset(),
}


_JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset(
        {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCEL_REQUESTED}
    ),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.WAITING_APPROVAL,
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCEL_REQUESTED,
        }
    ),
    JobStatus.WAITING_APPROVAL: frozenset(
        {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCEL_REQUESTED}
    ),
    JobStatus.CANCEL_REQUESTED: frozenset({JobStatus.CANCELLED, JobStatus.FAILED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


def validate_stage_transition(previous: StageStatus, next_status: StageStatus) -> None:
    if next_status not in _STAGE_TRANSITIONS[previous]:
        raise ValueError(f"Invalid stage transition: {previous.value} -> {next_status.value}")


def validate_job_transition(previous: JobStatus, next_status: JobStatus) -> None:
    if next_status not in _JOB_TRANSITIONS[previous]:
        raise ValueError(f"Invalid job transition: {previous.value} -> {next_status.value}")
