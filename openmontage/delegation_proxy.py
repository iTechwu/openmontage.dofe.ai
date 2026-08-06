"""Loopback proxy that signs Agent CLI model requests with Job delegation identity."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from urllib.parse import urlparse
import requests

from openmontage.invocation_store import ModelInvocationStore
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
        try:
            length = int(handler.headers.get("Content-Length", "0"))
            if length < 0 or length > _MAX_REQUEST_BYTES:
                handler.send_error(413)
                return
            body = handler.rfile.read(length) if length else None
            incoming_request_id = handler.headers.get("X-Request-Id", "").strip()
            seed = incoming_request_id or hashlib.sha256(body or b"").hexdigest()
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
                )
                invocation_id = record.model_invocation_id
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
            handler.send_response(upstream.status_code)
            for key, value in upstream.headers.items():
                if key.lower() not in _HOP_HEADERS:
                    handler.send_header(key, value)
            handler.send_header("Connection", "close")
            handler.end_headers()
            for chunk in upstream.raw.stream(64 * 1024, decode_content=False):
                handler.wfile.write(chunk)
            upstream.close()
            if self.invocation_store is not None:
                self.invocation_store.mark(
                    invocation_id,
                    "succeeded" if upstream.status_code < 400 else "failed",
                )
        except Exception:
            if self.invocation_store is not None and "invocation_id" in locals():
                self.invocation_store.mark(invocation_id, "unknown")
            handler.send_error(502)
