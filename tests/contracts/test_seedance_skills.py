from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from lib.pipeline_loader import load_pipeline
from tools.video.dofe_video import DofeVideo
from tools.video.higgsfield_video import HiggsFieldVideo
from tools.video.runway_video import RunwayVideo
from tools.video.seedance_replicate import SeedanceReplicate
from tools.video.seedance_video import SeedanceVideo
from tools.video.video_selector import VideoSelector
from tools.base_tool import ToolResult


ROOT = Path(__file__).resolve().parents[2]
SKILL_NAMES = (
    "seedance-provider",
    "seedance-directing",
    "seedance-continuity",
    "seedance-prompting",
    "seedance-quality",
)

SEEDANCE_PIPELINE_DIRECTORS = (
    "skills/pipelines/cinematic/scene-director.md",
    "skills/pipelines/cinematic/asset-director.md",
    "skills/pipelines/animation/scene-director.md",
    "skills/pipelines/animation/asset-director.md",
    "skills/pipelines/explainer/scene-director.md",
    "skills/pipelines/explainer/asset-director.md",
    "skills/pipelines/hybrid/scene-director.md",
    "skills/pipelines/hybrid/asset-director.md",
    "skills/pipelines/avatar-spokesperson/scene-director.md",
    "skills/pipelines/avatar-spokesperson/asset-director.md",
)

SEEDANCE_PIPELINES = (
    "cinematic",
    "animation",
    "animated-explainer",
    "hybrid",
    "avatar-spokesperson",
)


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_seedance_skills_use_functional_names_and_codex_metadata(skill_name: str):
    skill_dir = ROOT / ".agents" / "skills" / skill_name
    skill = (skill_dir / "SKILL.md").read_text()
    metadata = (skill_dir / "agents" / "openai.yaml").read_text()

    assert f"name: {skill_name}\n" in skill
    assert "TODO" not in skill
    assert f"${skill_name}" in metadata


def test_seedance_derivatives_retain_upstream_mit_license():
    license_text = (
        ROOT / ".agents" / "skills" / "seedance-provider" / "LICENSE"
    ).read_text()
    assert "MIT License" in license_text
    assert "Iamemily2050" in license_text


def test_reviewer_owns_seedance_cross_asset_graph_semantics():
    reviewer = (ROOT / "skills" / "meta" / "reviewer.md").read_text()
    lineage = (ROOT / "skills" / "meta" / "seedance-lineage-review.md").read_text()

    assert "seedance-lineage-review.md" in reviewer
    assert "asset_manifest.lineage_review" in lineage
    for required_check in (
        "parent_exists",
        "parent_precedes_child",
        "acyclic",
        "accepted_parent_authority",
        "observed_state_handoff",
        "extension_depth_and_reanchor",
        "beat_and_identity_continuity",
        "reference_binding_matches_preflight",
    ):
        assert f"`{required_check}`" in lineage
    assert "not a Python rule" in lineage


@pytest.mark.parametrize("director_path", SEEDANCE_PIPELINE_DIRECTORS)
def test_video_pipelines_route_seedance_through_shared_production_contract(director_path: str):
    content = (ROOT / director_path).read_text()
    assert "skills/creative/seedance-production.md" in content


@pytest.mark.parametrize("pipeline_name", SEEDANCE_PIPELINES)
def test_video_pipeline_manifests_enforce_seedance_stage_facts(pipeline_name: str):
    manifest = load_pipeline(pipeline_name)
    stages = {stage["name"]: stage for stage in manifest["stages"]}

    scene_text = " ".join(
        stages["scene_plan"]["review_focus"] + stages["scene_plan"]["success_criteria"]
    )
    asset_text = " ".join(
        stages["assets"]["review_focus"] + stages["assets"]["success_criteria"]
    )

    assert "Seedance" in scene_text
    assert "seedance_contract" in scene_text
    assert "Seedance" in asset_text
    assert "prompt_review" in asset_text
    assert "take_review" in asset_text
    assert "lineage_review" in asset_text


