"""Authenticated REST routes for OpenMontage Job control and replay."""

from __future__ import annotations

import base64
import hmac
import json
import os
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from openmontage.contracts import (
    JobAttribution,
    JobCreateRequest,
    JobSnapshot,
    WorkflowConfigurationError,
)
from openmontage.job_service import (
    JobConflictError,
    JobNotFoundError,
    JobService,
    JobStateError,
    JobSubmissionError,
)


class TrustedContextError(PermissionError):
    """Raised when a request lacks authenticated Gateway context."""


AttributionResolver = Callable[[Mapping[str, str] | None], JobAttribution]


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


class TrustedAttributionResolver:
    """Resolve attribution only after authenticating the AgentSpace service."""

    def __init__(self, service_token: str):
        if not service_token:
            raise ValueError("OpenMontage service token is required")
        self.service_token = service_token

    @classmethod
    def from_environment(cls) -> "TrustedAttributionResolver":
        return cls(os.environ.get("OPENMONTAGE_SERVICE_TOKEN", ""))

    def __call__(self, headers: Mapping[str, str] | None) -> JobAttribution:
        authorization = _header(headers, "Authorization") or ""
        expected = f"Bearer {self.service_token}"
        if not hmac.compare_digest(authorization, expected):
            raise TrustedContextError("OpenMontage service authentication failed")
        encoded = _header(headers, "X-Dofe-Job-Attribution")
        if not encoded:
            raise TrustedContextError("Trusted Job attribution is required")
        try:
            padding = "=" * (-len(encoded) % 4)
            raw = base64.urlsafe_b64decode(encoded + padding)
            return JobAttribution.model_validate_json(raw)
        except (ValueError, ValidationError) as exc:
            raise TrustedContextError("Trusted Job attribution is invalid") from exc


def default_job_service() -> JobService:
    from lib.paths import PROJECTS_DIR

    configured = os.environ.get("OPENMONTAGE_JOB_DB", "").strip()
    path = configured or str(PROJECTS_DIR / ".openmontage" / "jobs.sqlite3")
    return JobService(path)


def default_attribution_resolver() -> AttributionResolver:
    return TrustedAttributionResolver.from_environment()


def require_same_workspace(job: JobSnapshot, attribution: JobAttribution) -> None:
    if job.attribution.workspace_id != attribution.workspace_id:
        raise JobNotFoundError(f"OpenMontage Job not found: {job.job_id}")


