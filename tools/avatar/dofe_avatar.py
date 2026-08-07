"""DoFe.AI gateway digital-human avatar (endpointKind: digital_human).

No default model is embedded. The selected model must be present in the current
tenant catalog. Takes a portrait image plus a driving audio track.

Note on the avatar image ``role``: dev-guide §5.5 suggests ``role:"avatar"``,
but §2.3's hard constraint (from a real 400) limits asset roles to
``{reference, first_frame, last_frame}``. We use ``role:"reference"`` to stay
within the allowed set; switch to ``"avatar"`` in P2 once the endpoint ships.
Driving audio is too large to inline as a data URI, so a public https URL is
required (dev-guide §5.6).
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
from tools.dofe import DofeToolSpec, probe_video, resolve_image_source
from tools.dofe.media import is_https_url
from tools.dofe.models import resolve_alias
from tools.dofe.runtime import build_metadata, run_dofe_generation
from tools.dofe.status import configured_model_is_visible


class DofeAvatar(BaseTool):
    name = "dofe_avatar"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "avatar"
    provider = "dofe"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:DOFE_MODEL_API_KEY|DOFE_API_KEY", "env:DOFE_AVATAR_MODEL"]
    install_instructions = (
        "Set DOFE_MODEL_API_KEY in .env for the models.dofe.ai gateway. "
        "Set DOFE_ENABLED=true to prefer the dofe chain. "
        "Read GET /v1/models and set DOFE_AVATAR_MODEL to one returned model ID."
    )
    agent_skills = ["avatar-video"]

    dofe_spec = DofeToolSpec(
        capability="avatar",
        endpoint_kind="digital_human",
        asset_kind="video",
        default_ext=".mp4",
        probe=probe_video,
    )

    capabilities = ["avatar_video", "audio_driven_avatar"]
    supports = {
        "photo_to_video": True,
        "audio_driven_animation": True,
        "offline": False,
        "cloud_render": True,
    }
    best_for = ["avatar/presenter video via the models.dofe.ai gateway when DOFE_ENABLED=true"]
    not_good_for = ["offline avatar generation", "free local drafts"]
    fallback_tools = ["talking_head", "lip_sync", "kling_avatar"]
    # Unset: avatar is not live on the test gateway (dev-guide §6.2).

    input_schema = {
        "type": "object",
        "required": ["audio_url"],
        "anyOf": [{"required": ["image_url"]}, {"required": ["image_path"]}],
        "properties": {
            "image_url": {"type": "string", "description": "Portrait https URL (or use image_path)."},
            "image_path": {"type": "string", "description": "Local portrait image (inlined as a data URI)."},
            "audio_url": {"type": "string", "description": "Driving audio https URL (required)."},
            "audio_path": {"type": "string", "description": "Local audio is not supported inline — provide audio_url."},
            "prompt": {"type": "string", "description": "Optional description passed to the gateway."},
            "model_name": {"type": "string", "description": "Exact ID from GET /v1/models. Overrides DOFE_AVATAR_MODEL."},
            "task_id": {"type": "string", "description": "Resume polling an earlier timed-out dofe task."},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["image_url", "image_path", "audio_url", "prompt", "model_name"]
    side_effects = ["paid remote generation via models.dofe.ai gateway", "writes avatar video to output_path"]
    user_visible_verification = ["Watch generated avatar video for identity preservation and mouth motion"]

    def get_status(self) -> ToolStatus:
        status = super().get_status()
        if status == ToolStatus.UNAVAILABLE:
            return status
        return (
            ToolStatus.AVAILABLE
            if configured_model_is_visible("avatar", ("generate",))
            else ToolStatus.UNAVAILABLE
        )

    # ------------------------------------------------------------------ cost

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.35

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 240.0

    # ------------------------------------------------------------------ model

    def resolve_model(self, inputs: dict[str, Any]) -> str | None:
        return resolve_alias("avatar", "generate", explicit=inputs.get("model_name"))

    # ---------------------------------------------------------------- payload

    def _build_payload(self, inputs: dict[str, Any], model: str) -> dict[str, Any]:
        if not (inputs.get("image_url") or inputs.get("image_path")):
            raise ValueError("dofe avatar requires image_url or image_path")

        audio_url = inputs.get("audio_url")
        if inputs.get("audio_path") and not audio_url:
            raise ValueError(
                "dofe avatar audio must be a public https URL (audio_url); local audio files "
                "are too large to inline."
            )
        if not audio_url or not is_https_url(audio_url):
            raise ValueError("dofe avatar requires audio_url (https)")

        # Avatar portrait image. role:"reference" stays within the allowed set
        # (see module docstring). Text block, when present, never carries a role.
        content: list[dict[str, Any]] = [
            {
                "part": {
                    "type": "image_url",
                    "image_url": {"url": resolve_image_source(url=inputs.get("image_url"), path=inputs.get("image_path"))},
                },
                "order": 0,
                "role": "reference",
            },
            {
                "part": {"type": "audio_url", "audio_url": {"url": audio_url}},
                "order": 1,
            },
        ]
        if inputs.get("prompt"):
            content.append({"part": {"type": "text", "text": str(inputs["prompt"])}, "order": len(content)})

        payload: dict[str, Any] = {
            "model": model,
            "endpointKind": "digital_human",
            "content": content,
            "params": {},
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
