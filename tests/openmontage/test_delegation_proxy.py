from __future__ import annotations

import hashlib
import hmac
import sqlite3
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


def test_invocation_ledger_recovers_same_id_after_worker_crash(tmp_path) -> None:
    store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
    first = store.get_or_create(
        job_id="job-crash",
        stage="render",
        attempt=1,
        request_id="request-crash",
    )
    store.mark(first.model_invocation_id, "unknown")

    recovered = store.get_or_create(
        job_id="job-crash",
        stage="render",
        attempt=1,
        request_id="request-crash",
    )
    assert recovered.model_invocation_id == first.model_invocation_id
    assert [item.model_invocation_id for item in store.list_recoverable(job_id="job-crash")] == [
        first.model_invocation_id,
    ]


def test_invocation_ledger_backfills_fingerprint_for_pre_migration_rows(tmp_path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(database_path) as db:
        db.execute(
            """
            CREATE TABLE openmontage_model_invocation (
                job_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                request_id TEXT NOT NULL,
                model_invocation_id TEXT NOT NULL PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (job_id, stage, attempt, request_id)
            )
            """
        )
        db.execute(
            """INSERT INTO openmontage_model_invocation VALUES
               ('job-old', 'assets', 1, 'logical-1', 'om-existing',
                'unknown', '2026-08-06T00:00:00Z', '2026-08-06T00:00:00Z')"""
        )

    record = ModelInvocationStore(database_path).get_or_create(
        job_id="job-old",
        stage="assets",
        attempt=1,
        request_id="logical-1",
        request_fingerprint="fingerprint-1",
    )

    assert record.model_invocation_id == "om-existing"
    assert record.request_fingerprint == "fingerprint-1"
    store = ModelInvocationStore(database_path)
    store.save_response(
        "om-existing",
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=b'{"taskId":"task-existing"}',
    )
    cached = store.get_cached_response("om-existing")
    assert cached is not None
    assert cached.status_code == 200
    assert cached.body == b'{"taskId":"task-existing"}'


def test_proxy_replays_persisted_success_after_restart_without_forwarding_again(tmp_path) -> None:
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
        responses: list[requests.Response] = []
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
                responses.append(response)
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert len(captured) == 1
    assert responses[0].content == responses[1].content == b'{"ok":true}'
    assert store.list_recoverable(job_id="job-1") == []


def test_proxy_rejects_reused_logical_call_id_with_different_request(tmp_path) -> None:
    forwarded: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            forwarded.append(body)
            response = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

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
            pipeline_stage="assets",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        with DelegationSigningProxy(
            credential,
            invocation_store=ModelInvocationStore(tmp_path / "jobs.sqlite3"),
        ) as proxy:
            headers = {"X-OpenMontage-Logical-Call-Id": "scene-7-image-1"}
            first = requests.post(
                f"{proxy.base_url}/v1/generation/tasks",
                headers=headers,
                json={"model": "seedream-5.0", "prompt": "first"},
                timeout=5,
            )
            second = requests.post(
                f"{proxy.base_url}/v1/generation/tasks",
                headers=headers,
                json={"model": "seedream-5.0", "prompt": "changed"},
                timeout=5,
            )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(forwarded) == 1


def test_proxy_does_not_cache_event_stream_responses(tmp_path) -> None:
    forwarded = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal forwarded
            forwarded += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = b"data: done\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
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
            external_job_id="job-stream",
            pipeline_stage="script",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        for _ in range(2):
            with DelegationSigningProxy(credential, invocation_store=store) as proxy:
                response = requests.post(
                    f"{proxy.base_url}/v1/responses",
                    headers={"X-Request-Id": "stream-request"},
                    json={"model": "gpt-test", "stream": True},
                    timeout=5,
                )
                assert response.content == b"data: done\n\n"
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert forwarded == 2
