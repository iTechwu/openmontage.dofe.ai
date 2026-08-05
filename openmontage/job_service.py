"""Durable video Job snapshots and transactional event outbox."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from openmontage.contracts import (
    ApprovalStatus,
    JobAttribution,
    JobCreateRequest,
    JobEvent,
    JobEventType,
    JobSnapshot,
    JobStatus,
    OutboxRecord,
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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
                if existing["request_hash"] != request_hash:
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
                request=request,
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
                       next_attempt_at, delivered_at, last_error
                FROM openmontage_job_event
                WHERE delivery_status IN ('pending', 'retry')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC, sequence ASC
                LIMIT ?
                """,
                (effective_now.isoformat(), limit),
            ).fetchall()
        return [self._map_outbox_record(row) for row in rows]

    def get_outbox_record(self, event_id: str) -> OutboxRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT event_json, delivery_status, delivery_attempts,
                       next_attempt_at, delivered_at, last_error
                FROM openmontage_job_event
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            raise LookupError(f"OpenMontage Job event not found: {event_id}")
        return self._map_outbox_record(row)

    def mark_event_delivered(self, event_id: str, *, delivered_at: datetime | None = None) -> None:
        timestamp = delivered_at or _now()
        with self._connect() as connection:
            self._begin_write(connection)
            cursor = connection.execute(
                """
                UPDATE openmontage_job_event
                SET delivery_status = 'delivered', delivered_at = ?,
                    next_attempt_at = NULL, last_error = NULL
                WHERE event_id = ?
                """,
                (timestamp.isoformat(), event_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"OpenMontage Job event not found: {event_id}")

    def mark_event_failed(
        self,
        event_id: str,
        *,
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
                    next_attempt_at = ?, last_error = ?
                WHERE event_id = ?
                """,
                (next_attempt_at.isoformat(), error[:1000], event_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"OpenMontage Job event not found: {event_id}")

    def start_stage(self, job_id: str, stage_code: str) -> JobSnapshot:
        with self._connect() as connection:
            self._begin_write(connection)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            stage_index, stage = self._stage(snapshot, stage_code)
            incomplete = [
                predecessor.code
                for predecessor in snapshot.stages[:stage_index]
                if predecessor.status not in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}
            ]
            if incomplete:
                raise JobStateError(f"Stage {stage_code!r} has incomplete predecessor stages: {incomplete}")
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

    def complete_stage(self, job_id: str, stage_code: str) -> JobSnapshot:
        with self._connect() as connection:
            self._begin_write(connection)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
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

    def update_stage_progress(
        self,
        job_id: str,
        stage_code: str,
        *,
        completed_units: int,
        total_units: int,
        label_code: str,
    ) -> JobSnapshot:
        if total_units <= 0:
            raise JobStateError("total_units must be greater than zero")
        if completed_units < 0 or completed_units > total_units:
            raise JobStateError("completed_units must be between zero and total_units")
        if not label_code.strip():
            raise JobStateError("label_code must not be empty")

        with self._connect() as connection:
            self._begin_write(connection)
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

    def request_stage_approval(self, job_id: str, stage_code: str, *, reason: str) -> JobSnapshot:
        with self._connect() as connection:
            self._begin_write(connection)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
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
    ) -> JobSnapshot:
        if not code.strip():
            raise JobStateError("code must not be empty")
        if not message.strip():
            raise JobStateError("message must not be empty")

        with self._connect() as connection:
            self._begin_write(connection)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
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
                        "message": message,
                        "retryable": retryable,
                    },
                },
            )

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

    def confirm_cancel(self, job_id: str) -> JobSnapshot:
        with self._connect() as connection:
            self._begin_write(connection)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
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

    def complete_job(self, job_id: str) -> JobSnapshot:
        with self._connect() as connection:
            self._begin_write(connection)
            snapshot = self._load_job(connection, job_id).model_copy(deep=True)
            if any(
                stage.status not in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}
                for stage in snapshot.stages
            ):
                raise JobStateError("Cannot complete Job until all stages succeeded or were skipped")
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

    def _load_job(self, connection: sqlite3.Connection, job_id: str) -> JobSnapshot:
        row = connection.execute(
            "SELECT snapshot_json FROM openmontage_job WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise JobNotFoundError(f"OpenMontage Job not found: {job_id}")
        return JobSnapshot.model_validate_json(row["snapshot_json"])

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
