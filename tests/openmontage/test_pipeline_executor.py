from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

import pytest

from lib.checkpoint import init_project
from openmontage.contracts import JobAttribution, JobCreateRequest
from openmontage.job_service import JobService
from openmontage.pipeline_executor import (
    AgentCommandPipelineExecutor,
    PipelineExecutionCancelled,
    PipelineExecutionError,
    PipelineExecutionIncomplete,
    StageAssignment,
    _configure_agent_command_for_delegation,
    _is_codex_exec_command,
    delegated_executor_availability,
)
from tools.dofe.delegation import DelegatedModelCredential


WRITE_CHECKPOINT_SCRIPT = r"""
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

prompt = sys.stdin.read()
prefix = "OPENMONTAGE_ASSIGNMENT_PATH="
assignment_line = next(line for line in prompt.splitlines() if line.startswith(prefix))
assignment_path = pathlib.Path(json.loads(assignment_line[len(prefix):]))
assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
pathlib.Path(sys.argv[1]).write_text(prompt, encoding="utf-8")
if len(sys.argv) > 2:
    pathlib.Path(sys.argv[2]).write_text(json.dumps({
        key: os.environ.get(key)
        for key in ["DOFE_MODEL_API_KEY", "DOFE_MODEL_BASE_URL", "DOFE_DELEGATION_ID", "DOFE_EXTERNAL_JOB_ID", "DOFE_PIPELINE_STAGE", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENMONTAGE_SERVICE_TOKEN", "OPENMONTAGE_EVENT_SIGNING_SECRET", "FAL_KEY"]
    }), encoding="utf-8")
if len(sys.argv) > 4:
    pathlib.Path(sys.argv[3]).write_text(sys.argv[4], encoding="utf-8")
checkpoint = {
    "version": "1.0",
    "project_id": assignment["projectId"],
    "pipeline_type": assignment["pipeline"],
    "stage": assignment["stage"],
    "status": "in_progress",
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "checkpoint_policy": "guided",
    "human_approval_required": False,
    "human_approved": False,
    "artifacts": {},
    "metadata": {"partial_progress": {"completed_units": 1, "total_units": 3}},
}
checkpoint_path = pathlib.Path(assignment["projectDir"]) / f"checkpoint_{assignment['stage']}.json"
checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
"""

WRITE_TWO_CHECKPOINTS_SCRIPT = WRITE_CHECKPOINT_SCRIPT + r"""
next_checkpoint = dict(checkpoint)
next_checkpoint["stage"] = "proposal"
next_path = pathlib.Path(assignment["projectDir"]) / "checkpoint_proposal.json"
next_path.write_text(json.dumps(next_checkpoint), encoding="utf-8")
"""


def _job(tmp_path: Path):
    service = JobService(tmp_path / "jobs.sqlite3")
    job = service.create_job(
        JobCreateRequest(
            client_request_id="executor-request",
            workflow="animated-explainer",
            input={"type": "text", "inlineText": "Explain executor leases"},
            brief={"title": "Executor leases", "durationSeconds": 30},
            output={"container": "mp4", "resolution": "1080x1920"},
            budget={"maxAmount": "20.00", "currency": "CNY"},
        ),
        JobAttribution(
            workspace_id="ws-executor",
            employee_id="employee-executor",
            runtime_id="runtime-executor",
            root_task_id="task-executor",
            conversation_id="conversation-executor",
            source_invocation_id="invocation-executor",
            trace_id="trace-executor",
        ),
    )
    projects_dir = tmp_path / "projects"
    project_dir = init_project(
        job.job_id,
        title="Executor leases",
        pipeline_type=job.workflow.name,
        pipeline_dir=projects_dir,
    )
    return job, projects_dir, project_dir


def _delegated_credential(job, *, api_key: str) -> DelegatedModelCredential:
    return DelegatedModelCredential(
        api_key=api_key,
        models_base_url="https://models.test/api",
        delegation_id="delegation-diagnostics",
        external_job_id=job.job_id,
        pipeline_stage="research",
        runtime_credential_id="runtime-credential-diagnostics",
        expires_at="2099-08-06T09:00:01Z",
    )


def _codex_command(tmp_path: Path, *real_command: str) -> list[str]:
    """Return a Codex-shaped argv that actually execs ``real_command``.

    Tests can exercise the delegated Codex model-lock path without requiring
    the real Codex binary to be installed. The fake ``codex`` executable
    strips any ``-c key=value`` provider config injected by
    ``_configure_agent_command_for_delegation`` and the ``exec`` subcommand,
    then forwards to the underlying command.
    """
    codex = tmp_path / "codex"
    codex.write_text(
        '#!/usr/bin/env python3\n'
        'import os, sys\n'
        'args = sys.argv[1:]\n'
        '# Consume Codex -c provider_config pairs injected by the executor.\n'
        'while len(args) >= 2 and args[0] == "-c":\n'
        '    args = args[2:]\n'
        'if args and args[0] == "exec":\n'
        '    args = args[1:]\n'
        'os.execvp(args[0], args)\n'
    )
    codex.chmod(0o755)
    return [str(codex), "exec"] + list(real_command)


