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
    ToolStatus,
    ToolTier,
)
from tools.dofe import DofeToolSpec, probe_audio
from tools.dofe.models import resolve_alias
from tools.dofe.runtime import build_metadata, run_dofe_generation
from tools.dofe.status import configured_model_is_visible


# MiniMax 音色清单（生成对话/narration 时从此列表选；id 中的空格是 MiniMax 原始 id 的一部分，必须保留）。
MINIMAX_VOICES = {
    "male": [
        ("Chinese_bazong",                            "温柔霸总 - 低沉,成熟,深情"),
        ("Chinese_worker_male",                       "班味男社畜 - 沉稳,职场,疲惫"),
        ("hunyin_6",                                  "舒朗男声 - 清亮,干脆利落,意气风发"),
        ("Chinese (Mandarin)_Unrestrained_Young_Man", "不羁青年 - 低沉磁性,慵懒随意,霸道"),
        ("Chinese (Mandarin)_Stubborn_Friend",        "嘴硬竹马 - 清朗温暖,自然随性,邻家大男孩"),
        ("Chinese_radient_storyteller_nv1",           "说书爷爷 - 沙哑,鼻音,引人入胜"),
        ("Chinese_weather_forecaster_nv1",            "天气播报员 - 沙哑,有质感,节奏感"),
        ("Chinese (Mandarin)_Pure-hearted_Boy",       "清澈邻家弟弟 - 清澈干净,娓娓道来,邻家男孩感"),
    ],
    "female": [
        ("Chinese_huolishaonv",          "元气少女 - 甜美,活泼,娇俏"),
        ("Chinese_worker_female",        "班味女社畜 - 疲惫,生动,紧张"),
        ("Chinese_cixianglaoren",        "亲切奶奶 - 温厚,亲切,沧桑"),
        ("Chinese (Mandarin)_Mature_Woman", "傲娇御姐 - 低沉沙哑,慵懒舒缓,性感撩人"),
        ("Arrogant_Miss",                "嚣张小姐 - 娇俏明亮,灵动跳跃,傲娇自信"),
        ("Chinese_sweet_girl_nv1",       "甜美少女 - 清脆,富有表现力,朝气蓬勃"),
        ("Chinese_crisp_podcaster_nv1",  "清爽女声 - 清晰,对话感,深思"),
    ],
}

# 缺省旁白音色（voice 未指定时兜底）
DEFAULT_VOICE = "hunyin_6"

# 合法音色 id 集合（非法值兜底默认，避免打到上游报 business_error）
VALID_VOICE_IDS = frozenset(vid for group in MINIMAX_VOICES.values() for vid, _ in group)


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
        "Read GET /v1/models and set DOFE_TTS_MODEL to one returned model ID."
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
            "voice": {
                "type": "string",
                "description": (
                    "MiniMax 音色 id，从内置清单选："
                    "男声 " + "；".join(f"{k}（{v}）" for k, v in MINIMAX_VOICES["male"])
                    + "；女声 " + "；".join(f"{k}（{v}）" for k, v in MINIMAX_VOICES["female"])
                    + f"。缺省用 {DEFAULT_VOICE}。"
                ),
            },
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
            "model_name": {"type": "string", "description": "Exact ID from GET /v1/models. Overrides DOFE_TTS_MODEL."},
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

    def get_status(self) -> ToolStatus:
        status = super().get_status()
        if status == ToolStatus.UNAVAILABLE:
            return status
        return (
            ToolStatus.AVAILABLE
            if configured_model_is_visible("tts", ("generate",))
            else ToolStatus.UNAVAILABLE
        )

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
        voice = inputs.get("voice") or inputs.get("voice_id") or DEFAULT_VOICE
        if voice not in VALID_VOICE_IDS:
            voice = DEFAULT_VOICE
        params["speaker"] = voice
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
