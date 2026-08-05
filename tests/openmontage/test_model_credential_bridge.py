from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from openmontage.contracts import JobAttribution
from openmontage.model_credential_bridge import (
    ModelCredentialBridgeClient,
    ModelCredentialBridgeError,
)


class _Response:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

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
    assert call["json"] == {"stage": "research"}


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