def _exception_chain_diagnostics(error: BaseException) -> str:
    pending = [error]
    seen: set[int] = set()
    diagnostics = [
        "".join(traceback.format_exception(type(error), error, error.__traceback__))
    ]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        diagnostics.append(str(current))
        for attribute in ("stdout", "stderr", "output", "cmd", "args"):
            value = getattr(current, attribute, None)
            if value:
                diagnostics.append(str(value))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return "\n".join(diagnostics)


def test_executor_writes_assignment_invokes_real_argv_and_reads_checkpoint(
    tmp_path: Path,
) -> None:
    job, projects_dir, project_dir = _job(tmp_path)
    prompt_capture = tmp_path / "prompt.txt"
    assignment = StageAssignment.from_job(
        job,
        stage="research",
        stage_attempt=1,
        projects_dir=projects_dir,
    )
    executor = AgentCommandPipelineExecutor(
        [sys.executable, "-c", WRITE_CHECKPOINT_SCRIPT, str(prompt_capture)],
        timeout_seconds=5,
    )

    result = executor.execute(assignment)

    assignment_payload = json.loads(result.assignment_path.read_text(encoding="utf-8"))
    assert assignment_payload["jobId"] == job.job_id
    assert assignment_payload["projectId"] == job.job_id
    assert assignment_payload["projectDir"] == str(project_dir.resolve())
    assert assignment_payload["pipeline"] == "animated-explainer"
    assert assignment_payload["stage"] == "research"
    assert assignment_payload["stageAttempt"] == 1
    assert assignment_payload["directorSkill"] == "skills/pipelines/explainer/research-director.md"
    assert assignment_payload["attribution"]["employeeId"] == "employee-executor"
    assert result.status == "in_progress"
    assert result.checkpoint["metadata"]["partial_progress"] == {
        "completed_units": 1,
        "total_units": 3,
    }
    assert "Execute exactly one OpenMontage pipeline stage" in prompt_capture.read_text()


def test_executor_uses_configured_model_key_without_job_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "configured-model-key")
    monkeypatch.setenv("DOFE_MODEL_BASE_URL", "https://ixicai.cn/api")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")
    job, projects_dir, _ = _job(tmp_path)
    environment_capture = tmp_path / "environment.json"
    assignment = StageAssignment.from_job(
        job,
        stage="research",
        stage_attempt=1,
        projects_dir=projects_dir,
    )
    executor = AgentCommandPipelineExecutor(
        _codex_command(
            tmp_path,
            sys.executable,
            "-c",
            WRITE_CHECKPOINT_SCRIPT,
            str(tmp_path / "prompt.txt"),
            str(environment_capture),
        ),
        timeout_seconds=5,
    )

    executor.execute(assignment)

    environment = json.loads(environment_capture.read_text())
    assert environment["DOFE_MODEL_API_KEY"] == "configured-model-key"
    assert environment["OPENAI_API_KEY"] == "configured-model-key"
    assert environment["DOFE_MODEL_BASE_URL"] == "https://ixicai.cn/api"
    assert environment["OPENAI_BASE_URL"] == "https://ixicai.cn/api/v1"
    assert environment["DOFE_DELEGATION_ID"] is None


def test_executor_does_not_fallback_to_openai_key_for_dofe_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOFE_MODEL_BASE_URL", "https://ixicai.cn/api")
    monkeypatch.delenv("DOFE_MODEL_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")
    job, projects_dir, _ = _job(tmp_path)
    assignment = StageAssignment.from_job(
        job,
        stage="research",
        stage_attempt=1,
        projects_dir=projects_dir,
    )
    executor = AgentCommandPipelineExecutor(
        _codex_command(
            tmp_path,
            sys.executable,
            "-c",
            WRITE_CHECKPOINT_SCRIPT,
            str(tmp_path / "prompt.txt"),
        ),
        timeout_seconds=5,
    )

    with pytest.raises(PipelineExecutionError, match="DOFE_MODEL_API_KEY is required"):
        executor.execute(assignment)


def test_executor_redacts_configured_model_key_from_self_contained_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "bare-configured-model-secret")
    monkeypatch.setenv("DOFE_MODEL_BASE_URL", "https://ixicai.cn/api")
    job, projects_dir, _ = _job(tmp_path)
    diagnostic_script = r"""
import os
import sys

sys.stderr.write("self-contained provider failure\n")
sys.stderr.write(os.environ["DOFE_MODEL_API_KEY"] + "\n")
sys.exit(7)
"""
    executor = AgentCommandPipelineExecutor(
        _codex_command(tmp_path, sys.executable, "-c", diagnostic_script),
        timeout_seconds=5,
    )

    with pytest.raises(PipelineExecutionError) as error_info:
        executor.execute(
            StageAssignment.from_job(
                job,
                stage="research",
                stage_attempt=1,
                projects_dir=projects_dir,
            ),
        )

    log_path = (
        projects_dir
        / job.job_id
        / ".openmontage"
        / "executor"
        / "research-attempt-1.json"
    )
    assert "bare-configured-model-secret" not in str(error_info.value)
    assert "bare-configured-model-secret" not in log_path.read_text(encoding="utf-8")
    assert "self-contained provider failure" in str(error_info.value)