@pytest.mark.parametrize(
    "tool_class",
    [SeedanceVideo, SeedanceReplicate],
)
def test_seedance_provider_tools_load_full_production_skill_chain(tool_class):
    skills = tool_class().agent_skills
    assert skills[:5] == list(SKILL_NAMES)


@pytest.mark.parametrize("tool_class", [RunwayVideo, HiggsFieldVideo])
def test_multi_model_gateways_only_expose_seedance_skills_for_seedance(tool_class):
    tool = tool_class()
    assert tool.agent_skills_for({"model": "seedance_2.0"})[:5] == list(SKILL_NAMES)
    assert tool.agent_skills_for({"model": "kling_3.0"}) == ["ai-video-gen"]


def test_dofe_gateway_selects_skills_from_model_alias(monkeypatch):
    tool = DofeVideo()
    monkeypatch.setenv("DOFE_VIDEO_MODEL", "kling-3.0")
    assert tool.agent_skills_for({}) == ["ai-video-gen"]
    assert tool.agent_skills_for({"model_name": "seedance-2.0-fast"})[:5] == list(SKILL_NAMES)


def test_selector_exposes_input_aware_skills_in_execution_context():
    tool = RunwayVideo()
    native_inputs = VideoSelector._adapt_inputs_for_tool(tool, {"model_name": "gen4_turbo"})
    seedance_inputs = VideoSelector._adapt_inputs_for_tool(tool, {"model": "seedance_2.0"})
    native = VideoSelector._tool_context_payload(tool, native_inputs)
    seedance = VideoSelector._tool_context_payload(tool, seedance_inputs)

    assert native["required_agent_skills"] == ["ai-video-gen"]
    assert seedance["required_agent_skills"][:5] == list(SKILL_NAMES)
    assert tool.get_info()["agent_skills"][:5] == list(SKILL_NAMES)


def test_selector_adapts_shared_fields_to_runway_contract():
    adapted = VideoSelector._adapt_inputs_for_tool(
        RunwayVideo(),
        {
            "prompt": "A car enters frame.",
            "model_name": "gen4_turbo",
            "aspect_ratio": "9:16",
            "duration": "10",
            "reference_image_url": "https://example.com/car.png",
        },
    )

    assert adapted["model"] == "gen4_turbo"
    assert adapted["ratio"] == "9:16"
    assert adapted["duration"] == 10
    assert adapted["image_url"] == "https://example.com/car.png"


def test_selector_filters_providers_that_contradict_explicit_aspect_ratio():
    selector = VideoSelector()
    candidates = selector._filter_candidates(
        {"operation": "text_to_video", "aspect_ratio": "21:9"},
        [RunwayVideo(), HiggsFieldVideo()],
    )

    assert [tool.name for tool in candidates] == ["higgsfield_video"]


def test_selector_filters_providers_that_contradict_explicit_model():
    selector = VideoSelector()
    candidates = selector._filter_candidates(
        {"operation": "text_to_video", "model_name": "kling_3.0"},
        [RunwayVideo(), HiggsFieldVideo()],
    )

    assert [tool.name for tool in candidates] == ["higgsfield_video"]


def test_provider_preflight_distinguishes_declared_contract_from_live_verification(monkeypatch):
    monkeypatch.setenv("RUNWAY_API_KEY", "test-key")
    report = RunwayVideo().preflight(
        {
            "prompt": "A car enters frame.",
            "operation": "text_to_video",
            "model": "seedance_2.0",
            "duration": 5,
            "ratio": "16:9",
        },
        live=True,
    )

    assert report["status"] == "degraded"
    assert report["verification_level"] == "declared_tool_contract"
    assert report["live_probe"]["status"] == "not_supported"


def test_provider_preflight_blocks_undeclared_prompt_token_syntax(monkeypatch):
    monkeypatch.setenv("RUNWAY_API_KEY", "test-key")
    report = RunwayVideo().preflight(
        {
            "prompt": "Use the vehicle reference.",
            "operation": "image_to_video",
            "model": "seedance_2.0",
            "duration": 5,
            "ratio": "16:9",
            "image_url": "https://example.com/car.png",
            "reference_roles": [
                {
                    "tag": "vehicle-identity-reference",
                    "binding_mode": "prompt_token",
                    "role": "identity",
                }
            ],
        }
    )

    assert report["status"] == "blocked"
    assert any("prompt-token syntax" in error["message"] for error in report["errors"])


