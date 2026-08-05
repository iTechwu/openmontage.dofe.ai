"""Durable Job Worker that reconciles external Agent checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

from lib.checkpoint import (
    CheckpointValidationError,
    PROJECT_MARKER_FILENAME,
    init_project,
    read_checkpoint,
)
from openmontage.contracts import ApprovalStatus, JobSnapshot, JobStatus, StageSnapshot, StageStatus
from openmontage.job_service import JobLease, JobLeaseError, JobService
from openmontage.pipeline_executor import (
    PipelineExecutionError,
    PipelineExecutionResult,
    PipelineExecutor,
    StageAssignment,
)


@dataclass(frozen=True)
class JobWorkerResult:
    job_id: str
    stage: str | None
    outcome: str


class JobWorker:
    def __init__(
        self,
        service: JobService,
        executor: PipelineExecutor,
        *,
        projects_dir: str | Path,
        worker_id: str,
        lease_duration: timedelta = timedelta(minutes=2),
        heartbeat_interval: timedelta = timedelta(seconds=30),
        retry_delay: timedelta = timedelta(seconds=15),
        max_executor_attempts: int = 3,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be greater than zero")
        if heartbeat_interval <= timedelta(0) or heartbeat_interval >= lease_duration:
            raise ValueError("heartbeat_interval must be positive and shorter than lease_duration")
        if retry_delay < timedelta(0):
            raise ValueError("retry_delay must not be negative")
        if max_executor_attempts < 1:
            raise ValueError("max_executor_attempts must be greater than zero")
        self.service = service
        self.executor = executor
        self.projects_dir = Path(projects_dir).expanduser().resolve()
        self.worker_id = worker_id
        self.lease_duration = lease_duration
        self.heartbeat_interval = heartbeat_interval
        self.retry_delay = retry_delay
        self.max_executor_attempts = max_executor_attempts

    def run_once(self, *, now: datetime | None = None) -> JobWorkerResult | None:
        lease = self.service.claim_job(
            worker_id=self.worker_id,
            lease_duration=self.lease_duration,
            now=now,
        )
        if lease is None:
            return None
        snapshot = self.service.get_job(lease.job_id)
        if snapshot.status == JobStatus.CANCEL_REQUESTED:
            self.service.confirm_cancel(
                snapshot.job_id,
                lease_token=lease.lease_token,
                lease_now=self._clock(now),
            )
            self._release(lease, now=now, reset_attempts=True)
            return JobWorkerResult(snapshot.job_id, snapshot.current_stage, "job_cancelled")

        self._ensure_project(snapshot)
        stage = self._next_stage(snapshot)
        if stage is None:
            self.service.complete_job(
                snapshot.job_id,
                lease_token=lease.lease_token,
                lease_now=self._clock(now),
            )
            self._release(lease, now=now, reset_attempts=True)
            return JobWorkerResult(snapshot.job_id, None, "job_completed")

        try:
            checkpoint = read_checkpoint(
                self.projects_dir,
                snapshot.job_id,
                stage.code,
            )
        except (CheckpointValidationError, OSError, ValueError) as exc:
            self._fail_job(
                lease,
                code="OPENMONTAGE_VALIDATION_FAILED",
                message="Pipeline checkpoint validation failed",
                now=now,
            )
            return JobWorkerResult(snapshot.job_id, stage.code, "job_failed")

        return self._advance(
            lease,
            snapshot,
            stage,
            checkpoint,
            now=now,
            checkpoint_was_recovered=checkpoint is not None,
        )

    def _advance(
        self,
        lease: JobLease,
        snapshot: JobSnapshot,
        stage: StageSnapshot,
        checkpoint: dict | None,
        *,
        now: datetime | None,
        checkpoint_was_recovered: bool,
    ) -> JobWorkerResult:
        if stage.status == StageStatus.PENDING:
            snapshot = self.service.start_stage(
                snapshot.job_id,
                stage.code,
                lease_token=lease.lease_token,
                lease_now=self._clock(now),
            )
            stage = self._stage(snapshot, stage.code)

        if checkpoint is not None and checkpoint["status"] in {"completed", "awaiting_human"}:
            if stage.approval_required and stage.approval_status != ApprovalStatus.APPROVED:
                if stage.status != StageStatus.WAITING_APPROVAL:
                    self.service.request_stage_approval(
                        snapshot.job_id,
                        stage.code,
                        reason="Review the completed pipeline stage before continuing",
                        lease_token=lease.lease_token,
                        lease_now=self._clock(now),
                    )
                self._release(lease, now=now, reset_attempts=True)
                return JobWorkerResult(snapshot.job_id, stage.code, "waiting_approval")
            if checkpoint["status"] == "completed":
                self.service.complete_stage(
                    snapshot.job_id,
                    stage.code,
                    lease_token=lease.lease_token,
                    lease_now=self._clock(now),
                )
                self._release(lease, now=now, reset_attempts=True)
                outcome = "stage_reconciled" if checkpoint_was_recovered else "stage_completed"
                return JobWorkerResult(snapshot.job_id, stage.code, outcome)

        if checkpoint is not None and checkpoint["status"] == "failed":
            self._fail_job(
                lease,
                code="OPENMONTAGE_PIPELINE_STAGE_FAILED",
                message="Pipeline stage reported a failed checkpoint",
                now=now,
            )
            return JobWorkerResult(snapshot.job_id, stage.code, "job_failed")

        latest = self.service.get_job(snapshot.job_id)
        assignment = StageAssignment.from_job(
            latest,
            stage=stage.code,
            stage_attempt=self._stage(latest, stage.code).attempt,
            projects_dir=self.projects_dir,
        )
        try:
            execution, lease = self._execute_with_heartbeat(assignment, lease, now=now)
        except PipelineExecutionError:
            if lease.attempt >= self.max_executor_attempts:
                self._fail_job(
                    lease,
                    code="OPENMONTAGE_AGENT_EXECUTOR_FAILED",
                    message="External Agent executor failed after bounded retries",
                    now=now,
                )
                return JobWorkerResult(snapshot.job_id, stage.code, "job_failed")
            self._release(
                lease,
                now=now,
                retry_at=self._clock(now) + self.retry_delay,
                error="External Agent executor failed",
            )
            return JobWorkerResult(snapshot.job_id, stage.code, "retry_scheduled")

        return self._reconcile_execution(
            lease,
            execution,
            now=now,
        )

    def _reconcile_execution(
        self,
        lease: JobLease,
        execution: PipelineExecutionResult,
        *,
        now: datetime | None,
    ) -> JobWorkerResult:
        snapshot = self.service.get_job(lease.job_id)
        stage = self._stage(snapshot, snapshot.current_stage or execution.checkpoint["stage"])
        checkpoint = execution.checkpoint
        if checkpoint["status"] == "completed":
            if stage.approval_required and stage.approval_status != ApprovalStatus.APPROVED:
                self.service.request_stage_approval(
                    snapshot.job_id,
                    stage.code,
                    reason="Review the completed pipeline stage before continuing",
                    lease_token=lease.lease_token,
                    lease_now=self._clock(now),
                )
                self._release(lease, now=now, reset_attempts=True)
                return JobWorkerResult(snapshot.job_id, stage.code, "waiting_approval")
            self.service.complete_stage(
                snapshot.job_id,
                stage.code,
                lease_token=lease.lease_token,
                lease_now=self._clock(now),
            )
            self._release(lease, now=now, reset_attempts=True)
            return JobWorkerResult(snapshot.job_id, stage.code, "stage_completed")
        if checkpoint["status"] == "awaiting_human":
            if not stage.approval_required:
                self._fail_job(
                    lease,
                    code="OPENMONTAGE_VALIDATION_FAILED",
                    message="Pipeline checkpoint requested an undeclared approval gate",
                    now=now,
                )
                return JobWorkerResult(snapshot.job_id, stage.code, "job_failed")
            self.service.request_stage_approval(
                snapshot.job_id,
                stage.code,
                reason="Review the pipeline stage before continuing",
                lease_token=lease.lease_token,
                lease_now=self._clock(now),
            )
            self._release(lease, now=now, reset_attempts=True)
            return JobWorkerResult(snapshot.job_id, stage.code, "waiting_approval")
        if checkpoint["status"] == "failed":
            self._fail_job(
                lease,
                code="OPENMONTAGE_PIPELINE_STAGE_FAILED",
                message="Pipeline stage reported a failed checkpoint",
                now=now,
            )
            return JobWorkerResult(snapshot.job_id, stage.code, "job_failed")

        progress = self._checkpoint_progress(checkpoint, stage.code)
        if progress is not None and stage.progress != progress:
            self.service.update_stage_progress(
                snapshot.job_id,
                stage.code,
                completed_units=progress["completedUnits"],
                total_units=progress["totalUnits"],
                label_code=progress["labelCode"],
                lease_token=lease.lease_token,
                lease_now=self._clock(now),
            )
        self._release(
            lease,
            now=now,
            retry_at=self._clock(now) + self.retry_delay,
            reset_attempts=True,
        )
        return JobWorkerResult(snapshot.job_id, stage.code, "stage_in_progress")

    def _execute_with_heartbeat(
        self,
        assignment: StageAssignment,
        lease: JobLease,
        *,
        now: datetime | None,
    ) -> tuple[PipelineExecutionResult, JobLease]:
        if now is not None:
            return self.executor.execute(assignment), lease

        stop = Event()
        state: dict[str, JobLease | BaseException] = {"lease": lease}

        def heartbeat() -> None:
            while not stop.wait(self.heartbeat_interval.total_seconds()):
                try:
                    current = state["lease"]
                    assert isinstance(current, JobLease)
                    state["lease"] = self.service.heartbeat_lease(
                        current,
                        lease_duration=self.lease_duration,
                    )
                except BaseException as exc:
                    state["error"] = exc
                    stop.set()

        thread = Thread(target=heartbeat, name=f"openmontage-heartbeat-{lease.job_id}", daemon=True)
        thread.start()
        try:
            result = self.executor.execute(assignment)
        finally:
            stop.set()
            thread.join(timeout=self.heartbeat_interval.total_seconds() + 1)
        if "error" in state:
            raise JobLeaseError("Job lease heartbeat failed") from state["error"]
        renewed = state["lease"]
        assert isinstance(renewed, JobLease)
        return result, renewed

    def _ensure_project(self, snapshot: JobSnapshot) -> None:
        project_dir = self.projects_dir / snapshot.job_id
        if (project_dir / PROJECT_MARKER_FILENAME).is_file():
            return
        title = snapshot.request.brief.get("title")
        init_project(
            snapshot.job_id,
            title=title if isinstance(title, str) and title.strip() else snapshot.job_id,
            pipeline_type=snapshot.workflow.name,
            pipeline_dir=self.projects_dir,
        )

    @staticmethod
    def _next_stage(snapshot: JobSnapshot) -> StageSnapshot | None:
        if snapshot.current_stage is not None:
            return JobWorker._stage(snapshot, snapshot.current_stage)
        return next((stage for stage in snapshot.stages if stage.status == StageStatus.PENDING), None)

    @staticmethod
    def _stage(snapshot: JobSnapshot, code: str) -> StageSnapshot:
        return next(stage for stage in snapshot.stages if stage.code == code)

    @staticmethod
    def _checkpoint_progress(checkpoint: dict, stage: str) -> dict | None:
        metadata = checkpoint.get("metadata")
        partial = metadata.get("partial_progress") if isinstance(metadata, dict) else None
        if not isinstance(partial, dict):
            return None
        completed = partial.get("completed_units")
        total = partial.get("total_units")
        label = partial.get("label_code", f"openmontage.stage.{stage}.progress")
        if (
            not isinstance(completed, int)
            or isinstance(completed, bool)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or total <= 0
            or completed < 0
            or completed > total
            or not isinstance(label, str)
            or not label.strip()
        ):
            return None
        return {
            "completedUnits": completed,
            "totalUnits": total,
            "labelCode": label,
        }

    def _fail_job(
        self,
        lease: JobLease,
        *,
        code: str,
        message: str,
        now: datetime | None,
    ) -> None:
        self.service.fail_job(
            lease.job_id,
            code=code,
            message=message,
            retryable=False,
            lease_token=lease.lease_token,
            lease_now=self._clock(now),
        )
        self._release(lease, now=now, reset_attempts=True)

    def _release(
        self,
        lease: JobLease,
        *,
        now: datetime | None,
        retry_at: datetime | None = None,
        error: str | None = None,
        reset_attempts: bool = False,
    ) -> None:
        self.service.release_lease(
            lease.job_id,
            lease_token=lease.lease_token,
            retry_at=retry_at,
            error=error,
            reset_attempts=reset_attempts,
            now=self._clock(now),
        )

    @staticmethod
    def _clock(fixed: datetime | None) -> datetime:
        return fixed or datetime.now(timezone.utc)
