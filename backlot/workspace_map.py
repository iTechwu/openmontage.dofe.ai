"""Read-only job→workspace resolution for Backlot (docs/0903 §4).

The OpenMontage Job Service atomically exports a job→workspace manifest to
``<projects>/.openmontage/workspace-map.json``. Backlot mounts the projects
volume read-only and never opens the job database, so this manifest is the
only tenant-binding source available to the web board. A missing or corrupt
manifest resolves to no workspace at all (fail closed): unbound project
directories stay invisible to authenticated sessions.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

# Must match openmontage.job_service.WORKSPACE_MAP_NAME; kept in sync by a
# dedicated test rather than a runtime import (Backlot stays decoupled from
# the openmontage package).
WORKSPACE_MAP_NAME = "workspace-map.json"


class WorkspaceMap:
    """mtime-cached reader for the atomic job→workspace manifest."""

    def __init__(self, projects_root: Path):
        self._path = Path(projects_root) / ".openmontage" / WORKSPACE_MAP_NAME
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._mapping: dict[str, str] = {}

    def workspace_of(self, project_id: str) -> str | None:
        """Return the owning workspace of a project, or None when unbound."""
        return self._refresh().get(project_id)

    def projects_for(self, workspace_id: str | None) -> set[str]:
        """Return every project id bound to a workspace (empty when None)."""
        if not workspace_id:
            return set()
        return {
            project_id
            for project_id, workspace in self._refresh().items()
            if workspace == workspace_id
        }

    def _refresh(self) -> dict[str, str]:
        with self._lock:
            try:
                mtime = self._path.stat().st_mtime
            except OSError:
                self._mtime = None
                self._mapping = {}
                return self._mapping
            if mtime != self._mtime:
                try:
                    loaded = json.loads(self._path.read_text(encoding="utf-8"))
                    # JSON object keys are always strings; only keep entries
                    # whose workspace value is a string too.
                    self._mapping = {
                        str(key): value
                        for key, value in loaded.items()
                        if isinstance(value, str)
                    }
                except (OSError, ValueError):
                    self._mapping = {}
                self._mtime = mtime
            return self._mapping
