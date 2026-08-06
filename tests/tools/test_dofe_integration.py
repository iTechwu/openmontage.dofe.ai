"""Integration tests against the live models.dofe.ai test gateway (dev-guide §8.3).

These make real (billable) network calls, so they are SKIPPED unless explicitly
enabled with DOFE_RUN_INTEGRATION=1. They never run under `make test` or
`make test-contracts`.

Manual run:
  DOFE_RUN_INTEGRATION=1 \\
  DOFE_MODEL_BASE_URL=https://model.local.dofe.ai/api \\
  DOFE_MODEL_API_KEY=sk-... \\
  DOFE_IMAGE_MODEL=seedream-5.0 \\
  .venv/bin/python -m pytest tests/tools/test_dofe_integration.py -v -s
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

pytestmark = pytest.mark.skipif(
    os.environ.get("DOFE_RUN_INTEGRATION") != "1",
    reason="Set DOFE_RUN_INTEGRATION=1 (and DOFE_MODEL_API_KEY) to run live gateway tests.",
)


def _output(tmp_path, name, ext):
    # Keep the artifact inside the tmp dir but under a projects/ subtree so the
    # runtime's projects/ enforcement accepts the path verbatim.
    path = tmp_path / "projects" / "test-dofe-integration" / "assets" / (name + ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def test_dofe_image_real_generation(tmp_path):
    from tools.graphics.dofe_image import DofeImage

    result = DofeImage().execute({
        "prompt": "a single red apple on a clean white table, soft studio light, photoreal",
        "output_path": _output(tmp_path, "apple", ".png"),
    })
    assert result.success, f"image generation failed: {result.error}"
    assert result.data["provider"] == "dofe"
    assert result.data["dofe_task_id"]
    assert result.data["dofe_status"] == "succeeded"
    assert result.data["dofe_final_cost"] is not None
    assert result.data["model"] == os.environ.get("DOFE_IMAGE_MODEL", "seedream-5.0")
    out = Path(result.data["output_path"])
    assert out.is_file() and out.stat().st_size >= 1024


def test_dofe_video_seedance_returns_clear_error(tmp_path):
    from tools.video.dofe_video import DofeVideo

    result = DofeVideo().execute({
        "prompt": "a cat playing piano",
        "output_path": _output(tmp_path, "cat", ".mp4"),
    })
    # An unavailable seedance-2.0-fast route must fail clearly,
    # fast, and name the model + a suggestion — never hang.
    assert not result.success
    assert result.error is not None
    assert "seedance-2.0-fast" in result.error
    assert "dofe" in result.error.lower()
