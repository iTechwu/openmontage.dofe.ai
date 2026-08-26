from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_compose_wires_mcp_and_a_dedicated_event_publisher() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    mcp = services["openmontage-mcp"]
    publisher = services["openmontage-events"]

    assert mcp["environment"]["OPENMONTAGE_JOB_DB"] == "/data/projects/.openmontage/jobs.sqlite3"
    assert "OPENMONTAGE_SERVICE_TOKEN" in mcp["environment"]
    assert "/healthz" in mcp["healthcheck"]["test"][-1]
    assert "/v1/models" in mcp["healthcheck"]["test"][-1]
    assert "$${DOFE_MODEL_BASE_URL}" in mcp["healthcheck"]["test"][-1]
    assert "$${DOFE_MODEL_API_KEY}" in mcp["healthcheck"]["test"][-1]
    assert publisher["command"][:2] == ["events", "publish"]
    assert publisher["environment"]["OPENMONTAGE_EVENT_ENDPOINT"]
    assert "OPENMONTAGE_EVENT_SIGNING_SECRET" in publisher["environment"]
    assert publisher["volumes"] == mcp["volumes"]
    assert publisher["healthcheck"] == {"disable": True}


def test_compose_ships_no_in_container_job_worker() -> None:
    """The image is MCP-only: no bundled agent CLI means no worker service and
    no agent-executor environment leaking into the MCP deployment."""
    compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    services = yaml.safe_load(compose_text)["services"]

    assert "openmontage-worker" not in services
    assert "worker" not in compose_text
    for forbidden in (
        "OPENMONTAGE_AGENT_EXECUTOR_JSON",
        "OPENMONTAGE_AGENT_TIMEOUT_SECONDS",
        "OPENMONTAGE_AGENT_MODEL_ID",
    ):
        assert forbidden not in compose_text


def test_example_environment_documents_agentspace_bridge_secrets() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    for name in (
        "OPENMONTAGE_SERVICE_TOKEN",
        "OPENMONTAGE_EVENT_ENDPOINT",
        "OPENMONTAGE_EVENT_SIGNING_SECRET",
        "OPENMONTAGE_JOB_DB",
        "OPENMONTAGE_MODEL_CREDENTIAL_BASE_URL",
    ):
        assert f"{name}=" in example


def test_image_is_mcp_only_and_does_not_bundle_an_agent_cli() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    services = yaml.safe_load(compose)["services"]

    assert "codex" not in dockerfile.lower()
    assert "org.opencontainers.image.revision=${OPENMONTAGE_IMAGE_REVISION}" in dockerfile
    assert services["openmontage-events"]["build"]["args"]["OPENMONTAGE_IMAGE_REVISION"] == (
        "${OPENMONTAGE_IMAGE_REVISION:-unknown}"
    )
    assert "OPENMONTAGE_IMAGE_REVISION=\"$(IMAGE_REVISION)\" docker compose build" in makefile
    assert "hyperframes telemetry disable" in dockerfile
    assert "HYPERFRAMES_NO_TELEMETRY=1" in dockerfile
    assert "ONNXRUNTIME_NODE_INSTALL_CUDA=skip" in dockerfile
    assert "chmod -R a-w /app" in dockerfile
    assert "ln -s /data/cache/remotion-webpack" in dockerfile
    assert "!remotion-composer/public/fonts/**" in dockerignore
    assert "HYPERFRAMES_BROWSER_PATH=/app/remotion-composer/node_modules/.remotion/" in dockerfile
    for package in ("unzip", "libnspr4", "libnss3"):
        assert package in dockerfile
    assert services["openmontage-mcp"]["shm_size"] == "512mb"
