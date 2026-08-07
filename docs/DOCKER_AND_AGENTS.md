# Docker, CLI, MCP, and Agent Skill

OpenMontage exposes reference-video preparation through one shared application
service. The CLI and MCP server return the same project and analysis contract;
Codex or Claude then follows the repository's pipeline instructions and approval
gates to produce the new video.

All model-backed stages are fail-closed to the DoFe Airouter. Host processes use
`https://model.local.dofe.ai/api`; Compose services use the shared-network
address `${DOFE_DOCKER_MODEL_BASE_URL:-http://api:3101}`. Before choosing a
model, OpenMontage performs an authenticated `GET /v1/models` against that
effective base URL and accepts only an exact returned ID. Direct vendor model
fallbacks and guessed defaults are disabled when `DOFE_ENABLED=true`.

HeyGen is not required for the reference-clone example. The car/cinematic
workflow uses AIRouter video generation plus Remotion, HyperFrames, or FFmpeg
composition. Keep HeyGen as an optional avatar/presenter/lip-sync provider only;
do not configure or call it for this example.

AIRouter task responses include native-currency cost, settlement source, and a
sanitized pricing breakdown. Local composition has zero external model charge;
its render wall time is recorded separately and should be combined with your
host CPU/RAM pricing if infrastructure cost, rather than API spend, is required.
Do not combine different native currencies into one total without an explicit
exchange-rate source and timestamp.

Price preflight uses AIRouter's HMAC-authenticated internal quote endpoint. Set
`DOFE_TENANT_ID` and `INTERNAL_API_SECRET` in the OpenMontage deployment
environment. The Compose service sends these requests over the shared network
to `http://api:3101/internal/pricing/quote`; secrets are never returned by MCP.
The quote request uses the exact model ID selected from the current tenant
catalog. Actual spend is finalized from the provider-reported output-token usage.

## Docker deployment

Generate independent service and event-signing secrets in `.env`, then build and
start the Streamable HTTP MCP server:

```bash
openssl rand -hex 32  # OPENMONTAGE_SERVICE_TOKEN
openssl rand -hex 32  # OPENMONTAGE_EVENT_SIGNING_SECRET
# Tag the built image with the current commit so the OCI revision label carries
# real provenance instead of "unknown". `make docker-build` does this for you.
export OPENMONTAGE_IMAGE_REVISION="$(git rev-parse --verify HEAD 2>/dev/null || printf unknown)"
docker compose up --build -d openmontage-mcp
curl http://localhost:8765/healthz
```

The MCP endpoint is `http://localhost:8765/mcp`. Generated projects persist in
`./projects`; the local music library is mounted from `./music_library`.
The durable Job snapshot and transactional event outbox are stored at
`./projects/.openmontage/jobs.sqlite3`.

When AgentSpace is available, start the signed event publisher as well:

```bash
docker compose --profile agentspace up --build -d openmontage-mcp openmontage-events
```

`OPENMONTAGE_IMAGE_REVISION` (exported above) flows through the Compose build
arg into the image's `org.opencontainers.image.revision` label, so every
standard launch records the commit it was built from.

`OPENMONTAGE_SERVICE_TOKEN` authenticates AgentSpace requests to OpenMontage.
`OPENMONTAGE_EVENT_SIGNING_SECRET` signs the exact event body sent to
`OPENMONTAGE_EVENT_ENDPOINT`; use the same signing secret in the AgentSpace
event bridge. The defaults target AgentSpace on the Docker host at port `1455`.
Override the endpoint with an internal service URL when both applications share
a Docker network. Do not reuse either secret as a model-provider credential.

`OPENMONTAGE_ARTIFACT_BRIDGE_BASE_URL` is the AgentSpace internal origin used
for Job-scoped media transfer. `ArtifactBridgeClient.download_input()` obtains
a one-time READ grant and verifies the downloaded size and SHA-256 before an
atomic local publish. `upload_output()` hashes the file without buffering it,
obtains a metadata-bound WRITE grant, streams the file to AgentSpace, and
verifies the returned employee Artifact manifest. After upload, persist that
manifest with `JobService.publish_artifact()` so the ordered
`openmontage.artifact.published` event, REST query, and MCP query remain
recoverable across restarts. Media bytes never belong in MCP JSON.

