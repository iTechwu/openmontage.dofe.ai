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
    ):
        self.service = service
        self.endpoint = endpoint
        self.signer = EventSigner(secret)
        self.post = post
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.nonce_factory = nonce_factory or (lambda: uuid4().hex)
        self.timeout_seconds = timeout_seconds

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
        return cls(service, endpoint=endpoint, secret=secret)

    def publish_pending(self, *, limit: int = 100) -> PublishResult:
        now = self.clock()
        delivered = 0
        failed = 0
        for record in self.service.list_pending_outbox(now=now, limit=limit):
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
                if not 200 <= int(response.status_code) < 300:
                    raise RuntimeError(f"AgentSpace event bridge returned HTTP {response.status_code}")
                self.service.mark_event_delivered(record.event.event_id, delivered_at=now)
                delivered += 1
            except Exception as exc:
                delay_seconds = min(300, 2 ** (record.delivery_attempts + 1))
                self.service.mark_event_failed(
                    record.event.event_id,
                    error=str(exc),
                    next_attempt_at=now + timedelta(seconds=delay_seconds),
                )
                failed += 1
        return PublishResult(delivered=delivered, failed=failed)
