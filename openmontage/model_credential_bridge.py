"""Authenticated client for AgentSpace's Job-stage model credential escrow."""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

import requests

from openmontage.contracts import JobAttribution
from tools.dofe.delegation import DelegatedModelCredential


class ModelCredentialBridgeError(RuntimeError):
    """Raised when a delegated credential cannot be issued safely."""


class ModelCredentialBridgeClient:
    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        session: Any | None = None,
        timeout: tuple[float, float] = (5.0, 30.0),
    ) -> None:
        self.base_url = _base_url(base_url)
        self.service_token = service_token.strip()
        if not self.service_token:
            raise ModelCredentialBridgeError("OpenMontage service token is required")
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout

    @classmethod
    def from_environment(cls, *, session: Any | None = None) -> "ModelCredentialBridgeClient":
        return cls(
            base_url=os.environ.get("OPENMONTAGE_MODEL_CREDENTIAL_BASE_URL", ""),
            service_token=os.environ.get("OPENMONTAGE_SERVICE_TOKEN", ""),
            session=session,
        )

    def issue(
        self,
        *,
        job_id: str,
        stage: str,
        attribution: JobAttribution,
    ) -> DelegatedModelCredential:
        normalized_job_id = _identifier(job_id, "job_id")
        normalized_stage = _identifier(stage, "stage")
        try:
            response = self.session.post(
                f"{self.base_url}/api/internal/openmontage/jobs/"
                f"{quote(normalized_job_id, safe='')}/model-credential",
                headers={
                    "Authorization": f"Bearer {self.service_token}",
                    "X-Dofe-Job-Attribution": _attribution(attribution),
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"stage": normalized_stage},
                timeout=self.timeout,
                allow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise ModelCredentialBridgeError("AgentSpace model credential request failed") from exc
        credential = _parse_credential(payload)
        if credential.external_job_id != normalized_job_id or credential.pipeline_stage != normalized_stage:
            raise ModelCredentialBridgeError("AgentSpace model credential identity does not match the Job stage")
        return credential


def _parse_credential(value: Any) -> DelegatedModelCredential:
    if not isinstance(value, dict):
        raise ModelCredentialBridgeError("AgentSpace model credential response is invalid")
    expected = {
        "schemaVersion",
        "jobId",
        "stage",
        "delegationId",
        "runtimeCredentialId",
        "modelsBaseUrl",
        "apiKey",
        "spendLimit",
        "currency",
        "expiresAt",
    }
    if set(value) != expected or value.get("schemaVersion") != 1:
        raise ModelCredentialBridgeError("AgentSpace model credential response is invalid")
    for field in expected - {"schemaVersion"}:
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ModelCredentialBridgeError("AgentSpace model credential response is invalid")
    expires_at = _future_timestamp(value["expiresAt"])
    return DelegatedModelCredential(
        api_key=value["apiKey"],
        models_base_url=_base_url(value["modelsBaseUrl"]),
        delegation_id=_identifier(value["delegationId"], "delegation_id"),
        external_job_id=_identifier(value["jobId"], "job_id"),
        pipeline_stage=_identifier(value["stage"], "stage"),
        runtime_credential_id=_identifier(value["runtimeCredentialId"], "runtime_credential_id"),
        expires_at=expires_at,
    )


def _attribution(value: JobAttribution) -> str:
    raw = json.dumps(value.to_wire(), separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _identifier(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        raise ModelCredentialBridgeError(f"OpenMontage {field} is invalid")
    return normalized


def _future_timestamp(value: str) -> str:
    normalized = value.strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelCredentialBridgeError("AgentSpace model credential expiry is invalid") from exc
    if parsed.tzinfo is None:
        raise ModelCredentialBridgeError("AgentSpace model credential expiry is invalid")
    if parsed <= datetime.now(timezone.utc):
        raise ModelCredentialBridgeError("AgentSpace model credential has expired")
    return normalized


def _base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ModelCredentialBridgeError("OpenMontage model credential base URL is invalid")
    return normalized
