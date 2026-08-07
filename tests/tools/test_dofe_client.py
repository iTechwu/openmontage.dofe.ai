"""Unit tests for the dofe gateway client (dev-guide §8.1).

Uses requests_mock for HTTP and patches the client's sleep so retries/polling
run instantly. Covers: sync-terminal create, async create→poll→artifacts,
429/925429 retry, 5xx backoff (incl. non-retryable 502), 401/402/404 no-retry,
poll failed/timeout+cancel, envelope anomaly, download success/non-https/failure.
"""

from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path

import pytest
import requests_mock as _rm

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.dofe.client import DofeClient
from tools.dofe.runtime import _credential_free_url
from tools.dofe.errors import (
    DofeAPIError,
    DofeAuthError,
    DofeError,
    DofeModelUnavailableError,
    DofeQuotaError,
    DofeRateLimitError,
    DofeTaskFailedError,
    DofeTaskTimeoutError,
)

BASE = "https://dofe.test/api"
TASKS = f"{BASE}/v1/generation/tasks"
ARTIFACT_URL = "https://cdn.test/image.png"


def _ok(data):
    return {"code": 200, "msg": "ok", "data": data}


def _err(code, msg, *, details=None, error_code=None):
    return {
        "code": code,
        "msg": msg,
        "data": None,
        "error": {"code": str(error_code if error_code is not None else code), "details": details or {}},
    }


def _client(**kwargs):
    return DofeClient(api_key="test-key", base_url=BASE, **kwargs)


def test_client_uses_explicit_ca_bundle(monkeypatch, tmp_path):
    bundle = tmp_path / "rootCA.pem"
    bundle.write_text("test-ca")
    monkeypatch.setenv("DOFE_CA_BUNDLE", str(bundle))
    client = _client()
    assert client.session.verify == str(bundle)


def _succeeded_task(task_id="gen-1", with_assets=True):
    data = {
        "taskId": task_id,
        "status": "succeeded",
        "finalCost": "0.03000000",
        "estimatedCost": "0.03000000",
        "costCurrency": "CNY",
        "pricingBreakdown": {"billingUnit": "PER_IMAGE", "usage": {"imageCount": 1}},
    }
    if with_assets:
        data["outputAssets"] = [{"url": ARTIFACT_URL, "type": "image"}]
    return data


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Retries/polling must not actually sleep in tests.
    monkeypatch.setattr("tools.dofe.client.sleep", lambda *a, **k: None)


def _count(m, method, url_prefix):
    return sum(1 for r in m.request_history if r.method == method and r.url.startswith(url_prefix))


# --------------------------------------------------------------- create paths

def test_create_sync_terminal_returns_assets():
    with _rm.Mocker() as m:
        m.post(TASKS, json=_ok(_succeeded_task()))
        result = _client().submit_and_collect({"model": "seedream-5.0"}, timeout_seconds=60, poll_interval=1)
    assert result["status"] == "succeeded"
    assert result["assets"][0]["url"] == ARTIFACT_URL
    assert result["final_cost"] == "0.03000000"
    assert result["cost_currency"] == "CNY"
    assert result["pricing_breakdown"]["billingUnit"] == "PER_IMAGE"
    assert _count(m, "GET", TASKS) == 0  # no polling needed


def test_create_uses_payload_idempotency_key_as_logical_request_id():
    logical_call_id = "scene-007-image-01"
    with _rm.Mocker() as m:
        m.post(TASKS, json=_ok(_succeeded_task()))
        _client().create_task(
            {
                "model": "seedream-5.0",
                "metadata": {"openmontage_idempotency_key": logical_call_id},
            }
        )

    request = m.request_history[0]
    assert request.headers["X-OpenMontage-Logical-Call-Id"] == logical_call_id
    assert request.json()["idempotencyKey"] == logical_call_id


def test_create_rejects_conflicting_payload_and_metadata_idempotency_keys():
    with _rm.Mocker() as m:
        with pytest.raises(DofeAPIError, match="idempotency keys do not match"):
            _client().create_task(
                {
                    "model": "seedream-5.0",
                    "idempotencyKey": "explicit-call-0001",
                    "metadata": {"openmontage_idempotency_key": "metadata-call-0001"},
                }
            )

    assert m.request_history == []