def test_runtime_provider_preflight_matches_asset_contract(monkeypatch):
    monkeypatch.setenv("RUNWAY_API_KEY", "test-key")
    report = RunwayVideo().preflight(
        {
            "prompt": "A car enters frame.",
            "operation": "text_to_video",
            "model": "seedance_2.0",
            "duration": 5,
            "ratio": "16:9",
            "execution_scope": "sample",
        },
        live=False,
    )
    asset_schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "asset_manifest.schema.json").read_text()
    )

    Draft202012Validator(asset_schema["$defs"]["providerPreflight"]).validate(report)


def test_selector_preflight_resolves_provider_without_generation(monkeypatch):
    monkeypatch.setenv("RUNWAY_API_KEY", "test-key")
    provider = RunwayVideo()
    generated = False

    def fail_if_generated(_inputs):
        nonlocal generated
        generated = True
        raise AssertionError("preflight must not generate")

    provider.execute = fail_if_generated
    selector = VideoSelector()
    monkeypatch.setattr(selector, "_providers", lambda: [provider])

    result = selector.execute(
        {
            "prompt": "A car enters frame.",
            "operation": "preflight",
            "target_operation": "image_to_video",
            "model": "seedance_2.0",
            "duration": "5",
            "aspect_ratio": "16:9",
            "reference_image_url": "https://example.com/car.png",
            "reference_roles": [
                {
                    "tag": "vehicle-identity-reference",
                    "binding_mode": "input_parameter",
                    "role": "identity",
                }
            ],
            "live_preflight": False,
        }
    )

    assert result.success
    assert result.data["status"] == "passed"
    assert result.data["selected_tool"] == "runway_video"
    assert not generated


def test_selector_blocks_unverified_batch_before_provider_execution(monkeypatch):
    monkeypatch.setenv("RUNWAY_API_KEY", "test-key")
    provider = RunwayVideo()
    generated = False

    def generate(_inputs):
        nonlocal generated
        generated = True
        return ToolResult(success=True)

    provider.execute = generate
    selector = VideoSelector()
    monkeypatch.setattr(selector, "_providers", lambda: [provider])

    result = selector.execute(
        {
            "prompt": "A car enters frame.",
            "operation": "text_to_video",
            "model": "seedance_2.0",
            "duration": "5",
            "aspect_ratio": "16:9",
            "execution_scope": "batch",
            "live_preflight": True,
        }
    )

    assert not result.success
    assert "batch generation" in result.error
    assert result.data["provider_preflight"]["status"] == "degraded"
    assert not generated


def test_replicate_live_preflight_validates_remote_input_schema(monkeypatch):
    monkeypatch.setenv("REPLICATE_API_TOKEN", "test-token")

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "latest_version": {
                    "id": "version-123",
                    "openapi_schema": {
                        "components": {
                            "schemas": {
                                "Input": {
                                    "type": "object",
                                    "required": ["prompt"],
                                    "properties": {
                                        "prompt": {"type": "string"},
                                        "duration": {"type": "integer", "enum": [5, 10]},
                                        "aspect_ratio": {"type": "string"},
                                        "resolution": {"type": "string"},
                                        "generate_audio": {"type": "boolean"},
                                    },
                                    "additionalProperties": False,
                                }
                            }
                        }
                    },
                }
            }

    import requests

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response())
    report = SeedanceReplicate().preflight(
        {
            "prompt": "A car enters frame.",
            "operation": "text_to_video",
            "model_variant": "standard",
            "duration": "5",
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "generate_audio": False,
        },
        live=True,
    )

    assert report["status"] == "passed"
    assert report["verification_level"] == "live_provider_contract"
    assert report["live_probe"]["provider_contract_version"] == "version-123"


