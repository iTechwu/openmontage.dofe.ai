"""Model Context Protocol server for Codex and Claude clients."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from openmontage.contracts import JobCreateRequest
from openmontage.exchange import ProjectFileExporter
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
    def reference_clone_status(project_id: str) -> dict[str, Any]:
        """Return the prepared project's analysis and next pipeline stage."""
        return ReferenceCloneService().status(project_id)

    @server.tool()
    def submit_video_job(request: JobCreateRequest, ctx: Context) -> dict[str, Any]:
        """Create a video Job; request.workflow must name a pipeline, not a stage."""
        attribution = resolve_attribution(ctx.headers)
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

        Returns the file's public URL (``http://127.0.0.1:18090/...``) and the
        harness host path (``/exchange/...``). Copying is on demand and, by default,
        skips large media files, so the small analysis outputs (brief, keyframes, scenes,
        transcript, request JSON) are what get mirrored. Pass ``include_media=true`` to
        also mirror a media file (e.g. the reference video).
        """
        return ProjectFileExporter().export(project_id, relative_path, include_media=include_media)

    @server.tool()
    def sync_project_exports(project_id: str) -> dict[str, Any]:
        """Mirror a prepared project's whole analysis set into the shared file-server.

        Copies the artifacts, keyframes, scenes, transcript, briefs and manifest —
        everything the agent inspects — while leaving large media files uncopied. Returns
        the export root URL/host path and the list of files now available.
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
) -> Any:
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from openmontage.job_api import (
        create_job_routes,
        default_attribution_resolver,
        default_job_service,
    )

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

    routes = [Route("/healthz", health, methods=["GET"]), *create_job_routes(service, resolver)]
    for route in reversed(routes):
        app.routes.insert(0, route)
    return app


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
