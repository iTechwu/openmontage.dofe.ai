"""Unit tests for dofe_video payload construction (dev-guide §5.2, §8.1).

Focus: text block NO role; image_to_video uses role:"first_frame";
reference_to_video uses role:"reference" (cap 9); videoOperation/durationSeconds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.base_tool import ToolStatus
from tools.dofe.client import DofeClient
from tools.dofe.errors import DofeTaskTimeoutError
from tools.video.dofe_video import MAX_REFERENCE_IMAGES, DofeVideo


def _payload(inputs):
    return DofeVideo()._build_payload({**inputs}, "seedance-2.0-fast")


def test_text_to_video_text_block_has_no_role():
    payload = _payload({"prompt": "a cat", "operation": "text_to_video"})
    assert "role" not in payload["content"][0]
    assert payload["params"]["videoOperation"] == "text_to_video"


def test_endpoint_and_model():
    payload = _payload({"prompt": "x"})
    assert payload["endpointKind"] == "video_async"
    assert payload["model"] == "seedance-2.0-fast"


def test_image_to_video_uses_first_frame_role():
    payload = _payload({
        "prompt": "a cat", "operation": "image_to_video", "image_url": "https://cdn.test/f.png",
    })
    roles = [c.get("role") for c in payload["content"]]
    assert roles == [None, "first_frame"]


def test_image_to_video_requires_image():
    with pytest.raises(ValueError, match="image_to_video requires"):
        _payload({"prompt": "x", "operation": "image_to_video"})


def test_reference_to_video_reference_roles():
    refs = [f"https://cdn.test/r{i}.png" for i in range(3)]
    payload = _payload({"prompt": "x", "operation": "reference_to_video", "reference_image_urls": refs})
    roles = [c.get("role") for c in payload["content"]]
    assert roles[0] is None  # text
    assert all(r == "reference" for r in roles[1:])
    assert len(roles) == 4


def test_reference_to_video_accepts_selector_single_reference_alias():
    payload = _payload(
        {
            "prompt": "x",
            "operation": "reference_to_video",
            "reference_image_url": "https://cdn.test/reference.png",
        }
    )

    assert payload["content"][1]["role"] == "reference"
    assert payload["content"][1]["part"]["image_url"]["url"] == (
        "https://cdn.test/reference.png"
    )


def test_reference_to_video_rejects_non_https_reference_url():
    with pytest.raises(ValueError, match="must be https"):
        _payload(
            {
                "prompt": "x",
                "operation": "reference_to_video",
                "reference_image_urls": ["http://cdn.test/reference.png"],
            }
        )


def test_reference_to_video_local_paths_inlined(tmp_path):
    paths = []
    for i in range(2):
        p = tmp_path / f"r{i}.png"
        p.write_bytes(b"\x89PNG")
        paths.append(str(p))
    payload = _payload({"prompt": "x", "operation": "reference_to_video", "reference_image_paths": paths})
    for block in payload["content"][1:]:
        assert block["part"]["image_url"]["url"].startswith("data:image/png;base64,")


def test_reference_to_video_cap_enforced():
    refs = [f"https://cdn.test/r{i}.png" for i in range(MAX_REFERENCE_IMAGES + 1)]
    with pytest.raises(ValueError, match="at most"):
        _payload({"prompt": "x", "operation": "reference_to_video", "reference_image_urls": refs})


def test_duration_seconds_parsed_from_string():
    payload = _payload({"prompt": "x", "duration": "10"})
    assert payload["params"]["durationSeconds"] == 10


def test_duration_is_clamped_to_gateway_minimum():
    payload = _payload({"prompt": "x", "duration": "4"})
    assert payload["params"]["durationSeconds"] == 5


def test_generate_audio_and_ratio_mapped():
    payload = _payload({"prompt": "x", "generate_audio": True, "aspect_ratio": "9:16"})
    assert payload["params"]["generateAudio"] is True
    assert payload["params"]["ratio"] == "9:16"


class _PricingClient:
    def __init__(self):
        self.requests = []

    def quote(self, request):
        self.requests.append(request)
        has_video = request["pricingContext"]["hasVideoInput"]
        unit_price = 22 if has_video else 37
        quantity = request["outputTokens"]
        return {
            "modelAlias": "seedance-2.0-fast",
            "billingUnit": "TOKENS",
            "currency": "CNY",
            "estimatedTotal": quantity / 1_000_000 * unit_price,
            "source": "tenant",
            "warnings": [],
            "selection": {
                "quantity": quantity,
                "unitPrice": unit_price,
                "formula": f"{quantity} output tokens / 1000000 * {unit_price}",
            },
        }


def test_text_to_video_dry_run_reports_37_cny_unit_rate(monkeypatch):
    client = _PricingClient()
    monkeypatch.setattr("tools.video.dofe_video.DofePricingClient", lambda: client)
    monkeypatch.setattr(
        "tools.video.dofe_video.DofeClient.list_models",
        lambda _self: [{"id": "catalog-video"}],
    )

    result = DofeVideo().dry_run(
        {"prompt": "x", "operation": "text_to_video", "model_name": "catalog-video"}
    )

    assert result["estimated_cost_usd"] is None
    assert result["pricing"]["amount"] is None
    assert result["pricing"]["unit_price"] == 37
    assert result["pricing"]["currency"] == "CNY"
    assert result["pricing"]["quote_basis"] == "unit_rate"
    assert result["pricing"]["requires_actual_usage"] is True
    assert client.requests[0]["pricingContext"]["hasVideoInput"] is False


def test_image_to_video_quote_uses_video_input_rate_and_estimated_tokens(monkeypatch):
    client = _PricingClient()
    monkeypatch.setattr("tools.video.dofe_video.DofePricingClient", lambda: client)
    monkeypatch.setattr(
        "tools.video.dofe_video.DofeClient.list_models",
        lambda _self: [{"id": "catalog-video"}],
    )

    result = DofeVideo().dry_run(
        {
            "prompt": "x",
            "operation": "image_to_video",
            "model_name": "catalog-video",
            "estimated_output_tokens": 108_900,
        }
    )

    assert result["pricing"]["amount"] == pytest.approx(2.3958)
    assert result["pricing"]["currency"] == "CNY"
    assert result["pricing"]["unit_price"] == 22
    assert result["pricing"]["quote_basis"] == "estimated_usage"
    assert result["pricing"]["requires_actual_usage"] is False
    assert client.requests[0]["pricingContext"]["hasVideoInput"] is True


def test_video_dry_run_rejects_model_missing_from_catalog_before_quote(monkeypatch):
    def fail_quote():
        raise AssertionError("pricing must not run for a model absent from the catalog")

    monkeypatch.setattr("tools.video.dofe_video.DofePricingClient", fail_quote)
    monkeypatch.setattr(
        "tools.video.dofe_video.DofeClient.list_models",
        lambda _self: [{"id": "tenant-video"}],
    )

    result = DofeVideo().dry_run(
        {"prompt": "x", "model_name": "invented-video"}
    )

    assert result["pricing"]["available"] is False
    assert "not returned by GET /v1/models" in result["pricing"]["error"]


def test_status_fails_closed_when_configured_model_is_hidden(monkeypatch):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_VIDEO_MODEL", "hidden-video")
    monkeypatch.setattr(
        DofeClient,
        "list_models",
        lambda _self: [{"id": "visible-video"}],
    )

    assert DofeVideo().get_status() == ToolStatus.UNAVAILABLE


def test_status_is_available_when_any_configured_model_is_visible(monkeypatch):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_MODEL_TEXT_TO_VIDEO", "hidden-video")
    monkeypatch.setenv("DOFE_MODEL_REFERENCE_TO_VIDEO", "visible-video")
    monkeypatch.setattr(
        DofeClient,
        "list_models",
        lambda _self: [{"id": "visible-video"}],
    )

    assert DofeVideo().get_status() == ToolStatus.AVAILABLE


def test_live_preflight_blocks_an_unsupported_video_operation(monkeypatch):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_VIDEO_MODEL", "catalog-video")
    monkeypatch.setattr(
        DofeClient,
        "list_models",
        lambda _self: [{"id": "catalog-video"}],
    )
    monkeypatch.setattr(
        DofeClient,
        "get_playground_capability",
        lambda _self, _model: {
            "alias": "catalog-video",
            "modelType": "video",
            "state": "ready",
            "executor": "generation_task",
            "endpointKind": "video_async",
            "input": {
                "text": True,
                "acceptedAssetTypes": [],
                "roles": [],
            },
            "operations": [
                {
                    "id": "text_to_video",
                    "labelKey": "operation.text_to_video",
                    "constraints": {
                        "acceptedAssetTypes": [],
                        "roles": [],
                    },
                }
            ],
            "form": {"fields": []},
            "output": {"mode": "task"},
            "readiness": [],
        },
        raising=False,
    )

    report = DofeVideo().preflight(
        {
            "prompt": "Keep the vehicle identity stable.",
            "operation": "reference_to_video",
            "reference_image_urls": ["https://example.com/vehicle.png"],
        },
        live=True,
    )

    assert report["status"] == "blocked"
    assert any("reference_to_video" in error["message"] for error in report["errors"])


def test_live_preflight_verifies_dofe_reference_contract(monkeypatch):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_VIDEO_MODEL", "catalog-seedance")
    monkeypatch.setattr(
        DofeClient,
        "list_models",
        lambda _self: [{"id": "catalog-seedance"}],
    )
    monkeypatch.setattr(
        DofeClient,
        "get_playground_capability",
        lambda _self, _model: {
            "alias": "catalog-seedance",
            "modelType": "video",
            "state": "ready",
            "executor": "generation_task",
            "endpointKind": "video_async",
            "input": {
                "text": True,
                "acceptedAssetTypes": ["image"],
                "roles": ["reference"],
                "minInputAssets": 1,
                "maxInputAssets": 9,
            },
            "operations": [
                {
                    "id": "reference_to_video",
                    "labelKey": "operation.reference_to_video",
                    "constraints": {
                        "acceptedAssetTypes": ["image"],
                        "roles": ["reference"],
                        "minInputAssets": 1,
                        "maxInputAssets": 9,
                        "allowedValues": {
                            "videoOperation": ["reference_to_video"]
                        },
                    },
                }
            ],
            "form": {
                "fields": [
                    {"key": "durationSeconds", "type": "slider", "labelKey": "duration"},
                    {"key": "ratio", "type": "select", "labelKey": "ratio"},
                    {"key": "generateAudio", "type": "switch", "labelKey": "audio"},
                ]
            },
            "output": {"mode": "task"},
            "readiness": [],
        },
        raising=False,
    )

    report = DofeVideo().preflight(
        {
            "prompt": "Keep the vehicle identity stable.",
            "operation": "reference_to_video",
            "duration": "5",
            "aspect_ratio": "9:16",
            "generate_audio": False,
            "reference_image_urls": ["https://example.com/vehicle.png"],
            "reference_roles": [
                {
                    "tag": "vehicle-identity-reference",
                    "binding_mode": "input_parameter",
                    "role": "identity",
                }
            ],
        },
        live=True,
    )

    assert report["status"] == "passed"
    assert report["verification_level"] == "live_provider_contract"
    assert report["live_probe"]["operation"] == "reference_to_video"
    assert report["live_probe"]["reference_binding"]["roles"] == ["reference"]


@pytest.mark.parametrize(
    ("capability_change", "expected_error"),
    [
        ({"input": {"text": False}}, "text prompt"),
        (
            {
                "readiness": [
                    {
                        "code": "smoke_pending",
                        "severity": "blocked",
                        "action": "view_release_status",
                    }
                ]
            },
            "smoke_pending",
        ),
        (
            {
                "form": {
                    "fields": [
                        {"key": "durationSeconds", "type": "slider", "labelKey": "duration"},
                        {"key": "ratio", "type": "select", "labelKey": "ratio"},
                        {"key": "generateAudio", "type": "switch", "labelKey": "audio"},
                        {
                            "key": "resolution",
                            "type": "select",
                            "labelKey": "resolution",
                            "required": True,
                        },
                    ]
                }
            },
            "required provider parameter 'resolution'",
        ),
    ],
)
def test_live_preflight_fails_closed_on_non_executable_projection(
    monkeypatch,
    capability_change,
    expected_error,
):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_VIDEO_MODEL", "catalog-video")
    monkeypatch.setattr(
        DofeClient,
        "list_models",
        lambda _self: [{"id": "catalog-video"}],
    )
    capability = {
        "alias": "catalog-video",
        "modelType": "video",
        "state": "ready",
        "executor": "generation_task",
        "endpointKind": "video_async",
        "input": {"text": True, "acceptedAssetTypes": [], "roles": []},
        "operations": [
            {
                "id": "text_to_video",
                "labelKey": "operation.text_to_video",
                "constraints": {"acceptedAssetTypes": [], "roles": []},
            }
        ],
        "form": {
            "fields": [
                {"key": "durationSeconds", "type": "slider", "labelKey": "duration"},
                {"key": "ratio", "type": "select", "labelKey": "ratio"},
                {"key": "generateAudio", "type": "switch", "labelKey": "audio"},
            ]
        },
        "output": {"mode": "task"},
        "readiness": [],
    }
    capability.update(capability_change)
    monkeypatch.setattr(
        DofeClient,
        "get_playground_capability",
        lambda _self, _model: capability,
    )

    report = DofeVideo().preflight(
        {"prompt": "A controlled camera move."},
        live=True,
    )

    assert report["status"] == "blocked"
    assert any(expected_error in error["message"] for error in report["errors"])


# A passing live capability projection used by the paid-boundary tests below.
_PASSING_CAPABILITY = {
    "alias": "catalog-video",
    "modelType": "video",
    "state": "ready",
    "executor": "generation_task",
    "endpointKind": "video_async",
    "input": {"text": True},
    "operations": [
        {
            "id": "text_to_video",
            "constraints": {"acceptedAssetTypes": [], "roles": []},
        }
    ],
    "form": {
        "fields": [
            {"key": "durationSeconds", "min": 5},
            {"key": "ratio", "options": ["16:9", "9:16"]},
            {"key": "generateAudio", "type": "switch", "labelKey": "audio"},
        ]
    },
    "output": {"mode": "task"},
    "readiness": [],
}


def test_execute_fails_closed_at_paid_boundary_when_model_not_in_catalog(monkeypatch):
    """A direct caller that skipped the Skill cannot reach paid generation."""
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_VIDEO_MODEL", "seedance-2.0-fast")

    list_calls: list[int] = []
    monkeypatch.setattr(
        DofeClient,
        "list_models",
        lambda _self: list_calls.append(1) or [{"id": "another-model"}],
    )
    submit_calls: list[int] = []
    monkeypatch.setattr(
        DofeClient,
        "submit_and_collect",
        lambda self, *a, **k: submit_calls.append(1),
    )

    result = DofeVideo().execute({"prompt": "a cat playing piano"})

    assert not result.success
    # The catalog gate stopped it before any paid generation started.
    assert submit_calls == []
    # The tenant catalog is fetched exactly once and shared, not re-fetched.
    assert len(list_calls) == 1


def test_execute_runs_live_probe_and_shares_catalog_before_generation(monkeypatch):
    """Preflight is enforced at the boundary, then generation reuses the catalog."""
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_VIDEO_MODEL", "catalog-video")

    list_calls: list[int] = []
    monkeypatch.setattr(
        DofeClient,
        "list_models",
        lambda _self: list_calls.append(1) or [{"id": "catalog-video"}],
    )
    capability_calls: list[str] = []
    monkeypatch.setattr(
        DofeClient,
        "get_playground_capability",
        lambda _self, model: capability_calls.append(model) or _PASSING_CAPABILITY,
    )
    submit_calls: list[int] = []

    def fake_submit(self, *a, **k):
        submit_calls.append(1)
        raise DofeTaskTimeoutError("task timed out")

    monkeypatch.setattr(DofeClient, "submit_and_collect", fake_submit)

    result = DofeVideo().execute({"prompt": "a cat playing piano"})

    # Live preflight passed (capability probed) and reached paid generation,
    # which then timed out — proving the catalog was shared, not re-fetched.
    assert not result.success
    assert capability_calls == ["catalog-video"]
    assert len(submit_calls) == 1
    assert len(list_calls) == 1


def test_execute_blocks_paid_generation_when_live_probe_is_blocked(monkeypatch):
    """The shared paid boundary fail-closes when the live contract is blocked."""
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_VIDEO_MODEL", "catalog-video")

    list_calls: list[int] = []
    monkeypatch.setattr(
        DofeClient,
        "list_models",
        lambda _self: list_calls.append(1) or [{"id": "catalog-video"}],
    )
    submit_calls: list[int] = []
    monkeypatch.setattr(
        DofeClient,
        "submit_and_collect",
        lambda self, *a, **k: submit_calls.append(1),
    )

    def blocked_probe(self, inputs):
        return {
            "status": "blocked",
            "errors": ["operation reference_to_video is not supported by model catalog-video"],
        }

    monkeypatch.setattr(DofeVideo, "probe_provider_contract", blocked_probe)

    result = DofeVideo().execute(
        {"prompt": "a cat playing piano", "operation": "reference_to_video"}
    )

    assert not result.success
    assert "not supported" in result.error.lower()
    assert submit_calls == []
    assert len(list_calls) == 1


def test_paid_boundary_rejects_a_caller_supplied_forged_catalog():
    """A forgeable catalog can no longer be passed to the paid boundary.

    ``run_dofe_generation`` and ``probe_provider_contract`` no longer accept a
    ``catalog`` keyword: the boundary always fetches the authenticated tenant
    catalog via ``resolve_catalog``. Passing one must be a hard error so a forged
    snapshot can never unlock paid generation.
    """
    from tools.dofe.runtime import run_dofe_generation

    forged = [{"id": "catalog-video"}]
    tool = DofeVideo()
    inputs = {"prompt": "a cat playing piano", "operation": "text_to_video"}

    with pytest.raises(TypeError):
        run_dofe_generation(tool, inputs, catalog=forged)
    with pytest.raises(TypeError):
        tool.probe_provider_contract(inputs, catalog=forged)


def test_execute_consults_authenticated_catalog_not_any_caller_value(monkeypatch):
    """Paid generation is gated on the authenticated catalog, not a forge.

    The configured model is absent from the authenticated ``GET /v1/models``;
    no caller can supply a catalog that lists it, so paid submit is refused.
    """
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_VIDEO_MODEL", "catalog-video")

    monkeypatch.setattr(
        DofeClient,
        "list_models",
        lambda _self: [{"id": "some-other-model"}],
    )
    submit_calls: list[int] = []
    monkeypatch.setattr(
        DofeClient,
        "submit_and_collect",
        lambda self, *a, **k: submit_calls.append(1),
    )

    result = DofeVideo().execute({"prompt": "a cat playing piano"})

    assert not result.success
    assert submit_calls == []


def test_video_selector_shares_one_catalog_and_capability_per_request(monkeypatch):
    """A single selector request fetches catalog and capability exactly once."""
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_VIDEO_MODEL", "catalog-video")

    list_calls: list[int] = []
    monkeypatch.setattr(
        DofeClient,
        "list_models",
        lambda _self: list_calls.append(1) or [{"id": "catalog-video"}],
    )
    capability_calls: list[str] = []
    monkeypatch.setattr(
        DofeClient,
        "get_playground_capability",
        lambda _self, model: capability_calls.append(model) or _PASSING_CAPABILITY,
    )
    monkeypatch.setattr(
        DofeClient,
        "submit_and_collect",
        lambda _self, *_a, **_k: {
            "task_id": "task-1",
            "status": "succeeded",
            "assets": [{"url": "https://cdn.test/out.mp4", "kind": "video"}],
        },
    )
    monkeypatch.setattr(
        DofeClient,
        "download",
        lambda _self, _url, path: Path(path).write_bytes(b"video" * 300),
    )

    from tools.tool_registry import ToolRegistry

    registry = ToolRegistry()
    registry.discover("tools.video")
    selector = registry.get("video_selector")

    result = selector.execute(
        {
            "prompt": "a cat playing piano",
            "operation": "text_to_video",
            "allowed_providers": ["dofe"],
            "execution_scope": "sample",
            "allow_degraded_preflight": True,
        }
    )

    assert result.success
    assert result.data["selected_tool"] == "dofe_video"
    # Selection, preflight, and execution all shared one catalog snapshot.
    assert len(list_calls) == 1
    # The capability probe was also cached and executed only once per model.
    assert capability_calls == ["catalog-video"]
