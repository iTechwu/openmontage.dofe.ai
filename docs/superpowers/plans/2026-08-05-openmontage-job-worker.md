# OpenMontage Job Worker Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Connect durable OpenMontage Jobs to a real external agent executor, reconcile checkpoint facts into the Job state machine, and automatically publish the final video through AgentSpace Artifact Bridge.

**Architecture:** Python remains infrastructure only. `JobWorker` claims one durable lease, prepares a Job-scoped workspace and inputs, invokes a configured external agent command for exactly one pipeline stage, then treats the validated stage checkpoint as the source of truth. Lease fencing prevents stale workers from mutating the Job; a completed checkpoint lets a restarted worker advance state without repeating paid agent work.

**Tech Stack:** Python 3.10, SQLite, Pydantic, subprocess argv (no shell), existing checkpoint library, existing Artifact Bridge client, pytest, Docker Compose.

---

## Task 1: Durable execution leases

**Files:**
- Modify: `openmontage/job_service.py`
- Create: `tests/openmontage/test_job_leases.py`

- [ ] Add failing tests for single-winner claim, lease heartbeat, fencing, release, expiry recovery, and terminal/approval exclusion.
- [ ] Add an additive SQLite execution table and a typed lease contract.
- [ ] Implement atomic claim, heartbeat, release/retry, and lease validation.
- [ ] Run focused tests, then all `tests/openmontage`; commit and push.

## Task 2: Checkpoint-backed external Agent executor

**Files:**
- Create: `openmontage/pipeline_executor.py`
- Create: `tests/openmontage/test_pipeline_executor.py`

- [ ] Add failing tests for assignment payload, safe argv execution, timeout/non-zero handling, and checkpoint reconciliation.
- [ ] Build a stage assignment from the immutable Job snapshot and pipeline manifest.
- [ ] Invoke `OPENMONTAGE_AGENT_EXECUTOR_JSON` as an argv array with the assignment prompt on stdin; never use a shell.
- [ ] Validate the resulting stage checkpoint and return a typed outcome.
- [ ] Run focused tests, then all `tests/openmontage`; commit and push.

## Task 3: Job Worker state-machine integration

**Files:**
- Create: `openmontage/job_worker.py`
- Modify: `openmontage/job_service.py`
- Create: `tests/openmontage/test_job_worker.py`

- [ ] Add failing tests for queued-to-running progression, checkpoint-first crash recovery, approval pause/resume, cancellation, retry, and terminal failure.
- [ ] Initialize the canonical `projects/<job-id>` workspace and reconcile an existing checkpoint before invoking the executor.
- [ ] Start exactly one stage, invoke the executor, and map `completed`, `awaiting_human`, `in_progress`, and `failed` checkpoints into fenced Job transitions.
- [ ] Release or defer leases deterministically; never repeat a completed stage.
- [ ] Run focused tests, then all `tests/openmontage`; commit and push.

## Task 4: Artifact Bridge input/output automation

**Files:**
- Modify: `openmontage/job_worker.py`
- Modify: `openmontage/artifact_bridge.py` only if a reusable boundary is missing
- Modify: `tests/openmontage/test_job_worker.py`

- [ ] Add failing tests for artifact input download, input receipt reuse, final MP4 selection, upload, idempotent manifest persistence, and cleanup on failure.
- [ ] Download Artifact input into the Job workspace before the first stage and persist a hash-bound receipt.
- [ ] Resolve the final video only from validated `publish_log`/`render_report` paths inside the Job workspace.
- [ ] Upload through the existing Artifact Bridge and persist `PublishedArtifact` before completing the Job.
- [ ] Run focused tests, then all `tests/openmontage`; commit and push.

## Task 5: CLI, Docker, operations, and E2E-ready verification

**Files:**
- Modify: `openmontage/cli.py`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Modify: `docs/DOCKER_AND_AGENTS.md`
- Modify: `tests/openmontage/test_docker_job_bridge.py`
- Create: `tests/openmontage/test_job_worker_cli.py`

- [ ] Add failing CLI/Compose contract tests.
- [ ] Add `openmontage worker run [--once]` with bounded polling and graceful shutdown.
- [ ] Add an `openmontage-worker` Compose service sharing the Job DB and projects volume; do not add PostgreSQL, Redis, RabbitMQ, or Jenkins.
- [ ] Document executor argv configuration, Docker credential/mount requirements, recovery semantics, and remaining models delegation boundary.
- [ ] Run all OpenMontage tests and `compileall`; review the diff; commit and push.

## Completion gates

- [ ] A completed checkpoint survives Worker death and advances the Job without a second executor call.
- [ ] A stale lease token cannot mutate Job, stage, or Artifact state.
- [ ] Approval-gated work stops in `WAITING_APPROVAL` and resumes only after an authenticated AgentSpace approval.
- [ ] Final MP4 bytes never enter MCP JSON and are published exactly once as an employee Artifact.
- [ ] Docker Worker fails closed when no external Agent executor is configured.
- [ ] Remotion/HyperFrames render E2E and models delegation billing remain explicitly separate follow-up gates.
