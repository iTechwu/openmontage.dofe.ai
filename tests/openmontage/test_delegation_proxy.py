from __future__ import annotations

import gc
import gzip
import hashlib
import hmac
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread, get_ident

import pytest
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
                "Accept-Encoding",
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
                headers={
                    "Accept-Encoding": "br",
                    "Authorization": "Bearer untrusted",
                    "X-Request-Id": "codex-request-1",
                },
                json={"model": "gpt-test", "input": "hello"},
                timeout=5,
            )
        assert response.json() == {"ok": True}
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert captured["path"] == "/api/v1/responses"
    assert captured["Accept-Encoding"] == "identity"
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


def test_proxy_merges_same_content_responses_despite_volatile_client_metadata(tmp_path) -> None:
    """A crash-restart with identical request content recovers one invocation
    even when Codex regenerates its volatile client_metadata.

    ``prompt_cache_key`` and ``client_metadata`` are stripped from the content
    fingerprint because Codex regenerates them per ephemeral session. Two calls
    carrying the same durable content but different session metadata must map to
    the same invocation so the persisted response is replayed instead of
    re-billed. Genuinely different content still gets a distinct invocation
    (asserted by the second pair below).
    """
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
        # Same durable content, different volatile client_metadata → one forward.
        same_content_responses: list[requests.Response] = []
        for session_id in ("codex-session-a", "codex-session-b"):
            with DelegationSigningProxy(
                credential,
                invocation_store=store,
                stage_attempt=1,
            ) as proxy:
                same_content_responses.append(requests.post(
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
        # Different durable content (no logical-call header) → a new invocation.
        with DelegationSigningProxy(
            credential,
            invocation_store=store,
            stage_attempt=1,
        ) as proxy:
            different_content = requests.post(
                f"{proxy.base_url}/v1/responses",
                json={
                    "model": "catalog-model",
                    "instructions": "execute the durable idea stage",
                    "input": [{"role": "user", "content": "a different assignment"}],
                    "stream": True,
                    "prompt_cache_key": "shared-cache-key",
                    "client_metadata": {"thread_id": "codex-session-a"},
                },
                timeout=5,
            )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert [r.status_code for r in same_content_responses] == [200, 200]
    assert same_content_responses[0].content == same_content_responses[1].content
    assert different_content.status_code == 200
    # One forward for the recovered same-content call, one for the new content.
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


@pytest.mark.parametrize(
    "event_separator",
    (
        b"\n\n",
        b"\r\n\r\n",
        b"\r\r",
        b"\r\n\n",
        b"\n\r\n",
        b"\n\r",
        b"\r\n\r",
    ),
    ids=("lf", "crlf", "cr", "crlf-lf", "lf-crlf", "lf-cr", "crlf-cr"),
)
def test_proxy_replays_persisted_event_stream_after_restart(
    tmp_path,
    event_separator: bytes,
) -> None:
    forwarded = 0
    body = (
        b"event: response.completed\r\n"
        b'data: {"type":"response.completed",\r\n'
        b'data: "response":{"status":"completed"}}'
        + event_separator
    )

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
                assert response.content == body
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert forwarded == 1


@pytest.mark.parametrize(
    "event_separator",
    (b"\n\n", b"\r\n\r\n", b"\r\r", b"\r\n\n", b"\n\r\n", b"\n\r", b"\r\n\r"),
)
def test_sse_detector_preserves_crlf_at_every_chunk_boundary(
    event_separator: bytes,
) -> None:
    body = (
        b"event: response.completed\r\n"
        b'data: {"type":"response.completed",\r\n'
        b'data: "response":{"status":"completed"}}'
        + event_separator
    )

    for split_at in range(len(body) + 1):
        detector = delegation_proxy._SSECompletionDetector()
        detector.feed(body[:split_at])
        detector.feed(body[split_at:])
        assert detector.finish(), f"completion missed at byte split {split_at}"


def test_sse_detector_scans_large_events_incrementally() -> None:
    class CountingDetector(delegation_proxy._SSECompletionDetector):
        checks = 0

        def _line_ending_size(self, offset: int, *, final: bool) -> int | None:
            self.checks += 1
            return super()._line_ending_size(offset, final=final)

    body = b":" + (b"x" * (256 * 1024)) + b"\r\ndata: [DONE]\r\n\r\n"
    detector = CountingDetector()
    for offset in range(0, len(body), 1024):
        detector.feed(body[offset : offset + 1024])

    assert detector.finish()
    assert detector.checks <= len(body) + 1024


def test_proxy_rejects_unsupported_upstream_event_stream_encoding(tmp_path) -> None:
    captured_accept_encoding = ""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal captured_accept_encoding
            captured_accept_encoding = self.headers.get("Accept-Encoding", "")
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = b"unsupported-compressed-bytes"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Encoding", "br")
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
            external_job_id="job-unsupported-encoding",
            pipeline_stage="script",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        with DelegationSigningProxy(credential, invocation_store=store) as proxy:
            response = requests.post(
                f"{proxy.base_url}/v1/responses",
                headers={"Accept-Encoding": "br"},
                json={"model": "gpt-test", "stream": True},
                timeout=5,
            )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert captured_accept_encoding == "identity"
    assert response.status_code == 502
    assert response.headers.get("Content-Encoding") is None
    assert response.json()["error"]["code"] == (
        "OPENMONTAGE_UNSUPPORTED_CONTENT_ENCODING"
    )


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
            self.wfile.write(b"data: done\n\ndata: [DONE]\n\n")
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

    assert body == b"data: first\n\ndata: done\n\ndata: [DONE]\n\n"


def test_proxy_ends_stream_promptly_when_upstream_holds_connection_open(tmp_path) -> None:
    """Once the terminal frame is forwarded and cached, the proxy must stop
    reading, close upstream, and release the invocation lock — not block on
    HTTP EOF (bounded only by the 3600s read timeout) when an upstream keeps
    the connection open after response.completed / [DONE].

    Upstream writes the complete terminal stream, then deliberately holds the
    socket open until the test releases it. The client request must return the
    full body promptly; under the bug it would block on upstream EOF until the
    client's read timeout fires."""
    terminal_written = Event()
    release_upstream = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b"event: response.completed\n"
                b'data: {"type":"response.completed",'
                b'"response":{"status":"completed"}}\n\n'
                b"data: [DONE]\n\n"
            )
            self.wfile.flush()
            terminal_written.set()
            # Hold the connection open well past the client's read timeout.
            release_upstream.wait(timeout=30)

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
            external_job_id="job-held-open-stream",
            pipeline_stage="script",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        with DelegationSigningProxy(credential, invocation_store=store) as proxy:
            start = time.monotonic()
            response = requests.post(
                f"{proxy.base_url}/v1/responses",
                headers={"X-Request-Id": "held-open-stream-request"},
                json={"model": "gpt-test", "stream": True},
                timeout=10,
            )
            elapsed = time.monotonic() - start
            # Upstream sent the terminal marker and is still holding its socket
            # open — the proxy returned without waiting for that EOF.
            assert terminal_written.is_set()
            assert not release_upstream.is_set()
    finally:
        release_upstream.set()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)

    expected = (
        b"event: response.completed\n"
        b'data: {"type":"response.completed",'
        b'"response":{"status":"completed"}}\n\n'
        b"data: [DONE]\n\n"
    )
    assert response.status_code == 200
    assert response.content == expected
    # Milliseconds with the fix; ~10s ReadTimeout under the bug.
    assert elapsed < 5.0
    # The completed stream was cached as success and the ledger settled.
    assert store.list_recoverable(job_id="job-held-open-stream") == []


