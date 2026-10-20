"""Models-owned MCP delegation. No member/project key crosses this boundary."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from .errors import DofeAuthError

_REF = re.compile(r"^[a-f0-9]{64}$")
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SERVICE = "openmontage"
_context: ContextVar[McpDelegation | None] = ContextVar("openmontage_mcp_delegation", default=None)


class McpDelegationError(DofeAuthError):
    """Invalid, expired, revoked, or incorrectly scoped caller delegation."""


def models_base_url() -> str:
    value = os.environ.get("DOFE_MODEL_BASE_URL", "https://ai.hozonauto.com/api").rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != "ai.hozonauto.com" or parsed.path != "/api" or parsed.query or parsed.fragment:
        raise McpDelegationError("MCP delegation requires the first-party Models origin")
    return value


def service_headers() -> dict[str, str]:
    secret = os.environ.get("INTERNAL_API_SECRET", "").strip()
    if not secret:
        raise McpDelegationError("Models internal service authentication unavailable")
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), f"{timestamp}:{_SERVICE}".encode(), hashlib.sha256).hexdigest()
    return {"Authorization": f"Bearer {timestamp}:{signature}:{_SERVICE}", "X-Service-Name": _SERVICE}


def operation_for(method: str, path: str) -> str:
    exact = {
        ("GET", "/v1/models"): "models.list",
        ("POST", "/v1/chat/completions"): "chat.completions",
        ("POST", "/v1/responses"): "responses",
        ("POST", "/v1/generation/tasks"): "generation.tasks.create",
        ("POST", "/internal/pricing/quote"): "pricing.quote",
        ("POST", "/internal/media/upload"): "media.upload",
    }
    operation = exact.get((method.upper(), path))
    if operation:
        return operation
    if method.upper() == "GET" and re.fullmatch(r"/v1/models/[A-Za-z0-9._%:-]+/playground-capability", path):
        return "models.capability"
    match = re.fullmatch(r"/v1/generation/tasks/([A-Za-z0-9_-]+)(/artifacts|/cancel)?", path)
    if match and method.upper() == ("POST" if match[2] == "/cancel" else "GET"):
        return "generation.tasks." + ({None: "get", "/artifacts": "artifacts", "/cancel": "cancel"}[match[2]])
    raise McpDelegationError("Models operation is outside the MCP delegation contract")


@dataclass(frozen=True)
class McpDelegation:
    delegation_ref: str = field(repr=False)
    api_key_id: str
    tenant_id: str
    sso_team_id: str
    request_id: str
    allowed_operations: tuple[str, ...]
    expires_at: str
    mode: str
    member_id: str | None = None
    job_id: str | None = None
    stage: str = ""
    invocation_seed: str = ""
    _sequence: list[int] = field(default_factory=lambda: [0], repr=False, compare=False)
    proxy_token: str = field(default_factory=lambda: uuid4().hex, repr=False, compare=False)

    def ensure_current(self) -> None:
        if datetime.fromisoformat(self.expires_at.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            raise McpDelegationError("Models MCP delegation expired")

    def scoped(self, *, stage: str = "", invocation_seed: str = "") -> McpDelegation:
        return replace(self, stage=stage, invocation_seed=invocation_seed, _sequence=[0])

    def headers(self, method: str, path: str, *, invocation_id: str | None = None, body: Any = None) -> dict[str, str]:
        self.ensure_current()
        operation = operation_for(method, path)
        if operation not in self.allowed_operations:
            raise McpDelegationError("Models MCP delegation operation is not permitted")
        headers = {**service_headers(), "X-Dofe-Mcp-Delegation-Ref": self.delegation_ref,
                   "X-Dofe-Mcp-Delegation-Version": "1"}
        if self.mode == "job":
            if not self.job_id:
                raise McpDelegationError("Missing Models delegated job identity")
            headers["X-Dofe-Delegation-Job-Id"] = self.job_id
        if method.upper() == "POST" and operation in {"responses", "chat.completions", "generation.tasks.create"}:
            if invocation_id is None:
                self._sequence[0] += 1
                raw = json.dumps([self.job_id, self.stage, self.invocation_seed or self.request_id,
                                  self._sequence[0], path, body], sort_keys=True, separators=(",", ":"))
                invocation_id = "om-" + hashlib.sha256(raw.encode()).hexdigest()
            if not _ID.fullmatch(invocation_id):
                raise McpDelegationError("Invalid Models invocation identity")
            headers["X-Dofe-Model-Invocation-Id"] = invocation_id
        return headers

    def url(self, path: str) -> str:
        return models_base_url() + ("/internal" + path if path.startswith("/v1/") else path)

    def agent_environment(self, *, openai_base_url: str, dofe_base_url: str) -> dict[str, str]:
        # The subprocess receives only a local proxy password, never the service secret or delegation reference.
        return {"DOFE_ENABLED": "true", "OPENAI_API_KEY": self.proxy_token,
                "DOFE_MODEL_API_KEY": self.proxy_token, "OPENAI_BASE_URL": openai_base_url,
                "DOFE_MODEL_BASE_URL": dofe_base_url, "DOFE_INTERNAL_API_BASE_URL": dofe_base_url,
                "DOFE_MCP_PROXY_TOKEN": self.proxy_token, "DOFE_TENANT_ID": self.tenant_id}


def current_delegation() -> McpDelegation | None:
    return _context.get()


@contextmanager
def delegation_context(value: McpDelegation | None) -> Iterator[None]:
    token = _context.set(value)
    try:
        yield
    finally:
        _context.reset(token)


class ModelsDelegationClient:
    def __init__(self, session: Any = None):
        self.session = session or requests.Session()

    def _call(self, action: str, payload: dict[str, str]) -> McpDelegation:
        ref = payload["delegationRef"]
        if not _REF.fullmatch(ref):
            raise McpDelegationError("Invalid Models delegation reference")
        try:
            response = self.session.post(models_base_url() + "/internal/mcp/delegations/" + action,
                headers={**service_headers(), "Accept": "application/json", "Content-Type": "application/json"},
                json=payload, timeout=(5, 15), allow_redirects=False)
            body = response.json()
        except (requests.RequestException, ValueError, TypeError):
            raise McpDelegationError("Models delegation verification unavailable") from None
        data = body.get("data") if isinstance(body, dict) else None
        if response.status_code != 200 or not isinstance(body, dict) or str(body.get("code")) not in {"0", "200"} or not isinstance(data, dict):
            raise McpDelegationError("Models delegation verification refused")
        try:
            if (data["authenticated"] is not True or data["delegationRef"] != ref
                or data["audience"] != _SERVICE or data["route"] != "/mcp/montage"
                or data["mode"] not in {"request", "job"}):
                raise ValueError()
            for key in ("apiKeyId", "tenantId", "ssoTeamId", "requestId"):
                if not isinstance(data[key], str) or not _ID.fullmatch(data[key]):
                    raise ValueError()
            operations = data["allowedOperations"]
            if not isinstance(operations, list) or not all(isinstance(value, str) for value in operations):
                raise ValueError()
            if data["mode"] == "job" and (not isinstance(data.get("jobId"), str) or not _ID.fullmatch(data["jobId"])):
                raise ValueError()
            expiry = datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                raise ValueError()
            result = McpDelegation(ref, data["apiKeyId"], data["tenantId"], data["ssoTeamId"], data["requestId"],
                tuple(operations), data["expiresAt"], data["mode"], data.get("memberId"), data.get("jobId"))
            result.ensure_current()
            return result
        except (KeyError, TypeError, ValueError):
            raise McpDelegationError("Invalid Models delegation context") from None

    def introspect(self, ref: str) -> McpDelegation:
        return self._call("introspect", {"delegationRef": ref})

    def claim(self, value: McpDelegation, job_id: str, source_invocation_id: str) -> McpDelegation:
        claimed = self._call("claim", {"delegationRef": value.delegation_ref, "jobId": job_id,
                                      "sourceInvocationId": source_invocation_id})
        if (claimed.mode != "job" or claimed.job_id != job_id or claimed.api_key_id != value.api_key_id
            or claimed.tenant_id != value.tenant_id or claimed.sso_team_id != value.sso_team_id):
            raise McpDelegationError("Models job claim identity mismatch")
        return claimed
