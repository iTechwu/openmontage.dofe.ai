from __future__ import annotations

import base64
import json
from pathlib import Path

import jsonschema
import pytest
from starlette.testclient import TestClient

from openmontage.contracts import JobAttribution, PublishedArtifact
from openmontage.job_api import TrustedAttributionResolver
from openmontage.job_service import JobService
from openmontage.mcp_server import build_http_app, create_server


SERVICE_TOKEN = "service-token"


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


def _headers() -> dict[str, str]:
    encoded = base64.urlsafe_b64encode(
        json.dumps(_attribution().to_wire(), separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return {
        "Authorization": f"Bearer {SERVICE_TOKEN}",
        "X-Dofe-Job-Attribution": encoded,
    }


def _request() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "clientRequestId": "request-1",
        "workflow": "framework-smoke",
        "input": {"type": "text", "inlineText": "Smoke"},
        "brief": {"title": "Smoke"},
        "output": {"container": "mp4"},
        "budget": {"maxAmount": "1.00", "currency": "CNY"},
    }


def _client(tmp_path: Path) -> tuple[TestClient, JobService]:
    service = JobService(tmp_path / "jobs.sqlite3")
    app = build_http_app(
        job_service=service,
        attribution_resolver=TrustedAttributionResolver(SERVICE_TOKEN),
    )
    return TestClient(app), service


def test_rest_job_creation_requires_trusted_service_context(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.post("/api/v1/jobs", json=_request())

    assert response.status_code == 401
    assert response.json() == {"error": {"code": "OPENMONTAGE_UNAUTHORIZED"}}


def test_rest_job_creation_rejects_unknown_workflow_as_validation_error(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    request = {**_request(), "workflow": "compose"}

    response = client.post("/api/v1/jobs", json=request, headers=_headers())

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "OPENMONTAGE_VALIDATION_FAILED"
    assert "Unknown workflow 'compose'" in error["message"]
    assert "framework-smoke" in error["message"]
    assert "known workflow names" in error["message"]
    assert "openmontage_capabilities" in error["message"]
    assert "supported workflows" not in error["message"]
    assert "pipeline_defs" not in error["message"]


def test_rest_job_creation_reports_invalid_manifest_as_workflow_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_error: Exception,
) -> None:
    client, _ = _client(tmp_path)

    def fail_to_load_manifest(_workflow: str) -> None:
        raise manifest_error

    monkeypatch.setattr(
        "openmontage.contracts.load_pipeline_readonly",
        fail_to_load_manifest,
    )

    response = client.post("/api/v1/jobs", json=_request(), headers=_headers())

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "OPENMONTAGE_WORKFLOW_UNAVAILABLE",
            "message": "Workflow 'framework-smoke' is unavailable because its manifest is invalid",
        }
    }


@pytest.fixture(
    params=[
        jsonschema.ValidationError("internal path"),
        json.JSONDecodeError("internal path", "", 0),
        FileNotFoundError("internal path"),
    ],
    ids=["manifest-validation", "manifest-schema-json", "manifest-disappeared"],
)
def manifest_error(request: pytest.FixtureRequest) -> Exception:
    return request.param


def test_rest_job_creation_reports_invalid_manifest_projection_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(tmp_path)
    monkeypatch.setattr(
        "openmontage.contracts.load_pipeline_readonly",
        lambda _workflow: {
            "name": "",
            "version": "1",
            "stages": [{"name": "research"}],
        },
    )

    response = client.post("/api/v1/jobs", json=_request(), headers=_headers())

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "OPENMONTAGE_WORKFLOW_UNAVAILABLE",
            "message": "Workflow 'framework-smoke' is unavailable because its manifest is invalid",
        }
    }


def test_rest_job_creation_reports_manifest_name_mismatch_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(tmp_path)
    monkeypatch.setattr(
        "openmontage.contracts.load_pipeline_readonly",
        lambda _workflow: {
            "name": "different-workflow",
            "version": "1",
            "stages": [{"name": "research"}],
        },
    )

    response = client.post("/api/v1/jobs", json=_request(), headers=_headers())

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "OPENMONTAGE_WORKFLOW_UNAVAILABLE",
        "message": "Workflow 'framework-smoke' is unavailable because its manifest is invalid",
    }


