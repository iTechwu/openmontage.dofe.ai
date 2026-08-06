"""Tests for tenant-effective Airouter pricing quotes."""

from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path

import pytest
import requests_mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.dofe.pricing import DofePricingClient, DofePricingError


def test_quote_uses_hmac_and_forces_configured_tenant(monkeypatch):
    monkeypatch.setattr("tools.dofe.pricing.time.time", lambda: 1_800_000_000)
    secret = "internal-test-secret"
    tenant_id = "00000000-0000-4000-8000-000000000001"
    url = "https://models.test/internal/pricing/quote"

    with requests_mock.Mocker() as mock:
        mock.post(
            url,
            json={
                "code": 200,
                "msg": "ok",
                "data": {
                    "modelAlias": "seedance-2.0-fast",
                    "currency": "CNY",
                    "estimatedTotal": 37,
                },
            },
        )
        result = DofePricingClient(
            secret=secret, tenant_id=tenant_id, base_url="https://models.test"
        ).quote(
            {
                "tenantId": "00000000-0000-4000-8000-000000000099",
                "modelAlias": "seedance-2.0-fast",
                "outputTokens": 1_000_000,
                "pricingContext": {"hasVideoInput": False},
            }
        )

    request = mock.request_history[0]
    signature = hmac.new(
        secret.encode(), b"1800000000:openmontage", hashlib.sha256
    ).hexdigest()
    assert request.headers["Authorization"] == f"Bearer 1800000000:{signature}:openmontage"
    assert request.headers["x-service-name"] == "openmontage"
    assert request.json()["tenantId"] == tenant_id
    assert result["estimatedTotal"] == 37
    assert result["currency"] == "CNY"


def test_quote_fails_closed_without_internal_secret():
    client = DofePricingClient(secret="", tenant_id="tenant", base_url="https://models.test")
    with pytest.raises(DofePricingError, match="INTERNAL_API_SECRET"):
        client.quote({"modelAlias": "seedance-2.0-fast"})


def test_quote_fails_closed_without_tenant():
    client = DofePricingClient(secret="secret", tenant_id="", base_url="https://models.test")
    with pytest.raises(DofePricingError, match="DOFE_TENANT_ID"):
        client.quote({"modelAlias": "seedance-2.0-fast"})
