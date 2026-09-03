"""Durable video Job snapshots and transactional event outbox."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from openmontage.contracts import (
    ApprovalStatus,
    JobAttribution,
    JobCreateRequest,
    JobEvent,
    JobEventType,
    JobRequestSnapshot,
    JobSnapshot,
    JobStatus,
    OutboxRecord,
    PublishedArtifact,
    StageSnapshot,
    StageStatus,
    WorkflowDefinition,
    validate_job_transition,
    validate_stage_transition,
)
from openmontage.pipeline_executor import delegated_executor_availability

from lib.paths import PROJECTS_DIR


class JobConflictError(RuntimeError):
    """Raised when an idempotency key is reused with different immutable input."""


class JobSubmissionError(RuntimeError):
    """Raised when a new Job cannot be executed by the configured runtime."""


class JobStateError(RuntimeError):
    """Raised when a requested Job lifecycle transition is not allowed."""


class JobNotFoundError(LookupError):
    """Raised when a Job ID does not exist."""


class JobLeaseError(RuntimeError):
    """Raised when a Worker lease is invalid, expired, or no longer owned."""


class ClientStageError(RuntimeError):
    """Raised when a client-driven stage call violates the stage contract.

    Carries a stable machine-readable ``code`` (e.g. ``STAGE_ALREADY_OWNED``,
    ``IDEMPOTENCY_CONFLICT``, ``ARTIFACT_SCHEMA_INVALID``). The code is embedded
    in the message because the MCP SDK serializes tool exceptions as plain
    text — ``str(error)`` must contain the code for the client to branch on it.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class OutboxLeaseError(RuntimeError):
    """Raised when an event delivery lease is no longer owned."""


@dataclass(frozen=True)
class JobLease:
    job_id: str
    worker_id: str
    lease_token: str
    expires_at: datetime
    attempt: int
    snapshot: JobSnapshot


