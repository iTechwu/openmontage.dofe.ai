"""DoFe.AI gateway video generation (endpointKind: video_async).

Protocol-ready. On the test gateway the video adapters are not yet enabled
(seedance-2.0-fast, hailuo, kling), so failures must
surface a clear error (model + reason + suggestion) and never hang. All three
operations default to ``seedance-2.0-fast``. See dev-guide §5.2.
"""

from __future__ import annotations

import os
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
from tools.dofe import (
    DofePricingClient,
    DofePricingError,
    DofeToolSpec,
    probe_video,
    resolve_image_source,
)
from tools.dofe.models import resolve_alias
from tools.dofe.runtime import build_metadata, run_dofe_generation

MAX_REFERENCE_IMAGES = 9  # dev-guide §5.2: dofe enforces this server-side.


class DofeVideo(BaseTool):
    name = "dofe_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "dofe"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:DOFE_MODEL_API_KEY|DOFE_API_KEY"]
    install_instructions = (
        "Set DOFE_MODEL_API_KEY in .env for the models.dofe.ai gateway. "
        "Set DOFE_ENABLED=true to make selectors prefer the dofe chain. "
        "Override the default model with DOFE_VIDEO_MODEL (default seedance-2.0-fast)."
    )
    agent_skills = ["ai-video-gen"]

    _SEEDANCE_SKILLS = [
        "seedance-provider",
        "seedance-directing",
        "seedance-continuity",
        "seedance-prompting",
        "seedance-quality",
        "ai-video-gen",
    ]

    def agent_skills_for(self, inputs: dict[str, Any] | None = None) -> list[str]:
        selected = str((inputs or {}).get("model_name") or os.environ.get("DOFE_VIDEO_MODEL", "seedance-2.0-fast"))
        return list(self._SEEDANCE_SKILLS if "seedance" in selected.lower() else self.agent_skills)

    dofe_spec = DofeToolSpec(
        capability="video",
        endpoint_kind="video_async",
        asset_kind="video",
        default_ext=".mp4",
        probe=probe_video,
    )

    capabilities = ["text_to_video", "image_to_video", "reference_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "reference_to_video": True,
        "reference_image": True,
        "multiple_reference_images": True,
        "negative_prompt": True,
        "aspect_ratio": True,
        "seed": True,
    }
    reference_binding_contract = {
        "supported_modes": ["input_parameter"],
        "input_fields": [
            "image_url",
            "image_path",
            "reference_image_url",
            "reference_image_path",
            "reference_image_urls",
            "reference_image_paths",
        ],
        "prompt_token_syntax": None,
    }
    best_for = [
        "video generation via the models.dofe.ai gateway (seedance-2.0-fast and the gateway catalog)",
        "text/image/reference-to-video when DOFE_ENABLED=true",
    ]
    not_good_for = ["offline generation", "non-dofe model families"]
    fallback_tools = ["seedance_video", "kling_video", "veo_video", "minimax_video"]
    # Unset: video is not yet live on the test gateway. Leave the score empty so
    # the stability heuristic applies and dofe_video never silently steals the
    # default path from working providers (dev-guide §6.2).

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video", "reference_to_video"],
                "default": "text_to_video",
            },
            "duration": {
                "type": "string",
                "default": "5",
                "description": "Duration in seconds (minimum 5). Maps to params.durationSeconds.",
            },
            "aspect_ratio": {"type": "string", "default": "16:9", "description": "Maps to params.ratio."},
            "resolution": {"type": "string", "description": "e.g. '720p'. Maps to params.resolution."},
            "generate_audio": {
                "type": "boolean",
                "default": False,
                "description": "Request native synchronized audio (params.generateAudio).",
            },
            "negative_prompt": {"type": "string"},
            "seed": {"type": "integer"},
            "image_url": {"type": "string", "description": "First-frame https URL for image_to_video."},
            "image_path": {"type": "string", "description": "Local first-frame image (inlined as data URI)."},
            "reference_image_url": {"type": "string", "description": "Alias for image_url (first frame)."},
            "reference_image_path": {"type": "string", "description": "Alias for image_path."},
            "reference_image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 9 reference image URLs for reference_to_video.",
            },
            "reference_image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Local reference image paths (inlined as data URIs).",
            },
            "model_name": {
                "type": "string",
                "description": "Explicit dofe alias (e.g. seedance-2.0-fast). Overrides DOFE_VIDEO_MODEL.",
            },
            "estimated_output_tokens": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional provider output-token estimate used for a native-currency cost quote.",
            },
            "task_id": {"type": "string", "description": "Resume polling an earlier timed-out dofe task."},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = [
        "prompt", "operation", "duration", "aspect_ratio", "resolution",
        "generate_audio", "negative_prompt", "seed", "model_name",
    ]
    side_effects = ["paid remote generation via models.dofe.ai gateway", "writes video file to output_path"]
    user_visible_verification = ["Watch generated clip for motion coherence and prompt adherence"]

    # ------------------------------------------------------------------ cost

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # BaseTool's legacy estimate contract is USD-only. Seedance is billed in
        # CNY, so the authoritative native quote is exposed by dry_run instead.
        return 0.0

    def pricing_quote(self, inputs: dict[str, Any]) -> dict[str, Any]:
        model = self.resolve_model(inputs)
        if not model:
            raise DofePricingError("No Airouter video model alias could be resolved")
        operation = str(inputs.get("operation") or "text_to_video")
        requested_tokens = inputs.get("estimated_output_tokens")
        quote_tokens = int(requested_tokens) if requested_tokens is not None else 1_000_000
        context: dict[str, Any] = {
            "hasVideoInput": operation in {"image_to_video", "reference_to_video"},
            "hasAudio": bool(inputs.get("generate_audio", False)),
        }
        resolution = str(inputs.get("resolution") or "").strip().lower()
        if resolution in {"480p", "540p", "720p", "1080p", "4k"}:
            context["resolution"] = resolution
        quote = DofePricingClient().quote(
            {
                "modelAlias": model,
                "outputTokens": quote_tokens,
                "pricingContext": context,
            }
        )
        selection = quote.get("selection") if isinstance(quote.get("selection"), dict) else {}
        return {
            "amount": quote.get("estimatedTotal") if requested_tokens is not None else None,
            "currency": quote.get("currency"),
            "source": quote.get("source"),
            "billing_unit": quote.get("billingUnit"),
            "unit_price": selection.get("unitPrice"),
            "unit": "MToken",
            "formula": selection.get("formula"),
            "output_tokens": int(requested_tokens) if requested_tokens is not None else None,
            "quote_basis": "estimated_usage" if requested_tokens is not None else "unit_rate",
            "requires_actual_usage": requested_tokens is None,
            "pricing_context": context,
            "warnings": quote.get("warnings") or [],
        }

    def dry_run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        result = super().dry_run(inputs)
        result["estimated_cost_usd"] = None
        try:
            result["pricing"] = self.pricing_quote(inputs)
        except DofePricingError as exc:
            result["pricing"] = {
                "available": False,
                "source": "airouter_internal_api",
                "error": str(exc),
            }
        return result

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 180.0

    # ------------------------------------------------------------------ model

    def resolve_model(self, inputs: dict[str, Any]) -> str | None:
        operation = inputs.get("operation", "text_to_video")
        return resolve_alias("video", operation, explicit=inputs.get("model_name"))

    # ---------------------------------------------------------------- payload

    def _build_payload(self, inputs: dict[str, Any], model: str) -> dict[str, Any]:
        prompt = str(inputs.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        operation = inputs.get("operation", "text_to_video")

        # CRITICAL (dev-guide §2.3): text block carries NO role. Putting a role
        # on it returns 400 param_unsupported.
        content: list[dict[str, Any]] = [{"part": {"type": "text", "text": prompt}, "order": 0}]

        if operation == "image_to_video":
            url = inputs.get("image_url") or inputs.get("reference_image_url")
            path = inputs.get("image_path") or inputs.get("reference_image_path")
            if not url and not path:
                raise ValueError("image_to_video requires image_url/image_path (first frame)")
            content.append(
                {
                    "part": {"type": "image_url", "image_url": {"url": resolve_image_source(url=url, path=path)}},
                    "order": len(content),
                    "role": "first_frame",
                }
            )
        elif operation == "reference_to_video":
            refs = list(inputs.get("reference_image_urls") or [])
            for local_path in inputs.get("reference_image_paths") or []:
                refs.append(resolve_image_source(path=local_path))
            if len(refs) > MAX_REFERENCE_IMAGES:
                raise ValueError(
                    f"dofe reference_to_video accepts at most {MAX_REFERENCE_IMAGES} reference images; "
                    f"got {len(refs)}"
                )
            if not refs:
                raise ValueError(
                    "reference_to_video requires reference_image_urls or reference_image_paths"
                )
            for ref_url in refs:
                content.append(
                    {
                        "part": {"type": "image_url", "image_url": {"url": ref_url}},
                        "order": len(content),
                        "role": "reference",
                    }
                )

        params: dict[str, Any] = {
            "videoOperation": operation,
            "durationSeconds": _parse_duration(inputs.get("duration", "5")),
            "ratio": inputs.get("aspect_ratio", "16:9"),
        }
        if inputs.get("resolution"):
            params["resolution"] = inputs["resolution"]
        if "generate_audio" in inputs:
            params["generateAudio"] = bool(inputs["generate_audio"])
        if inputs.get("negative_prompt"):
            params["negativePrompt"] = inputs["negative_prompt"]
        if inputs.get("seed") is not None:
            params["seed"] = inputs["seed"]

        payload: dict[str, Any] = {
            "model": model,
            "endpointKind": "video_async",
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


def _parse_duration(value: Any, default: int = 5) -> int:
    """Map a duration hint ('5', 'auto', 5) to integer seconds."""

    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("", "auto"):
        return default
    try:
        return max(5, int(float(text)))
    except (TypeError, ValueError):
        return default
