"""Model Context Protocol server for Codex and Claude clients."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import Field, StrictInt

from openmontage.contracts import (
    ClientRequestId,
    JobBrief,
    JobBudget,
    JobCreateRequest,
    JobInput,
    JobOutput,
    WorkflowName,
)
from openmontage.exchange import ProjectFileExporter
from openmontage.instruction_files import read_instruction_file
from openmontage.reference_clone import ReferenceCloneService, capability_summary

try:
    from mcp.server.mcpserver.context import Context
except ImportError:  # pragma: no cover - create_server reports the actionable dependency error
    Context = Any  # type: ignore[misc,assignment]


def create_server(
    *,
    job_service: Any = None,
    attribution_resolver: Any = None,
) -> Any:
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise RuntimeError("Install MCP support with: pip install 'mcp>=2,<3'") from exc

    server = MCPServer(
        "OpenMontage",
        description="Prepare and inspect agent-led reference-video productions.",
        instructions=(
            "Use prepare_reference_clone when a user provides a video URL and wants a new, "
            "creatively differentiated video. Then follow the returned agent_instructions "
            "and the OpenMontage pipeline approval gates. Before submit_video_job, call "
            "openmontage_capabilities and follow its job_submission contract; workflow is a "
            "pipeline name, never a stage name such as compose."
        ),
        version="0.3.0",
    )

    def jobs() -> Any:
        nonlocal job_service
        if job_service is None:
            from openmontage.job_api import default_job_service

            job_service = default_job_service()
        return job_service

    def tool_gateway() -> Any:
        from openmontage.tool_gateway import ToolGateway

        return ToolGateway(jobs())

    def resolve_attribution(headers: Mapping[str, str] | None) -> Any:
        nonlocal attribution_resolver
        if attribution_resolver is None:
            from openmontage.job_api import default_attribution_resolver

            attribution_resolver = default_attribution_resolver()
        return attribution_resolver(headers)

    @server.tool()
    def prepare_reference_clone(
        source: str,
        project_id: str = "",
        pipeline_type: str = "auto",
        title: str = "",
        creative_brief: str = "",
        analysis_depth: Literal["transcript_only", "standard", "deep"] = "standard",
        max_keyframes: int = 20,
        max_resolution: Literal["360p", "480p", "720p", "1080p"] = "720p",
        cookie_file: str = "",
    ) -> dict[str, Any]:
        """Download/analyze a video URL (including Douyin) and prepare a new project."""
        return ReferenceCloneService().prepare(
            source,
            project_id=project_id,
            pipeline_type=pipeline_type,
            title=title,
            creative_brief=creative_brief,
            analysis_depth=analysis_depth,
            max_keyframes=max_keyframes,
            max_resolution=max_resolution,
            cookie_file=cookie_file,
        )

    @server.tool()
    def openmontage_capabilities() -> dict[str, Any]:
        """Return the compact provider and composition preflight summary."""
        return capability_summary()

    @server.tool()
    def invoke_openmontage_tool(
        tool_name: str,
        operation: Literal["catalog", "generate", "preflight", "rank", "progress"],
        inputs: dict[str, Any],
        ctx: Context,
        job_id: str = "",
        stage: str = "",
        stage_attempt: int | None = None,
        lease_token: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Execute one fixed logical CI tool through the server ToolRegistry.

        ``operation`` is the gateway lifecycle: ``catalog`` lists the exposed
        tools and their input schemas, ``generate`` runs the tool, ``preflight``
        validates the selected provider without generating, ``rank`` returns
        scored provider rankings, and ``progress`` reports progress. Tool-
        specific operations (e.g. video_selector's text_to_video / image_to_video
        / reference_to_video) go inside ``inputs``, not here.
        """
        from openmontage.job_api import require_same_workspace
        from openmontage.tool_gateway import ToolGatewayError

        try:
            attribution = resolve_attribution(ctx.headers)
            if operation == "catalog":
                return tool_gateway().invoke(tool_name=tool_name, operation=operation, inputs=inputs)
            snapshot = jobs().get_job(job_id)
            require_same_workspace(snapshot, attribution)
            return tool_gateway().invoke(
                tool_name=tool_name, operation=operation, inputs=inputs, job_id=job_id,
                stage=stage, stage_attempt=stage_attempt, lease_token=lease_token,
                idempotency_key=idempotency_key,
            )
        except ToolGatewayError as exc:
            return {
                "success": False,
                "status": "failed",
                "error": {"code": exc.code, "category": exc.category, "message": exc.message},
            }

    @server.tool()
    def reference_clone_status(project_id: str) -> dict[str, Any]:
        """Return the prepared project's analysis and next pipeline stage."""
        return ReferenceCloneService().status(project_id)

    @server.tool()
    def submit_video_job(
        clientRequestId: ClientRequestId,
        workflow: Annotated[
            WorkflowName,
            Field(
                description=(
                    "Pipeline manifest name from pipeline_defs; stage names such as compose "
                    "are invalid."
                )
            ),
        ],
        input: JobInput,
        brief: JobBrief,
        output: JobOutput,
        budget: JobBudget,
        ctx: Context,
        schemaVersion: Annotated[StrictInt, Field(ge=1, le=1)] = 1,
    ) -> dict[str, Any]:
        """Create a video Job.

        contract:
          Pass every field directly as a tool argument. Do not wrap them in request or arguments.
          workflow: a pipeline name (e.g. "animation"), never a stage (compose is
            a stage, not a workflow).
          input: use the TEXT branch — {"type":"text","inlineText":"<creative
            brief / concept>"}. Do NOT use the ARTIFACT branch {"type":"artifact",
            "artifactId":"..."} to reference a prepared project: a project id (clone-...)
            is not an artifact and is rejected at submission. artifactId is only for a real
            file already uploaded through the artifact bridge.
          brief/{title,durationSeconds,audience}, output/{container,resolution,fps},
            budget/{maxAmount,currency}, clientRequestId (idempotency key).
        """
        attribution = resolve_attribution(ctx.headers)
        request = JobCreateRequest(
            schema_version=schemaVersion,
            client_request_id=clientRequestId,
            workflow=workflow,
            input=input,
            brief=brief,
            output=output,
            budget=budget,
        )
        return jobs().create_job(request, attribution).to_wire()

    @server.tool()
    def get_video_job(job_id: str, ctx: Context) -> dict[str, Any]:
        """Return a durable video Job snapshot."""
        from openmontage.job_api import require_same_workspace

        attribution = resolve_attribution(ctx.headers)
        snapshot = jobs().get_job(job_id)
        require_same_workspace(snapshot, attribution)
        return snapshot.to_wire()

    @server.tool()
    def cancel_video_job(
        job_id: str,
        expected_sequence: int,
        idempotency_key: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Request cancellation with optimistic fencing and a stable retry key.

        Args:
            job_id: Durable Job identifier.
            expected_sequence: Current ``lastSequence`` from the Job snapshot or
                event replay. The request is rejected if the Job has moved past
                this sequence.
            idempotency_key: Caller-generated stable key. Retries with the same
                key and sequence return the same result without duplicate events.
        """
        from openmontage.job_api import require_same_workspace

        attribution = resolve_attribution(ctx.headers)
        snapshot = jobs().get_job(job_id)
        require_same_workspace(snapshot, attribution)
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        return jobs().request_cancel(
            job_id,
            expected_sequence=expected_sequence,
            idempotency_key=idempotency_key,
        ).to_wire()

    @server.tool()
    def approve_video_stage(
        job_id: str,
        stage: str,
        expected_sequence: int,
        idempotency_key: str,
        ctx: Context,
        approved: bool = True,
    ) -> dict[str, Any]:
        """Resolve approval with optimistic fencing and a stable retry key.

        Args:
            job_id: Durable Job identifier.
            stage: Stage code waiting for approval, e.g. ``proposal``.
            expected_sequence: Current ``lastSequence`` observed for the Job.
                Rejected if the Job has moved past this sequence.
            idempotency_key: Caller-generated stable key for idempotent retries.
            approved: ``True`` to approve the gate, ``False`` to reject it.
        """
        from openmontage.job_api import require_same_workspace

        attribution = resolve_attribution(ctx.headers)
        snapshot = jobs().get_job(job_id)
        require_same_workspace(snapshot, attribution)
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        return jobs().resolve_stage_approval(
            job_id,
            stage,
            approved=approved,
            expected_sequence=expected_sequence,
            idempotency_key=idempotency_key,
        ).to_wire()

    @server.tool()
    def begin_client_stage(
        job_id: str,
        stage: str,
        idempotency_key: str,
        ctx: Context,
        expected_sequence: int | None = None,
    ) -> dict[str, Any]:
        """Begin exclusive client-side execution of one pipeline stage.

        The client Agent drives one stage at a time: begin (lease + attempt),
        read instructions with ``read_openmontage_file``, do the cognitive
        work, call Gateway tools as usual, report progress with
        ``update_client_stage_progress``, then finish with
        ``submit_client_stage``. Returns an opaque ``leaseToken``,
        ``stageAttempt``, ``leaseExpiresAt`` and the latest Job snapshot.
        Replays of the same ``idempotency_key`` return the original result;
        a second live owner is rejected with ``STAGE_ALREADY_OWNED``.
        """
        from openmontage.job_api import require_same_workspace

        attribution = resolve_attribution(ctx.headers)
        snapshot = jobs().get_job(job_id)
        require_same_workspace(snapshot, attribution)
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        return jobs().begin_client_stage(
            job_id,
            stage,
            idempotency_key=idempotency_key,
            expected_sequence=expected_sequence,
        ).to_wire()

    @server.tool()
    def update_client_stage_progress(
        job_id: str,
        stage: str,
        stage_attempt: int,
        completed_units: int,
        total_units: int,
        label_code: str,
        lease_token: str,
        idempotency_key: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Report progress for a running client stage and renew its lease.

        Requires the ``leaseToken`` and ``stageAttempt`` returned by
        ``begin_client_stage``. Repeated calls with the same
        ``idempotency_key`` are safe replays.
        """
        from openmontage.job_api import require_same_workspace

        attribution = resolve_attribution(ctx.headers)
        snapshot = jobs().get_job(job_id)
        require_same_workspace(snapshot, attribution)
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        return jobs().update_client_stage_progress(
            job_id,
            stage,
            stage_attempt=stage_attempt,
            completed_units=completed_units,
            total_units=total_units,
            label_code=label_code,
            lease_token=lease_token,
            idempotency_key=idempotency_key,
        ).to_wire()

    @server.tool()
    def submit_client_stage(
        job_id: str,
        stage: str,
        stage_attempt: int,
        status: str,
        lease_token: str,
        idempotency_key: str,
        ctx: Context,
        artifacts: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        instruction_provenance: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Submit a client stage's artifacts, checkpoint and status atomically.

        ``status`` is one of ``completed`` / ``awaiting_human`` / ``failed`` /
        ``in_progress``. The server validates the lease, artifact and
        checkpoint schemas, approval rules and media references; writes the
        standard checkpoint under the CI project directory; records the Job
        event; and advances the Job. ``instruction_provenance`` is a list of
        ``{"path", "content_hash"}`` entries from ``read_openmontage_file``
        proving which instructions the client followed. Gated stages must be
        submitted as ``awaiting_human`` and completed only after
        ``approve_video_stage`` approves them.
        """
        from openmontage.job_api import require_same_workspace

        attribution = resolve_attribution(ctx.headers)
        snapshot = jobs().get_job(job_id)
        require_same_workspace(snapshot, attribution)
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        return jobs().submit_client_stage(
            job_id,
            stage,
            stage_attempt=stage_attempt,
            status=status,
            lease_token=lease_token,
            idempotency_key=idempotency_key,
            artifacts=artifacts,
            metadata=metadata,
            instruction_provenance=instruction_provenance,
        ).to_wire()

    @server.tool()
    def list_video_job_events(
        job_id: str,
        ctx: Context,
        after_sequence: int = 0,
    ) -> dict[str, Any]:
        """Replay ordered Job events after a sequence cursor."""
        from openmontage.job_api import require_same_workspace

        attribution = resolve_attribution(ctx.headers)
        snapshot = jobs().get_job(job_id)
        require_same_workspace(snapshot, attribution)
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        return {
            "events": [
                event.to_wire()
                for event in jobs().list_events(job_id, after_sequence=after_sequence)
            ],
            "lastSequence": snapshot.last_sequence,
        }

    @server.tool()
    def list_video_artifacts(job_id: str, ctx: Context) -> dict[str, Any]:
        """List durable video outputs published for a Job."""
        from openmontage.job_api import require_same_workspace

        attribution = resolve_attribution(ctx.headers)
        snapshot = jobs().get_job(job_id)
        require_same_workspace(snapshot, attribution)
        return {
            "artifacts": [artifact.to_wire() for artifact in snapshot.artifacts],
            "lastSequence": snapshot.last_sequence,
        }

    @server.tool()
    def list_project_files(project_id: str) -> dict[str, Any]:
        """List the files generated for a prepared reference project.

        Returns every file's project-relative path and size (metadata only; nothing is
        copied), plus the shared file-server export root when the file-server exporter
        is enabled. Use ``export_project_file`` to mirror a specific file into the
        shared exchange so the workspace agent can read it.
        """
        return ProjectFileExporter().list(project_id)

    @server.tool()
    def export_project_file(project_id: str, relative_path: str, include_media: bool = False) -> dict[str, Any]:
        """Mirror one project file (or a whole directory) into the shared file-server.

        Returns CI shared-mount references. Remote clients should use
        ``read_project_file`` for text; the loopback URL is CI-internal. Copying is on
        demand and, by default, skips large media files; pass ``include_media=true`` to
        mirror a media file (e.g. the reference video).
        """
        return ProjectFileExporter().export(project_id, relative_path, include_media=include_media)

    @server.tool()
    def read_project_file(project_id: str, relative_path: str, max_bytes: int = 2_000_000) -> dict[str, Any]:
        """Read a bounded UTF-8 analysis file through the authenticated MCP channel."""
        return ProjectFileExporter().read_text(project_id, relative_path, max_bytes=max_bytes)

    @server.tool()
    def read_openmontage_file(path: str, max_bytes: int = 2_000_000) -> dict[str, Any]:
        """Read an OpenMontage instruction file (Markdown/YAML/JSON) from CI.

        Reads the live CI repository on every call — no client-side caching and
        no logical skill-ID mapping. Only ``.md``/``.yaml``/``.yml``/``.json``
        files under the allowed instruction roots (``AGENT_GUIDE.md``,
        ``pipeline_defs/``, ``skills/``, ``.agents/skills/``, ``schemas/``,
        ``styles/``, ``remotion-composer/public/``, ``docs/``) are served; the
        response includes the actual server path, size, mtime, a SHA-256
        content hash, and the repository revision for instruction provenance.
        Project artifacts and checkpoints belong to ``read_project_file``.
        """
        return read_instruction_file(path, max_bytes=max_bytes)

    @server.tool()
    def sync_project_exports(project_id: str) -> dict[str, Any]:
        """Mirror a prepared project's whole analysis set into the shared file-server.

        Copies the artifacts, keyframes, scenes, transcript, briefs and manifest —
        everything the agent inspects — while leaving large media files uncopied. Returns
        the DSH-readable ``export_file_path`` (``/exchange/...``) and the list of files
        now available; each entry's ``file_path`` is what the DSH GUI can open.
        """
        return ProjectFileExporter().export_analysis(project_id)

    @server.tool()
    def cleanup_exports(project_id: str = "", max_age_days: float = 7.0, max_bytes: int = 0) -> dict[str, Any]:
        """Prune stale or over-budget project mirrors to keep the exchange healthy.

        Removes mirror files not modified within ``max_age_days`` and, when
        ``max_bytes`` is positive, evicts the oldest files until the mirror is under
        budget. Limits to one project when ``project_id`` is given. Only the mirror is
        touched; the authoritative project under ``/data/projects`` is never modified.
        """
        exporter = ProjectFileExporter()
        return exporter.cleanup(
            project_id=project_id or None,
            max_age_days=max_age_days,
            max_bytes=max_bytes or None,
        )

    @server.resource("openmontage://reference-clone-guide")
    def reference_clone_guide() -> str:
        """Return the authoritative agent workflow for URL-driven video recreation."""
        from lib.paths import REPO_ROOT

        return (REPO_ROOT / ".agents" / "skills" / "recreate-video" / "SKILL.md").read_text(
            encoding="utf-8"
        )

    return server


def build_http_app(
    host: str = "127.0.0.1",
    *,
    job_service: Any = None,
    attribution_resolver: Any = None,
    now_fn: Any = None,
) -> Any:
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from openmontage.job_api import (
        create_job_routes,
        default_attribution_resolver,
        default_job_service,
    )
    from openmontage.mcp_gateway_auth import McpGatewayAuthMiddleware

    service = job_service or default_job_service()
    resolver = attribution_resolver or default_attribution_resolver()
    server = create_server(job_service=service, attribution_resolver=resolver)
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host=host,
    )

    async def health(_: Any) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "openmontage-mcp"})

    routes = [
        Route("/healthz", health, methods=["GET"]),
        *create_job_routes(service, resolver, now_fn=now_fn),
    ]
    for route in reversed(routes):
        app.routes.insert(0, route)
    return McpGatewayAuthMiddleware(
        app,
        gateway_only=os.environ.get("OPENMONTAGE_MCP_GATEWAY_ONLY", "false").strip().lower()
        in {"1", "true", "yes", "on"},
    )


def run_server(
    transport: Literal["stdio", "streamable-http"] = "stdio",
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    server = create_server()
    if transport == "stdio":
        server.run("stdio")
        return
    import uvicorn

    uvicorn.run(build_http_app(host), host=host, port=port, log_level="info")


def main() -> None:
    from openmontage.cli import main as cli_main

    raise SystemExit(cli_main(["mcp"]))
