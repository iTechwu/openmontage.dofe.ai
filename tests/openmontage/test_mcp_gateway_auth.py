from __future__ import annotations

from starlette.testclient import TestClient
import pytest
from dataclasses import replace
from tools.dofe.mcp_delegation import ModelsDelegationClient
from tests.openmontage.g12_helpers import delegated

from openmontage.job_api import TrustedAttributionResolver
from openmontage.mcp_gateway_auth import gateway_attribution
from openmontage.mcp_server import build_http_app


@pytest.fixture(autouse=True)
def _models(monkeypatch):
    def introspect(self, ref):
        return delegated(delegation_ref=ref, member_id="member-42" if ref in {"b"*64,"c"*64} else None,
                         api_key_id="key-43" if ref == "c"*64 else "key-42")
    monkeypatch.setattr(ModelsDelegationClient,"introspect",introspect)


def _headers() -> dict[str, str]:
    return {
        "X-Dofe-Mcp-Delegation-Ref": "a" * 64,
        "X-Dofe-Mcp-Delegation-Version": "1",
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


def test_verified_gateway_member_survives_key_rotation(monkeypatch) -> None:
    monkeypatch.setenv("OPENMONTAGE_MCP_GATEWAY_SECRET", "gateway-secret")
    first = gateway_attribution({**_headers(), "x-dofe-member-id": " member-42 ", "X-Dofe-Mcp-Delegation-Ref":"b"*64})
    rotated = gateway_attribution({
        **_headers(), "X-Dofe-Member-Id": "member-42", "X-Dofe-Api-Key-Id": "key-43", "X-Dofe-Mcp-Delegation-Ref":"c"*64,
    })
    assert first is not None and rotated is not None
    assert first.employee_id == rotated.employee_id == "member-42"
    assert first.root_task_id != rotated.root_task_id


def test_delegated_envelope_with_leftover_bearer_is_rejected(monkeypatch) -> None:
    # G4 深度防御：网关委托路径必须消耗成员 bearer；委托信封携带 Authorization
    # 即说明未经过网关委托路径（对齐 tools 的双保险校验）。
    monkeypatch.setenv("OPENMONTAGE_MCP_GATEWAY_SECRET", "gateway-secret")
    client = TestClient(build_http_app(
        job_service=None,
        attribution_resolver=TrustedAttributionResolver("service-token"),
    ))
    response = client.post(
        "/mcp",
        headers={**_headers(), "X-Dofe-Mcp-Delegation-Ref": "d" * 64,
                 "Authorization": "Bearer sk-leftover"},
        json={},
    )
    assert response.status_code == 401


def test_member_header_does_not_bypass_gateway_auth(monkeypatch) -> None:
    monkeypatch.setenv("OPENMONTAGE_MCP_GATEWAY_SECRET", "gateway-secret")
    client = TestClient(build_http_app(
        job_service=None,
        attribution_resolver=TrustedAttributionResolver("service-token"),
    ))
    response = client.post("/mcp", headers={"X-Dofe-Member-Id": "member-42"}, json={})
    assert response.status_code == 401


def test_http_mcp_rejects_gateway_headers_before_jsonrpc(monkeypatch) -> None:
    monkeypatch.setenv("OPENMONTAGE_MCP_GATEWAY_SECRET", "gateway-secret")
    client = TestClient(build_http_app(
        job_service=None,
        attribution_resolver=TrustedAttributionResolver("service-token"),
    ))

    response = client.post("/mcp", headers={**_headers(), "X-Dofe-Tenant-Id": ""}, json={})

    assert response.status_code == 401
