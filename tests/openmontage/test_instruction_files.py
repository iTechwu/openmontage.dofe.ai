"""Tests for the read-only instruction file interface (plan §5, §16.1)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from openmontage.instruction_files import (
    ALLOWED_EXTENSIONS,
    DEFAULT_MAX_BYTES,
    InstructionFileError,
    read_instruction_file,
    verify_instruction_provenance,
)

REPO = Path(__file__).resolve().parents[2]


def _code(exc_info: pytest.ExceptionInfo[InstructionFileError]) -> str:
    return exc_info.value.code


# --- Happy path: the four allowed formats, served from the real repo -------


def test_reads_markdown_from_repo_root_file() -> None:
    result = read_instruction_file("AGENT_GUIDE.md")

    assert result["relative_path"] == "AGENT_GUIDE.md"
    assert result["path"] == str((REPO / "AGENT_GUIDE.md").resolve())
    assert len(result["content"]) > 0
    assert result["size"] == (REPO / "AGENT_GUIDE.md").stat().st_size
    assert result["content_hash"] == "sha256:" + hashlib.sha256(
        (REPO / "AGENT_GUIDE.md").read_bytes()
    ).hexdigest()
    assert result["modified_at"].endswith("Z")


def test_reads_yaml_pipeline_manifest() -> None:
    result = read_instruction_file("pipeline_defs/framework-smoke.yaml")
    assert result["relative_path"] == "pipeline_defs/framework-smoke.yaml"
    assert "framework-smoke" in result["content"]


def test_reads_yml_extension(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    target = root / "pipeline_defs"
    target.mkdir(parents=True)
    (target / "demo.yml").write_text("name: demo\n", encoding="utf-8")

    result = read_instruction_file("pipeline_defs/demo.yml", repo_root=root)
    assert result["content"] == "name: demo\n"


def test_reads_json_schemas_and_docs() -> None:
    schema = read_instruction_file("schemas/checkpoints/checkpoint.schema.json")
    assert json.loads(schema["content"])

    artifact_schema = read_instruction_file("schemas/artifacts/scene_plan.schema.json")
    assert json.loads(artifact_schema["content"])


def test_reads_nested_agent_skill() -> None:
    result = read_instruction_file(".agents/skills/seedance-prompting/SKILL.md")
    assert result["relative_path"].startswith(".agents/skills/")


def test_reads_remotion_public_demo_props() -> None:
    demo_dir = REPO / "remotion-composer" / "public" / "demo-props"
    if not demo_dir.is_dir():
        pytest.skip("no demo-props fixtures in this checkout")
    sample = next(demo_dir.glob("*.json"))
    result = read_instruction_file(f"remotion-composer/public/demo-props/{sample.name}")
    assert json.loads(result["content"]) is not None


def test_absolute_path_inside_repo_is_accepted() -> None:
    absolute = str((REPO / "pipeline_defs" / "framework-smoke.yaml").resolve())
    result = read_instruction_file(absolute)
    assert result["relative_path"] == "pipeline_defs/framework-smoke.yaml"


def test_repository_revision_is_present_in_git_checkout() -> None:
    result = read_instruction_file("AGENT_GUIDE.md")
    assert isinstance(result["repository_revision"], str)
    # In a git checkout this is the 40-char HEAD hash; empty only when git is
    # unavailable (best effort).
    assert result["repository_revision"] == "" or len(result["repository_revision"]) == 40


# --- Rejections ------------------------------------------------------------


@pytest.mark.parametrize("bad_path", ["", "   ", "skills/\x00secret.md"])
def test_rejects_empty_and_nul_paths(bad_path: str) -> None:
    with pytest.raises(InstructionFileError) as exc_info:
        read_instruction_file(bad_path)
    assert _code(exc_info) == "INVALID_PATH"


@pytest.mark.parametrize(
    "bad_path",
    [
        "../outside.md",
        "skills/../../etc/passwd.md",
        "docs/../../../tmp/x.json",
    ],
)
def test_rejects_path_traversal(bad_path: str) -> None:
    with pytest.raises(InstructionFileError) as exc_info:
        read_instruction_file(bad_path)
    assert _code(exc_info) == "PATH_OUTSIDE_REPOSITORY"


def test_rejects_absolute_path_outside_repo(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(InstructionFileError) as exc_info:
        read_instruction_file(str(outside))
    assert _code(exc_info) == "PATH_OUTSIDE_REPOSITORY"


def test_rejects_symlink_escaping_repo(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    outside = tmp_path / "secret.md"
    outside.write_text("top secret", encoding="utf-8")
    (root / "docs" / "link.md").symlink_to(outside)

    with pytest.raises(InstructionFileError) as exc_info:
        read_instruction_file("docs/link.md", repo_root=root)
    assert _code(exc_info) == "PATH_OUTSIDE_REPOSITORY"


def test_allows_symlink_staying_inside_repo(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "real.md").write_text("real content", encoding="utf-8")
    (root / "docs" / "alias.md").symlink_to(root / "docs" / "real.md")

    result = read_instruction_file("docs/alias.md", repo_root=root)
    assert result["content"] == "real content"
    assert result["relative_path"] == "docs/real.md"


@pytest.mark.parametrize(
    "name",
    ["tools/base_tool.py", "skills/pipelines/animation/run.sh", "docs/data.db"],
)
def test_rejects_code_and_database_extensions(name: str) -> None:
    # Extension policy fires regardless of whether the file exists.
    with pytest.raises(InstructionFileError) as exc_info:
        read_instruction_file(name)
    assert _code(exc_info) == "UNSUPPORTED_FILE_TYPE"


def test_rejects_media_extension() -> None:
    with pytest.raises(InstructionFileError) as exc_info:
        read_instruction_file("docs/demo.mp4")
    assert _code(exc_info) == "UNSUPPORTED_FILE_TYPE"


def test_rejects_readable_extension_outside_allowed_roots() -> None:
    # README.md is a real .md file in the repo but not under an allowed root.
    with pytest.raises(InstructionFileError) as exc_info:
        read_instruction_file("README.md")
    assert _code(exc_info) == "PATH_OUTSIDE_REPOSITORY"


def test_missing_file_returns_not_found() -> None:
    with pytest.raises(InstructionFileError) as exc_info:
        read_instruction_file("skills/pipelines/animation/no-such-director.md")
    assert _code(exc_info) == "INSTRUCTION_FILE_NOT_FOUND"


def test_directory_returns_not_found() -> None:
    with pytest.raises(InstructionFileError) as exc_info:
        read_instruction_file("skills")
    assert _code(exc_info) == "INSTRUCTION_FILE_NOT_FOUND"


def test_rejects_oversized_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    big = root / "docs" / "big.md"
    big.write_text("x" * 4096, encoding="utf-8")

    with pytest.raises(InstructionFileError) as exc_info:
        read_instruction_file("docs/big.md", max_bytes=1024, repo_root=root)
    assert _code(exc_info) == "FILE_TOO_LARGE"


def test_default_limit_is_two_megabytes() -> None:
    assert DEFAULT_MAX_BYTES == 2_000_000


def test_rejects_invalid_max_bytes() -> None:
    for bad in (0, -1, 10_000_001, 1.5, True):
        with pytest.raises(InstructionFileError) as exc_info:
            read_instruction_file("AGENT_GUIDE.md", max_bytes=bad)  # type: ignore[arg-type]
        assert _code(exc_info) == "INVALID_PATH"


def test_rejects_non_utf8_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "binary.md").write_bytes(b"\xff\xfe\x00\x01")

    with pytest.raises(InstructionFileError) as exc_info:
        read_instruction_file("docs/binary.md", repo_root=root)
    assert _code(exc_info) == "INSTRUCTION_FILE_UNAVAILABLE"


# --- Read-only guarantee ----------------------------------------------------


def test_reading_does_not_modify_the_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    target = root / "docs" / "note.md"
    target.write_text("immutable", encoding="utf-8")
    before = target.stat()

    read_instruction_file("docs/note.md", repo_root=root)

    after = target.stat()
    assert target.read_text(encoding="utf-8") == "immutable"
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_size == before.st_size
    # No sibling artifacts (.tmp / copies) are created by a read.
    assert sorted(p.name for p in (root / "docs").iterdir()) == ["note.md"]


# --- Provenance verification -------------------------------------------------


def test_provenance_roundtrip() -> None:
    served = read_instruction_file("AGENT_GUIDE.md")
    entries = verify_instruction_provenance(
        [{"path": "AGENT_GUIDE.md", "content_hash": served["content_hash"]}]
    )
    assert entries == [
        {"path": "AGENT_GUIDE.md", "content_hash": served["content_hash"]}
    ]


def test_provenance_rejects_stale_hash() -> None:
    with pytest.raises(InstructionFileError) as exc_info:
        verify_instruction_provenance(
            [{"path": "AGENT_GUIDE.md", "content_hash": "sha256:" + "0" * 64}]
        )
    assert _code(exc_info) == "PROVENANCE_STALE"


def test_provenance_rejects_malformed_entries() -> None:
    for bad in (
        "not-a-list",
        ["not-a-dict"],
        [{"path": "AGENT_GUIDE.md"}],
        [{"path": 1, "content_hash": "sha256:x"}],
    ):
        with pytest.raises(InstructionFileError) as exc_info:
            verify_instruction_provenance(bad)  # type: ignore[arg-type]
        assert _code(exc_info) == "INVALID_PROVENANCE"


def test_provenance_rejects_disallowed_file() -> None:
    with pytest.raises(InstructionFileError) as exc_info:
        verify_instruction_provenance(
            [{"path": "tools/base_tool.py", "content_hash": "sha256:" + "0" * 64}]
        )
    assert _code(exc_info) == "UNSUPPORTED_FILE_TYPE"


# --- MCP surface -------------------------------------------------------------


def test_mcp_server_exposes_read_openmontage_file() -> None:
    mcp = pytest.importorskip("mcp")
    import asyncio

    from openmontage.mcp_server import create_server

    server = create_server()
    tool_names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert "read_openmontage_file" in tool_names


def test_allowed_extensions_are_exactly_the_four_formats() -> None:
    assert ALLOWED_EXTENSIONS == frozenset({".md", ".yaml", ".yml", ".json"})