def test_scene_plan_accepts_seedance_generation_contract():
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "scene_plan.schema.json").read_text()
    )
    artifact = {
        "version": "1.0",
        "identity_registry": [
            {
                "id": "vehicle-white-suv",
                "kind": "vehicle",
                "role": "apparent rescuer",
                "canonical_anchor": "compact SUV, white paint, split lamps",
                "immutable_traits": {
                    "silhouette": "compact two-box SUV",
                    "proportions": "short overhangs and tall cabin",
                    "palette": ["white paint", "black glass"],
                    "materials": ["painted metal", "clear lamp lenses"],
                    "signature_features": ["split lamps", "five-spoke wheels"],
                },
                "allowed_changes": ["wheel rotation", "suspension compression"],
                "reference_tags": ["vehicle-identity-reference"],
                "postproduction_zones": ["license plate"],
            }
        ],
        "scenes": [
            {
                "id": "clip-01",
                "type": "generated",
                "description": "Vehicle hero enters the tunnel.",
                "start_seconds": 0,
                "end_seconds": 6,
                "generation_contract": {
                    "model_family": "seedance",
                    "mode": "reference_to_video",
                    "shot_structure": "single_take",
                    "continuation_type": "sequence_first_clip",
                    "felt_intent": "The apparent rescuer becomes a threat.",
                    "planned_start_state": "Tunnel entrance is empty.",
                    "planned_end_state": "Vehicle blocks the exit, facing camera.",
                    "identity_anchors": ["compact SUV, white paint, split lamps"],
                    "identity_ids": ["vehicle-white-suv"],
                    "reference_roles": [
                        {
                            "tag": "vehicle-identity-reference",
                            "binding_mode": "input_parameter",
                            "role": "identity",
                            "transfers": ["body geometry", "paint", "lights"],
                            "must_not_transfer": ["background", "text"],
                        }
                    ],
                    "continuity_locks": ["vehicle identity"],
                    "allowed_changes": ["wheel rotation", "suspension"],
                    "completed_beats": [],
                    "reserved_beats": ["transformation"],
                    "prompt_budget": {
                        "primary_spend": "identity",
                        "secondary_spend": "motion",
                        "economized": ["background traffic", "readable plate text"],
                    },
                    "seedance_contract": {
                        "lane": "narrative",
                        "authoring_state": {
                            "dramatic_function": "reveal",
                            "turn": "rescuer to threat",
                            "pov": "the trapped vehicle",
                            "power_shift": "the arriving SUV takes control",
                            "objective": "block the tunnel exit",
                            "obstacle_and_tactic": "slick pavement; brake across both lanes",
                            "subtext": "the rescue posture conceals an ambush",
                            "suppressed_behavior": "headlamps hesitate before flaring",
                            "specific_detail": "right wheel clips one broken reflector",
                            "specific_detail_provenance": "authored_choice",
                            "specific_detail_source": None,
                            "stock_solution_refused": "no generic transformation reveal",
                            "value_before": "the arrival reads as rescue",
                            "value_after": "the arrival controls the only exit",
                        },
                        "primary_action": "SUV slides sideways and blocks the exit",
                        "shot_design": {
                            "framing": "low medium-wide, SUV enters frame left",
                            "lens": "35mm moderate wide-angle perspective",
                            "blocking": "SUV crosses the trapped vehicle and occupies both lanes",
                            "camera": "35mm lateral track, then locked endpoint",
                            "camera_axis": "stay on the trapped vehicle side of the lane axis",
                            "screen_direction": "SUV travels left-to-right",
                            "lighting": "cold tunnel practicals sweep across white paint",
                            "behavior": "suspension compresses, tires bite, lamps hesitate",
                        },
                        "temporal_beats": [
                            {
                                "beat_id": "arrival-block",
                                "order": 1,
                                "duration_hint_seconds": 6,
                                "action": "SUV slides across both lanes and brakes",
                                "camera_phase": "track laterally, then settle locked",
                                "sound_phase": "tire water rises, then engine drops to idle",
                                "completed_end_state": "SUV is square to camera and blocks the exit",
                            }
                        ],
                        "sound_intent": "tire water, engine load, distant alarm; no music",
                        "prompt_carriers": [
                            "headlamps hesitate before flaring",
                            "right wheel clips one broken reflector",
                        ],
                        "exclusions": ["no transformation", "no readable plate text"],
                        "continuity_state": {
                            "source_status": "canonical_reference",
                            "extension_depth": 0,
                            "reanchor_required": False,
                            "observation_confidence": "high",
                            "uncertainties": [],
                        },
                    },
                    "generate_audio": False,
                },
            }
        ],
    }

    Draft202012Validator(schema).validate(artifact)