def test_list_models_returns_tenant_visible_aliases():
    with _rm.Mocker() as m:
        m.get(
            f"{BASE}/v1/models",
            json={"object": "list", "data": [{"id": "seedream-5.0"}]},
        )
        result = _client().list_models()

    assert [item["id"] for item in result] == ["seedream-5.0"]


def test_get_playground_capability_returns_exact_visible_model_projection():
    capability = {
        "alias": "seedance/video 2",
        "modelType": "video",
        "state": "ready",
    }
    with _rm.Mocker() as m:
        m.get(
            f"{BASE}/v1/models/seedance%2Fvideo%202/playground-capability",
            json=_ok({"capability": capability}),
        )
        result = _client().get_playground_capability("seedance/video 2")

    assert result == capability


def test_get_playground_capability_rejects_alias_mismatch():
    with _rm.Mocker() as m:
        m.get(
            f"{BASE}/v1/models/requested/playground-capability",
            json=_ok({"capability": {"alias": "different"}}),
        )
        with pytest.raises(DofeAPIError, match="alias mismatch"):
            _client().get_playground_capability("requested")


def test_delegated_requests_sign_job_stage_and_one_invocation_id_across_retries(monkeypatch):
    monkeypatch.setenv("DOFE_DELEGATION_ID", "delegation-1")
    monkeypatch.setenv("DOFE_EXTERNAL_JOB_ID", "job-1")
    monkeypatch.setenv("DOFE_PIPELINE_STAGE", "research")
    with _rm.Mocker() as m:
        m.get(f"{BASE}/v1/models", [
            {"status_code": 503, "json": _err(503, "retry")},
            {"json": {"object": "list", "data": []}},
        ])
        _client(max_5xx_retries=1).list_models()

    first, second = m.request_history
    invocation_id = first.headers["X-Dofe-Model-Invocation-Id"]
    assert second.headers["X-Dofe-Model-Invocation-Id"] == invocation_id
    assert first.headers["X-Dofe-Pipeline-Stage"] == "research"
    timestamp = first.headers["X-Dofe-Attribution-Timestamp"]
    expected = hmac.new(
        b"test-key",
        f"delegation-1\njob-1\nresearch\n{invocation_id}\n{timestamp}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert first.headers["X-Dofe-Attribution-Signature"] == expected


def test_create_then_poll_then_artifacts():
    task_url = f"{TASKS}/gen-2"
    with _rm.Mocker() as m:
        m.post(TASKS, json=_ok({"taskId": "gen-2", "status": "running"}))
        m.get(task_url, [
            {"json": _ok({"taskId": "gen-2", "status": "pending"})},
            {"json": _ok({"taskId": "gen-2", "status": "running"})},
            {"json": _ok({"taskId": "gen-2", "status": "succeeded"})},  # no outputAssets -> fetch artifacts
        ])
        m.get(f"{task_url}/artifacts", json=_ok({"taskId": "gen-2", "assets": [{"url": ARTIFACT_URL, "type": "image"}]}))
        result = _client().submit_and_collect({"model": "x"}, timeout_seconds=60, poll_interval=1)
    assert result["status"] == "succeeded"
    assert result["assets"][0]["url"] == ARTIFACT_URL
    assert _count(m, "GET", f"{task_url}/artifacts") == 1


def test_estimate_is_not_mislabeled_as_final_cost():
    with _rm.Mocker() as m:
        task = _succeeded_task()
        task.pop("finalCost")
        m.post(TASKS, json=_ok(task))
        result = _client().submit_and_collect({"model": "seedream-5.0"}, timeout_seconds=60)

    assert result["estimated_cost"] == "0.03000000"
    assert result["final_cost"] is None


def test_async_terminal_refreshes_once_for_settled_final_cost():
    task_url = f"{TASKS}/gen-cost"
    with _rm.Mocker() as m:
        m.post(
            TASKS,
            json=_ok(
                {
                    "taskId": "gen-cost",
                    "status": "running",
                    "estimatedCost": "0.06893568",
                }
            ),
        )
        m.get(
            task_url,
            [
                {
                    "json": _ok(
                        {
                            "taskId": "gen-cost",
                            "status": "succeeded",
                            "estimatedCost": "0.06893568",
                            "finalCost": None,
                            "costCurrency": "CNY",
                            "outputAssets": [{"url": "", "type": "document"}],
                        }
                    )
                },
                {
                    "json": _ok(
                        {
                            "taskId": "gen-cost",
                            "status": "succeeded",
                            "estimatedCost": "0.06893568",
                            "finalCost": "0.06893568",
                            "costCurrency": "CNY",
                            "outputAssets": [{"url": "", "type": "document"}],
                        }
                    )
                },
            ],
        )

        result = _client().submit_and_collect(
            {"model": "openspeech-auc"}, timeout_seconds=60, poll_interval=1
        )

    assert result["final_cost"] == "0.06893568"
    assert _count(m, "GET", task_url) == 2


def test_resume_existing_task_skips_create():
    task_url = f"{TASKS}/gen-9"
    with _rm.Mocker() as m:
        m.get(task_url, [
            {"json": _ok({"taskId": "gen-9", "status": "running"})},
            {"json": _ok({"taskId": "gen-9", "status": "succeeded", "outputAssets": [{"url": ARTIFACT_URL}]})},
        ])
        result = _client().submit_and_collect(
            {"model": "x"}, timeout_seconds=60, poll_interval=1, existing_task_id="gen-9",
        )
    assert result["task_id"] == "gen-9"
    assert _count(m, "POST", TASKS) == 0  # did not re-create


# ------------------------------------------------------------------- retries

def test_rate_limit_http_429_retries_then_succeeds():
    task_url = f"{TASKS}/gen-3"
    with _rm.Mocker() as m:
        m.post(TASKS, json=_ok({"taskId": "gen-3", "status": "running"}))
        m.get(task_url, [
            {"status_code": 429, "json": _err(925429, "slow down", details={"retryAfter": 0})},
            {"json": _ok({"taskId": "gen-3", "status": "succeeded", "outputAssets": [{"url": ARTIFACT_URL}]})},
        ])
        result = _client().submit_and_collect({"model": "x"}, timeout_seconds=60, poll_interval=1)
    assert result["status"] == "succeeded"
    assert _count(m, "GET", task_url) == 2


def test_rate_limit_business_code_925429_retries():
    task_url = f"{TASKS}/gen-4"
    with _rm.Mocker() as m:
        m.post(TASKS, json=_ok({"taskId": "gen-4", "status": "running"}))
        m.get(task_url, [
            {"json": _err(925429, "rate limited", details={"retryAfter": 0})},  # HTTP 200, bad biz code
            {"json": _ok({"taskId": "gen-4", "status": "succeeded", "outputAssets": [{"url": ARTIFACT_URL}]})},
        ])
        result = _client().submit_and_collect({"model": "x"}, timeout_seconds=60, poll_interval=1)
    assert result["status"] == "succeeded"
    assert _count(m, "GET", task_url) == 2


def test_5xx_backoff_retries_then_succeeds():
    task_url = f"{TASKS}/gen-5"
    with _rm.Mocker() as m:
        m.post(TASKS, json=_ok({"taskId": "gen-5", "status": "running"}))
        m.get(task_url, [
            {"status_code": 500, "json": _err(500, "boom")},
            {"json": _ok({"taskId": "gen-5", "status": "succeeded", "outputAssets": [{"url": ARTIFACT_URL}]})},
        ])
        result = _client().submit_and_collect({"model": "x"}, timeout_seconds=60, poll_interval=1)
    assert result["status"] == "succeeded"


def test_502_param_price_not_found_does_not_retry():
    task_url = f"{TASKS}/gen-6"
    with _rm.Mocker() as m:
        m.post(TASKS, json=_ok({"taskId": "gen-6", "status": "running"}))
        m.get(
            task_url,
            status_code=502,
            json=_err(502, "no price", details={"reason": "param_price_not_found"}, error_code="502"),
        )
        with pytest.raises(DofeAPIError):
            _client().submit_and_collect({"model": "x"}, timeout_seconds=60, poll_interval=1)
    assert _count(m, "GET", task_url) == 1  # not retried


def test_create_does_not_retry_on_5xx():
    # Create-task is never retried to avoid double charging (dev-guide §6.1).
    with _rm.Mocker() as m:
        m.post(TASKS, [
            {"status_code": 500, "json": _err(500, "boom")},
            {"json": _ok(_succeeded_task())},
        ])
        with pytest.raises(DofeAPIError):
            _client().create_task({"model": "x"})
    assert _count(m, "POST", TASKS) == 1


# --------------------------------------------------------------- no-retry 4xx

def test_401_raises_auth_no_retry():
    with _rm.Mocker() as m:
        m.get(f"{TASKS}/gen-x", status_code=401, json=_err(401, "bad key"))
        with pytest.raises(DofeAuthError):
            _client().get_task("gen-x")
    assert _count(m, "GET", f"{TASKS}/gen-x") == 1


def test_402_raises_quota_no_retry():
    with _rm.Mocker() as m:
        m.post(TASKS, status_code=402, json=_err(402, "no balance"))
        with pytest.raises(DofeQuotaError):
            _client().create_task({"model": "x"})


def test_404_raises_model_unavailable_no_retry():
    with _rm.Mocker() as m:
        m.post(TASKS, status_code=404, json=_err(404, "alias not found"))
        with pytest.raises(DofeModelUnavailableError):
            _client().create_task({"model": "ghost-9"})


# ------------------------------------------------------------- poll terminals

def test_poll_failed_raises_task_failed():
    task_url = f"{TASKS}/gen-7"
    with _rm.Mocker() as m:
        m.post(TASKS, json=_ok({"taskId": "gen-7", "status": "running"}))
        m.get(task_url, json=_ok({"taskId": "gen-7", "status": "failed", "errorCode": "render_error", "errorMessage": "gpu"}))
        with pytest.raises(DofeTaskFailedError) as exc:
            _client().submit_and_collect({"model": "x"}, timeout_seconds=60, poll_interval=1)
    assert exc.value.task_id == "gen-7"
    assert exc.value.error_code == "render_error"


def test_poll_timeout_cancels_and_raises():
    task_url = f"{TASKS}/gen-8"
    with _rm.Mocker() as m:
        m.post(TASKS, json=_ok({"taskId": "gen-8", "status": "running"}))
        m.get(task_url, json=_ok({"taskId": "gen-8", "status": "running"}))
        m.post(f"{task_url}/cancel", json=_ok({"taskId": "gen-8", "status": "cancelled"}))
        with pytest.raises(DofeTaskTimeoutError) as exc:
            _client().submit_and_collect({"model": "x"}, timeout_seconds=0, poll_interval=1)
    assert exc.value.task_id == "gen-8"
    assert _count(m, "POST", f"{task_url}/cancel") == 1


def test_cancel_terminal_409_swallowed():
    task_url = f"{TASKS}/gen-10"
    with _rm.Mocker() as m:
        m.post(f"{task_url}/cancel", status_code=409, json=_err(409, "Task already in terminal state: succeeded"))
        _client().cancel_task("gen-10")  # must not raise


# -------------------------------------------------------------- envelope edge

def test_envelope_anomaly_http200_bad_code_raises():
    with _rm.Mocker() as m:
        m.post(TASKS, status_code=200, json=_err(500, "internal in envelope"))  # HTTP 200 but code 500
        with pytest.raises(DofeAPIError):
            _client().create_task({"model": "x"})


def test_missing_task_id_raises():
    with _rm.Mocker() as m:
        m.post(TASKS, json=_ok({"status": "succeeded"}))  # no taskId
        with pytest.raises(DofeAPIError):
            _client().create_task({"model": "x"})


# ------------------------------------------------------------------ downloads

def test_download_success(tmp_path):
    with _rm.Mocker() as m:
        m.get(ARTIFACT_URL, content=b"\x89PNG\r\n\x1a\n" + b"x" * 2048)
        out = _client().download(ARTIFACT_URL, tmp_path / "img.png")
    assert out.read_bytes().startswith(b"\x89PNG")
    assert out.stat().st_size >= 1024


def test_download_rejects_non_https(tmp_path):
    with pytest.raises(DofeError):
        _client().download("http://evil.test/x.png", tmp_path / "img.png")


def test_download_failure_raises(tmp_path):
    with _rm.Mocker() as m:
        m.get(ARTIFACT_URL, status_code=503)
        with pytest.raises(DofeError):
            _client().download(ARTIFACT_URL, tmp_path / "img.png")
def test_presigned_artifact_url_is_redacted_for_tool_results():
    url = "https://assets.example/video.mp4?X-Tos-Credential=temporary&X-Tos-Signature=secret"
    assert _credential_free_url(url) == "https://assets.example/video.mp4"
