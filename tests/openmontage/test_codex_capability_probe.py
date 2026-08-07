"""Capability probe for tracked external blocker KB-001.

KB-001 (docs/KNOWN_BLOCKERS.md) records that Codex has no per-call model-request
identity, so ``DelegationSigningProxy`` dedups Responses on the content
fingerprint. That is an accepted limitation, NOT a closed feature. These tests
make the blocker non-drifting:

* the audited manifest stays in sync with the pinned Codex version (so a bump
  forces a re-probe and a re-verified blocker status);
* the external tracker is a concrete, test-enforced closed loop (PENDING needs a
  real upstream search URL + next action; FILED needs a real issue URL) and the
  next-review date cannot lapse into a stale green;
* while the capability is absent, the content-fingerprint fallback MUST still be
  present in the proxy (we rely on it);
* when the pinned Codex binary is discoverable, a live schema probe asserts the
  ``ModelProviderInfo`` element count still matches the audited baseline — and
  FAILS if it changes, because a new field may carry per-call identity, forcing a
  manual behavioral re-audit before KB-001 can be touched.

The live schema probe is best-effort in generic CI (Codex may be absent). The
dedicated CI job sets ``OPENMONTAGE_CODEX_PROBE_STRICT=1`` so the probe
FAILS instead of skipping — a missing binary or a changed schema breaks the job,
it does not slip past unnoticed. The manifest invariants run unconditionally.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NoReturn

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_MANIFEST_PATH = PROJECT_ROOT / "docs" / "codex_capability_probe.json"
_MANIFEST = json.loads(_MANIFEST_PATH.read_text())
_DOCKERFILE = (PROJECT_ROOT / "Dockerfile").read_text()


def _strict() -> bool:
    """The dedicated CI job opts in with OPENMONTAGE_CODEX_PROBE_STRICT=1 so the
    live probe fails (not skips) when the pinned Codex binary is missing or the
    schema has changed. Generic CI leaves it unset and the probe skips."""
    return os.environ.get("OPENMONTAGE_CODEX_PROBE_STRICT", "").strip().lower() in {"1", "true"}


def _bail(message: str) -> NoReturn:
    """Fail under the strict gate (dedicated CI), skip otherwise. Always raises."""
    if _strict():
        pytest.fail(message)
    pytest.skip(message)


def _dockerfile_codex_version() -> str:
    match = re.search(
        r"^ARG CODEX_CLI_VERSION=(?P<v>\d+\.\d+\.\d+)\s*$", _DOCKERFILE, re.MULTILINE
    )
    assert match, "Dockerfile must declare ARG CODEX_CLI_VERSION=<semver>"
    return match.group("v")


def test_manifest_tracks_the_pinned_codex_version() -> None:
    """The audited manifest must track exactly the current pin. A bump that does
    not re-probe and update the manifest fails here, forcing a re-verified
    blocker status rather than a silent stale record."""
    from openmontage.delegation_proxy import PINNED_CODEX_CLI_VERSION

    pin = _dockerfile_codex_version()
    assert _MANIFEST["pinned_codex_version"] == pin, (
        f"manifest pinned_codex_version ({_MANIFEST['pinned_codex_version']}) "
        f"must match the Dockerfile CODEX_CLI_VERSION pin ({pin})"
    )
    assert _MANIFEST["pinned_codex_version"] == PINNED_CODEX_CLI_VERSION, (
        "manifest must match delegation_proxy.PINNED_CODEX_CLI_VERSION"
    )
    assert _MANIFEST["blocker_id"] == "KB-001"
    baseline = _MANIFEST["model_provider_info_elements"]
    assert isinstance(baseline, int) and baseline > 0, (
        "manifest must record a positive model_provider_info_elements baseline "
        "(the audited ModelProviderInfo struct size the live schema probe checks)"
    )
    date.fromisoformat(_MANIFEST["next_review_by"])  # parseable ISO date


def test_external_tracker_is_concrete_not_a_placeholder() -> None:
    """The KB-001 external tracker must be a concrete, test-enforced closed loop,
    not a vague 'file if not already' placeholder.

    PENDING is allowed only with a concrete upstream issues-search URL and a
    non-empty next action; FILED requires a real ``openai/codex`` issue URL. The
    next-review date must not have lapsed — once it passes, this fails until
    KB-001 is re-probed and either closed or given a new future date.
    """
    tracker = _MANIFEST["external_tracker"]
    status = tracker["status"]
    assert status in {"PENDING", "FILED"}, f"unknown tracker status {status!r}"
    if status == "FILED":
        filed = tracker["filed_issue"]
        assert isinstance(filed, str) and re.match(
            r"^https://github\.com/openai/codex/issues/\d+$", filed
        ), (
            "FILED tracker needs a concrete issue URL "
            "(https://github.com/openai/codex/issues/<number>)"
        )
    else:  # PENDING
        assert tracker["search"].startswith("https://github.com/openai/codex/issues"), (
            "PENDING tracker needs a concrete upstream issues-search URL, not a homepage"
        )
        assert tracker["filed_issue"] is None, (
            "PENDING tracker must have filed_issue=null; set status=FILED when an issue exists"
        )
        assert tracker["next_action"].strip(), "PENDING tracker needs a non-empty next action"

    next_review = date.fromisoformat(_MANIFEST["next_review_by"])
    today = datetime.now(timezone.utc).date()
    assert today <= next_review, (
        f"KB-001 next_review_by {next_review} has passed (today {today}); re-probe "
        "and either close KB-001 or set a new future date."
    )


def test_fingerprint_fallback_presence_matches_recorded_capability() -> None:
    """While Codex lacks per-call identity (capability_present=false), the
    content-fingerprint replay fallback MUST still exist in the proxy — it is
    the accepted mitigation. If the manifest ever records capability_present=
    true, this fails until the fallback is removed and KB-001 is closed."""
    proxy_src = (PROJECT_ROOT / "openmontage" / "delegation_proxy.py").read_text()
    if _MANIFEST["capability_present"]:
        pytest.fail(
            "KB-001 unblock recorded as present (capability_present=true): remove "
            "the content_fingerprint replay fallback in openmontage/delegation_proxy.py, "
            "key all replay on per-call identity, and close KB-001 in "
            "docs/KNOWN_BLOCKERS.md. This test fails until that cleanup lands."
        )
    assert "content_fingerprint" in proxy_src, (
        "capability_present is false, so the content_fingerprint fallback must "
        "remain in delegation_proxy.py as the KB-001 mitigation"
    )


def _resolve_codex_package_root() -> Path | None:
    """Locate the installed @openai/codex package root from the codex bin, or
    None if Codex is not installed."""
    codex_bin = shutil.which("codex")
    if codex_bin is None:
        return None
    try:
        real = os.path.realpath(codex_bin)
    except OSError:
        return None
    directory = os.path.dirname(real)
    for _ in range(10):
        pkg_json = os.path.join(directory, "package.json")
        if os.path.isfile(pkg_json):
            try:
                data = json.loads(Path(pkg_json).read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            if data.get("name") == "@openai/codex":
                return Path(directory)
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return None


def _find_native_codex_binary(pkg_root: Path) -> Path | None:
    """Find the shipped native codex binary under the platform vendor dir."""
    candidates = sorted(pkg_root.glob("node_modules/@openai/codex-*/vendor/*/bin/codex"))
    return candidates[0] if candidates else None


def test_live_schema_probe_detects_model_provider_info_change() -> None:
    """Live schema-integrity probe. When the pinned Codex binary is discoverable,
    its ``struct ModelProviderInfo with <N> elements`` Debug signature must equal
    the audited baseline. A change means Codex grew/removed a model-provider
    config field — which could carry per-call identity — so the probe FAILS,
    forcing a manual behavioral re-audit before KB-001 can be touched.

    This is a schema change-detector, not a behavioral proof: it cannot show a
    new field varies per call. Per-call proof is the gated behavioral probe in
    the manifest (drive two distinct Codex Responses calls through
    DelegationSigningProxy, diff the per-call headers). The element count is
    exact and zero-false-positive, unlike a named-substring search: field names
    live as contiguous string-table internings (partial, with false neighbors),
    so substring hunting both misses unnamed fields and false-positives on
    unrelated strings.

    Under the strict gate (OPENMONTAGE_CODEX_PROBE_STRICT=1, the dedicated CI
    job) every 'cannot run' condition FAILS the job instead of skipping.
    """
    if shutil.which("strings") is None:
        _bail("`strings` unavailable; schema probe cannot run")

    pkg_root = _resolve_codex_package_root()
    if pkg_root is None:
        _bail("codex CLI not installed; schema probe cannot run")

    installed_version = json.loads((pkg_root / "package.json").read_text()).get("version")
    if installed_version != _MANIFEST["pinned_codex_version"]:
        _bail(
            f"installed codex {installed_version} != pinned "
            f"{_MANIFEST['pinned_codex_version']}; probe applies only to the pin"
        )

    binary = _find_native_codex_binary(pkg_root)
    if binary is None:
        _bail("native codex binary not found under the package; schema probe cannot run")

    result = subprocess.run(
        ["strings", str(binary)], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        _bail(f"`strings` failed on {binary}; schema probe cannot run")

    counts = [
        int(n)
        for n in re.findall(r"ModelProviderInfo with (\d+) elements", result.stdout)
    ]
    baseline = _MANIFEST["model_provider_info_elements"]
    assert counts, (
        "ModelProviderInfo element-count signature is missing from the binary; "
        "the probe can no longer detect a schema change. Re-audit manually."
    )
    assert all(c == baseline for c in counts), (
        f"ModelProviderInfo element count changed: binary reports {sorted(set(counts))}, "
        f"audited baseline is {baseline}. A model-provider config field was added or "
        "removed — it may carry per-call identity. Run the gated behavioral probe "
        "(manifest behavioral_probe): drive two distinct Codex Responses calls through "
        "DelegationSigningProxy and diff the per-call headers. Only if no per-call "
        "identity appears, update model_provider_info_elements and re-record; if one "
        "does, remove the content_fingerprint fallback and close KB-001."
    )