def test_proxy_advances_same_content_sequence_for_event_stream_without_store() -> None:
    invocation_ids: list[str] = []
    body = b"data: done\n\ndata: [DONE]\n\n"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            invocation_ids.append(self.headers.get("X-Dofe-Model-Invocation-Id", ""))
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
            external_job_id="job-stream-without-store",
            pipeline_stage="script",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        payload = {"model": "gpt-test", "stream": True}
        with DelegationSigningProxy(credential) as proxy:
            responses = [
                requests.post(
                    f"{proxy.base_url}/v1/responses",
                    json=payload,
                    timeout=5,
                )
                for _ in range(2)
            ]
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.content for response in responses] == [body, body]
    assert len(set(invocation_ids)) == 2


def test_proxy_does_not_repeat_uncacheable_successful_event_stream(
    monkeypatch,
    tmp_path,
) -> None:
    forwarded = 0
    body = b"data: oversized\n\ndata: [DONE]\n\n"
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
                    headers={
                        "X-OpenMontage-Logical-Call-Id": "concurrent-request"
                    },
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


def _replay_log_upstream(forwarded: list):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            forwarded.append(
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            )
            body = b'{"id":"resp-1","output":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:  # noqa: N802
            return None

    return Handler


def test_proxy_forwards_distinct_same_content_calls_separately_within_one_instance(
    tmp_path, caplog
) -> None:
    """Two genuinely distinct same-content Responses calls within ONE live proxy
    instance must both reach upstream — they are NOT collapsed onto one
    invocation.

    Previously the second was replayed from a content-fingerprint cache (the
    KB-001 wrong-merge), silently losing the second call's execution, billing,
    and attribution. Stable occurrence keys now give both calls distinct
    invocation ids, and no fingerprint replay is served."""
    forwarded: list[bytes] = []
    invocation_ids: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            forwarded.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            invocation_ids.append(self.headers.get("X-Dofe-Model-Invocation-Id", ""))
            body = b'{"id":"resp-1","output":[]}'
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
            external_job_id="job-distinct-same-content",
            pipeline_stage="idea",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        payload = {"model": "catalog-model", "input": "same assignment"}
        with DelegationSigningProxy(
            credential, invocation_store=store, stage_attempt=1
        ) as proxy:
            with caplog.at_level(logging.INFO, logger="openmontage.delegation_proxy"):
                first = requests.post(
                    f"{proxy.base_url}/v1/responses", json=payload, timeout=5
                )
                second = requests.post(
                    f"{proxy.base_url}/v1/responses", json=payload, timeout=5
                )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert first.status_code == second.status_code == 200
    assert len(forwarded) == 2  # both distinct calls reached upstream
    assert len(set(invocation_ids)) == 2  # distinct attribution per call
    assert [
        getattr(record, "replay_key_source", None)
        for record in caplog.records
        if getattr(record, "event", None) == "replay_served"
    ] == []  # no fingerprint replay — neither call collapsed onto the other


