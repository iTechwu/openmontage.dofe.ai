"""Shared fixtures for tools tests.

The developer/CI ``.env`` may enable ``DOFE_ENABLED=true``. That is the
production routing policy (DoFe-only model APIs), but the provider tests in
this directory exercise the direct-provider chain that remains available when
the switch is off. Pin the switch off here so results do not depend on the
ambient environment; tests that specifically cover the DoFe policy set or
delete the variable themselves after this fixture runs.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _dofe_switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOFE_ENABLED", "false")
