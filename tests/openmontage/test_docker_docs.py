"""Smoke tests for shell commands documented in docs/DOCKER_AND_AGENTS.md.

These guard against the host shell silently expanding a variable that must be
expanded inside the container (e.g. an unloaded .env yielding an empty
Authorization header), and against the container command reading a variable the
Compose file never injects. They parse the documented command and the Compose
file rather than requiring a running Docker daemon.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_DOC = (PROJECT_ROOT / "docs" / "DOCKER_AND_AGENTS.md").read_text()
_COMPOSE = (PROJECT_ROOT / "compose.yaml").read_text()
_DOCKERFILE = (PROJECT_ROOT / "Dockerfile").read_text()


def _container_query_body() -> str:
    exec_lines = [
        line
        for line in _DOC.splitlines()
        if "docker compose exec" in line and "v1/models" in line
    ]
    assert exec_lines, "container model-query command is missing from the docs"
    line = exec_lines[0]
    assert "sh -lc '" in line, "container curl must run under a container login shell"
    body = line.split("sh -lc '", 1)[1]
    assert body.endswith("'"), "container curl body must be single-quoted as one unit"
    return body


def test_container_model_query_defers_env_expansion_to_container() -> None:
    """The container-internal model query must expand $DOFE_MODEL_API_KEY inside
    the container, not on the host.

    The curl is wrapped in ``sh -lc '...'`` with the body single-quoted, so the
    host shell (which may not have sourced .env) cannot pre-expand the key to an
    empty value before ``docker compose exec`` runs.
    """
    body = _container_query_body()
    assert "$DOFE_MODEL_API_KEY" in body  # expanded in-container, not on host


def test_container_model_query_reads_compose_injected_var() -> None:
    """The container query must read the base-URL var Compose actually injects.

    compose.yaml injects ``DOFE_MODEL_BASE_URL`` (resolved from the host's
    ``DOFE_DOCKER_MODEL_BASE_URL``) into the container; the host-only
    ``DOFE_DOCKER_MODEL_BASE_URL`` is NOT in the container env. Reading the wrong
    var would silently fall back to the default and ignore a custom address.
    """
    match = re.search(
        r"^\s*(DOFE_MODEL_BASE_URL)\s*:\s*\$\{DOFE_DOCKER_MODEL_BASE_URL[^}]*\}",
        _COMPOSE,
        re.MULTILINE,
    )
    assert match, (
        "compose.yaml must inject DOFE_MODEL_BASE_URL from "
        "DOFE_DOCKER_MODEL_BASE_URL into the container environment"
    )
    injected_var = match.group(1)

    body = _container_query_body()
    assert f"${{{injected_var}" in body, (
        f"container query must read the Compose-injected ${{{injected_var}}}, "
        "not the host-only DOFE_DOCKER_MODEL_BASE_URL"
    )
    assert "DOFE_DOCKER_MODEL_BASE_URL" not in body, (
        "DOFE_DOCKER_MODEL_BASE_URL is host-only and is not injected into the "
        "container; reading it there always hits the default"
    )


def test_host_model_query_documents_env_loading() -> None:
    """The host-side query instructs loading .env first, so $DOFE_MODEL_API_KEY
    is actually present in the host shell that expands it."""
    assert "set -a; . ./.env; set +a" in _DOC
    # The host curl still reaches a host-reachable endpoint.
    assert "${DOFE_MODEL_BASE_URL:-https://model.local.dofe.ai/api}/v1/models" in _DOC


def test_codex_version_pin_is_the_single_source() -> None:
    """The Codex CLI pin must be identical wherever it is referenced.

    The canonical pin is the Dockerfile ``CODEX_CLI_VERSION`` build arg; the
    delegation proxy's ``PINNED_CODEX_CLI_VERSION`` constant is the code-side
    source the external-blocker analysis is verified against, and the docs state
    the same revision in prose. A bump in any one without the others is a silent
    drift — and the blocker tracking is only correct while these agree, so the
    test fails until all three (and the blocker re-verification) move together.
    """
    dockerfile_match = re.search(
        r"^ARG CODEX_CLI_VERSION=(?P<v>\d+\.\d+\.\d+)\s*$",
        _DOCKERFILE,
        re.MULTILINE,
    )
    assert dockerfile_match, "Dockerfile must declare ARG CODEX_CLI_VERSION=<semver>"
    dockerfile_version = dockerfile_match.group("v")

    from openmontage.delegation_proxy import PINNED_CODEX_CLI_VERSION

    assert PINNED_CODEX_CLI_VERSION == dockerfile_version, (
        f"delegation_proxy.PINNED_CODEX_CLI_VERSION ({PINNED_CODEX_CLI_VERSION}) "
        f"must match Dockerfile CODEX_CLI_VERSION ({dockerfile_version}); bumping "
        "the pin must re-verify the per-call-identity blocker claim."
    )

    doc_versions = set(re.findall(r"codex-cli\s+(?P<v>\d+\.\d+\.\d+)", _DOC))
    assert doc_versions, "DOCKER_AND_AGENTS.md must state the codex-cli pin version"
    assert doc_versions == {dockerfile_version}, (
        f"docs codex-cli version(s) {doc_versions} must match the Dockerfile pin "
        f"({dockerfile_version})"
    )
    # KB-001 records the revision the blocker was first verified against; that
    # literal must move with the pin or the tracking goes stale.
    known_blockers = (PROJECT_ROOT / "docs" / "KNOWN_BLOCKERS.md").read_text()
    assert f"codex-cli {dockerfile_version}" in known_blockers, (
        f"KNOWN_BLOCKERS.md must reference the pinned codex-cli {dockerfile_version} "
        "(KB-001 First verified revision)"
    )
    # The proxy module body must not re-literal the version outside the constant,
    # so the constant is the sole in-file source.
    proxy_src = (PROJECT_ROOT / "openmontage" / "delegation_proxy.py").read_text()
    literal_occurrences = re.findall(rf"\b{re.escape(dockerfile_version)}\b", proxy_src)
    assert literal_occurrences == [dockerfile_version], (
        "delegation_proxy.py must contain the pinned Codex version exactly once "
        "(in PINNED_CODEX_CLI_VERSION); comments must reference the constant by "
        f"name, not re-literal it. Found {len(literal_occurrences)} occurrence(s)."
    )


def test_known_blockers_next_review_matches_manifest() -> None:
    """KB-001's Next review date in KNOWN_BLOCKERS.md must equal the manifest's
    next_review_by. The manifest value is enforced as not-past-due by
    test_external_tracker_is_concrete_not_a_placeholder; this test keeps the
    human-readable doc from drifting out of sync with that enforced value, so the
    date the reviewer reads is the date the test guards."""
    known_blockers = (PROJECT_ROOT / "docs" / "KNOWN_BLOCKERS.md").read_text()
    manifest = json.loads(
        (PROJECT_ROOT / "docs" / "codex_capability_probe.json").read_text()
    )
    md_match = re.search(r"no later than (?P<d>\d{4}-\d{2}-\d{2})", known_blockers)
    assert md_match, (
        "KNOWN_BLOCKERS.md must state a parseable 'no later than YYYY-MM-DD' "
        "next-review date"
    )
    assert md_match.group("d") == manifest["next_review_by"], (
        f"KNOWN_BLOCKERS.md next-review {md_match.group('d')!r} must equal the "
        f"manifest next_review_by {manifest['next_review_by']!r}"
    )
