"""Authenticated REST routes for OpenMontage Job control and replay."""

from __future__ import annotations

import base64
import hmac
import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from starlette.exceptions import HTTPException
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
from openmontage.mcp_gateway_auth import gateway_attribution, gateway_attempted
from openmontage.public_contract import (
    SERIES_MAX_DAYS,
    SERIES_TIME_ZONES,
    envelope,
    public_artifact,
    public_event,
    public_job,
    public_overview,
    public_series,
    request_id,
    status_name,
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
        derived = gateway_attribution(headers)
        if derived is not None:
            return derived
        if gateway_attempted(headers):
            raise TrustedContextError("MCP Gateway context is invalid")
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
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> list[Route]:
    clock = now_fn or (lambda: datetime.now(timezone.utc))

    async def create_job(request: Request) -> JSONResponse:
        req_id = request_id(request.headers)
        try:
            attribution = attribution_resolver(request.headers)
            payload = JobCreateRequest.model_validate(await request.json())
            snapshot = service.create_job(payload, attribution)
            return envelope(public_job(snapshot), req_id, status_code=201)
        except Exception as exc:
            return _error_response(exc, req_id)

    async def get_job(request: Request) -> JSONResponse:
        req_id = request_id(request.headers)
        try:
            attribution = attribution_resolver(request.headers)
            snapshot = service.get_job(request.path_params["job_id"])
            require_same_workspace(snapshot, attribution)
            return envelope(public_job(snapshot), req_id)
        except Exception as exc:
            return _error_response(exc, req_id)

    async def list_jobs(request: Request) -> JSONResponse:
        req_id = request_id(request.headers)
        try:
            attribution = attribution_resolver(request.headers)
            raw_limit = request.query_params.get("limit", "50")
            limit = int(raw_limit)
            if limit < 1 or limit > 100:
                raise ValueError("limit must be between 1 and 100")
            items = service.list_jobs(attribution.workspace_id, limit=limit)
            return envelope(
                {"items": [public_job(item) for item in items], "limit": limit},
                req_id,
            )
        except Exception as exc:
            return _error_response(exc, req_id)

    async def overview(request: Request) -> JSONResponse:
        req_id = request_id(request.headers)
        try:
            attribution = attribution_resolver(request.headers)
            items = service.list_jobs(attribution.workspace_id, limit=100)
            running_ids = [
                item.job_id
                for item in items
                if status_name(item.status) == "RUNNING"
            ]
            executions = service.execution_states(running_ids)
            return envelope(
                public_overview(attribution.workspace_id, items, executions, clock()),
                req_id,
            )
        except Exception as exc:
            return _error_response(exc, req_id)

    async def series(request: Request) -> JSONResponse:
        req_id = request_id(request.headers)
        try:
            attribution = attribution_resolver(request.headers)
            start = _series_instant(request.query_params.get("start"), "start")
            end = _series_instant(request.query_params.get("end"), "end")
            if end <= start:
                raise ValueError("end must be after start")
            if end - start > timedelta(days=SERIES_MAX_DAYS):
                raise ValueError(f"series range must be at most {SERIES_MAX_DAYS} days")
            time_zone = _series_time_zone(request.query_params.get("timezone", "Asia/Shanghai"))
            entries = service.series_entries(attribution.workspace_id, start=start, end=end)
            return envelope(
                public_series(
                    attribution.workspace_id,
                    entries,
                    start=start,
                    end=end,
                    time_zone=time_zone,
                ),
                req_id,
            )
        except Exception as exc:
            return _error_response(exc, req_id)

    async def list_events(request: Request) -> JSONResponse:
        req_id = request_id(request.headers)
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
            return envelope(
                {
                    "events": [public_event(event) for event in events],
                    "lastSequence": snapshot.last_sequence,
                },
                req_id,
            )
        except Exception as exc:
            return _error_response(exc, req_id)

    async def list_artifacts(request: Request) -> JSONResponse:
        req_id = request_id(request.headers)
        try:
            attribution = attribution_resolver(request.headers)
            snapshot = service.get_job(request.path_params["job_id"])
            require_same_workspace(snapshot, attribution)
            return envelope(
                {
                    "artifacts": [
                        public_artifact(artifact) for artifact in snapshot.artifacts
                    ],
                    "lastSequence": snapshot.last_sequence,
                },
                req_id,
            )
        except Exception as exc:
            return _error_response(exc, req_id)

    async def cancel_job(request: Request) -> JSONResponse:
        req_id = request_id(request.headers)
        try:
            attribution = attribution_resolver(request.headers)
            job_id = request.path_params["job_id"]
            snapshot = service.get_job(job_id)
            require_same_workspace(snapshot, attribution)
            body = await _optional_json_object(request)
            return envelope(
                public_job(
                    service.request_cancel(
                        job_id,
                        expected_sequence=_expected_sequence(body),
                        idempotency_key=_header(request.headers, "Idempotency-Key"),
                    )
                ),
                req_id,
            )
        except Exception as exc:
            return _error_response(exc, req_id)

    async def approve_stage(request: Request) -> JSONResponse:
        req_id = request_id(request.headers)
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
            return envelope(
                public_job(
                    service.resolve_stage_approval(
                        job_id,
                        stage,
                        approved=approved,
                        expected_sequence=_expected_sequence(body),
                        idempotency_key=_header(request.headers, "Idempotency-Key"),
                    )
                ),
                req_id,
            )
        except Exception as exc:
            return _error_response(exc, req_id)

    return [
        Route("/api/v1/overview", overview, methods=["GET"]),
        Route("/api/v1/series", series, methods=["GET"]),
        Route("/api/v1/jobs", list_jobs, methods=["GET"]),
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


def _series_instant(value: str | None, label: str) -> datetime:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO 8601 instant") from exc
    if moment.tzinfo is None:
        raise ValueError(f"{label} must carry a timezone offset")
    return moment


def _series_time_zone(name: str) -> ZoneInfo:
    if name not in SERIES_TIME_ZONES:
        raise ValueError("timezone is not supported")
    return ZoneInfo(name)


def _expected_sequence(body: Mapping[str, Any]) -> int | None:
    value = body.get("expectedSequence")
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("expectedSequence must be a non-negative integer")
    return value


def _error_response(error: Exception, req_id: str = "") -> JSONResponse:
    """docs/0903 §3 error envelope: ``{"error": {code, message?, requestId}}``
    with ``Cache-Control: no-store``."""
    request_ref: dict[str, str] = {"requestId": req_id} if req_id else {}

    def body(code: str, message: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": code, **request_ref}
        if message:
            payload["message"] = message
        return {"error": payload}

    def respond(status: int, payload: dict[str, Any]) -> JSONResponse:
        return JSONResponse(
            payload, status_code=status, headers={"Cache-Control": "no-store"}
        )

    if isinstance(error, TrustedContextError):
        return respond(401, body("OPENMONTAGE_UNAUTHORIZED"))
    if isinstance(error, JobNotFoundError):
        return respond(404, body("OPENMONTAGE_JOB_NOT_FOUND"))
    if isinstance(error, JobConflictError):
        return respond(409, body("OPENMONTAGE_JOB_CONFLICT", str(error)))
    if isinstance(error, JobStateError):
        return respond(409, body("OPENMONTAGE_JOB_STATE_INVALID", str(error)))
    if isinstance(error, WorkflowConfigurationError):
        return respond(503, body("OPENMONTAGE_WORKFLOW_UNAVAILABLE", str(error)))
    if isinstance(error, JobSubmissionError):
        return respond(422, body("OPENMONTAGE_SUBMISSION_REJECTED", str(error)))
    if isinstance(error, HTTPException) and error.status_code == 405:
        return respond(405, body("OPENMONTAGE_METHOD_NOT_ALLOWED"))
    if isinstance(error, (ValidationError, ValueError, KeyError)):
        return respond(422, body("OPENMONTAGE_VALIDATION_FAILED", str(error)))
    raise error
