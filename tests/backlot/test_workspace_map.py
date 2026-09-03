from __future__ import annotations

import json
from pathlib import Path

from backlot.workspace_map import WORKSPACE_MAP_NAME, WorkspaceMap


def _write_map(root: Path, mapping: dict) -> Path:
    dot = root / ".openmontage"
    dot.mkdir(parents=True, exist_ok=True)
    path = dot / WORKSPACE_MAP_NAME
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return path


def test_workspace_map_name_matches_job_service() -> None:
    from openmontage.job_service import WORKSPACE_MAP_NAME as SERVICE_NAME

    assert WORKSPACE_MAP_NAME == SERVICE_NAME


def test_workspace_of_resolves_bound_projects(tmp_path: Path) -> None:
    _write_map(tmp_path, {"film": "tenant:a", "promo": "tenant:b"})
    workspace_map = WorkspaceMap(tmp_path)

    assert workspace_map.workspace_of("film") == "tenant:a"
    assert workspace_map.workspace_of("promo") == "tenant:b"
    assert workspace_map.workspace_of("missing") is None


def test_missing_manifest_fails_closed(tmp_path: Path) -> None:
    workspace_map = WorkspaceMap(tmp_path)

    assert workspace_map.workspace_of("film") is None
    assert workspace_map.projects_for("tenant:a") == set()


def test_corrupt_manifest_fails_closed(tmp_path: Path) -> None:
    dot = tmp_path / ".openmontage"
    dot.mkdir()
    (dot / WORKSPACE_MAP_NAME).write_text("{corrupt", encoding="utf-8")
    workspace_map = WorkspaceMap(tmp_path)

    assert workspace_map.workspace_of("film") is None


def test_projects_for_scopes_to_workspace(tmp_path: Path) -> None:
    _write_map(
        tmp_path,
        {"a1": "tenant:a", "a2": "tenant:a", "b1": "tenant:b"},
    )
    workspace_map = WorkspaceMap(tmp_path)

    assert workspace_map.projects_for("tenant:a") == {"a1", "a2"}
    assert workspace_map.projects_for("tenant:b") == {"b1"}
    assert workspace_map.projects_for(None) == set()


def test_manifest_rewrite_is_picked_up(tmp_path: Path) -> None:
    workspace_map = WorkspaceMap(tmp_path)
    assert workspace_map.workspace_of("film") is None

    _write_map(tmp_path, {"film": "tenant:a"})

    assert workspace_map.workspace_of("film") == "tenant:a"


def test_non_string_values_are_ignored(tmp_path: Path) -> None:
    _write_map(tmp_path, {"film": "tenant:a", "bad": 3, "worse": None})
    workspace_map = WorkspaceMap(tmp_path)

    assert workspace_map.workspace_of("film") == "tenant:a"
    assert workspace_map.workspace_of("bad") is None
    assert workspace_map.workspace_of("worse") is None
