"""On-demand export of OpenMontage project files through the shared file-server.

The workspace agent (DSH harness) cannot read OpenMontage's project directory
(``/data/projects/<id>/...``): it lives in the OpenMontage container namespace
and is not mounted into the harness. To let the agent fetch generated files we
mirror them *on demand* into the shared exchange directory that the
``mcp-file-server`` already serves (host ``/data/mcp-exchange`` ->
``http://host.docker.internal:18090``, and ``/exchange`` inside the harness).

The exporter is deliberately **lazy and disk-conscious**:

* Nothing is copied until a specific file or directory is requested.
* Copies happen one path at a time, so a 100+ MB media file is only mirrored
  when the caller explicitly asks for it. The small analysis outputs (brief,
  keyframes, scenes, transcript, request JSON) are a few KB each.

Configuration (the docker/CI deployment sets these; local dev leaves them unset,
in which case the exporter is disabled and container paths are returned as-is):

  OPENMONTAGE_EXPORT_DIR            container mirror target, e.g. /data/mcp-exchange/openmontage
  OPENMONTAGE_FILE_SERVER_BASE_URL  public file-server base, e.g. http://host.docker.internal:18090
  OPENMONTAGE_EXPORT_PREFIX         URL path segment under the base; default is the
                                    last component of OPENMONTAGE_EXPORT_DIR ("openmontage")
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from lib.paths import PROJECTS_DIR

_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")

# Harness mount point for the shared exchange directory.
_HARNESS_EXCHANGE_PATH = os.environ.get("OPENMONTAGE_HARNESS_EXCHANGE_PATH", "/exchange")

# Media extensions that are large and only worth mirroring when explicitly asked.
_MEDIA_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wav", ".mp3", ".m4a", ".flac"})


class ProjectFileExportError(RuntimeError):
    """Raised when a project file cannot be listed or exported safely."""


def _project_dir(projects_root: Path, project_id: str) -> Path:
    normalized = project_id.strip().lower()
    if not _PROJECT_ID_RE.fullmatch(normalized):
        raise ProjectFileExportError(
            "project_id must be 1-64 lowercase letters, numbers, or hyphens"
        )
    root = projects_root.expanduser().resolve()
    project_dir = (root / normalized).resolve()
    if project_dir.parent != root:
        raise ProjectFileExportError("Resolved project path escaped the projects directory")
    return project_dir


def _safe_relative(relative_path: str) -> Path:
    rel = Path(relative_path)
    if not rel.parts or rel == Path("."):
        raise ProjectFileExportError("relative_path must be a non-empty path within the project")
    if rel.is_absolute():
        raise ProjectFileExportError("relative_path must be a path relative to the project")
    if ".." in rel.parts:
        raise ProjectFileExportError("relative_path must not traverse outside the project")
    return rel


def _is_media(path: Path) -> bool:
    return path.suffix.lower() in _MEDIA_EXTENSIONS


def _ignore_media(dir_path: str, names: list[str]) -> set[str]:
    """copytree ignore callback that skips media files (keeps directories)."""
    skipped: set[str] = set()
    base = Path(dir_path)
    for name in names:
        candidate = base / name
        if candidate.is_file() and _is_media(candidate):
            skipped.add(name)
    return skipped


def _path_size(path: Path, *, include_media: bool = True) -> int:
    if path.is_file():
        if not include_media and _is_media(path):
            return 0
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            file_path = Path(root) / name
            if not include_media and _is_media(file_path):
                continue
            try:
                total += file_path.stat().st_size
            except OSError:
                pass
    return total


class ProjectFileExporter:
    """Mirror project files into the shared file-server on demand."""

    def __init__(
        self,
        *,
        projects_root: str | Path | None = None,
        export_dir: str | Path | None = None,
        base_url: str | None = None,
        prefix: str | None = None,
    ) -> None:
        self.projects_root = Path(projects_root or PROJECTS_DIR).expanduser().resolve()
        self.export_dir = Path(export_dir or os.environ.get("OPENMONTAGE_EXPORT_DIR", "")).expanduser()
        self.base_url = (base_url if base_url is not None else os.environ.get("OPENMONTAGE_FILE_SERVER_BASE_URL", "")).strip().rstrip("/")
        self.prefix = (prefix if prefix is not None else os.environ.get("OPENMONTAGE_EXPORT_PREFIX", "")) or (
            self.export_dir.name if self.export_dir.name else "openmontage"
        )

    @property
    def enabled(self) -> bool:
        return bool(self.export_dir.name and self.base_url)

    def _root_public_url(self, project_id: str) -> str:
        return f"{self.base_url}/{self.prefix}/{project_id}"

    def _root_host_path(self, project_id: str) -> str:
        return f"{_HARNESS_EXCHANGE_PATH}/{self.prefix}/{project_id}"

    def _export_project_dir(self, project_id: str) -> Path:
        return self.export_dir / project_id

    def root(self, project_id: str) -> dict[str, Any]:
        """Public URL + harness host path for a project's export root."""
        return {
            "project_id": project_id,
            "url": self._root_public_url(project_id),
            "host_path": self._root_host_path(project_id),
        }

    def list(self, project_id: str) -> dict[str, Any]:
        """Inventory the files generated for a project (relative paths + sizes).

        This only reads directory metadata; nothing is copied.
        """
        project_dir = _project_dir(self.projects_root, project_id)
        if not project_dir.is_dir():
            raise ProjectFileExportError(f"Project not found: {project_id}")
        files: list[dict[str, Any]] = []
        for root, _dirs, names in os.walk(project_dir):
            for name in names:
                path = Path(root) / name
                rel = path.relative_to(project_dir)
                files.append(
                    {
                        "relative_path": str(rel),
                        "size_bytes": path.stat().st_size,
                        "is_media": _is_media(path),
                    }
                )
        files.sort(key=lambda item: item["relative_path"])
        return {
            "project_id": project_id,
            "exports": self.root(project_id) if self.enabled else {"enabled": False},
            "files": files,
        }

    def export(self, project_id: str, relative_path: str, *, include_media: bool = False) -> dict[str, Any]:
        """Mirror one file (or a whole directory) into the exchange and return URLs.

        ``include_media=False`` (default) skips large media files so the default path
        mirrors only the small analysis outputs. Set ``include_media=True`` to mirror
        media files too (e.g. the reference video when the video is the deliverable).
        """
        if not self.enabled:
            raise ProjectFileExportError(
                "The file-server exporter is not configured; set OPENMONTAGE_EXPORT_DIR "
                "and OPENMONTAGE_FILE_SERVER_BASE_URL to enable it"
            )
        project_dir = _project_dir(self.projects_root, project_id)
        rel = _safe_relative(relative_path)
        source = (project_dir / rel).resolve()
        project_root_str = str(project_dir) + os.sep
        if not str(source).startswith(project_root_str) and source != project_dir:
            raise ProjectFileExportError("relative_path escaped the project directory")
        if not source.exists():
            raise ProjectFileExportError(f"Not found in project {project_id}: {relative_path}")

        destination = (self._export_project_dir(project_id) / rel).resolve()
        if source.is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            if include_media:
                shutil.copytree(source, destination, symlinks=True)
            else:
                shutil.copytree(source, destination, symlinks=True, ignore=_ignore_media)
            copied_size = _path_size(source, include_media=include_media)
        else:
            if _is_media(source) and not include_media:
                raise ProjectFileExportError(
                    f"{relative_path} is a media file; pass include_media=true to mirror it"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied_size = source.stat().st_size
        return {
            "project_id": project_id,
            "relative_path": str(rel),
            "size_bytes": copied_size,
            "is_media": _is_media(source),
            "url": f"{self._root_public_url(project_id)}/{rel}",
            "host_path": f"{self._root_host_path(project_id)}/{rel}",
        }


def from_environment(*, projects_root: str | Path | None = None) -> ProjectFileExporter:
    """Build an exporter from the process environment."""
    if projects_root is None:
        projects_root = os.environ.get("OPENMONTAGE_PROJECTS_DIR") or PROJECTS_DIR
    return ProjectFileExporter(projects_root=projects_root)
