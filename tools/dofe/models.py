"""Model selection backed by the tenant-visible DoFe gateway catalog.

Model selection is a strict two-layer cascade with no built-in defaults:

1. Explicit ``model_name`` passed by the caller (highest).
2. ``.env`` override — per-operation video env first, then the capability env.

The selected candidate is not usable until :func:`validate_catalog_alias`
confirms that the exact ID was returned by ``GET /v1/models`` for the current
tenant key. OpenMontage never manufactures or normalizes model names.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any

from .errors import DofeModelUnavailableError

# Capability-level env var that selects an ID from the gateway catalog.
CAPABILITY_ENV = {
    "video": "DOFE_VIDEO_MODEL",
    "image": "DOFE_IMAGE_MODEL",
    "tts": "DOFE_TTS_MODEL",
    "music": "DOFE_MUSIC_MODEL",
    "avatar": "DOFE_AVATAR_MODEL",
    "stt": "DOFE_STT_MODEL",
}

# Optional per-operation selectors for video (only the first set value is used;
# there is no preference chain or guessed fallback).
VIDEO_OPERATION_ENV = {
    "text_to_video": "DOFE_MODEL_TEXT_TO_VIDEO",
    "image_to_video": "DOFE_MODEL_IMAGE_TO_VIDEO",
    "reference_to_video": "DOFE_MODEL_REFERENCE_TO_VIDEO",
}

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

    return None


def catalog_model_ids(models: Iterable[Any]) -> tuple[str, ...]:
    """Extract exact, non-empty model IDs from a ``GET /v1/models`` response."""

    ids: list[str] = []
    seen: set[str] = set()
    for item in models:
        if not isinstance(item, Mapping):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        seen.add(model_id)
        ids.append(model_id)
    return tuple(ids)


def validate_catalog_alias(alias: str, models: Iterable[Any]) -> str:
    """Return ``alias`` only when the tenant catalog contains that exact ID."""

    visible = catalog_model_ids(models)
    if alias not in visible:
        raise DofeModelUnavailableError(
            f"Model {alias!r} was not returned by GET /v1/models for this tenant key",
            http_status=404,
            details={"catalog_model_count": len(visible)},
        )
    return alias


def config_env_name(capability: str) -> str | None:
    """The .env var a user sets to select this capability's catalog ID."""

    return CAPABILITY_ENV.get(capability)
