"""External Agent execution adapter for one checkpoint-backed pipeline stage."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from lib.checkpoint import CheckpointValidationError, read_checkpoint
from lib.pipeline_loader import get_stage_skill, load_pipeline_readonly
from openmontage.contracts import JobSnapshot
from openmontage.delegation_proxy import DelegationSigningProxy
from openmontage.invocation_store import ModelInvocationStore
from tools.dofe.client import DofeClient
from tools.dofe.delegation import DelegatedModelCredential
from tools.dofe.errors import DofeError
from tools.dofe.models import validate_catalog_alias


ROOT = Path(__file__).resolve().parent.parent
_CONTROL_PLANE_SECRET_ENV = {
    "INTERNAL_API_SECRET",
    "MODELS_INTERNAL_API_SECRET",
    "OPENMONTAGE_EVENT_SIGNING_SECRET",
    "OPENMONTAGE_SERVICE_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "FAL_KEY",
}
_SECRET_ENV_SUFFIX_PATTERN = (
    r"(?:_API_KEY|_ACCESS_KEY|_ACCESSKEY|_PASSWORD|_SECRET|_SECRET_KEY|"
    r"_SECRETKEY|_TOKEN)"
)
_SECRET_ENV_SUFFIX = re.compile(rf"{_SECRET_ENV_SUFFIX_PATTERN}$")
_DIAGNOSTIC_LIMIT = 4096
_BEARER_SECRET = re.compile(r"(?i)(\bBearer\s+)[^\s,;]+")
_SECRET_ENV_NAME = rf"[A-Z][A-Z0-9_]*{_SECRET_ENV_SUFFIX_PATTERN}"
_EXACT_SECRET_NAME = "(?:" + "|".join(
    re.escape(name)
    for name in sorted(_CONTROL_PLANE_SECRET_ENV | {"AWS_ACCESS_KEY_ID"})
) + ")"
_SECRET_NAME = (
    rf"(?:{_EXACT_SECRET_NAME}|{_SECRET_ENV_NAME}|api[_ -]?key|access[_ -]?key|"
    r"access[_ -]?token|"
    r"refresh[_ -]?token|"
    r"id[_ -]?token|token|secret|password|authorization)"
)
_JSON_SECRET_VALUE = re.compile(
    rf'(?i)("{_SECRET_NAME}"\s*:\s*")(?:(?:\\.)|[^"\\])*(")'
)
_SINGLE_QUOTED_SECRET_VALUE = re.compile(
    rf"(?i)('{_SECRET_NAME}'\s*:\s*')(?:(?:\\.)|[^'\\])*(')"
)
_SECRET_VALUE = re.compile(
    rf"(?i)(\b{_SECRET_NAME}\b\s*[:=]\s*)[^\s,;&]+"
)
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]+")


class PipelineExecutionError(RuntimeError):
    """Raised when the configured external Agent cannot execute safely."""


class PipelineExecutionCancelled(PipelineExecutionError):
    """Raised when the durable Job requests cancellation during Agent execution."""

    def __init__(self, *, stdout: Any = "", stderr: Any = "", return_code: int = -15) -> None:
        self.stdout = _output_text(stdout)
        self.stderr = _output_text(stderr)
        self.return_code = return_code
        super().__init__("External Agent executor cancelled by Job request")


class PipelineExecutionIncomplete(PipelineExecutionError):
    """Raised when an Agent exits without a valid checkpoint fact."""


@dataclass(frozen=True)
class StageAssignment:
    job_id: str
    project_id: str
    projects_dir: Path
    project_dir: Path
    pipeline: str
    pipeline_version: str
    stage: str
    stage_attempt: int
    director_skill: str | None
    request: dict[str, Any]
    attribution: dict[str, Any]
    job_snapshot: dict[str, Any]
    local_inputs: tuple[dict[str, Any], ...]

    @classmethod
    def from_job(
        cls,
        job: JobSnapshot,
        *,
        stage: str,
        stage_attempt: int,
        projects_dir: str | Path,
        local_inputs: Sequence[dict[str, Any]] = (),
    ) -> "StageAssignment":
        if stage_attempt < 1:
            raise PipelineExecutionError("stage_attempt must be greater than zero")
        if stage not in {item.code for item in job.stages}:
            raise PipelineExecutionError(
                f"Stage {stage!r} does not belong to Job workflow {job.workflow.name!r}"
            )
        base = Path(projects_dir).expanduser().resolve()
        manifest = load_pipeline_readonly(job.workflow.name)
        skill = get_stage_skill(manifest, stage)
        return cls(
            job_id=job.job_id,
            project_id=job.job_id,
            projects_dir=base,
            project_dir=base / job.job_id,
            pipeline=job.workflow.name,
            pipeline_version=job.workflow.version,
            stage=stage,
            stage_attempt=stage_attempt,
            director_skill=f"skills/{skill}.md" if skill else None,
            request=job.request.to_wire(),
            attribution=job.attribution.to_wire(),
            job_snapshot=job.to_wire(),
            local_inputs=tuple(dict(item) for item in local_inputs),
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "jobId": self.job_id,
            "projectId": self.project_id,
            "projectsDir": str(self.projects_dir),
            "projectDir": str(self.project_dir),
            "pipeline": self.pipeline,
            "pipelineVersion": self.pipeline_version,
            "stage": self.stage,
            "stageAttempt": self.stage_attempt,
            "directorSkill": self.director_skill,
            "request": self.request,
            "attribution": self.attribution,
            "jobSnapshot": self.job_snapshot,
            "localInputs": list(self.local_inputs),
        }


@dataclass(frozen=True)
class PipelineExecutionResult:
    status: str
    checkpoint: dict[str, Any]
    assignment_path: Path


class PipelineExecutor(Protocol):
    def execute(
        self,
        assignment: StageAssignment,
        *,
        credential: DelegatedModelCredential | None = None,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> PipelineExecutionResult:
        """Execute one stage and return the checkpoint-backed outcome."""


class AgentCommandPipelineExecutor:
    """Invoke a configured Agent argv and reconcile its durable checkpoint."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = 3600,
        invocation_store: ModelInvocationStore | None = None,
        agent_model_resolver: Callable[[DelegatedModelCredential], str] | None = None,
    ) -> None:
        self.command = _validate_command(command)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise PipelineExecutionError("timeout_seconds must be greater than zero")
        self.timeout_seconds = timeout_seconds
        self.invocation_store = invocation_store
        self._agent_model_resolver = (
            agent_model_resolver or _resolve_delegated_agent_model
        )

    @classmethod
    def from_environment(
        cls,
        *,
        invocation_store: ModelInvocationStore | None = None,
        agent_model_resolver: Callable[[DelegatedModelCredential], str] | None = None,
    ) -> "AgentCommandPipelineExecutor":
        raw = os.environ.get("OPENMONTAGE_AGENT_EXECUTOR_JSON", "")
        try:
            command = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise PipelineExecutionError(
                "OPENMONTAGE_AGENT_EXECUTOR_JSON must be a nonempty JSON argv array"
            ) from exc
        if not isinstance(command, list):
            raise PipelineExecutionError(
                "OPENMONTAGE_AGENT_EXECUTOR_JSON must be a nonempty JSON argv array"
            )
        raw_timeout = os.environ.get("OPENMONTAGE_AGENT_TIMEOUT_SECONDS", "3600")
        try:
            timeout_seconds = float(raw_timeout)
        except ValueError as exc:
            raise PipelineExecutionError(
                "OPENMONTAGE_AGENT_TIMEOUT_SECONDS must be a positive number"
            ) from exc
        return cls(
            command,
            timeout_seconds=timeout_seconds,
            invocation_store=invocation_store,
            agent_model_resolver=agent_model_resolver,
        )

    def execute(
        self,
        assignment: StageAssignment,
        *,
        credential: DelegatedModelCredential | None = None,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> PipelineExecutionResult:
        sensitive_values = (credential.api_key,) if credential is not None else ()
        assignment.project_dir.mkdir(parents=True, exist_ok=True)
        assignment_dir = assignment.project_dir / ".openmontage" / "assignments"
        assignment_dir.mkdir(parents=True, exist_ok=True)
        assignment_path = assignment_dir / (
            f"{assignment.stage}-attempt-{assignment.stage_attempt}.json"
        )
        _atomic_write_json(assignment_path, assignment.to_wire())
        prompt = _stage_prompt(assignment, assignment_path)
        checkpoint_inventory = _checkpoint_inventory(assignment.project_dir)
        unexpected_checkpoints = _unexpected_existing_checkpoints(
            assignment,
            checkpoint_inventory,
        )
        if unexpected_checkpoints:
            raise PipelineExecutionError(
                "External Agent executor found checkpoints outside the assigned stage progression: "
                + ", ".join(unexpected_checkpoints)
            )
        command = tuple(
            str(assignment.project_dir) if value == "{project_dir}" else value
            for value in self.command
        )

        try:
            if credential is None:
                completed = self._run(
                    command,
                    prompt,
                    environment=None,
                    cancellation_requested=cancellation_requested,
                )
            else:
                with DelegationSigningProxy(
                    credential,
                    invocation_store=self.invocation_store,
                    job_id=assignment.job_id,
                    stage=assignment.stage,
                    stage_attempt=assignment.stage_attempt,
                ) as proxy:
                    openai_base_url = f"{proxy.base_url}/v1"
                    environment = {
                        key: value
                        for key, value in os.environ.items()
                        if key not in _CONTROL_PLANE_SECRET_ENV
                        and not _SECRET_ENV_SUFFIX.search(key)
                    }
                    environment.update(
                        credential.agent_environment(
                            openai_base_url=openai_base_url,
                            dofe_base_url=proxy.base_url,
                        )
                    )
                    # Lock a catalog-verified model into the delegated Codex
                    # executor before any paid task is created (dev-guide
                    # §model-catalog). Other executors do not implement the
                    # delegated model-lock protocol, so they are fail-closed
                    # rather than allowed to choose their own model.
                    if not _is_codex_exec_command(command):
                        raise PipelineExecutionError(
                            "Delegated model execution is only supported for Codex; "
                            f"the configured executor {command[0]!r} does not implement "
                            "the tenant-catalog model-lock protocol"
                        )
                    delegated_model = self._agent_model_resolver(credential)
                    completed = self._run(
                        _configure_agent_command_for_delegation(
                            command,
                            openai_base_url,
                            model=delegated_model,
                        ),
                        prompt,
                        environment=environment,
                        cancellation_requested=cancellation_requested,
                    )
        except PipelineExecutionCancelled as exc:
            code = "EXECUTION_CANCELLED"
            _redact_exception_chain(exc, sensitive_values)
            _write_execution_log(
                assignment,
                stdout=exc.stdout,
                stderr=exc.stderr,
                return_code=exc.return_code,
                diagnostic_code=code,
                error="job_cancel_requested",
                sensitive_values=sensitive_values,
            )
            raise
        except subprocess.TimeoutExpired as exc:
            _redact_exception_chain(exc, sensitive_values)
            stdout = _output_text(exc.stdout)
            stderr = _output_text(exc.stderr)
            code = "EXECUTION_TIMEOUT"
            _write_execution_log(
                assignment,
                stdout=stdout,
                stderr=stderr,
                return_code=-1,
                diagnostic_code=code,
                error="timeout",
                timed_out=True,
                sensitive_values=sensitive_values,
            )
            raise PipelineExecutionError(
                f"External Agent executor timed out after {self.timeout_seconds:g} seconds"
                f" ({code}){_diagnostic_suffix(stdout, stderr, sensitive_values)}"
            ) from exc
        except OSError as exc:
            code = "CLI_CONFIGURATION_ERROR"
            _redact_exception_chain(exc, sensitive_values)
            error_text = _redact_diagnostic(str(exc), sensitive_values)
            _write_execution_log(
                assignment,
                stdout="",
                stderr=error_text,
                return_code=-1,
                diagnostic_code=code,
                error="process_start_failed",
                sensitive_values=sensitive_values,
            )
            raise PipelineExecutionError(
                f"External Agent executor could not be started ({code})"
                f"{_diagnostic_suffix('', error_text, sensitive_values)}"
            ) from exc

        diagnostic_code = _classify_diagnostic(completed.stdout, completed.stderr)
        _write_execution_log(
            assignment,
            stdout=_output_text(completed.stdout),
            stderr=_output_text(completed.stderr),
            return_code=completed.returncode,
            diagnostic_code=diagnostic_code,
            sensitive_values=sensitive_values,
        )
        changed_checkpoints = _changed_unassigned_checkpoints(
            assignment,
            checkpoint_inventory,
        )
        if changed_checkpoints:
            raise PipelineExecutionError(
                "External Agent executor changed checkpoints outside the assigned stage: "
                + ", ".join(changed_checkpoints)
            )
        if completed.returncode != 0:
            raise PipelineExecutionError(
                f"External Agent executor exited with exit code {completed.returncode}"
                f" ({diagnostic_code})"
                f"{_diagnostic_suffix(completed.stdout, completed.stderr, sensitive_values)}"
            )

        try:
            checkpoint = read_checkpoint(
                assignment.projects_dir,
                assignment.project_id,
                assignment.stage,
            )
        except (CheckpointValidationError, json.JSONDecodeError, OSError) as exc:
            raise PipelineExecutionError(
                "External Agent executor wrote an invalid stage checkpoint"
            ) from exc
        if checkpoint is None:
            raise PipelineExecutionIncomplete(
                "External Agent executor exited without a stage checkpoint"
            )
        if (
            checkpoint.get("project_id") != assignment.project_id
            or checkpoint.get("pipeline_type") != assignment.pipeline
            or checkpoint.get("stage") != assignment.stage
        ):
            raise PipelineExecutionError(
                "External Agent executor checkpoint identity does not match the assignment"
            )
        return PipelineExecutionResult(
            status=str(checkpoint["status"]),
            checkpoint=checkpoint,
            assignment_path=assignment_path,
        )

    def _run(
        self,
        command: tuple[str, ...],
        prompt: str,
        *,
        environment: dict[str, str] | None,
        cancellation_requested: Callable[[], bool] | None,
    ):
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            text=True,
            shell=False,
            env=environment,
            start_new_session=os.name == "posix",
        )
        input_pending = True
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stdout, stderr = _terminate_process(process)
                    raise subprocess.TimeoutExpired(
                        command,
                        self.timeout_seconds,
                        output=stdout,
                        stderr=stderr,
                    )
                try:
                    stdout, stderr = process.communicate(
                        input=prompt if input_pending else None,
                        timeout=min(0.25, remaining),
                    )
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired as exc:
                    input_pending = False
                    if cancellation_requested is not None and cancellation_requested():
                        stdout, stderr = _terminate_process(process)
                        raise PipelineExecutionCancelled(
                            stdout=stdout or exc.stdout,
                            stderr=stderr or exc.stderr,
                            return_code=process.returncode or -15,
                        )
                    if time.monotonic() >= deadline:
                        stdout, stderr = _terminate_process(process)
                        raise subprocess.TimeoutExpired(
                            command,
                            self.timeout_seconds,
                            output=stdout or exc.stdout,
                            stderr=stderr or exc.stderr,
                        )
        except BaseException:
            if process.poll() is None:
                _terminate_process(process)
            raise


