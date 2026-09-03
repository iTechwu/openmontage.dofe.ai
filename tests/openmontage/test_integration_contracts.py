from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[2]


def test_agent_skills_and_mcp_configs_are_discoverable():
    codex_skill = ROOT / ".agents" / "skills" / "recreate-video" / "SKILL.md"
    claude_skill = ROOT / ".claude" / "skills" / "recreate-video" / "SKILL.md"
    assert codex_skill.is_file()
    assert claude_skill.is_file()
    body = codex_skill.read_text()
    assert "www.douyin.com" in body or "douyin.com" in body
    assert "GET /v1/models" in body
    assert "exact returned ID" in body
    assert "Never invoke a vendor-direct model tool" in body

    claude_mcp = json.loads((ROOT / ".mcp.json").read_text())
    assert "openmontage" in claude_mcp["mcpServers"]
    codex_config = tomllib.loads((ROOT / ".codex" / "config.toml").read_text())
    assert "openmontage" in codex_config["mcp_servers"]


def test_docker_contract_exposes_mcp_and_persists_projects():
    dockerfile = (ROOT / "Dockerfile").read_text()
    compose = (ROOT / "compose.yaml").read_text()
    assert "EXPOSE 8765" in dockerfile
    assert "/healthz" in dockerfile
    assert "./projects:/data/projects" in compose
    assert "DOFE_ENABLED: \"true\"" in compose
    assert "OPENMONTAGE_MODEL_CREDENTIAL_BASE_URL" in compose
    assert "DOFE_DOCKER_MODEL_BASE_URL" in compose
    assert "DOFE_DOCKER_INTERNAL_API_BASE_URL" in compose
    assert "https://ixicai.cn/api" in compose
    assert "http://api:3101" not in compose
    assert "DOFE_MODEL_API_KEY:" not in compose
    assert "INTERNAL_API_SECRET:" not in compose
    assert "modelsdofeai_default" in compose


@pytest.mark.asyncio
async def test_mcp_server_publishes_reference_clone_surface():
    from mcp import Client

    from openmontage.mcp_server import create_server

    async with Client(create_server()) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()

    assert {tool.name for tool in tools.tools} == {
        "approve_video_stage",
        "cancel_video_job",
        "cleanup_exports",
        "export_project_file",
        "get_video_job",
        "list_project_files",
        "list_video_artifacts",
        "list_video_job_events",
        "openmontage_capabilities",
        "invoke_openmontage_tool",
        "prepare_reference_clone",
        "reference_clone_status",
        "submit_video_job",
        "sync_project_exports",
        "begin_client_stage",
        "update_client_stage_progress",
        "submit_client_stage",
        "read_openmontage_file",
        "read_project_file",
        "read_project_image",
    }
    assert {str(resource.uri) for resource in resources.resources} == {
        "openmontage://reference-clone-guide"
    }


@pytest.mark.asyncio
async def test_mcp_server_returns_project_images_as_native_content(tmp_path, monkeypatch):
    from mcp import Client

    from openmontage import exchange
    from openmontage.mcp_server import create_server

    project = tmp_path / "clone-demo" / "reference" / "keyframes"
    project.mkdir(parents=True)
    (project / "frame_0000.jpg").write_bytes(b"\xff\xd8\xfffake")
    monkeypatch.setattr(exchange, "PROJECTS_DIR", tmp_path)

    async with Client(create_server()) as client:
        result = await client.call_tool(
            "read_project_image",
            {
                "project_id": "clone-demo",
                "relative_path": "reference/keyframes/frame_0000.jpg",
            },
        )

    assert [item.type for item in result.content] == ["text", "image"]
    assert result.content[1].mime_type == "image/jpeg"
