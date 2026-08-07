"""Fail-closed DoFe tool status backed by the authenticated model catalog."""

from __future__ import annotations

from collections.abc import Iterable

from .client import DofeClient
from .errors import DofeError
from .models import catalog_model_ids, resolve_alias


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
    try:
        catalog = DofeClient(
            connect_timeout=2,
            read_timeout=5,
            max_rate_retries=0,
            max_5xx_retries=0,
            max_network_retries=0,
        ).list_models()
    except DofeError:
        return False
    return bool(configured & set(catalog_model_ids(catalog)))
