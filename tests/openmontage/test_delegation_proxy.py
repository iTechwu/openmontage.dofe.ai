from __future__ import annotations

import gc
import hashlib
import hmac
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import requests

import openmontage.delegation_proxy as delegation_proxy
from openmontage.delegation_proxy import DelegationSigningProxy
from openmontage.invocation_store import ModelInvocationStore
from tools.dofe.delegation import DelegatedModelCredential


def test_invocation_locks_are_released_from_process_registry() -> None:
    credential = DelegatedModelCredential(
        api_key="delegated-api-key",
        models_base_url="https://models.example.test/api",
        delegation_id="delegation-1",
        external_job_id="job-1",
        pipeline_stage="research",
        runtime_credential_id="runtime-credential-1",
        expires_at="2026-08-06T09:00:01Z",
    )
    proxy = DelegationSigningProxy(credential)

    lock = proxy._get_invocation_lock("invocation-1")
    assert len(proxy._invocation_locks) == 1

    del lock
    gc.collect()

    assert len(proxy._invocation_locks) == 0


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


def test_invocation_status_transition_clears_stale_response_cache(tmp_path) -> None:
    store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
    record = store.get_or_create(
        job_id="job-cache-transition",
        stage="script",
        attempt=1,
        request_id="stable-request",
        request_fingerprint="fingerprint",
    )
    store.save_response(
        record.model_invocation_id,
        status_code=504,
        headers={"Content-Type": "application/json"},
        body=b'{"error":"timeout"}',
    )
    store.mark(record.model_invocation_id, "succeeded")

    assert store.get_cached_response(record.model_invocation_id) is None


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


def test_proxy_reuses_responses_invocation_across_codex_process_metadata(tmp_path) -> None:
    forwarded: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            forwarded.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            body = b'{"id":"resp-stable","output":[]}'
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
            external_job_id="job-codex-restart",
            pipeline_stage="idea",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        responses: list[requests.Response] = []
        for session_id in ("codex-session-before-crash", "codex-session-after-restart"):
            with DelegationSigningProxy(
                credential,
                invocation_store=store,
                stage_attempt=1,
            ) as proxy:
                responses.append(requests.post(
                    f"{proxy.base_url}/v1/responses",
                    json={
                        "model": "catalog-model",
                        "instructions": "execute the durable idea stage",
                        "input": [{"role": "user", "content": "same assignment"}],
                        "stream": True,
                        "prompt_cache_key": session_id,
                        "client_metadata": {
                            "thread_id": "shared-thread",
                            "turn_id": "shared-thread-turn",
                            "turn_started_at_unix_ms": 1,
                        },
                    },
                    timeout=5,
                ))
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].content == responses[1].content
    assert len(forwarded) == 1


def test_proxy_does_not_merge_responses_with_different_client_metadata(tmp_path) -> None:
    forwarded: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            forwarded.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            body = b'{"id":"resp-stable","output":[]}'
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
            external_job_id="job-codex-metadata",
            pipeline_stage="idea",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        responses: list[requests.Response] = []
        for session_id in ("codex-session-a", "codex-session-b"):
            with DelegationSigningProxy(
                credential,
                invocation_store=store,
                stage_attempt=1,
            ) as proxy:
                responses.append(requests.post(
                    f"{proxy.base_url}/v1/responses",
                    json={
                        "model": "catalog-model",
                        "instructions": "execute the durable idea stage",
                        "input": [{"role": "user", "content": "same assignment"}],
                        "stream": True,
                        "prompt_cache_key": "shared-cache-key",
                        "client_metadata": {
                            "thread_id": session_id,
                            "turn_id": f"{session_id}-turn",
                            "turn_started_at_unix_ms": 1 if session_id.endswith("a") else 2,
                        },
                    },
                    timeout=5,
                ))
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert [response.status_code for response in responses] == [200, 200]
    assert len(forwarded) == 2


