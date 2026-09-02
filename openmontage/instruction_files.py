"""Read-only access to OpenMontage instruction files (Markdown / YAML / JSON).

The client Agent drives each pipeline stage from instructions that live in the
CI repository — pipeline manifests, stage director skills, meta skills and JSON
schemas. This module serves those files over MCP: every read resolves against
the live repository on CI (no client cache, no logical skill-ID mapping), is
bounded in size, and can never leave the repository root.

Only a fixed set of instruction roots is exposed (see ``ALLOWED_ROOTS``), and
only text formats an agent needs (``.md`` / ``.yaml`` / ``.yml`` / ``.json``).
Code, credentials, databases and media are rejected by extension, by root
allow-list, and by the repository boundary check — in that order of defense.

The interface is strictly read-only: it opens files with ``open(..., "r")`` and
provides no create / modify / delete / copy operation.
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.paths import REPO_ROOT

DEFAULT_MAX_BYTES = 2_000_000
MAX_BYTES_LIMIT = 10_000_000

ALLOWED_EXTENSIONS = frozenset({".md", ".yaml", ".yml", ".json"})

# Repository roots a client Agent may read instructions from. Anything else —
# even a readable .md elsewhere in the repo — is rejected so the exposure
# surface stays explicit and auditable.
ALLOWED_ROOTS = (
    "AGENT_GUIDE.md",
    "pipeline_defs",
    "skills",
    ".agents/skills",
    "schemas",
    "styles",
    "remotion-composer/public",
    "docs",
)


class InstructionFileError(RuntimeError):
    """Raised when an instruction file cannot be served.

    Carries a stable machine-readable ``code`` so the client Agent can
    distinguish a missing file (``INSTRUCTION_FILE_NOT_FOUND``) from a policy
    rejection (``UNSUPPORTED_FILE_TYPE`` / ``PATH_OUTSIDE_REPOSITORY``) and an
    unreadable file (``INSTRUCTION_FILE_UNAVAILABLE``). The code is also
    embedded in the message because the MCP SDK serializes tool exceptions as
    plain text — ``str(error)`` must contain the code for the client to branch
    on it.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _repo_root(repo_root: str | Path | None) -> Path:
    return Path(repo_root).expanduser().resolve() if repo_root is not None else REPO_ROOT