def test_executor_rejects_checkpoint_changes_outside_assigned_stage(tmp_path: Path) -> None:
    job, projects_dir, _ = _job(tmp_path)
    assignment = StageAssignment.from_job(
        job,
        stage="research",
        stage_attempt=1,
        projects_dir=projects_dir,
    )
    executor = AgentCommandPipelineExecutor(
        [
            sys.executable,
            "-c",
            WRITE_TWO_CHECKPOINTS_SCRIPT,
            str(tmp_path / "prompt.txt"),
        ],
        timeout_seconds=5,
    )

    with pytest.raises(PipelineExecutionError, match="outside the assigned stage"):
        executor.execute(assignment)

    second_prompt_capture = tmp_path / "second-prompt.txt"
    retry_executor = AgentCommandPipelineExecutor(
        [sys.executable, "-c", WRITE_CHECKPOINT_SCRIPT, str(second_prompt_capture)],
        timeout_seconds=5,
    )
    with pytest.raises(PipelineExecutionError, match="outside the assigned stage"):
        retry_executor.execute(assignment)
    assert not second_prompt_capture.exists()


def test_executor_injects_delegation_only_into_the_stage_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENMONTAGE_SERVICE_TOKEN", "must-not-reach-agent")
    monkeypatch.setenv("OPENMONTAGE_EVENT_SIGNING_SECRET", "must-not-reach-agent")
    monkeypatch.setenv("FAL_KEY", "must-not-reach-agent")
    job, projects_dir, project_dir = _job(tmp_path)
    environment_capture = tmp_path / "environment.json"
    project_argument_capture = tmp_path / "project-argument.txt"
    executor = AgentCommandPipelineExecutor(
        _codex_command(
            tmp_path,
            sys.executable,
            "-c",
            WRITE_CHECKPOINT_SCRIPT,
            str(tmp_path / "prompt.txt"),
            str(environment_capture),
            str(project_argument_capture),
            "{project_dir}",
        ),
        timeout_seconds=5,
        agent_model_resolver=lambda _cred: "catalog-agent-model",
    )
    assignment = StageAssignment.from_job(
        job,
        stage="research",
        stage_attempt=1,
        projects_dir=projects_dir,
    )
    credential = DelegatedModelCredential(
        api_key="delegated-api-key",
        models_base_url="https://models.test/api",
        delegation_id="delegation-1",
        external_job_id=job.job_id,
        pipeline_stage="research",
        runtime_credential_id="runtime-credential-1",
        expires_at="2099-08-06T09:00:01Z",
    )

    result = executor.execute(assignment, credential=credential)

    environment = json.loads(environment_capture.read_text())
    assert environment["DOFE_MODEL_API_KEY"] == "delegated-api-key"
    assert environment["DOFE_DELEGATION_ID"] == "delegation-1"
    assert environment["DOFE_PIPELINE_STAGE"] == "research"
    assert environment["DOFE_MODEL_BASE_URL"].startswith("http://127.0.0.1:")
    assert environment["DOFE_MODEL_BASE_URL"].endswith("/api")
    assert environment["OPENAI_BASE_URL"].startswith("http://127.0.0.1:")
    assert environment["OPENAI_BASE_URL"].endswith("/api/v1")
    assert environment["OPENMONTAGE_SERVICE_TOKEN"] is None
    assert environment["OPENMONTAGE_EVENT_SIGNING_SECRET"] is None
    assert environment["FAL_KEY"] is None
    assert project_argument_capture.read_text() == str(project_dir)
    assert "delegated-api-key" not in result.assignment_path.read_text()
    assert os.environ.get("DOFE_MODEL_API_KEY") != "delegated-api-key"


@pytest.mark.parametrize(
    "raw",
    [
        "codex exec --full-auto",
        "{}",
        "[]",
        '["codex", 7]',
        '["", "exec"]',
    ],
)
def test_executor_environment_requires_nonempty_json_argv(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("OPENMONTAGE_AGENT_EXECUTOR_JSON", raw)

    with pytest.raises(PipelineExecutionError, match="JSON argv"):
        AgentCommandPipelineExecutor.from_environment()


def test_delegated_executor_availability_reports_unset_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENMONTAGE_AGENT_EXECUTOR_JSON", raising=False)

    report = delegated_executor_availability()

    assert report == {
        "available": False,
        "executor": "",
        "reason": (
            "OPENMONTAGE_AGENT_EXECUTOR_JSON is not set; no external agent "
            "executor is configured"
        ),
    }


def test_delegated_executor_availability_reports_invalid_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENMONTAGE_AGENT_EXECUTOR_JSON", "not json")

    report = delegated_executor_availability()

    assert report["available"] is False
    assert report["executor"] == ""
    assert report["reason"].startswith("invalid OPENMONTAGE_AGENT_EXECUTOR_JSON")


def test_delegated_executor_availability_reports_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OPENMONTAGE_AGENT_EXECUTOR_JSON",
        json.dumps(["openmontage-no-such-executor", "exec", "-"]),
    )

    report = delegated_executor_availability()

    assert report["available"] is False
    assert report["executor"] == "openmontage-no-such-executor"
    assert "not found on PATH" in report["reason"]


