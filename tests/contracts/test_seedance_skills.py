from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tools.video.dofe_video import DofeVideo
from tools.video.higgsfield_video import HiggsFieldVideo
from tools.video.runway_video import RunwayVideo
from tools.video.seedance_replicate import SeedanceReplicate
from tools.video.seedance_video import SeedanceVideo
from tools.video.video_selector import VideoSelector


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


@pytest.mark.parametrize("director_path", SEEDANCE_PIPELINE_DIRECTORS)
def test_video_pipelines_route_seedance_through_shared_production_contract(director_path: str):
    content = (ROOT / director_path).read_text()
    assert "skills/creative/seedance-production.md" in content


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
    native = VideoSelector._tool_context_payload(tool, {"model": "gen4_turbo"})
    seedance = VideoSelector._tool_context_payload(tool, {"model": "seedance_2.0"})

    assert native["required_agent_skills"] == ["ai-video-gen"]
    assert seedance["required_agent_skills"][:5] == list(SKILL_NAMES)
    assert tool.get_info()["agent_skills"][:5] == list(SKILL_NAMES)


def test_scene_plan_accepts_seedance_generation_contract():
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "scene_plan.schema.json").read_text()
    )
    artifact = {
        "version": "1.0",
        "scenes": [
            {
                "id": "clip-01",
                "type": "generated",
                "description": "Vehicle hero enters the tunnel.",
                "start_seconds": 0,
                "end_seconds": 6,
                "generation_contract": {
                    "provider_family": "seedance",
                    "mode": "reference_to_video",
                    "shot_structure": "single_take",
                    "continuation_type": "sequence_first_clip",
                    "felt_intent": "The apparent rescuer becomes a threat.",
                    "planned_start_state": "Tunnel entrance is empty.",
                    "planned_end_state": "Vehicle blocks the exit, facing camera.",
                    "identity_anchors": ["compact SUV, white paint, split lamps"],
                    "reference_roles": [
                        {
                            "tag": "@Image1",
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
                            "stock_solution_refused": "no generic transformation reveal",
                        },
                        "primary_action": "SUV slides sideways and blocks the exit",
                        "shot_design": {
                            "framing": "low medium-wide, SUV enters frame left",
                            "camera": "35mm lateral track, then locked endpoint",
                            "lighting": "cold tunnel practicals sweep across white paint",
                            "behavior": "suspension compresses, tires bite, lamps hesitate",
                        },
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
                    "final": "@Image1 controls vehicle identity only...",
                    "skills_applied": [
                        "seedance-directing",
                        "seedance-continuity",
                        "seedance-prompting",
                        "seedance-quality",
                    ],
                    "continuity_checked": True,
                    "reference_roles_checked": True,
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
    }

    Draft202012Validator(schema).validate(artifact)


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
                    "provider_family": "seedance",
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
                    "skills_applied": ["seedance-prompting", "seedance-quality"],
                    "continuity_checked": True,
                    "reference_roles_checked": True,
                },
                "take_review": {
                    "decision": "keep",
                    "issues": [],
                    "accepted_as_canon": False,
                    "canon_status": "not_accepted",
                    "next_action": "continue",
                },
            }
        ],
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