def _provider_preflight_report() -> dict:
    return {
        "status": "passed",
        "verification_level": "declared_tool_contract",
        "tool": "dofe_video",
        "provider": "dofe",
        "tool_version": "0.1.0",
        "tool_status": "available",
        "operation": "reference_to_video",
        "execution_scope": "sample",
        "degraded_preflight_approved": False,
        "model_selection": {"field": "model_name", "value": "seedance-2.0-fast"},
        "input_schema_fingerprint": "0123456789abcdef",
        "declared_input_fields": ["model_name", "operation", "prompt"],
        "resolved_input_fields": ["model_name", "operation", "prompt"],
        "reference_binding": {
            "requested_modes": ["input_parameter"],
            "supported_modes": ["input_parameter"],
            "input_fields": ["reference_image_urls"],
            "prompt_token_syntax": None,
        },
        "live_probe": {
            "status": "not_requested",
            "verification_scope": [],
            "warnings": [],
            "errors": [],
        },
        "errors": [],
        "warnings": [],
        "would_execute": True,
    }


def _standalone_lineage_review(asset_id: str) -> dict:
    not_applicable = {
        "status": "not_applicable",
        "evidence": f"{asset_id} is a standalone root with no parent edge.",
    }
    return {
        "decision": "pass",
        "reviewed_asset_ids": [asset_id],
        "roots": [asset_id],
        "edges": [],
        "checks": {
            "unique_ids": {
                "status": "pass",
                "evidence": f"{asset_id} occurs exactly once in the current manifest.",
            },
            "parent_exists": dict(not_applicable),
            "parent_precedes_child": dict(not_applicable),
            "acyclic": {
                "status": "pass",
                "evidence": f"Walking {asset_id} reaches its root without revisiting an asset.",
            },
            "accepted_parent_authority": dict(not_applicable),
            "observed_state_handoff": dict(not_applicable),
            "extension_depth_and_reanchor": {
                "status": "pass",
                "evidence": f"{asset_id} is a root at extension depth zero.",
            },
            "beat_and_identity_continuity": {
                "status": "pass",
                "evidence": f"{asset_id} establishes canon and does not replay a prior beat.",
            },
            "reference_binding_matches_preflight": {
                "status": "pass",
                "evidence": f"{asset_id} uses the preflight-declared input_parameter binding.",
            },
        },
        "findings": [],
    }


def test_asset_manifest_accepts_prompt_and_take_reviews():
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "asset_manifest.schema.json").read_text()
    )
    artifact = {
        "version": "1.0",
        "assets": [
            {
                "id": "clip-01-take-01",
                "type": "video",
                "path": "assets/video/clip-01-take-01.mp4",
                "source_tool": "dofe_video",
                "scene_id": "clip-01",
                "model_family": "seedance",
                "provider": "dofe",
                "model": "seedance-2.0-fast",
                "prompt_review": {
                    "draft": "A cinematic SUV drives through a tunnel.",
                    "critique": ["Missing endpoint", "Identity role is ambiguous"],
                    "final": "The attached identity reference controls vehicle geometry only...",
                    "skills_applied": [
                        "seedance-provider",
                        "seedance-directing",
                        "seedance-continuity",
                        "seedance-prompting",
                        "seedance-quality",
                    ],
                    "continuity_checked": True,
                    "reference_roles_checked": True,
                    "provider_preflight": _provider_preflight_report(),
                },
                "take_review": {
                    "decision": "keep",
                    "issues": [],
                    "accepted_as_canon": True,
                    "canon_status": "accepted",
                    "observed_end_state": "SUV stops square to camera at the exit.",
                    "extension_depth": 0,
                    "observation_confidence": "high",
                    "uncertainties": [],
                    "next_action": "Compile clip-02 from the observed stop state.",
                },
            }
        ],
        "lineage_review": _standalone_lineage_review("clip-01-take-01"),
    }

    Draft202012Validator(schema).validate(artifact)