def test_proxy_forwards_concurrent_same_content_calls_as_distinct_invocations(
    tmp_path,
) -> None:
    """Independent calls must not collapse merely because they overlap in time."""
    forwarded = 0
    invocation_ids: list[str] = []
    both_proxy_requests_started = Event()

    class CoordinatedStore(ModelInvocationStore):
        def __init__(self, path) -> None:
            super().__init__(path)
            self._request_threads: set[int] = set()
            self._request_threads_lock = Lock()

        def get_or_create(self, **kwargs):
            with self._request_threads_lock:
                self._request_threads.add(get_ident())
                if len(self._request_threads) == 2:
                    both_proxy_requests_started.set()
            return super().get_or_create(**kwargs)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal forwarded
            assert both_proxy_requests_started.wait(timeout=5)
            forwarded += 1
            invocation_ids.append(self.headers.get("X-Dofe-Model-Invocation-Id", ""))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = f'{{"call":{forwarded}}}'.encode("ascii")
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
            external_job_id="job-concurrent-same-content",
            pipeline_stage="idea",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = CoordinatedStore(tmp_path / "jobs.sqlite3")
        payload = {"model": "catalog-model", "input": "same assignment"}
        with DelegationSigningProxy(
            credential, invocation_store=store, stage_attempt=1
        ) as proxy:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        requests.post,
                        f"{proxy.base_url}/v1/responses",
                        json=payload,
                        timeout=5,
                    )
                    for _ in range(2)
                ]
                responses = [future.result(timeout=5) for future in futures]
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert [response.status_code for response in responses] == [200, 200]
    assert forwarded == 2
    assert len(set(invocation_ids)) == 2


