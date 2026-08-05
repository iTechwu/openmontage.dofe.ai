"""Model Context Protocol server for Codex and Claude clients."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

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
            "and the OpenMontage pipeline approval gates."
        ),
        version="0.2.0",
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
    def submit_video_job(request: dict[str, Any], ctx: Context) -> dict[str, Any]:
        """Create an asynchronous video Job using trusted Gateway attribution."""
        from openmontage.contracts import JobCreateRequest

        attribution = resolve_attribution(ctx.headers)
        return jobs().create_job(JobCreateRequest.model_validate(request), attribution).to_wire()

    @server.tool()
    def get_video_job(job_id: str, ctx: Context) -> dict[str, Any]:
        """Return a durable video Job snapshot."""
        from openmontage.job_api import require_same_workspace

        attribution = resolve_attribution(ctx.headers)
        snapshot = jobs().get_job(job_id)
        require_same_workspace(snapshot, attribution)
        return snapshot.to_wire()

    @server.tool()
    def cancel_video_job(job_id: str, ctx: Context) -> dict[str, Any]:
        """Request cancellation of a video Job."""
        from openmontage.job_api import require_same_workspace

        attribution = resolve_attribution(ctx.headers)
        snapshot = jobs().get_job(job_id)
        require_same_workspace(snapshot, attribution)
        return jobs().request_cancel(job_id).to_wire()

    @server.tool()
    def approve_video_stage(
        job_id: str,
        stage: str,
        ctx: Context,
        approved: bool = True,
    ) -> dict[str, Any]:
        """Resolve a pending video stage approval."""
        from openmontage.job_api import require_same_workspace

        attribution = resolve_attribution(ctx.headers)
        snapshot = jobs().get_job(job_id)
        require_same_workspace(snapshot, attribution)
        return jobs().resolve_stage_approval(job_id, stage, approved=approved).to_wire()

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
