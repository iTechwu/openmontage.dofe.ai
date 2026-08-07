from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from threading import Barrier, Event
from time import sleep

import pytest

from openmontage.contracts import JobAttribution, JobCreateRequest, JobStatus
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
    ["release", "retry_settlement", "terminal_settlement"],
)
def test_settlement_mutation_by_owner_succeeds_after_lease_lapses_during_write_lock_wait(
    tmp_path: Path,
    operation: str,
) -> None:
    """An atomic settlement survives a lease lapse caused by write-lock
    contention.

    A settle and any concurrent writer both take ``BEGIN IMMEDIATE``. When
    another write queues the settle, the lease may lapse before the settle
    acquires the lock. Settlement uses the fencing token (ownership is proven by
    the token alone; expiry is only a claim-eligibility signal), so the same
    owner's settle must still apply — otherwise the publish-once guarantee
    degrades into retries and duplicate work. Only a newer token (a successful
    reclaim by another worker) fences a settle; a worker is never fenced by its
    own lapsed lease.
    """
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
        connection.row_factory = sqlite3.Row
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

    def settle_lease():
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
        future = executor.submit(settle_lease)
        assert service.begin_write_attempted.wait(timeout=1)
        remaining = (lease.expires_at - datetime.now(timezone.utc)).total_seconds()
        # Let the lease lapse while the settle waits for the write lock.
        sleep(max(remaining, 0) + 0.05)
        blocker.commit()
        # The owner's settle applies despite the lapsed lease — reaching
        # result() without raising is itself the proof; release returns None.
        future.result(timeout=2)
    blocker.close()

    snapshot_after = service.get_job(job.job_id)
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        lease_after = connection.execute(
            """
            SELECT worker_id, lease_token, lease_expires_at, next_attempt_at,
                   attempts, last_error
            FROM openmontage_job_execution
            WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()

    if operation == "release":
        assert snapshot_after == snapshot_before
        assert lease_after["lease_token"] is None
        assert lease_after["next_attempt_at"] is None
    elif operation == "retry_settlement":
        assert snapshot_after == snapshot_before
        assert lease_after["lease_token"] is None
        assert lease_after["next_attempt_at"] is not None
        assert lease_after["last_error"] == "executor failed"
    else:  # terminal_settlement
        assert snapshot_after.status == JobStatus.FAILED
        assert snapshot_after.last_sequence > snapshot_before.last_sequence
        assert lease_after["lease_token"] is None


@pytest.mark.parametrize("operation", ["heartbeat", "start"])
def test_live_mutation_fails_after_lease_lapses_during_write_lock_wait(
    tmp_path: Path,
    operation: str,
) -> None:
    """Live mutations (heartbeat, start) require a genuinely live lease.

    These are fenced by expiry, not only by a newer token: a worker whose lease
    lapsed while waiting for the write lock may be reaped at any moment, so
    letting it renew or start a stage (which begins paid execution) would reopen
    the double-Worker window the active/fencing split closes. The owning worker
    treats this the same as a reclaim-induced heartbeat loss and aborts the unit.
    """
    database_path = tmp_path / "jobs.sqlite3"
    service = WriteLockObservedJobService(database_path)
    service.create_job(_request(operation), _attribution())
    lease = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(milliseconds=150),
    )
    assert lease is not None

    blocker = sqlite3.connect(database_path)
    blocker.execute("BEGIN IMMEDIATE")
    service.begin_write_attempted.clear()

    def mutate_live():
        if operation == "heartbeat":
            return service.heartbeat_lease(lease, lease_duration=timedelta(seconds=1))
        return service.start_stage_or_confirm_cancel(
            lease.job_id,
            "research",
            lease_token=lease.lease_token,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(mutate_live)
        assert service.begin_write_attempted.wait(timeout=1)
        remaining = (lease.expires_at - datetime.now(timezone.utc)).total_seconds()
        sleep(max(remaining, 0) + 0.05)
        blocker.commit()
        with pytest.raises(JobLeaseError, match="expired"):
            future.result(timeout=2)
    blocker.close()


def test_expired_unreclaimed_lease_rejects_live_mutation_but_allows_settlement(
    tmp_path: Path,
) -> None:
    """An expired lease that has NOT been reaped: live mutations are rejected
    (active lease required), but atomic settlement still succeeds (the fencing
    token only checks that no newer claim took over). This is the precise
    active/fencing contract that closes the double-Worker execution window.
    """
    service = JobService(tmp_path / "jobs.sqlite3")
    service.create_job(_request("contract"), _attribution())
    lease = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    assert lease is not None
    # Lease (NOW + 30s) has lapsed; no other worker has reclaimed it.
    expired_now = NOW + timedelta(seconds=31)

    with pytest.raises(JobLeaseError, match="expired"):
        service.heartbeat_lease(
            lease,
            lease_duration=timedelta(seconds=30),
            now=expired_now,
        )
    with pytest.raises(JobLeaseError, match="expired"):
        service.start_stage_or_confirm_cancel(
            lease.job_id,
            "research",
            lease_token=lease.lease_token,
            now=expired_now,
        )
    # No mutation was applied — only the JOB_CREATED event remains.
    assert service.get_job(lease.job_id).last_sequence == 1

    # Atomic settlement still succeeds: the fencing token is current (no newer
    # claim), so the lapsed owner may release the lease for recovery.
    settled = service.release_lease_or_confirm_cancel(
        lease.job_id,
        lease_token=lease.lease_token,
        reset_attempts=True,
        now=expired_now,
    )
    assert settled.status != JobStatus.CANCELLED


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


def _corrupt_lease_expiry(database_path: Path, job_id: str, value: object) -> None:
    """Simulate a corrupted lease row: token present but expiry missing/illegal."""
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE openmontage_job_execution SET lease_expires_at = ? WHERE job_id = ?",
            (value, job_id),
        )


def test_corrupted_null_expiry_record_is_rejected_and_reclaimable(
    tmp_path: Path,
) -> None:
    """A lease row whose token is set but expiry is NULL cannot prove liveness,
    so the active-lease check fails closed and claim_job reclaims the record.
    This closes the fail-open gap where a stale owner kept writing while no new
    Worker could take over."""
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request("corrupt-null"), _attribution())
    lease = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    assert lease is not None
    _corrupt_lease_expiry(service.database_path, job.job_id, None)

    # Active check rejects: token matches, but no provable expiry (fail-closed).
    with pytest.raises(JobLeaseError, match="no valid expiry"):
        service.complete_stage(
            job.job_id,
            "research",
            lease_token=lease.lease_token,
            lease_now=NOW,
        )

    # The corrupted record is reclaimable: a new Worker takes over.
    reclaimed = service.claim_job(
        worker_id="worker-b",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    assert reclaimed is not None
    assert reclaimed.job_id == job.job_id
    assert reclaimed.lease_token != lease.lease_token


def test_corrupted_illegal_expiry_record_is_rejected_and_reclaimable(
    tmp_path: Path,
) -> None:
    """An unparseable expiry string parses to None, so the same fail-closed +
    reclaimable contract applies as for a NULL expiry."""
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request("corrupt-illegal"), _attribution())
    lease = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    assert lease is not None
    _corrupt_lease_expiry(service.database_path, job.job_id, "not-a-timestamp")

    with pytest.raises(JobLeaseError, match="no valid expiry"):
        service.complete_stage(
            job.job_id,
            "research",
            lease_token=lease.lease_token,
            lease_now=NOW,
        )

    reclaimed = service.claim_job(
        worker_id="worker-b",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    assert reclaimed is not None
    assert reclaimed.job_id == job.job_id


@pytest.mark.parametrize(
    "corrupt_value",
    [
        pytest.param(b"bad", id="blob"),
        pytest.param(12345, id="integer"),
        pytest.param(12.5, id="float"),
    ],
)
def test_corrupted_non_string_expiry_record_is_rejected_and_reclaimable(
    tmp_path: Path,
    corrupt_value: object,
) -> None:
    """A non-STRICT SQLite column can hold a BLOB or number instead of text.

    datetime.fromisoformat() raises TypeError (not ValueError) on those types,
    so the parser must fail closed to None and the record must stay reclaimable,
    rather than crashing the active-lease check or the Worker claim loop.
    """
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request("corrupt-nonstring"), _attribution())
    lease = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    assert lease is not None
    _corrupt_lease_expiry(service.database_path, job.job_id, corrupt_value)

    with pytest.raises(JobLeaseError, match="no valid expiry"):
        service.complete_stage(
            job.job_id,
            "research",
            lease_token=lease.lease_token,
            lease_now=NOW,
        )

    reclaimed = service.claim_job(
        worker_id="worker-b",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    assert reclaimed is not None
    assert reclaimed.job_id == job.job_id


@pytest.mark.parametrize(
    "stored_expiry, still_active",
    [
        # 12:00:01Z written as a +05:30 offset — strictly after NOW (12:00:00Z).
        pytest.param("2026-08-05T17:30:01+05:30", True, id="future-offset-active"),
        # 01:00:00Z written as a +05:30 offset — before NOW.
        pytest.param("2026-08-05T06:30:00+05:30", False, id="past-offset-expired"),
    ],
)
def test_offset_aware_expiry_compared_by_instant_for_reclaim(
    tmp_path: Path,
    stored_expiry: str,
    still_active: bool,
) -> None:
    """An offset-aware expiry is compared by absolute instant, so a non-UTC zone
    never flips a future expiry into a reclaimable record or vice versa.

    Exercises the claim/reclaim path (which parses expiry in Python); paired
    with the non-string tests above it covers the abnormal-timezone case the
    parser must not mishandle.
    """
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(f"offset-{still_active}"), _attribution())
    first = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    assert first is not None
    _corrupt_lease_expiry(service.database_path, job.job_id, stored_expiry)

    reclaimed = service.claim_job(
        worker_id="worker-b",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    if still_active:
        assert reclaimed is None  # token still live → not reclaimable
    else:
        assert reclaimed is not None
        assert reclaimed.job_id == job.job_id


def test_expired_token_rejected_on_legacy_public_api_but_settlement_succeeds(
    tmp_path: Path,
) -> None:
    """Legacy public mutation APIs require an ACTIVE lease: an expired token is
    rejected. The atomic settlement path stays fencing-only, so the same expired
    token can still settle. This is the active/fencing split for the public
    surface — previously these legacy APIs were fencing-only and let an expired
    token complete a stage or job."""
    service = JobService(tmp_path / "jobs.sqlite3")
    service.create_job(_request("legacy-active"), _attribution())
    lease = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    assert lease is not None
    expired_now = NOW + timedelta(seconds=31)

    # Legacy public API (complete_stage) now enforces an active lease.
    with pytest.raises(JobLeaseError, match="expired"):
        service.complete_stage(
            lease.job_id,
            "research",
            lease_token=lease.lease_token,
            lease_now=expired_now,
        )

    # Settlement stays fencing-only: the expired-but-current token still settles.
    settled = service.release_lease_or_confirm_cancel(
        lease.job_id,
        lease_token=lease.lease_token,
        reset_attempts=True,
        now=expired_now,
    )
    assert settled.status != JobStatus.CANCELLED


NAIVE_NOW = datetime(2026, 8, 5, 12, 5)  # no tzinfo — would TypeError vs aware expiry


def test_claim_rejects_naive_now(tmp_path: Path) -> None:
    """claim_job compares ``now`` against the parsed aware-UTC expiry in the
    reclaim loop; a naive ``now`` would raise TypeError there and crash the
    Worker claim loop. It is rejected at entry as a JobLeaseError instead."""
    service = JobService(tmp_path / "jobs.sqlite3")
    service.create_job(_request("naive-claim"), _attribution())
    with pytest.raises(JobLeaseError, match="timezone-aware"):
        service.claim_job(
            worker_id="worker-a",
            lease_duration=timedelta(seconds=30),
            now=NAIVE_NOW,
        )


def test_active_lease_check_rejects_naive_now(tmp_path: Path) -> None:
    """The active-lease gate compares the parsed aware expiry against ``now``;
    a naive ``now`` is rejected as a JobLeaseError before that comparison,
    on both the direct public entry and the lease_now mutation path."""
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request("naive-active"), _attribution())
    lease = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    assert lease is not None

    with pytest.raises(JobLeaseError, match="timezone-aware"):
        service.require_active_lease(
            job.job_id, lease_token=lease.lease_token, now=NAIVE_NOW
        )
    with pytest.raises(JobLeaseError, match="timezone-aware"):
        service.complete_stage(
            job.job_id,
            "research",
            lease_token=lease.lease_token,
            lease_now=NAIVE_NOW,
        )


def test_heartbeat_rejects_naive_now(tmp_path: Path) -> None:
    """heartbeat_lease routes through the active-lease gate, so a naive ``now``
    is rejected rather than TypeError-ing at the expiry comparison."""
    service = JobService(tmp_path / "jobs.sqlite3")
    service.create_job(_request("naive-heartbeat"), _attribution())
    lease = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    assert lease is not None
    with pytest.raises(JobLeaseError, match="timezone-aware"):
        service.heartbeat_lease(
            lease,
            lease_duration=timedelta(seconds=30),
            now=NAIVE_NOW,
        )


@pytest.mark.parametrize(
    "settle",
    [
        pytest.param(
            lambda service, lease, naive: service.release_lease(
                lease.job_id, lease_token=lease.lease_token, now=naive
            ),
            id="release",
        ),
        pytest.param(
            lambda service, lease, naive: service.release_lease_or_confirm_cancel(
                lease.job_id, lease_token=lease.lease_token, now=naive
            ),
            id="release-or-confirm",
        ),
        pytest.param(
            lambda service, lease, naive: service.complete_stage_or_confirm_cancel(
                lease.job_id,
                "research",
                lease_token=lease.lease_token,
                now=naive,
            ),
            id="complete-stage-or-confirm",
        ),
        pytest.param(
            lambda service, lease, naive: service.fail_job_or_confirm_cancel(
                lease.job_id,
                code="OPENMONTAGE_AGENT_EXECUTOR_FAILED",
                message="executor failed",
                retryable=False,
                lease_token=lease.lease_token,
                now=naive,
            ),
            id="fail-job-or-confirm",
        ),
    ],
)
def test_settlement_rejects_naive_now(tmp_path: Path, settle) -> None:
    """Every atomic settlement path (release, release-or-confirm,
    complete-stage-or-confirm, fail-job-or-confirm) routes through the fencing
    gate — or normalizes ``now`` at its own entry — so a naive clock is rejected
    as a JobLeaseError consistently, never a TypeError leaking from the
    expiry/token comparison."""
    service = JobService(tmp_path / "jobs.sqlite3")
    service.create_job(_request("naive-settle"), _attribution())
    lease = service.claim_job(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    assert lease is not None
    with pytest.raises(JobLeaseError, match="timezone-aware"):
        settle(service, lease, NAIVE_NOW)


_OFFSET_TZ = timezone(timedelta(hours=5, minutes=30))  # +05:30 == the IST-style case


def test_offset_now_compared_by_instant_in_sql_retry_window(tmp_path: Path) -> None:
    """A non-UTC offset ``now`` must be normalized to UTC before the lexical SQL
    comparison ``next_attempt_at <= ?``, otherwise the same instant compares
    inconsistently: 17:31:59+05:30 == 12:01:59Z, but as raw ISO strings
    '17:31...' > '12:02...' so a retry due at 12:02Z would look already-due and
    be claimed ~5.5 hours early."""
    service = JobService(tmp_path / "jobs.sqlite3")
    service.create_job(_request("offset-now"), _attribution())
    lease = service.claim_job(
        worker_id="worker-a", lease_duration=timedelta(seconds=30), now=NOW
    )
    assert lease is not None

    retry_at = NOW + timedelta(minutes=2)  # 12:02Z, persisted as UTC ISO
    service.release_lease(
        lease.job_id,
        lease_token=lease.lease_token,
        retry_at=retry_at,
        error="temporary failure",
        now=NOW,
    )

    due = service.claim_job(
        worker_id="worker-b",
        lease_duration=timedelta(seconds=30),
        # 17:31:59+05:30 == 12:01:59Z — one second before the retry is due.
        now=datetime(2026, 8, 5, 17, 31, 59, tzinfo=_OFFSET_TZ),
    )
    assert due is None, "offset now one second before due must NOT claim"

    reclaimed = service.claim_job(
        worker_id="worker-c",
        lease_duration=timedelta(seconds=30),
        # 17:32:00+05:30 == 12:02:00Z — exactly when the retry is due.
        now=datetime(2026, 8, 5, 17, 32, 0, tzinfo=_OFFSET_TZ),
    )
    assert reclaimed is not None
    assert reclaimed.job_id == lease.job_id


def test_naive_retry_at_is_rejected(tmp_path: Path) -> None:
    """retry_at is compared against the aware clock and persisted into the SQL
    retry window, so it must be normalized too: a naive retry_at is rejected
    rather than TypeError-ing at the comparison or storing a non-UTC string."""
    service = JobService(tmp_path / "jobs.sqlite3")
    service.create_job(_request("naive-retry-at"), _attribution())
    lease = service.claim_job(
        worker_id="worker-a", lease_duration=timedelta(seconds=30), now=NOW
    )
    assert lease is not None
    with pytest.raises(JobLeaseError, match="timezone-aware"):
        service.release_lease(
            lease.job_id,
            lease_token=lease.lease_token,
            retry_at=datetime(2026, 8, 5, 12, 5),  # naive
            error="temporary failure",
            now=NOW,
        )


def test_heartbeat_converts_offset_now_to_utc_before_persisting(tmp_path: Path) -> None:
    """heartbeat_lease must convert an offset ``now`` to UTC before persisting
    ``lease_expires_at``, else the raw offset ISO (e.g. 2026-08-05T17:32:00+05:30)
    is stored and later lexical comparisons against UTC strings misorder it."""
    service = JobService(tmp_path / "jobs.sqlite3")
    service.create_job(_request("heartbeat-offset"), _attribution())
    lease = service.claim_job(
        worker_id="worker-a", lease_duration=timedelta(minutes=5), now=NOW
    )
    assert lease is not None
    offset_now = datetime(2026, 8, 5, 17, 31, 0, tzinfo=_OFFSET_TZ)  # == 12:01:00Z
    renewed = service.heartbeat_lease(
        lease, lease_duration=timedelta(seconds=60), now=offset_now
    )
    with sqlite3.connect(service.database_path) as connection:
        stored = connection.execute(
            "SELECT lease_expires_at FROM openmontage_job_execution WHERE job_id = ?",
            (lease.job_id,),
        ).fetchone()[0]
    persisted = datetime.fromisoformat(stored)
    assert persisted.utcoffset() == timedelta(0), "persisted expiry must be UTC, not a raw offset"
    assert persisted == datetime(2026, 8, 5, 12, 2, 0, tzinfo=timezone.utc)
    assert renewed.expires_at.utcoffset() == timedelta(0)


def test_settlement_converts_offset_now_to_utc_before_persisting(tmp_path: Path) -> None:
    """Atomic settlement (fail-job-or-confirm-cancel) persists the clock as
    ``updated_at`` via _release_lease_record; an offset ``now`` must be converted
    to UTC first, else the raw offset ISO is stored. The same normalization runs
    in every *_or_confirm_cancel path (they share the effective_now resolution),
    so this is a witness for the whole settlement family."""
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request("settlement-offset"), _attribution())
    lease = service.claim_job(
        worker_id="worker-a", lease_duration=timedelta(minutes=5), now=NOW
    )
    assert lease is not None
    offset_now = datetime(2026, 8, 5, 17, 30, 15, tzinfo=_OFFSET_TZ)  # == 12:00:15Z
    service.fail_job_or_confirm_cancel(
        job.job_id,
        code="OPENMONTAGE_AGENT_EXECUTOR_FAILED",
        message="executor failed",
        retryable=False,
        lease_token=lease.lease_token,
        now=offset_now,
    )
    with sqlite3.connect(service.database_path) as connection:
        stored = connection.execute(
            "SELECT updated_at FROM openmontage_job_execution WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()[0]
    persisted = datetime.fromisoformat(stored)
    assert persisted.utcoffset() == timedelta(0), "persisted settlement time must be UTC"
    assert persisted == datetime(2026, 8, 5, 12, 0, 15, tzinfo=timezone.utc)
