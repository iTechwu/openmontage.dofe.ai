"""Speech-to-text through a tenant-visible AIRouter catalog model."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
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
from tools.dofe import DofeClient, DofeError
from tools.dofe import config as dofe_config
from tools.dofe.media import is_https_url
from tools.dofe.media_upload import DofeMediaUploadClient, DofeMediaUploadError
from tools.dofe.models import catalog_model_ids, resolve_alias, validate_catalog_alias
from tools.dofe.runtime import _parse_cost, build_metadata
from tools.dofe.status import configured_model_is_visible


def _write_resume_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending_path = path.with_name(f"{path.name}.tmp")
    pending_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pending_path.replace(path)


class DofeSpeechToText(BaseTool):
    name = "dofe_stt"
    version = "0.1.1"
    tier = ToolTier.CORE
    capability = "analysis"
    provider = "dofe"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    dependencies = ["env:DOFE_MODEL_API_KEY|DOFE_API_KEY"]
    install_instructions = (
        "Set DOFE_MODEL_API_KEY, read GET /v1/models, and set DOFE_STT_MODEL "
        "to an exact model ID returned for that tenant."
    )
    agent_skills = ["speech-to-text"]
    capabilities = ["transcribe", "language_detection"]
    best_for = ["recording-file transcription through the unified AIRouter"]
    not_good_for = ["live streaming transcription"]
    fallback_tools = []

    input_schema = {
        "type": "object",
        "oneOf": [{"required": ["audio_url"]}, {"required": ["audio_path"]}],
        "properties": {
            "audio_url": {
                "type": "string",
                "description": "Provider-accessible https:// or configured tos:// audio URL.",
            },
            "audio_path": {
                "type": "string",
                "description": (
                    "Local audio file. OpenMontage uploads it through AIRouter's internal "
                    "API before submitting the selected catalog model."
                ),
            },
            "duration_seconds": {
                "type": "number",
                "minimum": 0,
                "description": "Probed media duration used for auditable per-minute billing.",
            },
            "language": {"type": "string", "default": "zh-CN"},
            "sample_rate": {"type": "integer", "default": 16000},
            "asr_mode": {
                "type": "string",
                "enum": ["standard", "fast", "offPeak"],
                "default": "fast",
            },
            "audio_format": {"type": "string"},
            "model_name": {"type": "string"},
            "task_id": {"type": "string"},
            "output_path": {"type": "string"},
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "full_text": {"type": "string"},
            "segments": {"type": "array"},
            "language": {"type": "string"},
            "billing": {"type": "object"},
        },
    }
    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=10, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = [
        "audio_url",
        "duration_seconds",
        "language",
        "asr_mode",
        "sample_rate",
        "audio_format",
        "model_name",
    ]
    side_effects = [
        "uploads local audio to dofe-transcode through the AIRouter internal API",
        "paid transcription via model.local.dofe.ai/api",
        "writes transcript JSON",
    ]
    user_visible_verification = ["Review transcript text against the source audio"]

    def get_status(self) -> ToolStatus:
        status = super().get_status()
        if status == ToolStatus.UNAVAILABLE:
            return status
        return (
            ToolStatus.AVAILABLE
            if configured_model_is_visible("stt", ("transcribe",))
            else ToolStatus.UNAVAILABLE
        )

    def resolve_model(self, inputs: dict[str, Any]) -> str | None:
        return resolve_alias("stt", "transcribe", explicit=inputs.get("model_name"))

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # AIRouter owns the rate card and returns an estimate in native currency.
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return max(20.0, float(inputs.get("duration_seconds") or 0) * 0.25)

    def idempotency_key(self, inputs: dict[str, Any]) -> str:
        key_data = {
            "audio_url": str(inputs.get("audio_url") or "").strip() or None,
            "duration_seconds": float(inputs.get("duration_seconds") or 0),
            "language": str(inputs.get("language") or "zh-CN"),
            "asr_mode": str(inputs.get("asr_mode") or "fast"),
            "sample_rate": int(inputs.get("sample_rate") or 16000),
            "audio_format": str(inputs.get("audio_format") or "").strip() or None,
            "model_name": self.resolve_model(inputs),
            "run_id": str(inputs.get("run_id") or inputs.get("project_id") or "").strip()
            or None,
        }
        audio_path = str(inputs.get("audio_path") or "").strip()
        if audio_path:
            source = Path(audio_path)
            if source.is_file():
                digest = hashlib.sha256()
                with source.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                key_data["audio_sha256"] = digest.hexdigest()
            else:
                key_data["audio_path"] = audio_path
        raw = json.dumps(key_data, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        try:
            self.check_dependencies()
        except DependencyError as exc:
            return ToolResult(success=False, error=str(exc))

        requested_model = self.resolve_model(inputs)
        if not requested_model:
            return ToolResult(
                success=False,
                error=(
                    "Read GET /v1/models, then set DOFE_STT_MODEL to one returned ID "
                    "or pass it as model_name."
                ),
            )

        try:
            client = DofeClient()
            catalog = client.list_models()
            model = validate_catalog_alias(requested_model, catalog)
        except DofeError as exc:
            return ToolResult(
                success=False,
                data={"provider": "dofe", "model": requested_model},
                error=(
                    "AIRouter model catalog validation failed for "
                    f"{requested_model!r}: {exc}"
                ),
                duration_seconds=round(time.monotonic() - started, 2),
                model=requested_model,
            )

        output = Path(inputs.get("output_path") or "projects/dofe/analysis/transcript.json")
        idempotency_key = self.idempotency_key(inputs)
        resume_path = output.with_suffix(".resume.json")
        existing_task_id = str(inputs.get("task_id") or "").strip() or None
        resume_state: dict[str, Any] = {}
        if not existing_task_id and resume_path.is_file():
            try:
                resume = json.loads(resume_path.read_text(encoding="utf-8"))
                if resume.get("idempotency_key") == idempotency_key:
                    resume_state = resume
                    existing_task_id = str(resume.get("task_id") or "").strip() or None
            except (OSError, ValueError, AttributeError):
                pass

        audio_url = str(inputs.get("audio_url") or "").strip()
        saved_source_asset = resume_state.get("source_asset")
        source_asset: dict[str, Any] | None = (
            saved_source_asset if isinstance(saved_source_asset, dict) else None
        )
        audio_path = str(inputs.get("audio_path") or "").strip()
        if audio_path and not existing_task_id:
            saved_audio_url = str(resume_state.get("audio_url") or "").strip()
            if is_https_url(saved_audio_url) or saved_audio_url.startswith("tos://"):
                audio_url = saved_audio_url
            else:
                try:
                    source_asset = DofeMediaUploadClient().upload(audio_path, asset_type="audio")
                    audio_url = str(source_asset["url"])
                except DofeMediaUploadError as exc:
                    return ToolResult(
                        success=False,
                        data={"provider": "dofe", "model": model},
                        error=f"AIRouter audio staging failed before {model!r}: {exc}",
                        duration_seconds=round(time.monotonic() - started, 2),
                        model=model,
                    )
                resume_state = {
                    "idempotency_key": idempotency_key,
                    "audio_url": audio_url,
                    "source_asset": source_asset,
                }
                _write_resume_state(resume_path, resume_state)
        if existing_task_id:
            # The payload is not submitted on resume; only the existing task is polled.
            audio_url = "https://resume.invalid/already-submitted-audio"
        elif not (is_https_url(audio_url) or audio_url.startswith("tos://")):
            return ToolResult(
                success=False,
                error=(
                    "AIRouter STT requires a provider-accessible https:// or "
                    "configured tos:// URL. Upload the extracted local audio before STT."
                ),
                model=model,
            )

        content_part: dict[str, Any] = {"url": audio_url}
        if inputs.get("audio_format"):
            content_part["format"] = str(inputs["audio_format"])
        params: dict[str, Any] = {
            "asrMode": inputs.get("asr_mode", "fast"),
            "language": inputs.get("language", "zh-CN"),
            "sampleRate": int(inputs.get("sample_rate") or 16000),
        }
        duration_seconds = float(inputs.get("duration_seconds") or 0)
        if duration_seconds > 0:
            params["durationSeconds"] = duration_seconds

        payload: dict[str, Any] = {
            "model": model,
            "endpointKind": "speech_transcription_async",
            "content": [
                {
                    "part": {"type": "audio_url", "audio_url": content_part},
                    "order": 0,
                    "role": "audio_track",
                }
            ],
            "params": params,
        }
        metadata = build_metadata(inputs, idempotency_key)
        if metadata:
            payload["metadata"] = metadata

        try:
            result = client.submit_and_collect(
                payload,
                timeout_seconds=dofe_config.poll_max_seconds("stt"),
                poll_interval=dofe_config.poll_interval(),
                existing_task_id=existing_task_id,
            )
        except DofeError as exc:
            recoverable_task_id = str((exc.details or {}).get("task_id") or "").strip()
            if recoverable_task_id:
                resume_state.update(
                    {
                        "idempotency_key": idempotency_key,
                        "task_id": recoverable_task_id,
                    }
                )
                _write_resume_state(resume_path, resume_state)
            return ToolResult(
                success=False,
                data={"provider": "dofe", "model": model},
                error=f"AIRouter STT failed for model {model!r}: {exc}",
                duration_seconds=round(time.monotonic() - started, 2),
                model=model,
            )

        text = str(result.get("text") or "").strip()
        if not text:
            return ToolResult(
                success=False,
                data={"provider": "dofe", "model": model, "dofe_task_id": result.get("task_id")},
                error="AIRouter STT succeeded but returned no transcript text.",
                duration_seconds=round(time.monotonic() - started, 2),
                model=model,
            )

        estimated_cost = _parse_cost(result.get("estimated_cost"))
        final_cost = _parse_cost(result.get("final_cost"))
        cost_amount = final_cost if final_cost is not None else estimated_cost
        cost_currency = str(result.get("cost_currency") or "").upper() or None
        cost_source = "gateway_final" if final_cost is not None else "gateway_estimate"
        billing = {
            "amount": cost_amount,
            "currency": cost_currency,
            "source": cost_source,
            "is_final": final_cost is not None,
            "pricing_breakdown": result.get("pricing_breakdown"),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        transcript = {
            "full_text": text,
            "segments": [],
            "language": params["language"],
            "word_count": len(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", text)),
            "character_count": len(text),
            "provider": "dofe",
            "model": model,
            "dofe_task_id": result.get("task_id"),
            "catalog_model_count": len(catalog_model_ids(catalog)),
            "billing": billing,
            **({"source_asset": source_asset} if source_asset else {}),
        }
        output.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
        resume_path.unlink(missing_ok=True)
        return ToolResult(
            success=True,
            data=transcript,
            artifacts=[str(output)],
            cost_usd=cost_amount if cost_currency == "USD" and cost_amount is not None else 0.0,
            cost_amount=cost_amount,
            cost_currency=cost_currency,
            cost_source=cost_source,
            duration_seconds=round(time.monotonic() - started, 2),
            model=model,
        )
