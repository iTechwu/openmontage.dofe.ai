"""End-to-end client-stage pipeline test (plan §16.4).

Drives a full animation Job through every stage using a fake Gateway (no real
generation): create -> research -> proposal (approval) -> script -> scene_plan
-> assets -> edit -> compose -> publish -> Job completed. Media files are fake
bytes written to the project directory (standing in for CI media execution),
and every submission carries schema-valid canonical artifacts + instruction
provenance.
"""

from __future__ import annotations

import json
from pathlib import Path

from openmontage.client_stage_driver import ClientStageDriver, StageContext
from openmontage.contracts import (
    ApprovalStatus,
    JobAttribution,
    JobCreateRequest,
    JobStatus,
    StageStatus,
)
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


def _request() -> JobCreateRequest:
    return JobCreateRequest(
        client_request_id="request-e2e",
        workflow="animation",
        input={"type": "text", "inlineText": "Explain the product"},
        brief={"title": "Product video", "durationSeconds": 30},
        output={"container": "mp4", "resolution": "1080x1920"},
        budget={"maxAmount": "20.00", "currency": "CNY"},
    )


def _service(tmp_path: Path) -> JobService:
    return JobService(tmp_path / "jobs.sqlite3", projects_dir=tmp_path / "projects")


# --- fake gateway (stands in for the CI media tools) -------------------------


class FakeGateway:
    """Simulates the CI media execution layer without real generation.

    Each call writes a small fake media file into the project directory and
    returns metadata the client records in its artifacts — mirroring how a
    selector/compose tool behaves while keeping the test free of paid calls.
    """

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        self.calls: list[str] = []

    def generate_image(self, scene_id: str) -> dict:
        self.calls.append(f"image:{scene_id}")
        rel = f"assets/images/{scene_id}.png"
        target = self.project_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG fake")
        return {"path": rel, "type": "image", "scene_id": scene_id}

    def generate_narration(self, script_id: str) -> dict:
        self.calls.append(f"narration:{script_id}")
        rel = f"assets/audio/{script_id}.wav"
        target = self.project_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"RIFF fake")
        return {"path": rel, "type": "narration", "scene_id": script_id}

    def compose(self, render_name: str) -> dict:
        self.calls.append(f"compose:{render_name}")
        rel = f"renders/{render_name}.mp4"
        target = self.project_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00\x00\x00\x18ftyp fake")
        return {"path": rel}


# --- schema-valid canonical artifacts ----------------------------------------


def _research_brief() -> dict:
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
        "data_points": [
            {
                "claim": f"c{i}",
                "source_url": f"https://example.com/{i}",
                "credibility": "secondary_source",
            }
            for i in range(3)
        ],
        "audience_insights": {
            "common_questions": ["q1", "q2", "q3"],
            "misconceptions": [],
            "knowledge_level": "intermediate",
        },
        "angles_discovered": [
            {"name": f"a{i}", "hook": "h", "type": "evergreen", "why_now": "w"}
            for i in range(3)
        ],
        "sources": [
            {"url": f"https://example.com/{i}", "title": f"S{i}", "used_for": "landscape"}
            for i in range(5)
        ],
    }


