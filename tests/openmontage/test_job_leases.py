from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from threading import Barrier, Event
from time import sleep

import pytest

from openmontage.contracts import JobAttribution, JobCreateRequest
from openmontage.job_service import JobLeaseError, JobService


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


class WriteLockObservedJobService(JobService):
    def __init__(self, database_path: str | Path):
        self.begin_write_attempted = Event()
        super().__init__(database_path)

    def _begin_write(self, connection: sqlite3.Connection) -> None:
        self.begin_write_attempted.set()
        super()._begin_write(connection)


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

    with pytest.raises(JobLeaseError, match="lease token"):
        service.start_stage(
            old.job_id,
            "research",
            lease_token=old.lease_token,
            lease_now=NOW + timedelta(seconds=7),
        )
    assert service.get_job(old.job_id).last_sequence == 1


@pytest.mark.parametrize(
    "operation",
    ["heartbeat", "release", "retry_settlement", "terminal_settlement"],
)
def test_lease_mutation_rechecks_default_clock_after_waiting_for_write_lock(
    tmp_path: Path,
    operation: str,
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    service = WriteLockObservedJobService(database_path)
    job = service.create_job(_request(operation), _attribution())
    lease = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(milliseconds=150),
    )
    assert lease is not None
    snapshot_before = service.get_job(job.job_id)
    with sqlite3.connect(database_path) as connection:
        lease_before = connection.execute(
            """
            SELECT worker_id, lease_token, lease_expires_at, next_attempt_at,
                   attempts, last_error
            FROM openmontage_job_execution
            WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()

    blocker = sqlite3.connect(database_path)
    blocker.execute("BEGIN IMMEDIATE")
    service.begin_write_attempted.clear()

    def mutate_lease():
        if operation == "heartbeat":
            return service.heartbeat_lease(
                lease,
                lease_duration=timedelta(seconds=1),
            )
        if operation == "release":
            return service.release_lease(
                lease.job_id,
                lease_token=lease.lease_token,
            )
        if operation == "retry_settlement":
            return service.release_lease_or_confirm_cancel(
                lease.job_id,
                lease_token=lease.lease_token,
                retry_at=datetime.now(timezone.utc) + timedelta(seconds=5),
                error="executor failed",
            )
        return service.fail_job_or_confirm_cancel(
            lease.job_id,
            code="OPENMONTAGE_AGENT_EXECUTOR_FAILED",
            message="executor failed",
            retryable=False,
            lease_token=lease.lease_token,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(mutate_lease)
        assert service.begin_write_attempted.wait(timeout=1)
        remaining = (lease.expires_at - datetime.now(timezone.utc)).total_seconds()
        sleep(max(remaining, 0) + 0.05)
        blocker.commit()
        with pytest.raises(JobLeaseError, match="expired"):
            future.result(timeout=2)
    blocker.close()

    assert service.get_job(job.job_id) == snapshot_before
    with sqlite3.connect(database_path) as connection:
        lease_after = connection.execute(
            """
            SELECT worker_id, lease_token, lease_expires_at, next_attempt_at,
                   attempts, last_error
            FROM openmontage_job_execution
            WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()
    assert lease_after == lease_before


def test_claim_lease_starts_after_waiting_for_write_lock(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    service = WriteLockObservedJobService(database_path)
    service.create_job(_request("claim-after-lock"), _attribution())
    blocker = sqlite3.connect(database_path)
    blocker.execute("BEGIN IMMEDIATE")
    service.begin_write_attempted.clear()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            service.claim_job,
            worker_id="worker-a",
            lease_duration=timedelta(milliseconds=150),
        )
        assert service.begin_write_attempted.wait(timeout=1)
        sleep(0.2)
        blocker.commit()
        lock_released_at = datetime.now(timezone.utc)
        lease = future.result(timeout=2)
    blocker.close()

    assert lease is not None
    assert lease.expires_at > lock_released_at


@pytest.mark.parametrize("operation", ["release", "retry_settlement"])
def test_release_accepts_retry_becoming_due_while_waiting_for_write_lock(
    tmp_path: Path,
    operation: str,
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    service = WriteLockObservedJobService(database_path)
    service.create_job(_request(f"due-during-lock-{operation}"), _attribution())
    lease = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=3),
    )
    assert lease is not None
    blocker = sqlite3.connect(database_path)
    blocker.execute("BEGIN IMMEDIATE")
    service.begin_write_attempted.clear()
    retry_at = datetime.now(timezone.utc) + timedelta(milliseconds=500)

    def release_for_retry():
        if operation == "release":
            return service.release_lease(
                lease.job_id,
                lease_token=lease.lease_token,
                retry_at=retry_at,
                error="executor failed",
            )
        return service.release_lease_or_confirm_cancel(
            lease.job_id,
            lease_token=lease.lease_token,
            retry_at=retry_at,
            error="executor failed",
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(release_for_retry)
        assert service.begin_write_attempted.wait(timeout=1)
        remaining = (retry_at - datetime.now(timezone.utc)).total_seconds()
        sleep(max(remaining, 0) + 0.05)
        blocker.commit()
        future.result(timeout=2)
    blocker.close()

    reclaimed = service.claim_job(
        worker_id="worker-b",
        lease_duration=timedelta(seconds=30),
    )
    assert reclaimed is not None
    assert reclaimed.job_id == lease.job_id


@pytest.mark.parametrize("operation", ["release", "retry_settlement"])
def test_relative_retry_delay_starts_after_acquiring_write_lock(
    tmp_path: Path,
    operation: str,
) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    service = WriteLockObservedJobService(database_path)
    service.create_job(_request(f"relative-delay-{operation}"), _attribution())
    lease = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=3),
    )
    assert lease is not None
    blocker = sqlite3.connect(database_path)
    blocker.execute("BEGIN IMMEDIATE")
    service.begin_write_attempted.clear()

    def release_for_retry():
        kwargs = {
            "lease_token": lease.lease_token,
            "retry_delay": timedelta(milliseconds=150),
        }
        if operation == "release":
            return service.release_lease(lease.job_id, **kwargs)
        return service.release_lease_or_confirm_cancel(lease.job_id, **kwargs)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(release_for_retry)
        assert service.begin_write_attempted.wait(timeout=1)
        sleep(0.2)
        blocker.commit()
        future.result(timeout=2)
    blocker.close()

    assert service.claim_job(
        worker_id="worker-b",
        lease_duration=timedelta(seconds=30),
    ) is None
    sleep(0.2)
    reclaimed = service.claim_job(
        worker_id="worker-b",
        lease_duration=timedelta(seconds=30),
    )
    assert reclaimed is not None and reclaimed.job_id == lease.job_id


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