def test_delegated_executor_availability_accepts_resolvable_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OPENMONTAGE_AGENT_EXECUTOR_JSON",
        json.dumps([sys.executable, "-c", "pass"]),
    )

    report = delegated_executor_availability()

    assert report == {"available": True, "executor": sys.executable, "reason": ""}


def test_is_codex_exec_command_recognizes_windows_exe() -> None:
    assert _is_codex_exec_command([r"C:\Users\dev\AppData\Roaming\npm\codex.exe", "exec", "-"])
    assert _is_codex_exec_command(["codex.exe", "exec", "--full-auto"])
    assert not _is_codex_exec_command(["codex.exe"])  # missing exec subcommand
    assert not _is_codex_exec_command(["claude.exe", "exec", "-"])


def test_delegated_codex_uses_the_loopback_proxy_as_its_native_responses_provider() -> None:
    command = _configure_agent_command_for_delegation(
        ("codex", "exec", "--ephemeral", "-"),
        "http://127.0.0.1:43127/api/v1",
    )

    assert command[0] == "codex"
    assert command[-3:] == ("exec", "--ephemeral", "-")
    assert 'model_provider="dofe-delegated"' in command
    assert (
        'model_providers.dofe-delegated.base_url="http://127.0.0.1:43127/api/v1"'
        in command
    )
    assert 'model_providers.dofe-delegated.wire_api="responses"' in command
    assert "model_providers.dofe-delegated.supports_websockets=false" in command
    assert "model_providers.dofe-delegated.requires_openai_auth=true" in command


def test_delegated_non_codex_executor_command_is_unchanged() -> None:
    command = ("claude", "--print", "-")

    assert _configure_agent_command_for_delegation(
        command,
        "http://127.0.0.1:43127/api/v1",
    ) == command


def test_delegated_codex_command_injects_catalog_verified_model() -> None:
    command = _configure_agent_command_for_delegation(
        ("codex", "exec", "-"),
        "http://127.0.0.1:43127/api/v1",
        model="catalog-agent-model",
    )

    assert command[:1] == ("codex",)
    assert command[-2:] == ("exec", "-")
    assert 'model_provider="dofe-delegated"' in command
    assert 'model="catalog-agent-model"' in command


def test_delegated_codex_locks_catalog_verified_model_into_the_stage_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, projects_dir, _ = _job(tmp_path)
    credential = _delegated_credential(job, api_key="delegated-key")
    captured: dict[str, object] = {}

    def fake_run(self, command, prompt, *, environment, cancellation_requested):
        captured["command"] = command
        raise PipelineExecutionError("stop after codex config")

    monkeypatch.setattr(AgentCommandPipelineExecutor, "_run", fake_run)

    resolver_calls: list[DelegatedModelCredential] = []
    executor = AgentCommandPipelineExecutor(
        [str(tmp_path / "codex"), "exec", "-"],
        timeout_seconds=5,
        agent_model_resolver=lambda cred: resolver_calls.append(cred)
        or "catalog-agent-model",
    )

    with pytest.raises(PipelineExecutionError, match="stop after codex config"):
        executor.execute(
            StageAssignment.from_job(
                job, stage="research", stage_attempt=1, projects_dir=projects_dir
            ),
            credential=credential,
        )

    assert resolver_calls == [credential]
    command = captured["command"]
    assert 'model_provider="dofe-delegated"' in command
    assert 'model="catalog-agent-model"' in command


def test_delegated_codex_fails_closed_before_launch_when_model_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, projects_dir, _ = _job(tmp_path)
    credential = _delegated_credential(job, api_key="delegated-key")
    run_calls: list[int] = []
    monkeypatch.setattr(
        AgentCommandPipelineExecutor,
        "_run",
        lambda *a, **k: run_calls.append(1),
    )

    def fail_resolver(_cred: DelegatedModelCredential) -> str:
        raise PipelineExecutionError("OPENMONTAGE_AGENT_MODEL_ID is not set")

    executor = AgentCommandPipelineExecutor(
        [str(tmp_path / "codex"), "exec", "-"],
        timeout_seconds=5,
        agent_model_resolver=fail_resolver,
    )

    with pytest.raises(PipelineExecutionError, match="AGENT_MODEL_ID"):
        executor.execute(
            StageAssignment.from_job(
                job, stage="research", stage_attempt=1, projects_dir=projects_dir
            ),
            credential=credential,
        )

    assert run_calls == []  # the Agent process never launched


