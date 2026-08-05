# Docker, CLI, MCP, and Agent Skill

OpenMontage exposes reference-video preparation through one shared application
service. The CLI and MCP server return the same project and analysis contract;
Codex or Claude then follows the repository's pipeline instructions and approval
gates to produce the new video.

All model-backed stages are fail-closed to the DoFe Airouter at
`https://model.local.dofe.ai/api`. Direct vendor model fallbacks are disabled
when `DOFE_ENABLED=true`; the container receives the gateway key from `.env`.
Video generation defaults to `seedance-2.0-fast`; recording-file STT uses the
restricted `openspeech-auc` alias. Grant that alias to the deployment tenant
and configure a positive tenant-effective `PER_MINUTE` price before expecting
reference transcription to pass preflight. AIRouter rejects zero-priced or
unresolved pricing before task creation so provider spend cannot bypass billing.

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
For `seedance-2.0-fast`, the current tenant rate card is CNY 37/M output tokens
for text-to-video and CNY 22/M output tokens when the request has video/image
input. Actual spend is finalized from the provider-reported output-token usage.

## Docker deployment

Generate independent service and event-signing secrets in `.env`, then build and
start the Streamable HTTP MCP server:

```bash
openssl rand -hex 32  # OPENMONTAGE_SERVICE_TOKEN
openssl rand -hex 32  # OPENMONTAGE_EVENT_SIGNING_SECRET
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

The publisher reads the same SQLite outbox as the MCP server. A successful 2xx
response marks an event delivered; network and non-2xx failures remain durable
and retry with bounded exponential backoff. Operators can perform one flush and
receive a non-zero exit code on delivery failure:

```bash
docker compose run --rm openmontage-cli events publish --once --json
```

By default, Compose joins the existing `modelsdofeai_default` network and uses
the Airouter API service directly at `http://api:3101`. Override these names when
the models deployment uses a different Compose project or service address:

```bash
DOFE_DOCKER_NETWORK=my-models-network \
DOFE_INTERNAL_BASE_URL=http://models-api:3101 \
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
- `list_video_job_events`: replay ordered events after a sequence cursor.
- `list_video_artifacts`: list durable AgentSpace-backed outputs for a Job.
- `cancel_video_job`: request cooperative cancellation.
- `approve_video_stage`: approve or reject a pending human gate.
- `openmontage://reference-clone-guide`: shared agent workflow resource.

The Job tools never accept workspace, employee, conversation, task, invocation,
or trace attribution as model-controlled arguments. The authenticated AgentSpace
gateway supplies that context through the transport. REST equivalents are
available under `/api/v1/jobs`; cross-workspace reads return `404`.

Only transform references the user is authorized to use. The workflow requires
creative differentiation and does not promise a frame-for-frame copy.
