"""Fail-closed DoFe tool status backed by the authenticated model catalog."""

from __future__ import annotations

import contextvars
from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any

from .client import DofeClient
from .errors import DofeError
from .models import catalog_model_ids, resolve_alias

# Snapshot holder shared across dofe calls inside one ``catalog_snapshot()``
# context. ``None`` = no active snapshot (each call fetches its own catalog).
# A holder dict caches the first ``GET /v1/models`` and any
# ``GET /v1/models/{id}/playground`` capability lookups, so a single video
# selector request issues one catalog call and one capability call instead of
# one per selection/preflight/execution phase.
_CATALOG_SNAPSHOT: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "dofe_catalog_snapshot", default=None
)


def _catalog_client() -> DofeClient:
    return DofeClient(
        connect_timeout=2,
        read_timeout=5,
        max_rate_retries=0,
        max_5xx_retries=0,
        max_network_retries=0,
    )


def resolve_catalog() -> tuple[Any, bool]:
    """Return ``(catalog, ok)``. Reuses the active snapshot; fetches lazily.

    On the first consultation inside a ``catalog_snapshot()`` block this fetches
    once and caches the result; every later consultation in the same context
    reuses it. Outside a snapshot it fetches directly each call. Callers that
    need the catalog (status checks, agent-skill selection) share this entry
    point so whoever runs first inside a snapshot populates it for the rest.
    """
    holder = _CATALOG_SNAPSHOT.get()
    if holder is not None and "catalog" not in holder:
        try:
            holder["catalog"] = _catalog_client().list_models()
        except DofeError:
            holder["catalog"] = None
    if holder is not None:
        catalog = holder.get("catalog")
        return catalog, catalog is not None
    try:
        return _catalog_client().list_models(), True
    except DofeError:
        return None, False


@contextmanager
def catalog_snapshot():
    """Share one GET /v1/models (and capability probes) across all dofe calls.

    The fetch is lazy: nothing is requested until a status check or video
    preflight actually needs it, so wrapping a menu that has no dofe tool costs
    zero calls. Nested snapshots reuse the outer one (no extra fetch).
    """
    if _CATALOG_SNAPSHOT.get() is not None:
        yield
        return
    token = _CATALOG_SNAPSHOT.set({"capabilities": {}})
    try:
        yield
    finally:
        _CATALOG_SNAPSHOT.reset(token)


def resolve_playground_capability(client: DofeClient, model_id: str) -> Any:
    """Return the live playground capability for ``model_id``, reusing snapshot.

    Inside a ``catalog_snapshot()`` block the result is cached per model id so
    selection, preflight, and execution share one ``GET /v1/models/{id}/playground``
    per model. Outside a snapshot the call is made directly.
    """
    holder = _CATALOG_SNAPSHOT.get()
    if holder is not None:
        cached = holder["capabilities"].get(model_id)
        if cached is not None:
            return cached
        capability = client.get_playground_capability(model_id)
        holder["capabilities"][model_id] = capability
        return capability
    return client.get_playground_capability(model_id)


def configured_model_is_visible(
    capability: str,
    operations: Iterable[str | None],
) -> bool:
    """Return true when any configured exact alias is tenant-visible now."""

    configured = {
        alias
        for operation in operations
        if (alias := resolve_alias(capability, operation))
    }
    if not configured:
        return False
    catalog, ok = resolve_catalog()
    if not ok or catalog is None:
        return False
    return bool(configured & set(catalog_model_ids(catalog)))
