"""Model alias resolution for the dofe gateway (dev-guide §3.2/§3.3).

Model selection is a strict three-layer cascade — no preference chains, no
smart filtering, no auto-fallback:

1. Explicit ``model_name`` passed by the caller (highest).
2. ``.env`` override — per-operation video env first, then the capability env.
3. Built-in default from :data:`DEFAULT_ALIASES`.

Aliases are operational data on the gateway and must match **exactly**
(``seedance-2.0-fast`` != ``seedance-2-0-fast``); this module never normalizes them.
"""

from __future__ import annotations

import os
from typing import Any

# Capability-level env var that overrides the default alias for that family.
CAPABILITY_ENV = {
    "video": "DOFE_VIDEO_MODEL",
    "image": "DOFE_IMAGE_MODEL",
    "tts": "DOFE_TTS_MODEL",
    "music": "DOFE_MUSIC_MODEL",
    "avatar": "DOFE_AVATAR_MODEL",
    "stt": "DOFE_STT_MODEL",
}

# Optional per-operation overrides for video (only the first set value is used;
# there is no preference chain). All video operations default to seedance-2.0-fast.
VIDEO_OPERATION_ENV = {
    "text_to_video": "DOFE_MODEL_TEXT_TO_VIDEO",
    "image_to_video": "DOFE_MODEL_IMAGE_TO_VIDEO",
    "reference_to_video": "DOFE_MODEL_REFERENCE_TO_VIDEO",
}

# Built-in defaults. None means "no default until the gateway has the model" —
# the tool reports a clear configuration error rather than guessing.
DEFAULT_ALIASES: dict[tuple[str, str], str | None] = {
    ("video", "text_to_video"): "seedance-2.0-fast",
    ("video", "image_to_video"): "seedance-2.0-fast",
    ("video", "reference_to_video"): "seedance-2.0-fast",
    ("image", "generate"): "seedream-5.0",
    ("tts", "generate"): None,
    ("music", "generate"): None,
    ("avatar", "generate"): None,
    ("stt", "transcribe"): "openspeech-auc",
}

# Canonical default the test environment actually serves (used by tests).
VIDEO_DEFAULT_ALIAS = "seedance-2.0-fast"
IMAGE_DEFAULT_ALIAS = "seedream-5.0"
TTS_DEFAULT_ALIAS = None
STT_DEFAULT_ALIAS = "openspeech-auc"


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def resolve_alias(
    capability: str,
    operation: str | None = None,
    *,
    explicit: str | None = None,
) -> str | None:
    """Resolve the dofe model alias for a capability/operation.

    Returns ``None`` when nothing is configured — callers must surface a clear
    "set DOFE_*_MODEL" error rather than sending a request without a model.
    """

    if explicit and str(explicit).strip():
        return str(explicit).strip()

    if capability == "video":
        op = operation or "text_to_video"
        per_op = VIDEO_OPERATION_ENV.get(op)
        if per_op:
            value = _first_env(per_op)
            if value:
                return value

    cap_env = CAPABILITY_ENV.get(capability)
    if cap_env:
        value = _first_env(cap_env)
        if value:
            return value

    return DEFAULT_ALIASES.get((capability, operation or "generate"))


def config_env_name(capability: str) -> str | None:
    """The .env var a user sets to change this capability's default alias."""

    return CAPABILITY_ENV.get(capability)
