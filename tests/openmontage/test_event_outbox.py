from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from threading import Event, Thread
from time import sleep
from typing import Any

import pytest

from openmontage.contracts import JobAttribution, JobCreateRequest
from openmontage.event_outbox import EventSigner, OutboxPublisher, SignatureError
from openmontage.job_service import JobService, OutboxLeaseError


NOW = datetime(2026, 8, 5, 10, 0, 0, tzinfo=timezone.utc)


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
        workflow="framework-smoke",
        input={"type": "text", "inlineText": "Smoke"},
        brief={"title": "Smoke"},
        output={"container": "mp4"},
        budget={"maxAmount": "1.00", "currency": "CNY"},
    )


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _WriteLockObservedJobService(JobService):
    def __init__(self, database_path: Path):
        self.begin_write_attempted = Event()
        super().__init__(database_path)

    def _begin_write(self, connection: sqlite3.Connection) -> None:
        self.begin_write_attempted.set()
        super()._begin_write(connection)


def test_not_found_retry_limit_still_honors_overall_attempt_limit(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    publisher = OutboxPublisher(
        service,
        endpoint="https://agentspace.internal/events",
        secret="test-secret",
        post=lambda *_args, **_kwargs: _Response(404),
        clock=lambda: NOW,
        max_attempts=2,
        not_found_max_attempts=3,
    )

    publisher.publish_pending()
    publisher.clock = lambda: NOW + timedelta(seconds=2)
    result = publisher.publish_pending()

    record = service.get_outbox_record(service.list_events(job.job_id)[0].event_id)
    assert result.dead_lettered == 1
    assert record.delivery_attempts == 2


def test_event_signer_verifies_exact_body_and_rejects_tampering(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    event = service.list_events(job.job_id)[0]
    signer = EventSigner("test-secret")

    signed = signer.sign(event, timestamp=NOW, nonce="nonce-1")

    assert signer.verify(
        signed.body,
        signed.headers,
        now=NOW + timedelta(seconds=10),
    )["eventId"] == event.event_id
    with pytest.raises(SignatureError, match="signature"):
        signer.verify(
            signed.body + b" ",
            signed.headers,
            now=NOW + timedelta(seconds=10),
        )


def test_event_signer_rejects_expired_requests(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    signer = EventSigner("test-secret", max_age_seconds=300)
    signed = signer.sign(service.list_events(job.job_id)[0], timestamp=NOW, nonce="nonce-1")

    with pytest.raises(SignatureError, match="expired"):
        signer.verify(signed.body, signed.headers, now=NOW + timedelta(seconds=301))


def test_publisher_marks_successful_events_delivered(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    calls: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> _Response:
        calls.append({"url": url, **kwargs})
        return _Response(204)

    publisher = OutboxPublisher(
        service,
        endpoint="https://agentspace.internal/events",
        secret="test-secret",
        post=post,
        clock=lambda: NOW,
        nonce_factory=lambda: "nonce-1",
    )

    result = publisher.publish_pending()

    assert result.delivered == 1
    assert result.failed == 0
    assert len(calls) == 1
    assert calls[0]["headers"]["X-OpenMontage-Event-Id"].startswith("om_evt_")
    assert service.list_pending_outbox(now=NOW + timedelta(days=1)) == []
    assert service.get_outbox_record(service.list_events(job.job_id)[0].event_id).status == "delivered"


def test_publisher_records_delivery_completion_time_after_http_call(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    completed_at = NOW + timedelta(seconds=9)
    times = iter((NOW, completed_at))
    publisher = OutboxPublisher(
        service,
        endpoint="https://agentspace.internal/events",
        secret="test-secret",
        post=lambda *_args, **_kwargs: _Response(204),
        clock=lambda: next(times),
    )

    publisher.publish_pending(limit=1)

    event_id = service.list_events(job.job_id)[0].event_id
    assert service.get_outbox_record(event_id).delivered_at == completed_at


def test_concurrent_publishers_do_not_send_the_same_event_twice(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    service = JobService(db_path)
    service.create_job(_request(), _attribution())
    first_post_started = Event()
    release_first_post = Event()
    calls: list[str] = []
    failures: list[BaseException] = []

    def first_post(*_args: Any, **_kwargs: Any) -> _Response:
        calls.append("first")
        first_post_started.set()
        assert release_first_post.wait(timeout=5)
        return _Response(204)

    def second_post(*_args: Any, **_kwargs: Any) -> _Response:
        calls.append("second")
        return _Response(204)

    first = OutboxPublisher(
        JobService(db_path),
        endpoint="https://agentspace.internal/events",
        secret="test-secret",
        post=first_post,
        clock=lambda: NOW,
        nonce_factory=lambda: "nonce-first",
    )
    second = OutboxPublisher(
        JobService(db_path),
        endpoint="https://agentspace.internal/events",
        secret="test-secret",
        post=second_post,
        clock=lambda: NOW,
        nonce_factory=lambda: "nonce-second",
    )

    def run_first() -> None:
        try:
            first.publish_pending()
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below.
            failures.append(exc)

    thread = Thread(target=run_first)
    thread.start()
    assert first_post_started.wait(timeout=5)
    second_result = second.publish_pending()
    release_first_post.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert failures == []
    assert calls == ["first"]
    assert second_result == type(second_result)(delivered=0, failed=0, dead_lettered=0)


def test_concurrent_publishers_can_deliver_distinct_events(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    service = JobService(db_path)
    first_job = service.create_job(_request(), _attribution())
    second_request = _request().model_copy(update={"client_request_id": "request-2"})
    second_job = service.create_job(second_request, _attribution())
    first_event_id = service.list_events(first_job.job_id)[0].event_id
    second_event_id = service.list_events(second_job.job_id)[0].event_id
    first_post_started = Event()
    release_first_post = Event()
    calls: list[str] = []

    def blocking_post(*_args: Any, **kwargs: Any) -> _Response:
        event_id = kwargs["headers"]["X-OpenMontage-Event-Id"]
        calls.append(event_id)
        if event_id == first_event_id:
            first_post_started.set()
            assert release_first_post.wait(timeout=5)
        return _Response(204)

    first = OutboxPublisher(
        JobService(db_path),
        endpoint="https://agentspace.internal/events",
        secret="test-secret",
        post=blocking_post,
        clock=lambda: NOW,
    )
    second = OutboxPublisher(
        JobService(db_path),
        endpoint="https://agentspace.internal/events",
        secret="test-secret",
        post=blocking_post,
        clock=lambda: NOW,
    )
    thread = Thread(target=lambda: first.publish_pending(limit=2))
    thread.start()
    assert first_post_started.wait(timeout=5)

    second_result = second.publish_pending(limit=1)
    release_first_post.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert second_result.delivered == 1
    assert calls == [first_event_id, second_event_id]


def test_expired_delivery_claim_is_recoverable_and_fences_old_owner(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    event_id = service.list_events(job.job_id)[0].event_id

    first = service.claim_pending_outbox(
        lease_token="first-owner",
        now=NOW,
        lease_seconds=1,
        limit=1,
    )
    while_active = service.claim_pending_outbox(
        lease_token="second-owner",
        now=NOW + timedelta(milliseconds=500),
        lease_seconds=1,
        limit=1,
    )
    reclaimed = service.claim_pending_outbox(
        lease_token="second-owner",
        now=NOW + timedelta(seconds=1),
        lease_seconds=1,
        limit=1,
    )

    assert first[0].delivery_lease_token == "first-owner"
    assert while_active == []
    assert reclaimed[0].delivery_lease_token == "second-owner"
    with pytest.raises(OutboxLeaseError, match="no longer owned"):
        service.mark_event_delivered(
            event_id,
            lease_token="first-owner",
            delivered_at=NOW + timedelta(seconds=1),
        )
    assert service.get_outbox_record(event_id).delivery_lease_token == "second-owner"


def test_delivery_claim_lease_starts_after_waiting_for_write_lock(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    service = _WriteLockObservedJobService(database_path)
    service.create_job(_request(), _attribution())
    blocker = sqlite3.connect(database_path)
    blocker.execute("BEGIN IMMEDIATE")
    service.begin_write_attempted.clear()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            service.claim_pending_outbox,
            lease_token="publisher-a",
            lease_seconds=0.15,
            limit=1,
        )
        assert service.begin_write_attempted.wait(timeout=1)
        sleep(0.2)
        blocker.commit()
        lock_released_at = datetime.now(timezone.utc)
        claimed = future.result(timeout=2)
    blocker.close()

    assert len(claimed) == 1
    assert claimed[0].delivery_lease_expires_at is not None
    assert claimed[0].delivery_lease_expires_at > lock_released_at


def test_publisher_persists_retry_and_waits_for_backoff_after_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    service = JobService(db_path)
    job = service.create_job(_request(), _attribution())

    def failing_post(url: str, **kwargs: Any) -> _Response:
        raise OSError("bridge unavailable")

    publisher = OutboxPublisher(
        service,
        endpoint="https://agentspace.internal/events",
        secret="test-secret",
        post=failing_post,
        clock=lambda: NOW,
        nonce_factory=lambda: "nonce-1",
    )

    result = publisher.publish_pending()
    event_id = service.list_events(job.job_id)[0].event_id
    record = service.get_outbox_record(event_id)

    assert result.delivered == 0
    assert result.failed == 1
    assert record.status == "retry"
    assert record.delivery_attempts == 1
    assert record.last_error == "bridge unavailable"
    assert service.list_pending_outbox(now=NOW) == []

    restarted = JobService(db_path)
    assert [item.event.event_id for item in restarted.list_pending_outbox(now=NOW + timedelta(seconds=2))] == [
        event_id
    ]


def test_publisher_retry_backoff_starts_when_http_failure_finishes(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    failed_at = NOW + timedelta(seconds=9)
    times = iter((NOW, failed_at))

    def failing_post(*_args: Any, **_kwargs: Any) -> _Response:
        raise OSError("bridge unavailable")

    publisher = OutboxPublisher(
        service,
        endpoint="https://agentspace.internal/events",
        secret="test-secret",
        post=failing_post,
        clock=lambda: next(times),
    )

    publisher.publish_pending(limit=1)

    event_id = service.list_events(job.job_id)[0].event_id
    assert service.get_outbox_record(event_id).next_attempt_at == failed_at + timedelta(seconds=2)


def test_publisher_dead_letters_permanent_http_failure_without_retry(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    publisher = OutboxPublisher(
        service,
        endpoint="https://agentspace.internal/events",
        secret="test-secret",
        post=lambda *_args, **_kwargs: _Response(401),
        clock=lambda: NOW,
        nonce_factory=lambda: "nonce-1",
    )

    result = publisher.publish_pending()
    record = service.get_outbox_record(service.list_events(job.job_id)[0].event_id)

    assert result.delivered == 0
    assert result.failed == 0
    assert result.dead_lettered == 1
    assert record.status == "dead_letter"
    assert record.delivery_attempts == 1
    assert record.next_attempt_at is None
    assert record.delivered_at is None
    assert record.last_error == "AgentSpace event bridge returned HTTP 401"


def test_publisher_bounds_not_found_retries_for_job_link_race(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())
    publisher = OutboxPublisher(
        service,
        endpoint="https://agentspace.internal/events",
        secret="test-secret",
        post=lambda *_args, **_kwargs: _Response(404),
        clock=lambda: NOW,
        nonce_factory=lambda: "nonce-1",
        not_found_max_attempts=2,
    )

    first = publisher.publish_pending()
    event_id = service.list_events(job.job_id)[0].event_id
    first_record = service.get_outbox_record(event_id)

    assert first.failed == 1
    assert first.dead_lettered == 0
    assert first_record.status == "retry"

    publisher.clock = lambda: NOW + timedelta(seconds=2)
    second = publisher.publish_pending()
    final_record = service.get_outbox_record(event_id)

    assert second.failed == 0
    assert second.dead_lettered == 1
    assert final_record.status == "dead_letter"
    assert final_record.delivery_attempts == 2
    assert final_record.next_attempt_at is None


def test_publisher_bounds_transient_delivery_attempts(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(_request(), _attribution())

    def failing_post(*_args: Any, **_kwargs: Any) -> _Response:
        raise OSError("bridge unavailable")

    publisher = OutboxPublisher(
        service,
        endpoint="https://agentspace.internal/events",
        secret="test-secret",
        post=failing_post,
        clock=lambda: NOW,
        nonce_factory=lambda: "nonce-1",
        max_attempts=2,
    )

    publisher.publish_pending()
    publisher.clock = lambda: NOW + timedelta(seconds=2)
    result = publisher.publish_pending()
    record = service.get_outbox_record(service.list_events(job.job_id)[0].event_id)

    assert result.failed == 0
    assert result.dead_lettered == 1
    assert record.status == "dead_letter"
    assert record.delivery_attempts == 2


def test_publisher_does_not_reclassify_local_state_write_failure(tmp_path: Path) -> None:
    class FailingStateService(JobService):
        dead_letter_calls = 0

        def mark_event_dead_lettered(
            self,
            event_id: str,
            *,
            lease_token: str,
            error: str,
        ) -> None:
            self.dead_letter_calls += 1
            raise OSError("database is read-only")

    service = FailingStateService(tmp_path / "jobs.sqlite3")
    service.create_job(_request(), _attribution())
    publisher = OutboxPublisher(
        service,
        endpoint="https://agentspace.internal/events",
        secret="test-secret",
        post=lambda *_args, **_kwargs: _Response(401),
        clock=lambda: NOW,
        nonce_factory=lambda: "nonce-1",
    )

    with pytest.raises(OSError, match="database is read-only"):
        publisher.publish_pending()

    assert service.dead_letter_calls == 1


def test_publisher_from_environment_requires_a_complete_bridge_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    monkeypatch.delenv("OPENMONTAGE_EVENT_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENMONTAGE_EVENT_SIGNING_SECRET", raising=False)

    with pytest.raises(ValueError, match="OPENMONTAGE_EVENT_ENDPOINT"):
        OutboxPublisher.from_environment(service)

    monkeypatch.setenv("OPENMONTAGE_EVENT_ENDPOINT", "https://agentspace.internal/events")
    with pytest.raises(ValueError, match="OPENMONTAGE_EVENT_SIGNING_SECRET"):
        OutboxPublisher.from_environment(service)

    monkeypatch.setenv("OPENMONTAGE_EVENT_SIGNING_SECRET", "bridge-secret")
    publisher = OutboxPublisher.from_environment(service)

    assert publisher.endpoint == "https://agentspace.internal/events"
