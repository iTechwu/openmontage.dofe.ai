"""HTTP client for the models.dofe.ai gateway (dev-guide §4, §6.1).

Implements the unified task protocol: create → (poll) → artifacts → download,
with the retry / poll / error-classification policy from dev-guide §6.1.
Mirrors the shape of :mod:`tools._kling.client` (session reuse, structured
errors, injectable session) but targets the dofe ``{code,msg,data}`` envelope.
"""

from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep
from typing import Any

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover - requirements.txt installs it in production.
    pass

import requests

from . import config as cfg
from .errors import (
    NON_RETRYABLE_502_REASONS,
    RATE_LIMIT_CODE,
    RETRYABLE_5XX,
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
from .media import is_https_url, sanitize_for_log
from .delegation import delegated_credential_from_environment

# Terminal task statuses — polling stops here.
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "expired"}
SUCCESS_STATUS = "succeeded"

_5XX_BACKOFF = (0.5, 1.0, 2.0, 4.0)
_RATE_DEFAULT_WAIT = 5.0
_RATE_MAX_WAIT = 60.0
_NETWORK_BACKOFF = (1.0, 2.0)

_TRACE_HEADERS = ("x-trace-id", "x-request-id", "trace-id", "traceid", "request-id")


class DofeClient:
    """Synchronous client for the dofe gateway task API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        session: Any | None = None,
        *,
        connect_timeout: int | None = None,
        read_timeout: int | None = None,
        create_read_timeout: int | None = None,
        poll_interval: float | None = None,
        max_rate_retries: int = 3,
        max_5xx_retries: int = 3,
        max_network_retries: int = 2,
    ) -> None:
        self.api_key = api_key if api_key is not None else cfg.dofe_api_key()
        self.base_url = (base_url or cfg.dofe_base_url()).rstrip("/")
        try:
            self.delegation = (
                delegated_credential_from_environment(
                    api_key=self.api_key,
                    models_base_url=self.base_url,
                )
                if self.api_key
                else None
            )
        except ValueError as exc:
            raise DofeAuthError(str(exc)) from exc
        self.session = session if session is not None else requests.Session()
        if session is None:
            self.session.verify = cfg.dofe_ca_bundle()
        self._connect_timeout = connect_timeout if connect_timeout is not None else cfg.connect_timeout()
        self._read_timeout = read_timeout if read_timeout is not None else cfg.read_timeout()
        self._create_read_timeout = create_read_timeout if create_read_timeout is not None else cfg.create_read_timeout()
        self._poll_interval = poll_interval if poll_interval is not None else cfg.poll_interval()
        self._max_rate_retries = max_rate_retries
        self._max_5xx_retries = max_5xx_retries
        self._max_network_retries = max_network_retries

    # ------------------------------------------------------------------ headers

    def _headers(
        self,
        *,
        model_invocation_id: str | None = None,
        logical_call_id: str | None = None,
    ) -> dict[str, str]:
        if not self.api_key:
            raise DofeAuthError("DOFE_MODEL_API_KEY / DOFE_API_KEY is not set.")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if logical_call_id:
            headers["X-OpenMontage-Logical-Call-Id"] = logical_call_id
        if self.delegation is not None:
            headers.update(
                self.delegation.signed_headers(model_invocation_id=model_invocation_id)
            )
        return headers

    # -------------------------------------------------------------------- URLs

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}{path}"

    # --------------------------------------------------------------- envelope

    @staticmethod
    def _safe_json(response: Any) -> Any:
        try:
            return response.json()
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _body_ok(body: Any) -> bool:
        if not isinstance(body, dict):
            return False
        return str(body.get("code")) in {"0", "200"}

    @staticmethod
    def _unwrap(body: Any) -> dict[str, Any] | None:
        if isinstance(body, dict):
            data = body.get("data")
            return data if isinstance(data, dict) else None
        return None

    @staticmethod
    def _extract_retry_after(details: Any, headers: Any) -> float | None:
        if isinstance(details, dict) and details.get("retryAfter") is not None:
            try:
                return max(0.0, float(details["retryAfter"]))
            except (TypeError, ValueError):
                pass
        getter = getattr(headers, "get", None)
        if callable(getter):
            for header in ("Retry-After", "retry-after"):
                raw = getter(header)
                if raw:
                    try:
                        return max(0.0, float(raw))
                    except (TypeError, ValueError):
                        return None
        return None

    @staticmethod
    def _extract_trace_id(body: Any, headers: Any) -> str | None:
        getter = getattr(headers, "get", None)
        if callable(getter):
            for header in _TRACE_HEADERS:
                value = getter(header)
                if value:
                    return str(value)
        if isinstance(body, dict):
            for key in ("traceId", "trace_id", "requestId", "request_id"):
                if body.get(key):
                    return str(body[key])
        return None

    def _raise_for_status(self, status: int, body: Any, headers: Any) -> None:
        """Raise the appropriate :class:`DofeError` subclass for a non-OK response."""

        if 200 <= status < 300 and self._body_ok(body):
            return

        is_dict = isinstance(body, dict)
        biz_code = body.get("code") if is_dict else None
        msg = (body.get("msg") or body.get("message")) if is_dict else None
        msg = msg or f"HTTP {status}"
        err_block = body.get("error") if is_dict else None
        details = err_block.get("details") if isinstance(err_block, dict) else None
        err_code = err_block.get("code") if isinstance(err_block, dict) else None
        trace_id = self._extract_trace_id(body, headers)
        code_for_error = biz_code if biz_code is not None else err_code
        retry_after = self._extract_retry_after(details, headers)

        kwargs: dict[str, Any] = {
            "code": code_for_error,
            "http_status": status,
            "details": details,
            "trace_id": trace_id,
        }

        if status == 429 or str(biz_code) == RATE_LIMIT_CODE:
            raise DofeRateLimitError(msg, retry_after=retry_after, **kwargs)
        if status in (401, 403):
            raise DofeAuthError(msg, **kwargs)
        if status == 402:
            raise DofeQuotaError(msg, **kwargs)
        if status == 404:
            raise DofeModelUnavailableError(msg, **kwargs)
        raise DofeAPIError(msg, **kwargs)

    # ---------------------------------------------------------------- request

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        read_timeout: int | None = None,
        allow_retry: bool = True,
        accept_raw_success: bool = False,
        logical_call_id: str | None = None,
    ) -> dict[str, Any]:
        url = self._url(path)
        timeout = (self._connect_timeout, read_timeout or self._read_timeout)
        rate_attempts = 0
        server_attempts = 0
        network_attempts = 0
        request_headers = self._headers(logical_call_id=logical_call_id)

        while True:
            try:
                response = self.session.request(
                    method, url, headers=request_headers, timeout=timeout, json=json, params=params,
                )
            except requests.RequestException as exc:
                if allow_retry and network_attempts < self._max_network_retries:
                    network_attempts += 1
                    sleep(_NETWORK_BACKOFF[min(network_attempts - 1, len(_NETWORK_BACKOFF) - 1)])
                    continue
                raise DofeNetworkError(f"dofe {method} {path} failed: {exc}") from exc

            status = response.status_code
            body = self._safe_json(response)
            try:
                if not (accept_raw_success and 200 <= status < 300 and isinstance(body, dict)):
                    self._raise_for_status(status, body, response.headers)
            except DofeRateLimitError as exc:
                if allow_retry and rate_attempts < self._max_rate_retries:
                    rate_attempts += 1
                    wait = exc.retry_after if exc.retry_after is not None else _RATE_DEFAULT_WAIT
                    sleep(min(wait, _RATE_MAX_WAIT))
                    continue
                raise
            except DofeAPIError as exc:
                if allow_retry and self._is_5xx_retryable(exc):
                    if server_attempts < self._max_5xx_retries:
                        server_attempts += 1
                        sleep(_5XX_BACKOFF[min(server_attempts - 1, len(_5XX_BACKOFF) - 1)])
                        continue
                raise
            return body if isinstance(body, dict) else {}

    @staticmethod
    def _is_5xx_retryable(exc: DofeAPIError) -> bool:
        if exc.http_status not in RETRYABLE_5XX:
            return False
        if exc.http_status == 502:
            reason = (exc.details or {}).get("reason")
            if reason in NON_RETRYABLE_502_REASONS:
                return False
        return True

    # ----------------------------------------------------------- task methods

    def list_models(self) -> list[dict[str, Any]]:
        """Return aliases visible to the configured tenant API key."""

        body = self._request("get", "/v1/models", accept_raw_success=True)
        models = body.get("data") if isinstance(body, dict) else None
        if not isinstance(models, list):
            raise DofeAPIError(
                f"dofe model-list response missing data list: {sanitize_for_log(body)}"
            )
        return [item for item in models if isinstance(item, dict)]

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/generation/tasks. Not retried (avoid double charge)."""

        metadata = payload.get("metadata")
        logical_call_id = (
            str(metadata.get("openmontage_idempotency_key") or "").strip()
            if isinstance(metadata, dict)
            else ""
        )
        body = self._request(
            "post",
            "/v1/generation/tasks",
            json=payload,
            read_timeout=self._create_read_timeout,
            allow_retry=False,
            logical_call_id=logical_call_id or None,
        )
        data = self._unwrap(body)
        if not data or not data.get("taskId"):
            raise DofeAPIError(
                f"dofe create-task response missing data.taskId: {sanitize_for_log(body)}"
            )
        return data

    def get_task(self, task_id: str) -> dict[str, Any]:
        body = self._request("get", f"/v1/generation/tasks/{task_id}")
        return self._unwrap(body) or {}

    def get_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        body = self._request("get", f"/v1/generation/tasks/{task_id}/artifacts")
        data = self._unwrap(body) or {}
        assets = data.get("assets")
        if not isinstance(assets, list):
            raise DofeAPIError(
                f"dofe artifacts response missing assets list: {sanitize_for_log(body)}"
            )
        return assets

    def cancel_task(self, task_id: str) -> None:
        """Best-effort cancel; any error (incl. 409 already-terminal) is ignored."""

        try:
            self._request("post", f"/v1/generation/tasks/{task_id}/cancel")
        except DofeError:
            # Cleanup path — never let a cancel failure mask the original outcome.
            pass

    def wait_for_terminal(
        self,
        task_id: str,
        *,
        timeout_seconds: int,
        poll_interval: float | None = None,
    ) -> dict[str, Any]:
        """Poll ``get_task`` until a terminal status or the deadline.

        On timeout the task is cancelled (best-effort) before raising
        :class:`DofeTaskTimeoutError`. Interval backs off from ``poll_interval``
        up to 30s (dev-guide §6.1).
        """

        deadline = monotonic() + max(0, timeout_seconds)
        interval = max(1.0, float(poll_interval if poll_interval is not None else self._poll_interval))
        while True:
            data = self.get_task(task_id)
            status = str(data.get("status") or "").lower()
            if status in TERMINAL_STATUSES:
                return data
            if monotonic() >= deadline:
                self.cancel_task(task_id)
                raise DofeTaskTimeoutError(
                    f"dofe task {task_id} did not finish within {timeout_seconds}s; cancel attempted",
                    task_id=task_id,
                )
            remaining = deadline - monotonic()
            sleep(min(interval, max(0.0, remaining)))
            interval = min(interval * 1.5, 30.0)

    def submit_and_collect(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: int,
        poll_interval: float | None = None,
        asset_kind: str | None = None,
        existing_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Create (or resume) a task, drive it to terminal, fetch artifacts.

        Returns task output plus native-currency billing metadata. The
        create response may already be terminal with ``outputAssets`` (image_async
        blocks to completion); otherwise we poll then fetch artifacts.
        """

        if existing_task_id:
            data = {"taskId": existing_task_id, "status": "running"}
            task_id = existing_task_id
        else:
            data = self.create_task(payload)
            task_id = str(data.get("taskId"))

        status = str(data.get("status") or "").lower()
        assets = data.get("outputAssets") if isinstance(data.get("outputAssets"), list) else None
        estimated_cost = data.get("estimatedCost")
        final_cost = data.get("finalCost")
        awaited_terminal = status not in TERMINAL_STATUSES

        if awaited_terminal:
            data = self.wait_for_terminal(
                task_id, timeout_seconds=timeout_seconds, poll_interval=poll_interval,
            )
            status = str(data.get("status") or "").lower()
            if isinstance(data.get("outputAssets"), list):
                assets = data["outputAssets"]
            estimated_cost = data.get("estimatedCost") or estimated_cost
            final_cost = data.get("finalCost")

        # The gateway settles billing synchronously after persisting the terminal
        # provider status. Its first terminal response can therefore contain the
        # estimate but not the just-persisted final cost. Refresh once so callers
        # report the settled amount instead of permanently labeling it an estimate.
        if (
            awaited_terminal
            and status == SUCCESS_STATUS
            and final_cost is None
            and estimated_cost is not None
        ):
            settled = self.get_task(task_id)
            if str(settled.get("status") or "").lower() == SUCCESS_STATUS:
                data = settled
                final_cost = settled.get("finalCost")
                estimated_cost = settled.get("estimatedCost") or estimated_cost
                if isinstance(settled.get("outputAssets"), list):
                    assets = settled["outputAssets"]

        if status == SUCCESS_STATUS:
            if not assets:
                assets = self.get_artifacts(task_id)
            return {
                "task_id": task_id,
                "status": status,
                "assets": assets or [],
                "text": data.get("text"),
                "estimated_cost": estimated_cost,
                "final_cost": final_cost,
                "cost_currency": data.get("costCurrency"),
                "pricing_breakdown": data.get("pricingBreakdown"),
            }

        raise DofeTaskFailedError(
            f"dofe task {task_id} ended in status {status!r}",
            task_id=task_id,
            error_code=str(data.get("errorCode") or data.get("code") or "") or None,
            error_message=str(data.get("errorMessage") or data.get("msg") or "") or None,
        )

    # ----------------------------------------------------------------- download

    def download(self, url: str, output_path: str | Path, *, timeout: int | None = None) -> Path:
        """Download a presigned artifact URL to ``output_path`` (https only)."""

        if not is_https_url(url):
            raise DofeError(f"dofe artifact URL must be https: {sanitize_for_log(url)}")
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            response = self.session.get(url, timeout=timeout or self._read_timeout)
        except requests.RequestException as exc:
            raise DofeNetworkError(f"dofe artifact download failed: {exc}") from exc
        status = getattr(response, "status_code", 0)
        if not (200 <= int(status) < 300):
            raise DofeError(f"dofe artifact download failed (HTTP {status})")
        content = getattr(response, "content", None) or b""
        destination.write_bytes(content)
        return destination
