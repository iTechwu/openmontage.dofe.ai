"""Public REST projections for the OpenMontage Yootun surface.

Every success payload served under ``/api/yootun/v1/montage/*`` goes through
this module: responses carry the ``{data, meta}`` envelope with
``Cache-Control: no-store`` (docs/0903 §3), and explicit field allowlists keep
internal identity (job attribution, employee ids) out of the public contract.
Never serialise a ``JobSnapshot`` with ``to_wire()`` onto the public edge —
route it through :func:`public_job` instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from starlette.responses import JSONResponse

from openmontage.contracts import JobEvent, JobSnapshot, PublishedArtifact

SOURCE = "montage"
RECENT_JOBS_LIMIT = 10
RECENT_ARTIFACTS_LIMIT = 8
# Truthful worker row for client-stage-only deployments: the API process that
# answers this request *is* the component executing client stages.
SERVICE_WORKER_ID = "openmontage-mcp"

# Overview-facing statuses: the dashboard keys counts and renders these
# verbatim, so keep them lowercase (detail/list keep the canonical enum).
_LOWER_STATUS = {"WAITING_APPROVAL": "waiting_approval"}


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    direct = headers.get(name)
    if direct is not None:
        return direct
    expected = name.casefold()
    for key, value in headers.items():
        if key.casefold() == expected:
            return value
    return None


def status_name(status: Any) -> str:
    """Canonical uppercase status, tolerant of enum or stored-string forms."""
    value = getattr(status, "value", status)
    return str(value)


def public_status(status: Any) -> str:
    """Lowercase status used by the overview contract."""
    name = status_name(status)
    return _LOWER_STATUS.get(name, name.lower())


def request_id(headers: Mapping[str, str] | None) -> str:
    """Echo the caller's request/trace id, bounded, or mint a fresh one."""
    raw = _header(headers, "X-Request-Id") or _header(headers, "X-Trace-Id")
    value = (raw or "").strip()
    if not value:
        return f"om-{uuid.uuid4().hex}"
    return value[:128]


def envelope(
    data: Any,
    req_id: str,
    *,
    status_code: int = 200,
) -> JSONResponse:
    """docs/0903 §3 success envelope with no-store semantics."""
    return JSONResponse(
        {
            "data": data,
            "meta": {
                "source": SOURCE,
                "requestId": req_id,
                "generatedAt": datetime.now(timezone.utc).isoformat(),
            },
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def public_artifact(artifact: PublishedArtifact) -> dict[str, Any]:
    """Artifact allowlist: renames ``employeeArtifactId`` → ``artifactId``
    and drops the internal ``employeeId``."""
    return {
        "artifactId": artifact.employee_artifact_id,
        "jobId": artifact.job_id,
        "role": artifact.role,
        "fileName": artifact.file_name,
        "mediaType": artifact.media_type,
        "sizeBytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "publishedAt": _isoformat(artifact.published_at),
    }


def public_job(snapshot: JobSnapshot) -> dict[str, Any]:
    """Detail wire: explicit allowlist — no attribution, no employee ids."""
    return {
        "schemaVersion": snapshot.schema_version,
        "jobId": snapshot.job_id,
        "status": status_name(snapshot.status),
        "workflow": snapshot.workflow.to_wire(),
        "request": snapshot.request.to_wire(),
        "stages": [stage.to_wire() for stage in snapshot.stages],
        "artifacts": [public_artifact(artifact) for artifact in snapshot.artifacts],
        "currentStage": snapshot.current_stage,
        "lastSequence": snapshot.last_sequence,
        "createdAt": _isoformat(snapshot.created_at),
        "updatedAt": _isoformat(snapshot.updated_at),
    }


def public_event(event: JobEvent) -> dict[str, Any]:
    """Event allowlist: drops the attribution fields embedded in JobEvent."""
    return {
        "eventId": event.event_id,
        "eventType": status_name(event.event_type),
        "occurredAt": _isoformat(event.occurred_at),
        "jobId": event.job_id,
        "sequence": event.sequence,
        "payload": event.payload,
    }


def public_health(
    executions: Mapping[str, tuple[str | None, datetime | None]],
    now: datetime,
) -> dict[str, Any]:
    """Worker health derived from job lease freshness (docs/0903 §3.4).

    ``executions`` must only contain RUNNING jobs. A fresh lease means the
    worker is online; an expired lease means the job stopped progressing.
    Without lease evidence (client-stage-only deployments never claim jobs)
    the API service itself is the truthful worker row.
    """
    workers: list[dict[str, str]] = []
    degraded = False
    for job_id, (worker_id, lease_expires_at) in sorted(executions.items()):
        del job_id  # keyed iteration only; the worker row is per worker
        fresh = lease_expires_at is not None and lease_expires_at > now
        workers.append(
            {"id": worker_id or "unknown", "status": "online" if fresh else "degraded"}
        )
        degraded = degraded or not fresh
    if not workers:
        workers = [{"id": SERVICE_WORKER_ID, "status": "online"}]
    return {"service": "degraded" if degraded else "ok", "workers": workers}


def public_overview(
    workspace_id: str,
    items: Sequence[JobSnapshot],
    executions: Mapping[str, tuple[str | None, datetime | None]],
    now: datetime,
) -> dict[str, Any]:
    """Dashboard overview contract (docs/0903 §3.4)."""
    status_counts: dict[str, int] = {}
    artifact_total = 0
    all_artifacts: list[PublishedArtifact] = []
    for item in items:
        key = public_status(item.status)
        status_counts[key] = status_counts.get(key, 0) + 1
        artifact_total += len(item.artifacts)
        all_artifacts.extend(item.artifacts)
    all_artifacts.sort(key=_artifact_order, reverse=True)

    return {
        "workspaceId": workspace_id,
        "jobs": {
            "total": len(items),
            "queued": status_counts.get("queued", 0),
            "running": status_counts.get("running", 0),
            "completed": status_counts.get("succeeded", 0),
            "failed": status_counts.get("failed", 0),
            "statusCounts": status_counts,
        },
        "pendingApprovals": status_counts.get("waiting_approval", 0),
        "recentJobs": [
            {
                "jobId": item.job_id,
                "title": _job_title(item),
                "status": public_status(item.status),
                "workflow": item.workflow.name,
                "currentStage": item.current_stage,
                "createdAt": _isoformat(item.created_at),
                "updatedAt": _isoformat(item.updated_at),
            }
            for item in items[:RECENT_JOBS_LIMIT]
        ],
        "artifacts": {
            "total": artifact_total,
            "recent": [
                public_artifact(artifact)
                for artifact in all_artifacts[:RECENT_ARTIFACTS_LIMIT]
            ],
        },
        "health": public_health(executions, now),
    }


def _job_title(snapshot: JobSnapshot) -> str:
    brief = snapshot.request.brief
    if isinstance(brief, Mapping):
        title = brief.get("title")
        if isinstance(title, str):
            return title
    return ""


def _artifact_order(artifact: PublishedArtifact) -> str:
    return _isoformat(artifact.published_at)


def _isoformat(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
