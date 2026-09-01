"""安全的统一 CI 工具执行入口。

客户端只传逻辑工具名；具体 provider 和 ToolRegistry 始终留在 CI 进程内。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openmontage.job_service import JobService
from lib.pipeline_loader import load_pipeline_readonly
from tools.base_tool import ToolResult
from tools.tool_registry import registry


TOOL_ALIASES = {
    "music_gen": "dofe_music",
    "avatar_video": "dofe_avatar",
    "transcriber": "dofe_stt",
}

ALLOWED_TOOLS = frozenset({
    "image_selector", "video_selector", "tts_selector", "music_gen",
    "avatar_video", "transcriber", "math_animate", "diagram_gen",
    "code_snippet", "video_compose", "audio_mixer", "video_stitch",
    "hyperframes_compose", "composition_validator", "audio_probe", "export_bundle",
})

_PATH_KEYS = frozenset({"path", "file", "input_path", "output_path", "output_dir", "export_dir"})
_PATH_SUFFIXES = ("_path", "_paths", "_file", "_files", "_dir", "_dirs")
_OUTPUT_REQUIRED = frozenset({
    "image_selector", "video_selector", "tts_selector", "music_gen",
    "avatar_video", "transcriber", "math_animate", "diagram_gen",
    "code_snippet", "video_compose", "audio_mixer", "video_stitch",
    "hyperframes_compose", "export_bundle",
})


class ToolGatewayError(ValueError):
    def __init__(self, code: str, message: str, *, category: str = "tool_gateway") -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.message = message


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_relative(value: str) -> str:
    if "\x00" in value or value.startswith("/") or (len(value) > 1 and value[1] == ":"):
        raise ToolGatewayError("PATH_OUTSIDE_REPOSITORY", "tool paths must be project-relative")
    path = Path(value)
    if ".." in path.parts:
        raise ToolGatewayError("PATH_OUTSIDE_REPOSITORY", "tool paths must not contain '..'")
    return str(path)


def _rewrite_paths(value: Any, project_dir: Path, *, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _rewrite_paths(v, project_dir, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite_paths(v, project_dir, key=key) for v in value]
    if not isinstance(value, str):
        return value
    is_path = key.lower() in _PATH_KEYS or key.lower().endswith(_PATH_SUFFIXES)
    if not is_path or "://" in value:
        return value
    relative = _safe_relative(value)
    target = (project_dir / relative).resolve()
    if target != project_dir and project_dir not in target.parents:
        raise ToolGatewayError("PATH_OUTSIDE_REPOSITORY", "tool path escapes the Job project")
    return str(target)


def _relative_path(value: str, project_dir: Path) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        candidate = Path(value).resolve()
        return candidate.relative_to(project_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _artifact(value: str, project_dir: Path) -> dict[str, Any] | None:
    relative = _relative_path(value, project_dir)
    if relative is None:
        return None
    path = project_dir / relative
    if not path.is_file():
        return {"path": relative}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "asset_id": "asset_" + digest[:20],
        "path": relative,
        "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "size_bytes": path.stat().st_size,
        "sha256": digest,
    }


def _sanitize_data(value: Any, project_dir: Path) -> Any:
    if isinstance(value, dict):
        return {k: _sanitize_data(v, project_dir) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_data(v, project_dir) for v in value]
    if isinstance(value, str):
        return _relative_path(value, project_dir) or value
    return value


class ToolGateway:
    """Dispatch the fixed logical tool surface inside a Job workspace."""

    def __init__(self, service: JobService) -> None:
        self.service = service
        with self.service._connect() as db:  # shared durable Job database
            db.execute(
                """CREATE TABLE IF NOT EXISTS openmontage_tool_invocation (
                    job_id TEXT NOT NULL, stage TEXT NOT NULL, stage_attempt INTEGER NOT NULL,
                    tool_name TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                    status TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, stage, stage_attempt, tool_name, idempotency_key)
                )"""
            )

    def catalog(self) -> dict[str, Any]:
        registry.ensure_discovered()
        entries = []
        for name in sorted(ALLOWED_TOOLS):
            internal = TOOL_ALIASES.get(name, name)
            tool = registry.get(internal)
            if tool is None:
                entries.append({"name": name, "status": "unavailable"})
                continue
            info = tool.get_info()
            entries.append({
                "name": name,
                "status": info.get("status", "unavailable"),
                "capability": info.get("capability"),
                "input_schema": info.get("input_schema", {}),
                "required_agent_skills": info.get("agent_skills", []),
            })
        return {"tools": entries}

    def invoke(
        self,
        *,
        tool_name: str,
        operation: str,
        inputs: dict[str, Any],
        job_id: str = "",
        stage: str = "",
        stage_attempt: int | None = None,
        lease_token: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        if operation == "catalog":
            return self.catalog()
        if tool_name not in ALLOWED_TOOLS:
            raise ToolGatewayError("TOOL_NOT_ALLOWED", f"tool {tool_name!r} is not exposed")
        if operation not in {"rank", "preflight", "generate", "progress"}:
            raise ToolGatewayError("TOOL_INPUT_INVALID", f"unsupported operation {operation!r}")
        if not isinstance(inputs, dict):
            raise ToolGatewayError("TOOL_INPUT_INVALID", "inputs must be an object")
        if not job_id or not stage or stage_attempt is None or not lease_token.strip():
            raise ToolGatewayError("STAGE_LEASE_INVALID", "job, stage, attempt and lease are required")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 256:
            raise ToolGatewayError("IDEMPOTENCY_CONFLICT", "idempotency_key must be non-empty and <=256 characters")

        snapshot = self.service.get_job(job_id)
        try:
            manifest = load_pipeline_readonly(snapshot.workflow.name)
            stage_def = next(s for s in manifest["stages"] if s["name"] == stage)
        except (KeyError, StopIteration, FileNotFoundError) as exc:
            raise ToolGatewayError("STAGE_STATE_INVALID", f"unknown stage {stage!r}") from exc
        declared = set(stage_def.get("tools_available", []))
        if tool_name not in declared and not (tool_name == "avatar_video" and {"talking_head", "lip_sync"} & declared):
            raise ToolGatewayError("TOOL_NOT_ALLOWED", f"{tool_name!r} is not allowed in stage {stage!r}")

        now = datetime.now(timezone.utc)
        with self.service._connect() as db:
            self.service._require_client_lease(db, job_id, stage, lease_token, int(stage_attempt), now, fencing=(operation != "progress"))
        project_dir = (self.service.projects_dir / job_id).resolve()
        project_dir.mkdir(parents=True, exist_ok=True)
        internal = TOOL_ALIASES.get(tool_name, tool_name)
        tool = registry.get(internal)
        if tool is None:
            raise ToolGatewayError("TOOL_UNAVAILABLE", f"tool {tool_name!r} is unavailable")

        effective_inputs = dict(inputs)
        if operation in {"rank", "preflight", "generate"}:
            # Only inject the lifecycle operation when the tool's own
            # ``operation`` field accepts that value. Some tools use
            # ``operation`` for a different semantic — video_selector
            # (text_to_video / image_to_video / reference_to_video),
            # video_compose (compose / render / …), hyperframes_compose
            # (render / lint / validate / …) — none of which accept
            # "generate". When the tool declares no enum (or accepts the
            # value), inject so the selectors still receive rank/preflight/
            # generate as before.
            op_enum = (
                getattr(tool, "input_schema", {})
                .get("properties", {})
                .get("operation", {})
                .get("enum")
            )
            if op_enum is None or operation in op_enum:
                effective_inputs.setdefault("operation", operation)
        if operation == "generate" and tool_name in _OUTPUT_REQUIRED and not any(
            key in effective_inputs for key in ("output_path", "output_dir", "export_dir")
        ):
            raise ToolGatewayError(
                "TOOL_INPUT_INVALID",
                f"{tool_name} generation requires a project-relative output_path",
            )
        effective_inputs = _rewrite_paths(effective_inputs, project_dir)
        request_hash = hashlib.sha256(_canonical({"operation": operation, "inputs": effective_inputs}).encode()).hexdigest()
        key_args = (job_id, stage, int(stage_attempt), tool_name, idempotency_key)
        with self.service._connect() as db:
            self.service._begin_write(db)
            row = db.execute(
                "SELECT request_hash, status, result_json FROM openmontage_tool_invocation WHERE job_id=? AND stage=? AND stage_attempt=? AND tool_name=? AND idempotency_key=?",
                key_args,
            ).fetchone()
            if row is not None:
                request_hash_value = row["request_hash"] if hasattr(row, "keys") else row[0]
                status_value = row["status"] if hasattr(row, "keys") else row[1]
                result_json = row["result_json"] if hasattr(row, "keys") else row[2]
                if request_hash_value != request_hash:
                    raise ToolGatewayError("IDEMPOTENCY_CONFLICT", "idempotency key was reused with different inputs")
                if status_value == "completed" and result_json:
                    return json.loads(result_json)
            timestamp = now.isoformat()
            db.execute(
                "INSERT OR REPLACE INTO openmontage_tool_invocation VALUES (?, ?, ?, ?, ?, ?, 'in_flight', NULL, ?, ?)",
                (*key_args, request_hash, timestamp, timestamp),
            )

        try:
            result = tool.execute(effective_inputs)
            if not isinstance(result, ToolResult):
                raise ToolGatewayError("TOOL_RESULT_INVALID", "tool did not return ToolResult")
            response = self._wire_result(tool_name, operation, result, project_dir)
        except ToolGatewayError:
            raise
        except Exception as exc:
            response = {"success": False, "status": "failed", "tool_name": tool_name, "operation": operation,
                        "error": {"code": "TOOL_EXECUTION_FAILED", "category": "tool", "message": str(exc)[:300]}}
        with self.service._connect() as db:
            db.execute("UPDATE openmontage_tool_invocation SET status='completed', result_json=?, updated_at=? WHERE job_id=? AND stage=? AND stage_attempt=? AND tool_name=? AND idempotency_key=?",
                       (_canonical(response), datetime.now(timezone.utc).isoformat(), *key_args))
        return response

    def _wire_result(self, tool_name: str, operation: str, result: ToolResult, project_dir: Path) -> dict[str, Any]:
        artifacts: list[dict[str, Any]] = []
        for raw in result.artifacts:
            item = _artifact(str(raw), project_dir)
            if item is not None:
                artifacts.append(item)
        data = _sanitize_data(result.data, project_dir)
        if isinstance(data, dict):
            for key in ("output", "output_path", "path", "file", "video_path", "audio_path"):
                value = data.get(key)
                if isinstance(value, str):
                    item = _artifact(value, project_dir)
                    if item is not None and item not in artifacts:
                        artifacts.append(item)
        response: dict[str, Any] = {
            "success": bool(result.success), "status": "completed" if result.success else "failed",
            "tool_name": tool_name, "operation": operation, "data": data, "artifacts": artifacts,
        }
        if result.error:
            response["error"] = {"code": "TOOL_EXECUTION_FAILED", "category": "tool", "message": str(result.error)[:500]}
        if result.model:
            response["model"] = result.model
        if result.cost_currency or result.cost_amount is not None or result.cost_usd:
            response["cost"] = {"amount": result.cost_amount if result.cost_amount is not None else result.cost_usd,
                                "currency": result.cost_currency or "USD", "source": result.cost_source or "tool"}
        return response