def test_proxy_replays_same_content_across_instances_but_not_within_one(tmp_path) -> None:
    """A restarted stage replays every same-content occurrence in order."""
    forwarded = 0
    invocation_ids: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal forwarded
            forwarded += 1
            invocation_ids.append(self.headers.get("X-Dofe-Model-Invocation-Id", ""))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = f'{{"call":{forwarded}}}'.encode("ascii")
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
            external_job_id="job-discrimination",
            pipeline_stage="idea",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        payload = {"model": "catalog-model", "input": "same assignment"}
        # Two distinct same-content calls within ONE live instance → both forward.
        with DelegationSigningProxy(
            credential, invocation_store=store, stage_attempt=1
        ) as proxy:
            first = requests.post(f"{proxy.base_url}/v1/responses", json=payload, timeout=5)
            second = requests.post(f"{proxy.base_url}/v1/responses", json=payload, timeout=5)
        # A NEW instance repeats the same deterministic call sequence.
        with DelegationSigningProxy(
            credential, invocation_store=store, stage_attempt=1
        ) as proxy:
            third = requests.post(f"{proxy.base_url}/v1/responses", json=payload, timeout=5)
            fourth = requests.post(f"{proxy.base_url}/v1/responses", json=payload, timeout=5)
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert [
        first.status_code,
        second.status_code,
        third.status_code,
        fourth.status_code,
    ] == [200, 200, 200, 200]
    assert [response.json() for response in (first, second)] == [
        {"call": 1},
        {"call": 2},
    ]
    assert [response.json() for response in (third, fourth)] == [
        {"call": 1},
        {"call": 2},
    ]
    # Restart recovery replays both persisted occurrences without re-billing.
    assert forwarded == 2
    assert len(invocation_ids) == 2
    assert len(set(invocation_ids)) == 2  # distinct attribution per distinct call


def test_proxy_logs_replay_keyed_on_logical_call_id(tmp_path, caplog) -> None:
    """When the caller supplies X-OpenMontage-Logical-Call-Id, a same-id replay
    is logged as keyed on the logical call id — the safe dedup case that the
    fingerprint fallback exists to approximate."""
    forwarded: list = []

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _replay_log_upstream(forwarded))
    thread = Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = upstream.server_address
        credential = DelegatedModelCredential(
            api_key="delegated-api-key",
            models_base_url=f"http://{host}:{port}/api",
            delegation_id="delegation-1",
            external_job_id="job-logical-id-replay",
            pipeline_stage="idea",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        headers = {"X-OpenMontage-Logical-Call-Id": "idea-call-1"}
        payload = {"model": "catalog-model", "input": "same assignment"}
        with DelegationSigningProxy(
            credential, invocation_store=store, stage_attempt=1
        ) as proxy:
            with caplog.at_level(logging.INFO, logger="openmontage.delegation_proxy"):
                first = requests.post(
                    f"{proxy.base_url}/v1/responses",
                    json=payload,
                    headers=headers,
                    timeout=5,
                )
                second = requests.post(
                    f"{proxy.base_url}/v1/responses",
                    json=payload,
                    headers=headers,
                    timeout=5,
                )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert len(forwarded) == 1
    sources = [
        getattr(record, "replay_key_source", None)
        for record in caplog.records
        if getattr(record, "event", None) == "replay_served"
    ]
    assert sources == ["logical_call_id"]


def test_proxy_does_not_cache_truncated_event_stream_and_recovers_on_restart(
    tmp_path,
) -> None:
    """A truncated SSE stream — upstream closes after response.created without
    the response.completed/[DONE] terminal marker — is NOT cached as success.

    Caching it would replay a broken response forever and make the call
    unrecoverable. The invocation is marked failed instead, so a restart
    forwards the call again rather than replaying the truncated body."""
    forwarded = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal forwarded
            forwarded += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: response.created\n'
                b'data: {"type":"response.created","response":'
                b'{"id":"resp-trunc","status":"in_progress"}}\n\n'
            )
            self.wfile.flush()
            if forwarded == 1:
                # Truncated: close mid-stream, no terminal marker.
                return
            self.wfile.write(
                b'event: response.completed\n'
                b'data: {"type":"response.completed","response":'
                b'{"id":"resp-trunc","status":"completed"}}\n\n'
                b'data: [DONE]\n\n'
            )
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
            external_job_id="job-truncated-stream",
            pipeline_stage="script",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        responses: list[requests.Response] = []
        for _ in range(2):
            with DelegationSigningProxy(credential, invocation_store=store) as proxy:
                responses.append(
                    requests.post(
                        f"{proxy.base_url}/v1/responses",
                        headers={"X-Request-Id": "truncated-stream-request"},
                        json={"model": "gpt-test", "stream": True},
                        timeout=5,
                    )
                )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert [response.status_code for response in responses] == [200, 200]
    # The truncated first stream was not cached as success, so the restart
    # forwarded again (recovered) instead of replaying the broken body.
    assert forwarded == 2
    assert b"response.completed" not in responses[0].content
    assert b"response.completed" in responses[1].content


