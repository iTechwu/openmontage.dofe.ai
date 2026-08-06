from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from openmontage.contracts import JobEvent


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "schemas" / "integration" / "openmontage-job-event-v1.schema.json"
FIXTURE = ROOT / "tests" / "fixtures" / "openmontage" / "job-created-v1.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_job_event_v1_fixture_matches_the_published_json_schema() -> None:
    schema = _load(SCHEMA)
    fixture = _load(FIXTURE)

    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(instance=fixture, schema=schema)


def test_job_event_v1_fixture_round_trips_through_the_runtime_contract() -> None:
    fixture = _load(FIXTURE)

    assert JobEvent.model_validate(fixture).to_wire() == fixture
