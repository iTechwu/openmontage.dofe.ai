"""Model Context Protocol server for Codex and Claude clients."""

from __future__ import annotations

import base64
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


class StageContractError(RuntimeError):
    """The authoritative pipeline contract cannot be served safely."""


def stage_execution_contract(snapshot: Any, stage_code: str) -> dict[str, Any]:
    """Return the exact model-facing contract for one manifest stage."""
    from lib.pipeline_loader import load_pipeline_readonly
    from openmontage.client_stage_driver import PER_STAGE_INSTRUCTIONS
    from openmontage.tool_gateway import gateway_tools_for_declared

    try:
        manifest = load_pipeline_readonly(snapshot.workflow.name)
        stage_definition = next(
            item for item in manifest["stages"] if item["name"] == stage_code
        )
        stage_snapshot = next(item for item in snapshot.stages if item.code == stage_code)
        declared_tools = stage_definition.get("tools_available", [])
        produces = stage_definition.get("produces", [])
        if not isinstance(produces, list) or not all(
            isinstance(item, str) for item in produces
        ):
            raise TypeError("produces must be a list of strings")

        instruction_files = [
            "AGENT_GUIDE.md",
            f"pipeline_defs/{snapshot.workflow.name}.yaml",
        ]
        skill = stage_definition.get("skill")
        if isinstance(skill, str) and skill:
            instruction_files.append(f"skills/{skill}.md")
        instruction_files.extend(
            ["skills/meta/checkpoint-protocol.md", "skills/meta/reviewer.md"]
        )
        instruction_files.extend(PER_STAGE_INSTRUCTIONS.get(stage_code, ()))
        return {
            "declaredTools": list(declared_tools),
            "gatewayTools": gateway_tools_for_declared(declared_tools),
            "produces": list(produces),
            "humanApprovalRequired": bool(stage_snapshot.approval_required),
            "instructionFiles": instruction_files,
        }
    except Exception as exc:
        raise StageContractError from exc


