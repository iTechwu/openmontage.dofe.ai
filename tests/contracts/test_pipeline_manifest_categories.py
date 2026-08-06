"""Contracts for the category vocabulary used by shipped pipelines."""

import jsonschema
import pytest

from lib.pipeline_loader import load_pipeline


def test_documentary_pipeline_uses_a_schema_supported_category() -> None:
    manifest = load_pipeline("documentary-montage")

    assert manifest["category"] == "documentary"


def test_pipeline_loader_rejects_unsafe_stage_codes(tmp_path) -> None:
    (tmp_path / "unsafe.yaml").write_text(
        'name: unsafe\nversion: "1"\nstages:\n  - name: ../../escape\n',
        encoding="utf-8",
    )

    with pytest.raises(jsonschema.ValidationError):
        load_pipeline("unsafe", defs_dir=tmp_path)
