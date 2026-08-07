"""Unit tests for seedance_video preflight gating (Spec: direct-call bypass)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests as _requests  # noqa: E402

from tools.video.seedance_video import SeedanceVideo  # noqa: E402


def test_seedance_preflight_blocks_paid_post_when_reference_missing(monkeypatch) -> None:
    """A direct Seedance call that fails preflight never reaches the paid POST.

    image_to_video without a declared image reference fails the reference-binding
    contract, so preflight is blocked and the fal.ai submit is not sent. This
    closes the direct-call bypass around the live preflight gate.
    """
    monkeypatch.setenv("FAL_KEY", "test-fal-key")

    submitted: list[str] = []
    monkeypatch.setattr(
        _requests,
        "post",
        lambda *args, **kwargs: submitted.append(args[0] if args else kwargs.get("url")),
    )

    result = SeedanceVideo().execute(
        {"prompt": "a cat on Mars", "operation": "image_to_video"}
    )

    assert not result.success
    assert result.data["provider"] == "seedance"
    assert result.data["provider_preflight"]["status"] == "blocked"
    assert "preflight blocked" in result.error.lower()
    assert submitted == []  # no paid fal.ai POST


def test_seedance_preflight_blocks_paid_post_when_tool_unavailable(monkeypatch) -> None:
    """An unavailable tool (no FAL_KEY) fail-closes before any paid POST."""
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.delenv("FAL_AI_API_KEY", raising=False)

    submitted: list[str] = []
    monkeypatch.setattr(
        _requests,
        "post",
        lambda *args, **kwargs: submitted.append(args[0] if args else kwargs.get("url")),
    )

    result = SeedanceVideo().execute({"prompt": "a cat on Mars"})

    assert not result.success
    assert "FAL_KEY" in result.error
    assert submitted == []


def test_seedance_preflight_is_not_blocked_for_valid_text_to_video(monkeypatch) -> None:
    """A valid text_to_video request degrades (no live probe) but is not blocked."""
    monkeypatch.setenv("FAL_KEY", "test-fal-key")

    preflight = SeedanceVideo().preflight(
        {"prompt": "a cat on Mars", "operation": "text_to_video"},
        live=True,
    )

    assert preflight["status"] != "blocked"
    assert preflight["would_execute"] is True
