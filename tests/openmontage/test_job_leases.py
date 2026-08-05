from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from openmontage.contracts import JobAttribution, JobCreateRequest
from openmontage.job_service import JobLeaseError, JobService


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _attribution() -> JobAttribution:
    return JobAttribution(
        workspace_id="ws-lease",
        employee_id="employee-lease",
        runtime_id="runtime-lease",
        root_task_id="task-lease",
        conversation_id="conversation-lease",
        source_invocation_id="invocation-lease",
        trace_id="trace-lease",
    )


def _request(request_id: str = "lease-request") -> JobCreateRequest:
    return JobCreateRequest(
        client_request_id=request_id,
        workflow="animated-explainer",
        input={"type": "text", "inlineText": "Explain durable workers"},
        brief={"title": "Durable workers", "durationSeconds": 30},
        output={"container": "mp4", "resolution": "1080x1920"},
        budget={"maxAmount": "20.00", "currency": "CNY"},
    )


def test_concurrent_claim_has_one_winner(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    service = JobService(database_path)
    job = service.create_job(_request(), _attribution())
    barrier = Barrier(4)

    def claim(worker_number: int):
        barrier.wait()
        return JobService(database_path).claim_job(
            worker_id=f"worker-{worker_number}",
            lease_duration=timedelta(seconds=30),
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(claim, range(4)))

    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].job_id == job.job_id
    assert winners[0].attempt == 1


def test_heartbeat_release_and_reclaim_are_fenced_by_token(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    lease = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    assert lease is not None

    renewed = service.heartbeat_lease(
        lease,
        lease_duration=timedelta(seconds=45),
        now=NOW + timedelta(seconds=10),
    )
    assert renewed.expires_at == NOW + timedelta(seconds=55)

    with pytest.raises(JobLeaseError, match="lease token"):
        service.release_lease(
            job.job_id,
            lease_token="stale-token",
            now=NOW + timedelta(seconds=11),
        )

    service.release_lease(
        job.job_id,
        lease_token=renewed.lease_token,
        now=NOW + timedelta(seconds=11),
    )
    reclaimed = service.claim_job(
        worker_id="worker-b",
        lease_duration=timedelta(seconds=30),
        now=NOW + timedelta(seconds=12),
    )
    assert reclaimed is not None
    assert reclaimed.attempt == 2
    assert reclaimed.lease_token != lease.lease_token


def test_expired_lease_is_recovered_and_old_worker_is_fenced(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    service.create_job(_request(), _attribution())
    old = service.claim_job(
        worker_id="worker-old",
        lease_duration=timedelta(seconds=5),
        now=NOW,
    )
    assert old is not None

    recovered = service.claim_job(
        worker_id="worker-new",
        lease_duration=timedelta(seconds=30),
        now=NOW + timedelta(seconds=6),
    )
    assert recovered is not None
    assert recovered.job_id == old.job_id
    assert recovered.attempt == 2

    with pytest.raises(JobLeaseError, match="lease token"):
        service.heartbeat_lease(
            old,
            lease_duration=timedelta(seconds=30),
            now=NOW + timedelta(seconds=7),
        )


def test_retry_deferral_prevents_early_reclaim(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    service.create_job(_request(), _attribution())
    lease = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    assert lease is not None

    retry_at = NOW + timedelta(minutes=2)
    service.release_lease(
        lease.job_id,
        lease_token=lease.lease_token,
        retry_at=retry_at,
        error="temporary executor failure",
        now=NOW + timedelta(seconds=1),
    )

    assert service.claim_job(
        worker_id="worker-b",
        lease_duration=timedelta(seconds=30),
        now=retry_at - timedelta(microseconds=1),
    ) is None
    assert service.claim_job(
        worker_id="worker-b",
        lease_duration=timedelta(seconds=30),
        now=retry_at,
    ) is not None


def test_waiting_approval_and_terminal_jobs_are_not_claimed(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    waiting = service.create_job(_request("waiting"), _attribution())
    service.start_stage(waiting.job_id, "research")
    service.complete_stage(waiting.job_id, "research")
    service.start_stage(waiting.job_id, "proposal")
    service.request_stage_approval(waiting.job_id, "proposal", reason="Approve proposal")

    failed = service.create_job(_request("failed"), _attribution())
    service.fail_job(
        failed.job_id,
        code="OPENMONTAGE_RENDER_FAILED",
        message="Render failed",
        retryable=False,
    )

    assert service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    ) is None
