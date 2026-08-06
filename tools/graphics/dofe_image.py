"""DoFe.AI gateway image generation (endpointKind: image_async).

Routes the current ``seedream-5.0`` alias through the unified gateway. It also
supports reference-image edits via an ``image_url`` block
with ``role:"reference"`` (local files are inlined as a data URI). See
dev-guide §5.1.
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
    ToolTier,
)
from tools.dofe import DofeToolSpec, probe_image, resolve_image_source
from tools.dofe.models import resolve_alias
from tools.dofe.runtime import build_metadata, run_dofe_generation


class DofeImage(BaseTool):
    name = "dofe_image"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "image_generation"
    provider = "dofe"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:DOFE_MODEL_API_KEY|DOFE_API_KEY"]
    install_instructions = (
        "Set DOFE_MODEL_API_KEY in .env for the models.dofe.ai gateway. "
        "Set DOFE_ENABLED=true to make selectors prefer the dofe chain. "
        "Override the default model with DOFE_IMAGE_MODEL (default seedream-5.0)."
    )
    agent_skills = ["flux-best-practices", "bfl-api"]

    dofe_spec = DofeToolSpec(
        capability="image",
        endpoint_kind="image_async",
        asset_kind="image",
        default_ext=".png",
        probe=probe_image,
    )

    capabilities = ["generate_image", "text_to_image", "image_edit"]
    supports = {
        "text_to_image": True,
        "image_edit": True,
        "seed": True,
        "custom_size": True,
        "aspect_ratio": True,
    }
    best_for = [
        "image generation via the models.dofe.ai gateway (seedream-5.0 and the gateway catalog)",
        "reference-conditioned image edits when DOFE_ENABLED=true",
    ]
    not_good_for = ["offline generation", "non-dofe model families"]
    fallback_tools = ["flux_image", "google_imagen", "openai_image", "recraft_image"]
    # Image is the only dofe capability verified live on the test gateway, so a
    # modest calibrated quality score is honest. Video/TTS/music/avatar stay unset.
    quality_score = 0.8

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string", "description": "Image description / prompt."},
            "width": {"type": "integer", "default": 1024},
            "height": {"type": "integer", "default": 1024},
            "size": {
                "type": "string",
                "description": "Explicit resolution like '1024x1024'. Overrides width/height.",
            },
            "resolution": {
                "type": "string",
                "description": "Gateway resolution string (e.g. '1024x1024'). Overrides width/height.",
            },
            "aspect_ratio": {"type": "string", "description": "Aspect ratio hint (e.g. '16:9')."},
            "n": {"type": "integer", "default": 1, "description": "Number of images (outputCount)."},
            "quality": {"type": "string", "description": "Optional quality hint passed to the gateway."},
            "style": {"type": "string", "description": "Optional style hint passed to the gateway."},
            "seed": {"type": "integer"},
            "image_url": {"type": "string", "description": "https reference image for edit/inpaint."},
            "image_path": {"type": "string", "description": "Local reference image (inlined as a data URI)."},
            "model_name": {
                "type": "string",
                "description": "Explicit dofe alias (e.g. seedream-5.0). Overrides DOFE_IMAGE_MODEL.",
            },
            "task_id": {
                "type": "string",
                "description": "Resume polling an earlier timed-out dofe task by its id.",
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=200, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = [
        "prompt",
        "width",
        "height",
        "size",
        "resolution",
        "n",
        "seed",
        "model_name",
    ]
    side_effects = ["paid remote generation via models.dofe.ai gateway", "writes image file to output_path"]
    user_visible_verification = ["Inspect generated image for relevance, quality, and prompt adherence"]

    # ------------------------------------------------------------------ cost

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # Keep this conservative estimate until the gateway exposes live rate cards here.
        n = max(1, int(inputs.get("n") or 1))
        return round(0.03 * n, 4)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 30.0

    # ------------------------------------------------------------------ model

    def resolve_model(self, inputs: dict[str, Any]) -> str | None:
        return resolve_alias("image", "generate", explicit=inputs.get("model_name"))

    # ---------------------------------------------------------------- payload

    @staticmethod
    def _resolution(inputs: dict[str, Any]) -> str:
        explicit = inputs.get("resolution") or inputs.get("size")
        if explicit and "x" in str(explicit).lower():
            return str(explicit)
        width = inputs.get("width", 1024)
        height = inputs.get("height", 1024)
        return f"{int(width)}x{int(height)}"

    def _build_payload(self, inputs: dict[str, Any], model: str) -> dict[str, Any]:
        prompt = str(inputs.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")

        # CRITICAL (dev-guide §2.3): the text block must NOT carry a role.
        content: list[dict[str, Any]] = [{"part": {"type": "text", "text": prompt}, "order": 0}]

        if inputs.get("image_url") or inputs.get("image_path"):
            url = resolve_image_source(url=inputs.get("image_url"), path=inputs.get("image_path"))
            content.append(
                {
                    "part": {"type": "image_url", "image_url": {"url": url}},
                    "order": len(content),
                    "role": "reference",
                }
            )

        params: dict[str, Any] = {
            "resolution": self._resolution(inputs),
            "outputCount": max(1, int(inputs.get("n") or 1)),
        }
        if inputs.get("quality"):
            params["quality"] = inputs["quality"]
        if inputs.get("style"):
            params["style"] = inputs["style"]
        if inputs.get("seed") is not None:
            params["seed"] = inputs["seed"]
        if inputs.get("aspect_ratio"):
            params["ratio"] = inputs["aspect_ratio"]

        payload: dict[str, Any] = {
            "model": model,
            "endpointKind": "image_async",
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
