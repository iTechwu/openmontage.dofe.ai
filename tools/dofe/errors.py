"""Error types and retry policy for the models.dofe.ai gateway.

The dofe gateway returns a uniform envelope::

    {"code": 200, "msg": "ok", "data": {...}}                 # success
    {"code": ..., "msg": "...", "data": null,                 # error
     "error": {"code": "...", "details": {reason, recommendedAction,
                                          field, value, allowed}}}

Rate limiting surfaces as HTTP 429 or business code ``925429`` (with a
``retryAfter`` hint). See dev-guide §6.1 for the full classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Business code the gateway returns when rate limited.
RATE_LIMIT_CODE = "925429"

# HTTP statuses that never qualify for a retry — they are deterministic
# client/config errors, so retrying only burns time (and, for create-task,
# risks a double charge).
NON_RETRYABLE_HTTP = {400, 401, 402, 403, 404, 409}

# Transient server-side failures that are safe to retry with backoff.
RETRYABLE_5XX = {500, 502, 503, 504}

# reasons that, on a 502, mark the error as logical (param/billing) rather
# than transient — must NOT be retried (dev-guide §6.1).
NON_RETRYABLE_502_REASONS = {"param_unsupported", "param_price_not_found"}


def _code_str(code: Any) -> str | None:
    return None if code is None else str(code)


@dataclass
class DofeError(Exception):
    """Base class for all dofe gateway errors.

    ``message`` is safe to surface to the user; structured fields are kept for
    diagnostics. The API key is never stored here.
    """

    message: str
    code: str | int | None = None
    http_status: int | None = None
    details: dict[str, Any] | None = None
    trace_id: str | None = None

    def __str__(self) -> str:
        parts = [self.message]
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.http_status is not None:
            parts.append(f"http={self.http_status}")
        if self.trace_id:
            parts.append(f"trace={self.trace_id}")
        return " | ".join(parts)


@dataclass
class DofeAPIError(DofeError):
    """A non-2xx response, or a 2xx whose business ``code`` is not OK."""


@dataclass
class DofeAuthError(DofeAPIError):
    """HTTP 401/403 — the configured DoFe model API key is invalid."""


@dataclass
class DofeQuotaError(DofeAPIError):
    """HTTP 402 — gateway balance / billing not configured."""


@dataclass
class DofeModelUnavailableError(DofeAPIError):
    """HTTP 404 — alias does not exist or is not visible to this key."""


@dataclass
class DofeRateLimitError(DofeAPIError):
    """HTTP 429 or business code 925429. Carries a ``retry_after`` in seconds."""

    retry_after: float | None = None


@dataclass
class DofeNetworkError(DofeError):
    """A transport-level failure (DNS, connection, timeout)."""


@dataclass
class DofeTaskFailedError(DofeError):
    """A task reached a ``failed``/``cancelled``/``expired`` terminal state."""

    task_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class DofeTaskTimeoutError(DofeError):
    """Polling exceeded the deadline. The task was already cancelled."""

    task_id: str | None = None


def is_retryable_dofe_error(error: DofeError) -> bool:
    """Whether a single failed attempt is safe to retry per dev-guide §6.1.

    Rate-limit and 5xx errors retry (with the 502 logical-reason exception);
    every other classified error is terminal for retry purposes.
    """

    if isinstance(error, DofeRateLimitError):
        return True
    if isinstance(error, DofeAPIError) and error.http_status in RETRYABLE_5XX:
        reason = (error.details or {}).get("reason")
        if error.http_status == 502 and reason in NON_RETRYABLE_502_REASONS:
            return False
        return True
    return False
