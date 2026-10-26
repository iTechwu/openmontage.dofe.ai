from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests_mock
from starlette.testclient import TestClient

from tools.dofe.client import DofeClient
from tools.dofe.mcp_delegation import (ModelsDelegationClient, McpDelegationError,
    current_delegation, delegation_context, service_headers, operation_for)
from tools.dofe.pricing import DofePricingClient
from tools.dofe.media_upload import DofeMediaUploadClient
from openmontage.mcp_gateway_auth import McpGatewayAuthMiddleware, gateway_attribution
from tests.openmontage.g12_helpers import delegated, stub_models

BASE = "https://ai.hozonauto.com/api"


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "synthetic-internal-test-secret")
    monkeypatch.setenv("OPENMONTAGE_MCP_GATEWAY_SECRET", "synthetic-gateway-secret")
    monkeypatch.setenv("DOFE_MODEL_BASE_URL", BASE)
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "synthetic-project-key-must-not-be-used")
    monkeypatch.setenv("DOFE_TENANT_ID", "wrong-configured-tenant")


def envelope(value=None, **changes):
    value = value or delegated()
    data = {"authenticated": True, "delegationRef": value.delegation_ref, "apiKeyId": value.api_key_id,
        "tenantId": value.tenant_id, "ssoTeamId": value.sso_team_id, "requestId": value.request_id,
        "audience": "openmontage", "route": "/mcp/montage", "allowedOperations": list(value.allowed_operations),
        "expiresAt": value.expires_at, "mode": value.mode, "jobId": value.job_id, "memberId": value.member_id}
    data.update(changes)
    return {"code":200,"data":data}


def headers(value=None):
    value = value or delegated()
    return {"X-Dofe-Mcp-Gateway-Secret":"synthetic-gateway-secret", "X-Dofe-Auth-Verified":"models-api-key-v1",
        "X-Dofe-Mcp-Delegation-Ref":value.delegation_ref,"X-Dofe-Mcp-Delegation-Version":"1",
        "X-Dofe-Api-Key-Id":value.api_key_id,"X-Dofe-Tenant-Id":value.tenant_id,"X-Dofe-Sso-Team-Id":value.sso_team_id}


def test_service_hmac_is_bound_to_service(monkeypatch):
    monkeypatch.setattr("tools.dofe.mcp_delegation.time.time",lambda:123456)
    result=service_headers()
    expected=hmac.new(b"synthetic-internal-test-secret",b"123456:openmontage",hashlib.sha256).hexdigest()
    assert result == {"Authorization":f"Bearer 123456:{expected}:openmontage","X-Service-Name":"openmontage"}


@pytest.mark.parametrize("change",[{"audience":"tools"},{"route":"/mcp/knowledge"},{"authenticated":False},
    {"delegationRef":"b"*64},{"expiresAt":"2000-01-01T00:00:00Z"},{"expiresAt":"2030-01-01"},
    {"mode":"job","jobId":None},{"allowedOperations":"responses"},{"tenantId":"bad/tenant"}])
def test_introspection_rejects_invalid_binding(change):
    with requests_mock.Mocker() as mock:
        mock.post(BASE+"/internal/mcp/delegations/introspect",json=envelope(**change))
        with pytest.raises(McpDelegationError):ModelsDelegationClient().introspect("a"*64)


def test_introspection_does_not_follow_redirect_or_send_project_key():
    with requests_mock.Mocker() as mock:
        target=mock.post(BASE+"/internal/mcp/delegations/introspect",status_code=302,headers={"Location":"https://evil.test/"},json={})
        with pytest.raises(McpDelegationError):ModelsDelegationClient().introspect("a"*64)
        assert target.last_request.headers["X-Service-Name"] == "openmontage"
        assert "project-key" not in target.last_request.headers["Authorization"]
        assert len(mock.request_history)==1


def test_scoped_client_uses_internal_fixed_routes_and_original_identity():
    value=delegated(mode="job",job_id="om_job_1").scoped(stage="assets",invocation_seed="call-1")
    with requests_mock.Mocker() as mock, delegation_context(value):
        catalog=mock.get(BASE+"/internal/v1/models",json={"data":[{"id":"visible-model"}]})
        task=mock.post(BASE+"/internal/v1/generation/tasks",json={"code":200,"data":{"taskId":"gen-1"}})
        client=DofeClient(api_key="synthetic-explicit-key",base_url="https://wrong.test/api")
        assert client.api_key is None
        assert client.list_models()==[{"id":"visible-model"}]
        assert client.create_task({"model":"visible-model"})["taskId"]=="gen-1"
        assert catalog.last_request.headers["X-Dofe-Mcp-Delegation-Ref"]==value.delegation_ref
        assert task.last_request.headers["X-Dofe-Delegation-Job-Id"]=="om_job_1"
        assert task.last_request.headers["X-Dofe-Model-Invocation-Id"].startswith("om-")
        assert "synthetic-explicit-key" not in str(task.last_request.headers)


