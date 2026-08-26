# Runtime Integration (caller-is-the-runtime)

OpenMontage is a **pure MCP server**: it prepares projects, accepts Jobs, and
records checkpoints, but it never executes a creative stage itself. The runtime
that calls OpenMontage — DeepSeek Harness (DSH), Codex, Claude Code, or any
agent host — owns execution. That runtime runs `openmontage worker run`, and for
every stage the Worker invokes the runtime's **agent executor** (an argv from
`OPENMONTAGE_AGENT_EXECUTOR_JSON`) to do the actual creative work.

This is what keeps OpenMontage independent: it has no worker, no codex, and no
AgentSpace coupling baked in. Any runtime that satisfies the contract below can
drive it.

```
  ┌─────────────────┐   MCP (streamable-http)   ┌──────────────────────┐
  │  OpenMontage    │ ◄─────────────────────── │  caller runtime      │
  │  (MCP server)   │                           │  (DSH / codex / cc) │
  │  no worker      │                           │  ├─ worker run       │
  └─────────────────┘                           │  └─ agent executor   │
                                                └──────────────────────┘
                                                        │
                                        shared projects dir (SQLite job DB)
```

## The executor contract

`openmontage/pipeline_executor.py` (`AgentCommandPipelineExecutor`) is the
single source of truth for how a stage is dispatched. The executor is just an
argv array; the Worker feeds it the assignment and reconciles the checkpoint it
writes.

### Configuration (on the Worker process env)

| Env var | Meaning |
|---|---|
| `OPENMONTAGE_AGENT_EXECUTOR_JSON` | JSON argv array, e.g. `["node","/opt/…/agent-run.js"]`. The literal `{project_dir}` item is replaced with the trusted Job directory; no other interpolation is performed. Shell strings are rejected. |
| `OPENMONTAGE_AGENT_MODEL_ID` | Exact model id, verified against the live `GET /v1/models` catalog. Never hardcode an unverified id. |
| `OPENMONTAGE_AGENT_TIMEOUT_SECONDS` | Stage wall-clock limit (default `3600`). |

### Invocation (per stage)

1. The Worker atomically writes the assignment to
   `projects/<jobId>/.openmontage/assignments/<stage>-attempt-<n>.json`
   (`StageAssignment.to_wire()`): `jobId`, `projectId`, `projectsDir`,
   `projectDir`, `pipeline`, `pipelineVersion`, `stage`, `stageAttempt`,
   `directorSkill`, `request`, `attribution`, `jobSnapshot`, `localInputs`.
2. It spawns the executor argv with:
   - **stdin**: first line `OPENMONTAGE_ASSIGNMENT_PATH="<path>"` (JSON-escaped),
     followed by the human/agent stage prompt;
   - **cwd**: the OpenMontage checkout root, so `from lib.checkpoint import …`
     and `from tools…` resolve;
   - **env**: a sanitized copy of the Worker env (control-plane secrets and
     `*_KEY`/`*_SECRET`/`*_TOKEN` values are stripped) plus the OpenAI-compatible
     gateway route: `OPENAI_API_KEY`, `OPENAI_BASE_URL` (`<gateway>/v1`),
     `DOFE_MODEL_API_KEY`, `DOFE_MODEL_BASE_URL`.

### What the executor must do

1. Read the assignment path from the first stdin line, then the assignment JSON.
2. Do **exactly one** pipeline stage, working only inside `projectDir`, using
   the gateway route from the env (never a hardcoded vendor endpoint or key).
3. Write a checkpoint through the repo's own entry point
   `lib.checkpoint.write_checkpoint(projectsDir, projectId, stage, status,
   artifacts, pipeline_type=pipeline, …)` — see
   [`skills/meta/checkpoint-protocol.md`](../skills/meta/checkpoint-protocol.md).
   `status` is one of `completed`, `awaiting_human`, `in_progress`, `failed`.
   Write an `in_progress` heartbeat before consequential work.
4. Exit `0` for a terminal `completed`/`awaiting_human`; exit non-zero for
   `failed`. stdout is diagnostic only — it is never treated as the result.

