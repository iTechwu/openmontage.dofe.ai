from __future__ import annotations

import json
from pathlib import Path

from lib.pipeline_loader import list_pipelines
from tools.base_tool import ToolResult

from openmontage import reference_clone
from openmontage.capabilities import job_submission_capability
from openmontage.contracts import JobCreateRequest


def test_prepare_creates_agent_ready_airouter_project(monkeypatch, tmp_path):
    monkeypatch.setattr(
        reference_clone,
        "normalize_video_url",
        lambda _value: "https://www.douyin.com/video/7667931266800454975",
    )

    def fake_execute(_self, inputs):
        output = Path(inputs["output_dir"])
        output.mkdir(parents=True, exist_ok=True)
        brief = {
            "version": "1.0",
            "source": {
                "type": "douyin",
                "duration_seconds": 5,
                "title": "Reference title",
            },
            "content_analysis": {"summary": "", "topics": [], "target_audience": "general"},
            "structure_analysis": {"total_scenes": 1, "scenes": [], "pacing_profile": {}},
            "replication_guidance": {"suggested_pipeline": "animation"},
            "_analysis_meta": {
                "steps_completed": ["metadata", "download", "keyframes"],
                "steps_failed": [],
                "keyframe_count": 1,
                "scene_count": 1,
            },
        }
        (output / "video_analysis_brief.json").write_text(json.dumps(brief))
        return ToolResult(success=True, data=brief)

    monkeypatch.setattr(reference_clone.VideoAnalyzer, "execute", fake_execute)
    monkeypatch.setattr(reference_clone.registry, "discover", lambda: [])
    monkeypatch.setattr(
        reference_clone.DofeClient,
        "list_models",
        lambda _self: [
            {"id": "seedream-5.0"},
            {"id": "seedance-2.0-fast"},
        ],
    )
    monkeypatch.setattr(
        reference_clone.registry,
        "provider_menu_summary",
        lambda: {"composition_runtimes": {}, "capabilities": [], "setup_offers": [], "runtime_warnings": []},
    )

    result = reference_clone.ReferenceCloneService(projects_root=tmp_path).prepare(
        "https://www.douyin.com/video/7667931266800454975",
        creative_brief="Make it original",
    )
    assert result["project_id"] == "clone-douyin-7667931266800454975"
    assert result["pipeline_type"] == "animation"
    assert result["model_routing"] == {
        "policy": "dofe_airouter_only",
        "provider": "dofe",
        "base_url": "https://model.local.dofe.ai/api",
        "direct_provider_fallback": False,
    }
    assert result["preflight"]["airouter"]["status"] == "blocked"
    assert result["preflight"]["airouter"]["missing_required_models"] == ["openspeech-auc"]
    assert result["preflight"]["job_submission"]["workflow_field_is_pipeline"] is True
    assert any("preflight.job_submission" in item for item in result["agent_instructions"])
    assert Path(result["analysis"]["brief_path"]).is_file()
    assert Path(result["request_path"]).is_file()
    assert result["next_stage"] == "research"


def test_prepare_fails_when_download_did_not_complete(monkeypatch, tmp_path):
    monkeypatch.setattr(reference_clone, "normalize_video_url", lambda value: value)

    def fake_execute(_self, _inputs):
        return ToolResult(
            success=True,
            data={
                "_analysis_meta": {
                    "steps_completed": ["metadata"],
                    "steps_failed": ["download: cookies needed"],
                }
            },
        )

    monkeypatch.setattr(reference_clone.VideoAnalyzer, "execute", fake_execute)
    service = reference_clone.ReferenceCloneService(projects_root=tmp_path)
    try:
        service.prepare("https://www.douyin.com/video/7667931266800454975")
    except reference_clone.ReferenceCloneError as exc:
        assert "cookies needed" in str(exc)
    else:
        raise AssertionError("Expected preparation to fail without a downloaded reference")


def test_capabilities_include_replayable_job_submission_contract(monkeypatch):
    monkeypatch.setattr(
        reference_clone.registry,
        "provider_menu_summary",
        lambda: {"composition_runtimes": {}, "capabilities": []},
    )
    summary = reference_clone.capability_summary()
    contract = summary["job_submission"]
    assert contract["workflow_field_is_pipeline"] is True
    assert "compose is a stage" in contract["workflow_stage_warning"]
    assert contract["supported_workflows"] == sorted(list_pipelines())
    assert set(contract["required_fields"]) == {
        "clientRequestId",
        "workflow",
        "input",
        "brief",
        "output",
        "budget",
    }
    assert contract["request_schema"] == JobCreateRequest.model_json_schema(by_alias=True)
    assert JobCreateRequest.model_validate(contract["request_example"]).workflow == "animated-explainer"


def test_job_submission_preflight_excludes_invalid_workflow(monkeypatch):
    import jsonschema

    import openmontage.capabilities as capabilities
    import openmontage.contracts as contracts

    monkeypatch.setattr(capabilities, "list_pipelines", lambda: ["framework-smoke", "broken"])
    monkeypatch.setattr(contracts, "list_pipelines", lambda: ["framework-smoke", "broken"])
    load_valid_manifest = contracts.load_pipeline_readonly

    def load(workflow: str):
        if workflow == "broken":
            raise jsonschema.ValidationError("internal path")
        return load_valid_manifest(workflow)

    monkeypatch.setattr("openmontage.contracts.load_pipeline_readonly", load)

    contract = job_submission_capability()

    assert contract["supported_workflows"] == ["framework-smoke"]
    assert contract["unavailable_workflows"] == [
        {
            "workflow": "broken",
            "reason": "Workflow 'broken' is unavailable because its manifest is invalid",
        }
    ]
    assert contract["request_example"]["workflow"] == "framework-smoke"


def test_job_submission_preflight_omits_example_when_no_workflow_is_available(monkeypatch):
    import jsonschema

    import openmontage.capabilities as capabilities
    import openmontage.contracts as contracts

    monkeypatch.setattr(capabilities, "list_pipelines", lambda: ["broken"])
    monkeypatch.setattr(contracts, "list_pipelines", lambda: ["broken"])
    monkeypatch.setattr(
        contracts,
        "load_pipeline_readonly",
        lambda _workflow: (_ for _ in ()).throw(jsonschema.ValidationError("internal path")),
    )

    contract = job_submission_capability()

    assert contract["supported_workflows"] == []
    assert contract["request_example"] is None


def test_job_submission_preflight_excludes_workflow_with_duplicate_stages(monkeypatch):
    import openmontage.capabilities as capabilities
    import openmontage.contracts as contracts

    monkeypatch.setattr(capabilities, "list_pipelines", lambda: ["duplicate-stages"])
    monkeypatch.setattr(contracts, "list_pipelines", lambda: ["duplicate-stages"])
    monkeypatch.setattr(
        contracts,
        "load_pipeline_readonly",
        lambda _workflow: {
            "name": "duplicate-stages",
            "version": "1",
            "stages": [{"name": "compose"}, {"name": "compose"}],
        },
    )

    contract = job_submission_capability()

    assert contract["supported_workflows"] == []
    assert contract["unavailable_workflows"][0]["workflow"] == "duplicate-stages"