def test_seedance_asset_requires_provider_preflight_and_lineage_review():
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "asset_manifest.schema.json").read_text()
    )
    asset = {
        "id": "clip-01-take-01",
        "type": "video",
        "path": "assets/video/clip-01-take-01.mp4",
        "source_tool": "dofe_video",
        "scene_id": "clip-01",
        "model_family": "seedance",
        "provider": "dofe",
        "model": "seedance-2.0-fast",
        "prompt_review": {
            "draft": "draft",
            "final": "final",
            "skills_applied": list(SKILL_NAMES),
            "continuity_checked": True,
            "reference_roles_checked": True,
            "provider_preflight": _provider_preflight_report(),
        },
        "take_review": {
            "decision": "keep",
            "issues": [],
            "accepted_as_canon": True,
            "canon_status": "accepted",
            "observed_end_state": "SUV stops at the tunnel exit.",
            "extension_depth": 0,
            "observation_confidence": "high",
            "uncertainties": [],
            "next_action": "Review the next scene contract.",
        },
    }
    valid = {
        "version": "1.0",
        "assets": [asset],
        "lineage_review": _standalone_lineage_review(asset["id"]),
    }
    Draft202012Validator(schema).validate(valid)

    missing_preflight = deepcopy(valid)
    del missing_preflight["assets"][0]["prompt_review"]["provider_preflight"]
    assert any(
        "provider_preflight" in error.message
        for error in Draft202012Validator(schema).iter_errors(missing_preflight)
    )

    missing_lineage = deepcopy(valid)
    del missing_lineage["lineage_review"]
    assert any(
        "lineage_review" in error.message
        for error in Draft202012Validator(schema).iter_errors(missing_lineage)
    )

    inconsistent_pass = deepcopy(valid)
    inconsistent_pass["lineage_review"]["checks"]["acyclic"] = {
        "status": "fail",
        "evidence": "clip-01-take-01 points back to itself.",
    }
    assert list(Draft202012Validator(schema).iter_errors(inconsistent_pass))


def test_seedance_generation_contract_rejects_incomplete_directors_read():
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "scene_plan.schema.json").read_text()
    )
    artifact = {
        "version": "1.0",
        "scenes": [
            {
                "id": "clip-01",
                "type": "generated",
                "description": "Incomplete Seedance scene.",
                "start_seconds": 0,
                "end_seconds": 5,
                "generation_contract": {
                    "model_family": "seedance",
                    "mode": "text_to_video",
                    "shot_structure": "single_take",
                    "continuation_type": "standalone",
                    "felt_intent": "unease",
                    "planned_start_state": "empty road",
                    "planned_end_state": "SUV stops",
                    "identity_anchors": ["white compact SUV"],
                    "prompt_budget": {
                        "primary_spend": "motion",
                        "economized": ["background detail"],
                    },
                    "seedance_contract": {
                        "lane": "narrative",
                        "authoring_state": {"dramatic_function": "reveal"},
                        "primary_action": "SUV stops",
                        "shot_design": {
                            "framing": "wide",
                            "camera": "static",
                            "lighting": "streetlight",
                            "behavior": "hard braking",
                        },
                        "sound_intent": "tires",
                        "prompt_carriers": ["hard braking"],
                        "exclusions": ["no text"],
                        "continuity_state": {
                            "source_status": "planned",
                            "extension_depth": 0,
                            "observation_confidence": "unobserved",
                            "uncertainties": [],
                        },
                    },
                },
            }
        ],
    }

    errors = list(Draft202012Validator(schema).iter_errors(artifact))
    assert errors
    assert any("turn" in error.message for error in errors)


