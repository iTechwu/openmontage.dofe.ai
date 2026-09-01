"""客户端 Agent 使用的统一 MCP Gateway adapter。"""

from __future__ import annotations

from typing import Any, Callable


class McpGatewayAdapter:
    """把一个 MCP call_tool callable 适配成 Stage handler 的统一接口。"""

    def __init__(self, call_tool: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_tool = call_tool

    def call_tool(
        self,
        tool_name: str,
        inputs: dict[str, Any],
        *,
        job_id: str,
        stage: str,
        stage_attempt: int,
        lease_token: str,
        idempotency_key: str,
        operation: str = "generate",
    ) -> Any:
        return self._call_tool(
            "invoke_openmontage_tool",
            {
                "tool_name": tool_name,
                "operation": operation,
                "inputs": inputs,
                "job_id": job_id,
                "stage": stage,
                "stage_attempt": stage_attempt,
                "lease_token": lease_token,
                "idempotency_key": idempotency_key,
            },
        )

