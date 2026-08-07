"""Loopback proxy that signs Agent CLI model requests with Job delegation identity."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4
from weakref import WeakValueDictionary
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
# OpenAI Responses SSE streams are only complete once they emit a terminal
# marker — the response.completed event or the [DONE] sentinel. A stream that
# ends (upstream closes) without one is TRUNCATED and must not be cached as a
# success (see KB-001 / _forward). Scanned as a rolling window so a marker split
# across read1 boundaries is still detected.
_SSE_TERMINAL_MARKERS = (b"response.completed", b"[DONE]")
_SSE_MARKER_SCAN_WINDOW = 64
_LOGGER = logging.getLogger("openmontage.delegation_proxy")
# The Codex CLI revision the KB-001 analysis below was verified against. This is
# the single code-side source of that version: it is kept in sync with
# Dockerfile's CODEX_CLI_VERSION (the canonical build-time pin) and with
# docs/DOCKER_AND_AGENTS.md by test_codex_version_pin_is_the_single_source.
# Bumping the pin MUST trigger re-verification of the per-call-identity claim in
# the KB-001 comment, and a re-run of the capability probe.
PINNED_CODEX_CLI_VERSION = "0.146.0"
# ---------------------------------------------------------------------------
# KB-001 — Responses same-content wrong-merge is MITIGATED (not an open bug).
# Referenceable tracking: docs/KNOWN_BLOCKERS.md#kb-001. Audited probe state:
# docs/codex_capability_probe.json; enforced by
# tests/openmontage/test_codex_capability_probe.py.
#
# Codex emits no OpenMontage-specific logical-call header, and its own
# X-Request-Id / Idempotency-Key are regenerated per ephemeral session, so
# honoring them would give every crash-restart a fresh invocation id and defeat
# replay recovery. With no caller-supplied stable per-call identity, the proxy
# keys replay on the content fingerprint. That ALONE would wrong-merge two
# genuinely distinct same-content calls within one stage/attempt (the second
# would replay the first cached response, losing its execution/billing/
# attribution). The per-instance _locally_served guard in _forward closes that:
#
#   * a same-content call re-arriving SEQUENTIALLY within ONE live proxy
#     instance is treated as a distinct call — forwarded again with its own
#     invocation id, never collapsed onto the first;
#   * a same-content call re-arriving in a NEW instance (worker restart)
#     replays the persisted response from the durable ledger (recovery, no
#     re-bill), because each instance starts with an empty _locally_served;
#   * CONCURRENT in-flight retries still dedup on the content fingerprint —
#     the sibling has already committed to the shared seed before _locally_served
#     is marked at serve completion.
#
# _locally_served is marked ONLY on a committed success — a cached successful
# response (or a replay of one). A FAILED forward (upstream error, exception,
# or a TRUNCATED SSE stream — one that closed without its response.completed /
# [DONE] terminal marker) marks nothing, so an in-instance retry of that same
# logical call reuses the same content-keyed seed and the same invocation id
# (no double-billing, no split attribution). A truncated stream is also marked
# failed (not cached), so a restart recovers by re-forwarding instead of
# replaying a broken response forever.
#
# Callers that CAN supply a stable identity (native tool paths) set
# X-OpenMontage-Logical-Call-Id, which is used strictly and never falls back.
#
# Residual edge (concurrent): two CONCURRENT same-content arrivals AFTER the
# instance already served that content both forward (each gets a distinct seed).
# This is the conservative choice — prefer a possible double-bill over any
# wrong-merge — and is far rarer than the sequential distinct calls the guard
# now handles.
#
# Residual edge (restart): within one instance, the 2nd..Nth distinct same-
# content call gets a random ::distinct:: uuid seed that cannot be re-derived
# after a restart. So crash-restart recovery replays the 1st such call from the
# ledger but RE-FORWARDS the 2nd..Nth (a re-bill for those), since no caller
# re-supplies that random seed. Both residuals are inherent to having no native
# per-call identity; a native Codex per-call identity removes the guard (and
# both residuals) entirely.
# A native Codex per-call identity (per-call header interpolation or a stable
# per-call Idempotency-Key) would remove the need for the _locally_served
# heuristic entirely; the capability probe tracks when that arrives. Verified
# against codex-cli == PINNED_CODEX_CLI_VERSION: ModelProviderInfo exposes only
# base_url / query_params / env_key / wire_api / auth flags — no per-request
# header field (http_headers / env_http_headers exist only for HTTP MCP servers,
# resolved once executor-side) and no model-request-level Idempotency-Key.
_LOGICAL_CALL_HEADERS = (
    "X-OpenMontage-Logical-Call-Id",
)


def _request_fingerprint(method: str, path: str, body: bytes | None, content_type: str) -> str:
    normalized_body = body or b""
    if normalized_body and "json" in content_type.lower():
        try:
            parsed = json.loads(normalized_body)
            if _is_responses_path(path) and isinstance(parsed, dict):
                # Volatile fields are stripped so the fingerprint is a pure
                # function of the durable request content — the replay key
                # described in the logical-call identity policy above.
                # prompt_cache_key / client_metadata (cache key, thread/turn ids,
                # turn timestamps) are regenerated by Codex per ephemeral
                # session; leaving them in would change the fingerprint across a
                # crash-restart and a same-content replay would mint a new
                # invocation instead of recovering the cached response.
                parsed = {
                    key: value
                    for key, value in parsed.items()
                    if key not in {"prompt_cache_key", "client_metadata"}
                }
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


def _is_responses_path(path: str) -> bool:
    return urlparse(path).path.rstrip("/").endswith("/responses")


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
        self._invocation_locks_guard = Lock()
        self._invocation_locks: WeakValueDictionary[str, Lock] = WeakValueDictionary()
        # Content fingerprints this live proxy instance has already served a
        # response for. A same-content call re-arriving SEQUENTIALLY within one
        # live instance is a distinct call (forwarded again), not a replay — see
        # KB-001. A call re-arriving in a NEW instance (worker restart) still
        # replays from the durable ledger, because each instance starts empty.
        # Concurrent in-flight retries also still dedup: _locally_served is only
        # marked after a response has been served, by which point a concurrent
        # sibling has already committed to the shared (content-keyed) seed.
        self._locally_served: set[str] = set()
        self._served_lock = Lock()

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
        response_started = False
        invocation_lock: Lock | None = None
        lock_stack = ExitStack()
        upstream: requests.Response | None = None
        content_keyed = False
        served_content = False
        fingerprint = ""
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
            logical_call_id = next(
                (
                    value
                    for name in _LOGICAL_CALL_HEADERS
                    if (value := handler.headers.get(name, "").strip())
                ),
                "",
            )
            # Seed selection — see KB-001 and the logical-call identity policy
            # above. With a caller-supplied logical call id, dedup strictly on
            # it (the caller asserts the id is stable across retries of one call
            # and unique across distinct calls). Without one, the content
            # fingerprint is the durable key — BUT only for the FIRST serving of
            # that content in this live instance. A second same-content arrival
            # in the SAME instance is a distinct call: give it a unique seed so
            # it forwards as its own invocation instead of collapsing onto the
            # first (the wrong-merge). Cross-instance restart recovery still
            # dedups (a new instance starts with an empty _locally_served), as
            # do concurrent in-flight retries (the shared seed is committed
            # before _locally_served is marked at serve completion).
            if logical_call_id:
                seed = logical_call_id
                replay_key_source = "logical_call_id"
            else:
                with self._served_lock:
                    already_served = fingerprint in self._locally_served
                if already_served:
                    seed = f"{fingerprint}::distinct::{uuid4().hex}"
                    replay_key_source = "content_fingerprint"
                else:
                    seed = fingerprint
                    content_keyed = True
                    replay_key_source = "content_fingerprint"
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
                invocation_lock = self._get_invocation_lock(invocation_id)
                invocation_lock.acquire()
                lock_stack.callback(invocation_lock.release)
                lock_stack.enter_context(
                    self.invocation_store.invocation_lock(invocation_id)
                )
                record = self.invocation_store.get_or_create(
                    job_id=self.job_id,
                    stage=self.stage,
                    attempt=self.stage_attempt,
                    request_id=seed,
                    request_fingerprint=fingerprint,
                )
                cached = self.invocation_store.get_cached_response(invocation_id)
                if cached is not None:
                    response_cached = True
                    # A replay also resolves this content for the instance: a
                    # later same-content arrival in the same instance is again a
                    # distinct call, not another replay.
                    served_content = content_keyed
                    self._log_replay(
                        invocation_id,
                        replay_key_source=replay_key_source,
                        outcome="replayed_cached",
                    )
                    _send_response(handler, cached.status_code, cached.headers, cached.body)
                    return
                if record.status == "succeeded":
                    response_cached = True
                    self._log_replay(
                        invocation_id,
                        replay_key_source=replay_key_source,
                        outcome="not_replayable_409",
                    )
                    _send_response(
                        handler,
                        409,
                        {"Content-Type": "application/json"},
                        json.dumps(
                            {
                                "error": {
                                    "code": "OPENMONTAGE_RESPONSE_NOT_REPLAYABLE",
                                    "message": "The successful model response exceeded the replay cache limit",
                                }
                            },
                            separators=(",", ":"),
                        ).encode("utf-8"),
                    )
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
            # served_content is set ONLY when a content-keyed forward reaches a
            # committed success below (a cached success, or a replay of one) —
            # see KB-001. A FAILED forward (upstream error or a truncated SSE
            # stream) must NOT mark the content served, so an in-instance retry
            # reuses the same content-keyed seed (same invocation id) instead of
            # minting a distinct one — which would split one logical call across
            # two ids (double-billing + broken attribution). content_keyed is
            # False for logical/distinct seeds, so only the content-keyed path
            # can record the fingerprint.
            response_headers = {
                key: value
                for key, value in upstream.headers.items()
                if key.lower() not in _HOP_HEADERS
            }
            if upstream.headers.get("Content-Type", "").lower().split(";", 1)[0].strip() == (
                "text/event-stream"
            ):
                handler.send_response(upstream.status_code)
                for key, value in response_headers.items():
                    handler.send_header(key, value)
                handler.send_header("Connection", "close")
                handler.end_headers()
                response_started = True
                response_body = bytearray()
                can_buffer = True
                downstream_open = True
                seen_completed = False
                scan_tail = b""
                while True:
                    chunk = upstream.raw.read1(64 * 1024, decode_content=False)
                    if not chunk:
                        break
                    if not seen_completed:
                        scan_window = scan_tail + chunk
                        if any(
                            marker in scan_window
                            for marker in _SSE_TERMINAL_MARKERS
                        ):
                            seen_completed = True
                        scan_tail = scan_window[-_SSE_MARKER_SCAN_WINDOW:]
                    if can_buffer:
                        if len(response_body) + len(chunk) <= _MAX_CACHED_RESPONSE_BYTES:
                            response_body.extend(chunk)
                        else:
                            can_buffer = False
                            response_body.clear()
                    if downstream_open:
                        try:
                            handler.wfile.write(chunk)
                            handler.wfile.flush()
                        except OSError:
                            downstream_open = False
                if self.invocation_store is not None:
                    # Only a stream that reached its terminal marker is a
                    # success. A stream that ended without one is truncated —
                    # cache nothing and mark failed so a restart retries instead
                    # of replaying a broken response forever.
                    stream_succeeded = seen_completed and upstream.status_code < 400
                    if can_buffer:
                        if stream_succeeded:
                            self.invocation_store.save_response(
                                invocation_id,
                                status_code=upstream.status_code,
                                headers=response_headers,
                                body=bytes(response_body),
                            )
                            response_cached = True
                            served_content = content_keyed
                        else:
                            self.invocation_store.mark(invocation_id, "failed")
                    else:
                        self.invocation_store.mark(
                            invocation_id,
                            "succeeded" if stream_succeeded else "failed",
                        )
                return
            content_length = upstream.headers.get("Content-Length", "")
            can_buffer = True
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
                if self.invocation_store is not None:
                    self.invocation_store.save_response(
                        invocation_id,
                        status_code=upstream.status_code,
                        headers=response_headers,
                        body=response_body,
                    )
                    response_cached = True
                    # Only a successful (cacheable) response commits the content
                    # for this instance — a failed response leaves the content
                    # unmarked so a retry reuses the same invocation id.
                    if upstream.status_code < 400:
                        served_content = content_keyed
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
            if self.invocation_store is not None and not can_buffer:
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
            if response_started:
                handler.close_connection = True
            else:
                handler.send_error(502)
        finally:
            if upstream is not None:
                upstream.close()
            if content_keyed and served_content:
                with self._served_lock:
                    self._locally_served.add(fingerprint)
            lock_stack.close()

    def _get_invocation_lock(self, invocation_id: str) -> Lock:
        with self._invocation_locks_guard:
            lock = self._invocation_locks.get(invocation_id)
            if lock is None:
                lock = Lock()
                self._invocation_locks[invocation_id] = lock
            return lock

    def _log_replay(
        self,
        invocation_id: str,
        *,
        replay_key_source: str,
        outcome: str,
    ) -> None:
        """Emit a structured record when a cached/deduped response is served.

        replay_key_source distinguishes the safe dedup case (a caller-supplied
        logical call id) from the wrong-merge-prone fallback (content
        fingerprint), so the documented limitation is observable rather than
        silent. See the logical-call identity policy above.
        """
        _LOGGER.info(
            "openmontage delegation replay served",
            extra={
                "event": "replay_served",
                "outcome": outcome,
                "job_id": self.job_id,
                "stage": self.stage,
                "attempt": self.stage_attempt,
                "invocation_id": invocation_id,
                "replay_key_source": replay_key_source,
            },
        )