def test_seedance_keep_decision_must_enter_canon():
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "asset_manifest.schema.json").read_text()
    )
    artifact = {
        "version": "1.0",
        "assets": [
            {
                "id": "take-01",
                "type": "video",
                "path": "assets/video/take-01.mp4",
                "source_tool": "seedance_video",
                "scene_id": "clip-01",
                "model_family": "seedance",
                "provider": "fal",
                "model": "seedance-2.0",
                "prompt_review": {
                    "draft": "draft",
                    "final": "final",
                    "skills_applied": list(SKILL_NAMES),
                    "continuity_checked": True,
                    "reference_roles_checked": True,
                    "provider_preflight": _provider_preflight_report(),
                },
                "take_review": {
                    "decision": "keep",
                    "issues": [],
                    "accepted_as_canon": False,
                    "canon_status": "not_accepted",
                    "extension_depth": 0,
                    "observation_confidence": "high",
                    "uncertainties": [],
                    "next_action": "continue",
                },
            }
        ],
        "lineage_review": _standalone_lineage_review("take-01"),
    }

    assert list(Draft202012Validator(schema).iter_errors(artifact))


def test_generic_asset_reviews_do_not_require_seedance_specific_fields():
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "asset_manifest.schema.json").read_text()
    )
    artifact = {
        "version": "1.0",
        "assets": [
            {
                "id": "generic-take",
                "type": "video",
                "path": "assets/video/generic-take.mp4",
                "source_tool": "veo_video",
                "scene_id": "scene-01",
                "model_family": "generic",
                "prompt_review": {
                    "draft": "draft",
                    "final": "final",
                    "skills_applied": ["ai-video-gen"],
                    "continuity_checked": False,
                    "reference_roles_checked": False,
                },
                "take_review": {
                    "decision": "post_fix",
                    "issues": ["remove tail in post"],
                    "accepted_as_canon": False,
                    "next_action": "trim the tail",
                },
            }
        ],
    }

    Draft202012Validator(schema).validate(artifact)


def _minimal_seedance_scene_contract() -> dict:
    return {
        "model_family": "seedance",
        "mode": "image_to_video",
        "shot_structure": "single_take",
        "continuation_type": "standalone",
        "felt_intent": "Show controlled vehicle weight.",
        "planned_start_state": "SUV is stationary.",
        "planned_end_state": "SUV stops at the lane marker.",
        "identity_anchors": ["white compact SUV"],
        "identity_ids": ["vehicle-white-suv"],
        "prompt_budget": {
            "primary_spend": "motion",
            "economized": ["background traffic"],
        },
        "seedance_contract": {
            "lane": "utility",
            "authoring_state": {
                "utility_intent": "Demonstrate one controlled braking action.",
                "non_narrative_refusal": "Do not add conflict or transformation.",
            },
            "primary_action": "SUV brakes at the lane marker.",
            "shot_design": {
                "framing": "low medium-wide",
                "lens": "35mm natural perspective",
                "blocking": "SUV holds the lane center",
                "camera": "locked 35mm",
                "camera_axis": "driver-side profile axis",
                "screen_direction": "left-to-right, then stationary",
                "lighting": "overcast daylight",
                "behavior": "suspension compresses once",
            },
            "temporal_beats": [
                {
                    "beat_id": "controlled-stop",
                    "order": 1,
                    "action": "SUV brakes once at the lane marker",
                    "camera_phase": "locked throughout",
                    "completed_end_state": "SUV is stationary at the marker",
                }
            ],
            "sound_intent": "tire contact and engine load",
            "prompt_carriers": ["suspension compresses once"],
            "exclusions": ["no readable text"],
            "continuity_state": {
                "source_status": "canonical_reference",
                "extension_depth": 0,
                "reanchor_required": False,
                "observation_confidence": "high",
                "uncertainties": [],
            },
        },
    }


def _scene_artifact(contract: dict) -> dict:
    return {
        "version": "1.0",
        "identity_registry": [
            {
                "id": "vehicle-white-suv",
                "kind": "vehicle",
                "role": "demonstration vehicle",
                "canonical_anchor": "white compact SUV",
                "immutable_traits": {
                    "silhouette": "compact SUV",
                    "proportions": "short overhangs and tall cabin",
                    "palette": ["white paint"],
                    "materials": ["painted metal"],
                    "signature_features": ["split lamps"],
                },
                "allowed_changes": ["wheel rotation", "suspension compression"],
                "reference_tags": [],
            }
        ],
        "scenes": [
            {
                "id": "clip-01",
                "type": "generated",
                "description": "Controlled braking insert.",
                "start_seconds": 0,
                "end_seconds": 5,
                "generation_contract": contract,
            }
        ],
    }


