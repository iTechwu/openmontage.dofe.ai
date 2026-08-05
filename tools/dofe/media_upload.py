"""HMAC-authenticated uploads to AIRouter's internal TOS staging endpoint."""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import time
from pathlib import Path
from typing import Any

import requests

from . import config as cfg


class DofeMediaUploadError(RuntimeError):
    """Raised when media cannot be staged in AIRouter-managed storage."""


class DofeMediaUploadClient:
    """Upload local media to ``dofe-transcode/temp/generation-assets``."""

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
            raise DofeMediaUploadError(
                "INTERNAL_API_SECRET is not configured for AIRouter media upload"
            )
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
        }

    def upload(self, path: str | Path, *, asset_type: str = "audio") -> dict[str, Any]:
        source = Path(path)
        if not source.is_file():
            raise DofeMediaUploadError(f"Media file not found: {source}")
        if not self.tenant_id:
            raise DofeMediaUploadError("DOFE_TENANT_ID is not configured for media upload")
        if asset_type not in {"audio", "video", "image", "document"}:
            raise DofeMediaUploadError(f"Unsupported media asset type: {asset_type}")

        mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        try:
            with source.open("rb") as handle:
                response = self.session.post(
                    f"{self.base_url}/internal/media/upload",
                    headers=self._headers(),
                    data={"tenant_id": self.tenant_id, "asset_type": asset_type},
                    files={"file": (source.name, handle, mime_type)},
                    timeout=(cfg.connect_timeout(), cfg.create_read_timeout()),
                )
        except requests.RequestException as exc:
            raise DofeMediaUploadError(f"AIRouter media upload failed: {exc}") from exc

        try:
            body = response.json()
        except (ValueError, AttributeError) as exc:
            raise DofeMediaUploadError(
                f"AIRouter media upload returned non-JSON HTTP {response.status_code}"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            message = body.get("msg") if isinstance(body, dict) else None
            raise DofeMediaUploadError(
                f"AIRouter media upload returned HTTP {response.status_code}: "
                f"{message or 'request failed'}"
            )
        if not isinstance(body, dict) or str(body.get("code")) not in {"0", "200"}:
            raise DofeMediaUploadError("AIRouter media upload returned an unsuccessful response")
        data = body.get("data")
        if not isinstance(data, dict):
            raise DofeMediaUploadError("AIRouter media upload response is missing data")
        url = str(data.get("url") or "")
        key = str(data.get("key") or "")
        if (
            data.get("bucket") != "dofe-transcode"
            or not url.startswith("tos://dofe-transcode/temp/generation-assets/")
            or not key.startswith("temp/generation-assets/")
        ):
            raise DofeMediaUploadError("AIRouter media upload returned an invalid storage target")
        return data
