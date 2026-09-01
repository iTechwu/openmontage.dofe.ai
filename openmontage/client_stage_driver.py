"""Client-side stage driver implementing the plan §4 execution model.

The client Agent drives one pipeline stage at a time:

    1. get the Job and resolve the current executable stage
    2. begin_client_stage (exclusive lease + attempt)
    3. read the live CI instructions (manifest + director skill + meta skills
       + per-stage extras) — no local caching, provenance recorded
    4. read the predecessor artifact from the CI project
    5. run the stage handler (the cognitive work + Gateway tool calls)
    6. submit_client_stage with artifacts, status and instruction provenance
    7. stop at a gated stage and wait for approve_video_stage, then resume

The driver is pure plumbing: it owns the begin/read/submit/provenance loop and
leaves the per-stage cognitive decisions to the caller's ``handler`` callable.
The ``gateway`` is an opaque object handed to handlers (the CI media tools —
selectors, compose, mixers — which the client calls as usual and which this
driver never invokes directly).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from lib.pipeline_loader import get_stage_skill, load_pipeline_readonly
from lib.paths import REPO_ROOT

from openmontage.job_service import ClientStageLease, JobService
from openmontage.contracts import ApprovalStatus, JobStatus, StageStatus

# Standard instructions every stage reads live (plan §9).
STANDARD_INSTRUCTION_FILES = (
    "AGENT_GUIDE.md",
    "skills/meta/checkpoint-protocol.md",
    "skills/meta/reviewer.md",
)

# Per-stage instruction extras (plan §9 "按阶段按需读取"). Best-effort: a
# missing file is skipped, but a present file is read live and recorded in
# provenance. The list is generic across pipelines; pipelines that don't carry
# a given file simply don't read it.
PER_STAGE_INSTRUCTIONS: dict[str, tuple[str, ...]] = {
    "proposal": (
        "skills/meta/animation-runtime-selector.md",
        "skills/meta/taste-direction.md",
    ),
    "script": ("skills/meta/voice-performance-director.md",),
    "scene_plan": (
        "skills/creative/seedance-production.md",
        ".agents/skills/seedance-directing/SKILL.md",
    ),
    "assets": ("skills/creative/seedance-production.md",),
    "compose": (
        "skills/core/remotion.md",
        "skills/core/hyperframes.md",
        ".agents/skills/remotion-best-practices/SKILL.md",
    ),
}


@dataclass
class InstructionBundle:
    """Live instruction reads for one stage, with provenance for the submit."""

    provenance: list[dict[str, str]] = field(default_factory=list)
    contents: dict[str, str] = field(default_factory=dict)

    def add(self, relative_path: str, content_hash: str, content: str) -> None:
        self.provenance.append({"path": relative_path, "content_hash": content_hash})
        self.contents[relative_path] = content


@dataclass
class StageContext:
    """Everything a stage handler needs to do its cognitive work."""

    service: JobService
    gateway: Any
    job_id: str
    stage: str
    lease: ClientStageLease
    instructions: InstructionBundle
    predecessor: dict[str, Any] | None


# A handler returns the submission the driver should send: the stage status
# plus the artifacts / metadata. Gated stages return "awaiting_human"; the
# driver submits that and stops for approve_video_stage.
StageResult = tuple[str, dict[str, Any], dict[str, Any] | None]


def _submit_status_for(stage_status: StageStatus) -> str:
    """Map a settled stage status back to the submit-status string it implies."""
    return {
        StageStatus.SUCCEEDED: "completed",
        StageStatus.WAITING_APPROVAL: "awaiting_human",
        StageStatus.FAILED: "failed",
        StageStatus.CANCELLED: "failed",
    }.get(stage_status, "in_progress")


class ClientStageDriver:
    """Drive a Job through the client-stage loop (plan §4)."""

    def __init__(
        self,
        service: JobService,
        *,
        gateway: Any = None,
        instruction_root: str | Path | None = None,
        read_instruction_file: Callable[..., dict[str, Any]] | None = None,
        read_project_file: Callable[..., dict[str, Any]] | None = None,
        per_stage_instructions: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.service = service
        self.gateway = gateway
        self.instruction_root = instruction_root or REPO_ROOT
        self.per_stage = per_stage_instructions or PER_STAGE_INSTRUCTIONS

        if read_instruction_file is None:
            from openmontage.instruction_files import read_instruction_file as _read

            read_instruction_file = _read
        self._read_instruction_file = read_instruction_file

        if read_project_file is None:
            from openmontage.exchange import ProjectFileExporter

            def _read_project_file(project_id: str, relative_path: str, max_bytes: int = 2_000_000) -> dict[str, Any]:
                return ProjectFileExporter(projects_root=service.projects_dir).read_text(
                    project_id, relative_path, max_bytes=max_bytes
                )

            read_project_file = _read_project_file
        self._read_project_file = read_project_file

    # --- stage resolution ---------------------------------------------------

    def resolve_current_stage(self, job_id: str) -> str | None:
        """Return the stage to execute next, or None when the Job is terminal.

        The next stage is the first stage not yet SUCCEEDED/SKIPPED. A stage
        left RUNNING (a lost/expired lease) is the resume point and can be
        re-begun. A stage left WAITING_APPROVAL must NOT be begun — the caller
        approves it with ``approve_video_stage`` first, after which this
        returns the next stage.
        """
        snapshot = self.service.get_job(job_id)
        if snapshot.status in {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SUCCEEDED}:
            return None
        for stage in snapshot.stages:
            if stage.status not in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}:
                return stage.code
        return None

    # --- instruction reading (plan §9) ---------------------------------------

    def _director_skill_path(self, pipeline: str, stage: str) -> str | None:
        manifest = load_pipeline_readonly(pipeline)
        skill = get_stage_skill(manifest, stage)
        if not skill:
            return None
        return f"skills/{skill}.md"

    def read_stage_instructions(self, pipeline: str, stage: str) -> InstructionBundle:
        """Read the live CI instructions for a stage and record provenance.

        Required files (a missing one raises — the client must stop the stage,
        plan §18): the pipeline manifest, the stage director skill, and the
        three standard meta files. Per-stage extras are best-effort.
        """
        bundle = InstructionBundle()

        manifest_path = f"pipeline_defs/{pipeline}.yaml"
        for relative_path in (manifest_path, *STANDARD_INSTRUCTION_FILES):
            served = self._read_instruction_file(relative_path)
            bundle.add(served["relative_path"], served["content_hash"], served["content"])

        director = self._director_skill_path(pipeline, stage)
        if director is not None:
            served = self._read_instruction_file(director)
            bundle.add(served["relative_path"], served["content_hash"], served["content"])

        for relative_path in self.per_stage.get(stage, ()):
            try:
                served = self._read_instruction_file(relative_path)
            except Exception as exc:  # best-effort: pipelines differ in what they carry
                # The client may reach this through MCP, where the exception is
                # serialized to text and carries no ``.code`` attribute — match
                # the message too.
                if "INSTRUCTION_FILE_NOT_FOUND" in str(exc):
                    continue
                raise
            bundle.add(served["relative_path"], served["content_hash"], served["content"])

        return bundle

    # --- predecessor artifact (plan §5.4) --------------------------------------

    def read_predecessor_artifact(self, job_id: str, stage: str) -> dict[str, Any] | None:
        """Read the previous stage's checkpoint/artifact from the CI project.

        Uses the read-only project-file channel (``read_project_file``), never
        direct host-path access. Returns None for the first stage.
        """
        snapshot = self.service.get_job(job_id)
        codes = [s.code for s in snapshot.stages]
        index = codes.index(stage)
        if index == 0:
            return None
        predecessor = codes[index - 1]
        try:
            result = self._read_project_file(
                job_id, f"checkpoint_{predecessor}.json"
            )
            checkpoint = json.loads(result["content"])
            return {
                "stage": predecessor,
                "artifacts": checkpoint.get("artifacts", {}),
                "checkpoint": checkpoint,
            }
        except Exception:
            # Predecessor checkpoint not yet on CI (or unreadable): the submit
            # path still enforces the manifest gate policy, so returning None
            # here only affects what the handler can inspect, not correctness.
            return None

    def _read_stage_checkpoint(self, job_id: str, stage: str) -> dict[str, Any] | None:
        """Read this stage's own checkpoint (if any) from the CI project.

        Used for approval recovery: after ``approve_video_stage`` a gated stage
        already has its ``awaiting_human`` checkpoint (with the artifacts the
        client produced); the driver reuses them instead of re-running the
        handler.
        """
        try:
            result = self._read_project_file(job_id, f"checkpoint_{stage}.json")
            return json.loads(result["content"])
        except Exception:
            return None

    # --- stage loop -----------------------------------------------------------

    def drive_stage(
        self,
        job_id: str,
        stage: str,
        handler: Callable[[StageContext], StageResult],
        *,
        idempotency_key: str | None = None,
        expected_sequence: int | None = None,
    ) -> dict[str, Any]:
        """Begin, read, run, and submit a single stage.

        ``handler`` returns ``(status, artifacts, metadata)``. On
        ``awaiting_human`` the driver submits that status and stops — the
        caller approves with ``approve_video_stage`` and re-drives (or runs)
        the next stage. Returns the submitted Job snapshot wire dict plus the
        stage and whether it is now waiting for approval.
        """
        if idempotency_key is None:
            idempotency_key = f"client-{job_id}-{stage}-{uuid4().hex}"

        # ``expected_sequence`` is opt-in optimistic fencing (plan §8.2): a
        # caller that observed a sequence and wants to reject a concurrent
        # advance passes it explicitly. It is intentionally NOT auto-derived —
        # deriving it here would break idempotent retries (a retry observes a
        # higher sequence and the begin replay would collide).
        lease = self.service.begin_client_stage(
            job_id,
            stage,
            idempotency_key=f"{idempotency_key}:begin",
            expected_sequence=expected_sequence,
        )

        # A replayed begin (same idempotency_key) returns the original lease,
        # whose stage has already advanced past RUNNING — the earlier submit
        # settled it. Running the handler again would repeat paid Gateway calls,
        # so detect the replay and short-circuit to the current Job state.
        current = self.service.get_job(job_id)
        current_stage = next((s for s in current.stages if s.code == stage), None)
        if current_stage is not None and current_stage.status != StageStatus.RUNNING:
            return {
                "stage": stage,
                "status": _submit_status_for(current_stage.status),
                "waiting_approval": current_stage.status == StageStatus.WAITING_APPROVAL,
                "snapshot": current.to_wire(),
                "replayed": True,
            }

        snapshot = lease.snapshot
        pipeline = snapshot.workflow.name
        stage_snap = next((s for s in snapshot.stages if s.code == stage), None)

        # Approval recovery (plan §8.3, §10): a gated stage whose approval was
        # just granted already has its artifacts checkpointed as awaiting_human.
        # Reuse them and submit completed WITHOUT re-running the handler —
        # re-running it would repeat paid Gateway calls (image/video/TTS).
        if (
            stage_snap is not None
            and stage_snap.approval_required
            and stage_snap.approval_status == ApprovalStatus.APPROVED
        ):
            prior = self._read_stage_checkpoint(job_id, stage)
            if prior is not None and prior.get("status") == "awaiting_human":
                instructions = self.read_stage_instructions(pipeline, stage)
                result = self.service.submit_client_stage(
                    job_id,
                    stage,
                    stage_attempt=lease.stage_attempt,
                    status="completed",
                    lease_token=lease.lease_token,
                    idempotency_key=f"{idempotency_key}:submit",
                    artifacts=prior.get("artifacts", {}),
                    instruction_provenance=instructions.provenance,
                )
                return {
                    "stage": stage,
                    "status": "completed",
                    "waiting_approval": False,
                    "snapshot": result.to_wire(),
                    "replayed": False,
                    "reused_artifact": True,
                }

        instructions = self.read_stage_instructions(pipeline, stage)
        predecessor = self.read_predecessor_artifact(job_id, stage)

        context = StageContext(
            service=self.service,
            gateway=self.gateway,
            job_id=job_id,
            stage=stage,
            lease=lease,
            instructions=instructions,
            predecessor=predecessor,
        )
        status, artifacts, metadata = handler(context)

        result = self.service.submit_client_stage(
            job_id,
            stage,
            stage_attempt=lease.stage_attempt,
            status=status,
            lease_token=lease.lease_token,
            idempotency_key=f"{idempotency_key}:submit",
            artifacts=artifacts,
            metadata=metadata,
            instruction_provenance=instructions.provenance,
        )
        return {
            "stage": stage,
            "status": status,
            "waiting_approval": status == "awaiting_human",
            "snapshot": result.to_wire(),
            "replayed": False,
        }

    def run(
        self,
        job_id: str,
        handlers: dict[str, Callable[[StageContext], StageResult]],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Drive stages in order until a gate or completion.

        Returns a summary dict with the final snapshot and ``waiting_approval``
        when the run stopped at a gated stage awaiting ``approve_video_stage``.
        """
        base_key = idempotency_key or f"client-run-{job_id}-{uuid4().hex}"
        results: list[dict[str, Any]] = []
        while True:
            stage = self.resolve_current_stage(job_id)
            if stage is None:
                return {
                    "job_id": job_id,
                    "completed": True,
                    "snapshot": self.service.get_job(job_id).to_wire(),
                    "stages": results,
                }
            handler = handlers.get(stage)
            if handler is None:
                raise KeyError(f"no handler provided for stage {stage!r}")
            outcome = self.drive_stage(
                job_id,
                stage,
                handler,
                idempotency_key=f"{base_key}:{stage}",
            )
            # Progress heartbeats belong to update_client_stage_progress, not
            # the run loop; a handler returning "in_progress" keeps the stage
            # RUNNING and the next iteration would collide with its own lease.
            if outcome.get("status") == "in_progress":
                raise RuntimeError(
                    f"handler for stage {stage!r} returned 'in_progress'; the run "
                    "loop only accepts completed / awaiting_human / failed"
                )
            results.append(outcome)
            if outcome["waiting_approval"]:
                return {
                    "job_id": job_id,
                    "completed": False,
                    "waiting_approval": True,
                    "waiting_stage": stage,
                    "snapshot": outcome["snapshot"],
                    "stages": results,
                }