def test_generation_contract_requires_explicit_model_family():
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "scene_plan.schema.json").read_text()
    )
    contract = _minimal_seedance_scene_contract()
    del contract["model_family"]

    errors = list(Draft202012Validator(schema).iter_errors(_scene_artifact(contract)))
    assert any("model_family" in error.message for error in errors)


def test_seedance_scene_requires_project_identity_registry():
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "scene_plan.schema.json").read_text()
    )
    artifact = _scene_artifact(_minimal_seedance_scene_contract())
    del artifact["identity_registry"]

    errors = list(Draft202012Validator(schema).iter_errors(artifact))
    assert any("identity_registry" in error.message for error in errors)


def test_seedance_scene_requires_temporal_beats():
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "scene_plan.schema.json").read_text()
    )
    contract = _minimal_seedance_scene_contract()
    del contract["seedance_contract"]["temporal_beats"]

    errors = list(Draft202012Validator(schema).iter_errors(_scene_artifact(contract)))
    assert any("temporal_beats" in error.message for error in errors)


def test_connected_seedance_scene_rejects_planned_source_state():
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "scene_plan.schema.json").read_text()
    )
    contract = _minimal_seedance_scene_contract()
    contract["continuation_type"] = "seamless_continuation"
    contract["seedance_contract"]["continuity_state"] = {
        "source_status": "planned",
        "extension_depth": 1,
        "observation_confidence": "unobserved",
        "uncertainties": [],
    }

    assert list(Draft202012Validator(schema).iter_errors(_scene_artifact(contract)))


def test_connected_seedance_scene_requires_observed_parent_state():
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "scene_plan.schema.json").read_text()
    )
    contract = _minimal_seedance_scene_contract()
    contract["continuation_type"] = "seamless_continuation"
    contract["seedance_contract"]["continuity_state"] = {
        "source_status": "accepted",
        "extension_depth": 1,
        "observation_confidence": "high",
        "uncertainties": [],
    }

    errors = list(Draft202012Validator(schema).iter_errors(_scene_artifact(contract)))
    messages = " ".join(error.message for error in errors)
    assert "parent_asset_id" in messages
    assert "observed_start_state" in messages


def test_third_output_extension_requires_reanchor():
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "scene_plan.schema.json").read_text()
    )
    contract = _minimal_seedance_scene_contract()
    contract["continuation_type"] = "seamless_continuation"
    contract["seedance_contract"]["continuity_state"] = {
        "parent_asset_id": "clip-00-take-01",
        "source_status": "accepted",
        "observed_start_state": "SUV is aligned at the lane marker.",
        "extension_depth": 3,
        "reanchor_required": False,
        "observation_confidence": "high",
        "uncertainties": [],
    }

    assert list(Draft202012Validator(schema).iter_errors(_scene_artifact(contract)))


def test_seedance_asset_requires_full_skill_and_check_audit():
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "asset_manifest.schema.json").read_text()
    )
    artifact = {
        "version": "1.0",
        "assets": [
            {
                "id": "take-01",
                "type": "video",
                "path": "assets/video/take-01.mp4",
                "source_tool": "seedance_video",
                "scene_id": "clip-01",
                "model_family": "seedance",
                "provider": "fal",
                "model": "seedance-2.0",
                "prompt_review": {
                    "draft": "draft",
                    "final": "final",
                    "skills_applied": ["seedance-prompting", "seedance-quality"],
                    "continuity_checked": False,
                    "reference_roles_checked": False,
                    "provider_preflight": _provider_preflight_report(),
                },
                "take_review": {
                    "decision": "reject",
                    "issues": ["identity drift"],
                    "accepted_as_canon": False,
                    "canon_status": "not_accepted",
                    "extension_depth": 0,
                    "observation_confidence": "high",
                    "uncertainties": [],
                    "next_action": "re-anchor",
                },
            }
        ],
        "lineage_review": _standalone_lineage_review("take-01"),
    }

    assert list(Draft202012Validator(schema).iter_errors(artifact))
