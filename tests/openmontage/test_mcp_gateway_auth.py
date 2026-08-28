from __future__ import annotations

from starlette.testclient import TestClient

from openmontage.job_api import TrustedAttributionResolver
from openmontage.mcp_gateway_auth import gateway_attribution
from openmontage.mcp_server import build_http_app


def _headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer sk-models-user",
        "X-Dofe-Mcp-Gateway-Secret": "gateway-secret",
        "X-Dofe-Auth-Verified": "models-api-key-v1",
        "X-Dofe-Api-Key-Id": "key-42",
        "X-Dofe-Tenant-Id": "tenant-42",
        "X-Dofe-Sso-Team-Id": "team-42",
        "X-Request-Id": "req-42",
    }


def test_gateway_identity_maps_to_stable_workspace(monkeypatch) -> None:
    monkeypatch.setenv("OPENMONTAGE_MCP_GATEWAY_SECRET", "gateway-secret")

    attribution = gateway_attribution(_headers())

    assert attribution is not None
    assert attribution.workspace_id == "tenant:tenant-42"
    assert attribution.employee_id == "mcp-key:key-42"
    assert attribution.conversation_id == "req-42"
    assert TrustedAttributionResolver("service-token")(_headers()) == attribution


def test_gateway_context_with_wrong_secret_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("OPENMONTAGE_MCP_GATEWAY_SECRET", "gateway-secret")
    assert gateway_attribution({**_headers(), "X-Dofe-Mcp-Gateway-Secret": "wrong"}) is None


def test_http_mcp_rejects_gateway_headers_before_jsonrpc(monkeypatch) -> None:
    monkeypatch.setenv("OPENMONTAGE_MCP_GATEWAY_SECRET", "gateway-secret")
    client = TestClient(build_http_app(
        job_service=None,
        attribution_resolver=TrustedAttributionResolver("service-token"),
    ))

    response = client.post("/mcp", headers={**_headers(), "X-Dofe-Tenant-Id": ""}, json={})

    assert response.status_code == 401