## Durable Job Worker

The Job control plane does not execute creative stages inside Python. A
dedicated Worker claims a fenced SQLite lease, writes a stage assignment, and
invokes a Codex `exec` process. Delegated model execution is **Codex-only**:
Codex is the only executor that implements the tenant-catalog model-lock
protocol, so any non-Codex `OPENMONTAGE_AGENT_EXECUTOR_JSON` fails closed before
a paid task is created rather than being allowed to choose its own model. The
Agent reads the repository pipeline manifest and director skills, uses the
normal OpenMontage tools, and records its result as a validated checkpoint.
Configure the process as a JSON argv array; shell strings are intentionally
rejected:

```bash
export OPENMONTAGE_AGENT_EXECUTOR_JSON='["codex","exec","--skip-git-repo-check","--ephemeral","--ignore-user-config","-s","workspace-write","-C","/absolute/path/to/OpenMontage","--add-dir","{project_dir}","-"]'
export OPENMONTAGE_AGENT_TIMEOUT_SECONDS=3600
# Required: an exact model id visible in the delegated tenant GET /v1/models.
# The model catalog is the ONLY source of model ids — never hardcode one here.
# The Worker verifies it against the live catalog and fails closed if unset or
# invisible, so Codex can never silently fall back to the host default model.
export OPENMONTAGE_AGENT_MODEL_ID="<exact-id-from-catalog>"
# Discover it with an authenticated request to the model catalog. The key lives
# in .env (loaded into each service via the env_file directive); export it in a
# host shell before any host-side curl:
#   set -a; . ./.env; set +a
#   # Host-reachable endpoint (needs the model.local.dofe.ai CA + name resolution):
#   curl -H "Authorization: Bearer $DOFE_MODEL_API_KEY" "${DOFE_MODEL_BASE_URL:-https://model.local.dofe.ai/api}/v1/models"
#   # Container-internal only: api:3101 is Compose DNS on the shared dofe-models
#   # network, so it is NOT reachable from the host. Run it inside a service
#   # (the key is already in the container env, no .env sourcing needed):
#   docker compose exec openmontage-mcp curl -H "Authorization: Bearer $DOFE_MODEL_API_KEY" "${DOFE_DOCKER_MODEL_BASE_URL:-http://api:3101}/v1/models"
openmontage worker run --once --json
openmontage worker run --interval 2 --json
```

The command must read the assignment prompt from stdin. Its first line points
to the durable assignment JSON. The process must write `in_progress`,
`awaiting_human`, `completed`, or `failed` through the normal checkpoint
protocol; stdout is not treated as success and is not persisted. Use only flags
approved for the selected Agent CLI. The exact `{project_dir}` argv item is
replaced with the trusted current Job directory; no other interpolation is
performed. Do not put API keys, delegation secrets, or user input in argv.

For Docker, the image pins the approved `@openai/codex` CLI version and verifies
the executable during the build. A Codex executable installed on a macOS host
cannot be bind-mounted as a Linux executable. The Worker obtains a short-lived
Job delegation from AgentSpace for every stage, starts a loopback signing proxy,
and injects the key only into that stage process. The key is not stored in the
assignment, checkpoint, Compose source, argv, or executor log. The Docker
default also uses an ephemeral Codex session, ignores user configuration,
skips the absent image Git metadata check, keeps `/app` read-only at the OS
layer, and grants sandbox write access only to the current Job project. Codex
authenticates with the delegated models key; no interactive Codex account is
required. HyperFrames also runs without an account, and its anonymous telemetry
is disabled during the image build. It reuses Remotion's pinned Chrome Headless
Shell so the image does not download a second competing browser build. The image
includes the browser's Debian shared libraries, skips HyperFrames' optional CUDA
payload in favor of the bundled CPU runtime, and allocates 512 MB of shared
memory for render processes. The pinned Remotion fonts are included explicitly
in the build context, while its Webpack cache is redirected to the writable
`/data/cache` volume. Then start all three long-lived processes:

