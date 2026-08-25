#!/usr/bin/env python3
"""Periodic housekeeping for the OpenMontage file-server mirror.

The workspace agent cannot read ``/data/projects``, so OpenMontage mirrors project
analysis files into the shared exchange directory served by the ``mcp-file-server``
(``/data/mcp-exchange/openmontage``). Those mirrors are disposable — the project under
``/data/projects`` remains authoritative — so they must be pruned periodically so the
exchange directory does not grow without bound.

Run this on a schedule (host cron or a container timer). It only touches the mirror;
the authoritative project is never modified.

Defaults are read from the environment:
  OPENMONTAGE_EXPORT_DIR            mirror target (default /data/mcp-exchange/openmontage)
  OPENMONTAGE_EXPORT_MAX_AGE_DAYS   prune files not touched in N days (default 7)
  OPENMONTAGE_EXPORT_MAX_BYTES      evict oldest when the mirror exceeds this (default 20 GiB)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from openmontage.exchange import ProjectFileExporter  # noqa: E402


def main() -> int:
    exporter = ProjectFileExporter(
        export_dir=os.environ.get("OPENMONTAGE_EXPORT_DIR", "/data/mcp-exchange/openmontage"),
        base_url=os.environ.get("OPENMONTAGE_FILE_SERVER_BASE_URL", "http://host.docker.internal:18090"),
    )
    if not exporter.enabled:
        print("OpenMontage file-server exporter is not enabled; nothing to clean.")
        return 0
    result = exporter.cleanup(
        max_age_days=float(os.environ.get("OPENMONTAGE_EXPORT_MAX_AGE_DAYS", "7")),
        max_bytes=int(os.environ.get("OPENMONTAGE_EXPORT_MAX_BYTES", str(20 * 1024**3))),
    )
    print(f"exchange cleanup: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
