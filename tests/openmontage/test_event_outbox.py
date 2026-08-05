from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from openmontage.contracts import JobAttribution, JobCreateRequest
from openmontage.event_outbox import EventSigner, OutboxPublisher, SignatureError
from openmontage.job_service import JobService


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
