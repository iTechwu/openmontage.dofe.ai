from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest
from starlette.testclient import TestClient

from openmontage.contracts import JobAttribution, JobStatus, PublishedArtifact, StageStatus
from openmontage.job_api import TrustedAttributionResolver
from openmontage import job_service as job_service_module
from openmontage.job_service import JobService
from openmontage.mcp_gateway_auth import gateway_attribution
from openmontage.mcp_server import build_http_app, create_server, stage_execution_contract


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


def _client(
    tmp_path: Path,
    now_fn: Callable[[], datetime] | None = None,
) -> tuple[TestClient, JobService]:
    service = JobService(tmp_path / "jobs.sqlite3")
    app = build_http_app(
        job_service=service,
        attribution_resolver=TrustedAttributionResolver(SERVICE_TOKEN),
        now_fn=now_fn,
    )
    return TestClient(app), service


def test_rest_job_creation_requires_trusted_service_context(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.post("/api/v1/jobs", json=_request())

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    error = response.json()["error"]
    assert error["code"] == "OPENMONTAGE_UNAUTHORIZED"
    assert error["requestId"]


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
    error = response.json()["error"]
    assert error["code"] == "OPENMONTAGE_WORKFLOW_UNAVAILABLE"
    assert error["message"] == (
        "Workflow 'framework-smoke' is unavailable because its manifest is invalid"
    )


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
    error = response.json()["error"]
    assert error["code"] == "OPENMONTAGE_WORKFLOW_UNAVAILABLE"
    assert error["message"] == (
        "Workflow 'framework-smoke' is unavailable because its manifest is invalid"
    )


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
    error = response.json()["error"]
    assert error["code"] == "OPENMONTAGE_WORKFLOW_UNAVAILABLE"
    assert error["message"] == (
        "Workflow 'framework-smoke' is unavailable because its manifest is invalid"
    )
    assert error["requestId"]


def test_rest_job_creation_reports_duplicate_manifest_stages_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(tmp_path)
    monkeypatch.setattr(
        "openmontage.contracts.load_pipeline_readonly",
        lambda _workflow: {
            "name": "framework-smoke",
            "version": "1",
            "stages": [
                {"name": "research", "human_approval_default": False},
                {"name": "research", "human_approval_default": True},
            ],
        },
    )

    response = client.post("/api/v1/jobs", json=_request(), headers=_headers())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "OPENMONTAGE_WORKFLOW_UNAVAILABLE"


@pytest.mark.parametrize(
    "stages",
    [[], [{"name": "../../escape"}]],
    ids=["empty", "unsafe-stage-code"],
)
def test_rest_job_creation_reports_unsafe_manifest_stage_shape_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stages: list[dict[str, object]],
) -> None:
    client, _ = _client(tmp_path)
    monkeypatch.setattr(
        "openmontage.contracts.load_pipeline_readonly",
        lambda _workflow: {
            "name": "framework-smoke",
            "version": "1",
            "stages": stages,
        },
    )

    response = client.post("/api/v1/jobs", json=_request(), headers=_headers())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "OPENMONTAGE_WORKFLOW_UNAVAILABLE"


