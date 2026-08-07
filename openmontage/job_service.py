"""Durable video Job snapshots and transactional event outbox."""

from __future__ import annotations

import hashlib
import json
import sqlite3
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


class JobConflictError(RuntimeError):
    """Raised when an idempotency key is reused with different immutable input."""


class JobStateError(RuntimeError):
    """Raised when a requested Job lifecycle transition is not allowed."""


class JobNotFoundError(LookupError):
    """Raised when a Job ID does not exist."""


class JobLeaseError(RuntimeError):
    """Raised when a Worker lease is invalid, expired, or no longer owned."""


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_lease_expiry(raw: str | None) -> datetime | None:
    """Parse a stored lease expiry, tolerating naive (legacy) timestamps."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


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


class JobService:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
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

    def create_job(
        self,
        request: JobCreateRequest,
        attribution: JobAttribution,
    ) -> JobSnapshot:
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
            return self._persist_event(
                connection,
                snapshot,
                JobEventType.JOB_CREATED,
                {"workflow": {"name": workflow.name, "version": workflow.version}},
            )

    def get_job(self, job_id: str) -> JobSnapshot:
        with self._connect() as connection:
            return self._load_job(connection, job_id)

    def claim_job(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> JobLease | None:
        normalized_worker_id = worker_id.strip()
        if not normalized_worker_id or len(normalized_worker_id) > 256:
            raise JobLeaseError("worker_id must be between 1 and 256 characters")
        if lease_duration <= timedelta(0):
            raise JobLeaseError("lease_duration must be greater than zero")

        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = now if now is not None else _now()
            expires_at = effective_now + lease_duration
            rows = connection.execute(
                """
                SELECT job.job_id, job.snapshot_json
                FROM openmontage_job AS job
                LEFT JOIN openmontage_job_execution AS execution
                  ON execution.job_id = job.job_id
                WHERE (execution.lease_token IS NULL OR execution.lease_expires_at <= ?)
                  AND (execution.next_attempt_at IS NULL OR execution.next_attempt_at <= ?)
                ORDER BY job.created_at ASC
                """,
                (effective_now.isoformat(), effective_now.isoformat()),
            ).fetchall()
            selected: JobSnapshot | None = None
            for row in rows:
                candidate = JobSnapshot.model_validate_json(row["snapshot_json"])
                if candidate.status in {
                    JobStatus.QUEUED,
                    JobStatus.RUNNING,
                    JobStatus.CANCEL_REQUESTED,
                }:
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
        if lease_duration <= timedelta(0):
            raise JobLeaseError("lease_duration must be greater than zero")
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = now if now is not None else _now()
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
        if retry_at is not None and retry_delay is not None:
            raise JobLeaseError("retry_at and retry_delay are mutually exclusive")
        if retry_delay is not None and retry_delay < timedelta(0):
            raise JobLeaseError("retry_delay must not be negative")
        call_now = now if now is not None else _now()
        if retry_at is not None and retry_at < call_now:
            raise JobLeaseError("retry_at must not be earlier than now")
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = now if now is not None else _now()
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
        if retry_at is not None and retry_delay is not None:
            raise JobLeaseError("retry_at and retry_delay are mutually exclusive")
        if retry_delay is not None and retry_delay < timedelta(0):
            raise JobLeaseError("retry_delay must not be negative")
        call_now = now if now is not None else _now()
        if retry_at is not None and retry_at < call_now:
            raise JobLeaseError("retry_at must not be earlier than now")
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = now if now is not None else _now()
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
        with self._connect() as connection:
            self._begin_write(connection)
            self._require_lease_if_present(connection, job_id, lease_token, lease_now, fencing=True)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            return self._publish_artifact_snapshot(connection, snapshot, artifact)

    def list_pending_outbox(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[OutboxRecord]:
        effective_now = now or _now()
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
            effective_now = now if now is not None else _now()
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
        timestamp = delivered_at or _now()
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
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = now if now is not None else _now()
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
        with self._connect() as connection:
            self._begin_write(connection)
            self._require_lease_if_present(connection, job_id, lease_token, lease_now, fencing=True)
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
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = now if now is not None else _now()
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

    def request_stage_approval(
        self,
        job_id: str,
        stage_code: str,
        *,
        reason: str,
        lease_token: str | None = None,
        lease_now: datetime | None = None,
    ) -> JobSnapshot:
        with self._connect() as connection:
            self._begin_write(connection)
            self._require_lease_if_present(connection, job_id, lease_token, lease_now, fencing=True)
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
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = now if now is not None else _now()
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
        self._validate_job_failure(code, message)

        with self._connect() as connection:
            self._begin_write(connection)
            self._require_lease_if_present(connection, job_id, lease_token, lease_now, fencing=True)
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
        self._validate_job_failure(code, message)
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = now if now is not None else _now()
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
        with self._connect() as connection:
            self._begin_write(connection)
            self._require_lease_if_present(connection, job_id, lease_token, lease_now, fencing=True)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            return self._confirm_cancel_snapshot(connection, snapshot)

    def complete_job(
        self,
        job_id: str,
        *,
        lease_token: str | None = None,
        lease_now: datetime | None = None,
    ) -> JobSnapshot:
        with self._connect() as connection:
            self._begin_write(connection)
            self._require_lease_if_present(connection, job_id, lease_token, lease_now, fencing=True)
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
        with self._connect() as connection:
            self._begin_write(connection)
            effective_now = now if now is not None else _now()
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
        """
        row = cls._fetch_lease_row(connection, job_id)
        if row is None or row["lease_token"] != lease_token:
            raise JobLeaseError("Job lease token is no longer active")
        expires_at = _parse_lease_expiry(row["lease_expires_at"])
        if expires_at is not None and expires_at <= now:
            raise JobLeaseError("Job lease has expired")
        return row

    @classmethod
    def _require_current_fencing_token(
        cls,
        connection: sqlite3.Connection,
        job_id: str,
        lease_token: str,
        now: datetime,  # retained for the clock-based call contract; not a gate here
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
        """
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
