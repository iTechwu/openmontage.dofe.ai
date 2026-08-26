from __future__ import annotations

import json

from openmontage import cli
from openmontage.job_worker import JobWorkerResult


class _Worker:
    def __init__(self, result: JobWorkerResult | None) -> None:
        self.result = result
        self.calls = 0

    def run_once(self):
        self.calls += 1
        return self.result


def test_worker_once_prints_one_json_result(
    monkeypatch,
    capsys,
) -> None:
    worker = _Worker(
        JobWorkerResult(
            job_id="om_job_1",
            stage="research",
            outcome="stage_completed",
        )
    )
    monkeypatch.setattr(cli, "_build_job_worker", lambda args: worker)

    exit_code = cli.main(["worker", "run", "--once", "--json"])

    assert exit_code == 0
    assert worker.calls == 1
    assert json.loads(capsys.readouterr().out) == {
        "jobId": "om_job_1",
        "stage": "research",
        "outcome": "stage_completed",
    }


def test_worker_once_reports_idle_as_success(monkeypatch, capsys) -> None:
    worker = _Worker(None)
    monkeypatch.setattr(cli, "_build_job_worker", lambda args: worker)

    exit_code = cli.main(["worker", "run", "--once", "--json"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "idle"}


def test_worker_fails_closed_without_executor_configuration(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("OPENMONTAGE_AGENT_EXECUTOR_JSON", raising=False)

    exit_code = cli.main(["worker", "run", "--once", "--json"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["success"] is False
    assert "OPENMONTAGE_AGENT_EXECUTOR_JSON" in error["error"]


def test_worker_fails_fast_when_executor_binary_is_missing(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv(
        "OPENMONTAGE_AGENT_EXECUTOR_JSON",
        json.dumps(["openmontage-no-such-executor", "exec", "-"]),
    )

    exit_code = cli.main(["worker", "run", "--once", "--json"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["success"] is False
    assert "not found on PATH" in error["error"]
