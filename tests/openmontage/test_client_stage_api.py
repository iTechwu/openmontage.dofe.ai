"""Tests for the client-driven stage API (plan §6-§8, §16.2, §16.3)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openmontage.contracts import (
    JobAttribution,
    JobCreateRequest,
    JobEventType,
    JobStatus,
    StageStatus,
)
from openmontage.instruction_files import read_instruction_file
from openmontage.job_service import (
    ClientStageError,
    ClientStageLease,
    JobConflictError,
    JobService,
    JobStateError,
)


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


def _request(workflow: str = "animated-explainer") -> JobCreateRequest:
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


def _job(service: JobService, workflow: str = "animated-explainer"):
    return service.create_job(_request(workflow), _attribution())


def _begin(service: JobService, job_id: str, stage: str, **kwargs) -> ClientStageLease:
    return service.begin_client_stage(
        job_id, stage, idempotency_key=f"begin-{stage}-{kwargs.pop('key_suffix', '1')}", **kwargs
    )


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
            "saturated_angles": ["done to death"],
            "underserved_gaps": ["an open gap"],
        },
        "data_points": [data_point(i) for i in range(3)],
        "audience_insights": {
            "common_questions": ["q1", "q2", "q3"],
            "misconceptions": [],
            "knowledge_level": "intermediate",
        },
        "angles_discovered": [
            {"name": f"angle {i}", "hook": "h", "type": "evergreen", "why_now": "w"}
            for i in range(3)
        ],
        "sources": [source(i) for i in range(5)],
    }


def _provenance() -> list[dict[str, str]]:
    served = read_instruction_file("AGENT_GUIDE.md")
    return [{"path": "AGENT_GUIDE.md", "content_hash": served["content_hash"]}]


# --- begin_client_stage -----------------------------------------------------


def test_begin_starts_first_stage_and_moves_job_to_running(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    assert job.status == JobStatus.QUEUED

    lease = _begin(service, job.job_id, "research", expected_sequence=job.last_sequence)

    assert lease.stage == "research"
    assert lease.stage_attempt == 1
    assert lease.lease_token.startswith("om_clease_")
    assert lease.expires_at > datetime.now(timezone.utc)
    snapshot = service.get_job(job.job_id)
    assert snapshot.status == JobStatus.RUNNING
    assert snapshot.current_stage == "research"
    assert snapshot.stages[0].status == StageStatus.RUNNING
    assert snapshot.stages[0].attempt == 1

    events = service.list_events(job.job_id)
    assert events[-1].event_type == JobEventType.CLIENT_STAGE_STARTED
    assert events[-1].payload["stage"] == "research"
    assert events[-1].payload["stageAttempt"] == 1
    assert "leaseToken" not in json.dumps(events[-1].payload)


def test_begin_creates_project_workspace_on_disk(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    _begin(service, job.job_id, "research")

    marker = tmp_path / "projects" / job.job_id / "project.json"
    assert marker.is_file()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["pipeline_type"] == "animated-explainer"


def test_begin_rejects_stage_skip(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)

    with pytest.raises(ClientStageError) as exc_info:
        _begin(service, job.job_id, "script")
    assert exc_info.value.code == "STAGE_STATE_INVALID"
    assert "predecessor" in str(exc_info.value)


def test_begin_rejects_unknown_stage(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    with pytest.raises(JobStateError):
        _begin(service, job.job_id, "no_such_stage")


def test_begin_sequence_conflict_is_rejected(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)

    with pytest.raises(JobConflictError):
        _begin(service, job.job_id, "research", expected_sequence=999)

    # Correct sequence passes.
    _begin(service, job.job_id, "research", expected_sequence=job.last_sequence)


def test_begin_is_idempotent_and_conflicts_on_different_arguments(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)

    first = service.begin_client_stage(job.job_id, "research", idempotency_key="k-1")
    replay = service.begin_client_stage(job.job_id, "research", idempotency_key="k-1")
    assert replay.lease_token == first.lease_token
    assert replay.stage_attempt == first.stage_attempt
    # No duplicate events.
    assert len(service.list_events(job.job_id)) == 2  # created + started

    with pytest.raises(ClientStageError) as exc_info:
        service.begin_client_stage(
            job.job_id, "research", idempotency_key="k-1", expected_sequence=0
        )
    assert exc_info.value.code == "IDEMPOTENCY_CONFLICT"


def test_begin_rejects_second_live_owner(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    _begin(service, job.job_id, "research", key_suffix="a")

    with pytest.raises(ClientStageError) as exc_info:
        _begin(service, job.job_id, "research", key_suffix="b")
    assert exc_info.value.code == "STAGE_ALREADY_OWNED"


def test_begin_after_lease_expiry_supersedes_stale_lease(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    t0 = datetime.now(timezone.utc)
    first = service.begin_client_stage(
        job.job_id, "research", idempotency_key="k-a",
        lease_duration=timedelta(seconds=60), now=t0,
    )

    # Still live: a second owner is rejected.
    with pytest.raises(ClientStageError) as exc_info:
        service.begin_client_stage(
            job.job_id, "research", idempotency_key="k-b", now=t0 + timedelta(seconds=30)
        )
    assert exc_info.value.code == "STAGE_ALREADY_OWNED"

    # After expiry: re-begin succeeds with a fresh attempt and token.
    second = service.begin_client_stage(
        job.job_id, "research", idempotency_key="k-b", now=t0 + timedelta(seconds=61)
    )
    assert second.stage_attempt == 2
    assert second.lease_token != first.lease_token

    # The stale token can no longer submit.
    with pytest.raises(ClientStageError) as exc_info:
        service.submit_client_stage(
            job.job_id, "research",
            stage_attempt=1, status="in_progress",
            lease_token=first.lease_token, idempotency_key="s-stale",
        )
    assert exc_info.value.code == "STAGE_LEASE_INVALID"


# --- update_client_stage_progress -------------------------------------------


def test_progress_updates_snapshot_renews_lease_and_is_idempotent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    t0 = datetime.now(timezone.utc)
    lease = service.begin_client_stage(
        job.job_id, "research", idempotency_key="b-1",
        lease_duration=timedelta(seconds=120), now=t0,
    )

    snapshot = service.update_client_stage_progress(
        job.job_id, "research",
        stage_attempt=lease.stage_attempt,
        completed_units=1, total_units=3, label_code="research.scanning",
        lease_token=lease.lease_token, idempotency_key="p-1",
        lease_duration=timedelta(seconds=120), now=t0 + timedelta(seconds=100),
    )
    stage = snapshot.stages[0]
    assert stage.progress == {
        "completedUnits": 1, "totalUnits": 3, "labelCode": "research.scanning"
    }
    event = service.list_events(job.job_id)[-1]
    assert event.event_type == JobEventType.CLIENT_STAGE_PROGRESSED

    # Lease was renewed past the original expiry: progress at +130s works.
    snapshot2 = service.update_client_stage_progress(
        job.job_id, "research",
        stage_attempt=lease.stage_attempt,
        completed_units=2, total_units=3, label_code="research.writing",
        lease_token=lease.lease_token, idempotency_key="p-2",
        lease_duration=timedelta(seconds=120), now=t0 + timedelta(seconds=130),
    )
    assert snapshot2.stages[0].progress["completedUnits"] == 2

    # Replay returns the original result without a new event.
    before = len(service.list_events(job.job_id))
    replay = service.update_client_stage_progress(
        job.job_id, "research",
        stage_attempt=lease.stage_attempt,
        completed_units=1, total_units=3, label_code="research.scanning",
        lease_token=lease.lease_token, idempotency_key="p-1",
        now=t0 + timedelta(seconds=140),
    )
    assert replay.last_sequence == snapshot.last_sequence
    assert len(service.list_events(job.job_id)) == before


def test_progress_requires_valid_lease_attempt_and_expiry(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    t0 = datetime.now(timezone.utc)
    lease = service.begin_client_stage(
        job.job_id, "research", idempotency_key="b-1",
        lease_duration=timedelta(seconds=60), now=t0,
    )

    with pytest.raises(ClientStageError) as exc_info:
        service.update_client_stage_progress(
            job.job_id, "research", stage_attempt=lease.stage_attempt,
            completed_units=1, total_units=2, label_code="x",
            lease_token="om_clease_wrong", idempotency_key="p-1", now=t0,
        )
    assert exc_info.value.code == "STAGE_LEASE_INVALID"

    with pytest.raises(ClientStageError) as exc_info:
        service.update_client_stage_progress(
            job.job_id, "research", stage_attempt=99,
            completed_units=1, total_units=2, label_code="x",
            lease_token=lease.lease_token, idempotency_key="p-2", now=t0,
        )
    assert exc_info.value.code == "STAGE_ATTEMPT_MISMATCH"

    with pytest.raises(ClientStageError) as exc_info:
        service.update_client_stage_progress(
            job.job_id, "research", stage_attempt=lease.stage_attempt,
            completed_units=1, total_units=2, label_code="x",
            lease_token=lease.lease_token, idempotency_key="p-3",
            now=t0 + timedelta(seconds=61),
        )
    assert exc_info.value.code == "STAGE_LEASE_EXPIRED"

    with pytest.raises(ClientStageError):
        service.update_client_stage_progress(
            job.job_id, "research", stage_attempt=lease.stage_attempt,
            completed_units=3, total_units=2, label_code="x",
            lease_token=lease.lease_token, idempotency_key="p-4", now=t0,
        )


# --- submit_client_stage: checkpoint + validation ----------------------------


def test_submit_in_progress_writes_checkpoint_and_keeps_lease(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    lease = _begin(service, job.job_id, "research")

    snapshot = service.submit_client_stage(
        job.job_id, "research",
        stage_attempt=lease.stage_attempt, status="in_progress",
        lease_token=lease.lease_token, idempotency_key="s-1",
        metadata={"partial_progress": {"completedUnits": 1, "totalUnits": 2}},
    )

    assert snapshot.stages[0].status == StageStatus.RUNNING
    checkpoint = json.loads(
        (tmp_path / "projects" / job.job_id / "checkpoint_research.json").read_text("utf-8")
    )
    assert checkpoint["status"] == "in_progress"
    assert checkpoint["project_id"] == job.job_id
    assert checkpoint["pipeline_type"] == "animated-explainer"
    assert checkpoint["metadata"]["partial_progress"] == {"completedUnits": 1, "totalUnits": 2}
    event = service.list_events(job.job_id)[-1]
    assert event.event_type == JobEventType.CLIENT_STAGE_CHECKPOINTED

    # The lease is still usable afterwards.
    service.update_client_stage_progress(
        job.job_id, "research", stage_attempt=lease.stage_attempt,
        completed_units=2, total_units=2, label_code="research.done",
        lease_token=lease.lease_token, idempotency_key="p-9",
    )


def test_submit_completed_requires_approval_on_gated_stage(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service, workflow="framework-smoke")
    lease = _begin(service, job.job_id, "research")

    with pytest.raises(ClientStageError) as exc_info:
        service.submit_client_stage(
            job.job_id, "research",
            stage_attempt=lease.stage_attempt, status="completed",
            lease_token=lease.lease_token, idempotency_key="s-1",
            artifacts={"research_brief": _research_brief()},
        )
    assert exc_info.value.code == "HUMAN_APPROVAL_REQUIRED"
    # No checkpoint or state change was persisted.
    assert service.get_job(job.job_id).stages[0].status == StageStatus.RUNNING


def test_gated_stage_awaiting_human_then_approval_then_completed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service, workflow="framework-smoke")
    lease = _begin(service, job.job_id, "research")

    snapshot = service.submit_client_stage(
        job.job_id, "research",
        stage_attempt=lease.stage_attempt, status="awaiting_human",
        lease_token=lease.lease_token, idempotency_key="s-1",
        artifacts={"research_brief": _research_brief()},
        instruction_provenance=_provenance(),
    )
    assert snapshot.status == JobStatus.WAITING_APPROVAL
    assert snapshot.stages[0].status == StageStatus.WAITING_APPROVAL
    checkpoint = json.loads(
        (tmp_path / "projects" / job.job_id / "checkpoint_research.json").read_text("utf-8")
    )
    assert checkpoint["status"] == "awaiting_human"
    assert checkpoint["metadata"]["instruction_provenance"][0]["path"] == "AGENT_GUIDE.md"
    event = service.list_events(job.job_id)[-1]
    assert event.event_type == JobEventType.CLIENT_STAGE_AWAITING_APPROVAL
    assert event.payload["instructionProvenance"][0]["content_hash"].startswith("sha256:")

    # The lease was released: further submits with it are rejected.
    with pytest.raises(ClientStageError) as exc_info:
        service.submit_client_stage(
            job.job_id, "research",
            stage_attempt=lease.stage_attempt, status="completed",
            lease_token=lease.lease_token, idempotency_key="s-2",
            artifacts={"research_brief": _research_brief()},
        )
    assert exc_info.value.code == "STAGE_LEASE_INVALID"

    # Human approves via the existing approval tool path.
    approved = service.resolve_stage_approval(
        job.job_id, "research", approved=True,
        expected_sequence=snapshot.last_sequence, idempotency_key="approve-1",
    )
    assert approved.stages[0].status == StageStatus.RUNNING
    assert approved.status == JobStatus.RUNNING

    # Client re-begins the same stage and finalizes it.
    lease2 = _begin(service, job.job_id, "research", key_suffix="2")
    assert lease2.stage_attempt == 2
    done = service.submit_client_stage(
        job.job_id, "research",
        stage_attempt=lease2.stage_attempt, status="completed",
        lease_token=lease2.lease_token, idempotency_key="s-3",
        artifacts={"research_brief": _research_brief()},
    )
    assert done.stages[0].status == StageStatus.SUCCEEDED
    checkpoint = json.loads(
        (tmp_path / "projects" / job.job_id / "checkpoint_research.json").read_text("utf-8")
    )
    assert checkpoint["status"] == "completed"
    assert checkpoint["human_approved"] is True
    # The superseded awaiting_human checkpoint was archived.
    history = list((tmp_path / "projects" / job.job_id / "history").glob("checkpoint_research_*.json"))
    assert len(history) == 1
    assert json.loads(history[0].read_text("utf-8"))["status"] == "awaiting_human"


def test_awaiting_human_rejected_on_ungated_stage(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)  # animated-explainer research is not gated
    lease = _begin(service, job.job_id, "research")
    with pytest.raises(ClientStageError) as exc_info:
        service.submit_client_stage(
            job.job_id, "research",
            stage_attempt=lease.stage_attempt, status="awaiting_human",
            lease_token=lease.lease_token, idempotency_key="s-1",
            artifacts={"research_brief": _research_brief()},
        )
    assert exc_info.value.code == "STAGE_STATE_INVALID"


def test_submit_rejects_invalid_artifact_schema(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    lease = _begin(service, job.job_id, "research")

    with pytest.raises(ClientStageError) as exc_info:
        service.submit_client_stage(
            job.job_id, "research",
            stage_attempt=lease.stage_attempt, status="completed",
            lease_token=lease.lease_token, idempotency_key="s-1",
            artifacts={"research_brief": {"version": "1.0"}},
        )
    assert exc_info.value.code == "ARTIFACT_SCHEMA_INVALID"
    # Nothing persisted: no checkpoint, stage still running.
    assert not (tmp_path / "projects" / job.job_id / "checkpoint_research.json").exists()
    assert service.get_job(job.job_id).stages[0].status == StageStatus.RUNNING


def test_submit_rejects_missing_canonical_artifact(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    lease = _begin(service, job.job_id, "research")
    with pytest.raises(ClientStageError) as exc_info:
        service.submit_client_stage(
            job.job_id, "research",
            stage_attempt=lease.stage_attempt, status="completed",
            lease_token=lease.lease_token, idempotency_key="s-1",
            artifacts={},
        )
    assert exc_info.value.code == "ARTIFACT_SCHEMA_INVALID"


def test_submit_is_idempotent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    lease = _begin(service, job.job_id, "research")
    kwargs = dict(
        stage_attempt=lease.stage_attempt, status="completed",
        lease_token=lease.lease_token, idempotency_key="s-1",
        artifacts={"research_brief": _research_brief()},
    )
    first = service.submit_client_stage(job.job_id, "research", **kwargs)
    before_events = len(service.list_events(job.job_id))

    replay = service.submit_client_stage(job.job_id, "research", **kwargs)
    assert replay.last_sequence == first.last_sequence
    assert len(service.list_events(job.job_id)) == before_events

    with pytest.raises(ClientStageError) as exc_info:
        service.submit_client_stage(
            job.job_id, "research", **{**kwargs, "status": "failed"}
        )
    assert exc_info.value.code == "IDEMPOTENCY_CONFLICT"


def test_submit_failed_fails_stage_and_job(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    lease = _begin(service, job.job_id, "research")

    snapshot = service.submit_client_stage(
        job.job_id, "research",
        stage_attempt=lease.stage_attempt, status="failed",
        lease_token=lease.lease_token, idempotency_key="s-1",
        metadata={"error": "web research providers unreachable", "error_code": "RESEARCH_BLOCKED"},
    )
    assert snapshot.status == JobStatus.FAILED
    assert snapshot.stages[0].status == StageStatus.FAILED
    types = [e.event_type for e in service.list_events(job.job_id)]
    assert types[-2:] == [JobEventType.CLIENT_STAGE_FAILED, JobEventType.JOB_FAILED]
    checkpoint = json.loads(
        (tmp_path / "projects" / job.job_id / "checkpoint_research.json").read_text("utf-8")
    )
    assert checkpoint["status"] == "failed"
    assert "unreachable" in checkpoint["error"]


def test_submit_provenance_must_match_live_files(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    lease = _begin(service, job.job_id, "research")

    with pytest.raises(ClientStageError) as exc_info:
        service.submit_client_stage(
            job.job_id, "research",
            stage_attempt=lease.stage_attempt, status="completed",
            lease_token=lease.lease_token, idempotency_key="s-1",
            artifacts={"research_brief": _research_brief()},
            instruction_provenance=[{"path": "AGENT_GUIDE.md", "content_hash": "sha256:" + "0" * 64}],
        )
    assert exc_info.value.code == "PROVENANCE_STALE"


# --- media reference validation ----------------------------------------------


def test_submit_rejects_absolute_and_traversing_media_paths(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    lease = _begin(service, job.job_id, "research")

    def brief_with(url: str) -> dict:
        return {
            **_research_brief(),
            "visual_references": [
                {"description": "d", "url": url, "what_works": "w"}
            ],
        }

    for bad_path in ("/data/projects/other/assets/x.png", "../other-job/assets/x.png"):
        with pytest.raises(ClientStageError) as exc_info:
            service.submit_client_stage(
                job.job_id, "research",
                stage_attempt=lease.stage_attempt, status="completed",
                lease_token=lease.lease_token, idempotency_key=f"s-{bad_path[:6]}",
                artifacts={"research_brief": brief_with(bad_path)},
            )
        assert exc_info.value.code == "MEDIA_REFERENCE_INVALID"


def test_submit_requires_referenced_media_to_exist(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    lease = _begin(service, job.job_id, "research")
    brief = {
        **_research_brief(),
        "visual_references": [
            {"description": "d", "url": "assets/images/missing.png", "what_works": "w"}
        ],
    }

    with pytest.raises(ClientStageError) as exc_info:
        service.submit_client_stage(
            job.job_id, "research",
            stage_attempt=lease.stage_attempt, status="completed",
            lease_token=lease.lease_token, idempotency_key="s-1",
            artifacts={"research_brief": brief},
        )
    assert exc_info.value.code == "MEDIA_REFERENCE_INVALID"

    # Create the referenced file on "CI" and resubmit with a fresh key.
    target = tmp_path / "projects" / job.job_id / "assets" / "images" / "missing.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG fake")
    snapshot = service.submit_client_stage(
        job.job_id, "research",
        stage_attempt=lease.stage_attempt, status="completed",
        lease_token=lease.lease_token, idempotency_key="s-2",
        artifacts={"research_brief": brief},
    )
    assert snapshot.stages[0].status == StageStatus.SUCCEEDED


def test_submit_verifies_sha256_sibling(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    lease = _begin(service, job.job_id, "research")
    target = tmp_path / "projects" / job.job_id / "assets" / "images" / "scene1.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fake png bytes")
    good_hash = hashlib.sha256(b"fake png bytes").hexdigest()

    brief = {
        **_research_brief(),
        "metadata": {"asset": {"path": "assets/images/scene1.png", "sha256": "0" * 64}},
    }
    with pytest.raises(ClientStageError) as exc_info:
        service.submit_client_stage(
            job.job_id, "research",
            stage_attempt=lease.stage_attempt, status="completed",
            lease_token=lease.lease_token, idempotency_key="s-1",
            artifacts={"research_brief": brief},
        )
    assert exc_info.value.code == "MEDIA_REFERENCE_INVALID"

    brief["metadata"]["asset"]["sha256"] = good_hash
    snapshot = service.submit_client_stage(
        job.job_id, "research",
        stage_attempt=lease.stage_attempt, status="completed",
        lease_token=lease.lease_token, idempotency_key="s-2",
        artifacts={"research_brief": brief},
    )
    assert snapshot.stages[0].status == StageStatus.SUCCEEDED


# --- cancellation during a client stage ---------------------------------------


def test_submit_during_cancel_request_confirms_cancel(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    lease = _begin(service, job.job_id, "research")
    service.request_cancel(
        job.job_id, expected_sequence=lease.snapshot.last_sequence,
        idempotency_key="cancel-1",
    )

    snapshot = service.submit_client_stage(
        job.job_id, "research",
        stage_attempt=lease.stage_attempt, status="in_progress",
        lease_token=lease.lease_token, idempotency_key="s-1",
    )
    assert snapshot.status == JobStatus.CANCELLED
    assert snapshot.stages[0].status == StageStatus.CANCELLED


def test_begin_on_cancel_requested_job_confirms_and_rejects(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job = _job(service)
    service.request_cancel(
        job.job_id, expected_sequence=job.last_sequence, idempotency_key="cancel-1"
    )
    with pytest.raises(ClientStageError) as exc_info:
        _begin(service, job.job_id, "research")
    assert exc_info.value.code == "JOB_CANCELLED"
    assert service.get_job(job.job_id).status == JobStatus.CANCELLED


# --- MCP surface ---------------------------------------------------------------


def test_mcp_server_exposes_client_stage_tools() -> None:
    pytest.importorskip("mcp")
    import asyncio

    from openmontage.mcp_server import create_server

    server = create_server()
    tool_names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {
        "begin_client_stage",
        "update_client_stage_progress",
        "submit_client_stage",
    } <= tool_names


def test_submit_archives_checkpoint_when_cancelled_during_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel racing the checkpoint write must not leave an in_progress/
    completed checkpoint for a CANCELLED Job (review P1)."""
    service = _service(tmp_path)
    job = _job(service)
    lease = _begin(service, job.job_id, "research")

    import lib.checkpoint as checkpoint_module

    real_write_checkpoint = checkpoint_module.write_checkpoint

    def write_then_cancel(*args, **kwargs):
        result = real_write_checkpoint(*args, **kwargs)
        # A concurrent client cancels the Job right after the checkpoint hits
        # disk but before submit reloads state inside the transaction.
        service.request_cancel(job.job_id, idempotency_key="cancel-1")
        return result

    monkeypatch.setattr(checkpoint_module, "write_checkpoint", write_then_cancel)

    snapshot = service.submit_client_stage(
        job.job_id, "research",
        stage_attempt=lease.stage_attempt, status="in_progress",
        lease_token=lease.lease_token, idempotency_key="s-1",
    )
    assert snapshot.status == JobStatus.CANCELLED

    # The just-written checkpoint was archived, not left behind.
    checkpoint_path = tmp_path / "projects" / job.job_id / "checkpoint_research.json"
    assert not checkpoint_path.exists()
    history = list(
        (tmp_path / "projects" / job.job_id / "history").glob(
            "checkpoint_research_cancelled_*.json"
        )
    )
    assert len(history) == 1