def test_non_codex_delegated_executor_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-Codex executors cannot start a delegated paid stage without a model lock."""
    job, projects_dir, _ = _job(tmp_path)
    credential = _delegated_credential(job, api_key="delegated-key")
    resolver_calls: list[DelegatedModelCredential] = []
    executor = AgentCommandPipelineExecutor(
        [sys.executable, "-c", WRITE_CHECKPOINT_SCRIPT, str(tmp_path / "prompt.txt")],
        timeout_seconds=5,
        agent_model_resolver=lambda cred: resolver_calls.append(cred)
        or "must-not-be-used",
    )

    with pytest.raises(PipelineExecutionError, match="tenant-catalog model-lock"):
        executor.execute(
            StageAssignment.from_job(
                job, stage="research", stage_attempt=1, projects_dir=projects_dir
            ),
            credential=credential,
        )

    assert resolver_calls == []  # model resolution is Codex-only


def test_executor_reports_nonzero_exit_without_accepting_a_checkpoint(tmp_path: Path) -> None:
    job, projects_dir, _ = _job(tmp_path)
    diagnostic_script = r"""
import json
import sys

sys.stderr.write("connection refused with Bearer top-secret sk-abc123\n")
sys.stderr.write("ANTHROPIC_API_KEY=anthropic-provider-secret\n")
sys.stderr.write("AWS_SECRET_ACCESS_KEY=aws-provider-secret\n")
sys.stderr.write("OPENMONTAGE_SERVICE_TOKEN=service-provider-secret\n")
sys.stderr.write("FAL_KEY=fal-provider-secret\n")
sys.stderr.write("GOOGLE_APPLICATION_CREDENTIALS=google-provider-secret\n")
sys.stderr.write("AWS_ACCESS_KEY_ID=aws-key-id-provider-secret\n")
sys.stderr.write(json.dumps({
    "ANTHROPIC_API_KEY": "anthropic-json-secret",
    "AWS_SECRET_ACCESS_KEY": "aws-json-secret",
    "OPENMONTAGE_SERVICE_TOKEN": "service-json-secret",
    "FAL_KEY": "fal-json-secret",
    "GOOGLE_APPLICATION_CREDENTIALS": "google-json-secret",
    "AWS_ACCESS_KEY_ID": "aws-key-id-json-secret",
    "request_id": "req-safe-context",
}) + "\n")
sys.exit(7)
"""
    executor = AgentCommandPipelineExecutor(
        [sys.executable, "-c", diagnostic_script],
        timeout_seconds=5,
    )

    with pytest.raises(PipelineExecutionError, match="exit code 7") as error_info:
        executor.execute(StageAssignment.from_job(job, stage="research", stage_attempt=1, projects_dir=projects_dir))

    log_text = (
        projects_dir
        / job.job_id
        / ".openmontage"
        / "executor"
        / "research-attempt-1.json"
    ).read_text(encoding="utf-8")
    log = json.loads(log_text)
    exception_text = str(error_info.value)
    for secret in (
        "anthropic-provider-secret",
        "aws-provider-secret",
        "service-provider-secret",
        "anthropic-json-secret",
        "aws-json-secret",
        "service-json-secret",
        "fal-provider-secret",
        "google-provider-secret",
        "aws-key-id-provider-secret",
        "fal-json-secret",
        "google-json-secret",
        "aws-key-id-json-secret",
    ):
        assert secret not in exception_text
        assert secret not in log_text
    assert log["diagnosticCode"] == "MODEL_NETWORK_ERROR"
    assert "connection refused" in log["stderrTail"]
    assert "req-safe-context" in exception_text
    assert "req-safe-context" in log["stderrTail"]
    assert "top-secret" not in log["stderrTail"]
    assert "sk-abc123" not in log["stderrTail"]


def test_executor_classifies_missing_responses_route_as_endpoint_configuration_error(
    tmp_path: Path,
) -> None:
    job, projects_dir, _ = _job(tmp_path)
    diagnostic_script = """
import sys
sys.stderr.write("Reconnecting after connection failure: 404 Cannot POST /api/v1/responses\\n")
sys.exit(7)
"""
    executor = AgentCommandPipelineExecutor(
        [sys.executable, "-c", diagnostic_script],
        timeout_seconds=5,
    )

    with pytest.raises(PipelineExecutionError):
        executor.execute(
            StageAssignment.from_job(
                job,
                stage="research",
                stage_attempt=1,
                projects_dir=projects_dir,
            )
        )

    log = json.loads(
        (
            projects_dir
            / job.job_id
            / ".openmontage"
            / "executor"
            / "research-attempt-1.json"
        ).read_text(encoding="utf-8")
    )
    assert log["diagnosticCode"] == "MODEL_ENDPOINT_CONFIGURATION_ERROR"


def test_executor_redacts_delegated_credential_and_structured_secrets_from_diagnostics(
    tmp_path: Path,
) -> None:
    job, projects_dir, _ = _job(tmp_path)
    credential = _delegated_credential(
        job,
        api_key="raw-delegated-runtime-secret",
    )
    diagnostic_script = r"""
