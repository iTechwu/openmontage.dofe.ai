"""Capability probe for tracked external blocker KB-001.

KB-001 (docs/KNOWN_BLOCKERS.md) records that Codex has no per-call model-request
identity, so ``DelegationSigningProxy`` keys Responses replay on the content
fingerprint as the durable cross-instance key. The same-instance wrong-merge is
MITIGATED in OpenMontage by a per-instance distinct-call guard; what remains
open is the external capability (a native Codex per-call identity) that would
let that guard be removed. These tests make the blocker non-drifting:

* the audited manifest stays in sync with the pinned Codex version (so a bump
  forces a re-probe and a re-verified blocker status);
* the external tracker is a concrete, test-enforced closed loop: PENDING must be
  ready-to-file (a real upstream search URL + next action + a verbatim
  issue_draft), FILED needs a real issue URL; and the review deadline cannot
  lapse into a stale green NOR be pushed more than a quarter past the last probe
  (extending it requires re-probing, which advances probed_at);
* a CI workflow wiring test guarantees the behavioral probe actually runs against
  the pinned Codex under the strict gate, so the evidence path cannot quietly
  regress to skip-on-green drift;
* while the capability is absent, the content-fingerprint fallback MUST still be
  present in the proxy (we rely on it);
* when the pinned Codex binary is discoverable, a live BEHAVIORAL probe runs the
  real codex binary against a mock Responses upstream and observes the actual
  request surface (header names + body field names). It FAILS if that surface
  differs from the audited baseline — a new identity-carrying header/field
  demands a manual audit before KB-001 can be touched.

The behavioral probe is the real evidence: it sees request-layer headers a
static struct-size check is blind to (a request-level Idempotency-Key would not
change any struct's element count). It runs only under
``OPENMONTAGE_CODEX_PROBE_STRICT=1`` (the dedicated CI job that installs the
pinned Codex); generic CI without Codex skips it, while the manifest invariants
run unconditionally.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import NoReturn

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_MANIFEST_PATH = PROJECT_ROOT / "docs" / "codex_capability_probe.json"
_MANIFEST = json.loads(_MANIFEST_PATH.read_text())
_DOCKERFILE = (PROJECT_ROOT / "Dockerfile").read_text()

# A minimal valid Responses SSE stream: a created then completed response with a
# single output_text message. Enough for codex exec to accept the reply and exit
# cleanly after one model call, which is all the surface probe needs to observe.
_MINIMAL_RESPONSES_SSE = (
    b'event: response.created\n'
    b'data: {"type":"response.created","response":{"id":"resp_probe","object":"response","status":"in_progress","model":"probe","output":[],"created_at":0}}\n\n'
    b'event: response.completed\n'
    b'data: {"type":"response.completed","response":{"id":"resp_probe","object":"response","status":"completed","model":"probe","output":[{"type":"message","id":"msg_1","status":"completed","role":"assistant","content":[{"type":"output_text","text":"ok"}]}],"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2},"created_at":0}}\n\n'
)


def _strict() -> bool:
    """The dedicated CI job opts in with OPENMONTAGE_CODEX_PROBE_STRICT=1 so the
    live behavioral probe fails (not skips) when the pinned Codex binary is
    missing or the observed request surface has changed. Generic CI leaves it
    unset and the probe skips."""
    return os.environ.get("OPENMONTAGE_CODEX_PROBE_STRICT", "").strip().lower() in {"1", "true"}


def _bail(message: str) -> NoReturn:
    """Fail under the strict gate (dedicated CI), skip otherwise. Always raises."""
    if _strict():
        pytest.fail(message)
    pytest.skip(message)


def test_manifest_tracks_the_pinned_codex_version() -> None:
    """The audited manifest must track exactly the current pin and carry the
    behavioral baseline the live probe checks against. A bump that does not
    re-probe and update the manifest fails here, forcing a re-verified blocker
    status rather than a silent stale record."""
    from openmontage.delegation_proxy import PINNED_CODEX_CLI_VERSION

    assert "codex" not in _DOCKERFILE.lower(), (
        "the Docker image is MCP-only and must not reference the Codex CLI; "
        "the pin lives in delegation_proxy.PINNED_CODEX_CLI_VERSION"
    )
    assert _MANIFEST["pinned_codex_version"] == PINNED_CODEX_CLI_VERSION, (
        f"manifest pinned_codex_version ({_MANIFEST['pinned_codex_version']}) "
        f"must match delegation_proxy.PINNED_CODEX_CLI_VERSION "
        f"({PINNED_CODEX_CLI_VERSION})"
    )
    assert _MANIFEST["blocker_id"] == "KB-001"
    baseline = _MANIFEST["behavioral_probe_baseline"]
    assert isinstance(baseline.get("request_header_names"), list) and baseline["request_header_names"], (
        "manifest must record a non-empty behavioral_probe_baseline.request_header_names"
    )
    assert isinstance(baseline.get("request_body_fields"), list) and baseline["request_body_fields"], (
        "manifest must record a non-empty behavioral_probe_baseline.request_body_fields"
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
        # PENDING must be ready-to-file, not a vague intent: a concrete issue
        # draft (title + body) the owner can paste upstream verbatim. This keeps
        # "still PENDING" honest — it is one action away from FILED at all times.
        draft = tracker.get("issue_draft")
        assert isinstance(draft, dict), (
            "PENDING tracker needs an issue_draft (ready-to-file title + body)"
        )
        assert draft.get("title", "").strip(), (
            "issue_draft.title must be non-empty and file-ready"
        )
        assert draft.get("body", "").strip(), (
            "issue_draft.body must be non-empty and file-ready"
        )

    next_review = date.fromisoformat(_MANIFEST["next_review_by"])
    today = datetime.now(timezone.utc).date()
    assert today <= next_review, (
        f"KB-001 next_review_by {next_review} has passed (today {today}); re-probe "
        "and either close KB-001 or set a new future date."
    )


def test_review_deadline_window_bounds_probe_recency() -> None:
    """The next_review_by deadline cannot be pushed more than one quarter (95
    days) past probed_at, and probed_at cannot be in the future.

    This closes the 'review deadline can be formally extended' gap: a maintainer
    cannot keep the tracker perpetually green by bumping next_review_by forward
    each time it nears expiry. Extending the window requires advancing
    probed_at — i.e. actually re-running the behavioral probe — and the strict
    CI job re-verifies the baseline against the (re)installed pinned binary, so a
    version bump that did not undergo a real behavioral review cannot hide behind
    a fresh deadline."""
    probed_at = date.fromisoformat(_MANIFEST["probed_at"])
    next_review = date.fromisoformat(_MANIFEST["next_review_by"])
    today = datetime.now(timezone.utc).date()
    assert probed_at <= today, (
        f"probed_at {probed_at} is in the future (today {today}); a probe cannot "
        "be dated ahead of when it ran"
    )
    assert probed_at <= next_review, (
        f"next_review_by {next_review} is before probed_at {probed_at}"
    )
    window = next_review - probed_at
    assert window <= timedelta(days=95), (
        f"next_review_by is {window.days} days after probed_at (max 95). To extend "
        "the review deadline, re-run the behavioral probe and advance probed_at — "
        "do not just push next_review_by forward."
    )


def test_ci_wires_the_strict_capability_probe() -> None:
    """The behavioral probe is real evidence ONLY while a CI job runs it against
    the pinned Codex under the strict gate. Without Codex the probe silently
    skips, so a changed request surface or stale pin would pass green.

    This test pins that wiring in place: the workflow must install the
    pinned @openai/codex, set OPENMONTAGE_CODEX_PROBE_STRICT=1, and
    run this probe module. Dropping the job or the strict flag would return the
    probe to skip-on-green drift, so this fails until the wiring is restored —
    making the behavioral-evidence path a durable, tested invariant rather than a
    one-time setup that can quietly regress."""
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "@openai/codex@" in workflow, (
        "ci.yml must install the pinned @openai/codex via npm install -g @openai/codex@<pin>"
    )
    assert "PINNED_CODEX_CLI_VERSION" in workflow and "${{ steps.pin.outputs.version }}" in workflow, (
        "ci.yml must read the pin from openmontage/delegation_proxy.py "
        "(PINNED_CODEX_CLI_VERSION) and install that exact version, so a bump flows "
        "through automatically instead of drifting"
    )
    assert 'OPENMONTAGE_CODEX_PROBE_STRICT: "1"' in workflow, (
        "ci.yml must set OPENMONTAGE_CODEX_PROBE_STRICT: \"1\" so the probe FAILS (not "
        "skips) on a missing binary, an installed-vs-pinned version mismatch, or a "
        "request surface that differs from the audited baseline"
    )
    assert "tests/openmontage/test_codex_capability_probe.py" in workflow, (
        "ci.yml must run the capability probe test module in the strict job"
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


def _start_recording_upstream(captured: list[dict]) -> ThreadingHTTPServer:
    """Start a loopback mock OpenAI Responses upstream that records each
    request's header names and body field names into ``captured``."""

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else b""
            body_keys: list[str] = []
            if body:
                try:
                    body_keys = list(json.loads(body).keys())
                except (ValueError, UnicodeDecodeError):
                    body_keys = []
            captured.append(
                {
                    "path": self.path,
                    "header_names": [k.lower() for k in self.headers.keys()],
                    "body_fields": body_keys,
                }
            )
            if self.path.rstrip("/").endswith("/responses"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(_MINIMAL_RESPONSES_SSE)
                self.wfile.flush()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[]}')
                self.wfile.flush()

        def do_POST(self) -> None:  # noqa: N802
            self._handle()

        def do_GET(self) -> None:  # noqa: N802
            self._handle()

        def log_message(self, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def _wait_port_ready(port: int, deadline: float) -> None:
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.25)
            try:
                sock.connect(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.05)
    raise RuntimeError("mock upstream did not become ready")


