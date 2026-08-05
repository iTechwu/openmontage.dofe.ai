"""AgentSpace Artifact Bridge client for Job-scoped media inputs."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from uuid import uuid4

import requests
from pydantic import ValidationError

from openmontage.contracts import (
    ArtifactMetadata,
    ArtifactReadGrant,
    ArtifactWriteGrant,
    JobAttribution,
    OutputArtifactMetadata,
    PublishedArtifact,
)


class ArtifactBridgeError(RuntimeError):
    """Raised when an Artifact grant or downloaded object is unsafe."""


@dataclass(frozen=True)
class ArtifactDownload:
    path: Path
    artifact: ArtifactMetadata


class ArtifactBridgeClient:
    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        session: Any | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 300.0,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        self.service_token = service_token.strip()
        if not self.service_token:
            raise ArtifactBridgeError("OpenMontage Artifact Bridge service token is required")
        self.session = session if session is not None else requests.Session()
        self.timeout = (connect_timeout, read_timeout)

    @classmethod
    def from_environment(cls, *, session: Any | None = None) -> "ArtifactBridgeClient":
        return cls(
            base_url=os.environ.get("OPENMONTAGE_ARTIFACT_BRIDGE_BASE_URL", ""),
            service_token=os.environ.get("OPENMONTAGE_SERVICE_TOKEN", ""),
            session=session,
        )

    def download_input(
        self,
        *,
        job_id: str,
        attribution: JobAttribution,
        artifact_id: str,
        destination_dir: str | Path,
    ) -> ArtifactDownload:
        normalized_job_id = _identifier(job_id, "job_id")
        normalized_artifact_id = _identifier(artifact_id, "artifact_id")
        grant = self._issue_read_grant(
            normalized_job_id,
            normalized_artifact_id,
            attribution,
        )
        if grant.artifact.artifact_id != normalized_artifact_id:
            raise ArtifactBridgeError("Artifact grant returned a mismatched artifact identity")
        self._validate_download_url(grant)
        file_name = _safe_file_name(grant.artifact.file_name)
        output_dir = Path(destination_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / file_name
        partial = output_dir / f".{file_name}.part-{uuid4().hex}"

        try:
            self._download_verified(grant, partial)
            os.replace(partial, destination)
        except ArtifactBridgeError:
            partial.unlink(missing_ok=True)
            raise
        except Exception as exc:
            partial.unlink(missing_ok=True)
            raise ArtifactBridgeError("Artifact download failed") from exc
        return ArtifactDownload(path=destination, artifact=grant.artifact)

    def upload_output(
        self,
        *,
        job_id: str,
        attribution: JobAttribution,
        path: str | Path,
        role: str,
        media_type: str | None = None,
    ) -> PublishedArtifact:
        normalized_job_id = _identifier(job_id, "job_id")
        output_path = Path(path).expanduser().resolve()
        if not output_path.is_file():
            raise ArtifactBridgeError("Artifact output path must be a readable file")
        file_name = _safe_file_name(output_path.name)
        size_bytes, sha256 = _hash_file(output_path)
        if size_bytes < 1:
            raise ArtifactBridgeError("Artifact output file must not be empty")
        resolved_media_type = (
            media_type.strip().lower()
            if media_type is not None
            else (mimetypes.guess_type(file_name)[0] or "application/octet-stream")
        )
        metadata = OutputArtifactMetadata(
            role=_identifier(role, "role"),
            file_name=file_name,
            media_type=resolved_media_type,
            size_bytes=size_bytes,
            sha256=sha256,
        )
        grant = self._issue_write_grant(normalized_job_id, metadata, attribution)
        if grant.artifact != metadata:
            raise ArtifactBridgeError("Artifact write grant returned mismatched output metadata")
        self._validate_upload_url(grant)

        try:
            with output_path.open("rb") as handle:
                response = self.session.put(
                    grant.upload_url,
                    headers={
                        "Authorization": f"Bearer {grant.token}",
                        "Content-Type": metadata.media_type,
                        "Content-Length": str(metadata.size_bytes),
                        "X-Content-SHA256": metadata.sha256,
                    },
                    data=handle,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                published = PublishedArtifact.model_validate(response.json())
        except (requests.RequestException, ValidationError, ValueError, TypeError) as exc:
            raise ArtifactBridgeError("Artifact upload failed") from exc
        except Exception as exc:
            raise ArtifactBridgeError("Artifact upload failed") from exc
        if (
            published.job_id != normalized_job_id
            or published.employee_id != attribution.employee_id
            or published.role != metadata.role
            or published.file_name != metadata.file_name
            or published.media_type != metadata.media_type
            or published.size_bytes != metadata.size_bytes
            or published.sha256 != metadata.sha256
        ):
            raise ArtifactBridgeError("Artifact publish manifest did not match the trusted Job output")
        return published

    def _issue_read_grant(
        self,
        job_id: str,
        artifact_id: str,
        attribution: JobAttribution,
    ) -> ArtifactReadGrant:
        endpoint = (
            f"{self.base_url}/api/internal/openmontage/jobs/"
            f"{quote(job_id, safe='')}/artifact-grants"
        )
        try:
            response = self.session.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self.service_token}",
                    "Content-Type": "application/json",
                    "X-Dofe-Job-Attribution": _encode_attribution(attribution),
                },
                json={"attachmentId": artifact_id},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return ArtifactReadGrant.model_validate(response.json())
        except (requests.RequestException, ValidationError, ValueError, TypeError) as exc:
            raise ArtifactBridgeError("Artifact grant request failed") from exc
        except Exception as exc:
            raise ArtifactBridgeError("Artifact grant request failed") from exc

    def _issue_write_grant(
        self,
        job_id: str,
        artifact: OutputArtifactMetadata,
        attribution: JobAttribution,
    ) -> ArtifactWriteGrant:
        endpoint = (
            f"{self.base_url}/api/internal/openmontage/jobs/"
            f"{quote(job_id, safe='')}/artifact-grants"
        )
        try:
            response = self.session.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self.service_token}",
                    "Content-Type": "application/json",
                    "X-Dofe-Job-Attribution": _encode_attribution(attribution),
                },
                json={"operation": "WRITE", "artifact": artifact.to_wire()},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return ArtifactWriteGrant.model_validate(response.json())
        except (requests.RequestException, ValidationError, ValueError, TypeError) as exc:
            raise ArtifactBridgeError("Artifact write grant request failed") from exc
        except Exception as exc:
            raise ArtifactBridgeError("Artifact write grant request failed") from exc

    def _validate_download_url(self, grant: ArtifactReadGrant) -> None:
        parsed = urlparse(grant.download_url)
        base = urlparse(self.base_url)
        expected_path = f"/api/internal/openmontage/artifact-grants/{quote(grant.grant_id, safe='')}"
        if (
            parsed.scheme != base.scheme
            or parsed.netloc != base.netloc
            or parsed.username
            or parsed.password
            or parsed.path != expected_path
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ArtifactBridgeError("Artifact grant returned an unsafe download URL")

    def _validate_upload_url(self, grant: ArtifactWriteGrant) -> None:
        parsed = urlparse(grant.upload_url)
        base = urlparse(self.base_url)
        expected_path = f"/api/internal/openmontage/artifact-grants/{quote(grant.grant_id, safe='')}"
        if (
            parsed.scheme != base.scheme
            or parsed.netloc != base.netloc
            or parsed.username
            or parsed.password
            or parsed.path != expected_path
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ArtifactBridgeError("Artifact grant returned an unsafe upload URL")

    def _download_verified(self, grant: ArtifactReadGrant, partial: Path) -> None:
        digest = hashlib.sha256()
        total = 0
        try:
            response_context = self.session.get(
                grant.download_url,
                headers={"Authorization": f"Bearer {grant.token}"},
                stream=True,
                allow_redirects=True,
                timeout=self.timeout,
            )
            with response_context as response:
                response.raise_for_status()
                with partial.open("xb") as handle:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > grant.artifact.size_bytes:
                            raise ArtifactBridgeError("Artifact download integrity verification failed")
                        digest.update(chunk)
                        handle.write(chunk)
        except ArtifactBridgeError:
            raise
        except Exception as exc:
            raise ArtifactBridgeError("Artifact download failed") from exc
        if total != grant.artifact.size_bytes or digest.hexdigest() != grant.artifact.sha256:
            raise ArtifactBridgeError("Artifact download integrity verification failed")


def _validate_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlparse(normalized)
    except ValueError as exc:
        raise ArtifactBridgeError("OpenMontage Artifact Bridge base URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ArtifactBridgeError("OpenMontage Artifact Bridge base URL is invalid")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ArtifactBridgeError("OpenMontage Artifact Bridge base URL must not contain a path")
    return normalized


def _identifier(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 256:
        raise ArtifactBridgeError(f"{name} is invalid")
    return normalized


def _safe_file_name(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or Path(normalized).name != normalized
    ):
        raise ArtifactBridgeError("Artifact grant returned an unsafe file name")
    return normalized


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size_bytes += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise ArtifactBridgeError("Artifact output file could not be read") from exc
    return size_bytes, digest.hexdigest()


def _encode_attribution(attribution: JobAttribution) -> str:
    payload = json.dumps(
        attribution.to_wire(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
