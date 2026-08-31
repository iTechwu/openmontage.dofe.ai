"""Environment configuration and the single ``DOFE_ENABLED`` selector switch.

All dofe behavior keys off one switch (dev-guide §3.1)::

    DOFE_ENABLED=true + DOFE_MODEL_API_KEY set → route through dofe
    DOFE_ENABLED=true + dofe unavailable       → fail closed (no direct fallback)
    DOFE_ENABLED=false                         → existing provider chain

``select_dofe_if_enabled`` is the shared wiring the three capability selectors
call — keeps each selector's change to a couple of lines and identical behavior.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.base_tool import BaseTool

DEFAULT_BASE_URL = "https://ixicai.cn/api"

DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_READ_TIMEOUT = 30
DEFAULT_CREATE_READ_TIMEOUT = 300  # image_async blocks ~13s; widen generously.

DEFAULT_POLL_INTERVAL = 5
DEFAULT_POLL_MAX_VIDEO = 1800
DEFAULT_POLL_MAX_IMAGE = 600
DEFAULT_POLL_MAX_TTS = 300
DEFAULT_POLL_MAX_MUSIC = 900
DEFAULT_POLL_MAX_STT = 900

_POLL_MAX_BY_CAPABILITY = {
    "video": ("DOFE_POLL_MAX_VIDEO", DEFAULT_POLL_MAX_VIDEO),
    "image": ("DOFE_POLL_MAX_IMAGE", DEFAULT_POLL_MAX_IMAGE),
    "tts": ("DOFE_POLL_MAX_TTS", DEFAULT_POLL_MAX_TTS),
    "music": ("DOFE_POLL_MAX_MUSIC", DEFAULT_POLL_MAX_MUSIC),
    "stt": ("DOFE_POLL_MAX_STT", DEFAULT_POLL_MAX_STT),
    # avatar reuses the video budget (digital_human is video-class output).
    "avatar": ("DOFE_POLL_MAX_VIDEO", DEFAULT_POLL_MAX_VIDEO),
}

_TRUTHY = {"true", "1", "yes"}


class DofeRoutingError(RuntimeError):
    """Raised when strict Airouter routing is enabled but unavailable."""


_MODEL_API_CAPABILITIES = {
    "analysis",
    "avatar",
    "image_generation",
    "music_generation",
    "tts",
    "video_generation",
}
_MODEL_API_RUNTIMES = {"api", "hybrid"}


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    try:
        return max(0, int(float(raw.strip())))
    except (TypeError, ValueError):
        return default


def is_dofe_enabled() -> bool:
    """True when the ``DOFE_ENABLED`` master switch is on."""

    return _env_bool("DOFE_ENABLED")


def model_api_policy_error(tool: "BaseTool") -> str | None:
    """Return the DoFe-only policy error for a direct model API tool."""

    if not is_dofe_enabled():
        return None
    provider = str(getattr(tool, "provider", ""))
    if provider in {"dofe", "selector"}:
        return None
    capability = str(getattr(tool, "capability", ""))
    runtime = getattr(getattr(tool, "runtime", ""), "value", getattr(tool, "runtime", ""))
    tier = getattr(getattr(tool, "tier", ""), "value", getattr(tool, "tier", ""))
    # Stock-media tools historically share the image/video generation
    # capability so selectors can consider them, but they do not invoke a
    # model. Keep those source APIs available under the model-routing policy.
    if tier == "source":
        return None
    if capability not in _MODEL_API_CAPABILITIES or runtime not in _MODEL_API_RUNTIMES:
        return None
    return (
        f"DOFE_ENABLED=true: direct model API tool {getattr(tool, 'name', '')!r} "
        f"({provider or 'unknown'}) is disabled; use the DoFe Models provider"
    )


def model_api_tool_allowed(tool: "BaseTool") -> bool:
    """Whether a tool may be discovered under the current model API policy."""

    return model_api_policy_error(tool) is None


def dofe_api_key() -> str | None:
    raw = os.environ.get("DOFE_MODEL_API_KEY") or os.environ.get("DOFE_API_KEY")
    return raw.strip() if raw and raw.strip() else None


def dofe_base_url() -> str:
    raw = (
        os.environ.get("DOFE_MODEL_BASE_URL")
        or os.environ.get("DOFE_BASE_URL")
        or DEFAULT_BASE_URL
    )
    return raw.strip().rstrip("/")


def dofe_internal_base_url() -> str:
    """Base URL for HMAC-authenticated Airouter service endpoints."""

    raw = (
        os.environ.get("DOFE_INTERNAL_API_BASE_URL")
        or os.environ.get("DOFE_INTERNAL_BASE_URL")
        or ""
    )
    if raw.strip():
        return raw.strip().rstrip("/")
    base = dofe_base_url()
    return base[:-4] if base.endswith("/api") else base


def dofe_internal_api_secret() -> str | None:
    raw = os.environ.get("INTERNAL_API_SECRET")
    return raw.strip() if raw and raw.strip() else None


def dofe_tenant_id() -> str | None:
    raw = os.environ.get("DOFE_TENANT_ID")
    return raw.strip() if raw and raw.strip() else None


def dofe_ca_bundle() -> str | bool:
    """Return an optional enterprise CA bundle path without disabling TLS checks."""

    raw = os.environ.get("DOFE_CA_BUNDLE", "").strip()
    if not raw:
        return True
    path = Path(raw).expanduser()
    if not path.is_file():
        raise DofeRoutingError(f"DOFE_CA_BUNDLE does not point to a readable file: {path}")
    return str(path)


def connect_timeout() -> int:
    return _env_int("DOFE_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT)


def read_timeout() -> int:
    return _env_int("DOFE_READ_TIMEOUT", DEFAULT_READ_TIMEOUT)


def create_read_timeout() -> int:
    return _env_int("DOFE_CREATE_READ_TIMEOUT", DEFAULT_CREATE_READ_TIMEOUT)


def poll_interval() -> float:
    return float(_env_int("DOFE_POLL_INTERVAL", DEFAULT_POLL_INTERVAL))


def poll_max_seconds(capability: str) -> int:
    env_name, default = _POLL_MAX_BY_CAPABILITY.get(
        capability, ("DOFE_POLL_MAX_IMAGE", DEFAULT_POLL_MAX_IMAGE)
    )
    return _env_int(env_name, default)


def select_dofe_if_enabled(candidates: list["BaseTool"], name: str) -> "BaseTool | None":
    """Shared selector wiring for the ``DOFE_ENABLED`` switch.

    Returns the named dofe tool when the switch is on and the tool is AVAILABLE.
    When the switch is on but the route is unavailable, fail closed so no model
    call can silently bypass the unified Airouter.
    """

    # Local import avoids a circular import at module load (base_tool ← dofe).
    from tools.base_tool import ToolStatus

    if not is_dofe_enabled():
        return None
    dofe = next((tool for tool in candidates if tool.name == name), None)
    if dofe is None:
        # Direct unit-level selector calls may supply a partial candidate list.
        # Real registry discovery always includes the DoFe provider tools.
        return None
    if dofe.get_status() == ToolStatus.AVAILABLE:
        return dofe
    raise DofeRoutingError(
        f"DOFE_ENABLED=true but {name} is unavailable; direct-provider fallback is disabled"
    )
