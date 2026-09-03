"""On-demand export of OpenMontage project files through the shared file-server.

OpenMontage's project directory (``/data/projects/<id>/...``) lives in the CI
container namespace and is not mounted into the desktop Agent. The exchange
directory is therefore only a CI-side delivery mirror. Desktop visual
inspection uses the authenticated ``read_project_image`` MCP tool, which
returns native image content; clients must never resolve the mirror's
``/exchange`` path locally.

The exporter mirrors files with a small margin but never over-copies, and keeps
the mirror healthy with periodic cleanup:

* ``export_analysis`` mirrors a project's whole analysis set (artifacts, keyframes,
  scenes, transcript, briefs, manifest) in one go — everything the agent inspects —
  while leaving large media files uncopied.
* ``export`` mirrors a single file (or directory) on demand; media is skipped unless
  ``include_media=true``.
* ``cleanup`` prunes stale mirrors (by age) and evicts the oldest files when the
  mirror exceeds a size budget, so the exchange directory does not grow without bound.
  It only touches the mirror; the authoritative project under ``/data/projects`` is
  never modified.

Configuration (the docker/CI deployment sets these; local dev leaves them unset,
in which case the exporter is disabled and container paths are returned as-is):

  OPENMONTAGE_EXPORT_DIR            container mirror target, e.g. /data/mcp-exchange/openmontage
  OPENMONTAGE_FILE_SERVER_BASE_URL  CI-internal file-server base, e.g. http://127.0.0.1:18090
  OPENMONTAGE_EXPORT_PREFIX         URL path segment under the base; default is the
                                    last component of OPENMONTAGE_EXPORT_DIR ("openmontage")
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from lib.paths import PROJECTS_DIR

_PROJECT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}[a-zA-Z0-9]$|^[a-zA-Z0-9]$")

# Harness mount point for the shared exchange directory.
_HARNESS_EXCHANGE_PATH = os.environ.get("OPENMONTAGE_HARNESS_EXCHANGE_PATH", "/exchange")

# Media extensions that are large and only worth mirroring when explicitly asked.
_MEDIA_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wav", ".mp3", ".m4a", ".flac"})
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


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


def _image_media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


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
    """Mirror project files into the shared file-server, with a small margin.

    Mirroring is *need-driven but not stingy*: when a project is prepared (or the
    agent asks to sync it), the whole *analysis* set is mirrored in one go —
    artifacts, keyframes, scenes, transcript, briefs — but large media files are
    left alone unless the agent explicitly requests one. That mirrors the outputs
    the agent actually inspects while avoiding an unbounded copy of every video.

    Disk is kept healthy through ``cleanup``, which prunes stale mirrors (by age)
    and caps the mirror size (oldest-first eviction), so the exchange directory
    does not grow without bound.
    """

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

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ProjectFileExportError(
                "The file-server exporter is not configured; set OPENMONTAGE_EXPORT_DIR "
                "and OPENMONTAGE_FILE_SERVER_BASE_URL to enable it"
            )

    def _mirror(self, source: Path, destination: Path, *, include_media: bool) -> int:
        """Copy a file/dir into the destination, honoring the media policy.

        Returns the number of bytes mirrored. ``include_media=False`` skips media
        files inside a directory (but keeps the directory structure + analysis files).
        """
        if source.is_file():
            if _is_media(source) and not include_media:
                raise ProjectFileExportError(
                    f"{source.name} is a media file; pass include_media=true to mirror it"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            return source.stat().st_size
        if destination.exists():
            shutil.rmtree(destination)
        if include_media:
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copytree(source, destination, symlinks=True, ignore=_ignore_media)
        return _path_size(source, include_media=include_media)

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
        """Mirror one file (or a whole directory) into the exchange and return references.

        ``include_media=False`` (default) skips large media files so the default path
        mirrors only the small analysis outputs. Set ``include_media=True`` to mirror
        media files too (e.g. the reference video when the video is the deliverable).

        ``host_path``/``file_path`` (``/exchange/...``) is only a CI DSH shared-mount
        reference. Remote clients should use ``read_project_file`` for text; the
        loopback ``url`` is CI-internal.
        """
        self._require_enabled()
        project_dir = _project_dir(self.projects_root, project_id)
        rel = _safe_relative(relative_path)
        source = (project_dir / rel).resolve()
        if not str(source).startswith(str(project_dir) + os.sep) and source != project_dir:
            raise ProjectFileExportError("relative_path escaped the project directory")
        if not source.exists():
            raise ProjectFileExportError(f"Not found in project {project_id}: {relative_path}")

        destination = (self._export_project_dir(project_id) / rel).resolve()
        copied_size = self._mirror(source, destination, include_media=include_media)
        host_path = f"{self._root_host_path(project_id)}/{rel}"
        return {
            "project_id": project_id,
            "relative_path": str(rel),
            "size_bytes": copied_size,
            "is_media": _is_media(source),
            "file_path": host_path,
            "host_path": host_path,
            "url": f"{self._root_public_url(project_id)}/{rel}",
        }

    def read_text(self, project_id: str, relative_path: str, *, max_bytes: int = 2_000_000) -> dict[str, Any]:
        """Read a bounded UTF-8 project file through the MCP response."""
        if max_bytes <= 0 or max_bytes > 10_000_000:
            raise ProjectFileExportError("max_bytes must be between 1 and 10000000")
        project_dir = _project_dir(self.projects_root, project_id)
        rel = _safe_relative(relative_path)
        source = (project_dir / rel).resolve()
        if not str(source).startswith(str(project_dir) + os.sep):
            raise ProjectFileExportError("relative_path escaped the project directory")
        if not source.is_file():
            raise ProjectFileExportError(f"Not a file in project {project_id}: {relative_path}")
        if _is_media(source):
            raise ProjectFileExportError("Media files must be delivered with export_project_file(include_media=true)")
        size_bytes = source.stat().st_size
        if size_bytes > max_bytes:
            raise ProjectFileExportError(
                f"File is {size_bytes} bytes; reduce max_bytes or use export_project_file"
            )
        try:
            content = source.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectFileExportError("Only UTF-8 text files can be read through MCP") from exc
        return {
            "project_id": project_id,
            "relative_path": str(rel),
            "size_bytes": size_bytes,
            "content": content,
        }

    def read_image_bytes(
        self,
        project_id: str,
        relative_path: str,
        *,
        max_bytes: int = 4_000_000,
    ) -> tuple[str, bytes, str]:
        """Read one project image for native MCP image content.

        The bytes stay inside the authenticated OpenMontage MCP call.  This is
        intentionally separate from ``export``: a desktop Agent cannot resolve
        the CI-only ``/exchange`` mount, and returning that path creates a
        misleading local-file contract.
        """
        if max_bytes <= 0 or max_bytes > 10_000_000:
            raise ProjectFileExportError("max_bytes must be between 1 and 10000000")
        project_dir = _project_dir(self.projects_root, project_id)
        rel = _safe_relative(relative_path)
        if rel.suffix.lower() not in _IMAGE_EXTENSIONS:
            raise ProjectFileExportError(
                "Only PNG, JPEG, WebP, and GIF images can be read through MCP"
            )
        source = (project_dir / rel).resolve()
        if not str(source).startswith(str(project_dir) + os.sep):
            raise ProjectFileExportError("relative_path escaped the project directory")
        if not source.is_file():
            raise ProjectFileExportError(f"Not an image file in project {project_id}: {relative_path}")
        size_bytes = source.stat().st_size
        if size_bytes > max_bytes:
            raise ProjectFileExportError(
                f"Image is {size_bytes} bytes; reduce max_bytes or use a smaller keyframe"
            )
        data = source.read_bytes()
        media_type = _image_media_type(data)
        if media_type is None:
            raise ProjectFileExportError(
                "Image bytes are not a valid PNG, JPEG, WebP, or GIF"
            )
        return str(rel), data, media_type

    def export_analysis(self, project_id: str) -> dict[str, Any]:
        """Mirror the whole project analysis set, skipping large media files.

        This is the "need-plus-margin" copy: it mirrors the artifacts, keyframes,
        scenes, transcript, briefs and project manifest — everything the agent
        inspects — but leaves any media file (e.g. the reference video) uncopied.

        ``export_file_path``/``export_host_path`` (``/exchange/...``) is the reference the
        DSH GUI can open; ``export_root_url`` is only for the agent's own fetch on CI.
        """
        self._require_enabled()
        project_dir = _project_dir(self.projects_root, project_id)
        if not project_dir.is_dir():
            raise ProjectFileExportError(f"Project not found: {project_id}")
        destination = self._export_project_dir(project_id)
        copied_size = self._mirror(project_dir, destination, include_media=False)
        mirrored = self._mirrored_files(project_id)
        host_path = self._root_host_path(project_id)
        return {
            "project_id": project_id,
            "export_file_path": host_path,
            "export_host_path": host_path,
            "export_root_url": self._root_public_url(project_id),
            "size_bytes": copied_size,
            "mirrored_files": mirrored,
        }

    def _mirrored_files(self, project_id: str) -> list[dict[str, Any]]:
        """List files already mirrored for a project under the exchange dir."""
        destination = self._export_project_dir(project_id)
        if not destination.is_dir():
            return []
        files: list[dict[str, Any]] = []
        for root, _dirs, names in os.walk(destination):
            for name in names:
                path = Path(root) / name
                rel = path.relative_to(destination)
                files.append(
                    {
                        "relative_path": str(rel),
                        "size_bytes": path.stat().st_size,
                        "is_media": _is_media(path),
                    }
                )
        files.sort(key=lambda item: item["relative_path"])
        return files

    def mirror_size(self, project_id: str | None = None) -> int:
        """Total bytes currently mirrored under the exchange dir."""
        base = self._project_scope(project_id) if project_id else self.export_dir
        if not base.exists():
            return 0
        total = 0
        for root, _dirs, names in os.walk(base):
            for name in names:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
        return total

    def cleanup(
        self,
        *,
        project_id: str | None = None,
        max_age_days: float | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Prune stale/over-budget mirrors to keep the exchange directory healthy.

        ``max_age_days`` removes files not modified within the window. ``max_bytes``
        caps the total mirror size and evicts the oldest files first when exceeded.
        Empty directories are removed afterwards. This runs on the *mirror* only; the
        authoritative project under ``/data/projects`` is never touched.
        """
        base = self._project_scope(project_id) if project_id else self.export_dir
        if not base.exists():
            return {"removed_count": 0, "removed_bytes": 0, "remaining_bytes": 0}

        now = time.time()
        cutoff = now - (max_age_days * 86400.0) if max_age_days else now
        entries: list[tuple[float, int, Path]] = []  # (mtime, size, path) for files
        for root, _dirs, names in os.walk(base):
            for name in names:
                path = Path(root) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, path))

        removed_bytes = 0
        remaining = [e for e in entries]
        if max_age_days is not None:
            removed, keep = [], []
            for (mtime, size, path) in entries:
                (removed if mtime < cutoff else keep).append((mtime, size, path))
            for (_mtime, _size, path) in removed:
                try:
                    removed_bytes += _size
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            remaining = keep

        if max_bytes is not None and sum(size for _m, size, _p in remaining) > max_bytes:
            total = sum(size for _m, size, _p in remaining)
            # evict oldest first until under budget
            for (_mtime, size, path) in sorted(remaining, key=lambda item: item[0]):
                if total <= max_bytes:
                    break
                try:
                    total -= size
                    removed_bytes += size
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

        # remove empty dirs (bottom-up)
        for root, _dirs, _names in os.walk(base, topdown=False):
            try:
                os.rmdir(root)
            except OSError:
                pass
        return {
            "removed_count": 0,
            "removed_bytes": removed_bytes,
            "remaining_bytes": self.mirror_size(project_id=project_id),
        }

    def _project_scope(self, project_id: str) -> Path:
        return (self.export_dir / project_id).resolve()


def from_environment(*, projects_root: str | Path | None = None) -> ProjectFileExporter:
    """Build an exporter from the process environment."""
    if projects_root is None:
        projects_root = os.environ.get("OPENMONTAGE_PROJECTS_DIR") or PROJECTS_DIR
    return ProjectFileExporter(projects_root=projects_root)
