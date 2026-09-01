"""Tests for the client-side stage driver (plan §4, §9, §10, §16.4 scaffolding)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from openmontage.client_stage_driver import (
    PER_STAGE_INSTRUCTIONS,
    STANDARD_INSTRUCTION_FILES,
    ClientStageDriver,
    StageContext,
)
from openmontage.contracts import (
    JobAttribution,
    JobCreateRequest,
    JobStatus,
    StageStatus,
)
from openmontage.instruction_files import read_instruction_file
from openmontage.job_service import JobService


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


def _request(workflow: str = "animation") -> JobCreateRequest:
    return JobCreateRequest(
        client_request_id="request-1",
        workflow=workflow,
        input={"type": "text", "inlineText": "Explain the product"},
        brief={"title": "Product video", "durationSeconds": 30},
        output={"container": "mp4", "resolution": "1080x1920"},
        budget={"maxAmount": "20.00", "currency": "CNY"},
    )


def _service(tmp_path: Path) -> JobService:
    return JobService(tmp_path / "jobs.sqlite3", projects_dir=tmp_path / "projects")


def _driver(service: JobService, **kwargs) -> ClientStageDriver:
    return ClientStageDriver(service, gateway=object(), **kwargs)


def _research_brief() -> dict:
    def source(i: int) -> dict:
        return {"url": f"https://example.com/{i}", "title": f"Source {i}", "used_for": "landscape"}

    def data_point(i: int) -> dict:
        return {
            "claim": f"Claim {i}",
            "source_url": f"https://example.com/{i}",
            "credibility": "secondary_source",
        }

    return {
        "version": "1.0",
        "topic": "Product",
        "research_date": "2026-09-01",
        "landscape": {
            "existing_content": [
                {"title": "a", "source": "youtube", "angle": "x", "what_it_covers": "y"}
                for _ in range(3)
            ],
            "saturated_angles": ["done"],
            "underserved_gaps": ["gap"],
        },
        "data_points": [data_point(i) for i in range(3)],
        "audience_insights": {
            "common_questions": ["q1", "q2", "q3"],
            "misconceptions": [],
            "knowledge_level": "intermediate",
        },
        "angles_discovered": [
            {"name": f"a{i}", "hook": "h", "type": "evergreen", "why_now": "w"}
            for i in range(3)
        ],
        "sources": [source(i) for i in range(5)],
    }


# --- stage resolution --------------------------------------------------------


def test_resolve_current_stage_starts_at_research(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())
    driver = _driver(service)

    assert driver.resolve_current_stage(job.job_id) == "research"


def test_resolve_current_stage_returns_none_for_terminal_job(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())
    driver = _driver(service)

    lease = service.begin_client_stage(job.job_id, "research", idempotency_key="b")
    service.submit_client_stage(
        job.job_id, "research", stage_attempt=lease.stage_attempt, status="failed",
        lease_token=lease.lease_token, idempotency_key="s",
        metadata={"error": "boom"},
    )
    assert driver.resolve_current_stage(job.job_id) is None


# --- instruction reading -----------------------------------------------------


def test_read_stage_instructions_includes_manifest_director_and_meta(
    tmp_path: Path,
) -> None:
    driver = _driver(_service(tmp_path))
    bundle = driver.read_stage_instructions("animation", "research")

    paths = {entry["path"] for entry in bundle.provenance}
    assert "pipeline_defs/animation.yaml" in paths
    assert "skills/pipelines/animation/research-director.md" in paths
    assert "AGENT_GUIDE.md" in paths
    assert "skills/meta/checkpoint-protocol.md" in paths
    assert "skills/meta/reviewer.md" in paths
    # Provenance hashes match the live files.
    for entry in bundle.provenance:
        served = read_instruction_file(entry["path"])
        assert served["content_hash"] == entry["content_hash"]
        assert entry["path"] in bundle.contents
    assert len(bundle.contents) == len(bundle.provenance)


def test_read_stage_instructions_adds_per_stage_extras(tmp_path: Path) -> None:
    driver = _driver(_service(tmp_path))
    bundle = driver.read_stage_instructions("animation", "proposal")
    paths = {entry["path"] for entry in bundle.provenance}
    assert "skills/meta/animation-runtime-selector.md" in paths
    assert "skills/meta/taste-direction.md" in paths


def test_missing_required_instruction_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    driver = _driver(service)

    def _fail(path: str, **kwargs):
        if path == "pipeline_defs/animation.yaml":
            from openmontage.instruction_files import InstructionFileError

            raise InstructionFileError("INSTRUCTION_FILE_UNAVAILABLE", "boom")
        return read_instruction_file(path, **kwargs)

    monkeypatch.setattr(driver, "_read_instruction_file", _fail)
    with pytest.raises(Exception) as exc_info:
        driver.read_stage_instructions("animation", "research")
    assert getattr(exc_info.value, "code", None) == "INSTRUCTION_FILE_UNAVAILABLE"


def test_standard_and_per_stage_tables_are_declared() -> None:
    assert "AGENT_GUIDE.md" in STANDARD_INSTRUCTION_FILES
    assert "proposal" in PER_STAGE_INSTRUCTIONS
    assert "compose" in PER_STAGE_INSTRUCTIONS


# --- predecessor artifact -----------------------------------------------------


def test_read_predecessor_artifact_none_for_first_stage(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())
    driver = _driver(service)
    assert driver.read_predecessor_artifact(job.job_id, "research") is None


def test_read_predecessor_artifact_reads_checkpoint(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())
    driver = _driver(service)

    lease = service.begin_client_stage(job.job_id, "research", idempotency_key="b")
    service.submit_client_stage(
        job.job_id, "research", stage_attempt=lease.stage_attempt, status="completed",
        lease_token=lease.lease_token, idempotency_key="s",
        artifacts={"research_brief": _research_brief()},
    )

    predecessor = driver.read_predecessor_artifact(job.job_id, "proposal")
    assert predecessor is not None
    assert predecessor["stage"] == "research"
    assert "research_brief" in predecessor["artifacts"]


# --- drive_stage ---------------------------------------------------------------


def _research_handler(context: StageContext):
    # The handler receives live instructions and the lease; it returns a
    # completed research stage.
    assert context.stage == "research"
    assert "AGENT_GUIDE.md" in context.instructions.contents
    assert context.predecessor is None
    return "completed", {"research_brief": _research_brief()}, None


def test_drive_stage_runs_handler_and_submits_with_provenance(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())
    driver = _driver(service)

    outcome = driver.drive_stage(job.job_id, "research", _research_handler)

    assert outcome["status"] == "completed"
    assert outcome["waiting_approval"] is False
    snapshot = service.get_job(job.job_id)
    assert snapshot.stages[0].status == StageStatus.SUCCEEDED

    checkpoint = json.loads(
        (tmp_path / "projects" / job.job_id / "checkpoint_research.json").read_text("utf-8")
    )
    provenance = checkpoint["metadata"]["instruction_provenance"]
    assert {entry["path"] for entry in provenance} >= {
        "AGENT_GUIDE.md",
        "pipeline_defs/animation.yaml",
        "skills/pipelines/animation/research-director.md",
    }
    # Event records provenance too (plan §14).
    last = service.list_events(job.job_id)[-1]
    assert last.payload["instructionProvenance"] == provenance


def test_drive_stage_stops_at_approval_gate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())
    driver = _driver(service)

    # research is not gated; complete it first.
    driver.drive_stage(job.job_id, "research", _research_handler)

    def proposal_handler(context: StageContext):
        assert context.predecessor is not None
        assert context.predecessor["stage"] == "research"
        return "awaiting_human", {"proposal_packet": _proposal_packet()}, None

    outcome = driver.drive_stage(job.job_id, "proposal", proposal_handler)
    assert outcome["waiting_approval"] is True
    snapshot = service.get_job(job.job_id)
    assert snapshot.status == JobStatus.WAITING_APPROVAL
    assert snapshot.stages[1].status == StageStatus.WAITING_APPROVAL


# --- run loop ------------------------------------------------------------------


def _proposal_packet() -> dict:
    def concept(i: int) -> dict:
        return {
            "id": f"c{i}",
            "title": f"title {i}",
            "hook": "h",
            "narrative_structure": "story",
            "visual_approach": "v",
            "target_duration_seconds": 30,
            "why_this_works": "w",
        }

    return {
        "version": "1.0",
        "concept_options": [concept(i) for i in range(3)],
        "selected_concept": {"concept_id": "c0", "rationale": "r"},
        "production_plan": {
            "pipeline": "animation",
            "stages": [
                {"stage": "research", "tools": [], "approach": "web research"},
                {"stage": "proposal", "tools": [], "approach": "concept selection"},
            ],
            "render_runtime": "remotion",
        },
        "cost_estimate": {
            "total_estimated_usd": 1.0,
            "line_items": [
                {"tool": "video_compose", "operation": "render", "estimated_usd": 1.0}
            ],
            "budget_verdict": "within_budget",
        },
        "approval": {"status": "pending"},
    }


def test_run_stops_at_first_gate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())
    driver = _driver(service)

    handlers = {
        "research": _research_handler,
        "proposal": lambda ctx: ("awaiting_human", {"proposal_packet": _proposal_packet()}, None),
    }
    summary = driver.run(job.job_id, handlers)

    assert summary["completed"] is False
    assert summary["waiting_approval"] is True
    assert summary["waiting_stage"] == "proposal"
    assert [r["stage"] for r in summary["stages"]] == ["research", "proposal"]
    assert service.get_job(job.job_id).status == JobStatus.WAITING_APPROVAL


def test_run_completes_when_no_gates(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = service.create_job(_request(workflow="deterministic-video-smoke"), _attribution())
    driver = _driver(service)

    # deterministic-video-smoke has a single ungated compose stage, so the run
    # should drive it to completion.
    def compose_handler(ctx: StageContext):
        # CI produces the render; the client references it by relative path.
        render = ctx.service.projects_dir / ctx.job_id / "renders" / "final.mp4"
        render.parent.mkdir(parents=True, exist_ok=True)
        render.write_bytes(b"\x00\x00\x00 ftyp fake")
        return "completed", {"render_report": _render_report()}, None

    summary = driver.run(job.job_id, {"compose": compose_handler})
    assert summary["completed"] is True
    assert service.get_job(job.job_id).status == JobStatus.SUCCEEDED
    assert [r["stage"] for r in summary["stages"]] == ["compose"]


def _render_report() -> dict:
    return {
        "version": "1.0",
        "outputs": [
            {
                "path": "renders/final.mp4",
                "format": "mp4",
                "resolution": "1080x1920",
                "duration_seconds": 30,
            }
        ],
    }


def test_run_rejects_in_progress_handler(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = service.create_job(_request(workflow="deterministic-video-smoke"), _attribution())
    driver = _driver(service)

    def compose_handler(ctx: StageContext):
        return "in_progress", {}, {"partial_progress": {}}

    with pytest.raises(RuntimeError, match="in_progress"):
        driver.run(job.job_id, {"compose": compose_handler})


def test_run_missing_handler_raises(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = service.create_job(_request(workflow="deterministic-video-smoke"), _attribution())
    driver = _driver(service)

    with pytest.raises(KeyError, match="compose"):
        driver.run(job.job_id, {})


def test_drive_stage_replay_skips_handler(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())
    driver = _driver(service)

    calls: list[str] = []

    def handler(context: StageContext):
        calls.append("ran")
        return "completed", {"research_brief": _research_brief()}, None

    key = "stable-key"
    first = driver.drive_stage(job.job_id, "research", handler, idempotency_key=key)
    assert first["replayed"] is not True
    assert calls == ["ran"]

    # Replay with the same key: the handler must NOT run again.
    second = driver.drive_stage(job.job_id, "research", handler, idempotency_key=key)
    assert second["replayed"] is True
    assert calls == ["ran"]  # unchanged — no duplicate paid work
    assert second["status"] == "completed"


def test_resolve_current_stage_advances_after_completion(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())
    driver = _driver(service)

    assert driver.resolve_current_stage(job.job_id) == "research"
    driver.drive_stage(job.job_id, "research", _research_handler)
    assert driver.resolve_current_stage(job.job_id) == "proposal"