```bash
docker compose --profile agentspace up --build -d \
  openmontage-mcp openmontage-events openmontage-worker
```

`openmontage-worker` fails closed when the Agent executor, Artifact Bridge, or
AgentSpace model credential bridge configuration is absent, or when
`OPENMONTAGE_AGENT_MODEL_ID` is unset or not visible in the delegated tenant
`GET /v1/models`. Compose passes `OPENMONTAGE_AGENT_MODEL_ID` through from
`.env`; set it to an exact catalog id before starting the worker. This is
deliberate: the service does not fall back to a Python creative orchestrator, a
shared provider key, a Runtime's parent credential, or an unverified model.

### Recovery and publication semantics

- One Worker owns a Job lease at a time. Heartbeats extend active work; expiry
  lets another Worker recover it, while the old lease token is fenced from all
  Job, stage, and Artifact mutations.
- A valid completed checkpoint is reconciled before the Agent is invoked. If a
  paid stage finished just before a Worker crash, recovery advances the Job
  without repeating that stage.
- Approval checkpoints enter `WAITING_APPROVAL`. They are not claimable again
  until AgentSpace resolves the authenticated approval; the next assignment
  contains the latest approved Job snapshot.
- Artifact inputs are downloaded into `projects/<job-id>/inputs/` using a
  one-time grant. A local receipt is reused only after size and SHA-256
  verification.
- The final MP4 is accepted only from a validated `publish_log` or
  `render_report` path inside the Job workspace. Artifact Bridge upload and the
  durable `artifact.published` event complete before `job.completed`.
- AgentSpace publication is idempotent by root Task, content digest, and file
  name. A crash after upload but before local manifest persistence returns the
  same employee Artifact on retry.

The Worker carries trusted employee, Runtime Task, Job, stage, invocation, and
trace identifiers in each assignment. Before each Agent stage it retrieves the
matching active delegation from AgentSpace. Native OpenMontage tools sign their
models requests directly; Codex OpenAI-compatible traffic passes through the
loopback signing proxy. Both paths use a stable model invocation identifier for
retries, allowing models to reject replays and preserve authoritative Job and
employee attribution.

The publisher reads the same SQLite outbox as the MCP server. A successful 2xx
response marks an event delivered; network and non-2xx failures remain durable
and retry with bounded exponential backoff. Operators can perform one flush and
receive a non-zero exit code on delivery failure:

```bash
docker compose run --rm openmontage-cli events publish --once --json
```

By default, Compose joins the existing `modelsdofeai_default` network. The
Worker does not receive a static models URL or parent credential: AgentSpace
returns the approved models base URL with each stage delegation. Override the
network and AgentSpace bridge origin when deployments use different names:

```bash
DOFE_DOCKER_NETWORK=my-models-network \
OPENMONTAGE_MODEL_CREDENTIAL_BASE_URL=http://agentspace-web:1455 \
docker compose up -d openmontage-mcp
```

Run the CLI without starting a long-lived container:

```bash
docker compose run --rm openmontage-cli clone \
  "https://www.douyin.com/video/7667931266800454975" \
  --brief "Keep the pacing and camera energy, but tell an original story" \
  --json
```

## Douyin access

The downloader accepts direct videos, `v.douyin.com` short links, mobile share
links, and pasted share text. It tries current yt-dlp first and then parses the
structured JSON in Douyin's public mobile share page. This fallback does not need
an account and avoids relying on fragile HTML regex extraction.

If Douyin restricts both public paths, export a Netscape-format `cookies.txt`
from a browser session you are authorized to use, then mount it read-only:

