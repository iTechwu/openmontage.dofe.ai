"""Signed delivery for durable OpenMontage Job events."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

import requests

from openmontage.contracts import JobEvent
from openmontage.job_service import JobService


class SignatureError(ValueError):
    """Raised when an event request signature is invalid or stale."""


@dataclass(frozen=True)
class SignedEvent:
    body: bytes
    headers: dict[str, str]


@dataclass(frozen=True)
class PublishResult:
    delivered: int
    failed: int
    dead_lettered: int = 0


class EventSigner:
    def __init__(self, secret: str, *, max_age_seconds: int = 300):
        if not secret:
            raise ValueError("OpenMontage event signing secret is required")
        self._secret = secret.encode("utf-8")
        self.max_age_seconds = max_age_seconds

    def sign(
        self,
        event: JobEvent,
        *,
        timestamp: datetime | None = None,
        nonce: str | None = None,
    ) -> SignedEvent:
        occurred = timestamp or datetime.now(timezone.utc)
        timestamp_value = str(int(occurred.timestamp()))
        nonce_value = nonce or uuid4().hex
        body = json.dumps(
            event.to_wire(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = self._signature(timestamp_value, nonce_value, body)
        return SignedEvent(
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-OpenMontage-Event-Id": event.event_id,
                "X-OpenMontage-Timestamp": timestamp_value,
                "X-OpenMontage-Nonce": nonce_value,
                "X-OpenMontage-Signature": signature,
            },
        )

    def verify(
        self,
        body: bytes,
        headers: dict[str, str],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        try:
            timestamp_value = headers["X-OpenMontage-Timestamp"]
            nonce = headers["X-OpenMontage-Nonce"]
            supplied_signature = headers["X-OpenMontage-Signature"]
            timestamp = datetime.fromtimestamp(int(timestamp_value), tz=timezone.utc)
        except (KeyError, TypeError, ValueError) as exc:
            raise SignatureError("Invalid OpenMontage event signature headers") from exc

        effective_now = now or datetime.now(timezone.utc)
        if abs((effective_now - timestamp).total_seconds()) > self.max_age_seconds:
            raise SignatureError("OpenMontage event signature has expired")

        expected_signature = self._signature(timestamp_value, nonce, body)
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise SignatureError("Invalid OpenMontage event signature")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SignatureError("Signed OpenMontage event body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise SignatureError("Signed OpenMontage event body must be an object")
        return payload

    def _signature(self, timestamp: str, nonce: str, body: bytes) -> str:
        message = timestamp.encode("ascii") + b"." + nonce.encode("utf-8") + b"." + body
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()


class OutboxPublisher:
    def __init__(
        self,
        service: JobService,
        *,
        endpoint: str,
        secret: str,
        post: Callable[..., Any] = requests.post,
        clock: Callable[[], datetime] | None = None,
        nonce_factory: Callable[[], str] | None = None,
        timeout_seconds: float = 10.0,
        max_attempts: int = 20,
        not_found_max_attempts: int = 5,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be greater than zero")
        if not_found_max_attempts < 1:
            raise ValueError("not_found_max_attempts must be greater than zero")
        self.service = service
        self.endpoint = endpoint
        self.signer = EventSigner(secret)
        self.post = post
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.nonce_factory = nonce_factory or (lambda: uuid4().hex)
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.not_found_max_attempts = not_found_max_attempts

    @classmethod
    def from_environment(cls, service: JobService | None = None) -> "OutboxPublisher":
        endpoint = os.environ.get("OPENMONTAGE_EVENT_ENDPOINT", "").strip()
        if not endpoint:
            raise ValueError("OPENMONTAGE_EVENT_ENDPOINT is required")
        secret = os.environ.get("OPENMONTAGE_EVENT_SIGNING_SECRET", "")
        if not secret:
            raise ValueError("OPENMONTAGE_EVENT_SIGNING_SECRET is required")
        if service is None:
            from openmontage.job_api import default_job_service

            service = default_job_service()
        return cls(
            service,
            endpoint=endpoint,
            secret=secret,
            max_attempts=_positive_env_int("OPENMONTAGE_EVENT_MAX_ATTEMPTS", 20),
            not_found_max_attempts=_positive_env_int(
                "OPENMONTAGE_EVENT_NOT_FOUND_MAX_ATTEMPTS", 5
            ),
        )

    def publish_pending(self, *, limit: int = 100) -> PublishResult:
        lease_seconds = max(30.0, self.timeout_seconds * 2)
        delivered = 0
        failed = 0
        dead_lettered = 0
        for _ in range(limit):
            now = self.clock()
            lease_token = uuid4().hex
            records = self.service.claim_pending_outbox(
                lease_token=lease_token,
                now=now,
                lease_seconds=lease_seconds,
                limit=1,
            )
            if not records:
                break
            record = records[0]
            signed = self.signer.sign(
                record.event,
                timestamp=now,
                nonce=self.nonce_factory(),
            )
            try:
                response = self.post(
                    self.endpoint,
                    data=signed.body,
                    headers=signed.headers,
                    timeout=self.timeout_seconds,
                )
                status_code = int(response.status_code)
            except Exception as exc:
                error = str(exc)
                failed_at = self.clock()
                if record.delivery_attempts + 1 >= self.max_attempts:
                    self.service.mark_event_dead_lettered(
                        record.event.event_id,
                        lease_token=lease_token,
                        error=error,
                    )
                    dead_lettered += 1
                else:
                    self._schedule_retry(
                        record.event.event_id,
                        lease_token,
                        record.delivery_attempts,
                        error,
                        failed_at,
                    )
                    failed += 1
                continue
            completed_at = self.clock()
            if not 200 <= status_code < 300:
                error = f"AgentSpace event bridge returned HTTP {status_code}"
                if self._should_dead_letter(record.delivery_attempts, status_code):
                    self.service.mark_event_dead_lettered(
                        record.event.event_id,
                        lease_token=lease_token,
                        error=error,
                    )
                    dead_lettered += 1
                else:
                    self._schedule_retry(
                        record.event.event_id,
                        lease_token,
                        record.delivery_attempts,
                        error,
                        completed_at,
                    )
                    failed += 1
                continue
            self.service.mark_event_delivered(
                record.event.event_id,
                lease_token=lease_token,
                delivered_at=completed_at,
            )
            delivered += 1
        return PublishResult(
            delivered=delivered,
            failed=failed,
            dead_lettered=dead_lettered,
        )

    def _should_dead_letter(self, previous_attempts: int, status_code: int) -> bool:
        attempt = previous_attempts + 1
        if attempt >= self.max_attempts:
            return True
        if status_code == 404:
            return attempt >= self.not_found_max_attempts
        if status_code in {408, 425, 429} or 500 <= status_code < 600:
            return False
        return True

    def _schedule_retry(
        self,
        event_id: str,
        lease_token: str,
        previous_attempts: int,
        error: str,
        now: datetime,
    ) -> None:
        delay_seconds = min(300, 2 ** (previous_attempts + 1))
        self.service.mark_event_failed(
            event_id,
            lease_token=lease_token,
            error=error,
            next_attempt_at=now + timedelta(seconds=delay_seconds),
        )


def _positive_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value
