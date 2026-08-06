from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from openmontage.artifact_bridge import ArtifactBridgeClient, ArtifactBridgeError
from openmontage.contracts import JobAttribution


def _attribution() -> JobAttribution:
    return JobAttribution(
        workspace_id="ws-1",
        employee_id="employee-1",
        runtime_id="runtime-1",
        root_task_id="task-1",
        conversation_id="conversation-1",
        source_invocation_id="invocation-1",
        trace_id="trace-1",
    )


def _grant(content: bytes = b"video") -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "grantId": "om_ag_1",
        "operation": "READ",
        "downloadUrl": "http://agentspace.internal:1455/api/internal/openmontage/artifact-grants/om_ag_1",
        "token": "t" * 43,
        "expiresAt": "2026-08-05T10:05:00Z",
        "artifact": {
            "artifactId": "att-video-1",
            "fileName": "reference.mp4",
            "mediaType": "video/mp4",
            "sizeBytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    }


def _write_grant(content: bytes = b"video") -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "grantId": "om_ag_write_1",
        "operation": "WRITE",
        "uploadUrl": "http://agentspace.internal:1455/api/internal/openmontage/artifact-grants/om_ag_write_1",
        "token": "w" * 43,
        "expiresAt": "2026-08-05T10:05:00Z",
        "artifact": {
            "role": "final_video",
            "fileName": "final.mp4",
            "mediaType": "video/mp4",
            "sizeBytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    }


def _published(content: bytes = b"video") -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "jobId": "om_job_1",
        "employeeArtifactId": "eart-1",
        "employeeId": "employee-1",
        "role": "final_video",
        "fileName": "final.mp4",
        "mediaType": "video/mp4",
        "sizeBytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "publishedAt": "2026-08-05T10:00:02Z",
    }


class _Response:
    def __init__(self, *, payload: dict[str, Any] | None = None, content: bytes = b"", status: int = 200):
        self._payload = payload
        self._content = content
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload

    def iter_content(self, chunk_size: int = 64 * 1024):
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset : offset + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Session:
    def __init__(
        self,
        grant: dict[str, Any],
        content: bytes,
        published: dict[str, Any] | None = None,
    ):
        self.grant = grant
        self.content = content
        self.published = published
        self.post_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.post_calls.append({"url": url, **kwargs})
        return _Response(payload=self.grant, status=201)

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.get_calls.append({"url": url, **kwargs})
        return _Response(content=self.content)

    def put(self, url: str, **kwargs: Any) -> _Response:
        data = kwargs.pop("data")
        uploaded = data.read()
        self.put_calls.append({"url": url, "uploaded": uploaded, **kwargs})
        return _Response(payload=self.published, status=201)


def test_download_input_uses_trusted_attribution_and_atomically_verifies_bytes(tmp_path: Path) -> None:
    content = b"video"
    session = _Session(_grant(content), content)
    client = ArtifactBridgeClient(
        base_url="http://agentspace.internal:1455",
        service_token="service-token",
        session=session,
    )

    result = client.download_input(
        job_id="om_job_1",
        attribution=_attribution(),
        artifact_id="att-video-1",
        destination_dir=tmp_path / "input",
    )

    assert result.path == tmp_path / "input" / "reference.mp4"
    assert result.path.read_bytes() == content
    assert not list((tmp_path / "input").glob("*.part-*"))
    assert session.post_calls[0]["url"] == (
        "http://agentspace.internal:1455/api/internal/openmontage/jobs/om_job_1/artifact-grants"
    )
    post_headers = session.post_calls[0]["headers"]
    assert post_headers["Authorization"] == "Bearer service-token"
    decoded = json.loads(base64.urlsafe_b64decode(post_headers["X-Dofe-Job-Attribution"] + "=="))
    assert decoded["employeeId"] == "employee-1"
    assert session.get_calls[0]["headers"] == {"Authorization": f"Bearer {'t' * 43}"}
    assert session.get_calls[0]["allow_redirects"] is True


def test_download_input_rejects_tampered_bytes_and_removes_partial_file(tmp_path: Path) -> None:
    session = _Session(_grant(b"expected"), b"tampered")
    client = ArtifactBridgeClient(
        base_url="http://agentspace.internal:1455",
        service_token="service-token",
        session=session,
    )

    with pytest.raises(ArtifactBridgeError, match="integrity"):
        client.download_input(
            job_id="om_job_1",
            attribution=_attribution(),
            artifact_id="att-video-1",
            destination_dir=tmp_path / "input",
        )

    assert not (tmp_path / "input" / "reference.mp4").exists()
    assert not list((tmp_path / "input").glob("*.part-*"))


def test_download_input_rejects_mismatched_grants_and_paths(tmp_path: Path) -> None:
    wrong_artifact = _grant()
    wrong_artifact["artifact"]["artifactId"] = "att-other"
    client = ArtifactBridgeClient(
        base_url="http://agentspace.internal:1455",
        service_token="service-token",
        session=_Session(wrong_artifact, b"video"),
    )
    with pytest.raises(ArtifactBridgeError, match="artifact identity"):
        client.download_input(
            job_id="om_job_1",
            attribution=_attribution(),
            artifact_id="att-video-1",
            destination_dir=tmp_path / "input",
        )

    unsafe_name = _grant()
    unsafe_name["artifact"]["fileName"] = "../escape.mp4"
    client = ArtifactBridgeClient(
        base_url="http://agentspace.internal:1455",
        service_token="service-token",
        session=_Session(unsafe_name, b"video"),
    )
    with pytest.raises(ArtifactBridgeError, match="file name"):
        client.download_input(
            job_id="om_job_1",
            attribution=_attribution(),
            artifact_id="att-video-1",
            destination_dir=tmp_path / "input",
        )


def test_upload_output_hashes_file_and_publishes_with_one_time_grant(tmp_path: Path) -> None:
    content = b"video"
    output = tmp_path / "final.mp4"
    output.write_bytes(content)
    session = _Session(_write_grant(content), b"", _published(content))
    client = ArtifactBridgeClient(
        base_url="http://agentspace.internal:1455",
        service_token="service-token",
        session=session,
    )

    result = client.upload_output(
        job_id="om_job_1",
        attribution=_attribution(),
        path=output,
        role="final_video",
    )

    assert result.employee_artifact_id == "eart-1"
    assert result.employee_id == "employee-1"
    post = session.post_calls[0]
    assert post["json"] == {
        "operation": "WRITE",
        "artifact": {
            "role": "final_video",
            "fileName": "final.mp4",
            "mediaType": "video/mp4",
            "sizeBytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    }
    put = session.put_calls[0]
    assert put["uploaded"] == content
    assert put["headers"]["Authorization"] == f"Bearer {'w' * 43}"
    assert put["headers"]["Content-Length"] == str(len(content))


def test_upload_output_rejects_mismatched_publish_manifest(tmp_path: Path) -> None:
    content = b"video"
    output = tmp_path / "final.mp4"
    output.write_bytes(content)
    mismatched = _published(content)
    mismatched["employeeId"] = "employee-2"
    client = ArtifactBridgeClient(
        base_url="http://agentspace.internal:1455",
        service_token="service-token",
        session=_Session(_write_grant(content), b"", mismatched),
    )

    with pytest.raises(ArtifactBridgeError, match="publish manifest"):
        client.upload_output(
            job_id="om_job_1",
            attribution=_attribution(),
            path=output,
            role="final_video",
        )
