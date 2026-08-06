"""Agent-facing OpenMontage runtime capability descriptions."""

from __future__ import annotations

from typing import Any

from lib.pipeline_loader import list_pipelines

from openmontage.contracts import (
    JobCreateRequest,
    WorkflowConfigurationError,
    WorkflowDefinition,
)


def job_submission_capability() -> dict[str, Any]:
    """Return the submission contract and only workflows that pass manifest preflight."""
    supported_workflows: list[str] = []
    unavailable_workflows: list[dict[str, str]] = []
    for workflow in sorted(list_pipelines()):
        try:
            WorkflowDefinition.from_pipeline(workflow)
        except (ValueError, WorkflowConfigurationError) as exc:
            unavailable_workflows.append({"workflow": workflow, "reason": str(exc)})
        else:
            supported_workflows.append(workflow)

    request_schema = JobCreateRequest.model_json_schema(by_alias=True)
    return {
        "workflow_field_is_pipeline": True,
        "workflow_stage_warning": "compose is a stage, not a workflow; use a pipeline name",
        "supported_workflows": supported_workflows,
        "unavailable_workflows": unavailable_workflows,
        "request_schema": request_schema,
        "request_example": {
            "schemaVersion": 1,
            "clientRequestId": "<unique-per-business-attempt>",
            "workflow": "animated-explainer",
            "input": {"type": "text", "inlineText": "Create a concise product video"},
            "brief": {"title": "Product video", "durationSeconds": 12},
            "output": {"container": "mp4", "resolution": "1280x720", "fps": 30},
            "budget": {"maxAmount": "1.00", "currency": "CNY"},
        },
        "required_fields": request_schema.get("required", []),
        "idempotency": "Reuse clientRequestId when retrying the same business submission; change it for a new Job.",
    }
