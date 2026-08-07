from __future__ import annotations

import base64
import json
from typing import Any

import pytest
import requests

from openmontage.contracts import JobAttribution
from openmontage.model_credential_bridge import (
    ModelCredentialBridgeClient,
    ModelCredentialBridgeError,
)


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self.payload


class _Session:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return _Response(self.payload)


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


def _credential() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "jobId": "om_job_1",
        "stage": "research",
        "delegationId": "00000000-0000-4000-8000-000000000002",
        "runtimeCredentialId": "00000000-0000-4000-8000-000000000001",
        "modelsBaseUrl": "https://models.test/api",
        "apiKey": "delegated-api-key",
        "spendLimit": "20.00",
        "currency": "CNY",
        "expiresAt": "2099-08-06T09:00:01Z",
    }


def test_fetches_a_stage_credential_with_service_auth_and_trusted_attribution() -> None:
    session = _Session(_credential())
    client = ModelCredentialBridgeClient(
        base_url="http://agentspace.internal:1455",
        service_token="service-token",
        session=session,
    )

    credential = client.issue(job_id="om_job_1", stage="research", attribution=_attribution())

    assert credential.api_key == "delegated-api-key"
    assert credential.external_job_id == "om_job_1"
    call = session.calls[0]
    assert call["url"].endswith("/api/internal/openmontage/jobs/om_job_1/model-credential")
    assert call["headers"]["Authorization"] == "Bearer service-token"
    decoded = json.loads(base64.urlsafe_b64decode(call["headers"]["X-Dofe-Job-Attribution"] + "=="))
    assert decoded["employeeId"] == "employee-1"
    assert call["json"] == {"stage": "research", "stageAttempt": 1}


def test_rejects_a_mismatched_or_malformed_credential_response() -> None:
    payload = _credential()
    payload["stage"] = "render"
    client = ModelCredentialBridgeClient(
        base_url="http://agentspace.internal:1455",
        service_token="service-token",
        session=_Session(payload),
    )

    with pytest.raises(ModelCredentialBridgeError, match="identity"):
        client.issue(job_id="om_job_1", stage="research", attribution=_attribution())


def test_rejects_an_expired_credential_response() -> None:
    payload = _credential()
    payload["expiresAt"] = "2020-08-06T09:00:01Z"
    client = ModelCredentialBridgeClient(
        base_url="http://agentspace.internal:1455",
        service_token="service-token",
        session=_Session(payload),
    )

    with pytest.raises(ModelCredentialBridgeError, match="expired"):
        client.issue(job_id="om_job_1", stage="research", attribution=_attribution())


def test_reports_safe_http_status_and_error_code_without_response_secrets() -> None:
    session = _Session({})
    session.post = lambda *_args, **_kwargs: _Response({
        "error": {
            "code": "OPENMONTAGE_MODEL_CREDENTIAL_UNAVAILABLE",
            "message": "vault failed for sk-should-never-be-logged",
        },
    }, status_code=503)
    client = ModelCredentialBridgeClient(
        base_url="http://agentspace.internal:1455",
        service_token="service-token",
        session=session,
    )

    with pytest.raises(ModelCredentialBridgeError) as raised:
        client.issue(job_id="om_job_1", stage="research", attribution=_attribution())

    message = str(raised.value)
    assert "HTTP 503" in message
    assert "OPENMONTAGE_MODEL_CREDENTIAL_UNAVAILABLE" in message
    assert "sk-should-never-be-logged" not in message