### What the Worker validates after the executor exits

- a checkpoint exists (else `PipelineExecutionIncomplete`);
- its identity matches the assignment (`project_id`, `pipeline_type`, `stage`);
- no checkpoint **outside** the assigned stage changed;
- exit code is `0`.

The checkpoint is the execution result; a missing or identity-mismatched
checkpoint fails the stage regardless of stdout.

## Two execution modes

- **Self-contained (default, clean-MCP).** No AgentSpace. The Worker uses
  OpenMontage's own gateway credential + the pinned `OPENMONTAGE_AGENT_MODEL_ID`
  and injects the gateway route into the executor. This is the path DSH uses.
- **Delegated (AgentSpace).** The Worker obtains a per-stage delegation and a
  short-lived signing proxy. **Delegated model-lock is Codex-only**: any
  non-Codex executor fails closed before a paid task is created.

## Worker bootstrap

`scripts/run-worker.sh` provisions the OpenMontage **client** (package deps,
ffmpeg, remotion node_modules) into a persistent directory and runs
`openmontage worker run`. It is runtime-agnostic: it does **not** provision or
choose your agent executor — that is the caller's job. Provision your runtime's
executor first, then:

```bash
# 1. Provision the executor for YOUR runtime (see recipes below).
# 2. Point OpenMontage at it and run the worker:
export OPENMONTAGE_AGENT_EXECUTOR_JSON='["node","/opt/…/agent-run.js"]'
export OPENMONTAGE_AGENT_MODEL_ID='<exact-catalog-id>'
OPENMONTAGE_PROJECTS_DIR=/data/projects \
  bash scripts/run-worker.sh --provision && bash scripts/run-worker.sh --run
```

`--provision` is idempotent (fast no-op once populated); `--run` execs the
worker loop. Run both in a supervisor or `restart: unless-stopped` container so
the worker self-heals.

## Per-runtime recipes

### DeepSeek Harness (DSH) — reference implementation

Executor: the `dsh-agent-run` plugin (`deepseek-harness/plugins/dsh-agent-run`),
a headless harness spine that drives one agent turn and writes the checkpoint
through `lib.checkpoint` itself.

```bash
export OPENMONTAGE_AGENT_EXECUTOR_JSON='["node","/opt/dsh-plugins/dsh-agent-run/bin/agent-run.js"]'
```

The DSH deployment (`docker-helm.dofe.ai`) runs this inside the DSH container
with a supervisor that boots the web UI (as `node`) plus the worker (as root),
wired in `scripts/dsh-supervisor.sh` + `scripts/openmontage-runtime.sh`. It is
the canonical end-to-end example of this contract.

### Codex

```bash
npm install -g @openai/codex@0.146.0   # match PINNED_CODEX_CLI_VERSION
export OPENMONTAGE_AGENT_EXECUTOR_JSON='["codex","exec","--skip-git-repo-check","--ephemeral","--ignore-user-config","-s","workspace-write","-C","/abs/path/to/OpenMontage","--add-dir","{project_dir}","-"]'
```

Codex is also the only executor that implements the **delegated** model-lock
protocol, so it is required when Jobs carry an AgentSpace-delegated credential.

### Claude Code

```bash
export OPENMONTAGE_AGENT_EXECUTOR_JSON='["claude","-p","--allowedTools","Bash","-"]'
```

Claude Code follows the same contract via the stage prompt + checkpoint
protocol. If the pinned CLI cannot produce the `lib.checkpoint`-format file
directly, wrap it in a thin script that reads the assignment path from stdin,
invokes `claude -p` with the prompt, and then writes the checkpoint through
`lib.checkpoint.write_checkpoint` (mirroring `dsh-agent-run`'s step 8).

## Failing closed

`submit_video_job` and the Worker both fail closed while the executor is
unset/unresolvable, or when `OPENMONTAGE_AGENT_MODEL_ID` is unset/invisible to
the catalog. `openmontage_capabilities` reports this under
`job_submission.delegated_execution`. Never fall back to a shared provider key,
a parent credential, or an unverified model.