@pytest.mark.parametrize("method,path",[("POST","/v1/embeddings"),("GET","https://evil.test"),
    ("GET","/v1/models?admin=1"),("GET","/v1/generation/tasks/../secret"),("DELETE","/v1/generation/tasks/a")])
def test_operation_allowlist_rejects_escape(method,path):
    with pytest.raises(McpDelegationError):operation_for(method,path)


def test_missing_scope_and_expiry_never_fall_back_to_project_key():
    for value in [delegated(allowed_operations=()),delegated(expires_at="2000-01-01T00:00:00+00:00")]:
        with delegation_context(value), requests_mock.Mocker() as mock:
            with pytest.raises(McpDelegationError):DofeClient().list_models()
            assert not mock.request_history


def test_two_concurrent_request_contexts_do_not_mix():
    def inspect(index):
        value=delegated(delegation_ref=str(index)*64,api_key_id=f"key-{index}",tenant_id=f"tenant-{index}")
        with delegation_context(value):return DofeClient().mcp_delegation
    with ThreadPoolExecutor(max_workers=2) as pool:
        left,right=pool.map(inspect,[1,2])
    assert left.tenant_id=="tenant-1" and right.tenant_id=="tenant-2"
    assert current_delegation() is None


def test_pricing_and_upload_take_tenant_from_delegation(tmp_path):
    source=tmp_path/"audio.wav";source.write_bytes(b"synthetic-audio")
    value=delegated(mode="job",job_id="om_job_1")
    with delegation_context(value), requests_mock.Mocker() as mock:
        pricing=mock.post(BASE+"/internal/pricing/quote",json={"code":200,"data":{"amount":"1"}})
        upload=mock.post(BASE+"/internal/media/upload",json={"code":200,"data":{"url":"tos://dofe-transcode/temp/generation-assets/a.wav", "bucket":"dofe-transcode", "key":"temp/generation-assets/a.wav"}})
        DofePricingClient(tenant_id="forged").quote({"model":"visible-model","tenantId":"forged"})
        DofeMediaUploadClient(tenant_id="forged").upload(source)
        assert pricing.last_request.json()["tenantId"]==value.tenant_id
        assert pricing.last_request.headers["X-Dofe-Delegation-Job-Id"]==value.job_id
        assert value.tenant_id.encode() in upload.last_request.body
        assert b"wrong-configured-tenant" not in upload.last_request.body


def test_child_environment_contains_only_loopback_password():
    value=delegated(mode="job",job_id="om_job_1")
    result=value.agent_environment(openai_base_url="http://127.0.0.1:1234/api/v1",dofe_base_url="http://127.0.0.1:1234/api")
    assert value.delegation_ref not in json.dumps(result)
    assert "INTERNAL_API_SECRET" not in result
    assert result["OPENAI_API_KEY"]==value.proxy_token


def test_middleware_separates_mcp_from_legacy_readonly_rest(monkeypatch):
    async def app(scope,receive,send):
        mapped=gateway_attribution(dict((k.decode(),v.decode()) for k,v in scope["headers"]))
        await send({"type":"http.response.start","status":200 if mapped else 401,"headers":[]})
        await send({"type":"http.response.body","body":b"ok"})
    stub_models(monkeypatch,delegated())
    client=TestClient(McpGatewayAuthMiddleware(app,gateway_only=True))
    legacy={k:v for k,v in headers().items() if "Delegation" not in k};legacy["Authorization"]="Bearer synthetic-member-test-key"
    assert client.get("/api/v1/jobs",headers=legacy).status_code==200
    assert client.post("/api/v1/jobs",headers=legacy).status_code==401
    assert client.post("/mcp",headers=legacy).status_code==401
    assert client.post("/mcp",headers=headers()).status_code==200


