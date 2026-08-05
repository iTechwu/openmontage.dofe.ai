"""Command-line interface for agent and container integrations."""

from __future__ import annotations

import argparse
import json
import os
import sys
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
    except (ReferenceCloneError, ValueError, OSError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 1
