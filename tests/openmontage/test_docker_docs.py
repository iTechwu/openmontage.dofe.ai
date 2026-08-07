"""Smoke tests for shell commands documented in docs/DOCKER_AND_AGENTS.md.

These guard against the host shell silently expanding a variable that must be
expanded inside the container (e.g. an unloaded .env yielding an empty
Authorization header). They parse the documented command rather than requiring
a running Docker daemon.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_DOC = (PROJECT_ROOT / "docs" / "DOCKER_AND_AGENTS.md").read_text()


def test_container_model_query_defers_env_expansion_to_container() -> None:
    """The container-internal model query must expand $DOFE_MODEL_API_KEY inside
    the container, not on the host.

    The curl is wrapped in ``sh -lc '...'`` with the body single-quoted, so the
    host shell (which may not have sourced .env) cannot pre-expand the key to an
    empty value before ``docker compose exec`` runs.
    """
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
    # The key and the Compose-DNS base URL are expanded by the container shell,
    # never by the host.
    assert "$DOFE_MODEL_API_KEY" in body
    assert "${DOFE_DOCKER_MODEL_BASE_URL:-http://api:3101}" in body


def test_host_model_query_documents_env_loading() -> None:
    """The host-side query instructs loading .env first, so $DOFE_MODEL_API_KEY
    is actually present in the host shell that expands it."""
    assert "set -a; . ./.env; set +a" in _DOC
    # The host curl still reaches a host-reachable endpoint.
    assert "${DOFE_MODEL_BASE_URL:-https://model.local.dofe.ai/api}/v1/models" in _DOC
