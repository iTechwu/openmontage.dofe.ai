from __future__ import annotations

from pathlib import Path

import pytest

from openmontage import invocation_store


class _FakeMsvcrt:
    LK_LOCK = 1
    LK_UNLCK = 2

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []

    def locking(self, file_descriptor: int, mode: int, length: int) -> None:
        self.calls.append((file_descriptor, mode, length))


def test_windows_invocation_lock_uses_one_stable_file_byte(monkeypatch, tmp_path: Path):
    backend = _FakeMsvcrt()
    monkeypatch.setattr(invocation_store, "_fcntl", None, raising=False)
    monkeypatch.setattr(invocation_store, "_msvcrt", backend, raising=False)
    store = invocation_store.ModelInvocationStore(tmp_path / "jobs.sqlite3")

    with store.invocation_lock("invocation-1"):
        lock_path = next((tmp_path / ".openmontage-invocation-locks").iterdir())
        assert lock_path.read_bytes() == b"\0"

    assert [call[1:] for call in backend.calls] == [
        (backend.LK_LOCK, 1),
        (backend.LK_UNLCK, 1),
    ]


def test_invocation_lock_reports_missing_platform_backend(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(invocation_store, "_fcntl", None, raising=False)
    monkeypatch.setattr(invocation_store, "_msvcrt", None, raising=False)
    store = invocation_store.ModelInvocationStore(tmp_path / "jobs.sqlite3")

    with pytest.raises(RuntimeError, match="file locking is unavailable"):
        with store.invocation_lock("invocation-1"):
            pass