def create_server(
    *,
    job_service: Any = None,
    attribution_resolver: Any = None,
) -> Any:
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise RuntimeError("Install MCP support with: pip install 'mcp>=2,<3'") from exc

    from openmontage.job_service import (
        ClientStageError,
        JobConflictError,
        JobNotFoundError,
        JobStateError,
    )

    client_stage_errors = (
        ClientStageError,
        JobConflictError,
        JobNotFoundError,
        JobStateError,
        StageContractError,
    )

    server = MCPServer(
        "OpenMontage",
        description="Prepare and inspect agent-led reference-video productions.",
        instructions=(
            "Use prepare_reference_clone when a user provides a video URL and wants a new, "
            "creatively differentiated video. Then follow the returned agent_instructions "
            "and the OpenMontage pipeline approval gates. Before submit_video_job, call "
            "openmontage_capabilities and follow its job_submission contract; workflow is a "
            "pipeline name, never a stage name such as compose. After submission, drive every "
            "client-owned stage with begin_client_stage, zero or more stage-allowed "
            "invoke_openmontage_tool calls, then submit_client_stage. Every non-catalog "
            "invocation requires the active job_id, "
            "stage, stage_attempt, lease_token, and a stable non-empty idempotency_key. Read "
            "the returned stageContract: only call gatewayTools, read every instructionFiles "
            "entry, and map each read result to instruction_provenance as "
            "{\"path\": result.relative_path, \"content_hash\": result.content_hash}. "
            "Submit artifacts keyed by produces; declaredTools are manifest vocabulary only. "
            "Job query tools (get_video_job, list_video_job_events, and "
            "list_video_artifacts) require the durable Job ID returned by "
            "submit_video_job; a clone/project ID is not a Job ID. Use "
            "list_project_files/read_project_file for prepared project files."
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

        return ToolGateway(jobs(), stage_contract_factory=stage_execution_contract)

    def resolve_attribution(headers: Mapping[str, str] | None) -> Any:
        nonlocal attribution_resolver
        if attribution_resolver is None:
            from openmontage.job_api import default_attribution_resolver

            attribution_resolver = default_attribution_resolver()
        return attribution_resolver(headers)

    def _client_stage_error(error: Exception) -> dict[str, Any]:
        if isinstance(error, ClientStageError):
            code = error.code
            safe_messages = {
                "CHECKPOINT_WRITE_FAILED": "OpenMontage could not persist the stage checkpoint",
                "INSTRUCTION_FILE_UNAVAILABLE": (
                    "OpenMontage could not verify instruction provenance"
                ),
                "PROJECT_INIT_FAILED": "OpenMontage could not initialize the project workspace",
            }
            message = safe_messages.get(code, str(error).removeprefix(f"{code}: "))
            category = "client_stage"
        elif isinstance(error, JobNotFoundError):
            code = "OPENMONTAGE_JOB_NOT_FOUND"
            message = (
                "OpenMontage Job was not found or is not visible to this workspace. "
                "job_id must be the durable ID returned by submit_video_job; for a "
                "prepared clone/project ID, use list_project_files/read_project_file."
            )
            category = "job"
        elif isinstance(error, JobConflictError):
            code = "OPENMONTAGE_JOB_CONFLICT"
            message = str(error)
            category = "job"
        elif isinstance(error, JobStateError):
            code = "OPENMONTAGE_JOB_STATE_INVALID"
            message = str(error)
            category = "job"
        elif isinstance(error, StageContractError):
            code = "OPENMONTAGE_STAGE_CONTRACT_UNAVAILABLE"
            message = "OpenMontage stage execution contract is unavailable"
            category = "job"
        else:  # pragma: no cover - callers restrict the caught exception types
            raise error
        return {
            "success": False,
            "status": "failed",
            "error": {"code": code, "category": category, "message": message},
        }

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

        Every non-catalog call belongs to an active client-stage lease. The
        caller must first create a Job and call ``begin_client_stage`` for the
        current stage. That response's ``jobId``, ``stage``, ``stageAttempt``,
        and ``leaseToken`` map to this tool's ``job_id``, ``stage``,
        ``stage_attempt``, and ``lease_token`` arguments; also pass a stable
        ``idempotency_key``. A stage may make zero or more calls using only the
        exact names in ``stageContract.gatewayTools`` before
        ``submit_client_stage``; do not call this tool as a standalone provider
        API or invent a call when ``gatewayTools`` is empty.
        """
        from openmontage.job_api import require_same_workspace
        from openmontage.tool_gateway import ToolGatewayError

        try:
            attribution = resolve_attribution(ctx.headers)
            if operation == "catalog":
                return tool_gateway().invoke(tool_name=tool_name, operation=operation, inputs=inputs)
            if (
                not job_id.strip()
                or not stage.strip()
                or stage_attempt is None
                or not lease_token.strip()
                or not idempotency_key.strip()
            ):
                return {
                    "success": False,
                    "status": "failed",
                    "error": {
                        "code": "STAGE_LEASE_INVALID",
                        "category": "tool_gateway",
                        "message": (
                            "job_id, stage, stage_attempt, lease_token and idempotency_key "
                            "are required; obtain lease fields from begin_client_stage and "
                            "supply a stable idempotency_key"
                        ),
                    },
                }
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
        except client_stage_errors as exc:
            return _client_stage_error(exc)

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

        try:
            attribution = resolve_attribution(ctx.headers)
            snapshot = jobs().get_job(job_id)
            require_same_workspace(snapshot, attribution)
            return snapshot.to_wire()
        except client_stage_errors as exc:
            return _client_stage_error(exc)

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
        ``stageAttempt``, ``leaseExpiresAt``, the latest Job snapshot, and a
        ``stageContract`` containing manifest ``declaredTools``, exact callable
        ``gatewayTools``, ``produces``, approval
        policy, and ``instructionFiles`` to read with
        ``read_openmontage_file``. Use ``produces`` as the top-level keys in
        ``submit_client_stage.artifacts`` and preserve each instruction read's
        result to an ``instruction_provenance`` entry as
        ``{"path": result.relative_path, "content_hash": result.content_hash}``.
        Replays of the same ``idempotency_key`` return the original result;
        a second live owner is rejected with ``STAGE_ALREADY_OWNED``.
        """
        from openmontage.job_api import require_same_workspace

        try:
            attribution = resolve_attribution(ctx.headers)
            snapshot = jobs().get_job(job_id)
            require_same_workspace(snapshot, attribution)
            result = jobs().begin_client_stage(
                job_id,
                stage,
                idempotency_key=idempotency_key,
                expected_sequence=expected_sequence,
                stage_contract_factory=stage_execution_contract,
            ).to_wire()
            return result
        except client_stage_errors as exc:
            return _client_stage_error(exc)

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

        try:
            attribution = resolve_attribution(ctx.headers)
            snapshot = jobs().get_job(job_id)
            require_same_workspace(snapshot, attribution)
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
        except client_stage_errors as exc:
            return _client_stage_error(exc)

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
        """Submit a client stage's artifacts, checkpoint and status as one operation.

        ``status`` is one of ``completed`` / ``awaiting_human`` / ``failed`` /
        ``in_progress``. The server validates the lease, artifact and
        checkpoint schemas, approval rules and media references; writes the
        standard checkpoint under the CI project directory; records the Job
        event; and advances the Job. ``instruction_provenance`` is a list of
        entries built from ``read_openmontage_file`` results as
        ``{"path": result.relative_path, "content_hash": result.content_hash}``,
        proving which instructions the client followed. Gated stages must be
        submitted as ``awaiting_human`` and completed only after
        ``approve_video_stage`` approves them.

        ``artifacts`` is keyed by canonical artifact name; for example, the
        research stage requires ``{"research_brief": {<brief fields>}}``.
        Do not place the brief's fields directly at the ``artifacts`` level.
        """
        from openmontage.job_api import require_same_workspace

        try:
            attribution = resolve_attribution(ctx.headers)
            snapshot = jobs().get_job(job_id)
            require_same_workspace(snapshot, attribution)
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
        except client_stage_errors as exc:
            return _client_stage_error(exc)

    @server.tool()
    def list_video_job_events(
        job_id: str,
        ctx: Context,
        after_sequence: int = 0,
    ) -> dict[str, Any]:
        """Replay ordered Job events after a sequence cursor."""
        from openmontage.job_api import require_same_workspace

        try:
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
        except client_stage_errors as exc:
            return _client_stage_error(exc)

    @server.tool()
    def list_video_artifacts(job_id: str, ctx: Context) -> dict[str, Any]:
        """List durable video outputs published for a Job.

        ``job_id`` must be the durable Job ID returned by ``submit_video_job``.
        A prepared reference-clone/project ID (for example ``clone-douyin-*``)
        is not a Job ID; use ``list_project_files`` and ``read_project_file``
        to inspect that project's analysis files instead.
        """
        from openmontage.job_api import require_same_workspace

        try:
            attribution = resolve_attribution(ctx.headers)
            snapshot = jobs().get_job(job_id)
            require_same_workspace(snapshot, attribution)
            return {
                "artifacts": [artifact.to_wire() for artifact in snapshot.artifacts],
                "lastSequence": snapshot.last_sequence,
            }
        except client_stage_errors as exc:
            return _client_stage_error(exc)

    @server.tool()
    def list_project_files(project_id: str) -> dict[str, Any]:
        """List the files generated for a prepared reference project.

        Returns every file's project-relative path and size (metadata only; nothing is
        copied), plus the shared file-server export root when the file-server exporter
        is enabled. Use ``read_project_file``/``read_project_image`` for client-side
        inspection; use ``export_project_file`` only for CI-side delivery workflows.
        """
        return ProjectFileExporter().list(project_id)

    @server.tool()
    def export_project_file(project_id: str, relative_path: str, include_media: bool = False) -> dict[str, Any]:
        """Mirror one project file (or a whole directory) into the shared file-server.

        Returns CI shared-mount references. Remote clients must use
        ``read_project_file``/``read_project_image`` instead; the loopback URL and
        ``/exchange`` path are CI-internal. Copying is on demand and, by default,
        skips large media files; pass ``include_media=true`` for CI-side delivery.
        """
        return ProjectFileExporter().export(project_id, relative_path, include_media=include_media)

    @server.tool()
    def read_project_file(project_id: str, relative_path: str, max_bytes: int = 2_000_000) -> dict[str, Any]:
        """Read a bounded UTF-8 analysis file through the authenticated MCP channel."""
        return ProjectFileExporter().read_text(project_id, relative_path, max_bytes=max_bytes)

    @server.tool(structured_output=False)
    def read_project_image(
        project_id: str,
        relative_path: str,
        max_bytes: int = 4_000_000,
    ) -> Any:
        """Read one project image and return it as native MCP image content.

        This is the only supported visual-inspection path for prepared
        projects. The image is read in the OpenMontage/CI process and returned
        inline, so clients never need to resolve a CI-only ``/exchange`` path.
        """
        from mcp.types import CallToolResult, ImageContent, TextContent

        relative, data, media_type = ProjectFileExporter().read_image_bytes(
            project_id,
            relative_path,
            max_bytes=max_bytes,
        )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        f"<path>{relative}</path>\n<type>image</type>\n"
                        f"<content>{media_type} image, {len(data)} bytes</content>"
                    ),
                ),
                ImageContent(
                    data=base64.b64encode(data).decode("ascii"),
                    mimeType=media_type,
                ),
            ]
        )

    @server.tool()
    def read_openmontage_file(path: str, max_bytes: int = 2_000_000) -> dict[str, Any]:
        """Read an OpenMontage instruction file (Markdown/YAML/JSON) from CI.

        Reads the live CI repository on every call — no client-side caching and
        no logical skill-ID mapping. Only ``.md``/``.yaml``/``.yml``/``.json``
        files under the allowed instruction roots (``AGENT_GUIDE.md``,
        ``pipeline_defs/``, ``skills/``, ``.agents/skills/``, ``schemas/``,
        ``styles/``, ``remotion-composer/public/``, ``docs/``) are served; the
        response includes the repo-relative path, size, mtime, a SHA-256 content
        hash, and the repository revision for instruction provenance. It never
        exposes the CI filesystem path.
        Project artifacts and checkpoints belong to ``read_project_file``.
        """
        return read_instruction_file(path, max_bytes=max_bytes)

    @server.tool()
    def sync_project_exports(project_id: str) -> dict[str, Any]:
        """Mirror a prepared project's whole analysis set into the shared file-server.

        Copies the artifacts, keyframes, scenes, transcript, briefs and manifest —
        everything needed by CI-side delivery workflows — while leaving large media
        files uncopied. Returns CI-only ``export_file_path``/``/exchange`` references;
        remote Agents must use ``read_project_file`` or ``read_project_image`` for
        inspection.
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
