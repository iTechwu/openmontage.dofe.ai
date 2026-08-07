"""Capability probe for tracked external blocker KB-001.

KB-001 (docs/KNOWN_BLOCKERS.md) records that Codex has no per-call model-request
identity, so ``DelegationSigningProxy`` dedups Responses on the content
fingerprint. That is an accepted limitation, NOT a closed feature. These tests
make the blocker non-drifting:

* the audited manifest stays in sync with the pinned Codex version (so a bump
  forces a re-probe and a re-verified blocker status);
* while the capability is absent, the content-fingerprint fallback MUST still be
  present in the proxy (we rely on it);
* when the pinned Codex binary is discoverable, a live probe re-asserts that no
  unblock signal has appeared — and FAILS if one has, demanding the fingerprint
  fallback be removed and KB-001 closed.

The live probe is best-effort (generic CI may not have Codex installed); the
manifest invariants run unconditionally.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_MANIFEST_PATH = PROJECT_ROOT / "docs" / "codex_capability_probe.json"
_MANIFEST = json.loads(_MANIFEST_PATH.read_text())
_DOCKERFILE = (PROJECT_ROOT / "Dockerfile").read_text()


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
    assert _MANIFEST["unblock_signals"], (
        "manifest must record at least one audited unblock signal for the probe"
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


def test_live_probe_fails_when_codex_gains_per_call_identity() -> None:
    """Live capability probe. When the pinned Codex binary is discoverable, it
    must NOT carry any audited unblock signal. If it does, the content-fingerprint
    fallback is the wrong design and this fails loudly until KB-001 is closed.

    Skipped (not failed) only when Codex is not installed or is a different
    version than the pin: the manifest invariants above still enforce the
    audited state unconditionally.
    """
    if shutil.which("strings") is None:
        pytest.skip("`strings` not available; manifest invariants still enforced")

    pkg_root = _resolve_codex_package_root()
    if pkg_root is None:
        pytest.skip("codex CLI not installed; manifest invariants still enforced")

    installed_version = json.loads((pkg_root / "package.json").read_text()).get("version")
    if installed_version != _MANIFEST["pinned_codex_version"]:
        pytest.skip(
            f"installed codex {installed_version} != pinned "
            f"{_MANIFEST['pinned_codex_version']}; probe applies only to the pin"
        )

    binary = _find_native_codex_binary(pkg_root)
    if binary is None:
        pytest.skip("native codex binary not found under the package; manifest enforced")

    result = subprocess.run(
        ["strings", str(binary)], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        pytest.skip(f"`strings` failed on {binary}; manifest invariants still enforced")

    haystack = result.stdout.lower()
    exclusions = {e.lower() for e in _MANIFEST.get("unblock_signal_exclusions", [])}
    gained = []
    for signal in _MANIFEST["unblock_signals"]:
        token = signal.lower()
        if token in haystack and token not in exclusions:
            gained.append(signal)

    assert not gained, (
        f"codex {installed_version} now carries per-call identity signal(s) "
        f"{gained} (excluded tokens {sorted(exclusions)}). The content-fingerprint "
        "replay fallback in openmontage/delegation_proxy.py is no longer the right "
        "design: remove it, key all replay on per-call identity, set "
        "capability_present=true is NOT sufficient — close KB-001 in "
        "docs/KNOWN_BLOCKERS.md and update docs/codex_capability_probe.json after "
        "the fallback is gone."
    )
