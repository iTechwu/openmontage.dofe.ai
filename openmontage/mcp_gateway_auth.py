"""Phase-one CI MCP gateway authentication for OpenMontage."""

from __future__ import annotations

import hmac
import json
import os
import uuid
from collections.abc import Mapping
from typing import Any

from openmontage.contracts import JobAttribution

GATEWAY_MARKER = "models-api-key-v1"
GATEWAY_RUNTIME_ID = "mcp-gateway"


def gateway_attempted(headers: Mapping[str, str] | None) -> bool:
    return any(
        _header(headers, name)
        for name in (
            "X-Dofe-Mcp-Gateway-Secret",
            "X-Dofe-Auth-Verified",
            "X-Dofe-Api-Key-Id",
            "X-Dofe-Tenant-Id",
            "X-Dofe-Sso-Team-Id",
        )
    )


def gateway_attribution(headers: Mapping[str, str] | None) -> JobAttribution | None:
    if not gateway_attempted(headers):
        return None
    configured = os.environ.get("OPENMONTAGE_MCP_GATEWAY_SECRET", "").strip()
    provided = (_header(headers, "X-Dofe-Mcp-Gateway-Secret") or "").strip()
    if (
        not configured
        or not provided
        or not hmac.compare_digest(configured, provided)
        or (_header(headers, "X-Dofe-Auth-Verified") or "").strip() != GATEWAY_MARKER
    ):
        return None

    api_key_id = (_header(headers, "X-Dofe-Api-Key-Id") or "").strip()
    tenant_id = (_header(headers, "X-Dofe-Tenant-Id") or "").strip()
    sso_team_id = (_header(headers, "X-Dofe-Sso-Team-Id") or "").strip()
    authorization = (_header(headers, "Authorization") or "").strip()
    if not api_key_id or not tenant_id or not sso_team_id or not _bearer_token(authorization):
        return None

    request_id = (
        (_header(headers, "X-Request-Id") or _header(headers, "X-Trace-Id") or "").strip()
        or f"mcp-{uuid.uuid4().hex}"
    )
    return JobAttribution(
        workspace_id=f"tenant:{tenant_id}",
        employee_id=f"mcp-key:{api_key_id}",
        runtime_id=GATEWAY_RUNTIME_ID,
        root_task_id=f"mcp:{api_key_id}",
        conversation_id=request_id,
        source_invocation_id=request_id,
        trace_id=request_id,
    )


class McpGatewayAuthMiddleware:
    """Reject malformed gateway headers before MCP JSON-RPC handling."""

    def __init__(self, app: Any, *, gateway_only: bool) -> None:
        self.app = app
        self.gateway_only = gateway_only

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") not in {"/mcp", "/mcp/"}:
            await self.app(scope, receive, send)
            return
        headers = _scope_headers(scope)
        if not gateway_attempted(headers):
            if self.gateway_only:
                await _unauthorized(send)
                return
            await self.app(scope, receive, send)
            return
        if gateway_attribution(headers) is None:
            await _unauthorized(send)
            return
        await self.app(scope, receive, send)


def _scope_headers(scope: dict[str, Any]) -> dict[str, str]:
    return {
        key.decode("latin-1"): value.decode("latin-1")
        for key, value in scope.get("headers", ())
    }


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    expected = name.casefold()
    for key, value in headers.items():
        if key.casefold() == expected:
            return value
    return None


def _bearer_token(authorization: str) -> str:
    scheme, separator, token = authorization.partition(" ")
    return token.strip() if separator and scheme.casefold() == "bearer" else ""


async def _unauthorized(send: Any) -> None:
    body = json.dumps(
        {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Unauthorized"}, "id": None},
        separators=(",", ":"),
    ).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"www-authenticate", b"Bearer"),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": body})
