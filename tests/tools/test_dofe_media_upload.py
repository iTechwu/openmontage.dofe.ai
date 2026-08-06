from __future__ import annotations

import hashlib
import hmac

import pytest
import requests_mock

from tools.dofe.media_upload import DofeMediaUploadClient, DofeMediaUploadError


def test_upload_uses_hmac_and_accepts_only_dofe_transcode_temp_target(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.dofe.media_upload.time.time", lambda: 1_800_000_000)
    source = tmp_path / "reference.wav"
    source.write_bytes(b"wave")
    secret = "internal-test-secret"
    tenant_id = "00000000-0000-4000-8000-000000000001"

    with requests_mock.Mocker() as mock:
        mock.post(
            "https://models.test/internal/media/upload",
            json={
                "code": 200,
                "msg": "ok",
                "data": {
                    "url": "tos://dofe-transcode/temp/generation-assets/id-reference.wav",
                    "storageVendor": "tos",
                    "bucket": "dofe-transcode",
                    "key": "temp/generation-assets/id-reference.wav",
                    "name": "reference.wav",
                    "mimeType": "audio/x-wav",
                    "sizeBytes": 4,
                },
            },
        )
        result = DofeMediaUploadClient(
            secret=secret, tenant_id=tenant_id, base_url="https://models.test"
        ).upload(source)

    request = mock.request_history[0]
    signature = hmac.new(
        secret.encode(), b"1800000000:openmontage", hashlib.sha256
    ).hexdigest()
    assert request.headers["Authorization"] == f"Bearer 1800000000:{signature}:openmontage"
    assert request.headers["x-service-name"] == "openmontage"
    assert "multipart/form-data" in request.headers["Content-Type"]
    assert tenant_id in request.text
    assert result["url"].startswith("tos://dofe-transcode/temp/generation-assets/")


def test_upload_rejects_an_unexpected_bucket(tmp_path):
    source = tmp_path / "reference.wav"
    source.write_bytes(b"wave")

    with requests_mock.Mocker() as mock:
        mock.post(
            "https://models.test/internal/media/upload",
            json={
                "code": 200,
                "msg": "ok",
                "data": {
                    "url": "tos://dofe-system/temp/generation-assets/id-reference.wav",
                    "bucket": "dofe-system",
                    "key": "temp/generation-assets/id-reference.wav",
                },
            },
        )
        with pytest.raises(DofeMediaUploadError, match="invalid storage target"):
            DofeMediaUploadClient(
                secret="secret",
                tenant_id="00000000-0000-4000-8000-000000000001",
                base_url="https://models.test",
            ).upload(source)