def test_proxy_does_not_treat_model_text_as_event_stream_completion(tmp_path) -> None:
    forwarded = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal forwarded
            forwarded += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b"event: response.output_text.delta\n"
                b'data: {"type":"response.output_text.delta",'
                b'"delta":"response.completed"}\n\n'
            )
            if forwarded > 1:
                self.wfile.write(
                    b"event: response.completed\n"
                    b'data: {"type":"response.completed",'
                    b'"response":{"status":"completed"}}\n\n'
                )
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
            external_job_id="job-terminal-text",
            pipeline_stage="script",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        payload = {"model": "gpt-test", "stream": True}
        responses = []
        for _ in range(2):
            with DelegationSigningProxy(credential, invocation_store=store) as proxy:
                responses.append(
                    requests.post(
                        f"{proxy.base_url}/v1/responses",
                        json=payload,
                        timeout=5,
                    )
                )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert forwarded == 2
    assert b'"type":"response.completed"' not in responses[0].content
    assert b'"type":"response.completed"' in responses[1].content


def test_proxy_does_not_cache_unframed_truncated_completion_event(tmp_path) -> None:
    forwarded = 0
    truncated = (
        b"event: response.completed\n"
        b'data: {"type":"response.completed","response":'
    )
    completed = (
        b"event: response.completed\n"
        b'data: {"type":"response.completed",'
        b'"response":{"status":"completed"}}\n\n'
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal forwarded
            forwarded += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = truncated if forwarded == 1 else completed
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
            external_job_id="job-unframed-completion",
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
                        json={"model": "gpt-test", "stream": True},
                        timeout=5,
                    )
                )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert forwarded == 2
    assert responses[0].content == truncated
    assert responses[1].content == completed


def test_sse_completion_requires_valid_completed_event_data() -> None:
    completion = delegation_proxy._SSECompletionDetector()

    completion.feed(
        b"event: response.completed\n"
        b'data: {"type":"response.completed","response":\n\n'
    )

    assert completion.finish() is False


def test_proxy_decodes_compressed_event_stream_before_completion_detection(tmp_path) -> None:
    forwarded = 0
    completed = (
        b"event: response.completed\n"
        b'data: {"type":"response.completed",'
        b'"response":{"status":"completed"}}\n\n'
    )
    compressed = gzip.compress(completed)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal forwarded
            forwarded += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(compressed)))
            self.end_headers()
            self.wfile.write(compressed)

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
            external_job_id="job-compressed-stream",
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
                        json={"model": "gpt-test", "stream": True},
                        timeout=5,
                    )
                )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert forwarded == 1
    assert [response.content for response in responses] == [completed, completed]
    assert all("Content-Encoding" not in response.headers for response in responses)


def test_proxy_retries_failed_forward_with_same_invocation_id_within_one_instance(
    tmp_path,
) -> None:
    """A failed forward within ONE live instance must NOT mark the content
    served, so an in-instance retry reuses the same content-keyed seed — the
    same invocation id — instead of minting a distinct one.

    A failed occurrence stays retryable until claimed, so the 504 and its retry
    land on one invocation id instead of splitting billing and attribution."""
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
            external_job_id="job-in-instance-retry",
            pipeline_stage="script",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        with DelegationSigningProxy(credential, invocation_store=store) as proxy:
            first = requests.post(
                f"{proxy.base_url}/v1/responses",
                json={"model": "gpt-test", "input": "hello"},
                timeout=5,
            )
            second = requests.post(
                f"{proxy.base_url}/v1/responses",
                json={"model": "gpt-test", "input": "hello"},
                timeout=5,
            )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert [first.status_code, second.status_code] == [504, 200]
    assert forwarded == 2
    # Same logical call retried within one instance → one invocation id (no split).
    assert len(set(invocation_ids)) == 1


