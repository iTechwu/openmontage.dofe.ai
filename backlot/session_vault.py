"""Encrypted shared RP state on a private local persistent volume.

Short SQLite transactions allow different sessions to proceed concurrently.
OS family locks serialize rotation across workers, including network recovery.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class VaultUnavailable(RuntimeError):
    pass


class SessionVault:
    def __init__(self, path: Path, encryption_key: str):
        self.path = path
        self.cipher = Fernet(encryption_key.encode("ascii"))
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.is_symlink():
            raise VaultUnavailable("Session database must not be a symlink")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(path, 0o600)
        self.locks = path.parent / "locks"
        self.locks.mkdir(mode=0o700, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS rp_state (id TEXT PRIMARY KEY, kind TEXT NOT NULL, sid TEXT, family TEXT, expires REAL NOT NULL, value BLOB NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS rp_sid ON rp_state(sid)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS rp_family ON rp_state(family) WHERE family IS NOT NULL")

    @staticmethod
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def lock(self, identity: str):
        descriptor = os.open(self.locks / self.digest(identity), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + 20
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise VaultUnavailable("Session operation is busy")
                    time.sleep(0.025)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def get(self, identity: str, *, allow_expired: bool = False) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT expires,value FROM rp_state WHERE id=?", (self.digest(identity),)).fetchone()
        if row is None or not allow_expired and row[0] <= time.time():
            return None
        try:
            value = json.loads(self.cipher.decrypt(row[1]))
            return value if isinstance(value, dict) else None
        except (InvalidToken, ValueError, TypeError) as exc:
            raise VaultUnavailable("Session state cannot be verified") from exc

    def put(self, identity: str, value: dict, expires: float, *, kind: str = "session", sid: str | None = None, family: str | None = None):
        encrypted = self.cipher.encrypt(json.dumps(value, separators=(",", ":")).encode())
        with self.connection() as db:
            db.execute("INSERT INTO rp_state(id,kind,sid,family,expires,value) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET expires=excluded.expires,value=excluded.value,sid=excluded.sid,family=excluded.family", (
                self.digest(identity), kind, self.digest(sid) if sid else None,
                self.digest(family) if family else None, expires, encrypted))

    def consume(self, identity: str) -> dict | None:
        with self.lock(identity):
            value = self.get(identity)
            self.delete(identity)
            return value

    def delete(self, identity: str):
        with self.connection() as db:
            db.execute("DELETE FROM rp_state WHERE id=?", (self.digest(identity),))

    def revoke_sid(self, sid: str):
        with self.connection() as db:
            db.execute("DELETE FROM rp_state WHERE sid=?", (self.digest(sid),))
