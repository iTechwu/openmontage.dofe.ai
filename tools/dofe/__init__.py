"""Shared package for the models.dofe.ai gateway provider family.

Public surface used by the six ``dofe_*`` tools and the three selectors:

- :class:`DofeClient` — HTTP/task client (auth, retry, poll, download).
- :class:`DofeToolSpec`, :func:`run_dofe_generation` — shared execute body.
- :func:`select_dofe_if_enabled`, :func:`is_dofe_enabled` — the DOFE_ENABLED switch.
- The :class:`DofeError` hierarchy for typed error handling.
"""

from __future__ import annotations

from . import config
from .client import DofeClient
from .config import DofeRoutingError, is_dofe_enabled, select_dofe_if_enabled
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
from .media import file_to_data_uri, is_https_url, resolve_image_source, sanitize_for_log
from .media_upload import DofeMediaUploadClient, DofeMediaUploadError
from .models import catalog_model_ids, resolve_alias, validate_catalog_alias
from .pricing import DofePricingClient, DofePricingError
from .runtime import DofeToolSpec, probe_audio, probe_image, probe_video, run_dofe_generation

__all__ = [
    "config",
    "DofeClient",
    "DofeToolSpec",
    "DofeError",
    "DofeRoutingError",
    "DofePricingClient",
    "DofePricingError",
    "DofeMediaUploadClient",
    "DofeMediaUploadError",
    "DofeAPIError",
    "DofeAuthError",
    "DofeQuotaError",
    "DofeModelUnavailableError",
    "DofeRateLimitError",
    "DofeNetworkError",
    "DofeTaskFailedError",
    "DofeTaskTimeoutError",
    "file_to_data_uri",
    "is_dofe_enabled",
    "is_https_url",
    "probe_audio",
    "probe_image",
    "probe_video",
    "catalog_model_ids",
    "resolve_alias",
    "resolve_image_source",
    "run_dofe_generation",
    "sanitize_for_log",
    "select_dofe_if_enabled",
    "validate_catalog_alias",
]
