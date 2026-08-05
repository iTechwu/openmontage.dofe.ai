"""Model Context Protocol server for Codex and Claude clients."""

from __future__ import annotations

from typing import Any, Literal

from openmontage.reference_clone import ReferenceCloneService, capability_summary


def create_server() -> Any:
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

    @server.resource("openmontage://reference-clone-guide")
    def reference_clone_guide() -> str:
        """Return the authoritative agent workflow for URL-driven video recreation."""
        from lib.paths import REPO_ROOT

        return (REPO_ROOT / ".agents" / "skills" / "recreate-video" / "SKILL.md").read_text(
            encoding="utf-8"
        )

    return server


def build_http_app(host: str = "127.0.0.1") -> Any:
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    server = create_server()
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host=host,
    )

    async def health(_: Any) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "openmontage-mcp"})

    app.routes.insert(0, Route("/healthz", health, methods=["GET"]))
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
