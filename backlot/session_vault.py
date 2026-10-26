"""Encrypted shared RP state on a private local persistent volume.

Short SQLite transactions allow different sessions to proceed concurrently.
OS family locks serialize rotation across workers, including network recovery.
"""
from __future__ import annotations

import base64
import hmac
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
    def __init__(self, path: Path, encryption_key: str, *, namespace: str = "", binding: str = ""):
        self.namespace, self.binding = namespace, binding
        self.path = path
        key = encryption_key.encode("ascii")
        if namespace:
            key = base64.urlsafe_b64encode(hmac.new(base64.urlsafe_b64decode(key), ("openmontage-realm-v1:" + namespace + ":" + binding).encode(), hashlib.sha256).digest())
        self.cipher = Fernet(key)
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

    def record_id(self, identity):
        return self.digest(self.namespace + ":" + identity if self.namespace else identity)

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
        descriptor = os.open(self.locks / self.record_id(identity), os.O_CREAT | os.O_RDWR, 0o600)
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
            row = db.execute("SELECT expires,value FROM rp_state WHERE id=?", (self.record_id(identity),)).fetchone()
        if row is None or not allow_expired and row[0] <= time.time():
            return None
        try:
            value = json.loads(self.cipher.decrypt(row[1]))
            if self.namespace:
                if not isinstance(value, dict) or value.get("realm_binding") != self.binding or value.get("record") != self.record_id(identity):
                    raise ValueError("Invalid realm binding")
                value = value.get("payload")
            return value if isinstance(value, dict) else None
        except (InvalidToken, ValueError, TypeError) as exc:
            raise VaultUnavailable("Session state cannot be verified") from exc

    def put(self, identity: str, value: dict, expires: float, *, kind: str = "session", sid: str | None = None, family: str | None = None):
        payload = {"realm_binding": self.binding, "record": self.record_id(identity), "payload": value} if self.namespace else value
        encrypted = self.cipher.encrypt(json.dumps(payload, separators=(",", ":")).encode())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM rp_state WHERE expires<=?", (time.time(),))
            if sid and kind != "logout" and db.execute("SELECT 1 FROM rp_state WHERE id=? AND expires>?", (self.digest("revoked:" + sid), time.time())).fetchone():
                raise VaultUnavailable("Session has been revoked")
            db.execute("INSERT INTO rp_state(id,kind,sid,family,expires,value) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET expires=excluded.expires,value=excluded.value,sid=excluded.sid,family=excluded.family,kind=excluded.kind", (
                self.record_id(identity), kind, self.digest(sid) if sid else None,
                self.digest(family) if family else None, expires, encrypted))
            db.commit()

    def consume(self, identity: str) -> dict | None:
        with self.lock(identity):
            value = self.get(identity)
            self.delete(identity)
            return value

    def delete(self, identity: str):
        with self.connection() as db:
            db.execute("DELETE FROM rp_state WHERE id=?", (self.record_id(identity),))

    def revoke_sid(self, sid: str):
        # A concurrent in-flight refresh cannot resurrect a back-channel logout.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM rp_state WHERE sid=? AND kind!='logout'", (self.digest(sid),))
            db.execute("INSERT OR REPLACE INTO rp_state(id,kind,expires,value) VALUES(?,?,?,?)", (
                self.digest("revoked:" + sid), "revocation", time.time() + 31 * 86400, self.cipher.encrypt(b"{}")))
            db.commit()