import json
import os
import sys

sys.stderr.write("provider request failed\n")
sys.stderr.write(os.environ["DOFE_MODEL_API_KEY"] + "\n")
sys.stderr.write(json.dumps({
    "token": "json-token-secret",
    "api_key": "escaped-secret-prefix-\"escaped-secret-suffix",
    "access_token": "oauth-access-secret",
    "refresh_token": "oauth-refresh-secret",
    "id_token": "oauth-id-secret",
}) + "\n")
sys.stderr.write("https://models.test/v1/responses?token=query-token-secret&request_id=req-1\n")
sys.exit(7)
"""
    executor = AgentCommandPipelineExecutor(
        _codex_command(tmp_path, sys.executable, "-c", diagnostic_script),
        timeout_seconds=5,
        agent_model_resolver=lambda _cred: "catalog-agent-model",
    )

    with pytest.raises(PipelineExecutionError) as error_info:
        executor.execute(
            StageAssignment.from_job(
                job,
                stage="research",
                stage_attempt=1,
                projects_dir=projects_dir,
            ),
            credential=credential,
        )

    log_path = (
        projects_dir
        / job.job_id
        / ".openmontage"
        / "executor"
        / "research-attempt-1.json"
    )
    exception_text = str(error_info.value)
    log_text = log_path.read_text(encoding="utf-8")
    for secret in (
        credential.api_key,
        "json-token-secret",
        "escaped-secret-prefix",
        "escaped-secret-suffix",
        "oauth-access-secret",
        "oauth-refresh-secret",
        "oauth-id-secret",
        "query-token-secret",
    ):
        assert secret not in exception_text
        assert secret not in log_text
    assert "provider request failed" in exception_text
    assert "provider request failed" in json.loads(log_text)["stderrTail"]


@pytest.mark.parametrize(
    ("api_key", "secondary_secret"),
    [
        ("token", "secondary-provider-alpha"),
        ("secret", "secondary-provider-beta"),
        ("api_key", "secondary-provider-gamma"),
    ],
)
def test_executor_redacts_structured_secrets_before_short_runtime_credentials(
    tmp_path: Path,
    api_key: str,
    secondary_secret: str,
) -> None:
    job, projects_dir, _ = _job(tmp_path)
    credential = _delegated_credential(job, api_key=api_key)
    diagnostic_script = r"""
import json
import os
import sys

