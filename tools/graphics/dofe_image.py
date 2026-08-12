"""DoFe.AI gateway image generation (endpointKind: image_async).

Routes a tenant-visible catalog model through the unified gateway. It also
supports reference-conditioned image edits and inpainting via an ``image_url``
block with ``role:"reference"`` / ``role:"mask"`` (local files are inlined as a
data URI). See dev-guide §5.1.
"""

from __future__ import annotations

import re
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
from tools.dofe import DofeClient, DofeError, DofeToolSpec, probe_image, resolve_image_source
from tools.dofe.models import resolve_alias, validate_catalog_alias
from tools.dofe.runtime import build_metadata, run_dofe_generation
from tools.dofe.status import (
    configured_model_is_visible,
    resolve_catalog,
    resolve_playground_capability,
)


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
        "Read GET /v1/models and set DOFE_IMAGE_MODEL to one returned model ID."
    )
    agent_skills = ["flux-best-practices", "bfl-api"]

    dofe_spec = DofeToolSpec(
        capability="image",
        endpoint_kind="image_async",
        asset_kind="image",
        default_ext=".png",
        probe=probe_image,
    )

    capabilities = ["generate_image", "text_to_image", "image_edit", "multi_reference_edit"]
    supports = {
        "text_to_image": True,
        "image_edit": True,
        "multi_reference_edit": True,
        "negative_prompt": "model_scoped",
        "seed": True,
        "custom_size": True,
        "aspect_ratio": True,
    }
    reference_binding_contract = {
        "supported_modes": ["input_parameter"],
        "input_fields": [
            "image_url",
            "image_path",
            "image_urls",
            "image_paths",
            "mask_url",
            "mask_path",
        ],
        "prompt_token_syntax": None,
    }
    best_for = [
        "image generation using an exact ID from the tenant-visible gateway catalog",
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
            "negative_prompt": {
                "type": "string",
                "description": "What to avoid; omitted for Seedream aliases, which do not accept it.",
            },
            "width": {"type": "integer"},
            "height": {"type": "integer"},
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
            "image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ordered https reference images for multi-reference edits.",
            },
            "image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ordered local reference images for multi-reference edits.",
            },
            "mask_url": {
                "type": "string",
                "description": "https mask image for inpainting (not allowed for data_uri_only edits lane).",
            },
            "mask_path": {
                "type": "string",
                "description": "Local mask image (inlined as a data URI) for inpainting.",
            },
            "output_format": {
                "type": "string",
                "enum": ["png", "jpeg", "webp"],
                "description": "Output image format (e.g. 'png', 'jpeg', 'webp').",
            },
            "output_compression": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "Output compression level 0-100 (GPT Image, jpeg/webp only).",
            },
            "response_format": {
                "type": "string",
                "enum": ["url", "b64_json"],
                "description": "Output encoding from the gateway (url or b64_json).",
            },
            "generation_mode": {
                "type": "string",
                "enum": ["generate", "edit"],
                "description": "Generation intent hint. A mask selects image_edit; references without a mask use image_to_image.",
            },
            "background": {
                "type": "string",
                "enum": ["transparent", "opaque", "auto"],
                "description": "Background control for GPT Image edits/generations.",
            },
            "moderation": {
                "type": "string",
                "description": "Safety moderation level for GPT Image.",
            },
            "thinking": {
                "type": "string",
                "enum": ["off", "low", "medium", "high"],
                "description": "Thinking strength for GPT Image 2 (only when capability declares it).",
            },
            "model_name": {
                "type": "string",
                "description": "Exact ID from GET /v1/models. Overrides DOFE_IMAGE_MODEL.",
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
        "negative_prompt",
        "image_url",
        "image_path",
        "image_urls",
        "image_paths",
        "mask_url",
        "mask_path",
        "width",
        "height",
        "size",
        "resolution",
        "aspect_ratio",
        "n",
        "seed",
        "quality",
        "style",
        "generation_mode",
        "output_format",
        "output_compression",
        "response_format",
        "background",
        "moderation",
        "thinking",
        "model_name",
    ]
    side_effects = ["paid remote generation via models.dofe.ai gateway", "writes image file to output_path"]
    user_visible_verification = ["Inspect generated image for relevance, quality, and prompt adherence"]

    def get_status(self) -> ToolStatus:
        status = super().get_status()
        if status == ToolStatus.UNAVAILABLE:
            return status
        return (
            ToolStatus.AVAILABLE
            if configured_model_is_visible("image", ("generate",))
            else ToolStatus.UNAVAILABLE
        )

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
    def _supports_negative_prompt(model: str) -> bool:
        model_family = model.rsplit("/", 1)[-1].lower()
        return not model_family.startswith("seedream-")

    @staticmethod
    def _reference_count(inputs: dict[str, Any]) -> int:
        return int(bool(inputs.get("image_url") or inputs.get("image_path"))) + len(
            inputs.get("image_urls") or []
        ) + len(inputs.get("image_paths") or [])

    @classmethod
    def _resolve_operation(cls, inputs: dict[str, Any]) -> str:
        """Resolve the DoFe operation from actual asset semantics.

        ``generation_mode=edit`` is an authoring intent, not proof that the
        request uses the multipart mask-edit contract. Reference-only edits,
        including Seedream multi-reference composition, belong to the
        ``image_to_image`` lane.
        """

        if inputs.get("mask_url") or inputs.get("mask_path"):
            return "image_edit"
        if cls._reference_count(inputs) > 0 or inputs.get("generation_mode") == "edit":
            return "image_to_image"
        return "text_to_image"

    def probe_provider_contract(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Validate the selected image operation against DoFe's live capability."""

        requested_model = self.resolve_model(inputs)
        if not requested_model:
            return {
                "status": "blocked",
                "verification_scope": ["tenant_model_catalog"],
                "errors": ["No DoFe image model ID is configured"],
                "warnings": [],
            }

        reference_count = self._reference_count(inputs)
        has_mask = bool(inputs.get("mask_url") or inputs.get("mask_path"))
        operation = self._resolve_operation(inputs)
        try:
            client = DofeClient()
            catalog, ok = resolve_catalog()
            if not ok or catalog is None:
                raise DofeError("DoFe model catalog unavailable")
            model = validate_catalog_alias(requested_model, catalog)
            capability = resolve_playground_capability(client, model)
        except DofeError as exc:
            return {
                "status": "blocked",
                "verification_scope": ["tenant_model_catalog", "playground_capability"],
                "model": requested_model,
                "operation": operation,
                "errors": [f"DoFe live capability validation failed: {exc}"],
                "warnings": [],
            }

        errors: list[str] = []
        readiness = capability.get("readiness")
        if isinstance(readiness, list):
            for item in readiness:
                if isinstance(item, dict) and item.get("severity") == "blocked":
                    errors.append(f"DoFe readiness {item.get('code') or 'unknown'}")
        if capability.get("modelType") != "image":
            errors.append("DoFe capability modelType is not image")
        if capability.get("state") not in {"ready", "warning"}:
            errors.append(f"DoFe capability state {capability.get('state')!r} is not executable")
        if capability.get("executor") != "generation_task":
            errors.append("DoFe capability executor is not generation_task")
        if capability.get("endpointKind") != "image_async":
            errors.append("DoFe capability endpointKind is not image_async")
        operation_contract = next(
            (
                item
                for item in capability.get("operations") or []
                if isinstance(item, dict) and item.get("id") == operation
            ),
            None,
        )
        if operation_contract is None:
            errors.append(
                f"DoFe capability does not expose operation {operation!r} for model {model!r}"
            )
            constraints: dict[str, Any] = {}
        else:
            raw_constraints = operation_contract.get("constraints")
            constraints = raw_constraints if isinstance(raw_constraints, dict) else {}
        raw_input = capability.get("input")
        capability_input = raw_input if isinstance(raw_input, dict) else {}
        max_input_assets = constraints.get(
            "maxInputAssets", capability_input.get("maxInputAssets")
        )
        if max_input_assets is not None and reference_count > int(max_input_assets):
            errors.append(
                f"DoFe model {model!r} accepts at most {max_input_assets} input assets; got {reference_count}"
            )
        allowed_values = constraints.get("allowedValues")
        ratios = allowed_values.get("ratio") if isinstance(allowed_values, dict) else None
        ratios = ratios or capability.get("supportedRatios") or []
        requested_ratio = inputs.get("aspect_ratio")
        if requested_ratio and ratios and requested_ratio not in ratios:
            errors.append(
                f"DoFe model {model!r} does not support ratio {requested_ratio!r}; "
                f"use one of: {', '.join(ratios)}"
            )
        if operation == "image_to_image" and reference_count == 0:
            errors.append("image_to_image requires at least one input image")
        if operation == "image_edit" and reference_count == 0:
            errors.append("image_edit requires at least one input image")
        input_transport = constraints.get("inputTransport")
        if operation in {"image_edit", "image_to_image"} and input_transport == "data_uri_only":
            for key in ("image_url", "image_urls", "mask_url"):
                if inputs.get(key):
                    errors.append(
                        f"DoFe model {model!r} operation {operation!r} requires inline data URIs; "
                        f"{key} is not allowed"
                    )
        # FOLLOW-UP-REVIEW P1：operation 投影已声明 mask / outputCount / imageSizeRule /
        # allowedParams——preflight 据此校验 mask 支持、输出数量与像素/边长规则。
        if has_mask and operation == "image_edit" and constraints.get("mask") is not True:
            errors.append(
                f"DoFe model {model!r} operation {operation!r} does not declare mask support"
            )
        output_count = constraints.get("outputCount")
        requested_n = int(inputs.get("n") or 1)
        if isinstance(output_count, dict) and output_count.get("max") is not None:
            if requested_n > int(output_count["max"]):
                errors.append(
                    f"DoFe model {model!r} operation {operation!r} accepts at most "
                    f"{output_count['max']} outputs; got {requested_n}"
                )
        size_rule = constraints.get("imageSizeRule")
        if isinstance(size_rule, dict):
            resolution = self._resolution(inputs, model)
            dims = self._parse_resolution(resolution)
            if dims is not None:
                width, height = dims
                pixels = width * height
                min_pixels = size_rule.get("minPixels")
                max_pixels = size_rule.get("maxPixels")
                if min_pixels is not None and pixels < int(min_pixels):
                    errors.append(
                        f"DoFe model {model!r} requires at least {min_pixels} pixels; got {pixels}"
                    )
                if max_pixels is not None and pixels > int(max_pixels):
                    errors.append(
                        f"DoFe model {model!r} allows at most {max_pixels} pixels; got {pixels}"
                    )
                edge = size_rule.get("edgeMultiple")
                if edge is not None and (width % int(edge) != 0 or height % int(edge) != 0):
                    errors.append(
                        f"DoFe model {model!r} requires edges to be multiples of {edge}; got {resolution}"
                    )
        # operation 声明 allowedParams 时，校验显式传入的参数键在集合内（避免发给不支持参数的 lane）。
        # P1 复审：Playground 输出 camelCase（outputFormat/outputCompression/responseFormat），
        # 本工具输入是 snake_case——比较前归一，否则已支持参数被误拦。
        allowed_params = constraints.get("allowedParams")
        if isinstance(allowed_params, list):
            requested_params = [
                "output_format",
                "output_compression",
                "response_format",
                "background",
                "moderation",
                "quality",
                "style",
                "thinking",
            ]
            unsupported = [
                key
                for key in requested_params
                if inputs.get(key) is not None
                and self._playground_param_name(key) not in allowed_params
            ]
            if unsupported:
                errors.append(
                    f"DoFe model {model!r} operation {operation!r} does not accept params: "
                    f"{', '.join(unsupported)}"
                )

        return {
            "status": "blocked" if errors else "passed",
            "verification_scope": [
                "tenant_model_catalog",
                "playground_capability",
                "image_operation",
                "reference_binding",
            ],
            "model": model,
            "operation": operation,
            "reference_binding": {
                "input_asset_count": reference_count,
                "max_input_assets": max_input_assets,
            },
            "errors": errors,
            "warnings": [],
        }

    @staticmethod
    def _resolution(inputs: dict[str, Any], model: str | None = None) -> str:
        # size 优先于 resolution（与网关路由口径一致）。显式档位（如 1K/2K）与 WxH 都原样转发，
        # 不把非 x 档位回退成 1024x1024（P1 复审：静默方图）。Seedream 的默认 2K
        # 同时满足其 capability 最低像素约束；其他模型保留历史 1024x1024 默认值。
        explicit = inputs.get("size") or inputs.get("resolution")
        if explicit:
            return str(explicit)
        has_explicit_dimensions = (
            inputs.get("width") is not None or inputs.get("height") is not None
        )
        model_family = (model or "").rsplit("/", 1)[-1].lower()
        if not has_explicit_dimensions and model_family.startswith("seedream-"):
            return "2K"
        width = inputs.get("width", 1024)
        height = inputs.get("height", 1024)
        return f"{int(width)}x{int(height)}"

    @staticmethod
    def _parse_resolution(resolution: str) -> tuple[int, int] | None:
        match = re.fullmatch(r"(\d+)\s*[xX]\s*(\d+)", resolution.strip())
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))

    @staticmethod
    def _playground_param_name(key: str) -> str:
        """OpenMontage snake_case 参数名 → Playground 投影的 camelCase 名（P1 复审：命名对齐）。"""
        return {
            "output_format": "outputFormat",
            "output_compression": "outputCompression",
            "response_format": "responseFormat",
            "background": "background",
            "moderation": "moderation",
            "quality": "quality",
            "style": "style",
            "thinking": "thinking",
        }.get(key, key)

    @staticmethod
    def _collect_references(inputs: dict[str, Any]) -> list[tuple[str | None, str | None]]:
        references: list[tuple[str | None, str | None]] = []
        if inputs.get("image_url") or inputs.get("image_path"):
            references.append((inputs.get("image_url"), inputs.get("image_path")))
        references.extend((url, None) for url in inputs.get("image_urls") or [])
        references.extend((None, path) for path in inputs.get("image_paths") or [])
        return references

    def _build_payload(self, inputs: dict[str, Any], model: str) -> dict[str, Any]:
        prompt = str(inputs.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")

        # CRITICAL (dev-guide §2.3): the text block must NOT carry a role.
        content: list[dict[str, Any]] = [{"part": {"type": "text", "text": prompt}, "order": 0}]

        for reference_url, reference_path in self._collect_references(inputs):
            url = resolve_image_source(url=reference_url, path=reference_path)
            content.append(
                {
                    "part": {"type": "image_url", "image_url": {"url": url}},
                    "order": len(content),
                    "role": "reference",
                }
            )

        mask_url = inputs.get("mask_url")
        mask_path = inputs.get("mask_path")
        if mask_url or mask_path:
            url = resolve_image_source(url=mask_url, path=mask_path)
            content.append(
                {
                    "part": {"type": "image_url", "image_url": {"url": url}},
                    "order": len(content),
                    "role": "mask",
                }
            )

        params: dict[str, Any] = {
            "resolution": self._resolution(inputs, model),
            "outputCount": max(1, int(inputs.get("n") or 1)),
        }
        if inputs.get("negative_prompt") and self._supports_negative_prompt(model):
            params["negativePrompt"] = inputs["negative_prompt"]
        if inputs.get("quality"):
            params["quality"] = inputs["quality"]
        if inputs.get("style"):
            params["style"] = inputs["style"]
        if inputs.get("seed") is not None:
            params["seed"] = inputs["seed"]
        if inputs.get("aspect_ratio"):
            params["ratio"] = inputs["aspect_ratio"]
        if inputs.get("output_format"):
            params["output_format"] = inputs["output_format"]
        if inputs.get("output_compression") is not None:
            params["output_compression"] = inputs["output_compression"]
        if inputs.get("response_format"):
            params["response_format"] = inputs["response_format"]
        if inputs.get("background"):
            params["background"] = inputs["background"]
        if inputs.get("moderation"):
            params["moderation"] = inputs["moderation"]
        if inputs.get("thinking"):
            params["thinking"] = inputs["thinking"]

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