@pytest.mark.parametrize(
    ("field", "value", "message_fragment"),
    [
        ("schemaVersion", True, "schemaVersion"),
        ("schemaVersion", 1.0, "schemaVersion"),
        ("clientRequestId", "   ", "clientRequestId"),
        ("input", {"type": "artifact"}, "artifactId"),
        ("input", {"type": "artifact", "artifactId": "   "}, "artifactId"),
        ("input", {"type": "text"}, "inlineText"),
        ("input", {"type": "text", "inlineText": "   "}, "inlineText"),
        ("brief", {}, "title"),
        ("brief", {"title": "   "}, "title"),
        ("brief", {"title": "Smoke", "durationSeconds": 86401}, "durationSeconds"),
        ("brief", {"title": "Smoke", "durationSeconds": True}, "durationSeconds"),
        ("output", {}, "container"),
        ("output", {"container": "mp4", "resolution": "8193x1080"}, "resolution"),
        ("output", {"container": "mp4", "fps": True}, "fps"),
        ("budget", {}, "maxAmount"),
        ("budget", {"maxAmount": "1.00"}, "currency"),
    ],
)
def test_rest_job_creation_rejects_incomplete_nested_contracts(
    tmp_path: Path,
    field: str,
    value: dict[str, object],
    message_fragment: str,
) -> None:
    client, _ = _client(tmp_path)
    request = {**_request(), field: value}

    response = client.post("/api/v1/jobs", json=request, headers=_headers())

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "OPENMONTAGE_VALIDATION_FAILED"
    assert message_fragment in error["message"]


def test_rest_job_create_status_and_event_replay(tmp_path: Path) -> None:
    client, service = _client(tmp_path)

    created = client.post("/api/v1/jobs", json=_request(), headers=_headers())
    assert created.status_code == 201
    job_id = created.json()["jobId"]

    service.start_stage(job_id, "research")
    status = client.get(f"/api/v1/jobs/{job_id}", headers=_headers())
    replay = client.get(
        f"/api/v1/jobs/{job_id}/events?afterSequence=1",
        headers=_headers(),
    )

    assert status.status_code == 200
    assert status.json()["status"] == "RUNNING"
    assert status.json()["currentStage"] == "research"
    assert [event["sequence"] for event in replay.json()["events"]] == [2]
    assert replay.json()["lastSequence"] == 2


