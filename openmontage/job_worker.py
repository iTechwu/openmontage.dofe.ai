"""Durable Job Worker that reconciles external Agent checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Any

from lib.checkpoint import (
    CheckpointValidationError,
    PROJECT_MARKER_FILENAME,
    init_project,
    read_checkpoint,
)
from openmontage.artifact_bridge import ArtifactBridgeClient, ArtifactBridgeError
from openmontage.contracts import ApprovalStatus, JobSnapshot, JobStatus, StageSnapshot, StageStatus
from openmontage.job_service import JobLease, JobLeaseError, JobService
from openmontage.model_credential_bridge import (
    ModelCredentialBridgeClient,
    ModelCredentialBridgeError,
)
from openmontage.pipeline_executor import (
    PipelineExecutionError,
    PipelineExecutionResult,
    PipelineExecutor,
    StageAssignment,
)
from tools.dofe.delegation import DelegatedModelCredential


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class JobWorkerResult:
    job_id: str
    stage: str | None
    outcome: str


class FinalArtifactError(RuntimeError):
    """Raised when a completed pipeline has no safe final MP4."""


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
        artifact_bridge: ArtifactBridgeClient | None = None,
        model_credential_bridge: ModelCredentialBridgeClient | None = None,
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
        self.artifact_bridge = artifact_bridge
        self.model_credential_bridge = model_credential_bridge

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
            if not any(artifact.role == "final_video" for artifact in snapshot.artifacts):
                if self.artifact_bridge is None:
                    self._fail_job(
                        lease,
                        code="OPENMONTAGE_ARTIFACT_BRIDGE_UNAVAILABLE",
                        message="Artifact Bridge is required to publish the final video",
                        now=now,
                    )
                    return JobWorkerResult(snapshot.job_id, None, "job_failed")
                try:
                    final_video = self._resolve_final_video(snapshot)
                    published = self.artifact_bridge.upload_output(
                        job_id=snapshot.job_id,
                        attribution=snapshot.attribution,
                        path=final_video,
                        role="final_video",
                        media_type="video/mp4",
                    )
                    self.service.publish_artifact(
                        snapshot.job_id,
                        published,
                        lease_token=lease.lease_token,
                        lease_now=self._clock(now),
                    )
                except FinalArtifactError:
                    self._fail_job(
                        lease,
                        code="OPENMONTAGE_RENDER_FAILED",
                        message="Pipeline did not produce a safe final MP4",
                        now=now,
                    )
                    return JobWorkerResult(snapshot.job_id, None, "job_failed")
                except (ArtifactBridgeError, OSError):
                    if lease.attempt >= self.max_executor_attempts:
                        self._fail_job(
                            lease,
                            code="OPENMONTAGE_ARTIFACT_UPLOAD_FAILED",
                            message="Final video upload failed after bounded retries",
                            now=now,
                        )
                        return JobWorkerResult(snapshot.job_id, None, "job_failed")
                    self._release(
                        lease,
                        now=now,
                        retry_at=self._clock(now) + self.retry_delay,
                        error="Final video upload failed",
                    )
                    return JobWorkerResult(snapshot.job_id, None, "artifact_retry_scheduled")
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
        try:
            local_inputs = self._prepare_inputs(latest)
        except (ArtifactBridgeError, OSError, ValueError):
            self._fail_job(
                lease,
                code="OPENMONTAGE_ARTIFACT_INPUT_FAILED",
                message="Job input Artifact could not be prepared safely",
                now=now,
            )
            return JobWorkerResult(snapshot.job_id, stage.code, "job_failed")
        assignment = StageAssignment.from_job(
            latest,
            stage=stage.code,
            stage_attempt=self._stage(latest, stage.code).attempt,
            projects_dir=self.projects_dir,
            local_inputs=local_inputs,
        )
        credential: DelegatedModelCredential | None = None
        if self.model_credential_bridge is not None:
            try:
                credential = self.model_credential_bridge.issue(
                    job_id=latest.job_id,
                    stage=stage.code,
                    stage_attempt=self._stage(latest, stage.code).attempt,
                    attribution=latest.attribution,
                )
            except ModelCredentialBridgeError:
                if lease.attempt >= self.max_executor_attempts:
                    self._fail_job(
                        lease,
                        code="OPENMONTAGE_MODEL_CREDENTIAL_UNAVAILABLE",
                        message="Job-scoped model credential was unavailable after bounded retries",
                        now=now,
                    )
                    return JobWorkerResult(snapshot.job_id, stage.code, "job_failed")
                self._release(
                    lease,
                    now=now,
                    retry_at=self._clock(now) + self.retry_delay,
                    error="Job-scoped model credential was unavailable",
                )
                return JobWorkerResult(snapshot.job_id, stage.code, "credential_retry_scheduled")
        try:
            execution, lease = self._execute_with_heartbeat(
                assignment,
                lease,
                credential=credential,
                now=now,
            )
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
        credential: DelegatedModelCredential | None,
        now: datetime | None,
    ) -> tuple[PipelineExecutionResult, JobLease]:
        if now is not None:
            return self.executor.execute(assignment, credential=credential), lease

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
            result = self.executor.execute(assignment, credential=credential)
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

    def _prepare_inputs(self, snapshot: JobSnapshot) -> tuple[dict[str, Any], ...]:
        request_input = snapshot.request.input
        if request_input.get("type") != "artifact":
            return ()
        artifact_id = request_input.get("artifactId")
        if not isinstance(artifact_id, str) or not artifact_id.strip():
            raise ValueError("Artifact input requires artifactId")
        if self.artifact_bridge is None:
            raise ArtifactBridgeError("Artifact Bridge is required for Artifact input")

        project_dir = (self.projects_dir / snapshot.job_id).resolve()
        receipt_path = project_dir / ".openmontage" / "input-artifact.json"
        receipt = self._read_verified_input_receipt(
            receipt_path,
            project_dir=project_dir,
            artifact_id=artifact_id,
        )
        if receipt is not None:
            return (receipt,)

        downloaded = self.artifact_bridge.download_input(
            job_id=snapshot.job_id,
            attribution=snapshot.attribution,
            artifact_id=artifact_id,
            destination_dir=project_dir / "inputs",
        )
        resolved = downloaded.path.resolve()
        if not resolved.is_relative_to(project_dir):
            raise ArtifactBridgeError("Downloaded Artifact escaped the Job workspace")
        receipt = {
            "artifactId": downloaded.artifact.artifact_id,
            "path": str(resolved.relative_to(project_dir)),
            "fileName": downloaded.artifact.file_name,
            "mediaType": downloaded.artifact.media_type,
            "sizeBytes": downloaded.artifact.size_bytes,
            "sha256": downloaded.artifact.sha256,
        }
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_json(receipt_path, receipt)
        return (receipt,)

    @staticmethod
    def _read_verified_input_receipt(
        receipt_path: Path,
        *,
        project_dir: Path,
        artifact_id: str,
    ) -> dict[str, Any] | None:
        if not receipt_path.is_file():
            return None
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict) or receipt.get("artifactId") != artifact_id:
                return None
            relative_path = receipt.get("path")
            expected_size = receipt.get("sizeBytes")
            expected_sha256 = receipt.get("sha256")
            if (
                not isinstance(relative_path, str)
                or not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
                or expected_size < 1
                or not isinstance(expected_sha256, str)
            ):
                return None
            path = (project_dir / relative_path).resolve()
            if not path.is_relative_to(project_dir) or not path.is_file():
                return None
            size, sha256 = JobWorker._hash_file(path)
            if size != expected_size or sha256 != expected_sha256:
                return None
            return receipt
        except (json.JSONDecodeError, OSError, ValueError):
            return None

    def _resolve_final_video(self, snapshot: JobSnapshot) -> Path:
        project_dir = (self.projects_dir / snapshot.job_id).resolve()
        raw_paths: list[str] = []
        publish = read_checkpoint(self.projects_dir, snapshot.job_id, "publish")
        if publish is not None:
            publish_log = publish.get("artifacts", {}).get("publish_log")
            entries = publish_log.get("entries") if isinstance(publish_log, dict) else None
            if isinstance(entries, list):
                for entry in reversed(entries):
                    if isinstance(entry, dict) and entry.get("status") in {"exported", "published"}:
                        export_path = entry.get("export_path")
                        if isinstance(export_path, str):
                            raw_paths.append(export_path)
        compose = read_checkpoint(self.projects_dir, snapshot.job_id, "compose")
        if compose is not None:
            render_report = compose.get("artifacts", {}).get("render_report")
            outputs = render_report.get("outputs") if isinstance(render_report, dict) else None
            if isinstance(outputs, list):
                for output in outputs:
                    if isinstance(output, dict) and isinstance(output.get("path"), str):
                        raw_paths.append(output["path"])

        for raw_path in raw_paths:
            value = Path(raw_path).expanduser()
            candidates = [value] if value.is_absolute() else [project_dir / value, ROOT / value]
            for candidate in candidates:
                resolved = candidate.resolve()
                if (
                    resolved.is_relative_to(project_dir)
                    and resolved.is_file()
                    and resolved.suffix.lower() == ".mp4"
                ):
                    return resolved
        raise FinalArtifactError("No safe final MP4 was declared by the pipeline")

    @staticmethod
    def _hash_file(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)

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
