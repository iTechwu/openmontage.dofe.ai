"""DoFe.AI gateway music generation (endpointKind: music_async).

No default model is embedded. The selected model must be present in the current
tenant catalog or the tool fails before creating a task.
"""

from __future__ import annotations

from typing import Any

from tools.base_tool import (
    BaseTool,
    DependencyError,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)
from tools.dofe import DofeToolSpec, probe_audio
from tools.dofe.models import resolve_alias
from tools.dofe.runtime import build_metadata, run_dofe_generation
from tools.dofe.status import configured_model_is_visible


class DofeMusic(BaseTool):
    name = "dofe_music"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "music_generation"
    provider = "dofe"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:DOFE_MODEL_API_KEY|DOFE_API_KEY", "env:DOFE_MUSIC_MODEL"]
    install_instructions = (
        "Set DOFE_MODEL_API_KEY in .env for the models.dofe.ai gateway. "
        "Set DOFE_ENABLED=true to prefer the dofe chain. "
        "Read GET /v1/models and set DOFE_MUSIC_MODEL to one returned model ID."
    )
    agent_skills = ["music"]

    dofe_spec = DofeToolSpec(
        capability="music",
        endpoint_kind="music_async",
        asset_kind="audio",
        default_ext=".mp3",
        probe=probe_audio,
    )

    capabilities = ["generate_background_music", "generate_song", "generate_instrumental"]
    supports = {
        "instrumental": True,
        "vocals": True,
        "custom_lyrics": True,
        "style_control": True,
    }
    best_for = ["music generation via the models.dofe.ai gateway when DOFE_ENABLED=true"]
    not_good_for = ["offline generation", "sound effects"]
    fallback_tools = ["music_gen", "suno_music"]
    # Unset: music is not live on the test gateway (dev-guide §6.2).

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string", "description": "Music description / prompt."},
            "tags": {"type": "string", "description": "Genre/style tags. Maps to params.tags."},
            "instrumental": {"type": "boolean", "description": "Instrumental only (no vocals)."},
            "lyrics": {"type": "string", "description": "Custom lyrics. Maps to params.lyrics."},
            "music_mode": {
                "type": "string",
                "enum": ["inspiration", "custom"],
                "default": "inspiration",
                "description": "Suno generation mode. Inspiration uses the prompt directly; custom enables lyrics/title controls.",
            },
            "model_name": {"type": "string", "description": "Exact ID from GET /v1/models. Overrides DOFE_MUSIC_MODEL."},
            "task_id": {"type": "string", "description": "Resume polling an earlier timed-out dofe task."},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=100, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = [
        "prompt", "tags", "instrumental", "lyrics", "music_mode", "model_name"
    ]
    side_effects = ["paid remote generation via models.dofe.ai gateway", "writes audio file to output_path"]
    user_visible_verification = ["Listen to generated music for mood, genre accuracy, and quality"]

    def get_status(self) -> ToolStatus:
        status = super().get_status()
        if status == ToolStatus.UNAVAILABLE:
            return status
        return (
            ToolStatus.AVAILABLE
            if configured_model_is_visible("music", ("generate",))
            else ToolStatus.UNAVAILABLE
        )

    # ------------------------------------------------------------------ cost

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.05

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 120.0

    # ------------------------------------------------------------------ model

    def resolve_model(self, inputs: dict[str, Any]) -> str | None:
        return resolve_alias("music", "generate", explicit=inputs.get("model_name"))

    # ---------------------------------------------------------------- payload

    def _build_payload(self, inputs: dict[str, Any], model: str) -> dict[str, Any]:
        prompt = str(inputs.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")

        # Text block never carries a role (dev-guide §2.3).
        content: list[dict[str, Any]] = [{"part": {"type": "text", "text": prompt}, "order": 0}]

        params: dict[str, Any] = {
            "operation": "generate",
            "musicMode": str(inputs.get("music_mode") or "inspiration"),
        }
        if inputs.get("tags"):
            params["tags"] = inputs["tags"]
        if inputs.get("instrumental") is not None:
            params["instrumental"] = bool(inputs["instrumental"])
        if inputs.get("lyrics"):
            params["lyrics"] = inputs["lyrics"]

        payload: dict[str, Any] = {
            "model": model,
            "endpointKind": "music_async",
            "content": content,
            "params": params,
        }
        metadata = build_metadata(inputs, self.idempotency_key(inputs))
        if metadata:
            payload["metadata"] = metadata
        return payload

    # ---------------------------------------------------------------- execute

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            self.check_dependencies()
        except DependencyError as exc:
            return ToolResult(success=False, error=str(exc))
        return run_dofe_generation(self, inputs)
