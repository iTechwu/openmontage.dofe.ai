"""Command-line interface for agent and container integrations."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from openmontage.reference_clone import (
    ReferenceCloneError,
    ReferenceCloneService,
    capability_summary,
)


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
                    {"delivered": result.delivered, "failed": result.failed},
                    as_json=args.json,
                )
                if args.once:
                    return 1 if result.failed else 0
                time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except (ReferenceCloneError, ValueError, OSError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 1
