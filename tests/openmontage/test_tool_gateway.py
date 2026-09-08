from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from openmontage.contracts import JobAttribution, JobCreateRequest
from openmontage.job_service import ClientStageError, JobService
from openmontage.tool_gateway import (
    ALLOWED_TOOLS,
    ToolGateway,
    ToolGatewayError,
    gateway_tools_for_declared,
    stage_allows_gateway_tool,
)
from tools.base_tool import BaseTool, ToolResult


def _job_attribution() -> JobAttribution:
    return JobAttribution(
        workspace_id="ws-1",
        employee_id="employee-1",
        runtime_id="runtime-1",
        root_task_id="task-1",
        conversation_id="conversation-1",
        source_invocation_id="invocation-1",
        trace_id="trace-1",
    )


class _FakeTool(BaseTool):
    name = "image_selector"
    capability = "image_generation"
    provider = "selector"
    input_schema = {"type": "object", "properties": {"output_path": {"type": "string"}}}

    def execute(self, inputs: dict) -> ToolResult:
        target = Path(inputs["output_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-image")
        return ToolResult(success=True, data={"output_path": str(target)}, artifacts=[str(target)])


class _FakeRegistry:
    def __init__(self) -> None:
        self.tool = _FakeTool()

    def ensure_discovered(self) -> None:
        return None

    def get(self, name: str):
        return self.tool if name == "image_selector" else None


class _FakeDb:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.conn.commit()
        return False

    def execute(self, *args):
        return self.conn.execute(*args)


class _FakeService:
    def __init__(self, root: Path) -> None:
        self.projects_dir = root
        self.snapshot = SimpleNamespace(
            workflow=SimpleNamespace(name="animation"),
            status="RUNNING",
        )
        self.db = _FakeDb()

    def _connect(self):
        return self.db

    def get_job(self, job_id: str):
        return self.snapshot

    @staticmethod
    def _begin_write(connection):
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _require_client_lease(*args, **kwargs):
        return {
            "response_json": json.dumps(
                {
                    "stageContract": {
                        "gatewayTools": ["image_selector", "video_selector"]
                    }
                }
            )
        }


@pytest.fixture()
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolGateway:
    monkeypatch.setattr("openmontage.tool_gateway.registry", _FakeRegistry())
    return ToolGateway(_FakeService(tmp_path / "projects"))


def _invoke(gateway: ToolGateway, *, key: str = "call-1", inputs: dict | None = None):
    return gateway.invoke(
        tool_name="image_selector",
        operation="generate",
        inputs={"output_path": "assets/images/one.png"} if inputs is None else inputs,
        job_id="job-1",
        stage="assets",
        stage_attempt=1,
        lease_token="lease-1",
        idempotency_key=key,
    )


def test_catalog_exposes_logical_tools_only(gateway: ToolGateway) -> None:
    result = gateway.invoke(tool_name="", operation="catalog", inputs={})
    names = {entry["name"] for entry in result["tools"]}
    assert "image_selector" in names
    assert "dofe_image" not in names


def test_dofe_logical_aliases_never_dispatch_to_direct_provider(gateway: ToolGateway) -> None:
    from openmontage.tool_gateway import TOOL_ALIASES

    assert TOOL_ALIASES["music_gen"] == "dofe_music"
    assert TOOL_ALIASES["avatar_video"] == "dofe_avatar"
    assert TOOL_ALIASES["transcriber"] == "dofe_stt"


def test_generate_rewrites_path_and_returns_relative_artifact(gateway: ToolGateway) -> None:
    result = _invoke(gateway)
    assert result["success"] is True
    assert result["artifacts"][0]["path"] == "assets/images/one.png"
    assert (gateway.service.projects_dir / "job-1/assets/images/one.png").is_file()
    assert "/projects/" not in str(result)


def test_same_key_replays_without_running_tool_again(gateway: ToolGateway) -> None:
    first = _invoke(gateway, key="same")
    second = _invoke(gateway, key="same")
    assert second == first


def test_same_key_with_different_inputs_is_rejected(gateway: ToolGateway) -> None:
    _invoke(gateway, key="same")
    with pytest.raises(ToolGatewayError) as exc:
        _invoke(gateway, key="same", inputs={"output_path": "assets/images/two.png"})
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_path_traversal_is_rejected(gateway: ToolGateway) -> None:
    with pytest.raises(ToolGatewayError) as exc:
        _invoke(gateway, inputs={"output_path": "../outside.png"})
    assert exc.value.code == "PATH_OUTSIDE_REPOSITORY"


def test_plural_media_paths_are_rewritten_and_checked(gateway: ToolGateway) -> None:
    with pytest.raises(ToolGatewayError) as exc:
        _invoke(gateway, inputs={"output_path": "assets/images/one.png", "image_paths": ["../escape.png"]})
    assert exc.value.code == "PATH_OUTSIDE_REPOSITORY"


def test_generation_requires_explicit_project_output_path(gateway: ToolGateway) -> None:
    with pytest.raises(ToolGatewayError) as exc:
        _invoke(gateway, inputs={})
    assert exc.value.code == "TOOL_INPUT_INVALID"


def test_tool_not_declared_for_stage_is_rejected(gateway: ToolGateway) -> None:
    with pytest.raises(ToolGatewayError) as exc:
        gateway.invoke(
            tool_name="video_compose", operation="generate", inputs={},
            job_id="job-1", stage="assets", stage_attempt=1,
            lease_token="lease-1", idempotency_key="compose-1",
        )
    assert exc.value.code == "TOOL_NOT_ALLOWED"


class _CapturingTool(BaseTool):
    """Tool that records the inputs handed to execute()."""

    name = "video_selector"
    capability = "video_generation"
    provider = "selector"
    input_schema = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video", "reference_to_video", "rank", "preflight"],
                "default": "text_to_video",
            },
            "output_path": {"type": "string"},
        },
    }
    received: dict | None = None

    def execute(self, inputs: dict) -> ToolResult:
        type(self).received = dict(inputs)
        target = Path(inputs["output_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-media")
        return ToolResult(success=True, data={"output_path": str(target)}, artifacts=[str(target)])


class _SingleToolRegistry:
    def __init__(self, tool: BaseTool) -> None:
        self.tool = tool

    def ensure_discovered(self) -> None:
        return None

    def get(self, name: str):
        return self.tool


def _gateway_with(tool: BaseTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolGateway:
    monkeypatch.setattr("openmontage.tool_gateway.registry", _SingleToolRegistry(tool))
    return ToolGateway(_FakeService(tmp_path / "projects"))


class _RaisingTool(_FakeTool):
    def execute(self, inputs: dict) -> ToolResult:
        raise RuntimeError("Bearer provider-secret at /data/private/tool.log")


class _FailedResultTool(_FakeTool):
    def execute(self, inputs: dict) -> ToolResult:
        return ToolResult(
            success=False,
            data={
                "stderr": "Bearer provider-secret at /data/private/provider-stderr.log",
                "nested": {"stdout": "provider-secret"},
            },
            artifacts=["/data/private/provider-output.mp4"],
            error="Bearer provider-secret at /data/private/provider-response.json",
        )


class _InconsistentFailedResultTool(_FailedResultTool):
    def execute(self, inputs: dict) -> ToolResult:
        result = super().execute(inputs)
        result.success = True
        return result


@pytest.mark.parametrize(
    "tool_type", [_RaisingTool, _FailedResultTool, _InconsistentFailedResultTool]
)
def test_tool_failures_do_not_expose_provider_details(
    tool_type: type[BaseTool],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway = _gateway_with(tool_type(), tmp_path, monkeypatch)

    result = _invoke(gateway)

    assert result["success"] is False
    assert result["error"] == {
        "code": "TOOL_EXECUTION_FAILED",
        "category": "tool",
        "message": "OpenMontage tool execution failed; retry or choose another allowed tool",
    }
    assert "provider-secret" not in str(result)
    assert "/data/private" not in str(result)
    assert "provider-secret" not in caplog.text
    assert "/data/private" not in caplog.text
    assert result["data"] == {}
    assert result["artifacts"] == []


def test_failed_tool_result_does_not_materialize_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_read = False

    def track_artifact(_value: str, _project_dir: Path) -> None:
        nonlocal artifact_read
        artifact_read = True

    monkeypatch.setattr("openmontage.tool_gateway._artifact", track_artifact)
    gateway = _gateway_with(_FailedResultTool(), tmp_path, monkeypatch)

    result = _invoke(gateway)

    assert result["success"] is False
    assert artifact_read is False


def test_declared_avatar_tools_map_to_exact_gateway_names() -> None:
    declared = [
        "talking_head",
        "lip_sync",
        "tts_selector",
        "subtitle_gen",
        "image_selector",
        "audio_enhance",
        "video_selector",
    ]

    assert gateway_tools_for_declared(declared) == [
        "avatar_video",
        "tts_selector",
        "image_selector",
        "video_selector",
    ]
    assert stage_allows_gateway_tool(declared, "avatar_video") is True
    assert stage_allows_gateway_tool(declared, "talking_head") is False


def test_invoke_uses_persisted_lease_contract_when_manifest_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_manifest_load(_workflow: str) -> None:
        raise OSError("/data/private/pipeline_defs/animation.yaml")

    monkeypatch.setattr("lib.pipeline_loader.load_pipeline_readonly", fail_manifest_load)
    gateway = _gateway_with(_FakeTool(), tmp_path, monkeypatch)

    result = _invoke(gateway)

    assert result["success"] is True


def test_real_job_service_begin_contract_authorizes_gateway_invoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = JobService(
        tmp_path / "jobs.sqlite3", projects_dir=tmp_path / "projects"
    )
    job = service.create_job(
        JobCreateRequest(
            client_request_id="gateway-contract-integration",
            workflow="framework-smoke",
            input={"type": "text", "inlineText": "Smoke"},
            brief={"title": "Smoke"},
            output={"container": "mp4"},
            budget={"maxAmount": "1.00", "currency": "CNY"},
        ),
        JobAttribution(
            workspace_id="ws-1",
            employee_id="employee-1",
            runtime_id="runtime-1",
            root_task_id="task-1",
            conversation_id="conversation-1",
            source_invocation_id="invocation-1",
            trace_id="trace-1",
        ),
    )
    lease = service.begin_client_stage(
        job.job_id,
        "research",
        idempotency_key="begin-gateway-contract-integration",
        stage_contract_factory=lambda _snapshot, _stage: {
            "gatewayTools": ["image_selector"]
        },
    )
    monkeypatch.setattr("openmontage.tool_gateway.registry", _FakeRegistry())
    gateway = ToolGateway(service)

    result = gateway.invoke(
        tool_name="image_selector",
        operation="generate",
        inputs={"output_path": "assets/images/integration.png"},
        job_id=job.job_id,
        stage="research",
        stage_attempt=lease.stage_attempt,
        lease_token=lease.lease_token,
        idempotency_key="invoke-gateway-contract-integration",
    )

    assert result["success"] is True
    assert result["artifacts"][0]["path"] == "assets/images/integration.png"


def test_expired_client_lease_cannot_invoke_gateway_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = JobService(
        tmp_path / "jobs.sqlite3", projects_dir=tmp_path / "projects"
    )
    job = service.create_job(
        JobCreateRequest(
            client_request_id="expired-gateway-lease",
            workflow="framework-smoke",
            input={"type": "text", "inlineText": "Smoke"},
            brief={"title": "Smoke"},
            output={"container": "mp4"},
            budget={"maxAmount": "1.00", "currency": "CNY"},
        ),
        _job_attribution(),
    )
    lease = service.begin_client_stage(
        job.job_id,
        "research",
        idempotency_key="begin-expired-gateway-lease",
        now=datetime.now(timezone.utc) - timedelta(hours=2),
        lease_duration=timedelta(minutes=30),
        stage_contract_factory=lambda _snapshot, _stage: {
            "gatewayTools": ["image_selector"]
        },
    )
    monkeypatch.setattr("openmontage.tool_gateway.registry", _FakeRegistry())
    gateway = ToolGateway(service)

    with pytest.raises(ClientStageError) as exc_info:
        gateway.invoke(
            tool_name="image_selector",
            operation="generate",
            inputs={"output_path": "assets/images/expired.png"},
            job_id=job.job_id,
            stage="research",
            stage_attempt=lease.stage_attempt,
            lease_token=lease.lease_token,
            idempotency_key="invoke-expired-gateway-lease",
        )

    assert exc_info.value.code == "STAGE_LEASE_EXPIRED"
    assert not (service.projects_dir / job.job_id / "assets/images/expired.png").exists()


def test_legacy_active_lease_contract_is_backfilled_before_invoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = JobService(
        tmp_path / "jobs.sqlite3", projects_dir=tmp_path / "projects"
    )
    job = service.create_job(
        JobCreateRequest(
            client_request_id="legacy-gateway-lease",
            workflow="framework-smoke",
            input={"type": "text", "inlineText": "Smoke"},
            brief={"title": "Smoke"},
            output={"container": "mp4"},
            budget={"maxAmount": "1.00", "currency": "CNY"},
        ),
        _job_attribution(),
    )
    lease = service.begin_client_stage(
        job.job_id,
        "research",
        idempotency_key="begin-legacy-gateway-lease",
    )
    assert lease.stage_contract is None
    monkeypatch.setattr("openmontage.tool_gateway.registry", _FakeRegistry())
    contract = {
        "declaredTools": ["image_selector"],
        "gatewayTools": ["image_selector"],
        "produces": [],
        "humanApprovalRequired": False,
        "instructionFiles": [],
    }
    gateway = ToolGateway(
        service,
        stage_contract_factory=lambda _snapshot, _stage: contract,
    )

    result = gateway.invoke(
        tool_name="image_selector",
        operation="generate",
        inputs={"output_path": "assets/images/legacy.png"},
        job_id=job.job_id,
        stage="research",
        stage_attempt=lease.stage_attempt,
        lease_token=lease.lease_token,
        idempotency_key="invoke-legacy-gateway-lease",
    )

    assert result["success"] is True
    with service._connect() as connection:
        row = service._fetch_active_client_lease(connection, job.job_id, "research")
    assert row is not None
    assert json.loads(row["response_json"])["stageContract"] == contract


def test_every_manifest_gateway_tool_uses_the_gateway_allowlist() -> None:
    from lib.pipeline_loader import list_pipelines, load_pipeline_readonly

    for workflow in list_pipelines():
        manifest = load_pipeline_readonly(workflow)
        for stage in manifest["stages"]:
            declared = stage.get("tools_available", [])
            gateway_tools = gateway_tools_for_declared(declared)
            assert set(gateway_tools) <= ALLOWED_TOOLS
            assert all(
                stage_allows_gateway_tool(declared, tool_name)
                for tool_name in gateway_tools
            )


def test_generate_operation_not_injected_when_tool_enum_rejects_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _CapturingTool()
    gateway = _gateway_with(tool, tmp_path, monkeypatch)
    _CapturingTool.received = None
    result = gateway.invoke(
        tool_name="video_selector", operation="generate",
        inputs={"output_path": "assets/video/one.mp4"},
        job_id="job-1", stage="assets", stage_attempt=1,
        lease_token="lease-1", idempotency_key="video-1",
    )
    assert result["success"] is True
    assert _CapturingTool.received is not None
    # "generate" must NOT leak into a tool whose operation enum rejects it.
    assert "operation" not in _CapturingTool.received


def test_generate_operation_injected_when_tool_enum_accepts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _CapturingTool()
    tool.input_schema = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["generate", "rank"], "default": "generate"},
            "output_path": {"type": "string"},
        },
    }
    gateway = _gateway_with(tool, tmp_path, monkeypatch)
    _CapturingTool.received = None
    result = gateway.invoke(
        tool_name="video_selector", operation="generate",
        inputs={"output_path": "assets/video/one.mp4"},
        job_id="job-1", stage="assets", stage_attempt=1,
        lease_token="lease-1", idempotency_key="video-2",
    )
    assert result["success"] is True
    assert _CapturingTool.received is not None
    assert _CapturingTool.received.get("operation") == "generate"
