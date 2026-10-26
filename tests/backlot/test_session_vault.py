import concurrent.futures
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from backlot.session_vault import SessionVault, VaultUnavailable


def test_shared_restart_storage_is_private_and_authenticated(tmp_path: Path):
    key = Fernet.generate_key().decode()
    vault = SessionVault(tmp_path / "auth" / "sessions.sqlite3", key)
    vault.put("opaque-cookie", {"refresh_token": "sensitive-refresh"}, 9999999999)
    assert SessionVault(vault.path, key).get("opaque-cookie") == {"refresh_token": "sensitive-refresh"}
    assert b"sensitive-refresh" not in vault.path.read_bytes()
    assert vault.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(VaultUnavailable):
        SessionVault(vault.path, Fernet.generate_key().decode()).get("opaque-cookie")


def test_worker_locks_serialize_one_identity_without_lost_updates(tmp_path: Path):
    key = Fernet.generate_key().decode()
    vault = SessionVault(tmp_path / "sessions.sqlite3", key)
    vault.put("session", {"count": 0}, 9999999999)

    def increment(_):
        worker = SessionVault(vault.path, key)
        with worker.lock("session"):
            value = worker.get("session")
            value["count"] += 1
            worker.put("session", value, 9999999999)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(increment, range(32)))
    assert vault.get("session")["count"] == 32


def test_revocation_and_single_use_transactions(tmp_path: Path):
    vault = SessionVault(tmp_path / "sessions.sqlite3", Fernet.generate_key().decode())
    vault.put("first", {"ok": True}, 9999999999, sid="sid-one", family="family-one")
    vault.put("other", {"ok": True}, 9999999999, sid="sid-two", family="family-two")
    vault.put("transaction", {"verifier": "secret"}, 9999999999, kind="transaction")
    assert vault.consume("transaction") == {"verifier": "secret"}
    assert vault.consume("transaction") is None
    vault.revoke_sid("sid-one")
    assert vault.get("first") is None
    assert vault.get("other") is not None
