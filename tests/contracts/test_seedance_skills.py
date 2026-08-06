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


ROOT = Path(__file__).resolve().parents[2]
SKILL_NAMES = (
    "seedance-provider",
    "seedance-directing",
    "seedance-continuity",
    "seedance-prompting",
    "seedance-quality",
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
                    "observed_end_state": "SUV stops square to camera at the exit.",
                    "next_action": "Compile clip-02 from the observed stop state.",
                },
            }
        ],
    }

    Draft202012Validator(schema).validate(artifact)