@pytest.mark.parametrize("bad_key", [None, "", "   ", "x" * 257])
def test_client_stage_endpoints_reject_invalid_idempotency_key(
    tmp_path: Path, bad_key,
) -> None:
    """Service-layer gate: begin/update/submit reject an empty/oversized
    idempotency key instead of silently skipping the idempotency record."""
    service = _service(tmp_path)
    job = _job(service)

    with pytest.raises(ClientStageError) as exc_info:
        service.begin_client_stage(job.job_id, "research", idempotency_key=bad_key)
    assert exc_info.value.code == "IDEMPOTENCY_CONFLICT"

    lease = service.begin_client_stage(job.job_id, "research", idempotency_key="ok")

    with pytest.raises(ClientStageError) as exc_info:
        service.update_client_stage_progress(
            job.job_id, "research", stage_attempt=lease.stage_attempt,
            completed_units=1, total_units=1, label_code="x",
            lease_token=lease.lease_token, idempotency_key=bad_key,
        )
    assert exc_info.value.code == "IDEMPOTENCY_CONFLICT"

    with pytest.raises(ClientStageError) as exc_info:
        service.submit_client_stage(
            job.job_id, "research", stage_attempt=lease.stage_attempt,
            status="in_progress", lease_token=lease.lease_token,
            idempotency_key=bad_key,
        )
    assert exc_info.value.code == "IDEMPOTENCY_CONFLICT"
