"""Shared execute body for the five dofe provider tools (dev-guide §3.4).

Each tool is thin: it declares a :class:`DofeToolSpec`, a ``resolve_model()``,
and a ``_build_payload(inputs, model)``, then delegates ``execute()`` to
:func:`run_dofe_generation`. Centralizing the create→poll→download→probe flow
keeps all five tools consistent and prevents drift in error handling.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from tools.base_tool import ToolResult

from . import config as cfg
from .client import DofeClient
from .errors import (
    DofeAPIError,
    DofeAuthError,
    DofeError,
    DofeModelUnavailableError,
    DofeNetworkError,
    DofeQuotaError,
    DofeRateLimitError,
    DofeTaskFailedError,
    DofeTaskTimeoutError,
)
from .media import sanitize_for_log
from .models import catalog_model_ids, config_env_name, validate_catalog_alias

ARTIFACT_MIN_BYTES = 1024  # dev-guide §4.4: image/audio/video ≥ 1KB


@dataclass(frozen=True)
class DofeToolSpec:
    """Describes how a dofe tool talks to the gateway."""

    capability: str           # video / image / tts / music / avatar
    endpoint_kind: str        # image_async / video_async / speech_synthesis / ...
    asset_kind: str           # image / video / audio — which artifact to download
    default_ext: str          # ".png" / ".mp4" / ".mp3"
    probe: Callable[[Path], dict[str, Any]]  # ffprobe / audio probe / size-only
    min_bytes: int = ARTIFACT_MIN_BYTES


# --------------------------------------------------------------------- probes

def probe_video(path: Path) -> dict[str, Any]:
    from tools.video._shared import probe_output

    return probe_output(path)


def probe_audio(path: Path) -> dict[str, Any]:
    from tools.analysis.audio_probe import probe_duration

    info: dict[str, Any] = {"file_size_bytes": path.stat().st_size}
    duration = probe_duration(str(path))
    if duration:
        info["audio_duration_seconds"] = round(duration, 2)
    return info


def probe_image(path: Path) -> dict[str, Any]:
    from PIL import Image

    try:
        with Image.open(path) as image:
            return {
                "file_size_bytes": path.stat().st_size,
                "image_format": str(image.format or "").lower(),
                "width": image.width,
                "height": image.height,
            }
    except OSError as exc:
        raise DofeError(f"downloaded dofe image is invalid: {exc}") from exc


def _normalize_image_format(path: Path) -> None:
    """Transcode an image when the upstream bytes do not match the target suffix."""

    from PIL import Image

    expected_by_suffix = {
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".png": "PNG",
        ".webp": "WEBP",
    }
    expected = expected_by_suffix.get(path.suffix.lower())
    if expected is None:
        return

    temporary = path.with_name(f".{path.stem}.converted{path.suffix}")
    try:
        with Image.open(path) as image:
            actual = str(image.format or "").upper()
            if actual == expected:
                image.verify()
                return
            image.load()
            converted = image.convert("RGB") if expected == "JPEG" and image.mode not in {"RGB", "L"} else image.copy()
            converted.save(temporary, format=expected)
        temporary.replace(path)
    except OSError as exc:
        if temporary.is_file():
            temporary.unlink()
        raise DofeError(f"downloaded dofe image could not be normalized: {exc}") from exc


# ----------------------------------------------------------------- output path

def _enforce_projects_path(provided: str | None, tool_name: str, ext: str) -> Path:
    """Guarantee the output lives under ``projects/<id>/`` (dev-guide §6.2)."""

    suffix = ext if ext.startswith(".") else f".{ext}"
    if provided:
        target = Path(provided)
        if not target.suffix:
            target = target.with_suffix(suffix)
    else:
        target = Path("projects") / "dofe" / "assets" / f"{tool_name}{suffix}"
    if "projects" not in target.parts:
        # Never write to the repo root / cwd — force the workspace contract.
        target = Path("projects") / "dofe" / "assets" / target.name
        if not target.suffix:
            target = target.with_suffix(suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


# ----------------------------------------------------------------- artifacts

def _pick_asset(assets: list[dict[str, Any]] | None, kind: str) -> dict[str, Any] | None:
    if not assets:
        return None
    for asset in assets:
        if isinstance(asset, dict) and str(asset.get("type", "")).lower() == kind and asset.get("url"):
            return asset
    for asset in assets:  # fall back to the first asset with a URL
        if isinstance(asset, dict) and asset.get("url"):
            return asset
    return None


def _credential_free_url(value: Any) -> str | None:
    """Keep artifact provenance without exposing presigned query credentials."""

    if not isinstance(value, str) or not value:
        return None
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _validate_artifact(path: Path, min_bytes: int) -> None:
    size = path.stat().st_size
    if size <= 0 or size < min_bytes:
        try:
            path.unlink()
        except OSError:
            pass
        raise DofeError(
            f"downloaded dofe artifact is too small ({size} bytes; expected >= {min_bytes}); discarded"
        )


def _parse_cost(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(str(value)), 6)
    except (TypeError, ValueError):
        return None


def build_metadata(inputs: dict[str, Any], idempotency_key: str | None) -> dict[str, Any]:
    """Build the gateway ``metadata`` block: run provenance + idempotency key.

    The idempotency key lets a retried create-task de-dupe on the server side
    (dev-guide §6.1). It never contains the API key.
    """

    metadata: dict[str, Any] = {}
    run_id = inputs.get("run_id") or inputs.get("project_id")
    if run_id:
        metadata["openmontage_run_id"] = str(run_id)
    if idempotency_key:
        metadata["openmontage_idempotency_key"] = idempotency_key
    return metadata


# ----------------------------------------------------------------- error text

def _suggestion(exc: DofeError, capability: str) -> str:
    env_var = config_env_name(capability) or f"DOFE_{capability.upper()}_MODEL"
    if isinstance(exc, DofeAuthError):
        return "Check that DOFE_MODEL_API_KEY is valid and permitted to use this model."
    if isinstance(exc, DofeQuotaError):
        return "Gateway billing/quota is not configured for this model; check the dofe account balance."
    if isinstance(exc, DofeModelUnavailableError):
        return (
            f"Alias is not published or not visible to your key. Verify {env_var} matches the gateway "
            "GET /v1/models catalog exactly."
        )
    if isinstance(exc, DofeRateLimitError):
        wait = f" retry after {exc.retry_after}s" if exc.retry_after is not None else ""
        return f"Rate limited by the gateway;{wait} then retry."
    if isinstance(exc, DofeTaskTimeoutError):
        return "Timed out waiting for the task (it was cancelled). Retry, or resume with inputs.task_id."
    if isinstance(exc, DofeTaskFailedError):
        return "The task failed server-side; try a different model or parameters."
    if isinstance(exc, DofeNetworkError):
        return "Could not reach the dofe gateway; check DOFE_MODEL_BASE_URL and network connectivity."
    if isinstance(exc, DofeAPIError):
        reason = (exc.details or {}).get("reason")
        if reason == "param_price_not_found":
            return "Billing is not configured for this model at the requested parameters."
        if exc.http_status == 500:
            return f"The gateway may not have an adapter for this model yet; try a different {env_var}."
    return ""


def _format_user_error(exc: DofeError, model: str, spec: DofeToolSpec) -> str:
    details = getattr(exc, "details", None) or {}
    recommendation = ""
    if details.get("recommendedAction"):
        recommendation = f" Gateway says: {details['recommendedAction']}"
    message = (
        f"dofe {spec.capability} generation failed for model {model!r}: {exc.message}.{recommendation}"
    )
    suggestion = _suggestion(exc, spec.capability)
    if suggestion:
        message += f" {suggestion}"
    trace = getattr(exc, "trace_id", None)
    if trace:
        message += f" (trace={trace})"
    return message


def _error_result(
    tool: Any,
    exc: DofeError,
    model: str | None,
    spec: DofeToolSpec,
    start: float,
) -> ToolResult:
    data: dict[str, Any] = {
        "provider": "dofe",
        "model": model,
        "dofe_error_code": getattr(exc, "code", None),
        "dofe_http_status": getattr(exc, "http_status", None),
    }
    if isinstance(exc, DofeTaskFailedError):
        data["dofe_task_id"] = exc.task_id
        if exc.error_code:
            data["dofe_error_code"] = exc.error_code
    if isinstance(exc, DofeTaskTimeoutError) and exc.task_id:
        # Supports re-entrant resume: re-run with inputs.task_id to continue polling.
        data["pending_task"] = {
            "task_id": exc.task_id,
            "provider": "dofe",
            "model": model,
            "capability": spec.capability,
        }
    return ToolResult(
        success=False,
        data=data,
        error=_format_user_error(exc, str(model), spec),
        duration_seconds=round(time.monotonic() - start, 2),
        model=model,
    )


# ----------------------------------------------------------------- main entry

def run_dofe_generation(tool: Any, inputs: dict[str, Any]) -> ToolResult:
    """Execute a dofe generation end-to-end.

    ``tool`` must expose: ``dofe_spec`` (DofeToolSpec), ``resolve_model(inputs)``,
    ``_build_payload(inputs, model)``, ``estimate_cost(inputs)``, ``name``,
    ``install_instructions``.
    """

    api_key = cfg.dofe_api_key()
    if not api_key:
        return ToolResult(
            success=False,
            error="DOFE_MODEL_API_KEY / DOFE_API_KEY is not set. "
            + getattr(tool, "install_instructions", ""),
        )

    spec: DofeToolSpec = tool.dofe_spec
    start = time.monotonic()

    requested_model = tool.resolve_model(inputs)
    if not requested_model:
        env_var = config_env_name(spec.capability) or f"DOFE_{spec.capability.upper()}_MODEL"
        return ToolResult(
            success=False,
            error=(
                f"No dofe {spec.capability} model selected. Read GET /v1/models, then set "
                f"{env_var} to one returned ID or pass it as model_name. "
                f"(capability={spec.capability})"
            ),
        )

    client = DofeClient(api_key=api_key)
    try:
        catalog = client.list_models()
        model = validate_catalog_alias(requested_model, catalog)
    except DofeError as exc:
        return _error_result(tool, exc, requested_model, spec, start)

    output_path = _enforce_projects_path(inputs.get("output_path"), tool.name, spec.default_ext)

    try:
        payload = tool._build_payload(inputs, model)
    except (ValueError, FileNotFoundError) as exc:
        return ToolResult(
            success=False,
            data={"provider": "dofe", "model": model},
            error=f"dofe {spec.capability} request invalid: {exc}",
            duration_seconds=round(time.monotonic() - start, 2),
        )

    try:
        result = client.submit_and_collect(
            payload,
            timeout_seconds=cfg.poll_max_seconds(spec.capability),
            poll_interval=cfg.poll_interval(),
            asset_kind=spec.asset_kind,
            existing_task_id=inputs.get("task_id"),
        )
        asset = _pick_asset(result.get("assets"), spec.asset_kind)
        if not asset:
            raise DofeError(
                f"dofe task {result.get('task_id')} returned no {spec.asset_kind} artifact"
            )
        url = asset.get("url")
        if not url:
            raise DofeError(f"dofe artifact has no download URL: {sanitize_for_log(asset)}")
        client.download(url, output_path)
        if spec.asset_kind == "image":
            _normalize_image_format(output_path)
        _validate_artifact(output_path, spec.min_bytes)
        probed = spec.probe(output_path)
    except DofeError as exc:
        return _error_result(tool, exc, model, spec, start)

    estimated_cost = _parse_cost(result.get("estimated_cost"))
    final_cost = _parse_cost(result.get("final_cost"))
    cost_amount = final_cost if final_cost is not None else estimated_cost
    cost_currency = str(result.get("cost_currency") or "").upper() or None
    cost_source = "gateway_final" if final_cost is not None else "gateway_estimate"
    pricing_breakdown = result.get("pricing_breakdown")
    data: dict[str, Any] = {
        "provider": "dofe",
        "model": model,
        "dofe_task_id": result.get("task_id"),
        "dofe_status": result.get("status"),
        "dofe_final_cost": final_cost,
        "dofe_estimated_cost": estimated_cost,
        "billing": {
            "amount": cost_amount,
            "currency": cost_currency,
            "source": cost_source,
            "is_final": final_cost is not None,
            "pricing_breakdown": (
                pricing_breakdown if isinstance(pricing_breakdown, dict) else None
            ),
        },
        "dofe_endpoint_kind": spec.endpoint_kind,
        "provider_asset_urls": [
            safe_url
            for asset in result.get("assets", [])
            if isinstance(asset, dict)
            if (safe_url := _credential_free_url(asset.get("url")))
        ],
        "alternatives_considered": [],
        "catalog_model_count": len(catalog_model_ids(catalog)),
        "output": str(output_path),
        "output_path": str(output_path),
        "format": output_path.suffix.lstrip(".") or spec.default_ext.lstrip("."),
        **probed,
    }
    if inputs.get("seed") is not None:
        data["seed"] = inputs["seed"]

    return ToolResult(
        success=True,
        data=data,
        artifacts=[str(output_path)],
        cost_usd=cost_amount if cost_currency == "USD" and cost_amount is not None else 0.0,
        cost_amount=cost_amount,
        cost_currency=cost_currency,
        cost_source=cost_source,
        duration_seconds=round(time.monotonic() - start, 2),
        model=model,
    )