def _terminate_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Terminate a CLI process tree and preserve whatever output was captured."""
    if process.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
    try:
        return process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        return process.communicate()


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)) or not command:
        raise PipelineExecutionError(
            "OPENMONTAGE_AGENT_EXECUTOR_JSON must be a nonempty JSON argv array"
        )
    if any(not isinstance(value, str) or not value.strip() for value in command):
        raise PipelineExecutionError(
            "OPENMONTAGE_AGENT_EXECUTOR_JSON must be a nonempty JSON argv array"
        )
    return tuple(command)


def _is_codex_exec_command(command: Sequence[str]) -> bool:
    """True when the Agent argv is a Codex ``exec`` invocation.

    Accepts both Unix ``codex`` and Windows ``codex.exe`` so absolute paths or
    explicit executables on either platform enter the delegated model-lock path.
    """

    if not command:
        return False
    # Normalize backslashes so Windows paths parse correctly on POSIX as well.
    name = Path(str(command[0]).replace("\\", "/")).name.lower()
    if name not in {"codex", "codex.exe"}:
        return False
    try:
        command.index("exec", 1)
    except ValueError:
        return False
    return True


def _resolve_delegated_agent_model(credential: DelegatedModelCredential) -> str:
    """Return a tenant-catalog-verified model id for the delegated Codex executor.

    The candidate is read from ``OPENMONTAGE_AGENT_MODEL_ID`` and must be visible
    in a live ``GET /v1/models`` for the delegated credential. Fail closed — no
    paid task is created while the model is unset, unreachable, or invisible.
    """

    candidate = os.environ.get("OPENMONTAGE_AGENT_MODEL_ID", "").strip()
    if not candidate:
        raise PipelineExecutionError(
            "OPENMONTAGE_AGENT_MODEL_ID is not set; the delegated Agent executor "
            "requires a tenant-catalog-verified model before any paid task"
        )
    try:
        catalog = DofeClient(
            api_key=credential.api_key,
            base_url=credential.models_base_url,
        ).list_models()
        return validate_catalog_alias(candidate, catalog)
    except DofeError as exc:
        raise PipelineExecutionError(
            f"Delegated Agent model {candidate!r} is not visible to the tenant "
            f"model catalog: {exc}"
        ) from exc


def _configure_agent_command_for_delegation(
    command: tuple[str, ...],
    openai_base_url: str,
    *,
    model: str | None = None,
) -> tuple[str, ...]:
    if not _is_codex_exec_command(command):
        return command
    exec_index = command.index("exec", 1)

    provider = "dofe-delegated"
    provider_config = (
        "-c",
        f'model_provider="{provider}"',
        "-c",
        f'model_providers.{provider}.name="DoFe delegated gateway"',
        "-c",
        f"model_providers.{provider}.base_url={json.dumps(openai_base_url)}",
        "-c",
        f'model_providers.{provider}.wire_api="responses"',
        "-c",
        f"model_providers.{provider}.supports_websockets=false",
        "-c",
        f"model_providers.{provider}.requires_openai_auth=true",
    )
    # The catalog-verified model pins exactly which Responses model Codex calls,
    # so execution never silently falls back to the host's default model.
    if model:
        provider_config = provider_config + ("-c", f"model={json.dumps(model)}")
    return command[:exec_index] + provider_config + command[exec_index:]


def _stage_prompt(assignment: StageAssignment, assignment_path: Path) -> str:
    skill_instruction = (
        f"Read `{assignment.director_skill}` before stage work."
        if assignment.director_skill
        else "Follow the stage contract declared in the pipeline manifest."
    )
    return "\n".join(
        [
            f"OPENMONTAGE_ASSIGNMENT_PATH={json.dumps(str(assignment_path))}",
            "Execute exactly one OpenMontage pipeline stage for the durable Job assignment above.",
            "Read `AGENT_GUIDE.md`, the assignment JSON, the named pipeline manifest, and "
            "`skills/meta/checkpoint-protocol.md` before acting.",
            skill_instruction,
            "Work only inside the assignment project directory and use the repository tool registry.",
            "Write an `in_progress` checkpoint before consequential work and refresh factual partial "
            "progress where available.",
            "Finish by writing exactly one valid checkpoint status: `completed`, `awaiting_human`, "
            "`in_progress`, or `failed`.",
            "At a human gate, write `awaiting_human` and stop. Do not execute a later stage.",
            "Do not print media bytes or secrets. The checkpoint is the execution result.",
        ]
    )


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _checkpoint_inventory(project_dir: Path) -> dict[str, str]:
    inventory: dict[str, str] = {}
    for path in project_dir.glob("checkpoint_*.json"):
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            inventory[path.name] = f"non-regular:{stat.S_IFMT(mode)}"
            continue
        digest = hashlib.sha256()
        with path.open("rb") as checkpoint_file:
            for chunk in iter(lambda: checkpoint_file.read(64 * 1024), b""):
                digest.update(chunk)
        inventory[path.name] = digest.hexdigest()
    return inventory


def _changed_unassigned_checkpoints(
    assignment: StageAssignment,
    before: dict[str, str],
) -> list[str]:
    after = _checkpoint_inventory(assignment.project_dir)
    assigned_name = f"checkpoint_{assignment.stage}.json"
    return sorted(
        name
        for name in before.keys() | after.keys()
        if name != assigned_name and before.get(name) != after.get(name)
    )


def _unexpected_existing_checkpoints(
    assignment: StageAssignment,
    inventory: dict[str, str],
) -> list[str]:
    stages = assignment.job_snapshot.get("stages", [])
    stage_codes = [
        item.get("code")
        for item in stages
        if isinstance(item, dict) and isinstance(item.get("code"), str)
    ]
    assigned_index = stage_codes.index(assignment.stage)
    allowed_names = {
        f"checkpoint_{stage}.json" for stage in stage_codes[: assigned_index + 1]
    }
    return sorted(name for name in inventory if name not in allowed_names)


def _write_execution_log(
    assignment: StageAssignment,
    *,
    stdout: str,
    stderr: str,
    return_code: int,
    diagnostic_code: str | None = None,
    error: str | None = None,
    timed_out: bool = False,
    sensitive_values: Sequence[str] = (),
) -> None:
    log_dir = assignment.project_dir / ".openmontage" / "executor"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{assignment.stage}-attempt-{assignment.stage_attempt}.json"
    stdout_text = _output_text(stdout)
    stderr_text = _output_text(stderr)
    payload: dict[str, Any] = {
        "returnCode": return_code,
        "stdoutCharacters": len(stdout_text),
        "stderrCharacters": len(stderr_text),
        "stdoutTail": _diagnostic_excerpt(stdout_text, sensitive_values),
        "stderrTail": _diagnostic_excerpt(stderr_text, sensitive_values),
        "diagnosticCode": diagnostic_code or _classify_diagnostic(stdout_text, stderr_text),
        "timedOut": timed_out,
    }
    if error:
        payload["error"] = error
    _atomic_write_json(path, payload)


def _output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _redact_diagnostic(value: str, sensitive_values: Sequence[str] = ()) -> str:
    redacted = _BEARER_SECRET.sub(r"\1[REDACTED]", value)
    redacted = _JSON_SECRET_VALUE.sub(r"\1[REDACTED]\2", redacted)
    redacted = _SINGLE_QUOTED_SECRET_VALUE.sub(r"\1[REDACTED]\2", redacted)
    redacted = _SECRET_VALUE.sub(r"\1[REDACTED]", redacted)
    redacted = _OPENAI_KEY.sub("sk-[REDACTED]", redacted)
    for sensitive_value in sorted(set(sensitive_values), key=len, reverse=True):
        if sensitive_value:
            redacted = redacted.replace(sensitive_value, "[REDACTED]")
    return redacted


def _redact_os_error(error: OSError, sensitive_values: Sequence[str]) -> None:
    error.args = tuple(
        _redact_diagnostic(value, sensitive_values) if isinstance(value, str) else value
        for value in error.args
    )
    for attribute in ("filename", "filename2"):
        value = getattr(error, attribute, None)
        if isinstance(value, str):
            setattr(error, attribute, _redact_diagnostic(value, sensitive_values))


def _redact_exception_chain(
    error: BaseException,
    sensitive_values: Sequence[str],
) -> None:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, PipelineExecutionCancelled):
            current.stdout = _redact_exception_output(
                current.stdout,
                sensitive_values,
            )
            current.stderr = _redact_exception_output(
                current.stderr,
                sensitive_values,
            )
        if isinstance(current, subprocess.TimeoutExpired):
            current.cmd = _redact_exception_value(current.cmd, sensitive_values)
            current.args = _redact_exception_value(current.args, sensitive_values)
            current.output = _redact_exception_output(
                current.output,
                sensitive_values,
            )
            current.stderr = _redact_exception_output(
                current.stderr,
                sensitive_values,
            )
        if isinstance(current, OSError):
            _redact_os_error(current, sensitive_values)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


def _redact_exception_value(value: Any, sensitive_values: Sequence[str]) -> Any:
    if isinstance(value, str):
        return _redact_diagnostic(value, sensitive_values)
    if isinstance(value, bytes):
        return _redact_exception_output(value, sensitive_values)
    if isinstance(value, tuple):
        return tuple(
            _redact_exception_value(item, sensitive_values) for item in value
        )
    if isinstance(value, list):
        return [_redact_exception_value(item, sensitive_values) for item in value]
    return value


def _redact_exception_output(value: Any, sensitive_values: Sequence[str]) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        decoded = value.decode("utf-8", errors="surrogateescape")
        return _redact_diagnostic(decoded, sensitive_values).encode(
            "utf-8",
            errors="surrogateescape",
        )
    return _redact_diagnostic(_output_text(value), sensitive_values)


def _diagnostic_excerpt(value: str, sensitive_values: Sequence[str] = ()) -> str:
    redacted = _redact_diagnostic(value, sensitive_values)
    if len(redacted) <= _DIAGNOSTIC_LIMIT:
        return redacted
    return redacted[-_DIAGNOSTIC_LIMIT:]


def _classify_diagnostic(stdout: Any, stderr: Any) -> str:
    text = f"{_output_text(stderr)}\n{_output_text(stdout)}".lower()
    if any(marker in text for marker in ("command not found", "no such file or directory", "spawn", "executable")):
        return "CLI_CONFIGURATION_ERROR"
    if any(marker in text for marker in ("unauthorized", "forbidden", "401", "403", "api key", "access key", "invalid token")):
        return "MODEL_AUTH_ERROR"
    if "404" in text and any(
        marker in text
        for marker in ("cannot post", "not found", "/responses", "/chat/completions")
    ):
        return "MODEL_ENDPOINT_CONFIGURATION_ERROR"
    if any(marker in text for marker in ("timeout", "timed out", "deadline exceeded")):
        return "MODEL_TIMEOUT"
    if any(marker in text for marker in ("connection", "connect", "network", "dns", "refused", "reset by peer", "502", "503", "504")):
        return "MODEL_NETWORK_ERROR"
    if any(marker in text for marker in ("rate limit", "429", "model", "provider")):
        return "MODEL_PROVIDER_ERROR"
    return "AGENT_EXECUTOR_ERROR"


def _diagnostic_suffix(
    stdout: Any,
    stderr: Any,
    sensitive_values: Sequence[str] = (),
) -> str:
    excerpt = _diagnostic_excerpt(
        _output_text(stderr) or _output_text(stdout),
        sensitive_values,
    ).strip()
    return f": {excerpt}" if excerpt else ""