def test_live_behavioral_probe_observes_request_surface() -> None:
    """Live behavioral probe. Runs the pinned codex binary non-interactively
    against a mock Responses upstream and observes the ACTUAL request surface
    (header names + body field names) Codex sends on /responses. It must equal
    the audited baseline. A new or removed name fails the probe — a new
    identity-carrying header (e.g. Idempotency-Key) or request field demands a
    manual audit before KB-001 can be touched.

    Why behavioral, not struct element-count: a request-level identity header
    never changes ModelProviderInfo's element count, so element-count is blind to
    the exact unblock signal; element-count also trips on unrelated struct
    changes and misses add+delete pairs. Observing the real wire surface has none
    of those failure modes.

    Under the strict gate (OPENMONTAGE_CODEX_PROBE_STRICT=1, the dedicated CI
    job) every 'cannot run' condition FAILS the job instead of skipping. stdin is
    closed (DEVNULL) so codex exec does not block waiting for piped input.
    """
    pkg_root = _resolve_codex_package_root()
    if pkg_root is None:
        _bail("codex CLI not installed; behavioral probe cannot run")
    installed_version = json.loads((pkg_root / "package.json").read_text()).get("version")
    if installed_version != _MANIFEST["pinned_codex_version"]:
        _bail(
            f"installed codex {installed_version} != pinned "
            f"{_MANIFEST['pinned_codex_version']}; probe applies only to the pin"
        )
    binary = _find_native_codex_binary(pkg_root)
    if binary is None:
        _bail("native codex binary not found under the package; behavioral probe cannot run")

    captured: list[dict] = []
    server = _start_recording_upstream(captured)
    port = server.server_address[1]
    try:
        _wait_port_ready(port, time.monotonic() + 5.0)
        env = {**os.environ, "PROBE_KEY": "dummy"}
        proc = subprocess.Popen(
            [
                str(binary),
                "exec",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "--ephemeral",
                "-c", 'model_providers.probe.name="probe"',
                "-c", f"model_providers.probe.base_url=\"http://127.0.0.1:{port}\"",
                "-c", 'model_providers.probe.wire_api="responses"',
                "-c", 'model_providers.probe.env_key="PROBE_KEY"',
                "-c", "model_providers.probe.requires_openai_auth=false",
                "-c", 'model_provider="probe"',
                "-m",
                "probe-model",
                "say ok",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            _bail("codex exec did not finish within 60s; behavioral probe cannot run")
    finally:
        server.shutdown()
        server.server_close()

    requests = [r for r in captured if "/responses" in r["path"]]
    if not requests:
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        _bail(
            "behavioral probe captured no /responses request "
            f"(codex exit {proc.returncode}; stderr: {stderr[:400]!r})"
        )

    transport = {"host", "content-length"}
    observed_headers = sorted(
        {name for req in requests for name in req["header_names"] if name not in transport}
    )
    observed_fields = sorted({f for req in requests for f in req["body_fields"]})
    baseline = _MANIFEST["behavioral_probe_baseline"]
    expected_headers = sorted(baseline["request_header_names"])
    expected_fields = sorted(baseline["request_body_fields"])

    assert observed_headers == expected_headers, (
        "Codex /responses request header-name set changed.\n"
        f"  expected: {expected_headers}\n"
        f"  observed: {observed_headers}\n"
        f"  added:   {sorted(set(observed_headers) - set(expected_headers))}\n"
        f"  removed: {sorted(set(expected_headers) - set(observed_headers))}\n"
        "A new header may carry per-call identity. Run the manual per-call-variation "
        "audit; if it does (e.g. Idempotency-Key), remove the content_fingerprint "
        "fallback in openmontage/delegation_proxy.py and close KB-001; if it does not, "
        "update behavioral_probe_baseline.request_header_names after auditing."
    )
    assert observed_fields == expected_fields, (
        "Codex /responses request body field-name set changed.\n"
        f"  expected: {expected_fields}\n"
        f"  observed: {observed_fields}\n"
        f"  added:   {sorted(set(observed_fields) - set(expected_fields))}\n"
        f"  removed: {sorted(set(expected_fields) - set(observed_fields))}\n"
        "A new field may carry per-call identity. Audit as above; update "
        "behavioral_probe_baseline.request_body_fields only after confirming it is not."
    )
