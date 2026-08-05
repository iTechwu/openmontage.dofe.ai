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
    assert "https://model.local.dofe.ai/api" in body
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
    assert "http://api:3101" in compose
    assert "modelsdofeai_default" in compose


@pytest.mark.asyncio
async def test_mcp_server_publishes_reference_clone_surface():
    from mcp import Client

    from openmontage.mcp_server import create_server

    async with Client(create_server()) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()

    assert {tool.name for tool in tools.tools} == {
        "openmontage_capabilities",
        "prepare_reference_clone",
        "reference_clone_status",
    }
    assert {str(resource.uri) for resource in resources.resources} == {
        "openmontage://reference-clone-guide"
    }