def _proposal_packet() -> dict:
    return {
        "version": "1.0",
        "concept_options": [
            {
                "id": f"c{i}",
                "title": f"title {i}",
                "hook": "h",
                "narrative_structure": "story",
                "visual_approach": "v",
                "target_duration_seconds": 30,
                "why_this_works": "w",
            }
            for i in range(3)
        ],
        "selected_concept": {"concept_id": "c0", "rationale": "r"},
        "production_plan": {
            "pipeline": "animation",
            "stages": [
                {"stage": "research", "tools": [], "approach": "research"},
                {"stage": "proposal", "tools": [], "approach": "plan"},
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


def _script() -> dict:
    return {
        "version": "1.0",
        "title": "Product video",
        "total_duration_seconds": 30,
        "sections": [
            {"id": "s1", "text": "Intro", "start_seconds": 0, "end_seconds": 10},
            {"id": "s2", "text": "Body", "start_seconds": 10, "end_seconds": 30},
        ],
    }


def _scene_plan() -> dict:
    return {
        "version": "1.0",
        "scenes": [
            {
                "id": "scene1",
                "type": "animation",
                "description": "opening",
                "start_seconds": 0,
                "end_seconds": 10,
            },
            {
                "id": "scene2",
                "type": "text_card",
                "description": "stat",
                "start_seconds": 10,
                "end_seconds": 30,
            },
        ],
    }


# --- E2E helper: drive the pipeline, auto-approving gates --------------------


def _run_with_approval(
    service: JobService,
    driver: ClientStageDriver,
    job_id: str,
    handlers: dict,
) -> list[dict]:
    """Drive stages to completion, approving each gate as it appears.

    Each ``drive_stage`` call is a distinct business operation, so it gets a
    fresh idempotency key — a gated stage is two operations (submit
    awaiting_human, then submit completed after approval) and must not share a
    key, or the second begin would replay the first.
    """
    outcomes: list[dict] = []
    counter = 0
    # Each stage needs at most two drive calls (awaiting_human, then completed)
    # plus one approval — an upper bound guards against a buggy handler looping
    # forever instead of failing fast.
    stage_count = len(service.get_job(job_id).stages)
    max_iterations = stage_count * 3 + 2
    while counter <= max_iterations:
        stage = driver.resolve_current_stage(job_id)
        if stage is None:
            return outcomes
        snapshot = service.get_job(job_id)
        stage_snap = next((s for s in snapshot.stages if s.code == stage), None)
        if stage_snap is not None and stage_snap.status == StageStatus.WAITING_APPROVAL:
            service.resolve_stage_approval(
                job_id,
                stage,
                approved=True,
                expected_sequence=snapshot.last_sequence,
                idempotency_key=f"e2e-approve-{stage}",
            )
            continue
        counter += 1
        outcome = driver.drive_stage(
            job_id, stage, handlers[stage], idempotency_key=f"e2e-{stage}-{counter}"
        )
        outcomes.append(outcome)
    raise AssertionError(
        f"pipeline did not converge after {max_iterations} iterations; "
        f"last stage: {driver.resolve_current_stage(job_id)!r}"
    )


# --- gated handler wrapper -----------------------------------------------------


def _gated(build):
    """Return a handler that gates once (awaiting_human) then completes.

    The artifact is built once and cached across the two phases, so approval
    never re-runs paid generation (the awaiting_human submit already recorded
    the assets CI produced).
    """

    cache: dict[str, dict] = {}

    def handler(ctx: StageContext):
        if ctx.stage not in cache:
            cache[ctx.stage] = build(ctx)
        artifact = cache[ctx.stage]
        stage_snap = next(
            (s for s in ctx.lease.snapshot.stages if s.code == ctx.stage), None
        )
        assert stage_snap is not None, f"stage {ctx.stage!r} missing from snapshot"
        if stage_snap.approval_status != ApprovalStatus.APPROVED:
            return "awaiting_human", {_canonical(ctx.stage): artifact}, None
        return "completed", {_canonical(ctx.stage): artifact}, None

    return handler


def _ungated(build):
    def handler(ctx: StageContext):
        return "completed", {_canonical(ctx.stage): build(ctx)}, None

    return handler


def _canonical(stage: str) -> str:
    return {
        "research": "research_brief",
        "proposal": "proposal_packet",
        "script": "script",
        "scene_plan": "scene_plan",
        "assets": "asset_manifest",
        "edit": "edit_decisions",
        "compose": "render_report",
        "publish": "publish_log",
    }[stage]


# --- the test -----------------------------------------------------------------


def test_full_pipeline_end_to_end_with_fake_gateway(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = service.create_job(_request(), _attribution())
    gateway = FakeGateway(tmp_path / "projects" / job.job_id)
    driver = ClientStageDriver(service, gateway=gateway)

    def assets_build(ctx: StageContext):
        img = gateway.generate_image("scene1")
        narration = gateway.generate_narration("s1")
        return {
            "version": "1.0",
            "assets": [
                {
                    "id": "img-1",
                    "type": "image",
                    "path": img["path"],
                    "source_tool": "image_selector",
                    "scene_id": "scene1",
                },
                {
                    "id": "narr-1",
                    "type": "narration",
                    "path": narration["path"],
                    "source_tool": "tts_selector",
                    "scene_id": "s1",
                },
            ],
        }

    def edit_build(ctx: StageContext):
        return {
            "version": "1.0",
            "cuts": [
                {"id": "cut1", "source": "img-1", "in_seconds": 0, "out_seconds": 10},
                {"id": "cut2", "source": "narr-1", "in_seconds": 0, "out_seconds": 30},
            ],
            "render_runtime": "remotion",
        }

    def compose_build(ctx: StageContext):
        render = gateway.compose("final")
        return {
            "version": "1.0",
            "outputs": [
                {
                    "path": render["path"],
                    "format": "mp4",
                    "resolution": "1080x1920",
                    "duration_seconds": 30,
                }
            ],
        }

    def publish_build(ctx: StageContext):
        return {
            "version": "1.0",
            "entries": [
                {"platform": "douyin", "status": "exported", "timestamp": "2026-09-01T12:00:00Z"}
            ],
        }

    handlers = {
        "research": _ungated(lambda ctx: _research_brief()),
        "proposal": _gated(lambda ctx: _proposal_packet()),
        "script": _gated(lambda ctx: _script()),
        "scene_plan": _gated(lambda ctx: _scene_plan()),
        "assets": _gated(assets_build),
        "edit": _ungated(edit_build),
        "compose": _ungated(compose_build),
        "publish": _gated(publish_build),
    }

    outcomes = _run_with_approval(service, driver, job.job_id, handlers)

    # Every stage ran (gated stages appear once as awaiting_human, once completed).
    stages_covered = {o["stage"] for o in outcomes}
    assert stages_covered == {
        "research", "proposal", "script", "scene_plan",
        "assets", "edit", "compose", "publish",
    }

    # The Job reached SUCCEEDED with every stage completed.
    snapshot = service.get_job(job.job_id)
    assert snapshot.status == JobStatus.SUCCEEDED
    assert all(s.status == StageStatus.SUCCEEDED for s in snapshot.stages)

    # The fake gateway did the media work the client orchestrated — exactly
    # once per asset (approval must not re-run paid generation).
    from collections import Counter

    assert Counter(gateway.calls) == {
        "image:scene1": 1,
        "narration:s1": 1,
        "compose:final": 1,
    }

    # Instruction provenance was recorded on the gate checkpoint (proposal).
    proposal_cp = tmp_path / "projects" / job.job_id / "checkpoint_proposal.json"
    proposal = json.loads(proposal_cp.read_text(encoding="utf-8"))
    assert proposal["status"] == "completed"
    provenance_paths = {e["path"] for e in proposal["metadata"]["instruction_provenance"]}
    assert "pipeline_defs/animation.yaml" in provenance_paths
    assert "skills/pipelines/animation/proposal-director.md" in provenance_paths

    # Media references were validated and recorded (plan §7, §12): the assets
    # checkpoint lists the media files CI produced, and the compose checkpoint
    # lists the render. No cross-Job or absolute path escaped validation.
    assets_cp = json.loads(
        (tmp_path / "projects" / job.job_id / "checkpoint_assets.json").read_text("utf-8")
    )
    assert set(assets_cp["metadata"]["media_references"]) == {
        "assets/audio/s1.wav",
        "assets/images/scene1.png",
    }
    compose_cp = json.loads(
        (tmp_path / "projects" / job.job_id / "checkpoint_compose.json").read_text("utf-8")
    )
    assert compose_cp["metadata"]["media_references"] == ["renders/final.mp4"]

    # Instruction provenance is recorded on every stage checkpoint (plan §14),
    # not just the gated ones.
    for stage in ("research", "proposal", "script", "scene_plan", "assets", "edit", "compose", "publish"):
        cp = json.loads(
            (tmp_path / "projects" / job.job_id / f"checkpoint_{stage}.json").read_text("utf-8")
        )
        prov = cp["metadata"]["instruction_provenance"]
        assert prov, f"stage {stage} checkpoint recorded no instruction provenance"
        paths = {e["path"] for e in prov}
        assert "pipeline_defs/animation.yaml" in paths
        assert any(
            p.startswith("skills/pipelines/animation/") and p.endswith("-director.md")
            for p in paths
        ), f"stage {stage} checkpoint missing its director skill provenance"

    # No Job events carried media binaries, tokens, or credentials (plan §14):
    # the event list is non-empty and no serialized event mentions a lease
    # token (by key or by the opaque token value it mints) or an API key.
    events = service.list_events(job.job_id)
    assert events, "expected a non-empty event log"
    for event in events:
        serialized = json.dumps(event.to_wire())
        for forbidden in ("leaseToken", "om_clease_", "apiKey", "authorization"):
            assert forbidden not in serialized, f"event leaked {forbidden!r}"