def _repository_revision(repo_root: str) -> str:
    """Best-effort ``git rev-parse HEAD`` for provenance; empty when unavailable.

    Deliberately uncached: the interface promises a live read of the CI
    repository, and a stale revision would corrupt instruction provenance
    recorded against it. The call costs ~10ms.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _resolve_within_repo(raw_path: str, root: Path) -> Path:
    """Resolve ``raw_path`` to a real file inside ``root`` or raise.

    Accepts repo-relative paths and absolute paths that already point inside
    the repository. Rejects empty/NUL input, ``..`` traversal, absolute paths
    outside the repo, and symlinks whose target escapes the repo.
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise InstructionFileError("INVALID_PATH", "path must be a non-empty string")
    if "\x00" in raw_path:
        raise InstructionFileError("INVALID_PATH", "path must not contain NUL bytes")

    candidate = Path(raw_path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        if ".." in candidate.parts:
            raise InstructionFileError(
                "PATH_OUTSIDE_REPOSITORY",
                "path must not traverse outside the repository ('..')",
            )
        resolved = (root / candidate).resolve()

    if resolved != root and root not in resolved.parents:
        raise InstructionFileError(
            "PATH_OUTSIDE_REPOSITORY",
            f"path resolves outside the OpenMontage repository: {raw_path}",
        )
    return resolved


def _relative_instruction_path(resolved: Path, root: Path) -> str:
    relative = resolved.relative_to(root)
    parts = relative.parts
    if not parts:
        raise InstructionFileError("INVALID_PATH", "path must name a file, not the repository root")
    # Allowed-root check: the first path segment(s) must match an allow-listed
    # root exactly (".agents/skills" is a two-segment root).
    for allowed in ALLOWED_ROOTS:
        allowed_parts = Path(allowed).parts
        if parts[: len(allowed_parts)] == allowed_parts:
            if len(parts) == len(allowed_parts) and resolved.is_dir():
                # e.g. the bare directory "skills" itself
                raise InstructionFileError(
                    "INSTRUCTION_FILE_NOT_FOUND",
                    f"path is a directory, not a file: {relative}",
                )
            return str(relative)
    raise InstructionFileError(
        "PATH_OUTSIDE_REPOSITORY",
        f"path is outside the allowed instruction roots {list(ALLOWED_ROOTS)}: {relative}",
    )


def read_instruction_file(
    path: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Read one instruction file from the live CI repository.

    Returns the actual server path, repo-relative path, UTF-8 content, size,
    modification time, a SHA-256 content hash, and the repository revision so
    the client can record instruction provenance without caching the file.
    """
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= MAX_BYTES_LIMIT:
        raise InstructionFileError(
            "INVALID_PATH",
            f"max_bytes must be an integer between 1 and {MAX_BYTES_LIMIT}",
        )

    root = _repo_root(repo_root)
    resolved = _resolve_within_repo(path, root)

    if resolved.is_dir():
        raise InstructionFileError(
            "INSTRUCTION_FILE_NOT_FOUND",
            f"path is a directory, not a file: {path}",
        )
    if resolved.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise InstructionFileError(
            "UNSUPPORTED_FILE_TYPE",
            f"only {sorted(ALLOWED_EXTENSIONS)} instruction files are readable: {path}",
        )
    relative_path = _relative_instruction_path(resolved, root)

    if not resolved.is_file():
        raise InstructionFileError(
            "INSTRUCTION_FILE_NOT_FOUND",
            f"instruction file not found: {relative_path}",
        )

    try:
        # Read at most max_bytes+1 bytes in a single call: the oversize
        # decision and the content come from the same read, so a growing file
        # cannot slip past the limit between a stat() and a later read.
        with open(resolved, "rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise InstructionFileError(
            "INSTRUCTION_FILE_UNAVAILABLE",
            f"instruction file cannot be read: {relative_path} ({exc})",
        ) from exc
    if len(data) > max_bytes:
        raise InstructionFileError(
            "FILE_TOO_LARGE",
            f"instruction file exceeds the {max_bytes} byte limit: {relative_path}",
        )
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstructionFileError(
            "INSTRUCTION_FILE_UNAVAILABLE",
            f"instruction file is not valid UTF-8: {relative_path}",
        ) from exc

    modified_at = datetime.fromtimestamp(resolved.stat().st_mtime, tz=timezone.utc)
    return {
        "path": str(resolved),
        "relative_path": relative_path,
        "content": content,
        "size": len(data),
        "modified_at": modified_at.isoformat().replace("+00:00", "Z"),
        # Hash the exact bytes served so the client can verify provenance
        # against the same payload it received (no newline normalization).
        "content_hash": f"sha256:{hashlib.sha256(data).hexdigest()}",
        "repository_revision": _repository_revision(str(root)),
    }


def verify_instruction_provenance(
    provenance: Any,
    *,
    repo_root: str | Path | None = None,
) -> list[dict[str, str]]:
    """Validate a client-submitted instruction-provenance list.

    Each entry must be ``{"path": <repo-relative path>, "content_hash":
    "sha256:..."}`` and match the file currently on CI — proving the client
    read the live instructions it claims to have followed. Returns the
    normalized entries; raises ``InstructionFileError`` on any mismatch.
    """
    if not isinstance(provenance, list):
        raise InstructionFileError(
            "INVALID_PROVENANCE", "instruction_provenance must be a list of {path, content_hash}"
        )
    normalized: list[dict[str, str]] = []
    for entry in provenance:
        if not isinstance(entry, dict):
            raise InstructionFileError(
                "INVALID_PROVENANCE", "each provenance entry must be an object"
            )
        entry_path = entry.get("path")
        entry_hash = entry.get("content_hash")
        if not isinstance(entry_path, str) or not isinstance(entry_hash, str):
            raise InstructionFileError(
                "INVALID_PROVENANCE",
                "each provenance entry needs string 'path' and 'content_hash'",
            )
        served = read_instruction_file(entry_path, repo_root=repo_root)
        if served["content_hash"] != entry_hash:
            raise InstructionFileError(
                "PROVENANCE_STALE",
                f"instruction file changed since the client read it: {entry_path}; "
                "re-read it and resubmit",
            )
        normalized.append({"path": served["relative_path"], "content_hash": entry_hash})
    return normalized
