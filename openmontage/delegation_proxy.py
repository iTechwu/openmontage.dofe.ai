"""Loopback proxy that signs Agent CLI model requests with Job delegation identity."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
import hashlib
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Any
from urllib.parse import urlparse
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
_LOGGER = logging.getLogger("openmontage.delegation_proxy")
_UPSTREAM_ACCEPT_ENCODING = "identity"
_SUPPORTED_UPSTREAM_CONTENT_ENCODINGS = frozenset(
    encoding.strip().lower()
    for encoding in requests.utils.DEFAULT_ACCEPT_ENCODING.split(",")
    if encoding.strip()
) | {"identity"}
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
# Codex emits no OpenMontage-specific logical-call header, and its own request
# identity changes across ephemeral sessions. The fallback therefore combines
# the durable content fingerprint with a per-instance occurrence ordinal. The
# first occurrence keeps the legacy fingerprint key; later occurrences use
# `<fingerprint>::occurrence::<n>`. Allocation is atomic, so concurrent distinct
# calls receive different invocation ids. A restarted deterministic stage begins
# again at occurrence 1 and replays every persisted occurrence in the same order.
# Failed occurrences remain retryable until one retry claims that ordinal, so a
# sequential retry reuses the same invocation id even when a later concurrent
# occurrence completed first.
#
# Responses SSE is complete only when a fully framed event has a JSON `type` of
# `response.completed`, or an exact `data: [DONE]` sentinel. EOF before that is
# failed and never cached; generated text or a malformed event merely containing
# "response.completed" is not a terminal event. CR, LF, and CRLF event framing
# are accepted. Upstream requests use identity encoding; a server that ignores
# that negotiation is accepted only when the local HTTP decoder supports its
# declared Content-Encoding.
#
# Callers that CAN supply a stable identity (native tool paths) set
# X-OpenMontage-Logical-Call-Id, which is used strictly and never falls back.
#
# Residual edge: without a caller identity, replay assumes the same-content call
# order is deterministic across a stage restart. Concurrent retry-vs-distinct
# intent is also unknowable from identical bytes alone. Callers that need exact
# identity across reordered or hedged calls must supply the logical-call header.
# A native Codex per-call identity (per-call header interpolation or a stable
# per-call Idempotency-Key) would remove the occurrence fallback entirely; the
# capability probe tracks when that arrives. Verified
# against codex-cli == PINNED_CODEX_CLI_VERSION: ModelProviderInfo exposes only
# base_url / query_params / env_key / wire_api / auth flags — no per-request
# header field (http_headers / env_http_headers exist only for HTTP MCP servers,
# resolved once executor-side) and no model-request-level Idempotency-Key.
_LOGICAL_CALL_HEADERS = (
    "X-OpenMontage-Logical-Call-Id",
)


@dataclass
class _ContentCallSequence:
    next_ordinal: int = 1
    pending: set[int] = field(default_factory=set)
    failed: set[int] = field(default_factory=set)


class _SSECompletionDetector:
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._scan_offset = 0
        self.completed = False

    def feed(self, chunk: bytes) -> None:
        if self.completed:
            return
        self._buffer.extend(chunk)
        self._drain_complete_events(final=False)

    def finish(self) -> bool:
        self._drain_complete_events(final=True)
        return self.completed

    def _drain_complete_events(self, *, final: bool) -> None:
        while delimiter := self._next_delimiter(final=final):
            offset, size = delimiter
            event = bytes(self._buffer[:offset])
            del self._buffer[: offset + size]
            self._scan_offset = 0
            if _sse_event_is_terminal(event):
                self.completed = True
                return

    def _next_delimiter(self, *, final: bool) -> tuple[int, int] | None:
        offset = self._scan_offset
        while offset < len(self._buffer):
            first_size = self._line_ending_size(offset, final=final)
            if first_size is None:
                self._scan_offset = offset
                return None
            if first_size == 0:
                offset += 1
                continue
            second_offset = offset + first_size
            second_size = self._line_ending_size(second_offset, final=final)
            if second_size is None:
                self._scan_offset = offset
                return None
            if second_size:
                return offset, first_size + second_size
            offset = second_offset + 1
        self._scan_offset = offset
        return None

    def _line_ending_size(self, offset: int, *, final: bool) -> int | None:
        if offset >= len(self._buffer):
            return None
        if self._buffer[offset] == ord("\n"):
            return 1
        if self._buffer[offset] != ord("\r"):
            return 0
        next_offset = offset + 1
        if next_offset < len(self._buffer):
            return 2 if self._buffer[next_offset] == ord("\n") else 1
        return 1 if final else None


def _sse_event_is_terminal(event: bytes) -> bool:
    data_lines: list[bytes] = []
    normalized = event.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    for line in normalized.split(b"\n"):
        if not line or line.startswith(b":"):
            continue
        field_name, separator, value = line.partition(b":")
        if separator and value.startswith(b" "):
            value = value[1:]
        if field_name == b"data":
            data_lines.append(value)
    data = b"\n".join(data_lines).strip()
    if data == b"[DONE]":
        return True
    if not data:
        return False
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("type") == "response.completed"


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


def _content_encoding_is_supported(value: str) -> bool:
    encodings = {
        encoding.strip().lower()
        for encoding in value.split(",")
        if encoding.strip()
    }
    return encodings <= _SUPPORTED_UPSTREAM_CONTENT_ENCODINGS


def _start_response(
    handler: BaseHTTPRequestHandler,
    status_code: int,
    headers: dict[str, str],
) -> None:
    handler.send_response(status_code)
    for key, value in headers.items():
        if key.lower() not in _HOP_HEADERS:
            handler.send_header(key, value)
    handler.send_header("Connection", "close")
    handler.end_headers()


def _send_response(
    handler: BaseHTTPRequestHandler,
    status_code: int,
    headers: dict[str, str],
    body: bytes,
) -> None:
    _start_response(handler, status_code, headers)
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
        self._content_call_sequences: dict[str, _ContentCallSequence] = {}
        self._content_call_sequences_lock = Lock()

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

    def _allocate_content_call(self, fingerprint: str) -> int:
        with self._content_call_sequences_lock:
            sequence = self._content_call_sequences.setdefault(
                fingerprint, _ContentCallSequence()
            )
            if sequence.failed:
                ordinal = min(sequence.failed)
                sequence.failed.remove(ordinal)
            else:
                ordinal = sequence.next_ordinal
                sequence.next_ordinal += 1
            sequence.pending.add(ordinal)
            return ordinal

    def _finish_content_call(
        self,
        fingerprint: str,
        ordinal: int,
        *,
        succeeded: bool,
    ) -> None:
        with self._content_call_sequences_lock:
            sequence = self._content_call_sequences[fingerprint]
            sequence.pending.discard(ordinal)
            if not succeeded:
                sequence.failed.add(ordinal)

    def _settle_failed_forward(
        self,
        fingerprint: str,
        content_ordinal: int | None,
        invocation_id: str,
    ) -> None:
        if self.invocation_store is not None:
            self.invocation_store.mark(invocation_id, "failed")
        if content_ordinal is not None:
            self._finish_content_call(
                fingerprint,
                content_ordinal,
                succeeded=False,
            )

    def _forward(self, handler: BaseHTTPRequestHandler) -> None:
        response_cached = False
        response_started = False
        invocation_lock: Lock | None = None
        lock_stack = ExitStack()
        upstream: requests.Response | None = None
        content_ordinal: int | None = None
        call_succeeded = False
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
            if logical_call_id:
                seed = logical_call_id
                replay_key_source = "logical_call_id"
            else:
                content_ordinal = self._allocate_content_call(fingerprint)
                seed = (
                    fingerprint
                    if content_ordinal == 1
                    else f"{fingerprint}::occurrence::{content_ordinal}"
                )
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
                    self._log_replay(
                        invocation_id,
                        replay_key_source=replay_key_source,
                        outcome="replayed_cached",
                    )
                    _send_response(handler, cached.status_code, cached.headers, cached.body)
                    call_succeeded = True
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
            headers["Accept-Encoding"] = _UPSTREAM_ACCEPT_ENCODING
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
            if upstream.headers.get("Content-Type", "").lower().split(";", 1)[0].strip() == (
                "text/event-stream"
            ):
                content_encoding = upstream.headers.get("Content-Encoding", "")
                # Completion markers live in the decoded SSE representation.
                # Forward and cache those decoded bytes, and remove the stale
                # transfer representation metadata from the upstream response.
                decoded_response_headers = {
                    key: value
                    for key, value in response_headers.items()
                    if key.lower() != "content-encoding"
                }
                if upstream.status_code >= 400:
                    # The status already makes this stream terminal. Close it
                    # without waiting for an unbounded error body, and publish
                    # the retryable occurrence before the client sees failure.
                    self._settle_failed_forward(
                        fingerprint,
                        content_ordinal,
                        invocation_id,
                    )
                    content_ordinal = None
                    _start_response(
                        handler,
                        upstream.status_code,
                        decoded_response_headers,
                    )
                    response_started = True
                    return
                if not _content_encoding_is_supported(content_encoding):
                    self._settle_failed_forward(
                        fingerprint,
                        content_ordinal,
                        invocation_id,
                    )
                    content_ordinal = None
                    error_body = json.dumps(
                        {
                            "error": {
                                "code": "OPENMONTAGE_UNSUPPORTED_CONTENT_ENCODING",
                                "message": (
                                    "Upstream SSE response used unsupported "
                                    f"Content-Encoding: {content_encoding}"
                                ),
                            }
                        },
                        separators=(",", ":"),
                    ).encode("utf-8")
                    _send_response(
                        handler,
                        502,
                        {"Content-Type": "application/json"},
                        error_body,
                    )
                    response_started = True
                    return
                response_headers = decoded_response_headers
                _start_response(handler, upstream.status_code, response_headers)
                response_started = True
                response_body = bytearray()
                can_buffer = True
                downstream_open = True
                completion = _SSECompletionDetector()
                while True:
                    chunk = upstream.raw.read1(64 * 1024, decode_content=True)
                    if not chunk:
                        break
                    completion.feed(chunk)
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
                # Only a stream that reached its terminal marker is a success.
                # A stream that ended without one is truncated.
                stream_succeeded = completion.finish() and upstream.status_code < 400
                if self.invocation_store is not None:
                    if can_buffer:
                        if stream_succeeded:
                            self.invocation_store.save_response(
                                invocation_id,
                                status_code=upstream.status_code,
                                headers=response_headers,
                                body=bytes(response_body),
                            )
                            response_cached = True
                        else:
                            self.invocation_store.mark(invocation_id, "failed")
                    else:
                        self.invocation_store.mark(
                            invocation_id,
                            "succeeded" if stream_succeeded else "failed",
                        )
                call_succeeded = stream_succeeded and downstream_open
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
                _send_response(handler, upstream.status_code, response_headers, response_body)
                call_succeeded = upstream.status_code < 400
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
                call_succeeded = upstream.status_code < 400
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
            lock_stack.close()
            if content_ordinal is not None:
                self._finish_content_call(
                    fingerprint,
                    content_ordinal,
                    succeeded=call_succeeded,
                )

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

        replay_key_source distinguishes a caller-supplied logical call id from
        the order-dependent content-occurrence fallback, so the documented
        limitation is observable rather than silent.
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
