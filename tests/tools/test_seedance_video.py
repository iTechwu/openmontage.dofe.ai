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
    monkeypatch.delenv("DOFE_ENABLED", raising=False)
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
    monkeypatch.delenv("DOFE_ENABLED", raising=False)
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
    """A valid text_to_video request honestly degrades (no live probe), not blocked.

    Seedance exposes no side-effect-free live contract probe, so a valid request
    reports ``degraded`` — never ``passed`` (which would imply a verified live
    entitlement). Samples may proceed in this state; batches may not.
    """
    monkeypatch.setenv("FAL_KEY", "test-fal-key")

    preflight = SeedanceVideo().preflight(
        {"prompt": "a cat on Mars", "operation": "text_to_video"},
        live=True,
    )

    assert preflight["status"] == "degraded"
    assert preflight["would_execute"] is True


def test_seedance_direct_call_fail_closed_when_dofe_enabled(monkeypatch) -> None:
    """A direct Seedance call never bypasses the DoFe fail-closed route.

    When DOFE_ENABLED=true the selector forces video generation through the
    unified Airouter. A direct execute() bypasses that route and the catalog, so
    it fail-closes before any key lookup or paid fal.ai POST — even with FAL_KEY
    set.
    """
    monkeypatch.setenv("DOFE_ENABLED", "true")
    monkeypatch.setenv("FAL_KEY", "test-fal-key")

    submitted: list[str] = []
    monkeypatch.setattr(
        _requests,
        "post",
        lambda *args, **kwargs: submitted.append(args[0] if args else kwargs.get("url")),
    )

    result = SeedanceVideo().execute({"prompt": "a cat on Mars"})

    assert not result.success
    assert "DOFE_ENABLED" in result.error
    assert submitted == []  # no paid fal.ai POST


def test_seedance_batch_degraded_preflight_blocks_paid_post(monkeypatch) -> None:
    """A direct batch call with a degraded (unverified) preflight is fail-closed.

    Seedance has no live probe, so a valid request degrades. The selector blocks
    a degraded batch unless allow_degraded_preflight=true; a direct execute()
    must enforce the same gate so it cannot reach a paid fal.ai POST on an
    unverified contract.
    """
    monkeypatch.setenv("FAL_KEY", "test-fal-key")
    monkeypatch.delenv("DOFE_ENABLED", raising=False)

    submitted: list[str] = []
    monkeypatch.setattr(
        _requests,
        "post",
        lambda *args, **kwargs: submitted.append(args[0] if args else kwargs.get("url")),
    )

    result = SeedanceVideo().execute(
        {"prompt": "a cat on Mars", "execution_scope": "batch"}
    )

    assert not result.success
    assert result.data["provider_preflight"]["status"] == "degraded"
    assert "allow_degraded_preflight" in result.error
    assert submitted == []  # no paid fal.ai POST
