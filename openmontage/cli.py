"""Command-line interface for agent and container integrations."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
from datetime import timedelta
from typing import Any

from openmontage.reference_clone import (
    ReferenceCloneError,
    ReferenceCloneService,
    capability_summary,
)

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_OPENMONTAGE_HANDLER_MARK = "_openmontage_logging"


class _StderrHandler(logging.StreamHandler):
    """StreamHandler that resolves ``sys.stderr`` at emit time.

    pytest's ``capsys`` swaps ``sys.stderr`` per test, but a handler caches the
    stream it was constructed with. Re-resolving on every emit keeps captured
    logs inside the active test (and out of the terminal) without leaking.
    """

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        self.stream = sys.stderr
        super().emit(record)


class _StructuredFormatter(logging.Formatter):
    """Append non-reserved ``extra=`` fields as ``key=value`` after the message.

    Lets the delegation proxy's replay records (``replay_key_source``,
    ``invocation_id``, ...) surface as structured fields on one line instead of
    being dropped by a formatter that only knows ``%(message)s``.
    """

    _RESERVED = frozenset(
        {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "taskName", "message", "asctime",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = [
            f"{key}={record.__dict__[key]!r}"
            for key in sorted(record.__dict__)
            if key not in self._RESERVED and not key.startswith("_")
        ]
        return f"{base} {' '.join(extras)}" if extras else base


def _configure_logging(level_name: str) -> None:
    """Attach an OpenMontage stderr handler so INFO records actually emit.

    Without this the root logger's default effective level is WARNING, so the
    delegation proxy's wrong-merge replay records (logged at INFO) stay silent
    under the Worker/CLI default configuration. Idempotent: repeated CLI entry
    (e.g. in tests) reuses the tagged handler instead of stacking new ones.
    """
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    root = logging.getLogger()
    handler = next(
        (h for h in root.handlers if getattr(h, _OPENMONTAGE_HANDLER_MARK, False)),
        None,
    )
    if handler is None:
        handler = _StderrHandler()
        handler.setFormatter(
            _StructuredFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        setattr(handler, _OPENMONTAGE_HANDLER_MARK, True)
        root.addHandler(handler)
    handler.setLevel(level)
    root.setLevel(level)


def _print(value: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if "project_id" in value:
        print(f"Project: {value['project_id']}")
        print(f"Pipeline: {value.get('pipeline_type', 'unknown')}")
        print(f"Next stage: {value.get('next_stage') or 'complete'}")
        print(f"Workspace: {value.get('project_dir', '')}")
        if value.get("request_path"):
            print(f"Clone request: {value['request_path']}")
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openmontage",
        description="Prepare and operate agent-led OpenMontage video productions.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("OPENMONTAGE_LOG_LEVEL", "INFO").upper(),
        choices=_LOG_LEVELS,
        help="Logging verbosity for the OpenMontage process (default: INFO, or "
        "$OPENMONTAGE_LOG_LEVEL). INFO is required for the delegation replay "
        "audit records to be observable.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    clone = sub.add_parser(
        "clone",
        help="Download and analyze a reference URL, then prepare a clone project.",
    )
    clone.add_argument("source", help="Video URL or pasted share text (including Douyin)")
    clone.add_argument("--project-id", default="")
    clone.add_argument("--pipeline", default="auto")
    clone.add_argument("--title", default="")
    clone.add_argument("--brief", default="", help="How the new video should differ")
    clone.add_argument(
        "--depth", choices=["transcript_only", "standard", "deep"], default="standard"
    )
    clone.add_argument("--max-keyframes", type=int, default=20)
    clone.add_argument(
        "--max-resolution", choices=["360p", "480p", "720p", "1080p"], default="720p"
    )
    clone.add_argument("--cookies", default="", help="Netscape cookies.txt path")
    clone.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="Show the next pipeline stage for a clone project.")
    status.add_argument("project_id")
    status.add_argument("--json", action="store_true")

    capabilities = sub.add_parser("capabilities", help="Show the compact provider preflight.")
    capabilities.add_argument("--json", action="store_true")

    mcp = sub.add_parser("mcp", help="Run the OpenMontage MCP server.")
    mcp.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio"
    )
    mcp.add_argument("--host", default=os.environ.get("OPENMONTAGE_MCP_HOST", "127.0.0.1"))
    mcp.add_argument(
        "--port", type=int, default=int(os.environ.get("OPENMONTAGE_MCP_PORT", "8765"))
    )

    events = sub.add_parser("events", help="Operate the durable AgentSpace event outbox.")
    event_commands = events.add_subparsers(dest="events_command", required=True)
    publish = event_commands.add_parser("publish", help="Publish pending signed Job events.")
    publish.add_argument("--once", action="store_true", help="Flush once and exit.")
    publish.add_argument("--interval", type=float, default=2.0)
    publish.add_argument("--limit", type=int, default=100)
    publish.add_argument("--json", action="store_true")

    worker = sub.add_parser("worker", help="Run the durable video Job Worker.")
    worker_commands = worker.add_subparsers(dest="worker_command", required=True)
    run = worker_commands.add_parser("run", help="Claim and execute queued video Jobs.")
    run.add_argument("--once", action="store_true", help="Process at most one lease and exit.")
    run.add_argument("--interval", type=float, default=2.0, help="Idle poll interval in seconds.")
    run.add_argument("--lease-seconds", type=float, default=120.0)
    run.add_argument("--heartbeat-seconds", type=float, default=30.0)
    run.add_argument("--retry-seconds", type=float, default=15.0)
    run.add_argument("--max-attempts", type=int, default=3)
    run.add_argument("--json", action="store_true")
    return parser


def _build_job_worker(args: argparse.Namespace):
    from lib.paths import PROJECTS_DIR
    from openmontage.artifact_bridge import ArtifactBridgeClient
    from openmontage.job_api import default_job_service
    from openmontage.job_worker import JobWorker
    from openmontage.model_credential_bridge import ModelCredentialBridgeClient
    from openmontage.invocation_store import ModelInvocationStore
    from openmontage.pipeline_executor import AgentCommandPipelineExecutor

    worker_id = os.environ.get("OPENMONTAGE_WORKER_ID", "").strip()
    if not worker_id:
        worker_id = f"{socket.gethostname()}-{os.getpid()}"
    service = default_job_service()
    return JobWorker(
        service,
        AgentCommandPipelineExecutor.from_environment(
            invocation_store=ModelInvocationStore(service.database_path),
        ),
        projects_dir=PROJECTS_DIR,
        worker_id=worker_id,
        lease_duration=timedelta(seconds=args.lease_seconds),
        heartbeat_interval=timedelta(seconds=args.heartbeat_seconds),
        retry_delay=timedelta(seconds=args.retry_seconds),
        max_executor_attempts=args.max_attempts,
        artifact_bridge=ArtifactBridgeClient.from_environment(),
        model_credential_bridge=ModelCredentialBridgeClient.from_environment(),
    )


def _worker_document(result: Any | None) -> dict[str, Any]:
    if result is None:
        return {"outcome": "idle"}
    return {
        "jobId": result.job_id,
        "stage": result.stage,
        "outcome": result.outcome,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.log_level)
    try:
        if args.command == "clone":
            value = ReferenceCloneService().prepare(
                args.source,
                project_id=args.project_id,
                pipeline_type=args.pipeline,
                title=args.title,
                creative_brief=args.brief,
                analysis_depth=args.depth,
                max_keyframes=args.max_keyframes,
                max_resolution=args.max_resolution,
                cookie_file=args.cookies,
            )
            _print(value, as_json=args.json)
            return 0
        if args.command == "status":
            _print(ReferenceCloneService().status(args.project_id), as_json=args.json)
            return 0
        if args.command == "capabilities":
            _print(capability_summary(), as_json=args.json)
            return 0
        if args.command == "mcp":
            from openmontage.mcp_server import run_server

            run_server(args.transport, host=args.host, port=args.port)
            return 0
        if args.command == "events" and args.events_command == "publish":
            from openmontage.event_outbox import OutboxPublisher

            if args.interval <= 0:
                raise ValueError("--interval must be greater than zero")
            if args.limit <= 0:
                raise ValueError("--limit must be greater than zero")
            publisher = OutboxPublisher.from_environment()
            while True:
                result = publisher.publish_pending(limit=args.limit)
                _print(
                    {
                        "delivered": result.delivered,
                        "failed": result.failed,
                        "deadLettered": result.dead_lettered,
                    },
                    as_json=args.json,
                )
                if args.once:
                    return 1 if result.failed or result.dead_lettered else 0
                time.sleep(args.interval)
        if args.command == "worker" and args.worker_command == "run":
            if args.interval <= 0:
                raise ValueError("--interval must be greater than zero")
            if args.lease_seconds <= 0:
                raise ValueError("--lease-seconds must be greater than zero")
            if args.heartbeat_seconds <= 0:
                raise ValueError("--heartbeat-seconds must be greater than zero")
            if args.retry_seconds < 0:
                raise ValueError("--retry-seconds must not be negative")
            if args.max_attempts < 1:
                raise ValueError("--max-attempts must be greater than zero")
            worker = _build_job_worker(args)
            while True:
                result = worker.run_once()
                if result is not None or args.once:
                    _print(_worker_document(result), as_json=args.json)
                if args.once:
                    return 0
                if result is None:
                    time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except (ReferenceCloneError, ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 1
