from __future__ import annotations

import json
from pathlib import Path

from openmontage.contracts import JobAttribution, JobCreateRequest
from openmontage.job_service import WORKSPACE_MAP_NAME, JobService


def _attribution(workspace_id: str = "ws-1") -> JobAttribution:
    return JobAttribution(
        workspace_id=workspace_id,
        employee_id="employee-1",
        runtime_id="runtime-1",
        root_task_id="task-1",
        conversation_id="conversation-1",
        source_invocation_id="invocation-1",
        trace_id="trace-1",
    )


def _create_job(service: JobService, client_request_id: str):
    request = JobCreateRequest.model_validate(
        {
            "schemaVersion": 1,
            "clientRequestId": client_request_id,
            "workflow": "framework-smoke",
            "input": {"type": "text", "inlineText": "Smoke"},
            "brief": {"title": "Smoke"},
            "output": {"container": "mp4"},
            "budget": {"maxAmount": "1.00", "currency": "CNY"},
        }
    )
    return service.create_job(request, _attribution())


def _map_path(root: Path) -> Path:
    return root / ".openmontage" / WORKSPACE_MAP_NAME


def test_workspace_map_is_exported_and_self_heals(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    service = JobService(root / ".openmontage" / "jobs.sqlite3")
    # Startup export with no jobs writes an empty manifest.
    assert json.loads(_map_path(root).read_text()) == {}

    job_id = _create_job(service, "request-1").job_id
    other = JobService(root / ".openmontage" / "jobs.sqlite3")
    _create_job_other_workspace(other, "request-2")

    mapping = json.loads(_map_path(root).read_text())
    assert set(mapping.values()) == {"ws-1", "ws-2"}
    assert mapping[job_id] == "ws-1"

    # A corrupt manifest is rewritten on the next service construction.
    _map_path(root).write_text("{corrupt")
    JobService(root / ".openmontage" / "jobs.sqlite3")
    assert json.loads(_map_path(root).read_text()) == mapping


def test_workspace_map_export_failure_never_breaks_submission(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "projects"
    service = JobService(root / ".openmontage" / "jobs.sqlite3")

    def broken_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only volume")

    monkeypatch.setattr("openmontage.job_service.os.replace", broken_replace)

    snapshot = _create_job(service, "request-1")

    # The submission committed; only the export warned.
    assert snapshot.job_id


def _create_job_other_workspace(service: JobService, client_request_id: str):
    request = JobCreateRequest.model_validate(
        {
            "schemaVersion": 1,
            "clientRequestId": client_request_id,
            "workflow": "framework-smoke",
            "input": {"type": "text", "inlineText": "Smoke"},
            "brief": {"title": "Smoke"},
            "output": {"container": "mp4"},
            "budget": {"maxAmount": "1.00", "currency": "CNY"},
        }
    )
    return service.create_job(request, _attribution("ws-2"))