```bash
export OPENMONTAGE_YTDLP_COOKIES_HOST=/absolute/path/to/cookies.txt
docker compose -f compose.yaml -f deploy/compose.cookies.yaml up -d openmontage-mcp
```

The application never reads browser profiles automatically and never returns
cookie contents through CLI or MCP responses.

If a shared Docker network is unavailable, use the HTTPS host-gateway fallback.
For an internal or mkcert certificate, mount its PEM root CA read-only so TLS
verification remains enabled:

```bash
export DOFE_CA_BUNDLE_HOST="/absolute/path/to/rootCA.pem"
docker compose \
  -f compose.yaml \
  -f deploy/compose.host-airouter.yaml \
  -f deploy/compose.ca.yaml \
  up -d openmontage-mcp
```

## Local CLI

Install the package or run it ephemerally with uv:

```bash
python -m pip install -e .
openmontage clone "<video-url>" --brief "<new creative direction>" --json

# No installation:
uv run --with-editable . openmontage clone "<video-url>" --json
```

Useful commands:

```bash
openmontage capabilities --json
openmontage status <project-id> --json
openmontage mcp --transport stdio
openmontage mcp --transport streamable-http --host 0.0.0.0 --port 8765
openmontage events publish --once --json
openmontage worker run --once --json
openmontage worker run --interval 2 --json
```

`clone` prepares the production: it downloads and analyzes the reference, writes
`video_analysis_brief.json`, performs preflight, initializes the suggested
pipeline, and returns its first stage. The agent remains responsible for creative
decisions and approval gates; this intentionally preserves OpenMontage's
instruction-driven architecture.

## Codex

The project skill is `.agents/skills/recreate-video/SKILL.md`. Project-local
`.codex/config.toml` registers the stdio MCP server. Trust the repository and
restart Codex after first checkout so both are discovered.

Invoke it with:

```text
Use $recreate-video with https://www.douyin.com/video/7667931266800454975.
Make the new version about a different subject but keep the rapid cinematic pacing.
```

For a Docker-hosted MCP connection instead, register the Streamable HTTP URL
`http://localhost:8765/mcp` in the user's Codex MCP configuration.

## Claude Code

Claude discovers `.claude/skills/recreate-video/SKILL.md`, which points to the
same authoritative skill. The committed `.mcp.json` registers the local stdio
server. Approve the project MCP server when Claude Code prompts on first use.

Invoke it with:

```text
Use /recreate-video on this Douyin link: <url>. Create an original 9:16 version.
```

## MCP tools

- `prepare_reference_clone`: download, analyze, preflight, and initialize.
- `openmontage_capabilities`: compact provider/runtime readiness summary.
- `reference_clone_status`: current project and next pipeline stage.
- `submit_video_job`: create an asynchronous, attributable video Job.
- `get_video_job`: return the durable Job and manifest-derived stage snapshot.
- `list_video_job_events`: replay ordered Job events after a sequence cursor.
- `list_video_artifacts`: list durable AgentSpace-backed outputs for a Job.
- `cancel_video_job(job_id, expected_sequence, idempotency_key)`: request cooperative
  cancellation. `expected_sequence` is the current `lastSequence` from `get_video_job`
  or event replay; `idempotency_key` is a caller-generated stable key for retries.
- `approve_video_stage(job_id, stage, expected_sequence, idempotency_key, approved=true)`:
  approve or reject a pending human gate. `expected_sequence` and `idempotency_key`
  follow the same optimistic-fencing contract as `cancel_video_job`.
- `openmontage://reference-clone-guide`: shared agent workflow resource.

The Job tools never accept workspace, employee, conversation, task, invocation,
or trace attribution as model-controlled arguments. The authenticated AgentSpace
gateway supplies that context through the transport. REST equivalents are
available under `/api/v1/jobs`; cross-workspace reads return `404`.

Only transform references the user is authorized to use. The workflow requires
creative differentiation and does not promise a frame-for-frame copy.
