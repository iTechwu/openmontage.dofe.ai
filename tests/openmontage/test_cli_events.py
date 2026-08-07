from __future__ import annotations

import json

from openmontage.cli import main
from openmontage.event_outbox import OutboxPublisher, PublishResult


def test_event_publisher_can_flush_once_for_container_jobs(monkeypatch, capsys) -> None:
    limits: list[int] = []

    class Publisher:
        def publish_pending(self, *, limit: int) -> PublishResult:
            limits.append(limit)
            return PublishResult(delivered=2, failed=1, dead_lettered=1)

    monkeypatch.setattr(
        OutboxPublisher,
        "from_environment",
        classmethod(lambda cls: Publisher()),
    )

    exit_code = main(["events", "publish", "--once", "--limit", "25", "--json"])

    assert exit_code == 1
    assert limits == [25]
    assert json.loads(capsys.readouterr().out) == {
        "deadLettered": 1,
        "delivered": 2,
        "failed": 1,
    }
