"""Speech-to-text through AIRouter's ``openspeech-auc`` model."""

from __future__ import annotations

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
    ToolTier,
)
from tools.dofe import DofeClient, DofeError
from tools.dofe import config as dofe_config
from tools.dofe.media import is_https_url
from tools.dofe.media_upload import DofeMediaUploadClient, DofeMediaUploadError
from tools.dofe.models import resolve_alias
from tools.dofe.runtime import _parse_cost, build_metadata


class DofeSpeechToText(BaseTool):
    name = "dofe_stt"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "analysis"
    provider = "dofe"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    dependencies = ["env:DOFE_MODEL_API_KEY|DOFE_API_KEY"]
    install_instructions = (
        "Set DOFE_MODEL_API_KEY and grant that tenant access to the restricted "
        "AIRouter model alias configured by DOFE_STT_MODEL (default openspeech-auc)."
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
                    "API before submitting openspeech-auc."
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
        "audio_path",
        "duration_seconds",
        "language",
        "asr_mode",
    ]
    side_effects = [
        "uploads local audio to dofe-transcode through the AIRouter internal API",
        "paid transcription via model.local.dofe.ai/api",
        "writes transcript JSON",
    ]
    user_visible_verification = ["Review transcript text against the source audio"]

    def resolve_model(self, inputs: dict[str, Any]) -> str | None:
        return resolve_alias("stt", "transcribe", explicit=inputs.get("model_name"))

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # AIRouter owns the rate card and returns an estimate in native currency.
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return max(20.0, float(inputs.get("duration_seconds") or 0) * 0.25)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        try:
            self.check_dependencies()
        except DependencyError as exc:
            return ToolResult(success=False, error=str(exc))

        model = self.resolve_model(inputs)
        if not model:
            return ToolResult(success=False, error="Set DOFE_STT_MODEL to an AIRouter STT alias.")

        audio_url = str(inputs.get("audio_url") or "").strip()
        source_asset: dict[str, Any] | None = None
        audio_path = str(inputs.get("audio_path") or "").strip()
        if audio_path:
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
        if not (is_https_url(audio_url) or audio_url.startswith("tos://")):
            return ToolResult(
                success=False,
                error=(
                    "AIRouter openspeech-auc requires a provider-accessible https:// or "
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
        metadata = build_metadata(inputs, self.idempotency_key(inputs))
        if metadata:
            payload["metadata"] = metadata

        try:
            result = DofeClient().submit_and_collect(
                payload,
                timeout_seconds=dofe_config.poll_max_seconds("stt"),
                poll_interval=dofe_config.poll_interval(),
                existing_task_id=inputs.get("task_id"),
            )
        except DofeError as exc:
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
        output = Path(inputs.get("output_path") or "projects/dofe/analysis/transcript.json")
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
            "billing": billing,
            **({"source_asset": source_asset} if source_asset else {}),
        }
        output.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
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
