from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_compose_wires_durable_jobs_and_a_dedicated_event_publisher() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    mcp = compose["services"]["openmontage-mcp"]
    publisher = compose["services"]["openmontage-events"]

    assert mcp["environment"]["OPENMONTAGE_JOB_DB"] == "/data/projects/.openmontage/jobs.sqlite3"
    assert "OPENMONTAGE_SERVICE_TOKEN" in mcp["environment"]
    assert publisher["command"][:2] == ["events", "publish"]
    assert publisher["environment"]["OPENMONTAGE_EVENT_ENDPOINT"]
    assert "OPENMONTAGE_EVENT_SIGNING_SECRET" in publisher["environment"]
    assert publisher["volumes"] == mcp["volumes"]


def test_example_environment_documents_all_agentspace_bridge_secrets() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    for name in (
        "OPENMONTAGE_SERVICE_TOKEN",
        "OPENMONTAGE_EVENT_ENDPOINT",
        "OPENMONTAGE_EVENT_SIGNING_SECRET",
        "OPENMONTAGE_JOB_DB",
    ):
        assert f"{name}=" in example
