"""Application service for preparing a reference-driven video production."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lib.checkpoint import get_next_stage, init_project
from lib.paths import PROJECTS_DIR
from lib.pipeline_loader import load_pipeline_readonly, pipeline_supports_reference_input
from lib.video_sources import detect_video_platform, normalize_video_url
from openmontage.exchange import ProjectFileExportError, ProjectFileExporter
from tools.analysis.video_analyzer import VideoAnalyzer
from tools.dofe import config as dofe_config
from tools.dofe.client import DofeClient
from tools.dofe.errors import DofeError
from tools.dofe.models import catalog_model_ids, resolve_alias
from tools.tool_registry import registry

from openmontage.capabilities import job_submission_capability


_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")


class ReferenceCloneError(RuntimeError):
    """Raised when a reference-clone preparation cannot complete safely."""


def _project_id_for(url: str) -> str:
    parsed = urlparse(url)
    platform = detect_video_platform(url).replace("_", "-")
    tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if tail.isdigit():
        identifier = tail[-20:]
    else:
        identifier = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"clone-{platform}-{identifier}"[:64].rstrip("-")


def _validate_project_id(project_id: str) -> str:
    normalized = project_id.strip().lower()
    if not _PROJECT_ID_RE.fullmatch(normalized):
        raise ReferenceCloneError(
            "project_id must be 1-64 lowercase letters, numbers, or hyphens"
        )
    return normalized


def _select_pipeline(requested: str, brief: dict[str, Any]) -> str:
    pipeline = requested.strip()
    if pipeline == "auto":
        pipeline = str(
            (brief.get("replication_guidance") or {}).get("suggested_pipeline")
            or "animated-explainer"
        )
    try:
        manifest = load_pipeline_readonly(pipeline)
    except Exception as exc:
        raise ReferenceCloneError(f"Unknown or invalid pipeline: {pipeline}") from exc
    if not pipeline_supports_reference_input(manifest):
        raise ReferenceCloneError(f"Pipeline does not support reference video input: {pipeline}")
    return pipeline


def _brief_path(project_dir: Path) -> Path:
    return project_dir / "artifacts" / "video_analysis_brief.json"


def _exports_block(project_id: str) -> dict[str, Any]:
    """Describe (and materialize) the on-demand file-server export surface.

    When the file-server exporter is configured (docker/CI deployment) the workspace
    agent cannot read the container project directory, so project files are mirrored
    into the shared exchange. We mirror the analysis set with a small margin (whole
    artifacts/keyframes/scenes/transcript, leaving media uncopied) so the agent can
    fetch it immediately. Local (non-docker) runs leave this disabled and return
    container paths directly.
    """
    exporter = ProjectFileExporter()
    if not exporter.enabled:
        return {"enabled": False}
    try:
        mirrored = exporter.export_analysis(project_id).get("mirrored_files", [])
    except ProjectFileExportError:
        mirrored = []
    root = exporter.root(project_id)
    return {
        "enabled": True,
        "project_root_url": root["url"],
        "project_root_host_path": root["host_path"],
        "mirrored_files": mirrored,
        "instructions": (
            "List the prepared project files with list_project_files(project_id); mirror the "
            "analysis set (already available) or a single file with sync_project_exports / "
            "export_project_file, then read it from the returned host_path or fetch the "
            "returned url."
        ),
    }


def _airouter_model_preflight() -> dict[str, Any]:
    selected = {
        "image": resolve_alias("image", "generate"),
        "video": resolve_alias("video", "text_to_video"),
        "stt": resolve_alias("stt", "transcribe"),
    }
    unconfigured = [
        capability for capability, alias in selected.items() if not alias
    ]
    try:
        catalog = catalog_model_ids(DofeClient().list_models())
    except DofeError as exc:
        return {
            "status": "unreachable",
            "catalog_endpoint": f"{dofe_config.dofe_base_url()}/v1/models",
            "selected_models": selected,
            "catalog_models": [],
            "visible_selected_models": [],
            "missing_selected_models": [alias for alias in selected.values() if alias],
            "unconfigured_capabilities": unconfigured,
            "error": str(exc),
        }
    visible = set(catalog)
    missing = [alias for alias in selected.values() if alias and alias not in visible]
    return {
        "status": "ready" if not missing and not unconfigured else "blocked",
        "catalog_endpoint": f"{dofe_config.dofe_base_url()}/v1/models",
        "selected_models": selected,
        "catalog_models": list(catalog),
        "visible_selected_models": [alias for alias in selected.values() if alias in visible],
        "missing_selected_models": missing,
        "unconfigured_capabilities": unconfigured,
    }


class ReferenceCloneService:
    """Prepare durable inputs for an agent-led reference-video pipeline."""

    def __init__(self, *, projects_root: Path | None = None) -> None:
        self.projects_root = (projects_root or PROJECTS_DIR).expanduser().resolve()

    def prepare(
        self,
        source: str,
        *,
        project_id: str = "",
        pipeline_type: str = "auto",
        title: str = "",
        creative_brief: str = "",
        analysis_depth: str = "standard",
        max_keyframes: int = 20,
        max_resolution: str = "720p",
        cookie_file: str = "",
    ) -> dict[str, Any]:
        normalized_url = normalize_video_url(source)
        resolved_project_id = _validate_project_id(project_id or _project_id_for(normalized_url))
        project_dir = (self.projects_root / resolved_project_id).resolve()
        if project_dir.parent != self.projects_root:
            raise ReferenceCloneError("Resolved project path escaped the projects directory")

        analysis_dir = project_dir / "reference"
        result = VideoAnalyzer().execute(
            {
                "source": normalized_url,
                "analysis_depth": analysis_depth,
                "max_keyframes": max_keyframes,
                "max_resolution": max_resolution,
                "cookie_file": cookie_file,
                "output_dir": str(analysis_dir),
            }
        )
        if not result.success:
            raise ReferenceCloneError(result.error or "Reference analysis failed")
        brief = result.data
        meta = brief.get("_analysis_meta") or {}
        completed = set(meta.get("steps_completed") or [])
        if "download" not in completed:
            failures = "; ".join(meta.get("steps_failed") or [])
            raise ReferenceCloneError(
                "Reference video could not be downloaded"
                + (f": {failures}" if failures else "")
            )

        selected_pipeline = _select_pipeline(pipeline_type, brief)
        project_title = title.strip() or str((brief.get("source") or {}).get("title") or resolved_project_id)
        init_project(
            resolved_project_id,
            title=project_title,
            pipeline_type=selected_pipeline,
            pipeline_dir=self.projects_root,
        )

        canonical_brief = _brief_path(project_dir)
        canonical_brief.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(analysis_dir / "video_analysis_brief.json", canonical_brief)

        preflight = capability_summary()
        preflight["airouter"] = _airouter_model_preflight()
        models_base_url = dofe_config.dofe_base_url()
        request = {
            "version": "1.0",
            "status": "prepared",
            "project_id": resolved_project_id,
            "title": project_title,
            "pipeline_type": selected_pipeline,
            "source": {
                "submitted": source,
                "normalized_url": normalized_url,
                "platform": detect_video_platform(normalized_url),
            },
            "creative_brief": creative_brief.strip(),
            "analysis": {
                "brief_path": str(canonical_brief),
                "reference_dir": str(analysis_dir),
                "steps_completed": meta.get("steps_completed") or [],
                "steps_failed": meta.get("steps_failed") or [],
                "keyframe_count": meta.get("keyframe_count", 0),
                "scene_count": meta.get("scene_count", 0),
            },
            "preflight": preflight,
            "model_routing": {
                "policy": "dofe_airouter_only",
                "provider": "dofe",
                "base_url": models_base_url,
                "catalog_endpoint": f"{models_base_url}/v1/models",
                "direct_provider_fallback": False,
            },
            "next_stage": get_next_stage(
                self.projects_root, resolved_project_id, selected_pipeline
            ),
            "exports": _exports_block(resolved_project_id),
            "agent_instructions": [
                "Read skills/meta/video-reference-analyst.md and inspect the extracted keyframes.",
                "Present a five-aspect reference analysis and 2-3 differentiated concepts; do not make a carbon copy.",
                "Run the selected pipeline stage by stage and honor every manifest approval gate.",
                "Before submitting a Job, follow preflight.job_submission; workflow is the pipeline name, not a stage such as compose.",
                "Use only source material the user is authorized to reference or transform.",
                f"Fetch model IDs from {models_base_url}/v1/models before selection; use only an exact returned ID and block instead of guessing or using a direct-provider fallback.",
            ],
        }
        request_path = project_dir / "artifacts" / "reference_clone_request.json"
        with request_path.open("w", encoding="utf-8") as handle:
            json.dump(request, handle, ensure_ascii=False, indent=2)
        request["request_path"] = str(request_path)
        request["project_dir"] = str(project_dir)
        return request

    def status(self, project_id: str) -> dict[str, Any]:
        resolved = _validate_project_id(project_id)
        project_dir = (self.projects_root / resolved).resolve()
        request_path = project_dir / "artifacts" / "reference_clone_request.json"
        marker_path = project_dir / "project.json"
        if not request_path.is_file() or not marker_path.is_file():
            raise ReferenceCloneError(f"Prepared clone project not found: {resolved}")
        with request_path.open(encoding="utf-8") as handle:
            request = json.load(handle)
        with marker_path.open(encoding="utf-8") as handle:
            marker = json.load(handle)
        pipeline = marker["pipeline_type"]
        return {
            "project_id": resolved,
            "title": marker.get("title", resolved),
            "pipeline_type": pipeline,
            "next_stage": get_next_stage(self.projects_root, resolved, pipeline),
            "analysis": request.get("analysis", {}),
            "exports": _exports_block(resolved),
            "project_dir": str(project_dir),
        }


def capability_summary() -> dict[str, Any]:
    registry.discover()
    summary = registry.provider_menu_summary()
    summary["job_submission"] = job_submission_capability()
    return summary
