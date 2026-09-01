from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from openmontage.tool_gateway import ToolGateway, ToolGatewayError
from tools.base_tool import BaseTool, ToolResult


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
        return None


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