@dataclass(frozen=True)
class ClientStageLease:
    """Opaque lease returned by ``begin_client_stage`` (plan §6.1).

    The ``lease_token`` proves exclusive ownership of one (Job, stage) attempt
    to ``update_client_stage_progress`` / ``submit_client_stage``; it is never
    written to Job events. ``snapshot`` is the Job state at begin time so the
    client can plan the stage without an extra round trip.
    """

    job_id: str
    stage: str
    stage_attempt: int
    lease_token: str
    expires_at: datetime
    snapshot: JobSnapshot

    def to_wire(self) -> dict[str, Any]:
        return {
            "jobId": self.job_id,
            "stage": self.stage,
            "stageAttempt": self.stage_attempt,
            "leaseToken": self.lease_token,
            "leaseExpiresAt": self.expires_at.isoformat().replace("+00:00", "Z"),
            "lastSequence": self.snapshot.last_sequence,
            "job": self.snapshot.to_wire(),
        }

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> "ClientStageLease":
        expires_raw = data["leaseExpiresAt"]
        expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        return cls(
            job_id=data["jobId"],
            stage=data["stage"],
            stage_attempt=int(data["stageAttempt"]),
            lease_token=data["leaseToken"],
            expires_at=expires,
            snapshot=JobSnapshot.model_validate(data["job"]),
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def client_stage_only_enabled() -> bool:
    """Whether CI is in client-stage-only mode (plan §13).

    When set, the legacy Worker must not claim or advance Jobs: every stage is
    driven by the client Agent through ``begin/update/submit_client_stage``.
    A Job stays ``QUEUED`` after creation until the client begins its first
    stage.

    Parsing is allow-list and fail-closed: any value other than ``1``/``true``/
    ``yes``/``on`` (case-insensitive, trimmed) — including unset, empty, ``0``,
    or a typo — disables the mode. An accidental unknown value therefore cannot
    leave a deployment half-locked.
    """
    return os.environ.get("OPENMONTAGE_CLIENT_STAGE_ONLY", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _reject_legacy_mutation_in_client_stage_only() -> None:
    """Fence off the legacy Worker mutation surface in client-stage-only mode.

    ``claim_job`` returns ``None`` instead (that is the Worker's "no work"
    signal, not an error); every other legacy mutation — heartbeat, release,
    start/complete, progress, approval, fail, confirm — raises, so a stale
    Worker or a direct service call cannot advance a Job outside the client
    Stage API.
    """
    if client_stage_only_enabled():
        raise JobStateError(
            "client-stage-only mode is enabled; Jobs are advanced exclusively by "
            "the client Stage API (begin/update/submit_client_stage), not the "
            "legacy Worker"
        )


def _validate_client_idempotency_key(idempotency_key: str) -> None:
    """Require a non-empty string idempotency key ≤256 chars (plan §8.1).

    A missing/empty key makes ``_command_hash`` return ``None``, which silently
    skips the idempotency record and lets a retry duplicate a checkpoint write
    or a state advance. Reject it with a stable ``IDEMPOTENCY_CONFLICT`` code
    instead of failing open. The MCP tool layer already guards this; this is
    the service-layer gate so a direct call cannot bypass it.
    """
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ClientStageError(
            "IDEMPOTENCY_CONFLICT", "idempotency_key must be a non-empty string"
        )
    if len(idempotency_key) > 256:
        raise ClientStageError(
            "IDEMPOTENCY_CONFLICT", "idempotency_key must be at most 256 characters"
        )


def _parse_lease_expiry(raw: str | None) -> datetime | None:
    """Parse a stored lease expiry, tolerating legacy/naive and corrupt values.

    The ``openmontage_job_execution`` table is not declared STRICT, so a corrupt
    write can store a BLOB or a number instead of an ISO-8601 string. Any such
    value fails closed to ``None`` ("no valid expiry") so the active-lease check
    rejects the token and the claim/reclaim path can take the record back,
    rather than raising and leaving a stuck token in place. An offset-aware
    value is normalized to UTC so the lexical SQL comparison of the persisted
    ISO string agrees with the instant comparison used here.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_utc(value: datetime, field: str) -> datetime:
    """Reject a naive datetime and convert an aware one to UTC.

    Lease expiries and retry timestamps are normalized to UTC and then persisted
    as ISO-8601 strings that SQLite compares lexically. A non-UTC offset (e.g.
    ``+05:30``) serializes to a different string than its UTC instant, so a SQL
    comparison like ``next_attempt_at <= ?`` would order the same instant
    inconsistently — delaying a retry by hours or letting a lease be claimed
    early. Every clock value is therefore converted to UTC before it is
    persisted or compared. Naive values are rejected (not silently assumed UTC)
    so a local-time clock bug surfaces at the lease boundary.
    """
    if value.tzinfo is None:
        raise JobLeaseError(f"{field} must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


def _normalize_now(now: datetime | None, *, field: str = "now") -> datetime:
    """Resolve the optional clock argument to an aware-UTC datetime (see
    ``_to_utc``). ``None`` resolves to the current UTC instant.

    Applied everywhere a caller-supplied clock is persisted or compared: the
    lease gate (``_require_active_lease``, ``_require_current_fencing_token``),
    ``claim_job`` / ``release_lease`` / settlement, ``heartbeat_lease``, and the
    outbox delivery paths. ``field`` labels the value in the rejection error so
    a naive-clock bug points at the offending entry point.
    """
    if now is None:
        return _now()
    return _to_utc(now, field)


def _resolve_release_retry(
    *,
    now: datetime | None,
    retry_at: datetime | None,
    retry_delay: timedelta | None,
) -> tuple[datetime, datetime | None]:
    """Validate and normalize the release retry arguments shared by
    ``release_lease`` and ``release_lease_or_confirm_cancel``.

    Returns ``(call_now, retry_at)`` with ``call_now`` aware-UTC and ``retry_at``
    aware-UTC or ``None``. Raises ``JobLeaseError`` on mutual exclusion, a
    negative delay, a naive clock, or a ``retry_at`` earlier than ``now``.
    """
    if retry_at is not None and retry_delay is not None:
        raise JobLeaseError("retry_at and retry_delay are mutually exclusive")
    if retry_delay is not None and retry_delay < timedelta(0):
        raise JobLeaseError("retry_delay must not be negative")
    call_now = _normalize_now(now)
    retry_at = _to_utc(retry_at, "retry_at") if retry_at is not None else None
    if retry_at is not None and retry_at < call_now:
        raise JobLeaseError("retry_at must not be earlier than now")
    return call_now, retry_at


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _same_request_identity(snapshot_json: str, expected: dict[str, Any]) -> bool:
    """Compare a legacy snapshot after applying the current semantic normalization."""
    try:
        snapshot = json.loads(snapshot_json)
        stored_request = JobCreateRequest.model_validate(snapshot["request"])
        stored_attribution = JobAttribution.model_validate(snapshot["attribution"])
        expected_request = JobCreateRequest.model_validate(expected["request"])
        expected_attribution = JobAttribution.model_validate(expected["attribution"])
    except (KeyError, TypeError, ValueError):
        return False
    return stored_request == expected_request and stored_attribution == expected_attribution


def _summarize_error(message: str, max_len: int) -> str:
    """Return a compact error summary that preserves both head and tail.

    Truncating only the end loses root causes that happen to be at the tail
    of a long diagnostic chain; truncating only the beginning loses context.
    A head+tail ellipsis keeps the start (what failed) and the end (why it
    failed) while fitting the caller's budget.
    """
    message = " ".join(message.split())
    if not message or len(message) <= max_len:
        return message
    head_len = (max_len - 5) // 2
    tail_len = max_len - head_len - 5
    return f"{message[:head_len]} ... {message[-tail_len:]}"


def _validate_artifact_input(request: JobCreateRequest) -> None:
    """Reject ``input.type=artifact`` when the ``artifactId`` is a prepared project id.

    The agent-facing video workflow expects the creative brief as ``input.type=text``
    with ``inlineText``. A common mistake is to pass the reference-clone ``project_id``
    (e.g. ``clone-douyin-...``) as ``input.artifactId``; that is not an artifact — the
    worker would later fail to resolve it via the artifact bridge with
    ``OPENMONTAGE_ARTIFACT_INPUT_FAILED``. Reject it here, at submission time, with a
    clear message instead.
    """
    input_value = request.input
    if getattr(input_value, "type", None) != "artifact":
        return
    artifact_id = input_value.artifact_id
    if PROJECTS_DIR.joinpath(artifact_id).is_dir():
        raise JobSubmissionError(
            f"input.artifactId '{artifact_id}' is a prepared project id, not an artifact; "
            'set input.type="text" with inlineText (the creative brief) for a video job, '
            "or upload a real file through the artifact bridge."
        )


# Read-only projection of job→workspace ownership for Backlot (docs/0903 §4).
# Backlot mounts projects read-only, so it consumes this atomic JSON manifest
# instead of opening the SQLite database from a :ro volume.
WORKSPACE_MAP_NAME = "workspace-map.json"


class JobService:
    def __init__(self, database_path: str | Path, *, projects_dir: str | Path | None = None):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        # Root under which client-stage checkpoints and project workspaces are
        # written (projects/<job_id>/...). Defaults to the canonical projects
        # root; tests pass a tmp directory.
        self.projects_dir = Path(projects_dir).expanduser().resolve() if projects_dir is not None else PROJECTS_DIR
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _begin_write(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS openmontage_job (
                    job_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    client_request_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (workspace_id, client_request_id)
                );

                CREATE TABLE IF NOT EXISTS openmontage_job_event (
                    event_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES openmontage_job(job_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    delivery_status TEXT NOT NULL DEFAULT 'pending',
                    delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    delivery_lease_token TEXT,
                    delivery_lease_expires_at TEXT,
                    delivered_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (job_id, sequence)
                );

                CREATE INDEX IF NOT EXISTS idx_openmontage_job_event_pending
                    ON openmontage_job_event(delivery_status, next_attempt_at, created_at);

                CREATE TABLE IF NOT EXISTS openmontage_job_command (
                    job_id TEXT NOT NULL REFERENCES openmontage_job(job_id) ON DELETE CASCADE,
                    idempotency_key TEXT NOT NULL,
                    command_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS openmontage_job_execution (
                    job_id TEXT PRIMARY KEY REFERENCES openmontage_job(job_id) ON DELETE CASCADE,
                    worker_id TEXT,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    next_attempt_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_openmontage_job_execution_claim
                    ON openmontage_job_execution(lease_expires_at, next_attempt_at);

                CREATE TABLE IF NOT EXISTS openmontage_client_stage_lease (
                    job_id TEXT NOT NULL REFERENCES openmontage_job(job_id) ON DELETE CASCADE,
                    stage TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    stage_attempt INTEGER NOT NULL,
                    lease_token TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, stage, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS idx_openmontage_client_stage_lease_active
                    ON openmontage_client_stage_lease(job_id, stage, status);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(openmontage_job_event)")
            }
            if "last_error" not in columns:
                connection.execute(
                    "ALTER TABLE openmontage_job_event ADD COLUMN last_error TEXT"
                )
            if "delivery_lease_token" not in columns:
                connection.execute(
                    "ALTER TABLE openmontage_job_event ADD COLUMN delivery_lease_token TEXT"
                )
            if "delivery_lease_expires_at" not in columns:
                connection.execute(
                    "ALTER TABLE openmontage_job_event ADD COLUMN delivery_lease_expires_at TEXT"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_openmontage_job_event_claim
                ON openmontage_job_event(
                    delivery_status, next_attempt_at,
                    delivery_lease_expires_at, created_at
                )
                """
            )
        self._export_workspace_map()

    def _export_workspace_map(self) -> None:
        """Best-effort atomic export of job→workspace ownership for Backlot.

        Never raises: a failed export only means Backlot cannot see new jobs
        until the next export (service construction or job creation) — it must
        not fail a submission that already committed.
        """
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT job_id, workspace_id FROM openmontage_job
                    ORDER BY created_at, job_id
                    """
                ).fetchall()
            payload = {row["job_id"]: row["workspace_id"] for row in rows}
            target = self.database_path.parent / WORKSPACE_MAP_NAME
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            os.replace(tmp, target)
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).warning(
                "Failed to export OpenMontage workspace map", exc_info=True
            )

    def execution_states(
        self,
        job_ids: Sequence[str],
    ) -> dict[str, tuple[str | None, datetime | None, datetime | None]]:
        """Return ``(worker_id, lease_expires_at, heartbeat_at)`` per job.

        ``heartbeat_at`` is the execution row's ``updated_at`` — the last time
        the worker renewed the lease. Jobs without an execution row are absent
        from the result. Used by the public overview to derive worker health
        from lease freshness and to surface last-heartbeat time.
        """
        ids = [job_id for job_id in job_ids if job_id]
        states: dict[str, tuple[str | None, datetime | None, datetime | None]] = {}
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            placeholders = ",".join("?" for _ in chunk)
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT job_id, worker_id, lease_expires_at, updated_at
                    FROM openmontage_job_execution
                    WHERE job_id IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
            for row in rows:
                raw = row["lease_expires_at"]
                expires = (
                    _to_utc(datetime.fromisoformat(raw), "lease_expires_at")
                    if raw
                    else None
                )
                raw_heartbeat = row["updated_at"]
                heartbeat = (
                    _to_utc(datetime.fromisoformat(raw_heartbeat), "updated_at")
                    if raw_heartbeat
                    else None
                )
                states[row["job_id"]] = (row["worker_id"], expires, heartbeat)
        return states

    def waiting_since(self, workspace_id: str, job_ids: Sequence[str]) -> dict[str, str]:
        """Latest awaiting-approval event time (UTC ISO text) per waiting job.

        Feeds the dashboard's approval-wait metrics: only jobs that are still
        WAITING_APPROVAL are passed in by the caller, so MAX(created_at) over
        ``JOB_WAITING_APPROVAL`` events is the moment the current wait began
        (a re-approved stage writes a fresh event each time it re-enters the
        wait).
        """
        ids = [job_id for job_id in job_ids if job_id]
        if not workspace_id or not ids:
            return {}
        waiting: dict[str, str] = {}
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            placeholders = ",".join("?" for _ in chunk)
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT e.job_id AS job_id, MAX(e.created_at) AS waited_since
                    FROM openmontage_job_event e
                    JOIN openmontage_job j ON j.job_id = e.job_id
                    WHERE j.workspace_id = ?
                      AND e.job_id IN ({placeholders})
                      AND e.event_json LIKE '%"openmontage.job.waiting_approval"%'
                    GROUP BY e.job_id
                    """,
                    (workspace_id, *chunk),
                ).fetchall()
            for row in rows:
                waiting[row["job_id"]] = row["waited_since"]
        return waiting

    def create_job(
        self,
        request: JobCreateRequest,
        attribution: JobAttribution,
    ) -> JobSnapshot:
        _validate_artifact_input(request)
        request_identity = {
            "request": request.to_wire(),
            "attribution": attribution.to_wire(),
        }
        request_hash = hashlib.sha256(_canonical_json(request_identity).encode("utf-8")).hexdigest()

        with self._connect() as connection:
            self._begin_write(connection)
            existing = connection.execute(
                """
                SELECT request_hash, snapshot_json
                FROM openmontage_job
                WHERE workspace_id = ? AND client_request_id = ?
                """,
                (attribution.workspace_id, request.client_request_id),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash and not _same_request_identity(
                    existing["snapshot_json"], request_identity
                ):
                    raise JobConflictError(
                        "client_request_id was already used with different Job input or attribution"
                    )
                return JobSnapshot.model_validate_json(existing["snapshot_json"])

            # Producer mode: when OpenMontage configures no executor (MCP-only
            # container, OPENMONTAGE_AGENT_EXECUTOR_JSON unset) the calling runtime
            # (DSH / codex / claudecode) owns job execution, so accept the submission
            # and let the caller's worker drive it. Only when OpenMontage itself runs a
            # worker (executor configured) do we fail closed before persisting a Job
            # that could never leave QUEUED.
            executor_raw = os.environ.get("OPENMONTAGE_AGENT_EXECUTOR_JSON", "").strip()
            if executor_raw:
                executor_availability = delegated_executor_availability()
                if not executor_availability["available"]:
                    raise JobSubmissionError(
                        "Job submission is unavailable: "
                        + executor_availability["reason"]
                    )

            workflow = WorkflowDefinition.from_pipeline(request.workflow)
            now = _now()
            snapshot = JobSnapshot(
                job_id=f"om_job_{uuid4().hex}",
                status=JobStatus.QUEUED,
                workflow=workflow,
                attribution=attribution,
                request=JobRequestSnapshot.model_validate(request.to_wire()),
                stages=tuple(
                    StageSnapshot(
                        code=stage.code,
                        label_code=stage.label_code,
                        approval_required=stage.approval_required,
                        approval_status=(
                            ApprovalStatus.REQUIRED
                            if stage.approval_required
                            else ApprovalStatus.NOT_REQUIRED
                        ),
                    )
                    for stage in workflow.stages
                ),
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                """
                INSERT INTO openmontage_job (
                    job_id, workspace_id, client_request_id, request_hash,
                    snapshot_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.job_id,
                    attribution.workspace_id,
                    request.client_request_id,
                    request_hash,
                    _canonical_json(snapshot.to_wire()),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            created = self._persist_event(
                connection,
                snapshot,
                JobEventType.JOB_CREATED,
                {"workflow": {"name": workflow.name, "version": workflow.version}},
            )
        self._export_workspace_map()
        return created

    def get_job(self, job_id: str) -> JobSnapshot:
        with self._connect() as connection:
            return self._load_job(connection, job_id)

    def list_jobs(self, workspace_id: str, *, limit: int = 50) -> list[JobSnapshot]:
        """List durable jobs for one authenticated workspace, newest first."""
        if not workspace_id:
            raise ValueError("workspace_id is required")
        bounded = min(100, max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT snapshot_json
                FROM openmontage_job
                WHERE workspace_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (workspace_id, bounded),
            ).fetchall()
        return [JobSnapshot.model_validate_json(row["snapshot_json"]) for row in rows]

    def series_entries(
        self,
        workspace_id: str,
        *,
        start: datetime,
        end: datetime,
        limit: int = 5000,
    ) -> list[tuple[str, str, list[tuple[str, str, Any, Any]]]]:
        """Creation-window entries ``(created_at, status, stages)`` for one
        workspace.

        Feeds the daily dashboard series: bounds are compared as UTC ISO text,
        matching the ``created_at`` column written by :meth:`create_job`
        (timezone-aware UTC ``isoformat``). Only lightweight fields are
        extracted from each snapshot (status + per-stage code/status/timing) —
        never full model validation, so a 31-day window stays cheap.
        """
        if not workspace_id:
            raise ValueError("workspace_id is required")
        if end <= start:
            raise ValueError("series end must be after start")
        bounded = min(20000, max(1, int(limit)))
        start_text = start.astimezone(timezone.utc).isoformat()
        end_text = end.astimezone(timezone.utc).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT created_at, snapshot_json
                FROM openmontage_job
                WHERE workspace_id = ?
                  AND created_at >= ?
                  AND created_at < ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (workspace_id, start_text, end_text, bounded),
            ).fetchall()
        entries: list[tuple[str, str, list[tuple[str, str, Any, Any]]]] = []
        for row in rows:
            payload = json.loads(row["snapshot_json"])
            status = payload.get("status") if isinstance(payload, dict) else None
            stages: list[tuple[str, str, Any, Any]] = []
            raw_stages = payload.get("stages") if isinstance(payload, dict) else None
            for stage in raw_stages or []:
                if not isinstance(stage, dict):
                    continue
                stages.append(
                    (
                        str(stage.get("code") or "unknown"),
                        str(stage.get("status") or ""),
                        stage.get("started_at"),
                        stage.get("completed_at"),
                    )
                )
            entries.append((row["created_at"], str(status) if status else "", stages))
        return entries

    def claim_job(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> JobLease | None:
        if client_stage_only_enabled():
            # CI-only mode (plan §13): the legacy Worker is disabled. Report no
            # claimable work so run_once stays idle and never advances a stage
            # that the client Stage API owns.
            return None
        normalized_worker_id = worker_id.strip()
        if not normalized_worker_id or len(normalized_worker_id) > 256:
            raise JobLeaseError("worker_id must be between 1 and 256 characters")
        if lease_duration <= timedelta(0):
            raise JobLeaseError("lease_duration must be greater than zero")

        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = _normalize_now(now)
            expires_at = effective_now + lease_duration
            rows = connection.execute(
                """
                SELECT job.job_id, job.snapshot_json,
                       execution.lease_token AS exec_token,
                       execution.lease_expires_at AS exec_expiry
                FROM openmontage_job AS job
                LEFT JOIN openmontage_job_execution AS execution
                  ON execution.job_id = job.job_id
                WHERE (execution.next_attempt_at IS NULL OR execution.next_attempt_at <= ?)
                ORDER BY job.created_at ASC
                """,
                (effective_now.isoformat(),),
            ).fetchall()
            selected: JobSnapshot | None = None
            for row in rows:
                candidate = JobSnapshot.model_validate_json(row["snapshot_json"])
                if candidate.status not in {
                    JobStatus.QUEUED,
                    JobStatus.RUNNING,
                    JobStatus.CANCEL_REQUESTED,
                }:
                    continue
                # A lease is reclaimable exactly when it is not provably live:
                # no token, or an expiry that is missing/illegal/past. Parsing
                # here (rather than in SQL) keeps reclaim symmetric with the
                # active-lease check in _require_active_lease, so a corrupted
                # expiry row is recoverable instead of stranding the Job.
                exec_token = row["exec_token"]
                parsed_expiry = _parse_lease_expiry(row["exec_expiry"])
                if (
                    exec_token is not None
                    and parsed_expiry is not None
                    and parsed_expiry > effective_now
                ):
                    continue
                selected = candidate
                break
            if selected is None:
                return None

            lease_token = f"om_lease_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO openmontage_job_execution (
                    job_id, worker_id, lease_token, lease_expires_at,
                    next_attempt_at, attempts, last_error, updated_at
                ) VALUES (?, ?, ?, ?, NULL, 1, NULL, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    worker_id = excluded.worker_id,
                    lease_token = excluded.lease_token,
                    lease_expires_at = excluded.lease_expires_at,
                    next_attempt_at = NULL,
                    attempts = openmontage_job_execution.attempts + 1,
                    last_error = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    selected.job_id,
                    normalized_worker_id,
                    lease_token,
                    expires_at.isoformat(),
                    effective_now.isoformat(),
                ),
            )
            execution = connection.execute(
                "SELECT attempts FROM openmontage_job_execution WHERE job_id = ?",
                (selected.job_id,),
            ).fetchone()
            return JobLease(
                job_id=selected.job_id,
                worker_id=normalized_worker_id,
                lease_token=lease_token,
                expires_at=expires_at,
                attempt=int(execution["attempts"]),
                snapshot=selected,
            )

    def heartbeat_lease(
        self,
        lease: JobLease,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> JobLease:
        _reject_legacy_mutation_in_client_stage_only()
        if lease_duration <= timedelta(0):
            raise JobLeaseError("lease_duration must be greater than zero")
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = _normalize_now(now)
            expires_at = effective_now + lease_duration
            self._require_active_lease(
                connection,
                lease.job_id,
                lease.lease_token,
                effective_now,
            )
            connection.execute(
                """
                UPDATE openmontage_job_execution
                SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (expires_at.isoformat(), effective_now.isoformat(), lease.job_id),
            )
            snapshot = self._load_job(connection, lease.job_id)
        return JobLease(
            job_id=lease.job_id,
            worker_id=lease.worker_id,
            lease_token=lease.lease_token,
            expires_at=expires_at,
            attempt=lease.attempt,
            snapshot=snapshot,
        )

    def release_lease(
        self,
        job_id: str,
        *,
        lease_token: str,
        retry_at: datetime | None = None,
        retry_delay: timedelta | None = None,
        error: str | None = None,
        reset_attempts: bool = False,
        now: datetime | None = None,
    ) -> None:
        _reject_legacy_mutation_in_client_stage_only()
        _, retry_at = _resolve_release_retry(
            now=now, retry_at=retry_at, retry_delay=retry_delay
        )
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = _normalize_now(now)
            effective_retry_at = (
                effective_now + retry_delay if retry_delay is not None else retry_at
            )
            self._require_current_fencing_token(
                connection,
                job_id,
                lease_token,
                effective_now,
            )
            self._release_lease_record(
                connection,
                job_id,
                retry_at=effective_retry_at,
                error=error,
                reset_attempts=reset_attempts,
                now=effective_now,
            )

    def release_lease_or_confirm_cancel(
        self,
        job_id: str,
        *,
        lease_token: str,
        retry_at: datetime | None = None,
        retry_delay: timedelta | None = None,
        error: str | None = None,
        reset_attempts: bool = False,
        now: datetime | None = None,
    ) -> JobSnapshot:
        _reject_legacy_mutation_in_client_stage_only()
        _, retry_at = _resolve_release_retry(
            now=now, retry_at=retry_at, retry_delay=retry_delay
        )
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = _normalize_now(now)
            effective_retry_at = (
                effective_now + retry_delay if retry_delay is not None else retry_at
            )
            self._require_current_fencing_token(connection, job_id, lease_token, effective_now)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            cancelled = snapshot.status == JobStatus.CANCEL_REQUESTED
            if cancelled:
                snapshot = self._confirm_cancel_snapshot(connection, snapshot)
            self._release_lease_record(
                connection,
                job_id,
                retry_at=None if cancelled else effective_retry_at,
                error=None if cancelled else error,
                reset_attempts=cancelled or reset_attempts,
                now=effective_now,
            )
            return snapshot

    def require_active_lease(
        self,
        job_id: str,
        *,
        lease_token: str,
        now: datetime | None = None,
    ) -> None:
        with self._connect() as connection:
            self._require_active_lease(connection, job_id, lease_token, now or _now())

    def list_events(self, job_id: str, *, after_sequence: int = 0) -> list[JobEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_json
                FROM openmontage_job_event
                WHERE job_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (job_id, after_sequence),
            ).fetchall()
        return [JobEvent.model_validate_json(row["event_json"]) for row in rows]

    def publish_artifact(
        self,
        job_id: str,
        artifact: PublishedArtifact,
        *,
        lease_token: str | None = None,
        lease_now: datetime | None = None,
    ) -> JobSnapshot:
        _reject_legacy_mutation_in_client_stage_only()
        with self._connect() as connection:
            self._begin_write(connection)
            self._require_lease_if_present(connection, job_id, lease_token, lease_now, fencing=False)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            return self._publish_artifact_snapshot(connection, snapshot, artifact)

    def list_pending_outbox(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[OutboxRecord]:
        effective_now = _normalize_now(now, field="now/outbox")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_json, delivery_status, delivery_attempts,
                       next_attempt_at, delivery_lease_token,
                       delivery_lease_expires_at, delivered_at, last_error
                FROM openmontage_job_event
                WHERE (
                        delivery_status IN ('pending', 'retry')
                        AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                      ) OR (
                        delivery_status = 'publishing'
                        AND delivery_lease_expires_at <= ?
                      )
                ORDER BY created_at ASC, sequence ASC
                LIMIT ?
                """,
                (effective_now.isoformat(), effective_now.isoformat(), limit),
            ).fetchall()
        return [self._map_outbox_record(row) for row in rows]

    def claim_pending_outbox(
        self,
        *,
        lease_token: str,
        now: datetime | None = None,
        lease_seconds: float = 30.0,
        limit: int = 100,
    ) -> list[OutboxRecord]:
        if not lease_token:
            raise ValueError("lease_token is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = _normalize_now(now)
            expires_at = effective_now + timedelta(seconds=lease_seconds)
            rows = connection.execute(
                """
                SELECT event_id
                FROM openmontage_job_event
                WHERE (
                        delivery_status IN ('pending', 'retry')
                        AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                      ) OR (
                        delivery_status = 'publishing'
                        AND delivery_lease_expires_at <= ?
                      )
                ORDER BY created_at ASC, sequence ASC
                LIMIT ?
                """,
                (effective_now.isoformat(), effective_now.isoformat(), limit),
            ).fetchall()
            event_ids = [str(row["event_id"]) for row in rows]
            if not event_ids:
                return []
            placeholders = ",".join("?" for _ in event_ids)
            connection.execute(
                f"""
                UPDATE openmontage_job_event
                SET delivery_status = 'publishing',
                    delivery_lease_token = ?,
                    delivery_lease_expires_at = ?
                WHERE event_id IN ({placeholders})
                """,
                (lease_token, expires_at.isoformat(), *event_ids),
            )
            claimed = connection.execute(
                f"""
                SELECT event_json, delivery_status, delivery_attempts,
                       next_attempt_at, delivery_lease_token,
                       delivery_lease_expires_at, delivered_at, last_error
                FROM openmontage_job_event
                WHERE event_id IN ({placeholders})
                ORDER BY created_at ASC, sequence ASC
                """,
                event_ids,
            ).fetchall()
        return [self._map_outbox_record(row) for row in claimed]

    def get_outbox_record(self, event_id: str) -> OutboxRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT event_json, delivery_status, delivery_attempts,
                       next_attempt_at, delivery_lease_token,
                       delivery_lease_expires_at, delivered_at, last_error
                FROM openmontage_job_event
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            raise LookupError(f"OpenMontage Job event not found: {event_id}")
        return self._map_outbox_record(row)

    def mark_event_delivered(
        self,
        event_id: str,
        *,
        lease_token: str,
        delivered_at: datetime | None = None,
    ) -> None:
        timestamp = _normalize_now(delivered_at, field="delivered_at")
        with self._connect() as connection:
            self._begin_write(connection)
            cursor = connection.execute(
                """
                UPDATE openmontage_job_event
                SET delivery_status = 'delivered', delivered_at = ?,
                    next_attempt_at = NULL, last_error = NULL,
                    delivery_lease_token = NULL,
                    delivery_lease_expires_at = NULL
                WHERE event_id = ? AND delivery_status = 'publishing'
                  AND delivery_lease_token = ?
                """,
                (timestamp.isoformat(), event_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise OutboxLeaseError(
                    f"OpenMontage Job event delivery lease is no longer owned: {event_id}"
                )

    def mark_event_failed(
        self,
        event_id: str,
        *,
        lease_token: str,
        error: str,
        next_attempt_at: datetime,
    ) -> None:
        next_attempt_at = _to_utc(next_attempt_at, "next_attempt_at/outbox")
        with self._connect() as connection:
            self._begin_write(connection)
            cursor = connection.execute(
                """
                UPDATE openmontage_job_event
                SET delivery_status = 'retry',
                    delivery_attempts = delivery_attempts + 1,
                    next_attempt_at = ?, last_error = ?,
                    delivery_lease_token = NULL,
                    delivery_lease_expires_at = NULL
                WHERE event_id = ? AND delivery_status = 'publishing'
                  AND delivery_lease_token = ?
                """,
                (next_attempt_at.isoformat(), error[:1000], event_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise OutboxLeaseError(
                    f"OpenMontage Job event delivery lease is no longer owned: {event_id}"
                )

    def mark_event_dead_lettered(
        self,
        event_id: str,
        *,
        lease_token: str,
        error: str,
    ) -> None:
        with self._connect() as connection:
            self._begin_write(connection)
            cursor = connection.execute(
                """
                UPDATE openmontage_job_event
                SET delivery_status = 'dead_letter',
                    delivery_attempts = delivery_attempts + 1,
                    next_attempt_at = NULL, delivered_at = NULL,
                    last_error = ?, delivery_lease_token = NULL,
                    delivery_lease_expires_at = NULL
                WHERE event_id = ?
                  AND delivery_status = 'publishing'
                  AND delivery_lease_token = ?
                """,
                (error[:1000], event_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise OutboxLeaseError(
                    f"OpenMontage Job event delivery lease is no longer owned: {event_id}"
                )

    def start_stage(
        self,
        job_id: str,
        stage_code: str,
        *,
        lease_token: str | None = None,
        lease_now: datetime | None = None,
    ) -> JobSnapshot:
        _reject_legacy_mutation_in_client_stage_only()
        with self._connect() as connection:
            self._begin_write(connection)
            self._require_lease_if_present(connection, job_id, lease_token, lease_now)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            return self._start_stage_snapshot(connection, snapshot, stage_code)

    def start_stage_or_confirm_cancel(
        self,
        job_id: str,
        stage_code: str,
        *,
        lease_token: str,
        now: datetime | None = None,
    ) -> JobSnapshot:
        _reject_legacy_mutation_in_client_stage_only()
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = _normalize_now(now)
            self._require_active_lease(connection, job_id, lease_token, effective_now)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            if snapshot.status == JobStatus.CANCEL_REQUESTED:
                snapshot = self._confirm_cancel_snapshot(connection, snapshot)
                self._release_lease_record(
                    connection,
                    job_id,
                    retry_at=None,
                    error=None,
                    reset_attempts=True,
                    now=effective_now,
                )
                return snapshot
            return self._start_stage_snapshot(connection, snapshot, stage_code)

    def complete_stage(
        self,
        job_id: str,
        stage_code: str,
        *,
        lease_token: str | None = None,
        lease_now: datetime | None = None,
    ) -> JobSnapshot:
        _reject_legacy_mutation_in_client_stage_only()
        with self._connect() as connection:
            self._begin_write(connection)
            self._require_lease_if_present(connection, job_id, lease_token, lease_now, fencing=False)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            return self._complete_stage_snapshot(connection, snapshot, stage_code)

    def complete_stage_or_confirm_cancel(
        self,
        job_id: str,
        stage_code: str,
        *,
        lease_token: str,
        now: datetime | None = None,
    ) -> JobSnapshot:
        _reject_legacy_mutation_in_client_stage_only()
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = _normalize_now(now)
            self._require_current_fencing_token(connection, job_id, lease_token, effective_now)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            if snapshot.status == JobStatus.CANCEL_REQUESTED:
                snapshot = self._confirm_cancel_snapshot(connection, snapshot)
            else:
                snapshot = self._complete_stage_snapshot(
                    connection,
                    snapshot,
                    stage_code,
                )
            self._release_lease_record(
                connection,
                job_id,
                retry_at=None,
                error=None,
                reset_attempts=True,
                now=effective_now,
            )
            return snapshot

    def update_stage_progress(
        self,
        job_id: str,
        stage_code: str,
        *,
        completed_units: int,
        total_units: int,
        label_code: str,
        lease_token: str | None = None,
        lease_now: datetime | None = None,
    ) -> JobSnapshot:
        _reject_legacy_mutation_in_client_stage_only()
        if total_units <= 0:
            raise JobStateError("total_units must be greater than zero")
        if completed_units < 0 or completed_units > total_units:
            raise JobStateError("completed_units must be between zero and total_units")
        if not label_code.strip():
            raise JobStateError("label_code must not be empty")

        with self._connect() as connection:
            self._begin_write(connection)
            self._require_lease_if_present(connection, job_id, lease_token, lease_now)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            _, stage = self._stage(snapshot, stage_code)
            if stage.status != StageStatus.RUNNING:
                raise JobStateError(f"Stage {stage_code!r} is not running")
            stage.progress = {
                "completedUnits": completed_units,
                "totalUnits": total_units,
                "labelCode": label_code,
            }
            return self._persist_event(
                connection,
                snapshot,
                JobEventType.STAGE_PROGRESSED,
                {**self._stage_payload(stage), "progress": stage.progress},
            )

    # ------------------------------------------------------------------
    # Client-driven stage execution (plan §6-§8)
    #
    # The client Agent owns the cognitive work of each stage. It begins a
    # stage (exclusive lease + attempt), reports progress, and submits the
    # stage result — artifacts, checkpoint and status — in a single business
    # operation. The server owns validation, checkpoint writing, Job state
    # transitions and the event log. No Worker/DSH executor is involved.
    # ------------------------------------------------------------------

    @staticmethod
    def _client_lease_duration(lease_duration: timedelta | None) -> timedelta:
        if lease_duration is not None:
            if lease_duration <= timedelta(0):
                raise ClientStageError(
                    "STAGE_LEASE_INVALID", "lease_duration must be greater than zero"
                )
            return lease_duration
        raw = os.environ.get("OPENMONTAGE_CLIENT_STAGE_LEASE_SECONDS", "1800").strip()
        try:
            seconds = float(raw)
        except ValueError:
            seconds = 1800.0
        if seconds <= 0:
            seconds = 1800.0
        return timedelta(seconds=seconds)

    @staticmethod
    def _fetch_active_client_lease(
        connection: sqlite3.Connection,
        job_id: str,
        stage: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT idempotency_key, stage_attempt, lease_token, expires_at
            FROM openmontage_client_stage_lease
            WHERE job_id = ? AND stage = ? AND status = 'active'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (job_id, stage),
        ).fetchone()

    @staticmethod
    def _release_client_lease(
        connection: sqlite3.Connection,
        job_id: str,
        stage: str,
        lease_token: str,
        *,
        now: datetime,
    ) -> None:
        connection.execute(
            """
            UPDATE openmontage_client_stage_lease
            SET status = 'released', updated_at = ?
            WHERE job_id = ? AND stage = ? AND lease_token = ? AND status = 'active'
            """,
            (now.isoformat(), job_id, stage, lease_token),
        )

    @classmethod
    def _require_client_lease(
        cls,
        connection: sqlite3.Connection,
        job_id: str,
        stage: str,
        lease_token: str,
        stage_attempt: int,
        now: datetime,
        *,
        fencing: bool,
    ) -> sqlite3.Row:
        """Require the caller to own the current (Job, stage) lease.

        ``fencing=False`` (progress heartbeats) additionally requires the lease
        to be unexpired; ``fencing=True`` (submit/settle) only requires the
        token to be current, mirroring the Worker settlement contract — a lease
        that lapsed mid-flight may still settle as long as nobody re-began the
        stage (a re-begin mints a fresh token, which fences the stale one).
        """
        now = _normalize_now(now)
        if not lease_token or not lease_token.strip():
            raise ClientStageError("STAGE_LEASE_INVALID", "lease_token must be non-empty")
        row = cls._fetch_active_client_lease(connection, job_id, stage)
        if row is None:
            raise ClientStageError(
                "STAGE_LEASE_INVALID",
                f"no active client lease for stage {stage!r}; call begin_client_stage first",
            )
        if row["lease_token"] != lease_token:
            raise ClientStageError(
                "STAGE_LEASE_INVALID",
                f"client lease token for stage {stage!r} is no longer current",
            )
        if int(row["stage_attempt"]) != stage_attempt:
            raise ClientStageError(
                "STAGE_ATTEMPT_MISMATCH",
                f"stage_attempt {stage_attempt} does not match the active lease "
                f"attempt {int(row['stage_attempt'])}",
            )
        if not fencing:
            expires_at = _parse_lease_expiry(row["expires_at"])
            if expires_at is None:
                raise ClientStageError(
                    "STAGE_LEASE_INVALID", "client lease has no valid expiry"
                )
            if expires_at <= now:
                raise ClientStageError(
                    "STAGE_LEASE_EXPIRED",
                    f"client lease for stage {stage!r} has expired; re-begin the stage",
                )
        return row

    @staticmethod
    def _renew_client_lease(
        connection: sqlite3.Connection,
        job_id: str,
        stage: str,
        lease_token: str,
        *,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        connection.execute(
            """
            UPDATE openmontage_client_stage_lease
            SET expires_at = ?, updated_at = ?
            WHERE job_id = ? AND stage = ? AND lease_token = ? AND status = 'active'
            """,
            (expires_at.isoformat(), now.isoformat(), job_id, stage, lease_token),
        )

    def begin_client_stage(
        self,
        job_id: str,
        stage_code: str,
        *,
        idempotency_key: str,
        expected_sequence: int | None = None,
        lease_duration: timedelta | None = None,
        now: datetime | None = None,
    ) -> ClientStageLease:
        """Begin exclusive client execution of one stage (plan §6.1).

        Validates the workflow/stage, predecessor completion, Job state,
        sequence fencing and concurrency, then marks the stage RUNNING, mints
        an opaque lease, and records the ``client_stage.started`` event.
        Idempotent on ``idempotency_key``: a replay with identical arguments
        returns the original response; different arguments are rejected with
        ``IDEMPOTENCY_CONFLICT``.
        """
        _validate_client_idempotency_key(idempotency_key)
        duration = self._client_lease_duration(lease_duration)
        request_hash = hashlib.sha256(
            _canonical_json(
                {
                    "command": "begin_client_stage",
                    "stage": stage_code,
                    "expectedSequence": expected_sequence,
                }
            ).encode("utf-8")
        ).hexdigest()

        # A cancel confirmation must survive a raise: the with-block rolls back
        # on exception, so confirm the cancel (persisting CANCELLED) and only
        # then surface the error once the transaction has committed.
        cancelled = False

        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = _normalize_now(now)

            replay = connection.execute(
                """
                SELECT request_hash, response_json
                FROM openmontage_client_stage_lease
                WHERE job_id = ? AND stage = ? AND idempotency_key = ?
                """,
                (job_id, stage_code, idempotency_key),
            ).fetchone()
            if replay is not None:
                if replay["request_hash"] != request_hash:
                    raise ClientStageError(
                        "IDEMPOTENCY_CONFLICT",
                        "idempotency_key was already used for a different begin_client_stage request",
                    )
                return ClientStageLease.from_wire(json.loads(replay["response_json"]))

            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            self._require_expected_sequence(snapshot, expected_sequence)
            if snapshot.status == JobStatus.CANCEL_REQUESTED:
                self._confirm_cancel_snapshot(connection, snapshot)
                cancelled = True

            if not cancelled:
                stage_index, stage = self._stage(snapshot, stage_code)

                active = self._fetch_active_client_lease(connection, job_id, stage_code)
                if active is not None:
                    expiry = _parse_lease_expiry(active["expires_at"])
                    if expiry is not None and expiry > effective_now:
                        raise ClientStageError(
                            "STAGE_ALREADY_OWNED",
                            f"stage {stage!r} already has an active client lease until "
                            f"{expiry.isoformat()}",
                        )
                    # Expired/corrupt lease: supersede it so the stage can resume.
                    connection.execute(
                        """
                        UPDATE openmontage_client_stage_lease
                        SET status = 'released', updated_at = ?
                        WHERE job_id = ? AND stage = ? AND status = 'active'
                        """,
                        (effective_now.isoformat(), job_id, stage_code),
                    )

                incomplete = [
                    predecessor.code
                    for predecessor in snapshot.stages[:stage_index]
                    if predecessor.status not in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}
                ]
                if incomplete:
                    raise ClientStageError(
                        "STAGE_STATE_INVALID",
                        f"stage {stage_code!r} has incomplete predecessor stages: {incomplete}",
                    )
                if snapshot.status == JobStatus.QUEUED:
                    validate_job_transition(JobStatus.QUEUED, JobStatus.RUNNING)
                    snapshot.status = JobStatus.RUNNING
                elif snapshot.status != JobStatus.RUNNING:
                    raise ClientStageError(
                        "STAGE_STATE_INVALID",
                        f"cannot begin a client stage while Job is {snapshot.status}",
                    )
                if stage.status == StageStatus.PENDING:
                    self._validate_stage(stage.status, StageStatus.RUNNING)
                elif stage.status == StageStatus.RUNNING:
                    # Resume after a lost/expired lease: same stage, new attempt.
                    pass
                else:
                    raise ClientStageError(
                        "STAGE_STATE_INVALID",
                        f"stage {stage_code!r} is {stage.status} and cannot be begun",
                    )

                stage.status = StageStatus.RUNNING
                stage.attempt += 1
                stage.started_at = effective_now
                stage.completed_at = None
                snapshot.current_stage = stage_code

                expires_at = effective_now + duration
                lease_token = f"om_clease_{uuid4().hex}"
                snapshot = self._persist_event(
                    connection,
                    snapshot,
                    JobEventType.CLIENT_STAGE_STARTED,
                    {
                        **self._stage_payload(stage),
                        "leaseExpiresAt": expires_at.isoformat().replace("+00:00", "Z"),
                    },
                )

                lease = ClientStageLease(
                    job_id=job_id,
                    stage=stage_code,
                    stage_attempt=stage.attempt,
                    lease_token=lease_token,
                    expires_at=expires_at,
                    snapshot=snapshot,
                )
                connection.execute(
                    """
                    INSERT INTO openmontage_client_stage_lease (
                        job_id, stage, idempotency_key, request_hash, stage_attempt,
                        lease_token, response_json, status, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        job_id,
                        stage_code,
                        idempotency_key,
                        request_hash,
                        stage.attempt,
                        lease_token,
                        _canonical_json(lease.to_wire()),
                        expires_at.isoformat(),
                        effective_now.isoformat(),
                        effective_now.isoformat(),
                    ),
                )

        # A cancelled Job cannot begin a stage: the cancel confirmation has
        # committed above; surface the error only now that the transaction is
        # closed so it is not rolled back.
        if cancelled:
            raise ClientStageError("JOB_CANCELLED", f"Job {job_id} has been cancelled")

        # Project workspace on disk (checkpoint home + Backlot marker). This
        # is filesystem I/O, kept outside the SQLite write transaction.
        from lib.checkpoint import init_project

        init_project(
            job_id,
            title=str(snapshot.request.brief.get("title") or job_id),
            pipeline_type=snapshot.workflow.name,
            pipeline_dir=self.projects_dir,
        )
        return lease

    def update_client_stage_progress(
        self,
        job_id: str,
        stage_code: str,
        *,
        stage_attempt: int,
        completed_units: int,
        total_units: int,
        label_code: str,
        lease_token: str,
        idempotency_key: str,
        lease_duration: timedelta | None = None,
        now: datetime | None = None,
    ) -> JobSnapshot:
        """Report client progress for a running stage and renew the lease.

        Validates the lease (token + attempt + expiry), updates the stage
        progress payload, renews the lease, and records the
        ``client_stage.progressed`` event. Replays of the same
        ``idempotency_key`` return the original snapshot.
        """
        _validate_client_idempotency_key(idempotency_key)
        if total_units <= 0:
            raise ClientStageError("STAGE_STATE_INVALID", "total_units must be greater than zero")
        if completed_units < 0 or completed_units > total_units:
            raise ClientStageError(
                "STAGE_STATE_INVALID", "completed_units must be between zero and total_units"
            )
        if not label_code.strip():
            raise ClientStageError("STAGE_STATE_INVALID", "label_code must not be empty")
        duration = self._client_lease_duration(lease_duration)

        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = _normalize_now(now)
            command_hash = self._command_hash(
                idempotency_key,
                {
                    "command": "update_client_stage_progress",
                    "stage": stage_code,
                    "stageAttempt": stage_attempt,
                    "completedUnits": completed_units,
                    "totalUnits": total_units,
                    "labelCode": label_code,
                },
            )
            replay = self._read_client_command_result(connection, job_id, idempotency_key, command_hash)
            if replay is not None:
                return replay
            self._require_client_lease(
                connection, job_id, stage_code, lease_token, stage_attempt,
                effective_now, fencing=False,
            )
            self._renew_client_lease(
                connection, job_id, stage_code, lease_token,
                expires_at=effective_now + duration, now=effective_now,
            )
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            _, stage = self._stage(snapshot, stage_code)
            if stage.status != StageStatus.RUNNING:
                raise ClientStageError(
                    "STAGE_STATE_INVALID", f"stage {stage_code!r} is not running"
                )
            stage.progress = {
                "completedUnits": completed_units,
                "totalUnits": total_units,
                "labelCode": label_code,
            }
            snapshot = self._persist_event(
                connection,
                snapshot,
                JobEventType.CLIENT_STAGE_PROGRESSED,
                {**self._stage_payload(stage), "progress": stage.progress},
            )
            self._record_command_result(connection, snapshot, idempotency_key, command_hash)
            return snapshot

    @staticmethod
    def _validate_client_submit_stage(
        snapshot: JobSnapshot,
        stage_code: str,
        stage_attempt: int,
        status: str,
    ) -> tuple[int, StageSnapshot]:
        """Shared validation for a client stage submission.

        Returns the (index, stage) of the target stage. Raises
        ``ClientStageError`` on a non-running stage, an attempt mismatch, or an
        approval-rule violation. Does NOT handle ``CANCEL_REQUESTED`` — the
        caller confirms the cancel first.
        """
        stage_index, stage = JobService._stage(snapshot, stage_code)
        if stage.status != StageStatus.RUNNING:
            raise ClientStageError(
                "STAGE_STATE_INVALID",
                f"stage {stage_code!r} is {stage.status}, not RUNNING",
            )
        if stage.attempt != stage_attempt:
            raise ClientStageError(
                "STAGE_ATTEMPT_MISMATCH",
                f"stage_attempt {stage_attempt} does not match the current "
                f"stage attempt {stage.attempt}",
            )
        if (
            status == "completed"
            and stage.approval_required
            and stage.approval_status != ApprovalStatus.APPROVED
        ):
            raise ClientStageError(
                "HUMAN_APPROVAL_REQUIRED",
                f"stage {stage_code!r} requires human approval: submit "
                "awaiting_human first and wait for approve_video_stage",
            )
        if status == "awaiting_human" and not stage.approval_required:
            raise ClientStageError(
                "STAGE_STATE_INVALID",
                f"stage {stage_code!r} does not require human approval",
            )
        return stage_index, stage

    def _archive_stage_checkpoint(self, job_id: str, stage_code: str) -> None:
        """Move a just-written checkpoint into ``history/`` after a cancel.

        ``submit_client_stage`` writes the checkpoint to disk *before* the
        state transition (outside the SQLite transaction). If the transition
        then confirms a cancellation, the on-disk checkpoint no longer matches
        the CANCELLED Job. Moving it aside to ``history/`` keeps the project
        directory consistent instead of leaving an ``in_progress``/``completed``
        checkpoint for a Job that is CANCELLED.
        """
        path = self.projects_dir / job_id / f"checkpoint_{stage_code}.json"
        if not path.exists():
            return
        try:
            import shutil

            history_dir = path.parent / "history"
            history_dir.mkdir(parents=True, exist_ok=True)
            target = history_dir / (
                f"checkpoint_{stage_code}_cancelled_{path.stat().st_mtime_ns}.json"
            )
            shutil.move(str(path), str(target))
        except OSError:
            import logging

            logging.getLogger(__name__).warning(
                "Could not archive cancelled checkpoint %s", path
            )

    def submit_client_stage(
        self,
        job_id: str,
        stage_code: str,
        *,
        stage_attempt: int,
        status: str,
        lease_token: str,
        idempotency_key: str,
        artifacts: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        instruction_provenance: Any = None,
        lease_duration: timedelta | None = None,
        now: datetime | None = None,
    ) -> JobSnapshot:
        """Submit the result of a client-driven stage in one business operation.

        Validates lease/stage/attempt, artifact and checkpoint schemas,
        predecessor and approval rules, media references and instruction
        provenance; writes the standard checkpoint under
        ``projects/<job_id>/``; records the Job event; advances the Job state;
        and releases (or, for ``in_progress``, renews) the lease. Returns the
        latest Job snapshot. Idempotent on ``idempotency_key``.

        ``idempotency_key`` must be unique per Job *within the submit/update/
        approve/cancel namespace* (these share the ``openmontage_job_command``
        table keyed by ``(job_id, idempotency_key)``) — reuse within that
        namespace with different arguments returns ``IDEMPOTENCY_CONFLICT``.
        ``begin_client_stage`` uses a separate lease table keyed by
        ``(job_id, stage, idempotency_key)``, so a begin key never collides
        with a submit key; still, a distinct key per operation is the cleanest
        contract (the client driver suffixes ``:begin`` / ``:submit`` for this
        reason).
        ``instruction_provenance`` is advisory audit metadata (what the client
        read); when supplied it is verified against the live CI repository
        before any state change, but it is not itself a hard gate.
        """
        _validate_client_idempotency_key(idempotency_key)
        allowed_statuses = {"completed", "awaiting_human", "failed", "in_progress"}
        if status not in allowed_statuses:
            raise ClientStageError(
                "STAGE_STATE_INVALID",
                f"status must be one of {sorted(allowed_statuses)}, got {status!r}",
            )
        if artifacts is not None and not isinstance(artifacts, dict):
            raise ClientStageError("ARTIFACT_SCHEMA_INVALID", "artifacts must be an object")
        if metadata is not None and not isinstance(metadata, dict):
            raise ClientStageError("ARTIFACT_SCHEMA_INVALID", "metadata must be an object")
        artifacts = artifacts or {}
        duration = self._client_lease_duration(lease_duration)

        command_hash = self._command_hash(
            idempotency_key,
            {
                "command": "submit_client_stage",
                "stage": stage_code,
                "stageAttempt": stage_attempt,
                "status": status,
                "artifacts": artifacts,
                "metadata": metadata,
                "instructionProvenance": instruction_provenance,
            },
        )

        # Idempotency replay is checked first, before any disk write, so a
        # retry never re-writes a checkpoint or re-archives it.
        with self._connect() as connection:
            replay = self._read_client_command_result(
                connection, job_id, idempotency_key, command_hash
            )
        if replay is not None:
            return replay

        # Instruction provenance is verified against the live CI repository
        # before any state changes (plan §14: events record what the client
        # actually read).
        provenance_entries: list[dict[str, str]] = []
        if instruction_provenance is not None:
            from openmontage.instruction_files import (
                InstructionFileError,
                verify_instruction_provenance,
            )

            try:
                provenance_entries = verify_instruction_provenance(instruction_provenance)
            except InstructionFileError as exc:
                raise ClientStageError(exc.code, f"instruction provenance rejected: {exc}") from exc

        # Media references are validated against the CI project directory;
        # completed/awaiting submissions must reference files that exist.
        from openmontage.media_references import (
            MediaReferenceError,
            validate_media_references,
        )

        require_exists = status in {"completed", "awaiting_human"}
        try:
            media_paths = validate_media_references(
                artifacts, self.projects_dir / job_id, require_exists=require_exists
            )
        except MediaReferenceError as exc:
            raise ClientStageError("MEDIA_REFERENCE_INVALID", str(exc)) from exc

        # Lease ownership is the first gate: without a current lease the client
        # must not write anything. It is checked read-only here, before the
        # checkpoint write, and re-checked inside the write transaction below.
        effective_now = _normalize_now(now)
        with self._connect() as connection:
            self._require_client_lease(
                connection, job_id, stage_code, lease_token, stage_attempt,
                effective_now, fencing=True,
            )

        # Stage / approval validation and the checkpoint write happen outside
        # the SQLite write transaction. The disk checkpoint must never be
        # written while holding ``BEGIN IMMEDIATE`` (slow filesystem I/O), and
        # must not be rolled back by a later state-transition failure: if the
        # transition fails after the checkpoint is written, the Job stays
        # RUNNING in SQLite (authoritative) and a re-begin reconciles by
        # archiving the superseded checkpoint (plan §8.3).
        snapshot = self.get_job(job_id)
        if snapshot.status != JobStatus.CANCEL_REQUESTED:
            _, stage = self._validate_client_submit_stage(
                snapshot, stage_code, stage_attempt, status
            )
            from lib.checkpoint import CheckpointValidationError, write_checkpoint

            checkpoint_metadata = {
                **(metadata or {}),
                "stage_attempt": stage_attempt,
                "instruction_provenance": provenance_entries,
                "media_references": media_paths,
            }
            try:
                write_checkpoint(
                    self.projects_dir,
                    job_id,
                    stage_code,
                    status,
                    artifacts,
                    pipeline_type=snapshot.workflow.name,
                    human_approval_required=stage.approval_required,
                    human_approved=stage.approval_status == ApprovalStatus.APPROVED,
                    error=(metadata or {}).get("error") if status == "failed" else None,
                    metadata=checkpoint_metadata,
                )
            except CheckpointValidationError as exc:
                raise ClientStageError("ARTIFACT_SCHEMA_INVALID", str(exc)) from exc
            except OSError as exc:
                raise ClientStageError(
                    "CHECKPOINT_WRITE_FAILED", f"checkpoint could not be written: {exc}"
                ) from exc

        with self._connect() as connection:
            self._begin_write(connection)
            self._require_client_lease(
                connection, job_id, stage_code, lease_token, stage_attempt,
                effective_now, fencing=True,
            )
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            if snapshot.status == JobStatus.CANCEL_REQUESTED:
                snapshot = self._confirm_cancel_snapshot(connection, snapshot)
                self._release_client_lease(
                    connection, job_id, stage_code, lease_token, now=effective_now
                )
                # The checkpoint was written to disk before this transaction.
                # The Job is now CANCELLED; archive the just-written checkpoint
                # so on-disk state cannot claim a stage advanced that the Job
                # no longer reflects (review P1: cancel/checkpoint race).
                self._archive_stage_checkpoint(job_id, stage_code)
                self._record_command_result(connection, snapshot, idempotency_key, command_hash)
                return snapshot

            # Re-validate inside the transaction against the freshly loaded
            # snapshot; the lease guarantees no concurrent owner, so this is
            # defense-in-depth against a state change between the checkpoint
            # write and the transition.
            _, stage = self._validate_client_submit_stage(
                snapshot, stage_code, stage_attempt, status
            )

            def stage_event_payload() -> dict[str, Any]:
                # Built lazily at persist time so the payload reflects the
                # stage status after the transition, not before it.
                return {
                    **self._stage_payload(stage),
                    "instructionProvenance": provenance_entries,
                }

            if status == "in_progress":
                # Heartbeat: keep the stage RUNNING, renew the lease, record
                # the checkpoint write so a reconnecting client can resume
                # from metadata.partial_progress.
                self._renew_client_lease(
                    connection, job_id, stage_code, lease_token,
                    expires_at=effective_now + duration, now=effective_now,
                )
                payload = stage_event_payload()
                if isinstance((metadata or {}).get("partial_progress"), dict):
                    payload["progress"] = (metadata or {})["partial_progress"]
                snapshot = self._persist_event(
                    connection, snapshot, JobEventType.CLIENT_STAGE_CHECKPOINTED, payload
                )
            elif status == "completed":
                self._validate_stage(stage.status, StageStatus.SUCCEEDED)
                stage.status = StageStatus.SUCCEEDED
                stage.completed_at = effective_now
                snapshot.current_stage = None
                snapshot = self._persist_event(
                    connection, snapshot, JobEventType.CLIENT_STAGE_COMPLETED, stage_event_payload()
                )
                self._release_client_lease(
                    connection, job_id, stage_code, lease_token, now=effective_now
                )
                if all(
                    item.status in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}
                    for item in snapshot.stages
                ):
                    snapshot = self._complete_job_snapshot(connection, snapshot)
            elif status == "awaiting_human":
                self._validate_stage(stage.status, StageStatus.WAITING_APPROVAL)
                try:
                    validate_job_transition(snapshot.status, JobStatus.WAITING_APPROVAL)
                except ValueError as exc:
                    raise ClientStageError("STAGE_STATE_INVALID", str(exc)) from exc
                stage.status = StageStatus.WAITING_APPROVAL
                stage.approval_status = ApprovalStatus.PENDING
                snapshot.status = JobStatus.WAITING_APPROVAL
                snapshot.current_stage = None
                snapshot = self._persist_event(
                    connection,
                    snapshot,
                    JobEventType.CLIENT_STAGE_AWAITING_APPROVAL,
                    stage_event_payload(),
                )
                self._release_client_lease(
                    connection, job_id, stage_code, lease_token, now=effective_now
                )
            else:  # failed
                error_message = str((metadata or {}).get("error") or "client stage failed")
                error_code = str(
                    (metadata or {}).get("error_code") or "OPENMONTAGE_CLIENT_STAGE_FAILED"
                )
                self._validate_stage(stage.status, StageStatus.FAILED)
                stage.status = StageStatus.FAILED
                stage.completed_at = effective_now
                failed_payload = stage_event_payload()
                failed_payload["error"] = {"code": error_code, "message": error_message[:500]}
                snapshot = self._persist_event(
                    connection, snapshot, JobEventType.CLIENT_STAGE_FAILED, failed_payload
                )
                snapshot = self._fail_job_snapshot(
                    connection,
                    snapshot,
                    code=error_code,
                    message=error_message,
                    retryable=bool((metadata or {}).get("retryable", False)),
                )
                self._release_client_lease(
                    connection, job_id, stage_code, lease_token, now=effective_now
                )

            self._record_command_result(connection, snapshot, idempotency_key, command_hash)
            return snapshot

    def request_stage_approval(
        self,
        job_id: str,
        stage_code: str,
        *,
        reason: str,
        lease_token: str | None = None,
        lease_now: datetime | None = None,
    ) -> JobSnapshot:
        _reject_legacy_mutation_in_client_stage_only()
        with self._connect() as connection:
            self._begin_write(connection)
            self._require_lease_if_present(connection, job_id, lease_token, lease_now, fencing=False)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            return self._request_stage_approval_snapshot(
                connection,
                snapshot,
                stage_code,
                reason=reason,
            )

    def request_stage_approval_or_confirm_cancel(
        self,
        job_id: str,
        stage_code: str,
        *,
        reason: str,
        lease_token: str,
        now: datetime | None = None,
    ) -> JobSnapshot:
        _reject_legacy_mutation_in_client_stage_only()
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = _normalize_now(now)
            self._require_current_fencing_token(connection, job_id, lease_token, effective_now)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            if snapshot.status == JobStatus.CANCEL_REQUESTED:
                snapshot = self._confirm_cancel_snapshot(connection, snapshot)
            else:
                snapshot = self._request_stage_approval_snapshot(
                    connection,
                    snapshot,
                    stage_code,
                    reason=reason,
                )
            self._release_lease_record(
                connection,
                job_id,
                retry_at=None,
                error=None,
                reset_attempts=True,
                now=effective_now,
            )
            return snapshot

    def resolve_stage_approval(
        self,
        job_id: str,
        stage_code: str,
        *,
        approved: bool,
        expected_sequence: int | None = None,
        idempotency_key: str | None = None,
    ) -> JobSnapshot:
        with self._connect() as connection:
            self._begin_write(connection)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            command_hash = self._command_hash(
                idempotency_key,
                {
                    "command": "resolve_stage_approval",
                    "stage": stage_code,
                    "approved": approved,
                    "expectedSequence": expected_sequence,
                },
            )
            replay = self._read_command_result(
                connection, job_id, idempotency_key, command_hash
            )
            if replay is not None:
                return replay
            self._require_expected_sequence(snapshot, expected_sequence)
            _, stage = self._stage(snapshot, stage_code)
            if stage.status != StageStatus.WAITING_APPROVAL:
                raise JobStateError(f"Stage {stage_code!r} has no pending approval")
            if approved:
                self._validate_stage(stage.status, StageStatus.RUNNING)
                validate_job_transition(snapshot.status, JobStatus.RUNNING)
                stage.status = StageStatus.RUNNING
                stage.approval_status = ApprovalStatus.APPROVED
                snapshot.status = JobStatus.RUNNING
            else:
                self._validate_stage(stage.status, StageStatus.FAILED)
                validate_job_transition(snapshot.status, JobStatus.FAILED)
                stage.status = StageStatus.FAILED
                stage.approval_status = ApprovalStatus.REJECTED
                stage.completed_at = _now()
                snapshot.status = JobStatus.FAILED
            snapshot = self._persist_event(
                connection,
                snapshot,
                JobEventType.APPROVAL_RESOLVED,
                {**self._stage_payload(stage), "approved": approved},
            )
            if not approved:
                snapshot = self._persist_event(
                    connection,
                    snapshot,
                    JobEventType.JOB_FAILED,
                    {
                        "stage": stage.code,
                        "status": JobStatus.FAILED.value,
                        "error": {
                            "code": "OPENMONTAGE_APPROVAL_REJECTED",
                            "message": "Stage approval was rejected",
                            "retryable": False,
                        },
                    },
                )
            self._record_command_result(
                connection, snapshot, idempotency_key, command_hash
            )
            return snapshot

    def fail_job(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        retryable: bool,
        lease_token: str | None = None,
        lease_now: datetime | None = None,
    ) -> JobSnapshot:
        _reject_legacy_mutation_in_client_stage_only()
        self._validate_job_failure(code, message)

        with self._connect() as connection:
            self._begin_write(connection)
            self._require_lease_if_present(connection, job_id, lease_token, lease_now, fencing=False)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            return self._fail_job_snapshot(
                connection,
                snapshot,
                code=code,
                message=message,
                retryable=retryable,
            )

    def fail_job_or_confirm_cancel(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        retryable: bool,
        lease_token: str,
        now: datetime | None = None,
    ) -> JobSnapshot:
        _reject_legacy_mutation_in_client_stage_only()
        self._validate_job_failure(code, message)
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = _normalize_now(now)
            self._require_current_fencing_token(connection, job_id, lease_token, effective_now)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            if snapshot.status == JobStatus.CANCEL_REQUESTED:
                snapshot = self._confirm_cancel_snapshot(connection, snapshot)
            else:
                snapshot = self._fail_job_snapshot(
                    connection,
                    snapshot,
                    code=code,
                    message=message,
                    retryable=retryable,
                )
            self._release_lease_record(
                connection,
                job_id,
                retry_at=None,
                error=None,
                reset_attempts=True,
                now=effective_now,
            )
            return snapshot

    def request_cancel(
        self,
        job_id: str,
        *,
        expected_sequence: int | None = None,
        idempotency_key: str | None = None,
    ) -> JobSnapshot:
        with self._connect() as connection:
            self._begin_write(connection)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            command_hash = self._command_hash(
                idempotency_key,
                {
                    "command": "request_cancel",
                    "expectedSequence": expected_sequence,
                },
            )
            replay = self._read_command_result(
                connection, job_id, idempotency_key, command_hash
            )
            if replay is not None:
                return replay
            self._require_expected_sequence(snapshot, expected_sequence)
            try:
                validate_job_transition(snapshot.status, JobStatus.CANCEL_REQUESTED)
            except ValueError as exc:
                raise JobStateError(str(exc)) from exc
            snapshot.status = JobStatus.CANCEL_REQUESTED
            snapshot = self._persist_event(
                connection,
                snapshot,
                JobEventType.JOB_CANCEL_REQUESTED,
                {"status": JobStatus.CANCEL_REQUESTED.value},
            )
            self._record_command_result(
                connection, snapshot, idempotency_key, command_hash
            )
            return snapshot

    def confirm_cancel(
        self,
        job_id: str,
        *,
        lease_token: str | None = None,
        lease_now: datetime | None = None,
    ) -> JobSnapshot:
        _reject_legacy_mutation_in_client_stage_only()
        with self._connect() as connection:
            self._begin_write(connection)
            self._require_lease_if_present(connection, job_id, lease_token, lease_now, fencing=False)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            return self._confirm_cancel_snapshot(connection, snapshot)

    def complete_job(
        self,
        job_id: str,
        *,
        lease_token: str | None = None,
        lease_now: datetime | None = None,
    ) -> JobSnapshot:
        _reject_legacy_mutation_in_client_stage_only()
        with self._connect() as connection:
            self._begin_write(connection)
            self._require_lease_if_present(connection, job_id, lease_token, lease_now, fencing=False)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            return self._complete_job_snapshot(connection, snapshot)

    def complete_job_or_confirm_cancel(
        self,
        job_id: str,
        *,
        artifact: PublishedArtifact | None,
        lease_token: str,
        now: datetime | None = None,
    ) -> JobSnapshot:
        _reject_legacy_mutation_in_client_stage_only()
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = _normalize_now(now)
            self._require_current_fencing_token(connection, job_id, lease_token, effective_now)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            if snapshot.status == JobStatus.CANCEL_REQUESTED:
                snapshot = self._confirm_cancel_snapshot(connection, snapshot)
            else:
                if artifact is not None:
                    snapshot = self._publish_artifact_snapshot(
                        connection,
                        snapshot,
                        artifact,
                    )
                snapshot = self._complete_job_snapshot(connection, snapshot)
            self._release_lease_record(
                connection,
                job_id,
                retry_at=None,
                error=None,
                reset_attempts=True,
                now=effective_now,
            )
            return snapshot

    def _start_stage_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot: JobSnapshot,
        stage_code: str,
    ) -> JobSnapshot:
        stage_index, stage = self._stage(snapshot, stage_code)
        incomplete = [
            predecessor.code
            for predecessor in snapshot.stages[:stage_index]
            if predecessor.status not in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}
        ]
        if incomplete:
            raise JobStateError(
                f"Stage {stage_code!r} has incomplete predecessor stages: {incomplete}"
            )
        if snapshot.status == JobStatus.QUEUED:
            validate_job_transition(JobStatus.QUEUED, JobStatus.RUNNING)
            snapshot.status = JobStatus.RUNNING
        elif snapshot.status != JobStatus.RUNNING:
            raise JobStateError(f"Cannot start a stage while Job is {snapshot.status}")
        self._validate_stage(stage.status, StageStatus.RUNNING)
        stage.status = StageStatus.RUNNING
        stage.attempt += 1
        stage.started_at = _now()
        stage.completed_at = None
        snapshot.current_stage = stage_code
        return self._persist_event(
            connection,
            snapshot,
            JobEventType.STAGE_STARTED,
            self._stage_payload(stage),
        )

    def _publish_artifact_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot: JobSnapshot,
        artifact: PublishedArtifact,
    ) -> JobSnapshot:
        if artifact.job_id != snapshot.job_id:
            raise JobStateError("Published artifact Job identity does not match")
        if artifact.employee_id != snapshot.attribution.employee_id:
            raise JobStateError("Published artifact employee identity does not match")
        existing = next(
            (
                item
                for item in snapshot.artifacts
                if item.employee_artifact_id == artifact.employee_artifact_id
            ),
            None,
        )
        if existing is not None:
            if existing != artifact:
                raise JobConflictError(
                    "employee_artifact_id was already published with different metadata"
                )
            return snapshot
        snapshot.artifacts = (*snapshot.artifacts, artifact)
        return self._persist_event(
            connection,
            snapshot,
            JobEventType.ARTIFACT_PUBLISHED,
            {
                "artifactId": artifact.employee_artifact_id,
                "employeeId": artifact.employee_id,
                "role": artifact.role,
                "fileName": artifact.file_name,
                "mediaType": artifact.media_type,
                "sizeBytes": artifact.size_bytes,
                "sha256": artifact.sha256,
                "publishedAt": artifact.published_at.isoformat().replace("+00:00", "Z"),
            },
        )

    def _complete_stage_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot: JobSnapshot,
        stage_code: str,
    ) -> JobSnapshot:
        _, stage = self._stage(snapshot, stage_code)
        if stage.approval_required and stage.approval_status != ApprovalStatus.APPROVED:
            raise JobStateError(f"Stage {stage_code!r} requires approval before completion")
        self._validate_stage(stage.status, StageStatus.SUCCEEDED)
        stage.status = StageStatus.SUCCEEDED
        stage.completed_at = _now()
        snapshot.current_stage = None
        return self._persist_event(
            connection,
            snapshot,
            JobEventType.STAGE_COMPLETED,
            self._stage_payload(stage),
        )

    def _request_stage_approval_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot: JobSnapshot,
        stage_code: str,
        *,
        reason: str,
    ) -> JobSnapshot:
        _, stage = self._stage(snapshot, stage_code)
        if not stage.approval_required:
            raise JobStateError(f"Stage {stage_code!r} does not require approval")
        self._validate_stage(stage.status, StageStatus.WAITING_APPROVAL)
        try:
            validate_job_transition(snapshot.status, JobStatus.WAITING_APPROVAL)
        except ValueError as exc:
            raise JobStateError(str(exc)) from exc
        stage.status = StageStatus.WAITING_APPROVAL
        stage.approval_status = ApprovalStatus.PENDING
        snapshot.status = JobStatus.WAITING_APPROVAL
        return self._persist_event(
            connection,
            snapshot,
            JobEventType.JOB_WAITING_APPROVAL,
            {**self._stage_payload(stage), "reason": reason},
        )

    def _complete_job_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot: JobSnapshot,
    ) -> JobSnapshot:
        if any(
            stage.status not in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}
            for stage in snapshot.stages
        ):
            raise JobStateError(
                "Cannot complete Job until all stages succeeded or were skipped"
            )
        try:
            validate_job_transition(snapshot.status, JobStatus.SUCCEEDED)
        except ValueError as exc:
            raise JobStateError(str(exc)) from exc
        snapshot.status = JobStatus.SUCCEEDED
        snapshot.current_stage = None
        return self._persist_event(
            connection,
            snapshot,
            JobEventType.JOB_COMPLETED,
            {"status": JobStatus.SUCCEEDED.value},
        )

    @staticmethod
    def _release_lease_record(
        connection: sqlite3.Connection,
        job_id: str,
        *,
        retry_at: datetime | None,
        error: str | None,
        reset_attempts: bool,
        now: datetime,
    ) -> None:
        connection.execute(
            """
            UPDATE openmontage_job_execution
            SET worker_id = NULL, lease_token = NULL, lease_expires_at = NULL,
                next_attempt_at = ?, last_error = ?,
                attempts = CASE WHEN ? THEN 0 ELSE attempts END,
                updated_at = ?
            WHERE job_id = ?
            """,
            (
                retry_at.isoformat() if retry_at is not None else None,
                error[:1000] if error else None,
                reset_attempts,
                now.isoformat(),
                job_id,
            ),
        )

    @staticmethod
    def _validate_job_failure(code: str, message: str) -> None:
        if not code.strip():
            raise JobStateError("code must not be empty")
        if not message.strip():
            raise JobStateError("message must not be empty")

    def _fail_job_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot: JobSnapshot,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> JobSnapshot:
        try:
            validate_job_transition(snapshot.status, JobStatus.FAILED)
        except ValueError as exc:
            raise JobStateError(str(exc)) from exc

        stage_code = snapshot.current_stage
        if stage_code is not None:
            _, stage = self._stage(snapshot, stage_code)
            if stage.status in {StageStatus.RUNNING, StageStatus.WAITING_APPROVAL}:
                self._validate_stage(stage.status, StageStatus.FAILED)
                stage.status = StageStatus.FAILED
                stage.completed_at = _now()
        snapshot.status = JobStatus.FAILED
        return self._persist_event(
            connection,
            snapshot,
            JobEventType.JOB_FAILED,
            {
                "stage": stage_code,
                "status": JobStatus.FAILED.value,
                "error": {
                    "code": code,
                    # AgentSpace's signed event contract caps failure summaries at
                    # 500 characters. Full executor diagnostics remain in the
                    # per-assignment execution log. Preserve both the start
                    # (what failed) and the end (root cause) in the bounded
                    # public event instead of keeping only the trailing bytes.
                    "message": _summarize_error(message, 500),
                    "retryable": retryable,
                },
            },
        )

    def _confirm_cancel_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot: JobSnapshot,
    ) -> JobSnapshot:
        try:
            validate_job_transition(snapshot.status, JobStatus.CANCELLED)
        except ValueError as exc:
            raise JobStateError(str(exc)) from exc
        for stage in snapshot.stages:
            if stage.status in {StageStatus.RUNNING, StageStatus.WAITING_APPROVAL}:
                self._validate_stage(stage.status, StageStatus.CANCELLED)
                stage.status = StageStatus.CANCELLED
                stage.completed_at = _now()
        snapshot.status = JobStatus.CANCELLED
        return self._persist_event(
            connection,
            snapshot,
            JobEventType.JOB_CANCELLED,
            {"status": JobStatus.CANCELLED.value},
        )

    def _load_job(self, connection: sqlite3.Connection, job_id: str) -> JobSnapshot:
        row = connection.execute(
            "SELECT snapshot_json FROM openmontage_job WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise JobNotFoundError(f"OpenMontage Job not found: {job_id}")
        return JobSnapshot.model_validate_json(row["snapshot_json"])

    @staticmethod
    def _fetch_lease_row(
        connection: sqlite3.Connection,
        job_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT lease_token, lease_expires_at
            FROM openmontage_job_execution
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()

    @classmethod
    def _require_active_lease(
        cls,
        connection: sqlite3.Connection,
        job_id: str,
        lease_token: str,
        now: datetime,
    ) -> sqlite3.Row:
        """Require a genuinely live lease: token match AND not yet expired.

        Used by live, in-flight mutations — heartbeat, start, progress — where a
        worker whose lease has lapsed must NOT keep driving the Job. A lapsed
        owner has lost its liveness guarantee: another Worker may reap the lease
        at any moment, so letting it start a stage (which begins paid execution)
        would open a double-Worker window. The lapsed owner's own next claim is
        the recovery path.

        ``now`` is normalized to aware-UTC here so a naive clock argument is
        rejected before the expiry comparison (which would otherwise raise
        TypeError against the parsed aware expiry).
        """
        now = _normalize_now(now)
        row = cls._fetch_lease_row(connection, job_id)
        if row is None or row["lease_token"] != lease_token:
            raise JobLeaseError("Job lease token is no longer active")
        expires_at = _parse_lease_expiry(row["lease_expires_at"])
        if expires_at is None:
            # Fail-closed: a token is present but its expiry is missing or
            # unparseable, so liveness cannot be proven. Honoring the token
            # here would fail-open and let a stale owner keep driving the Job
            # while a new Worker cannot reclaim it (claim_job treats this same
            # token-with-no-expiry state as reclaimable). The corrupted lease
            # is recovered by the next claim, not by trusting the stale token.
            raise JobLeaseError("Job lease has no valid expiry")
        if expires_at <= now:
            raise JobLeaseError("Job lease has expired")
        return row

    @classmethod
    def _require_current_fencing_token(
        cls,
        connection: sqlite3.Connection,
        job_id: str,
        lease_token: str,
        now: datetime,  # validated aware-UTC at entry; not an expiry gate here
    ) -> sqlite3.Row:
        """Require only that the token has not been superseded by a newer claim.

        Used by atomic settlement (complete, fail, cancel, release, publish) so
        the publish-once guarantee survives SQLite write-lock contention: the
        heartbeat and the settle both take ``BEGIN IMMEDIATE``. If the heartbeat
        cannot renew in time because its write is queued behind the settle's
        write, the lease may lapse before the settle acquires the lock — yet the
        same owner must still be allowed to settle, or the publish-once guarantee
        degrades into retries and duplicate work. Every claim mints a fresh
        token, so when a reaper takes over an expired lease the previous owner's
        next settle fails the token match below. Expiry alone never fences a
        settle; only a newer token does.

        ``now`` is normalized to aware-UTC so a naive clock argument is rejected
        consistently with the active-lease path (it is still not an expiry gate).
        """
        now = _normalize_now(now)
        row = cls._fetch_lease_row(connection, job_id)
        if row is None or row["lease_token"] != lease_token:
            raise JobLeaseError("Job lease token is no longer current")
        return row

    @classmethod
    def _require_lease_if_present(
        cls,
        connection: sqlite3.Connection,
        job_id: str,
        lease_token: str | None,
        lease_now: datetime | None,
        *,
        fencing: bool = False,
    ) -> None:
        if lease_token is None:
            return
        checker = cls._require_current_fencing_token if fencing else cls._require_active_lease
        checker(connection, job_id, lease_token, lease_now or _now())

    @staticmethod
    def _require_expected_sequence(
        snapshot: JobSnapshot,
        expected_sequence: int | None,
    ) -> None:
        if expected_sequence is None:
            return
        if expected_sequence < 0:
            raise JobStateError("expected_sequence must be non-negative")
        if snapshot.last_sequence != expected_sequence:
            raise JobConflictError(
                "Job changed since the action was requested; refresh and retry"
            )

    @staticmethod
    def _command_hash(
        idempotency_key: str | None,
        command: dict[str, Any],
    ) -> str | None:
        if idempotency_key is None:
            return None
        if not idempotency_key.strip() or len(idempotency_key) > 256:
            raise JobStateError("idempotency_key must be between 1 and 256 characters")
        return hashlib.sha256(_canonical_json(command).encode("utf-8")).hexdigest()

    @staticmethod
    def _read_command_result(
        connection: sqlite3.Connection,
        job_id: str,
        idempotency_key: str | None,
        command_hash: str | None,
    ) -> JobSnapshot | None:
        if idempotency_key is None or command_hash is None:
            return None
        row = connection.execute(
            """
            SELECT command_hash, snapshot_json
            FROM openmontage_job_command
            WHERE job_id = ? AND idempotency_key = ?
            """,
            (job_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["command_hash"] != command_hash:
            raise JobConflictError(
                "idempotency_key was already used for a different Job command"
            )
        return JobSnapshot.model_validate_json(row["snapshot_json"])

    def _read_client_command_result(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        idempotency_key: str | None,
        command_hash: str | None,
    ) -> JobSnapshot | None:
        """Read a prior client-stage command result, translating the generic
        idempotency conflict into the client-stage ``IDEMPOTENCY_CONFLICT``
        code the client Agent branches on."""
        try:
            return self._read_command_result(connection, job_id, idempotency_key, command_hash)
        except JobConflictError as exc:
            raise ClientStageError("IDEMPOTENCY_CONFLICT", str(exc)) from exc

    @staticmethod
    def _record_command_result(
        connection: sqlite3.Connection,
        snapshot: JobSnapshot,
        idempotency_key: str | None,
        command_hash: str | None,
    ) -> None:
        if idempotency_key is None or command_hash is None:
            return
        connection.execute(
            """
            INSERT INTO openmontage_job_command (
                job_id, idempotency_key, command_hash, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                snapshot.job_id,
                idempotency_key,
                command_hash,
                _canonical_json(snapshot.to_wire()),
                _now().isoformat(),
            ),
        )

    @staticmethod
    def _map_outbox_record(row: sqlite3.Row) -> OutboxRecord:
        return OutboxRecord(
            event=JobEvent.model_validate_json(row["event_json"]),
            status=row["delivery_status"],
            delivery_attempts=row["delivery_attempts"],
            next_attempt_at=row["next_attempt_at"],
            delivery_lease_token=row["delivery_lease_token"],
            delivery_lease_expires_at=row["delivery_lease_expires_at"],
            delivered_at=row["delivered_at"],
            last_error=row["last_error"],
        )

    @staticmethod
    def _stage(snapshot: JobSnapshot, stage_code: str) -> tuple[int, StageSnapshot]:
        for index, stage in enumerate(snapshot.stages):
            if stage.code == stage_code:
                return index, stage
        raise JobStateError(f"Unknown stage {stage_code!r} for workflow {snapshot.workflow.name!r}")

    @staticmethod
    def _validate_stage(previous: StageStatus | str, next_status: StageStatus) -> None:
        try:
            validate_stage_transition(StageStatus(previous), next_status)
        except ValueError as exc:
            raise JobStateError(str(exc)) from exc

    @staticmethod
    def _stage_payload(stage: StageSnapshot) -> dict[str, Any]:
        return {
            "stage": stage.code,
            "stageAttempt": stage.attempt,
            "status": StageStatus(stage.status).value,
            "approvalStatus": ApprovalStatus(stage.approval_status).value,
        }

    def _persist_event(
        self,
        connection: sqlite3.Connection,
        snapshot: JobSnapshot,
        event_type: JobEventType,
        payload: dict[str, Any],
    ) -> JobSnapshot:
        now = _now()
        sequence = snapshot.last_sequence + 1
        event = JobEvent.create(
            event_id=f"om_evt_{uuid4().hex}",
            event_type=event_type,
            job_id=snapshot.job_id,
            sequence=sequence,
            attribution=snapshot.attribution,
            payload=payload,
            occurred_at=now,
        )
        snapshot.last_sequence = sequence
        snapshot.updated_at = now
        connection.execute(
            """
            UPDATE openmontage_job
            SET snapshot_json = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (_canonical_json(snapshot.to_wire()), now.isoformat(), snapshot.job_id),
        )
        connection.execute(
            """
            INSERT INTO openmontage_job_event (
                event_id, job_id, sequence, event_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                snapshot.job_id,
                sequence,
                _canonical_json(event.to_wire()),
                now.isoformat(),
            ),
        )
        return snapshot
