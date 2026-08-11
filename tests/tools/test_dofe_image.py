"""Unit tests for dofe_image payload construction (dev-guide §5.1, §8.1).

Focus: text block NEVER carries a role; reference image inlined as a data URI
with role:"reference"; resolution/outputCount/param mapping.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.graphics.dofe_image import DofeImage
from tools.dofe.runtime import _normalize_image_format, probe_image


def _payload(inputs):
    return DofeImage()._build_payload({**inputs}, "seedream-5.0")


def test_text_block_has_no_role():
    payload = _payload({"prompt": "a red apple"})
    block = payload["content"][0]
    assert block["part"]["type"] == "text"
    assert "role" not in block, "text content block must not carry a role (dev-guide §2.3)"


def test_endpoint_and_model():
    payload = _payload({"prompt": "x"})
    assert payload["endpointKind"] == "image_async"
    assert payload["model"] == "seedream-5.0"


def test_resolution_from_width_height():
    payload = _payload({"prompt": "x", "width": 768, "height": 1344})
    assert payload["params"]["resolution"] == "768x1344"


def test_resolution_from_size_overrides_dimensions():
    payload = _payload({"prompt": "x", "width": 1, "height": 1, "size": "1024x1024"})
    assert payload["params"]["resolution"] == "1024x1024"


def test_output_count():
    payload = _payload({"prompt": "x", "n": 2})
    assert payload["params"]["outputCount"] == 2


def test_reference_image_uses_data_uri_with_reference_role(tmp_path):
    ref = tmp_path / "subject.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n" + b"img")
    payload = _payload({"prompt": "edit this", "image_path": str(ref)})
    assert payload["content"][0]["part"]["type"] == "text"
    ref_block = payload["content"][1]
    assert ref_block["part"]["type"] == "image_url"
    assert ref_block["role"] == "reference"
    assert ref_block["part"]["image_url"]["url"].startswith("data:image/png;base64,")


def test_reference_image_https_url_passthrough():
    payload = _payload({"prompt": "edit", "image_url": "https://cdn.test/r.png"})
    assert payload["content"][1]["part"]["image_url"]["url"] == "https://cdn.test/r.png"


def test_multiple_reference_images_preserve_declared_order(tmp_path):
    local_a = tmp_path / "environment.png"
    local_b = tmp_path / "vehicle.png"
    local_a.write_bytes(b"\x89PNG\r\n\x1a\n" + b"environment")
    local_b.write_bytes(b"\x89PNG\r\n\x1a\n" + b"vehicle")

    payload = _payload(
        {
            "prompt": "combine the references",
            "image_url": "https://cdn.test/primary.png",
            "image_urls": ["https://cdn.test/secondary.png"],
            "image_paths": [str(local_a), str(local_b)],
        }
    )

    references = payload["content"][1:]
    assert [block["order"] for block in references] == [1, 2, 3, 4]
    assert all(block["role"] == "reference" for block in references)
    assert references[0]["part"]["image_url"]["url"] == "https://cdn.test/primary.png"
    assert references[1]["part"]["image_url"]["url"] == "https://cdn.test/secondary.png"
    assert references[2]["part"]["image_url"]["url"].startswith("data:image/png;base64,")
    assert references[3]["part"]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize("model", ["seedream-5.0", "seedream-5.0-lite", "seedream-5.0-pro"])
def test_seedream_payload_never_sends_negative_prompt(model):
    payload = DofeImage()._build_payload(
        {
            "prompt": "x",
            "negative_prompt": "blurry",
            "seed": 7,
            "quality": "high",
            "style": "cinematic",
        },
        model,
    )
    p = payload["params"]
    assert "negativePrompt" not in p
    assert p["seed"] == 7
    assert p["quality"] == "high"
    assert p["style"] == "cinematic"


def test_non_seedream_payload_preserves_negative_prompt():
    payload = DofeImage()._build_payload(
        {"prompt": "x", "negative_prompt": "blurry"},
        "flux-pro-1.1",
    )

    assert payload["params"]["negativePrompt"] == "blurry"


def test_dofe_contract_advertises_model_scoped_negative_prompt():
    tool = DofeImage()
    assert tool.supports["negative_prompt"] == "model_scoped"
    assert "negative_prompt" in tool.input_schema["properties"]
    assert "negative_prompt" in tool.idempotency_key_fields


def test_execute_rejects_model_not_returned_by_gateway_catalog(monkeypatch):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_IMAGE_MODEL", "configured-but-hidden")
    monkeypatch.setattr(
        "tools.dofe.runtime.DofeClient.list_models",
        lambda _self: [{"id": "tenant-visible-image"}],
    )

    def fail_submit(*_args, **_kwargs):
        raise AssertionError("generation task must not be submitted for a hidden model")

    monkeypatch.setattr("tools.dofe.runtime.DofeClient.submit_and_collect", fail_submit)

    result = DofeImage().execute({"prompt": "catalog enforcement"})

    assert not result.success
    assert "not returned by GET /v1/models" in result.error
    assert result.data["model"] == "configured-but-hidden"


def test_negative_prompt_changes_dofe_idempotency_key():
    tool = DofeImage()
    base = {"prompt": "x", "model_name": "flux-pro-1.1"}

    assert tool.idempotency_key(base) != tool.idempotency_key(
        {**base, "negative_prompt": "blurry"}
    )


def test_reference_order_changes_dofe_idempotency_key():
    tool = DofeImage()
    base = {"prompt": "x", "model_name": "seedream-5.0"}

    assert tool.idempotency_key({**base, "image_paths": ["a.png", "b.png"]}) != tool.idempotency_key(
        {**base, "image_paths": ["b.png", "a.png"]}
    )


def test_metadata_carries_idempotency_key():
    payload = _payload({"prompt": "x", "seed": 1})
    assert "metadata" in payload
    assert "openmontage_idempotency_key" in payload["metadata"]


def test_live_preflight_blocks_incompatible_multi_reference_image_request(monkeypatch):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_IMAGE_MODEL", "gpt-image-2-sp")
    monkeypatch.setattr(
        "tools.dofe.status.DofeClient.list_models",
        lambda _self: [{"id": "gpt-image-2-sp"}],
    )
    monkeypatch.setattr(
        "tools.dofe.status.DofeClient.get_playground_capability",
        lambda _self, _model: {
            "alias": "gpt-image-2-sp",
            "modelType": "image",
            "state": "ready",
            "executor": "generation_task",
            "endpointKind": "image_async",
            "input": {"text": True, "acceptedAssetTypes": ["image"], "maxInputAssets": 1},
            "operations": [
                {
                    "id": "image_to_image",
                    "constraints": {
                        "acceptedAssetTypes": ["image"],
                        "roles": ["reference"],
                        "minInputAssets": 1,
                        "maxInputAssets": 1,
                        "allowedValues": {"ratio": ["1:1"]},
                    },
                }
            ],
            "form": {"fields": []},
            "output": {"mode": "asset"},
            "readiness": [],
        },
    )

    result = DofeImage().preflight(
        {
            "prompt": "compose the four references",
            "model_name": "gpt-image-2-sp",
            "aspect_ratio": "16:9",
            "image_urls": [
                "https://cdn.test/scene.png",
                "https://cdn.test/ae86.png",
                "https://cdn.test/001-fr.png",
                "https://cdn.test/yard.png",
            ],
        },
        live=True,
    )

    assert result["status"] == "blocked"
    messages = [error["message"] for error in result["errors"]]
    assert any("at most 1 input assets" in message for message in messages)
    assert any("does not support ratio '16:9'" in message for message in messages)


def test_downloaded_jpeg_is_normalized_to_requested_png(tmp_path):
    output = tmp_path / "generated.png"
    Image.new("RGB", (32, 24), "red").save(output, format="JPEG")

    _normalize_image_format(output)
    metadata = probe_image(output)

    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert metadata["image_format"] == "png"
    assert metadata["width"] == 32
    assert metadata["height"] == 24


# ------------------------------------------------------------------ image_edit


def test_mask_triggers_image_edit_operation_and_mask_role(tmp_path):
    ref = tmp_path / "subject.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n" + b"subject")
    mask = tmp_path / "mask.png"
    mask.write_bytes(b"\x89PNG\r\n\x1a\n" + b"mask")

    payload = DofeImage()._build_payload(
        {
            "prompt": "replace background",
            "image_path": str(ref),
            "mask_path": str(mask),
        },
        "gpt-image-2-sp",
    )

    text, ref_block, mask_block = payload["content"]
    assert text["part"]["type"] == "text"
    assert ref_block["role"] == "reference"
    assert mask_block["role"] == "mask"
    assert mask_block["part"]["image_url"]["url"].startswith("data:image/png;base64,")


def test_image_edit_with_mask_url_appends_mask_block():
    payload = DofeImage()._build_payload(
        {
            "prompt": "remove logo",
            "image_url": "https://cdn.test/product.png",
            "mask_url": "https://cdn.test/mask.png",
        },
        "gpt-image-2-sp",
    )

    ref_block, mask_block = payload["content"][1:]
    assert ref_block["role"] == "reference"
    assert mask_block["role"] == "mask"
    assert mask_block["part"]["image_url"]["url"] == "https://cdn.test/mask.png"


def test_generation_mode_edit_triggers_image_edit_without_mask():
    payload = DofeImage()._build_payload(
        {
            "prompt": "make it red",
            "generation_mode": "edit",
            "image_url": "https://cdn.test/car.png",
        },
        "gpt-image-2-sp",
    )

    assert len(payload["content"]) == 2
    assert payload["content"][1]["role"] == "reference"


def test_gpt_image_params_forwarded():
    payload = DofeImage()._build_payload(
        {
            "prompt": "x",
            "output_format": "webp",
            "output_compression": 75,
            "background": "transparent",
            "moderation": "low",
            "thinking": "medium",
        },
        "gpt-image-2-sp",
    )

    p = payload["params"]
    assert p["output_format"] == "webp"
    assert p["output_compression"] == 75
    assert p["background"] == "transparent"
    assert p["moderation"] == "low"
    assert p["thinking"] == "medium"


def test_mask_and_gpt_params_change_idempotency_key():
    tool = DofeImage()
    base = {"prompt": "x", "model_name": "gpt-image-2-sp"}

    assert tool.idempotency_key(base) != tool.idempotency_key({**base, "mask_path": "m.png"})
    assert tool.idempotency_key(base) != tool.idempotency_key(
        {**base, "output_format": "webp", "output_compression": 75}
    )


def test_live_preflight_blocks_https_for_data_uri_only_image_edit(monkeypatch):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_IMAGE_MODEL", "gpt-image-2-sp")
    monkeypatch.setattr(
        "tools.dofe.status.DofeClient.list_models",
        lambda _self: [{"id": "gpt-image-2-sp"}],
    )
    monkeypatch.setattr(
        "tools.dofe.status.DofeClient.get_playground_capability",
        lambda _self, _model: {
            "alias": "gpt-image-2-sp",
            "modelType": "image",
            "state": "ready",
            "executor": "generation_task",
            "endpointKind": "image_async",
            "input": {"text": True, "acceptedAssetTypes": ["image"], "maxInputAssets": 1},
            "operations": [
                {
                    "id": "image_edit",
                    "constraints": {
                        "acceptedAssetTypes": ["image"],
                        "roles": ["reference", "mask"],
                        "minInputAssets": 1,
                        "maxInputAssets": 1,
                        "inputTransport": "data_uri_only",
                    },
                }
            ],
            "form": {"fields": []},
            "output": {"mode": "asset"},
            "readiness": [],
        },
    )

    result = DofeImage().preflight(
        {
            "prompt": "remove text",
            "model_name": "gpt-image-2-sp",
            "image_url": "https://cdn.test/product.png",
            "mask_url": "https://cdn.test/mask.png",
        },
        live=True,
    )

    assert result["status"] == "blocked"
    messages = [error["message"] for error in result["errors"]]
    assert any("image_url is not allowed" in message for message in messages)
    assert any("mask_url is not allowed" in message for message in messages)


def test_live_preflight_blocks_image_edit_missing_input(monkeypatch):
    monkeypatch.setenv("DOFE_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("DOFE_IMAGE_MODEL", "gpt-image-2-sp")
    monkeypatch.setattr(
        "tools.dofe.status.DofeClient.list_models",
        lambda _self: [{"id": "gpt-image-2-sp"}],
    )
    monkeypatch.setattr(
        "tools.dofe.status.DofeClient.get_playground_capability",
        lambda _self, _model: {
            "alias": "gpt-image-2-sp",
            "modelType": "image",
            "state": "ready",
            "executor": "generation_task",
            "endpointKind": "image_async",
            "input": {"text": True, "acceptedAssetTypes": ["image"]},
            "operations": [
                {
                    "id": "image_edit",
                    "constraints": {
                        "acceptedAssetTypes": ["image"],
                        "roles": ["reference", "mask"],
                        "minInputAssets": 1,
                        "maxInputAssets": 1,
                    },
                }
            ],
            "form": {"fields": []},
            "output": {"mode": "asset"},
            "readiness": [],
        },
    )

    result = DofeImage().preflight(
        {
            "prompt": "remove text",
            "model_name": "gpt-image-2-sp",
            "generation_mode": "edit",
        },
        live=True,
    )

    assert result["status"] == "blocked"
    messages = [error["message"] for error in result["errors"]]
    assert any("image_edit requires at least one input image" in message for message in messages)
