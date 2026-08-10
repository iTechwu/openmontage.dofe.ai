"""DoFe.AI gateway video generation (endpointKind: video_async).

The model must be selected from the current tenant's ``GET /v1/models``
catalog. Failures surface the model, reason, and suggestion without silently
substituting another model or provider.
"""

from __future__ import annotations

import hashlib
import json
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
from tools.dofe import (
    DofeClient,
    DofeError,
    DofePricingClient,
    DofePricingError,
    DofeToolSpec,
    probe_video,
    resolve_image_source,
)
from tools.dofe.models import resolve_alias, validate_catalog_alias
from tools.dofe.runtime import build_metadata, run_dofe_generation
from tools.dofe.status import (
    configured_model_is_visible,
    resolve_catalog,
    resolve_playground_capability,
)

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
        "Read GET /v1/models and set DOFE_VIDEO_MODEL to one returned model ID."
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
        operation = str((inputs or {}).get("operation") or "text_to_video")
        selected = str(
            resolve_alias(
                "video",
                operation,
                explicit=(inputs or {}).get("model_name"),
            )
            or ""
        )
        if not selected:
            return list(self.agent_skills)
        catalog, ok = resolve_catalog()
        if not ok or catalog is None:
            return list(self.agent_skills)
        try:
            selected = validate_catalog_alias(selected, catalog)
        except DofeError:
            return list(self.agent_skills)
        return list(
            self._SEEDANCE_SKILLS
            if "seedance" in selected.lower()
            else self.agent_skills
        )

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
        "video generation using an exact ID from the tenant-visible gateway catalog",
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
                "description": "Exact ID from GET /v1/models. Overrides DOFE_VIDEO_MODEL.",
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

    def get_status(self) -> ToolStatus:
        status = super().get_status()
        if status == ToolStatus.UNAVAILABLE:
            return status
        return (
            ToolStatus.AVAILABLE
            if configured_model_is_visible(
                "video",
                (
                    "text_to_video",
                    "image_to_video",
                    "reference_to_video",
                ),
            )
            else ToolStatus.UNAVAILABLE
        )

    def probe_provider_contract(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Validate the exact model and operation against DoFe's live projection.

        The tenant model catalog is fetched through the authenticated
        :func:`resolve_catalog` entry point so preflight and paid execution share
        one ``GET /v1/models`` read without ever trusting a caller-supplied
        (potentially forged) catalog.
        """

        operation = str(inputs.get("operation") or "text_to_video")
        requested_model = self.resolve_model(inputs)
        if not requested_model:
            return _blocked_probe(
                operation=operation,
                errors=["No DoFe video model ID is configured for this operation"],
            )

        try:
            client = DofeClient()
            catalog, ok = resolve_catalog()
            if not ok or catalog is None:
                raise DofeError("DoFe model catalog unavailable")
            model = validate_catalog_alias(requested_model, catalog)
            capability = resolve_playground_capability(client, model)
        except DofeError as exc:
            return _blocked_probe(
                operation=operation,
                model=requested_model,
                errors=[f"DoFe live capability validation failed: {exc}"],
            )

        errors: list[str] = []
        warnings: list[str] = []
        readiness = capability.get("readiness")
        readiness_items = readiness if isinstance(readiness, list) else []
        for item in readiness_items:
            if not isinstance(item, dict):
                continue
            message = f"DoFe readiness {item.get('code') or 'unknown'}"
            if item.get("severity") == "blocked":
                errors.append(message)
            else:
                warnings.append(message)

        if capability.get("modelType") != "video":
            errors.append("DoFe capability modelType is not video")
        capability_input = capability.get("input")
        if not isinstance(capability_input, dict) or capability_input.get("text") is not True:
            errors.append("DoFe capability does not accept the required text prompt")
        if capability.get("state") not in {"ready", "warning"}:
            errors.append(
                f"DoFe capability state {capability.get('state')!r} is not executable"
            )
        if capability.get("executor") != "generation_task":
            errors.append("DoFe capability executor is not generation_task")
        if capability.get("endpointKind") != "video_async":
            errors.append("DoFe capability endpointKind is not video_async")
        output = capability.get("output")
        if not isinstance(output, dict) or output.get("mode") not in {"task", "asset"}:
            errors.append("DoFe capability output mode is not task/asset")

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
            errors.extend(_validate_operation_contract(operation, inputs, constraints))

        form = capability.get("form")
        raw_fields = form.get("fields") if isinstance(form, dict) else None
        fields = [item for item in (raw_fields or []) if isinstance(item, dict)]
        provider_params = self._build_params(inputs)
        provider_params.pop("videoOperation", None)
        errors.extend(_validate_provider_fields(provider_params, fields))

        fingerprint = hashlib.sha256(
            json.dumps(capability, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return {
            "status": "blocked" if errors else "passed",
            "verification_scope": [
                "tenant_model_catalog",
                "playground_capability",
                "video_operation",
                "reference_binding",
                "provider_parameters",
            ],
            "provider_contract_version": fingerprint,
            "model": model,
            "operation": operation,
            "endpoint_kind": capability.get("endpointKind"),
            "reference_binding": {
                "accepted_asset_types": list(constraints.get("acceptedAssetTypes") or []),
                "roles": list(constraints.get("roles") or []),
                "min_input_assets": constraints.get("minInputAssets"),
                "max_input_assets": constraints.get("maxInputAssets"),
            },
            "input_fields": sorted(
                str(field["key"]) for field in fields if field.get("key")
            ),
            "warnings": warnings,
            "errors": errors,
        }

    # ------------------------------------------------------------------ cost

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # BaseTool's legacy estimate contract is USD-only. Seedance is billed in
        # CNY, so the authoritative native quote is exposed by dry_run instead.
        return 0.0

    def pricing_quote(self, inputs: dict[str, Any]) -> dict[str, Any]:
        requested_model = self.resolve_model(inputs)
        if not requested_model:
            raise DofePricingError("No Airouter video model alias could be resolved")
        try:
            model = validate_catalog_alias(
                requested_model,
                DofeClient().list_models(),
            )
        except DofeError as exc:
            raise DofePricingError(
                f"Airouter model catalog validation failed: {exc}"
            ) from exc
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

    @staticmethod
    def _build_params(inputs: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "videoOperation": inputs.get("operation", "text_to_video"),
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
        return params

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
            refs: list[str] = []
            single_url = inputs.get("reference_image_url") or inputs.get("image_url")
            single_path = inputs.get("reference_image_path") or inputs.get("image_path")
            if single_url or single_path:
                refs.append(resolve_image_source(url=single_url, path=single_path))
            for remote_url in inputs.get("reference_image_urls") or []:
                refs.append(resolve_image_source(url=remote_url))
            for local_path in inputs.get("reference_image_paths") or []:
                refs.append(resolve_image_source(path=local_path))
            refs = list(dict.fromkeys(refs))
            if len(refs) > MAX_REFERENCE_IMAGES:
                raise ValueError(
                    f"dofe reference_to_video accepts at most {MAX_REFERENCE_IMAGES} reference images; "
                    f"got {len(refs)}"
                )
            if not refs:
                raise ValueError(
                    "reference_to_video requires a reference image URL or path"
                )
            for ref_url in refs:
                content.append(
                    {
                        "part": {"type": "image_url", "image_url": {"url": ref_url}},
                        "order": len(content),
                        "role": "reference",
                    }
                )

        payload: dict[str, Any] = {
            "model": model,
            "endpointKind": "video_async",
            "content": content,
            "params": self._build_params(inputs),
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
        # The shared paid boundary in run_dofe_generation fetches the
        # authenticated catalog, validates the exact model, and enforces the
        # live provider preflight. execute() never accepts or forwards a
        # caller-supplied catalog — a forgeable snapshot cannot unlock paid
        # generation (dev-guide §model-catalog).
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


def _blocked_probe(
    *,
    operation: str,
    errors: list[str],
    model: str | None = None,
) -> dict[str, Any]:
    return {
        "status": "blocked",
        "verification_scope": ["tenant_model_catalog"],
        "model": model,
        "operation": operation,
        "warnings": [],
        "errors": errors,
    }


def _input_asset_count(operation: str, inputs: dict[str, Any]) -> int:
    if operation == "image_to_video":
        return int(
            bool(
                inputs.get("image_url")
                or inputs.get("image_path")
                or inputs.get("reference_image_url")
                or inputs.get("reference_image_path")
            )
        )
    if operation == "reference_to_video":
        sources: list[str] = []
        single_source = (
            inputs.get("reference_image_url")
            or inputs.get("image_url")
            or inputs.get("reference_image_path")
            or inputs.get("image_path")
        )
        if single_source:
            sources.append(str(single_source))
        sources.extend(str(value) for value in inputs.get("reference_image_urls") or [])
        sources.extend(str(value) for value in inputs.get("reference_image_paths") or [])
        return len(dict.fromkeys(sources))
    return 0


def _validate_operation_contract(
    operation: str,
    inputs: dict[str, Any],
    constraints: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    asset_count = _input_asset_count(operation, inputs)
    if operation in {"image_to_video", "reference_to_video"}:
        if "image" not in (constraints.get("acceptedAssetTypes") or []):
            errors.append(f"DoFe operation {operation!r} does not accept image assets")
        required_role = "first_frame" if operation == "image_to_video" else "reference"
        if required_role not in (constraints.get("roles") or []):
            errors.append(
                f"DoFe operation {operation!r} does not expose role {required_role!r}"
            )

    minimum = constraints.get("minInputAssets")
    maximum = constraints.get("maxInputAssets")
    if isinstance(minimum, int) and asset_count < minimum:
        errors.append(
            f"DoFe operation {operation!r} requires at least {minimum} input assets"
        )
    if isinstance(maximum, int) and asset_count > maximum:
        errors.append(
            f"DoFe operation {operation!r} accepts at most {maximum} input assets"
        )

    allowed_values = constraints.get("allowedValues")
    allowed_operations = (
        allowed_values.get("videoOperation")
        if isinstance(allowed_values, dict)
        else None
    )
    if isinstance(allowed_operations, list) and operation not in allowed_operations:
        errors.append(
            f"DoFe operation constraint excludes videoOperation {operation!r}"
        )
    return errors


def _validate_provider_fields(
    parameter_values: dict[str, Any],
    fields: list[dict[str, Any]],
) -> list[str]:
    field_by_key = {
        str(field["key"]): field for field in fields if isinstance(field.get("key"), str)
    }
    errors: list[str] = []
    for key, field in field_by_key.items():
        if field.get("required") is True and key not in parameter_values:
            errors.append(f"DoFe capability is missing required provider parameter {key!r}")
    for key, value in parameter_values.items():
        field = field_by_key.get(key)
        if field is None:
            errors.append(f"DoFe capability form does not expose provider parameter {key!r}")
            continue
        options = field.get("options")
        if isinstance(options, list) and options and value not in options:
            errors.append(f"DoFe capability field {key!r} does not allow value {value!r}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = field.get("min")
            maximum = field.get("max")
            if isinstance(minimum, (int, float)) and value < minimum:
                errors.append(f"DoFe capability field {key!r} requires a value >= {minimum}")
            if isinstance(maximum, (int, float)) and value > maximum:
                errors.append(f"DoFe capability field {key!r} requires a value <= {maximum}")
    return errors