def test_proxy_keeps_cached_success_when_downstream_disconnects(monkeypatch, tmp_path) -> None:
    forwarded = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal forwarded
            forwarded += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = b'{"taskId":"task-accepted"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    original_send_response = delegation_proxy._send_response
    send_attempts = 0

    def disconnect_once(*args, **kwargs):
        nonlocal send_attempts
        send_attempts += 1
        if send_attempts == 1:
            raise BrokenPipeError("simulated downstream disconnect")
        return original_send_response(*args, **kwargs)

    monkeypatch.setattr(delegation_proxy, "_send_response", disconnect_once)
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = upstream.server_address
        credential = DelegatedModelCredential(
            api_key="delegated-api-key",
            models_base_url=f"http://{host}:{port}/api",
            delegation_id="delegation-1",
            external_job_id="job-disconnect",
            pipeline_stage="assets",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        with DelegationSigningProxy(credential, invocation_store=store) as proxy:
            headers = {"X-OpenMontage-Logical-Call-Id": "accepted-call"}
            with requests.Session() as session:
                try:
                    session.post(
                        f"{proxy.base_url}/v1/generation/tasks",
                        headers=headers,
                        json={"model": "seedream-5.0", "prompt": "scene"},
                        timeout=5,
                    )
                except requests.RequestException:
                    pass
                replay = session.post(
                    f"{proxy.base_url}/v1/generation/tasks",
                    headers=headers,
                    json={"model": "seedream-5.0", "prompt": "scene"},
                    timeout=5,
                )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert replay.status_code == 200
    assert replay.json() == {"taskId": "task-accepted"}
    assert forwarded == 1
    assert store.list_recoverable(job_id="job-disconnect") == []


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


def test_proxy_replays_persisted_event_stream_after_restart(tmp_path) -> None:
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

    assert forwarded == 1


def test_proxy_forwards_event_stream_before_upstream_completes(tmp_path) -> None:
    upstream_started = Event()
    finish_upstream = Event()
    downstream_received = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            upstream_started.set()
            assert finish_upstream.wait(timeout=5)
            self.wfile.write(b"data: done\n\n")
            self.wfile.flush()

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
            external_job_id="job-live-stream",
            pipeline_stage="script",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")

        def consume_stream(base_url: str) -> bytes:
            response = requests.post(
                f"{base_url}/v1/responses",
                headers={"X-Request-Id": "live-stream-request"},
                json={"model": "gpt-test", "stream": True},
                stream=True,
                timeout=5,
            )
            chunks = response.iter_content(chunk_size=1)
            first = next(chunks)
            downstream_received.set()
            return first + b"".join(chunks)

        with DelegationSigningProxy(credential, invocation_store=store) as proxy:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(consume_stream, proxy.base_url)
                assert upstream_started.wait(timeout=1)
                try:
                    assert downstream_received.wait(timeout=1)
                finally:
                    finish_upstream.set()
                body = future.result(timeout=5)
    finally:
        finish_upstream.set()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert body == b"data: first\n\ndata: done\n\n"


def test_proxy_does_not_repeat_uncacheable_successful_event_stream(
    monkeypatch,
    tmp_path,
) -> None:
    forwarded = 0
    body = b"data: oversized\n\n"
    monkeypatch.setattr(delegation_proxy, "_MAX_CACHED_RESPONSE_BYTES", 8)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal forwarded
            forwarded += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
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
            external_job_id="job-large-stream",
            pipeline_stage="script",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        responses = []
        for _ in range(2):
            with DelegationSigningProxy(credential, invocation_store=store) as proxy:
                responses.append(
                    requests.post(
                        f"{proxy.base_url}/v1/responses",
                        headers={"X-Request-Id": "large-stream-request"},
                        json={"model": "gpt-test", "stream": True},
                        timeout=5,
                    )
                )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert responses[0].status_code == 200
    assert responses[0].content == body
    assert responses[1].status_code == 409
    assert responses[1].json()["error"]["code"] == "OPENMONTAGE_RESPONSE_NOT_REPLAYABLE"
    assert forwarded == 1


def test_proxy_serializes_concurrent_replays_of_the_same_logical_call(tmp_path) -> None:
    forwarded = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal forwarded
            forwarded += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            time.sleep(0.1)
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
            external_job_id="job-concurrent",
            pipeline_stage="script",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        with DelegationSigningProxy(credential, invocation_store=store) as proxy:
            def request() -> requests.Response:
                return requests.post(
                    f"{proxy.base_url}/v1/responses",
                    headers={"X-Request-Id": "concurrent-request"},
                    json={"model": "gpt-test", "input": "hello"},
                    timeout=5,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _: request(), range(2)))
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.content for response in responses] == [b'{"ok":true}', b'{"ok":true}']
    assert forwarded == 1


def test_proxy_serializes_same_logical_call_across_proxy_instances(tmp_path) -> None:
    forwarded = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal forwarded
            forwarded += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            time.sleep(0.1)
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
            external_job_id="job-cross-proxy",
            pipeline_stage="script",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        with (
            DelegationSigningProxy(credential, invocation_store=store) as first_proxy,
            DelegationSigningProxy(credential, invocation_store=store) as second_proxy,
        ):
            def request(base_url: str) -> requests.Response:
                return requests.post(
                    f"{base_url}/v1/responses",
                    headers={"X-Request-Id": "cross-proxy-request"},
                    json={"model": "gpt-test", "input": "hello"},
                    timeout=5,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(
                    pool.map(request, [first_proxy.base_url, second_proxy.base_url])
                )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.content for response in responses] == [b'{"ok":true}', b'{"ok":true}']
    assert forwarded == 1


def test_proxy_retries_persisted_upstream_error_with_same_invocation_id(tmp_path) -> None:
    forwarded = 0
    invocation_ids: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal forwarded
            forwarded += 1
            invocation_ids.append(self.headers.get("X-Dofe-Model-Invocation-Id", ""))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            failed = forwarded == 1
            body = (
                b'{"error":{"code":"MODEL_TIMEOUT","message":"provider timed out"}}'
                if failed
                else b'{"ok":true}'
            )
            self.send_response(504 if failed else 200)
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
            external_job_id="job-error-replay",
            pipeline_stage="script",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        responses = []
        for _ in range(3):
            with DelegationSigningProxy(credential, invocation_store=store) as proxy:
                responses.append(
                    requests.post(
                        f"{proxy.base_url}/v1/responses",
                        headers={"X-Request-Id": "error-request"},
                        json={"model": "gpt-test", "input": "hello"},
                        timeout=5,
                    )
                )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert [response.status_code for response in responses] == [504, 200, 200]
    assert responses[0].json()["error"]["code"] == "MODEL_TIMEOUT"
    assert responses[1].json() == responses[2].json() == {"ok": True}
    assert forwarded == 2
    assert len(set(invocation_ids)) == 1
