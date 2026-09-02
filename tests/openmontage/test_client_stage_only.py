"""Tests for CI-only deployment mode (plan §13, §17 阶段五)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from openmontage.contracts import (
    JobAttribution,
    JobCreateRequest,
    JobStatus,
    PublishedArtifact,
    StageStatus,
)
from openmontage.job_service import JobService, client_stage_only_enabled


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


def _request() -> JobCreateRequest:
    return JobCreateRequest(
        client_request_id="request-1",
        workflow="animated-explainer",
        input={"type": "text", "inlineText": "Explain the product"},
        brief={"title": "Product video", "durationSeconds": 30},
        output={"container": "mp4", "resolution": "1080x1920"},
        budget={"maxAmount": "20.00", "currency": "CNY"},
    )


def _service(tmp_path: Path) -> JobService:
    return JobService(tmp_path / "jobs.sqlite3", projects_dir=tmp_path / "projects")


# --- config parsing -----------------------------------------------------------


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on"])
def test_client_stage_only_enabled_true_values(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENMONTAGE_CLIENT_STAGE_ONLY", raw)
    assert client_stage_only_enabled() is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "", "off"])
def test_client_stage_only_disabled_values(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENMONTAGE_CLIENT_STAGE_ONLY", raw)
    assert client_stage_only_enabled() is False


def test_client_stage_only_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENMONTAGE_CLIENT_STAGE_ONLY", raising=False)
    assert client_stage_only_enabled() is False


def test_client_stage_only_trims_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENMONTAGE_CLIENT_STAGE_ONLY", "  true  ")
    assert client_stage_only_enabled() is True


def test_client_stage_only_unknown_value_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENMONTAGE_CLIENT_STAGE_ONLY", "maybe")
    assert client_stage_only_enabled() is False


# --- worker is fenced out ------------------------------------------------------


def test_claim_job_is_fenced_out_in_client_stage_only_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENMONTAGE_AGENT_EXECUTOR_JSON", raising=False)
    monkeypatch.setenv("OPENMONTAGE_CLIENT_STAGE_ONLY", "true")
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())

    lease = service.claim_job(worker_id="legacy-worker", lease_duration=timedelta(minutes=1))
    assert lease is None  # the legacy Worker sees no claimable work

    # The Job stays QUEUED — no stage was advanced by the Worker.
    assert service.get_job(job.job_id).status == JobStatus.QUEUED


def test_claim_job_works_when_client_stage_only_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENMONTAGE_AGENT_EXECUTOR_JSON", raising=False)
    monkeypatch.setenv("OPENMONTAGE_CLIENT_STAGE_ONLY", "false")
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())

    lease = service.claim_job(worker_id="legacy-worker", lease_duration=timedelta(minutes=1))
    assert lease is not None
    assert lease.job_id == job.job_id


# --- client path still advances the Job ----------------------------------------


def test_client_stage_api_still_advances_in_client_stage_only_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENMONTAGE_AGENT_EXECUTOR_JSON", raising=False)
    monkeypatch.setenv("OPENMONTAGE_CLIENT_STAGE_ONLY", "true")
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())

    # Job is QUEUED until the client begins its first stage.
    assert service.get_job(job.job_id).status == JobStatus.QUEUED

    lease = service.begin_client_stage(job.job_id, "research", idempotency_key="b-1")
    assert service.get_job(job.job_id).status == JobStatus.RUNNING
    assert service.get_job(job.job_id).stages[0].status == StageStatus.RUNNING

    service.submit_client_stage(
        job.job_id, "research", stage_attempt=lease.stage_attempt, status="in_progress",
        lease_token=lease.lease_token, idempotency_key="s-1",
    )
    # Still RUNNING after an in_progress heartbeat.
    assert service.get_job(job.job_id).status == JobStatus.RUNNING


# --- worker CLI refuses to start ------------------------------------------------


def test_worker_cli_refuses_to_start_in_client_stage_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENMONTAGE_CLIENT_STAGE_ONLY", "true")
    import argparse

    from openmontage.cli import _build_job_worker

    args = argparse.Namespace(
        lease_seconds=120, heartbeat_seconds=30, retry_seconds=15, max_attempts=3
    )
    with pytest.raises(RuntimeError, match="OPENMONTAGE_CLIENT_STAGE_ONLY"):
        _build_job_worker(args)


# --- capability surface reports the mode ---------------------------------------


def test_job_submission_capability_reports_client_stage_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENMONTAGE_CLIENT_STAGE_ONLY", "true")
    from openmontage.capabilities import job_submission_capability

    capability = job_submission_capability()
    assert capability["client_stage_only"] is True


def test_job_submission_capability_reports_client_stage_only_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENMONTAGE_CLIENT_STAGE_ONLY", "false")
    from openmontage.capabilities import job_submission_capability

    assert job_submission_capability()["client_stage_only"] is False


def test_legacy_settle_methods_refuse_in_client_stage_only_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENMONTAGE_AGENT_EXECUTOR_JSON", raising=False)
    monkeypatch.setenv("OPENMONTAGE_CLIENT_STAGE_ONLY", "true")
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())

    with pytest.raises(Exception, match="client-stage-only"):
        service.start_stage(job.job_id, "research")
    with pytest.raises(Exception, match="client-stage-only"):
        service.complete_stage(job.job_id, "research")


def test_all_legacy_mutation_entry_points_refuse_in_client_stage_only_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every legacy Worker mutation entry point is fenced off, not just
    claim_job/start_stage/complete_stage (review P1)."""
    monkeypatch.delenv("OPENMONTAGE_AGENT_EXECUTOR_JSON", raising=False)
    monkeypatch.setenv("OPENMONTAGE_CLIENT_STAGE_ONLY", "true")
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())

    mutations = [
        lambda: service.heartbeat_lease(
            __import__("openmontage.job_service", fromlist=["JobLease"]).JobLease(
                job_id=job.job_id, worker_id="w", lease_token="t",
                expires_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                attempt=1, snapshot=service.get_job(job.job_id),
            ), lease_duration=timedelta(minutes=1),
        ),
        lambda: service.release_lease(job.job_id, lease_token="t"),
        lambda: service.release_lease_or_confirm_cancel(job.job_id, lease_token="t"),
        lambda: service.start_stage_or_confirm_cancel(job.job_id, "research", lease_token="t"),
        lambda: service.complete_stage_or_confirm_cancel(job.job_id, "research", lease_token="t"),
        lambda: service.update_stage_progress(
            job.job_id, "research", completed_units=1, total_units=1, label_code="x",
        ),
        lambda: service.request_stage_approval(job.job_id, "research", reason="r"),
        lambda: service.request_stage_approval_or_confirm_cancel(
            job.job_id, "research", reason="r", lease_token="t",
        ),
        lambda: service.fail_job(job.job_id, code="c", message="m", retryable=False),
        lambda: service.fail_job_or_confirm_cancel(
            job.job_id, code="c", message="m", retryable=False, lease_token="t",
        ),
        lambda: service.confirm_cancel(job.job_id),
        lambda: service.complete_job(job.job_id),
        lambda: service.complete_job_or_confirm_cancel(
            job.job_id, artifact=None, lease_token="t",
        ),
        lambda: service.publish_artifact(
            job.job_id,
            PublishedArtifact(
                job_id=job.job_id,
                employee_artifact_id="eart-1",
                employee_id="employee-1",
                role="final_video",
                file_name="final.mp4",
                media_type="video/mp4",
                size_bytes=5,
                sha256="a" * 64,
                published_at="2026-09-01T00:00:00Z",
            ),
        ),
    ]

    for mutation in mutations:
        with pytest.raises(Exception, match="client-stage-only"):
            mutation()

    # The Job was not advanced by any of the fenced mutations.
    assert service.get_job(job.job_id).status == JobStatus.QUEUED
