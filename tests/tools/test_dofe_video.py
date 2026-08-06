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

    result = DofeVideo().dry_run({"prompt": "x", "operation": "text_to_video"})

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

    result = DofeVideo().dry_run(
        {
            "prompt": "x",
            "operation": "image_to_video",
            "estimated_output_tokens": 108_900,
        }
    )

    assert result["pricing"]["amount"] == pytest.approx(2.3958)
    assert result["pricing"]["currency"] == "CNY"
    assert result["pricing"]["unit_price"] == 22
    assert result["pricing"]["quote_basis"] == "estimated_usage"
    assert result["pricing"]["requires_actual_usage"] is False
    assert client.requests[0]["pricingContext"]["hasVideoInput"] is True
