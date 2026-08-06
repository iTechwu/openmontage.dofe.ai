from __future__ import annotations

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import requests

from openmontage.delegation_proxy import DelegationSigningProxy
from openmontage.invocation_store import ModelInvocationStore
from tools.dofe.delegation import DelegatedModelCredential


def test_loopback_proxy_overwrites_auth_and_signs_each_agent_request() -> None:
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            captured["path"] = self.path
            for name in (
                "Authorization",
                "X-Dofe-Pipeline-Stage",
                "X-Dofe-Model-Invocation-Id",
                "X-Dofe-Attribution-Timestamp",
                "X-Dofe-Attribution-Signature",
            ):
                captured[name] = self.headers.get(name, "")
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = upstream.server_address
        credential = DelegatedModelCredential(
            api_key="delegated-api-key",
            models_base_url=f"http://{host}:{port}/api",
            delegation_id="delegation-1",
            external_job_id="job-1",
            pipeline_stage="research",
            runtime_credential_id="runtime-credential-1",
            expires_at="2026-08-06T09:00:01Z",
        )
        with DelegationSigningProxy(credential) as proxy:
            response = requests.post(
                f"{proxy.base_url}/v1/responses",
                headers={"Authorization": "Bearer untrusted", "X-Request-Id": "codex-request-1"},
                json={"model": "gpt-test", "input": "hello"},
                timeout=5,
            )
        assert response.json() == {"ok": True}
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert captured["path"] == "/api/v1/responses"
    assert captured["Authorization"] == "Bearer delegated-api-key"
    assert captured["X-Dofe-Pipeline-Stage"] == "research"
    invocation_id = captured["X-Dofe-Model-Invocation-Id"]
    timestamp = captured["X-Dofe-Attribution-Timestamp"]
    expected = hmac.new(
        b"delegated-api-key",
        f"delegation-1\njob-1\nresearch\n{invocation_id}\n{timestamp}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert captured["X-Dofe-Attribution-Signature"] == expected


def test_proxy_reuses_persisted_invocation_id_after_restart(tmp_path) -> None:
    captured: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            captured.append(self.headers.get("X-Dofe-Model-Invocation-Id", ""))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = upstream.server_address
        credential = DelegatedModelCredential(
            api_key="delegated-api-key",
            models_base_url=f"http://{host}:{port}/api",
            delegation_id="delegation-1",
            external_job_id="job-1",
            pipeline_stage="research",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        for _ in range(2):
            with DelegationSigningProxy(
                credential,
                invocation_store=store,
                stage_attempt=2,
            ) as proxy:
                response = requests.post(
                    f"{proxy.base_url}/v1/responses",
                    headers={"X-Request-Id": "stable-agent-request"},
                    json={"model": "gpt-test", "input": "hello"},
                    timeout=5,
                )
                assert response.status_code == 200
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert len(captured) == 2
    assert captured[0] == captured[1]
    assert store.list_recoverable(job_id="job-1") == []