@pytest.mark.parametrize(
    ("field", "value", "message_fragment"),
    [
        ("schemaVersion", True, "schemaVersion"),
        ("schemaVersion", 1.0, "schemaVersion"),
        ("clientRequestId", "   ", "clientRequestId"),
        ("clientRequestId", "r" * 257, "clientRequestId"),
        ("workflow", "w" * 129, "workflow"),
        ("input", {"type": "artifact"}, "artifactId"),
        ("input", {"type": "artifact", "artifactId": "   "}, "artifactId"),
        ("input", {"type": "artifact", "artifactId": "a" * 257}, "artifactId"),
        ("input", {"type": "text"}, "inlineText"),
        ("input", {"type": "text", "inlineText": "   "}, "inlineText"),
        ("input", {"type": "text", "inlineText": "t" * 100_001}, "inlineText"),
        ("brief", {}, "title"),
        ("brief", {"title": "   "}, "title"),
        ("brief", {"title": "t" * 513}, "title"),
        ("brief", {"title": "Smoke", "audience": "a" * 2_001}, "audience"),
        ("brief", {"title": "Smoke", "durationSeconds": 86401}, "durationSeconds"),
        ("brief", {"title": "Smoke", "durationSeconds": True}, "durationSeconds"),
        ("output", {}, "container"),
        ("output", {"container": "mp4", "resolution": "8193x1080"}, "resolution"),
        ("output", {"container": "mp4", "fps": True}, "fps"),
        ("budget", {}, "maxAmount"),
        ("budget", {"maxAmount": "1.00"}, "currency"),
    ],
)
def test_rest_job_creation_rejects_invalid_request_contracts(
    tmp_path: Path,
    field: str,
    value: object,
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
    assert created.headers["cache-control"] == "no-store"
    job_id = created.json()["data"]["jobId"]

    service.start_stage(job_id, "research")
    status = client.get(f"/api/v1/jobs/{job_id}", headers=_headers())
    replay = client.get(
        f"/api/v1/jobs/{job_id}/events?afterSequence=1",
        headers=_headers(),
    )

    assert status.status_code == 200
    assert status.json()["data"]["status"] == "RUNNING"
    assert status.json()["data"]["currentStage"] == "research"
    body = replay.json()
    assert body["meta"]["source"] == "montage"
    assert body["meta"]["requestId"]
    assert [event["sequence"] for event in body["data"]["events"]] == [2]
    assert body["data"]["lastSequence"] == 2
    # Public event projection drops the attribution fields embedded in JobEvent.
    assert "workspaceId" not in json.dumps(body["data"]["events"])


def test_rest_lists_published_video_artifacts(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    job_id = client.post("/api/v1/jobs", json=_request(), headers=_headers()).json()["data"]["jobId"]
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
    artifact = response.json()["data"]["artifacts"][0]
    assert artifact["artifactId"] == "eart-1"
    assert set(artifact) == {
        "artifactId", "jobId", "role", "fileName", "mediaType",
        "sizeBytes", "sha256", "publishedAt",
    }
    assert response.json()["data"]["lastSequence"] == 2


def test_rest_job_access_is_scoped_to_attribution_workspace(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    job_id = client.post("/api/v1/jobs", json=_request(), headers=_headers()).json()["data"]["jobId"]
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
    job_id = client.post("/api/v1/jobs", json=_request(), headers=_headers()).json()["data"]["jobId"]
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
    assert first.json()["data"]["lastSequence"] == 2
    assert repeated.json()["data"]["lastSequence"] == 2
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
        created = await client.call_tool("submit_video_job", _request())
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
    assert set(submit_schema["required"]) == {
        "clientRequestId",
        "workflow",
        "input",
        "brief",
        "output",
        "budget",
    }
    assert set(submit_schema["properties"]) == {
        "schemaVersion",
        "clientRequestId",
        "workflow",
        "input",
        "brief",
        "output",
        "budget",
    }
    assert "stage names such as compose are invalid" in submit_schema["properties"]["workflow"][
        "description"
    ]
    assert "discriminator" in submit_schema["properties"]["input"]
    assert "oneOf" in submit_schema["properties"]["input"]
    nested_refs = {
        submit_schema["properties"][name]["$ref"].rsplit("/", 1)[-1]
        for name in ("brief", "output", "budget")
    }
    input_refs = {
        item["$ref"].rsplit("/", 1)[-1]
        for item in submit_schema["properties"]["input"]["oneOf"]
    }
    for definition_name in nested_refs | input_refs:
        assert submit_schema["$defs"][definition_name]["additionalProperties"] is False
    for command_tool in ("cancel_video_job", "approve_video_stage"):
        command_schema = by_name[command_tool].input_schema
        assert {"expected_sequence", "idempotency_key"}.issubset(
            command_schema["required"]
        )
    assert created.structured_content["status"] == "QUEUED"
    assert artifacts.structured_content["artifacts"] == []


@pytest.mark.asyncio
async def test_mcp_submit_accepts_authenticated_gateway_without_job_credential_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp import Client

    service = JobService(tmp_path / "jobs.sqlite3")
    monkeypatch.setenv("OPENMONTAGE_MCP_GATEWAY_SECRET", "gateway-secret")
    monkeypatch.delenv("OPENMONTAGE_MODEL_CREDENTIAL_BASE_URL", raising=False)
    caller_model_key = "sk-models-user-must-not-persist"
    attribution = gateway_attribution({
        "Authorization": f"Bearer {caller_model_key}",
        "X-Dofe-Mcp-Gateway-Secret": "gateway-secret",
        "X-Dofe-Auth-Verified": "models-api-key-v1",
        "X-Dofe-Api-Key-Id": "key-submit",
        "X-Dofe-Tenant-Id": "tenant-submit",
        "X-Dofe-Sso-Team-Id": "team-submit",
        "X-Request-Id": "request-submit",
    })
    assert attribution is not None

    async with Client(
        create_server(
            job_service=service,
            attribution_resolver=lambda _headers: attribution,
        )
    ) as client:
        result = await client.call_tool("submit_video_job", _request())

    assert result.is_error is False
    assert result.structured_content["status"] == "QUEUED"
    job_id = result.structured_content["jobId"]
    snapshot = service.get_job(job_id)
    assert snapshot.attribution == attribution
    assert caller_model_key not in snapshot.model_dump_json()
    assert all(
        caller_model_key not in event.model_dump_json()
        for event in service.list_events(job_id)
    )
    assert caller_model_key.encode() not in service.database_path.read_bytes()


@pytest.mark.asyncio
async def test_mcp_cancel_replays_the_same_sequence_fenced_command(tmp_path: Path) -> None:
    from mcp import Client

    service = JobService(tmp_path / "jobs.sqlite3")

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        created = await client.call_tool("submit_video_job", _request())
        job_id = created.structured_content["jobId"]
        command = {
            "job_id": job_id,
            "expected_sequence": 1,
            "idempotency_key": "mcp-cancel-job-at-sequence-1",
        }
        first = await client.call_tool("cancel_video_job", command)
        repeated = await client.call_tool("cancel_video_job", command)

    assert first.structured_content["lastSequence"] == 2
    assert repeated.structured_content["lastSequence"] == 2
    assert [event.sequence for event in service.list_events(job_id)] == [1, 2]


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
            {**_request(), "workflow": "compose"},
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
        result = await client.call_tool("submit_video_job", _request())

    assert result.is_error is True
    assert "Workflow 'framework-smoke' is unavailable" in result.content[0].text
    assert "internal path" not in result.content[0].text


@pytest.mark.asyncio
async def test_mcp_tool_invocation_without_stage_lease_returns_structured_error(
    tmp_path: Path,
) -> None:
    from mcp import Client

    service = JobService(tmp_path / "jobs.sqlite3")

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        result = await client.call_tool(
            "invoke_openmontage_tool",
            {
                "tool_name": "tts_selector",
                "operation": "generate",
                "inputs": {"text": "test"},
            },
        )

    assert result.is_error is False
    assert result.structured_content == {
        "success": False,
        "status": "failed",
        "error": {
            "code": "STAGE_LEASE_INVALID",
            "category": "tool_gateway",
            "message": (
                "job_id, stage, stage_attempt, lease_token and idempotency_key "
                "are required; obtain lease fields from begin_client_stage and "
                "supply a stable idempotency_key"
            ),
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["job_id", "stage", "idempotency_key"])
async def test_mcp_tool_invocation_rejects_whitespace_stage_context(
    tmp_path: Path,
    field: str,
) -> None:
    from mcp import Client

    service = JobService(tmp_path / "jobs.sqlite3")
    arguments = {
        "tool_name": "tts_selector",
        "operation": "generate",
        "inputs": {"text": "test"},
        "job_id": "job-present",
        "stage": "assets",
        "stage_attempt": 1,
        "lease_token": "lease-present",
        "idempotency_key": "invoke-whitespace-context",
    }
    arguments[field] = "   "

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        result = await client.call_tool("invoke_openmontage_tool", arguments)

    assert result.is_error is False
    assert result.structured_content["error"]["code"] == "STAGE_LEASE_INVALID"


@pytest.mark.asyncio
async def test_mcp_tool_invocation_with_unknown_job_returns_structured_error(
    tmp_path: Path,
) -> None:
    from mcp import Client

    service = JobService(tmp_path / "jobs.sqlite3")

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        result = await client.call_tool(
            "invoke_openmontage_tool",
            {
                "tool_name": "tts_selector",
                "operation": "generate",
                "inputs": {"text": "test"},
                "job_id": "missing-job",
                "stage": "assets",
                "stage_attempt": 1,
                "lease_token": "lease-missing-job",
                "idempotency_key": "invoke-missing-job",
            },
        )

    assert result.is_error is False
    assert result.structured_content["error"]["code"] == "OPENMONTAGE_JOB_NOT_FOUND"


@pytest.mark.asyncio
async def test_mcp_tool_invocation_with_invalid_lease_returns_structured_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp import Client
    from openmontage.job_service import ClientStageError

    service = JobService(tmp_path / "jobs.sqlite3")

    def reject_invalid_lease(*_args: object, **_kwargs: object) -> None:
        raise ClientStageError("STAGE_LEASE_INVALID", "client lease is no longer current")

    monkeypatch.setattr("openmontage.tool_gateway.ToolGateway.invoke", reject_invalid_lease)

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        created = await client.call_tool("submit_video_job", _request())
        result = await client.call_tool(
            "invoke_openmontage_tool",
            {
                "tool_name": "tts_selector",
                "operation": "generate",
                "inputs": {"text": "test"},
                "job_id": created.structured_content["jobId"],
                "stage": "research",
                "stage_attempt": 1,
                "lease_token": "stale-lease",
                "idempotency_key": "invoke-stale-lease",
            },
        )

    assert result.is_error is False
    assert result.structured_content["error"]["code"] == "STAGE_LEASE_INVALID"
    assert result.structured_content["error"]["category"] == "client_stage"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "expected_sequence", "error_code"),
    [
        ("no-such-stage", None, "OPENMONTAGE_JOB_STATE_INVALID"),
        ("research", 999, "OPENMONTAGE_JOB_CONFLICT"),
    ],
)
async def test_mcp_stage_begin_maps_job_state_errors(
    tmp_path: Path,
    stage: str,
    expected_sequence: int | None,
    error_code: str,
) -> None:
    from mcp import Client

    service = JobService(tmp_path / "jobs.sqlite3")

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        created = await client.call_tool("submit_video_job", _request())
        arguments: dict[str, object] = {
            "job_id": created.structured_content["jobId"],
            "stage": stage,
            "idempotency_key": f"begin-{stage}",
        }
        if expected_sequence is not None:
            arguments["expected_sequence"] = expected_sequence
        result = await client.call_tool("begin_client_stage", arguments)

    assert result.is_error is False
    assert result.structured_content["error"]["code"] == error_code
    assert result.structured_content["error"]["category"] == "job"


@pytest.mark.asyncio
async def test_mcp_stage_begin_returns_and_replays_explicit_execution_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp import Client

    service = JobService(tmp_path / "jobs.sqlite3")

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        created = await client.call_tool(
            "submit_video_job", {**_request(), "workflow": "animation"}
        )
        result = await client.call_tool(
            "begin_client_stage",
            {
                "job_id": created.structured_content["jobId"],
                "stage": "research",
                "idempotency_key": "begin-research-contract",
            },
        )
        original = result.structured_content

        def fail_manifest_load(_workflow: str) -> None:
            raise OSError("/data/private/pipeline_defs/animation.yaml")

        monkeypatch.setattr("lib.pipeline_loader.load_pipeline_readonly", fail_manifest_load)
        replay = await client.call_tool(
            "begin_client_stage",
            {
                "job_id": created.structured_content["jobId"],
                "stage": "research",
                "idempotency_key": "begin-research-contract",
            },
        )

    assert result.is_error is False
    assert replay.is_error is False
    assert replay.structured_content == original
    assert result.structured_content["stageContract"] == {
        "declaredTools": [],
        "gatewayTools": [],
        "produces": ["research_brief"],
        "humanApprovalRequired": False,
        "instructionFiles": [
            "AGENT_GUIDE.md",
            "pipeline_defs/animation.yaml",
            "skills/pipelines/animation/research-director.md",
            "skills/meta/checkpoint-protocol.md",
            "skills/meta/reviewer.md",
        ],
    }


def test_stage_contract_distinguishes_manifest_and_gateway_tool_names(tmp_path: Path) -> None:
    service = JobService(tmp_path / "jobs.sqlite3")
    request = {**_request(), "workflow": "avatar-spokesperson"}
    from openmontage.contracts import JobCreateRequest

    job = service.create_job(JobCreateRequest.model_validate(request), _attribution())

    contract = stage_execution_contract(service.get_job(job.job_id), "assets")

    assert contract["declaredTools"] == [
        "talking_head",
        "lip_sync",
        "tts_selector",
        "subtitle_gen",
        "image_selector",
        "audio_enhance",
        "video_selector",
    ]
    assert contract["gatewayTools"] == [
        "avatar_video",
        "tts_selector",
        "image_selector",
        "video_selector",
    ]


@pytest.mark.asyncio
async def test_mcp_stage_begin_rejects_unavailable_contract_before_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp import Client

    def fail_manifest_load(_workflow: str) -> None:
        raise OSError("/data/private/pipeline_defs/animation.yaml")

    service = JobService(tmp_path / "jobs.sqlite3")

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        created = await client.call_tool("submit_video_job", _request())
        job_id = created.structured_content["jobId"]
        monkeypatch.setattr("lib.pipeline_loader.load_pipeline_readonly", fail_manifest_load)
        result = await client.call_tool(
            "begin_client_stage",
            {
                "job_id": job_id,
                "stage": "research",
                "idempotency_key": "begin-research-contract-failure",
            },
        )

    assert result.is_error is False
    assert result.structured_content["error"] == {
        "code": "OPENMONTAGE_STAGE_CONTRACT_UNAVAILABLE",
        "category": "job",
        "message": "OpenMontage stage execution contract is unavailable",
    }
    assert "/data/private" not in str(result.structured_content)
    snapshot = service.get_job(job_id)
    assert snapshot.status == JobStatus.QUEUED
    assert snapshot.stages[0].status == StageStatus.PENDING
    assert snapshot.stages[0].attempt == 0


@pytest.mark.asyncio
async def test_mcp_instruction_read_maps_to_submission_provenance(tmp_path: Path) -> None:
    from mcp import Client

    service = JobService(tmp_path / "jobs.sqlite3")
    request = {**_request(), "workflow": "animation"}

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        created = await client.call_tool("submit_video_job", request)
        job_id = created.structured_content["jobId"]
        lease = await client.call_tool(
            "begin_client_stage",
            {"job_id": job_id, "stage": "research", "idempotency_key": "begin-read-submit"},
        )
        instruction = await client.call_tool(
            "read_openmontage_file", {"path": "AGENT_GUIDE.md"}
        )
        submitted = await client.call_tool(
            "submit_client_stage",
            {
                "job_id": lease.structured_content["jobId"],
                "stage": lease.structured_content["stage"],
                "stage_attempt": lease.structured_content["stageAttempt"],
                "status": "completed",
                "lease_token": lease.structured_content["leaseToken"],
                "idempotency_key": "submit-read-submit",
                "artifacts": {
                    "research_brief": {
                        "version": "1.0",
                        "topic": "Smoke",
                        "research_date": "2026-09-08",
                        "landscape": {
                            "existing_content": [
                                {
                                    "title": "Example",
                                    "source": "test",
                                    "angle": "test",
                                    "what_it_covers": "test",
                                }
                                for _ in range(3)
                            ],
                            "saturated_angles": [],
                            "underserved_gaps": ["Test gap"],
                        },
                        "data_points": [
                            {
                                "claim": f"Claim {index}",
                                "source_url": f"https://example.com/{index}",
                                "credibility": "secondary_source",
                            }
                            for index in range(3)
                        ],
                        "audience_insights": {
                            "common_questions": ["q1", "q2", "q3"],
                            "misconceptions": [],
                            "knowledge_level": "intermediate",
                        },
                        "angles_discovered": [
                            {
                                "name": f"Angle {index}",
                                "hook": "Hook",
                                "type": "evergreen",
                                "why_now": "Now",
                            }
                            for index in range(3)
                        ],
                        "sources": [
                            {
                                "url": f"https://example.com/{index}",
                                "title": f"Source {index}",
                                "used_for": "landscape",
                            }
                            for index in range(5)
                        ],
                    }
                },
                "instruction_provenance": [
                    {
                        "path": instruction.structured_content["relative_path"],
                        "content_hash": instruction.structured_content["content_hash"],
                    }
                ],
            },
        )

    assert submitted.is_error is False
    assert submitted.structured_content["stages"][0]["status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_mcp_stage_begin_hides_project_initialization_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp import Client

    def fail_project_init(*_args: object, **_kwargs: object) -> None:
        raise OSError("/data/private/projects/job/project.json")

    monkeypatch.setattr("lib.checkpoint.init_project", fail_project_init)
    service = JobService(tmp_path / "jobs.sqlite3")

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        created = await client.call_tool("submit_video_job", _request())
        result = await client.call_tool(
            "begin_client_stage",
            {
                "job_id": created.structured_content["jobId"],
                "stage": "research",
                "idempotency_key": "begin-research-init-failure",
            },
        )

    assert result.is_error is False
    assert result.structured_content["error"] == {
        "code": "PROJECT_INIT_FAILED",
        "category": "client_stage",
        "message": "OpenMontage could not initialize the project workspace",
    }
    assert "/data/private" not in str(result.structured_content)


@pytest.mark.asyncio
async def test_mcp_stage_submission_with_invalid_lease_returns_structured_error(
    tmp_path: Path,
) -> None:
    from mcp import Client

    service = JobService(tmp_path / "jobs.sqlite3")

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        created = await client.call_tool("submit_video_job", _request())
        job_id = created.structured_content["jobId"]
        lease = await client.call_tool(
            "begin_client_stage",
            {"job_id": job_id, "stage": "research", "idempotency_key": "begin-research"},
        )
        result = await client.call_tool(
            "submit_client_stage",
            {
                "job_id": job_id,
                "stage": "research",
                "stage_attempt": lease.structured_content["stageAttempt"],
                "status": "completed",
                "lease_token": "wrong-lease",
                "idempotency_key": "submit-research",
            },
        )

    assert result.is_error is False
    assert result.structured_content["error"]["code"] == "STAGE_LEASE_INVALID"
    assert result.structured_content["error"]["category"] == "client_stage"


@pytest.mark.asyncio
async def test_mcp_stage_submission_requires_canonical_artifact_wrapper(
    tmp_path: Path,
) -> None:
    from mcp import Client

    service = JobService(tmp_path / "jobs.sqlite3")
    request = {**_request(), "workflow": "animation"}

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        created = await client.call_tool("submit_video_job", request)
        job_id = created.structured_content["jobId"]
        lease = await client.call_tool(
            "begin_client_stage",
            {"job_id": job_id, "stage": "research", "idempotency_key": "begin-research"},
        )
        result = await client.call_tool(
            "submit_client_stage",
            {
                "job_id": job_id,
                "stage": "research",
                "stage_attempt": lease.structured_content["stageAttempt"],
                "status": "completed",
                "lease_token": lease.structured_content["leaseToken"],
                "idempotency_key": "submit-research",
                "artifacts": {"version": "1.0", "topic": "unwrapped brief"},
            },
        )

    assert result.is_error is False
    assert result.structured_content["error"]["code"] == "ARTIFACT_SCHEMA_INVALID"
    assert "canonical artifact 'research_brief'" in result.structured_content["error"]["message"]


@pytest.mark.asyncio
async def test_mcp_stage_submission_hides_checkpoint_write_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp import Client

    def fail_checkpoint_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("/data/private/jobs/secret-checkpoint.json")

    monkeypatch.setattr("lib.checkpoint.write_checkpoint", fail_checkpoint_write)
    service = JobService(tmp_path / "jobs.sqlite3")

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        created = await client.call_tool(
            "submit_video_job", {**_request(), "workflow": "animation"}
        )
        job_id = created.structured_content["jobId"]
        lease = await client.call_tool(
            "begin_client_stage",
            {"job_id": job_id, "stage": "research", "idempotency_key": "begin-research"},
        )
        result = await client.call_tool(
            "submit_client_stage",
            {
                "job_id": job_id,
                "stage": "research",
                "stage_attempt": lease.structured_content["stageAttempt"],
                "status": "in_progress",
                "lease_token": lease.structured_content["leaseToken"],
                "idempotency_key": "submit-research-progress",
            },
        )

    assert result.is_error is False
    assert result.structured_content["error"] == {
        "code": "CHECKPOINT_WRITE_FAILED",
        "category": "client_stage",
        "message": "OpenMontage could not persist the stage checkpoint",
    }
    assert "/data/private" not in result.content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "message_fragment"),
    [
        ("schemaVersion", True, "schemaVersion"),
        ("schemaVersion", 1.0, "schemaVersion"),
        ("clientRequestId", "   ", "clientRequestId"),
        ("clientRequestId", "r" * 257, "clientRequestId"),
        ("input", {"type": "artifact"}, "artifactId"),
        ("input", {"type": "text", "inlineText": "   "}, "inlineText"),
        ("input", {"type": "text", "inlineText": "t" * 100_001}, "inlineText"),
        ("brief", {"title": "Smoke", "durationSeconds": True}, "durationSeconds"),
        ("output", {"container": "mp4", "fps": True}, "fps"),
        ("budget", {}, "maxAmount"),
    ],
)
async def test_mcp_job_creation_rejects_invalid_request_contracts(
    tmp_path: Path,
    field: str,
    value: object,
    message_fragment: str,
) -> None:
    from mcp import Client

    service = JobService(tmp_path / "jobs.sqlite3")

    async with Client(
        create_server(job_service=service, attribution_resolver=lambda _headers: _attribution())
    ) as client:
        result = await client.call_tool(
            "submit_video_job",
            {**_request(), field: value},
        )

    assert result.is_error is True
    assert message_fragment in result.content[0].text

# ---------------------------------------------------------------------------
# Public contract: docs/0903 §3 envelope + §3.4 overview shape
# ---------------------------------------------------------------------------


def test_rest_overview_matches_public_contract(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    client.post("/api/v1/jobs", json=_request(), headers=_headers())
    second = client.post(
        "/api/v1/jobs",
        json={
            **_request(),
            "clientRequestId": "request-2",
            "brief": {"title": "Second brief"},
        },
        headers=_headers(),
    ).json()["data"]["jobId"]
    service.start_stage(second, "research")
    service.publish_artifact(
        second,
        PublishedArtifact(
            job_id=second,
            employee_artifact_id="eart-9",
            employee_id="employee-1",
            role="final_video",
            file_name="final.mp4",
            media_type="video/mp4",
            size_bytes=7,
            sha256="b" * 64,
            published_at="2026-08-05T10:00:09Z",
        ),
    )

    response = client.get("/api/v1/overview", headers=_headers())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["meta"]["source"] == "montage"
    assert body["meta"]["requestId"]
    assert body["meta"]["generatedAt"]
    data = body["data"]
    assert data["workspaceId"] == "ws-1"
    assert data["jobs"] == {
        "total": 2,
        "queued": 1,
        "running": 1,
        "completed": 0,
        "failed": 0,
        "statusCounts": {"queued": 1, "running": 1},
    }
    assert data["pendingApprovals"] == 0
    recent = data["recentJobs"][0]
    assert recent["jobId"] == second
    assert recent["title"] == "Second brief"
    assert recent["status"] == "running"
    assert recent["workflow"] == "framework-smoke"
    assert recent["currentStage"] == "research"
    assert data["artifacts"]["total"] == 1
    artifact = data["artifacts"]["recent"][0]
    assert artifact["artifactId"] == "eart-9"
    assert artifact["jobId"] == second
    assert set(artifact) == {
        "artifactId", "jobId", "role", "fileName", "mediaType",
        "sizeBytes", "sha256", "publishedAt",
    }
    assert data["health"]["service"] == "ok"


def test_rest_series_buckets_jobs_per_creation_day(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    day_one = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
    day_two = datetime(2026, 9, 2, 2, 0, tzinfo=timezone.utc)
    real_now = job_service_module._now

    def restore_clock() -> None:
        job_service_module._now = real_now

    job_service_module._now = lambda: day_one
    try:
        client.post("/api/v1/jobs", json=_request(), headers=_headers())
        job_service_module._now = lambda: day_two
        second = client.post(
            "/api/v1/jobs",
            json={**_request(), "clientRequestId": "request-2"},
            headers=_headers(),
        ).json()["data"]["jobId"]
        service.start_stage(second, "research")
    finally:
        restore_clock()

    response = client.get(
        "/api/v1/series"
        "?start=2026-09-01T00:00:00%2B08:00&end=2026-09-03T00:00:00%2B08:00",
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["meta"]["source"] == "montage"
    data = body["data"]
    assert data["workspaceId"] == "ws-1"
    assert data["grain"] == "day"
    assert data["timeZone"] == "Asia/Shanghai"
    # 09-01 逐日： queued=1；09-02： running=1；无缺日期
    assert data["days"] == [
        {
            "date": "2026-09-01",
            "total": 1,
            "queued": 1,
            "running": 0,
            "waiting_approval": 0,
            "succeeded": 0,
            "failed": 0,
            "cancel_requested": 0,
            "cancelled": 0,
        },
        {
            "date": "2026-09-02",
            "total": 1,
            "queued": 0,
            "running": 1,
            "waiting_approval": 0,
            "succeeded": 0,
            "failed": 0,
            "cancel_requested": 0,
            "cancelled": 0,
        },
    ]
    assert data["statusCounts"] == {
        "queued": 1,
        "running": 1,
        "waiting_approval": 0,
        "succeeded": 0,
        "failed": 0,
        "cancel_requested": 0,
        "cancelled": 0,
    }
    assert data["pendingApprovals"] == 0
    assert "employeeId" not in json.dumps(data)
    # 阶段统计覆盖窗口内每个作业的 research 阶段；未完成的样本无耗时分位数
    research = next(stage for stage in data["stageStats"] if stage["stage"] == "research")
    assert research["count"] == 2
    assert research["succeeded"] == 0
    assert research["durationP50Ms"] is None


def test_public_series_stage_stats_aggregates_stage_durations() -> None:
    from zoneinfo import ZoneInfo

    from openmontage.public_contract import public_series

    entries = [
        (
            "2026-09-01T02:00:00+00:00",
            "SUCCEEDED",
            [
                ("research", "SUCCEEDED", "2026-09-01T02:00:00+00:00", "2026-09-01T03:00:00+00:00"),
                ("script", "FAILED", "2026-09-01T03:00:00+00:00", "2026-09-01T03:30:00+00:00"),
            ],
        ),
        (
            "2026-09-02T02:00:00+00:00",
            "RUNNING",
            [("research", "RUNNING", "2026-09-02T02:00:00+00:00", None)],
        ),
    ]
    result = public_series(
        "ws-1",
        entries,
        start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end=datetime(2026, 9, 3, tzinfo=timezone.utc),
        time_zone=ZoneInfo("Asia/Shanghai"),
    )

    research = next(stage for stage in result["stageStats"] if stage["stage"] == "research")
    assert research == {
        "stage": "research",
        "count": 2,
        "succeeded": 1,
        "failed": 0,
        "durationP50Ms": 3600000.0,
        "durationP95Ms": 3600000.0,
    }
    script = next(stage for stage in result["stageStats"] if stage["stage"] == "script")
    assert script["count"] == 1
    assert script["failed"] == 1
    assert script["durationP50Ms"] == 1800000.0
    # 无 attribution / employeeId 泄漏
    assert "employeeId" not in json.dumps(result["stageStats"])


def test_rest_series_isolates_other_workspaces(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    client.post("/api/v1/jobs", json=_request(), headers=_headers())
    other_attribution = JobAttribution(
        workspace_id="ws-2",
        employee_id="employee-2",
        runtime_id="runtime-2",
        root_task_id="task-2",
        conversation_id="conversation-2",
        source_invocation_id="invocation-2",
        trace_id="trace-2",
    )
    encoded = base64.urlsafe_b64encode(
        json.dumps(other_attribution.to_wire(), separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    other_headers = {
        "Authorization": f"Bearer {SERVICE_TOKEN}",
        "X-Dofe-Job-Attribution": encoded,
    }

    response = client.get(
        "/api/v1/series"
        "?start=2026-09-01T00:00:00%2B08:00&end=2026-09-03T00:00:00%2B08:00",
        headers=other_headers,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["workspaceId"] == "ws-2"
    assert data["statusCounts"] == {
        key: 0
        for key in (
            "queued", "running", "waiting_approval", "succeeded",
            "failed", "cancel_requested", "cancelled",
        )
    }
    assert all(day["total"] == 0 for day in data["days"])


def test_rest_series_requires_authentication_and_valid_ranges(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    unauthenticated = client.get(
        "/api/v1/series"
        "?start=2026-09-01T00:00:00%2B08:00&end=2026-09-03T00:00:00%2B08:00",
    )
    assert unauthenticated.status_code == 401

    window = "?start=2026-09-01T00:00:00%2B08:00&end=2026-09-03T00:00:00%2B08:00"
    for query, headers in [
        ("", _headers()),  # 缺少 start/end
        ("?start=2026-09-03T00:00:00%2B08:00&end=2026-09-01T00:00:00%2B08:00", _headers()),
        # 窗口上限已放宽到 366 天（docs/0903/dashboard P2），399 天仍拒绝
        ("?start=2026-09-01T00:00:00%2B08:00&end=2027-10-05T00:00:00%2B08:00", _headers()),
        (f"{window}&timezone=America/New_York", _headers()),
        ("?start=2026-09-01&end=2026-09-03T00:00:00%2B08:00", _headers()),
    ]:
        response = client.get(f"/api/v1/series{query}", headers=headers)
        assert response.status_code == 422, query
        assert (
            response.json()["error"]["code"] == "OPENMONTAGE_VALIDATION_FAILED"
        ), query


def test_rest_detail_and_list_hide_internal_identity(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    job_id = client.post(
        "/api/v1/jobs", json=_request(), headers=_headers()
    ).json()["data"]["jobId"]

    detail = client.get(f"/api/v1/jobs/{job_id}", headers=_headers())
    listing = client.get("/api/v1/jobs", headers=_headers())

    assert detail.status_code == 200
    assert listing.status_code == 200
    serialized = json.dumps([detail.json(), listing.json()])
    assert "attribution" not in serialized
    assert "employeeId" not in serialized
    assert "ws-1" not in serialized
    assert str(tmp_path) not in serialized


def test_rest_overview_without_leases_reports_service_worker(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    client.post("/api/v1/jobs", json=_request(), headers=_headers())

    response = client.get("/api/v1/overview", headers=_headers())

    data = response.json()["data"]
    assert data["health"] == {
        "service": "ok",
        "workers": [{"id": "openmontage-mcp", "status": "online", "lastHeartbeatAt": ""}],
    }


def test_rest_overview_reports_stale_lease_as_degraded(tmp_path: Path) -> None:
    start = datetime(2026, 8, 5, 10, 0, 0, tzinfo=timezone.utc)
    service = JobService(tmp_path / "jobs.sqlite3")
    client = TestClient(
        build_http_app(
            job_service=service,
            attribution_resolver=TrustedAttributionResolver(SERVICE_TOKEN),
            now_fn=lambda: start + timedelta(minutes=5),
        )
    )
    job_id = client.post(
        "/api/v1/jobs", json=_request(), headers=_headers()
    ).json()["data"]["jobId"]
    service.start_stage(job_id, "research")
    service.claim_job(
        worker_id="worker-1", lease_duration=timedelta(seconds=1), now=start
    )

    response = client.get("/api/v1/overview", headers=_headers())

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["health"] == {
        "service": "degraded",
        "workers": [{"id": "worker-1", "status": "degraded", "lastHeartbeatAt": "2026-08-05T10:00:00+00:00"}],
    }


def test_rest_overview_reports_approval_wait_metrics(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    job_id = client.post(
        "/api/v1/jobs", json=_request(), headers=_headers()
    ).json()["data"]["jobId"]
    service.start_stage(job_id, "research")
    service.request_stage_approval(job_id, "research", reason="needs human review")

    response = client.get("/api/v1/overview", headers=_headers())

    assert response.status_code == 200
    approvals = response.json()["data"]["approvals"]
    assert approvals["pending"] == 1
    assert approvals["oldestWaitingAt"]
    assert approvals["waitP50Ms"] is not None
    assert approvals["waitP95Ms"] is not None
    assert "employeeId" not in json.dumps(approvals)


def test_rest_overview_without_waiting_jobs_has_empty_approval_times(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    client.post("/api/v1/jobs", json=_request(), headers=_headers())

    response = client.get("/api/v1/overview", headers=_headers())

    assert response.status_code == 200
    approvals = response.json()["data"]["approvals"]
    assert approvals["pending"] == 0
    # 无等待作业时必须是空串/None 值，绝不能序列化出字符串 "None"
    assert approvals["oldestWaitingAt"] == ""
    assert approvals["waitP50Ms"] is None


def test_rest_overview_is_scoped_to_workspace(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    client.post("/api/v1/jobs", json=_request(), headers=_headers())
    other = _attribution().model_copy(update={"workspace_id": "ws-2"})
    encoded = base64.urlsafe_b64encode(
        json.dumps(other.to_wire(), separators=(",", ":")).encode("utf-8")
    ).decode("ascii")

    response = client.get(
        "/api/v1/overview",
        headers={
            "Authorization": f"Bearer {SERVICE_TOKEN}",
            "X-Dofe-Job-Attribution": encoded,
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["workspaceId"] == "ws-2"
    assert data["jobs"]["total"] == 0
    assert data["artifacts"]["total"] == 0


def test_rest_overview_echoes_request_id(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.get(
        "/api/v1/overview",
        headers={**_headers(), "X-Request-Id": "req-dashboard-42"},
    )

    assert response.json()["meta"]["requestId"] == "req-dashboard-42"
