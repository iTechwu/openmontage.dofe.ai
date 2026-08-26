"""Shared fixtures for the openmontage test suite.

``JobService.create_job`` fails closed unless a runnable external agent
executor is configured (see ``delegated_executor_availability``). Most tests
exercise the Job state machine rather than executor availability, so default
the suite to a guaranteed-real executable; tests that care about availability
monkeypatch the environment explicitly.
"""

from __future__ import annotations

import json
import sys

import pytest


@pytest.fixture(autouse=True)
def _available_agent_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "OPENMONTAGE_AGENT_EXECUTOR_JSON",
        json.dumps([sys.executable, "-c", "pass"]),
    )
