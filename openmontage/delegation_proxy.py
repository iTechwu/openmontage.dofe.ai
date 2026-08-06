"""Loopback proxy that signs Agent CLI model requests with Job delegation identity."""

from __future__ import annotations

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from urllib.parse import urlparse
import requests

from openmontage.invocation_store import InvocationRequestConflictError, ModelInvocationStore
from tools.dofe.delegation import DelegatedModelCredential


_HOP_HEADERS = {
    "authorization",
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_MAX_REQUEST_BYTES = 32 * 1024 * 1024
_MAX_CACHED_RESPONSE_BYTES = 8 * 1024 * 1024
_LOGICAL_CALL_HEADERS = (
    "X-OpenMontage-Logical-Call-Id",
    "Idempotency-Key",
    "X-Request-Id",
)


def _request_fingerprint(method: str, path: str, body: bytes | None, content_type: str) -> str:
    normalized_body = body or b""
    if normalized_body and "json" in content_type.lower():
        try:
            parsed = json.loads(normalized_body)
            normalized_body = json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    digest = hashlib.sha256()
    digest.update(method.upper().encode("ascii"))
    digest.update(b"\n")
    digest.update(path.encode("utf-8"))
    digest.update(b"\n")
    digest.update(normalized_body)
    return digest.hexdigest()


def _send_response(
    handler: BaseHTTPRequestHandler,
    status_code: int,
    headers: dict[str, str],
    body: bytes,
) -> None:
    handler.send_response(status_code)
    for key, value in headers.items():
        if key.lower() not in _HOP_HEADERS:
            handler.send_header(key, value)
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


class DelegationSigningProxy:
    def __init__(
        self,
        credential: DelegatedModelCredential,
        *,
        invocation_store: ModelInvocationStore | None = None,
        job_id: str | None = None,
        stage: str | None = None,
        stage_attempt: int = 1,
    ) -> None:
        self.credential = credential
        self.invocation_store = invocation_store
        self.job_id = job_id or credential.external_job_id
        self.stage = stage or credential.pipeline_stage
        self.stage_attempt = stage_attempt
        parsed = urlparse(credential.models_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Delegated models base URL is invalid")
        self._upstream_origin = f"{parsed.scheme}://{parsed.netloc}"
        self._upstream_prefix = parsed.path.rstrip("/")
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    def __enter__(self) -> "DelegationSigningProxy":
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                proxy._forward(self)

            def do_POST(self) -> None:  # noqa: N802
                proxy._forward(self)

            def do_DELETE(self) -> None:  # noqa: N802
                proxy._forward(self)

            def log_message(self, _format: str, *_args: Any) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, name="openmontage-model-proxy", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("Delegation signing proxy has not started")
        host, port = self._server.server_address
        return f"http://{host}:{port}{self._upstream_prefix}"

    def _forward(self, handler: BaseHTTPRequestHandler) -> None:
        response_cached = False
        try:
            length = int(handler.headers.get("Content-Length", "0"))
            if length < 0 or length > _MAX_REQUEST_BYTES:
                handler.send_error(413)
                return
            body = handler.rfile.read(length) if length else None
            fingerprint = _request_fingerprint(
                handler.command,
                handler.path,
                body,
                handler.headers.get("Content-Type", ""),
            )
            seed = next(
                (
                    value
                    for name in _LOGICAL_CALL_HEADERS
                    if (value := handler.headers.get(name, "").strip())
                ),
                fingerprint,
            )
            if self.invocation_store is None:
                invocation_id = "om-" + hashlib.sha256(
                    f"{self.credential.external_job_id}\n{self.credential.pipeline_stage}\n{seed}".encode()
                ).hexdigest()[:32]
            else:
                record = self.invocation_store.get_or_create(
                    job_id=self.job_id,
                    stage=self.stage,
                    attempt=self.stage_attempt,
                    request_id=seed,
                    request_fingerprint=fingerprint,
                )
                invocation_id = record.model_invocation_id
                cached = self.invocation_store.get_cached_response(invocation_id)
                if cached is not None:
                    _send_response(handler, cached.status_code, cached.headers, cached.body)
                    return
                self.invocation_store.mark(invocation_id, "in_flight")
            headers = {
                key: value
                for key, value in handler.headers.items()
                if key.lower() not in _HOP_HEADERS
            }
            headers["Authorization"] = f"Bearer {self.credential.api_key}"
            headers.update(self.credential.signed_headers(model_invocation_id=invocation_id))
            upstream = requests.request(
                handler.command,
                f"{self._upstream_origin}{handler.path}",
                headers=headers,
                data=body,
                stream=True,
                allow_redirects=False,
                timeout=(10, 3600),
            )
            response_headers = {
                key: value
                for key, value in upstream.headers.items()
                if key.lower() not in _HOP_HEADERS
            }
            content_type = upstream.headers.get("Content-Type", "").lower()
            content_length = upstream.headers.get("Content-Length", "")
            can_buffer = "text/event-stream" not in content_type
            if content_length.isdigit() and int(content_length) > _MAX_CACHED_RESPONSE_BYTES:
                can_buffer = False
            if can_buffer:
                response_body = upstream.raw.read(
                    _MAX_CACHED_RESPONSE_BYTES + 1,
                    decode_content=False,
                )
                can_buffer = len(response_body) <= _MAX_CACHED_RESPONSE_BYTES
            else:
                response_body = b""
            if can_buffer:
                if self.invocation_store is not None and upstream.status_code < 400:
                    self.invocation_store.save_response(
                        invocation_id,
                        status_code=upstream.status_code,
                        headers=response_headers,
                        body=response_body,
                    )
                    response_cached = True
                _send_response(handler, upstream.status_code, response_headers, response_body)
            else:
                handler.send_response(upstream.status_code)
                for key, value in response_headers.items():
                    handler.send_header(key, value)
                handler.send_header("Connection", "close")
                handler.end_headers()
                if response_body:
                    handler.wfile.write(response_body)
                for chunk in upstream.raw.stream(64 * 1024, decode_content=False):
                    handler.wfile.write(chunk)
            upstream.close()
            if self.invocation_store is not None:
                cached_success = upstream.status_code < 400 and can_buffer
                if not cached_success:
                    self.invocation_store.mark(
                        invocation_id,
                        "succeeded" if upstream.status_code < 400 else "failed",
                    )
        except InvocationRequestConflictError as exc:
            handler.send_error(409, str(exc))
        except Exception:
            if (
                self.invocation_store is not None
                and "invocation_id" in locals()
                and not response_cached
            ):
                self.invocation_store.mark(invocation_id, "unknown")
            handler.send_error(502)