def create_job_routes(
    service: JobService,
    attribution_resolver: AttributionResolver,
) -> list[Route]:
    async def create_job(request: Request) -> JSONResponse:
        try:
            attribution = attribution_resolver(request.headers)
            payload = JobCreateRequest.model_validate(await request.json())
            snapshot = service.create_job(payload, attribution)
            return JSONResponse(snapshot.to_wire(), status_code=201)
        except Exception as exc:
            return _error_response(exc)

    async def get_job(request: Request) -> JSONResponse:
        try:
            attribution = attribution_resolver(request.headers)
            snapshot = service.get_job(request.path_params["job_id"])
            require_same_workspace(snapshot, attribution)
            return JSONResponse(snapshot.to_wire())
        except Exception as exc:
            return _error_response(exc)

    async def list_events(request: Request) -> JSONResponse:
        try:
            attribution = attribution_resolver(request.headers)
            job_id = request.path_params["job_id"]
            snapshot = service.get_job(job_id)
            require_same_workspace(snapshot, attribution)
            raw_after = request.query_params.get("afterSequence", "0")
            after_sequence = int(raw_after)
            if after_sequence < 0:
                raise ValueError("afterSequence must be non-negative")
            events = service.list_events(job_id, after_sequence=after_sequence)
            return JSONResponse(
                {
                    "events": [event.to_wire() for event in events],
                    "lastSequence": snapshot.last_sequence,
                }
            )
        except Exception as exc:
            return _error_response(exc)

    async def list_artifacts(request: Request) -> JSONResponse:
        try:
            attribution = attribution_resolver(request.headers)
            snapshot = service.get_job(request.path_params["job_id"])
            require_same_workspace(snapshot, attribution)
            return JSONResponse(
                {
                    "artifacts": [artifact.to_wire() for artifact in snapshot.artifacts],
                    "lastSequence": snapshot.last_sequence,
                }
            )
        except Exception as exc:
            return _error_response(exc)

    async def cancel_job(request: Request) -> JSONResponse:
        try:
            attribution = attribution_resolver(request.headers)
            job_id = request.path_params["job_id"]
            snapshot = service.get_job(job_id)
            require_same_workspace(snapshot, attribution)
            body = await _optional_json_object(request)
            return JSONResponse(
                service.request_cancel(
                    job_id,
                    expected_sequence=_expected_sequence(body),
                    idempotency_key=_header(request.headers, "Idempotency-Key"),
                ).to_wire()
            )
        except Exception as exc:
            return _error_response(exc)

    async def approve_stage(request: Request) -> JSONResponse:
        try:
            attribution = attribution_resolver(request.headers)
            job_id = request.path_params["job_id"]
            snapshot = service.get_job(job_id)
            require_same_workspace(snapshot, attribution)
            body = await _optional_json_object(request)
            stage = body.get("stage")
            approved = body.get("approved", True)
            if not isinstance(stage, str) or not stage:
                raise ValueError("stage is required")
            if not isinstance(approved, bool):
                raise ValueError("approved must be boolean")
            return JSONResponse(
                service.resolve_stage_approval(
                    job_id,
                    stage,
                    approved=approved,
                    expected_sequence=_expected_sequence(body),
                    idempotency_key=_header(request.headers, "Idempotency-Key"),
                ).to_wire()
            )
        except Exception as exc:
            return _error_response(exc)

    return [
        Route("/api/v1/jobs", create_job, methods=["POST"]),
        Route("/api/v1/jobs/{job_id}", get_job, methods=["GET"]),
        Route("/api/v1/jobs/{job_id}/events", list_events, methods=["GET"]),
        Route("/api/v1/jobs/{job_id}/artifacts", list_artifacts, methods=["GET"]),
        Route("/api/v1/jobs/{job_id}/cancel", cancel_job, methods=["POST"]),
        Route("/api/v1/jobs/{job_id}/approve", approve_stage, methods=["POST"]),
    ]


async def _optional_json_object(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


def _expected_sequence(body: Mapping[str, Any]) -> int | None:
    value = body.get("expectedSequence")
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("expectedSequence must be a non-negative integer")
    return value


def _error_response(error: Exception) -> JSONResponse:
    if isinstance(error, TrustedContextError):
        return JSONResponse(
            {"error": {"code": "OPENMONTAGE_UNAUTHORIZED"}},
            status_code=401,
        )
    if isinstance(error, JobNotFoundError):
        return JSONResponse(
            {"error": {"code": "OPENMONTAGE_JOB_NOT_FOUND"}},
            status_code=404,
        )
    if isinstance(error, JobConflictError):
        return JSONResponse(
            {"error": {"code": "OPENMONTAGE_JOB_CONFLICT", "message": str(error)}},
            status_code=409,
        )
    if isinstance(error, JobStateError):
        return JSONResponse(
            {"error": {"code": "OPENMONTAGE_JOB_STATE_INVALID", "message": str(error)}},
            status_code=409,
        )
    if isinstance(error, WorkflowConfigurationError):
        return JSONResponse(
            {
                "error": {
                    "code": "OPENMONTAGE_WORKFLOW_UNAVAILABLE",
                    "message": str(error),
                }
            },
            status_code=503,
        )
    if isinstance(error, JobSubmissionError):
        return JSONResponse(
            {"error": {"code": "OPENMONTAGE_SUBMISSION_REJECTED", "message": str(error)}},
            status_code=422,
        )
    if isinstance(error, (ValidationError, ValueError, KeyError)):
        return JSONResponse(
            {"error": {"code": "OPENMONTAGE_VALIDATION_FAILED", "message": str(error)}},
            status_code=422,
        )
    raise error
