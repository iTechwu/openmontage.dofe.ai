"""Durable model invocation identities for replay-safe delegated requests."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator
from uuid import uuid4

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on Windows.
    _fcntl = None

try:  # Windows
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX.
    _msvcrt = None


@dataclass(frozen=True)
class ModelInvocationRecord:
    job_id: str
    stage: str
    attempt: int
    request_id: str
    request_fingerprint: str
    model_invocation_id: str
    status: str
    updated_at: str


@dataclass(frozen=True)
class CachedInvocationResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


class InvocationRequestConflictError(ValueError):
    """A logical call identifier was reused for different request content."""


class ModelInvocationStore:
    """SQLite-backed idempotency ledger shared by the Job Worker and proxy."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS openmontage_model_invocation (
                    job_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    attempt INTEGER NOT NULL CHECK (attempt > 0),
                    request_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL DEFAULT '',
                    model_invocation_id TEXT NOT NULL PRIMARY KEY,
                    status TEXT NOT NULL CHECK (status IN ('created', 'in_flight', 'succeeded', 'failed', 'unknown')),
                    response_status INTEGER,
                    response_headers TEXT,
                    response_body BLOB,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (job_id, stage, attempt, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_openmontage_model_invocation_recovery
                    ON openmontage_model_invocation(job_id, status, updated_at);
                """
            )
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(openmontage_model_invocation)")
            }
            if "request_fingerprint" not in columns:
                db.execute(
                    "ALTER TABLE openmontage_model_invocation "
                    "ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''"
                )
            for name, definition in (
                ("response_status", "INTEGER"),
                ("response_headers", "TEXT"),
                ("response_body", "BLOB"),
            ):
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE openmontage_model_invocation ADD COLUMN {name} {definition}"
                    )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 5000")
        return db

    @contextmanager
    def invocation_lock(self, model_invocation_id: str) -> Iterator[None]:
        if not model_invocation_id:
            raise ValueError("model_invocation_id is required")
        lock_dir = self.database_path.parent / ".openmontage-invocation-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_name = hashlib.sha256(model_invocation_id.encode("utf-8")).hexdigest()
        with (lock_dir / f"{lock_name}.lock").open("a+b") as lock_file:
            _lock_file(lock_file)
            try:
                yield
            finally:
                _unlock_file(lock_file)

    def get_or_create(
        self,
        *,
        job_id: str,
        stage: str,
        attempt: int,
        request_id: str,
        request_fingerprint: str | None = None,
    ) -> ModelInvocationRecord:
        fingerprint = request_fingerprint or request_id
        if not job_id or not stage or attempt < 1 or not request_id or not fingerprint:
            raise ValueError("job_id, stage, attempt, request_id and request_fingerprint are required")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO openmontage_model_invocation
                    (job_id, stage, attempt, request_id, request_fingerprint,
                     model_invocation_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?)
                ON CONFLICT(job_id, stage, attempt, request_id) DO NOTHING
                """,
                (job_id, stage, attempt, request_id, fingerprint, f"om-{uuid4().hex}", now, now),
            )
            db.execute(
                """UPDATE openmontage_model_invocation
                   SET request_fingerprint = ?, updated_at = ?
                   WHERE job_id = ? AND stage = ? AND attempt = ? AND request_id = ?
                     AND request_fingerprint = ''""",
                (fingerprint, now, job_id, stage, attempt, request_id),
            )
            row = db.execute(
                """SELECT job_id, stage, attempt, request_id, request_fingerprint,
                          model_invocation_id, status, updated_at
                   FROM openmontage_model_invocation
                   WHERE job_id = ? AND stage = ? AND attempt = ? AND request_id = ?""",
                (job_id, stage, attempt, request_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("model invocation ledger insert did not return a row")
        if row["request_fingerprint"] != fingerprint:
            raise InvocationRequestConflictError(
                "model invocation request id was reused for a different request"
            )
        return ModelInvocationRecord(**dict(row))

    def mark(self, model_invocation_id: str, status: str) -> ModelInvocationRecord:
        if status not in {"created", "in_flight", "succeeded", "failed", "unknown"}:
            raise ValueError("invalid model invocation status")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """UPDATE openmontage_model_invocation
                   SET status = ?, response_status = NULL,
                       response_headers = NULL, response_body = NULL, updated_at = ?
                   WHERE model_invocation_id = ?""",
                (status, now, model_invocation_id),
            )
            row = db.execute(
                """SELECT job_id, stage, attempt, request_id, request_fingerprint,
                          model_invocation_id, status, updated_at
                   FROM openmontage_model_invocation WHERE model_invocation_id = ?""",
                (model_invocation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(model_invocation_id)
        return ModelInvocationRecord(**dict(row))

    def save_response(
        self,
        model_invocation_id: str,
        *,
        status_code: int,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        if status_code < 200 or status_code >= 600:
            raise ValueError("only final HTTP invocation responses can be cached")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE openmontage_model_invocation
                   SET status = ?, response_status = ?, response_headers = ?,
                       response_body = ?, updated_at = ?
                   WHERE model_invocation_id = ?""",
                (
                    "succeeded" if status_code < 400 else "failed",
                    status_code,
                    json.dumps(headers, ensure_ascii=True, sort_keys=True),
                    body,
                    now,
                    model_invocation_id,
                ),
            )
        if cursor.rowcount != 1:
            raise KeyError(model_invocation_id)

    def get_cached_response(self, model_invocation_id: str) -> CachedInvocationResponse | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT response_status, response_headers, response_body
                   FROM openmontage_model_invocation
                   WHERE model_invocation_id = ? AND status = 'succeeded'
                     AND response_status IS NOT NULL AND response_headers IS NOT NULL
                     AND response_body IS NOT NULL""",
                (model_invocation_id,),
            ).fetchone()
        if row is None:
            return None
        headers = json.loads(row["response_headers"])
        if not isinstance(headers, dict):
            raise ValueError("cached invocation response headers are invalid")
        return CachedInvocationResponse(
            status_code=int(row["response_status"]),
            headers={str(key): str(value) for key, value in headers.items()},
            body=bytes(row["response_body"]),
        )

    def list_recoverable(self, *, job_id: str | None = None) -> list[ModelInvocationRecord]:
        query = """SELECT job_id, stage, attempt, request_id, request_fingerprint,
                          model_invocation_id, status, updated_at
                   FROM openmontage_model_invocation
                   WHERE status IN ('created', 'in_flight', 'unknown')"""
        args: tuple[str, ...] = ()
        if job_id:
            query += " AND job_id = ?"
            args = (job_id,)
        query += " ORDER BY updated_at ASC"
        with self._connect() as db:
            rows = db.execute(query, args).fetchall()
        return [ModelInvocationRecord(**dict(row)) for row in rows]


def _lock_file(lock_file: BinaryIO) -> None:
    if _fcntl is not None:
        _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
        return
    if _msvcrt is not None:
        lock_file.seek(0, 2)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_LOCK, 1)
        return
    raise RuntimeError("cross-process file locking is unavailable on this platform")


def _unlock_file(lock_file: BinaryIO) -> None:
    if _fcntl is not None:
        _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:
        lock_file.seek(0)
        _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_UNLCK, 1)