sys.stderr.write("short credential provider failure\n")
sys.stderr.write(os.environ["DOFE_MODEL_API_KEY"] + "\n")
sys.stderr.write(json.dumps({sys.argv[1]: sys.argv[2]}) + "\n")
sys.exit(7)
"""
    executor = AgentCommandPipelineExecutor(
        _codex_command(tmp_path, sys.executable, "-c", diagnostic_script, api_key, secondary_secret),
        timeout_seconds=5,
        agent_model_resolver=lambda _cred: "catalog-agent-model",
    )

    with pytest.raises(PipelineExecutionError) as error_info:
        executor.execute(
            StageAssignment.from_job(
                job,
                stage="research",
                stage_attempt=1,
                projects_dir=projects_dir,
            ),
            credential=credential,
        )

    log_text = (
        projects_dir
        / job.job_id
        / ".openmontage"
        / "executor"
        / "research-attempt-1.json"
    ).read_text(encoding="utf-8")
    exception_text = str(error_info.value)
    assert api_key not in exception_text
    assert api_key not in log_text
    assert secondary_secret not in exception_text
    assert secondary_secret not in log_text
    assert "short credential provider failure" in exception_text
    assert "short credential provider failure" in json.loads(log_text)["stderrTail"]


def test_executor_redacts_process_start_error_from_the_full_exception_chain(
    tmp_path: Path,
) -> None:
    job, projects_dir, _ = _job(tmp_path)
    credential = _delegated_credential(job, api_key="os-error-runtime-secret")
    query_secret = "os-error-query-secret"
    executor = AgentCommandPipelineExecutor(
        _codex_command(tmp_path, f"/definitely-missing/{credential.api_key}?token={query_secret}"),
        timeout_seconds=5,
        agent_model_resolver=lambda _cred: "catalog-agent-model",
    )

    with pytest.raises(PipelineExecutionError) as error_info:
        executor.execute(
            StageAssignment.from_job(
                job,
                stage="research",
                stage_attempt=1,
                projects_dir=projects_dir,
            ),
            credential=credential,
        )

    diagnostic_text = _exception_chain_diagnostics(error_info.value)
    log_text = (
        projects_dir
        / job.job_id
        / ".openmontage"
        / "executor"
        / "research-attempt-1.json"
    ).read_text(encoding="utf-8")

    assert credential.api_key not in diagnostic_text
    assert query_secret not in diagnostic_text
    assert credential.api_key not in log_text
    assert query_secret not in log_text
    assert "No such file or directory" in diagnostic_text
    assert "No such file or directory" in json.loads(log_text)["stderrTail"]


def test_executor_persists_timeout_diagnostics(tmp_path: Path) -> None:
    job, projects_dir, _ = _job(tmp_path)
    credential = _delegated_credential(job, api_key="timeout-runtime-secret")
    oauth_query_secret = "timeout-oauth-query-secret"
    executor = AgentCommandPipelineExecutor(
        _codex_command(
            tmp_path,
            sys.executable,
            "-c",
            # Write+flush the partial-context marker as the FIRST statement so it
            # lands in the pipe within microseconds of the child starting, before
            # any env access or heavier imports. The exec path runs two Python
            # startups (fake codex wrapper -> this -c); a 0.5s deadline raced
            # against child scheduling under load and occasionally captured an
            # empty stderr, so the marker is emitted first and the deadline below
            # gives ample headroom while still exercising the timeout path.
            "import sys; sys.stderr.write('partial timeout context\\n'); sys.stderr.flush(); import os, time; sys.stderr.write(os.environ['DOFE_MODEL_API_KEY']); sys.stderr.flush(); time.sleep(5)",
            f"https://models.test/v1/responses?access_token={oauth_query_secret}&request_id=timeout-1",
        ),
        timeout_seconds=1.5,
        agent_model_resolver=lambda _cred: "catalog-agent-model",
    )

    with pytest.raises(PipelineExecutionError, match="timed out") as error_info:
        executor.execute(
            StageAssignment.from_job(
                job,
                stage="research",
                stage_attempt=1,
                projects_dir=projects_dir,
            ),
            credential=credential,
        )

    log_text = (
        projects_dir
        / job.job_id
        / ".openmontage"
        / "executor"
        / "research-attempt-1.json"
    ).read_text(encoding="utf-8")
    log = json.loads(log_text)
    traceback_text = _exception_chain_diagnostics(error_info.value)
    assert credential.api_key not in str(error_info.value)
    assert credential.api_key not in traceback_text
    assert oauth_query_secret not in traceback_text
    assert credential.api_key not in log_text
    assert (
        "https://models.test/v1/responses?access_token=[REDACTED]&request_id=timeout-1"
        in traceback_text
    )
    assert log["timedOut"] is True
    assert log["returnCode"] == -1
    assert log["diagnosticCode"] == "EXECUTION_TIMEOUT"
    assert "partial timeout context" in log["stderrTail"]


def test_executor_preserves_timeout_output_types_while_redacting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, projects_dir, _ = _job(tmp_path)
    credential = _delegated_credential(job, api_key="bytes-runtime-secret")
    executor = AgentCommandPipelineExecutor(
        _codex_command(tmp_path, sys.executable, "-c", "pass"),
        timeout_seconds=5,
        agent_model_resolver=lambda _cred: "catalog-agent-model",
    )

    def raise_timeout(*_args, **_kwargs):
        error = subprocess.TimeoutExpired(
            cmd=b"runner?FAL_KEY=cmd-bytes-secret&request_id=cmd-safe",
            timeout=1.25,
            output=b"bytes timeout context bytes-runtime-secret",
            stderr=None,
        )
        error.args = (
            [b"FAL_KEY=list-bytes-secret"],
            (b"AWS_ACCESS_KEY_ID=tuple-bytes-secret",),
            ("nested", [(b"GOOGLE_APPLICATION_CREDENTIALS=nested-bytes-secret",)]),
            1.25,
        )
        raise error

    monkeypatch.setattr(executor, "_run", raise_timeout)

    with pytest.raises(PipelineExecutionError, match="timed out") as error_info:
        executor.execute(
            StageAssignment.from_job(
                job,
                stage="research",
                stage_attempt=1,
                projects_dir=projects_dir,
            ),
            credential=credential,
        )

    cause = error_info.value.__cause__
    assert isinstance(cause, subprocess.TimeoutExpired)
    assert isinstance(cause.output, bytes)
    assert b"bytes-runtime-secret" not in cause.output
    assert b"bytes timeout context" in cause.output
    assert cause.stderr is None
    diagnostic_text = _exception_chain_diagnostics(error_info.value)
    for secret in (
        "cmd-bytes-secret",
        "list-bytes-secret",
        "tuple-bytes-secret",
        "nested-bytes-secret",
    ):
        assert secret not in diagnostic_text
    assert isinstance(cause.cmd, bytes)
    assert cause.cmd == b"runner?FAL_KEY=[REDACTED]&request_id=cmd-safe"
    assert isinstance(cause.args, tuple)
    assert isinstance(cause.args[0], list)
    assert cause.args[0] == [b"FAL_KEY=[REDACTED]"]
    assert isinstance(cause.args[1], tuple)
    assert cause.args[1] == (b"AWS_ACCESS_KEY_ID=[REDACTED]",)
    assert isinstance(cause.args[2], tuple)
    assert isinstance(cause.args[2][1], list)
    assert isinstance(cause.args[2][1][0], tuple)
    assert cause.args[2][1][0] == (
        b"GOOGLE_APPLICATION_CREDENTIALS=[REDACTED]",
    )
    assert cause.args[-1] == 1.25
    assert cause.timeout == 1.25
    log_text = (
        projects_dir
        / job.job_id
        / ".openmontage"
        / "executor"
        / "research-attempt-1.json"
    ).read_text(encoding="utf-8")
    assert credential.api_key not in log_text
    assert "bytes timeout context" in json.loads(log_text)["stdoutTail"]


def test_executor_terminates_process_when_job_cancellation_is_requested(tmp_path: Path) -> None:
    job, projects_dir, _ = _job(tmp_path)
    credential = _delegated_credential(job, api_key="cancel-runtime-secret")
    oauth_query_secret = "cancel-oauth-query-secret"
    started = time.monotonic()
    executor = AgentCommandPipelineExecutor(
        _codex_command(
            tmp_path,
            sys.executable,
            "-c",
            "import os,sys,time; sys.stderr.write('cancel context\\n' + os.environ['DOFE_MODEL_API_KEY']); sys.stderr.flush(); time.sleep(30)",
            f"https://models.test/v1/responses?access_token={oauth_query_secret}&request_id=cancel-1",
        ),
        timeout_seconds=30,
        agent_model_resolver=lambda _cred: "catalog-agent-model",
    )

    with pytest.raises(PipelineExecutionCancelled) as error_info:
        executor.execute(
            StageAssignment.from_job(job, stage="research", stage_attempt=1, projects_dir=projects_dir),
            credential=credential,
            cancellation_requested=lambda: time.monotonic() - started > 0.3,
        )

    assert time.monotonic() - started < 5
    log_path = projects_dir / job.job_id / ".openmontage" / "executor" / "research-attempt-1.json"
    log_text = log_path.read_text(encoding="utf-8")
    log = json.loads(log_text)
    diagnostic_text = _exception_chain_diagnostics(error_info.value)
    assert credential.api_key not in error_info.value.stderr
    assert credential.api_key not in diagnostic_text
    assert oauth_query_secret not in diagnostic_text
    assert credential.api_key not in log_text
    assert (
        "https://models.test/v1/responses?access_token=[REDACTED]&request_id=cancel-1"
        in diagnostic_text
    )
    assert "cancel context" in error_info.value.stderr
    assert "cancel context" in log["stderrTail"]
    assert log["diagnosticCode"] == "EXECUTION_CANCELLED"
    assert log["error"] == "job_cancel_requested"


def test_executor_redacts_delegated_credential_from_success_log(tmp_path: Path) -> None:
    job, projects_dir, _ = _job(tmp_path)
    credential = _delegated_credential(job, api_key="success-runtime-secret")
    script = (
        "import os,sys\n"
        "sys.stderr.write('success context\\n' + os.environ['DOFE_MODEL_API_KEY'] + '\\n')\n"
        + WRITE_CHECKPOINT_SCRIPT
    )
    executor = AgentCommandPipelineExecutor(
        _codex_command(
            tmp_path,
            sys.executable,
            "-c",
            script,
            str(tmp_path / "success-prompt.txt"),
        ),
        timeout_seconds=5,
        agent_model_resolver=lambda _cred: "catalog-agent-model",
    )

    result = executor.execute(
        StageAssignment.from_job(
            job,
            stage="research",
            stage_attempt=1,
            projects_dir=projects_dir,
        ),
        credential=credential,
    )

    log_text = (
        projects_dir
        / job.job_id
        / ".openmontage"
        / "executor"
        / "research-attempt-1.json"
    ).read_text(encoding="utf-8")
    assert result.status == "in_progress"
    assert credential.api_key not in log_text
    assert "success context" in json.loads(log_text)["stderrTail"]


def test_executor_reports_timeout(tmp_path: Path) -> None:
    job, projects_dir, _ = _job(tmp_path)
    executor = AgentCommandPipelineExecutor(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        timeout_seconds=0.01,
    )

    with pytest.raises(PipelineExecutionError, match="timed out"):
        executor.execute(
            StageAssignment.from_job(
                job,
                stage="research",
                stage_attempt=1,
                projects_dir=projects_dir,
            )
        )


def test_executor_requires_a_valid_stage_checkpoint(tmp_path: Path) -> None:
    job, projects_dir, _ = _job(tmp_path)
    executor = AgentCommandPipelineExecutor(
        [sys.executable, "-c", "pass"],
        timeout_seconds=5,
    )

    with pytest.raises(PipelineExecutionIncomplete, match="checkpoint"):
        executor.execute(
            StageAssignment.from_job(
                job,
                stage="research",
                stage_attempt=1,
                projects_dir=projects_dir,
            )
        )
