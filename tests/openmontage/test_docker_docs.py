"""Smoke tests for shell commands documented in docs/DOCKER_AND_AGENTS.md.

These guard against the host shell silently expanding a variable that must be
expanded inside the container (e.g. an unloaded .env yielding an empty
Authorization header), and against the container command reading a variable the
Compose file never injects. They parse the documented command and the Compose
file rather than requiring a running Docker daemon.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_DOC = (PROJECT_ROOT / "docs" / "DOCKER_AND_AGENTS.md").read_text()
_COMPOSE = (PROJECT_ROOT / "compose.yaml").read_text()


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