def test_job_reference_is_private_and_survives_restart(monkeypatch,tmp_path):
    from openmontage.job_service import JobService
    from tests.openmontage.test_job_worker import _request
    value=delegated()
    stub_models(monkeypatch,value)
    service=JobService(tmp_path/"jobs.sqlite3",projects_dir=tmp_path/"projects")
    with delegation_context(value):
        attribution=gateway_attribution(headers(value))
        job=service.create_job(_request(request_id="g12-private-job"),attribution)
        assert service.create_job(_request(request_id="g12-private-job"),attribution).job_id==job.job_id
    restored=JobService(tmp_path/"jobs.sqlite3",projects_dir=tmp_path/"projects")
    assert restored.model_delegation(job.job_id).job_id==job.job_id
    assert value.delegation_ref not in job.model_dump_json()
    assert all(value.delegation_ref not in event.model_dump_json() for event in service.list_events(job.job_id))
    with pytest.raises(McpDelegationError):restored.model_delegation(job.job_id,caller=replace(value,api_key_id="other-key"))


def test_claim_failure_persists_no_job_or_reference_and_retry_reuses_job_id(monkeypatch,tmp_path):
    from openmontage.job_service import JobService
    from tests.openmontage.test_job_worker import _request
    value=delegated()
    state=stub_models(monkeypatch,value)
    service=JobService(tmp_path/"jobs.sqlite3",projects_dir=tmp_path/"projects")
    calls=[]
    def claim(self,current,job_id,source_invocation_id):
        calls.append(job_id)
        if len(calls)==1:raise McpDelegationError("verification unavailable")
        state["value"]=replace(value,mode="job",job_id=job_id)
        return state["value"]
    monkeypatch.setattr(ModelsDelegationClient,"claim",claim)
    with delegation_context(value):
        attribution=gateway_attribution(headers(value))
        with pytest.raises(McpDelegationError):service.create_job(_request(request_id="g12-retry"),attribution)
        assert service.list_jobs(attribution.workspace_id)==[]
        job=service.create_job(_request(request_id="g12-retry"),attribution)
    assert calls==[job.job_id,job.job_id]


def test_worker_refuses_missing_or_revoked_delegation_without_using_project_key(monkeypatch,tmp_path):
    from openmontage.job_service import JobService
    from openmontage.job_worker import JobWorker
    from tests.openmontage.test_job_worker import _request,FakeExecutor,NOW
    value=delegated();stub_models(monkeypatch,value)
    service=JobService(tmp_path/"jobs.sqlite3",projects_dir=tmp_path/"projects")
    with delegation_context(value):job=service.create_job(_request(request_id="g12-revoked"),gateway_attribution(headers()))
    def refuse(*args,**kwargs):raise McpDelegationError("revoked")
    monkeypatch.setattr(ModelsDelegationClient,"introspect",refuse)
    executor=FakeExecutor([])
    worker=JobWorker(service,executor,projects_dir=tmp_path/"projects",worker_id="test-worker")
    result=worker.run_once(now=NOW)
    assert result.outcome=="job_failed"
    assert not executor.assignments
    event=service.list_events(job.job_id)[-1]
    assert event.payload["error"]["code"]=="OPENMONTAGE_MODEL_DELEGATION_UNAVAILABLE"


def test_g12_proxy_reuses_transport_but_never_legacy_auth_or_arbitrary_path(monkeypatch):
    from types import SimpleNamespace
    from openmontage.delegation_proxy import McpDelegationProxy
    value=delegated(mode="job",job_id="om_job_1");stub_models(monkeypatch,value)
    proxy=McpDelegationProxy(value,job_id=value.job_id,stage="research")
    request=SimpleNamespace(command="POST",path="/api/v1/responses",headers={"Authorization":f"Bearer {value.proxy_token}"})
    proxy._validate_incoming(request)
    url,signed=proxy._upstream_request(request,"invocation-1")
    assert url==BASE+"/internal/v1/responses"
    assert signed["X-Dofe-Delegation-Job-Id"]==value.job_id
    assert signed["X-Dofe-Mcp-Delegation-Ref"]==value.delegation_ref
    assert value.proxy_token not in signed["Authorization"]
    for path in ["/api/admin", "/api/v1/responses?access_token=x", "https://evil.test/api/v1/responses"]:
        with pytest.raises(McpDelegationError):proxy._validate_incoming(SimpleNamespace(command="POST",path=path,headers=request.headers))
    with pytest.raises(McpDelegationError):proxy._validate_incoming(SimpleNamespace(command="POST",path=request.path,headers={}))
    def revoke(*args,**kwargs):raise McpDelegationError("revoked")
    monkeypatch.setattr(ModelsDelegationClient,"introspect",revoke)
    with pytest.raises(McpDelegationError):proxy._validate_incoming(request)