def test_rest_lists_published_video_artifacts(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    job_id = client.post("/api/v1/jobs", json=_request(), headers=_headers()).json()["jobId"]
    service.publish_artifact(
        job_id,
        PublishedArtifact(
            job_id=job_id,
            employee_artifact_id="eart-1",
            employee_id="employee-1",
            role="final_video",
            file_name="final.mp4",
            media_type="video/mp4",
            size_bytes=5,
            sha256="a" * 64,
            published_at="2026-08-05T10:00:02Z",
        ),
    )

    response = client.get(f"/api/v1/jobs/{job_id}/artifacts", headers=_headers())

    assert response.status_code == 200
    assert response.json()["artifacts"][0]["employeeArtifactId"] == "eart-1"
    assert response.json()["lastSequence"] == 2


def test_rest_job_access_is_scoped_to_attribution_workspace(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    job_id = client.post("/api/v1/jobs", json=_request(), headers=_headers()).json()["jobId"]
    other = _attribution().model_copy(update={"workspace_id": "ws-2"})
    encoded = base64.urlsafe_b64encode(
        json.dumps(other.to_wire(), separators=(",", ":")).encode("utf-8")
    ).decode("ascii")

    response = client.get(
        f"/api/v1/jobs/{job_id}",
        headers={
            "Authorization": f"Bearer {SERVICE_TOKEN}",
            "X-Dofe-Job-Attribution": encoded,
        },
    )

    assert response.status_code == 404


def test_rest_cancel_uses_persisted_idempotency_and_sequence_fencing(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    job_id = client.post("/api/v1/jobs", json=_request(), headers=_headers()).json()["jobId"]
    headers = {**_headers(), "Idempotency-Key": "cancel-job-1-at-sequence-1"}

    first = client.post(
        f"/api/v1/jobs/{job_id}/cancel",
        json={"expectedSequence": 1},
        headers=headers,
    )
    repeated = client.post(
        f"/api/v1/jobs/{job_id}/cancel",
        json={"expectedSequence": 1},
        headers=headers,
    )
    conflicting = client.post(
        f"/api/v1/jobs/{job_id}/cancel",
        json={"expectedSequence": 2},
        headers=headers,
    )

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert first.json()["lastSequence"] == 2
    assert repeated.json()["lastSequence"] == 2
    assert conflicting.status_code == 409
    assert [event.sequence for event in service.list_events(job_id)] == [1, 2]


@pytest.mark.asyncio
async def test_mcp_job_tools_do_not_expose_trusted_attribution_as_model_input(tmp_path: Path) -> None:
    from mcp import Client

    service = JobService(tmp_path / "jobs.sqlite3")

    def resolve_for_test(_headers: object) -> JobAttribution:
        return _attribution()

    async with Client(
        create_server(job_service=service, attribution_resolver=resolve_for_test)
    ) as client:
        tools = await client.list_tools()
        created = await client.call_tool("submit_video_job", {"request": _request()})
        artifacts = await client.call_tool(
            "list_video_artifacts",
            {"job_id": created.structured_content["jobId"]},
        )

    by_name = {tool.name: tool for tool in tools.tools}
    assert {
        "submit_video_job",
        "get_video_job",
        "cancel_video_job",
        "approve_video_stage",
        "list_video_job_events",
        "list_video_artifacts",
    }.issubset(by_name)
    submit_schema = by_name["submit_video_job"].input_schema
    serialized_schema = json.dumps(submit_schema)
    assert "workspaceId" not in serialized_schema
    assert "employeeId" not in serialized_schema
    request_schema = submit_schema["properties"]["request"]
    if "$ref" in request_schema:
        request_schema = submit_schema["$defs"][request_schema["$ref"].rsplit("/", 1)[-1]]
    assert set(request_schema["required"]) == {
        "clientRequestId",
        "workflow",
        "input",
        "brief",
        "output",
        "budget",
    }
    assert set(request_schema["properties"]) == {
        "schemaVersion",
        "clientRequestId",
        "workflow",
        "input",
        "brief",
        "output",
        "budget",
    }
    assert "stage names such as compose are invalid" in request_schema["properties"]["workflow"][
        "description"
    ]
    assert request_schema["additionalProperties"] is False
    assert "discriminator" in request_schema["properties"]["input"]
    assert "oneOf" in request_schema["properties"]["input"]
    nested_refs = {
        request_schema["properties"][name]["$ref"].rsplit("/", 1)[-1]
        for name in ("brief", "output", "budget")
    }
    input_refs = {
        item["$ref"].rsplit("/", 1)[-1]
        for item in request_schema["properties"]["input"]["oneOf"]
    }
    for definition_name in nested_refs | input_refs:
        assert submit_schema["$defs"][definition_name]["additionalProperties"] is False
    assert created.structured_content["status"] == "QUEUED"
    assert artifacts.structured_content["artifacts"] == []


@pytest.mark.asyncio
async def test_mcp_job_creation_rejects_unknown_workflow_with_actionable_error(
    tmp_path: Path,
) -> None:
    from mcp import Client

    service = JobService(tmp_path / "jobs.sqlite3")

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        result = await client.call_tool(
            "submit_video_job",
            {"request": {**_request(), "workflow": "compose"}},
        )

    assert result.is_error is True
    message = result.content[0].text
    assert "Unknown workflow 'compose'" in message
    assert "framework-smoke" in message
    assert "known workflow names" in message
    assert "openmontage_capabilities" in message
    assert "supported workflows" not in message
    assert "pipeline_defs" not in message


@pytest.mark.asyncio
async def test_mcp_job_creation_reports_invalid_manifest_without_internal_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp import Client

    def fail_to_load_manifest(_workflow: str) -> None:
        raise jsonschema.ValidationError("internal path")

    monkeypatch.setattr(
        "openmontage.contracts.load_pipeline_readonly",
        fail_to_load_manifest,
    )
    service = JobService(tmp_path / "jobs.sqlite3")

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        result = await client.call_tool("submit_video_job", {"request": _request()})

    assert result.is_error is True
    assert "Workflow 'framework-smoke' is unavailable" in result.content[0].text
    assert "internal path" not in result.content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "message_fragment"),
    [
        ("schemaVersion", True, "schemaVersion"),
        ("schemaVersion", 1.0, "schemaVersion"),
        ("clientRequestId", "   ", "clientRequestId"),
        ("input", {"type": "artifact"}, "artifactId"),
        ("input", {"type": "text", "inlineText": "   "}, "inlineText"),
        ("brief", {"title": "Smoke", "durationSeconds": True}, "durationSeconds"),
        ("output", {"container": "mp4", "fps": True}, "fps"),
        ("budget", {}, "maxAmount"),
    ],
)
async def test_mcp_job_creation_rejects_invalid_nested_contracts(
    tmp_path: Path,
    field: str,
    value: dict[str, object],
    message_fragment: str,
) -> None:
    from mcp import Client

    service = JobService(tmp_path / "jobs.sqlite3")

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        result = await client.call_tool(
            "submit_video_job",
            {"request": {**_request(), field: value}},
        )

    assert result.is_error is True
    assert message_fragment in result.content[0].text
