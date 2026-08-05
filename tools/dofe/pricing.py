"""Tenant-effective pricing quotes from the Airouter internal API."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import requests

from . import config as cfg


class DofePricingError(RuntimeError):
    """Raised when an authoritative Airouter pricing quote is unavailable."""


class DofePricingClient:
    """Small HMAC-authenticated client for ``POST /internal/pricing/quote``."""

    def __init__(
        self,
        *,
        secret: str | None = None,
        tenant_id: str | None = None,
        base_url: str | None = None,
        session: Any | None = None,
    ) -> None:
        self.secret = secret if secret is not None else cfg.dofe_internal_api_secret()
        self.tenant_id = tenant_id if tenant_id is not None else cfg.dofe_tenant_id()
        self.base_url = (base_url or cfg.dofe_internal_base_url()).rstrip("/")
        self.session = session if session is not None else requests.Session()
        if session is None:
            self.session.verify = cfg.dofe_ca_bundle()

    def _headers(self) -> dict[str, str]:
        if not self.secret:
            raise DofePricingError("INTERNAL_API_SECRET is not configured for Airouter pricing")
        timestamp = str(int(time.time()))
        service_name = "openmontage"
        signature = hmac.new(
            self.secret.encode("utf-8"),
            f"{timestamp}:{service_name}".encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Authorization": f"Bearer {timestamp}:{signature}:{service_name}",
            "x-service-name": service_name,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.tenant_id:
            raise DofePricingError("DOFE_TENANT_ID is not configured for tenant pricing")
        payload = {**request, "tenantId": self.tenant_id}
        try:
            response = self.session.post(
                f"{self.base_url}/internal/pricing/quote",
                headers=self._headers(),
                json=payload,
                timeout=(cfg.connect_timeout(), cfg.read_timeout()),
            )
        except requests.RequestException as exc:
            raise DofePricingError(f"Airouter pricing request failed: {exc}") from exc

        try:
            body = response.json()
        except (ValueError, AttributeError) as exc:
            raise DofePricingError(
                f"Airouter pricing returned non-JSON HTTP {response.status_code}"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            message = body.get("msg") if isinstance(body, dict) else None
            raise DofePricingError(
                f"Airouter pricing returned HTTP {response.status_code}: {message or 'request failed'}"
            )
        if not isinstance(body, dict) or str(body.get("code")) not in {"0", "200"}:
            raise DofePricingError("Airouter pricing returned an unsuccessful response envelope")
        data = body.get("data")
        if not isinstance(data, dict):
            raise DofePricingError("Airouter pricing response is missing data")
        return data
