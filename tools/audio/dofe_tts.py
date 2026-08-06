"""DoFe.AI gateway text-to-speech (endpointKind: speech_synthesis).

Protocol-ready. No TTS alias is currently visible through the configured
gateway key, so a missing model surfaces a clear "set DOFE_TTS_MODEL" error
rather than sending a request with a guessed alias.
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
from tools.dofe import DofeToolSpec, probe_audio
from tools.dofe.models import resolve_alias
from tools.dofe.runtime import build_metadata, run_dofe_generation


class DofeTTS(BaseTool):
    name = "dofe_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "dofe"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:DOFE_MODEL_API_KEY|DOFE_API_KEY", "env:DOFE_TTS_MODEL"]
    install_instructions = (
        "Set DOFE_MODEL_API_KEY in .env for the models.dofe.ai gateway. "
        "Set DOFE_ENABLED=true to make tts_selector prefer the dofe chain. "
        "Set DOFE_TTS_MODEL to a published TTS alias once the gateway registers one."
    )
    agent_skills = ["text-to-speech"]

    dofe_spec = DofeToolSpec(
        capability="tts",
        endpoint_kind="speech_synthesis",
        asset_kind="audio",
        default_ext=".mp3",
        probe=probe_audio,
    )

    capabilities = ["text_to_speech"]
    supports = {
        "multilingual": True,
        "voice_selection": True,
        "offline": False,
    }
    best_for = ["TTS via the models.dofe.ai gateway when DOFE_ENABLED=true"]
    not_good_for = ["offline generation", "voice cloning"]
    fallback_tools = ["piper_tts", "openai_tts", "elevenlabs_tts"]
    # Unset: TTS is not live on the test gateway (dev-guide §6.2).

    input_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string"},
            "voice": {"type": "string", "description": "Speaker/voice id. Maps to params.speaker."},
            "format": {
                "type": "string",
                "default": "mp3",
                "enum": ["mp3", "wav", "ogg_opus", "pcm"],
                "description": "Output audio format.",
            },
            "speed": {
                "type": "number",
                "minimum": -50,
                "maximum": 100,
                "description": "Speech rate (params.speechRate, -50..100).",
            },
            "model_name": {"type": "string", "description": "Explicit dofe TTS alias. Overrides DOFE_TTS_MODEL."},
            "task_id": {"type": "string", "description": "Resume polling an earlier timed-out dofe task."},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=50, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["text", "voice", "format", "speed", "model_name"]
    side_effects = ["paid remote generation via models.dofe.ai gateway", "writes audio file to output_path"]
    user_visible_verification = ["Listen to generated audio for intelligibility and tone"]

    # ------------------------------------------------------------------ cost

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return round(len(inputs.get("text", "")) * 0.000015, 4)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 20.0

    # ------------------------------------------------------------------ model

    def resolve_model(self, inputs: dict[str, Any]) -> str | None:
        return resolve_alias("tts", "generate", explicit=inputs.get("model_name"))

    # ---------------------------------------------------------------- payload

    def _build_payload(self, inputs: dict[str, Any], model: str) -> dict[str, Any]:
        text = str(inputs.get("text") or "").strip()
        if not text:
            raise ValueError("text is required")

        # Text block never carries a role (dev-guide §2.3).
        content: list[dict[str, Any]] = [{"part": {"type": "text", "text": text}, "order": 0}]

        params: dict[str, Any] = {"format": inputs.get("format", "mp3")}
        if inputs.get("voice"):
            params["speaker"] = inputs["voice"]
        if inputs.get("speed") is not None:
            params["speechRate"] = inputs["speed"]

        payload: dict[str, Any] = {
            "model": model,
            "endpointKind": "speech_synthesis",
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