def test_proxy_retries_earlier_concurrent_failure_with_same_invocation_id(
    tmp_path,
) -> None:
    second_call_finished = Event()
    request_ids_by_invocation: dict[str, str] = {}
    upstream_invocations: dict[str, list[str]] = {"first": [], "second": []}
    first_attempts = 0

    class TrackingStore(ModelInvocationStore):
        def get_or_create(self, **kwargs):
            record = super().get_or_create(**kwargs)
            request_ids_by_invocation[record.model_invocation_id] = kwargs["request_id"]
            return record

    class CoordinatedProxy(DelegationSigningProxy):
        def _finish_content_call(self, fingerprint, ordinal, *, succeeded):
            super()._finish_content_call(
                fingerprint,
                ordinal,
                succeeded=succeeded,
            )
            if ordinal == 2 and succeeded:
                second_call_finished.set()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal first_attempts
            invocation_id = self.headers.get("X-Dofe-Model-Invocation-Id", "")
            request_id = request_ids_by_invocation[invocation_id]
            is_second = request_id.endswith("::occurrence::2")
            key = "second" if is_second else "first"
            upstream_invocations[key].append(invocation_id)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if is_second:
                status = 200
            else:
                first_attempts += 1
                if first_attempts == 1:
                    assert second_call_finished.wait(timeout=5)
                    status = 504
                else:
                    status = 200
            body = b'{"ok":true}' if status == 200 else b'{"error":"timeout"}'
            self.send_response(status)
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
            external_job_id="job-concurrent-retry",
            pipeline_stage="script",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = TrackingStore(tmp_path / "jobs.sqlite3")
        payload = {"model": "gpt-test", "input": "same"}
        with CoordinatedProxy(credential, invocation_store=store) as proxy:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        requests.post,
                        f"{proxy.base_url}/v1/responses",
                        json=payload,
                        timeout=5,
                    )
                    for _ in range(2)
                ]
                concurrent = [future.result(timeout=5) for future in futures]
            retry = requests.post(
                f"{proxy.base_url}/v1/responses",
                json=payload,
                timeout=5,
            )
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert sorted(response.status_code for response in concurrent) == [200, 504]
    assert retry.status_code == 200
    assert len(upstream_invocations["first"]) == 2
    assert len(set(upstream_invocations["first"])) == 1
    assert len(upstream_invocations["second"]) == 1


def test_proxy_reuses_pending_failed_stream_invocation_for_immediate_retry(
    tmp_path,
) -> None:
    release_failed_stream = Event()
    retry_entered_allocation = Event()
    invocation_ids: list[str] = []
    upstream_calls = 0
    allocation_calls = 0
    allocation_lock = Lock()

    class CoordinatedProxy(DelegationSigningProxy):
        def _allocate_content_call(self, fingerprint):
            nonlocal allocation_calls
            with allocation_lock:
                allocation_calls += 1
                if allocation_calls == 2:
                    retry_entered_allocation.set()
            return super()._allocate_content_call(fingerprint)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal upstream_calls
            upstream_calls += 1
            invocation_ids.append(self.headers.get("X-Dofe-Model-Invocation-Id", ""))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if upstream_calls == 1:
                self.send_response(504)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.flush()
                assert release_failed_stream.wait(timeout=5)
                return
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
            external_job_id="job-pending-stream-retry",
            pipeline_stage="script",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        payload = {"model": "gpt-test", "stream": True}
        with CoordinatedProxy(credential, invocation_store=store) as proxy:
            first = requests.post(
                f"{proxy.base_url}/v1/responses",
                json=payload,
                stream=True,
                timeout=5,
            )
            assert first.status_code == 504
            with ThreadPoolExecutor(max_workers=1) as executor:
                retry_future = executor.submit(
                    requests.post,
                    f"{proxy.base_url}/v1/responses",
                    json=payload,
                    timeout=5,
                )
                assert retry_entered_allocation.wait(timeout=1)
                retry = retry_future.result(timeout=2)
                release_failed_stream.set()
            first.close()
    finally:
        release_failed_stream.set()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert retry.status_code == 200
    assert upstream_calls == 2
    assert len(set(invocation_ids)) == 1
