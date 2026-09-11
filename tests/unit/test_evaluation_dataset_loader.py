"""Tests for JsonlManifestEvaluationDatasetLoader (Slice A)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from book_graph_rag.domain.evaluation_models import (
    DatasetLoadError,
    DatasetManifestMismatch,
    UnknownDatasetError,
)
from book_graph_rag.infrastructure.evaluation_dataset_loader import (
    JsonlManifestEvaluationDatasetLoader,
)


def _manifest(record: dict[str, Any], file: Path) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "datasets": [
            {
                "id": record["id"],
                "file": str(file),
                "sha256": record["sha256"],
                "provenance": record.get("provenance", "test"),
                "model_id": record.get("model_id", ""),
                "record_count": record.get("record_count", 0),
            }
        ],
    }


def test_manifest_sha256_mismatch_fails_fast(tmp_path: Path) -> None:
    """A wrong sha256 stops loading before records are parsed."""
    data_file = tmp_path / "data.jsonl"
    data_file.write_text('{"id":"r1"}\n', encoding="utf-8")
    manifest_file = tmp_path / "MANIFEST.json"
    manifest_file.write_text(
        json.dumps(
            _manifest(
                {"id": "data", "sha256": "0" * 64, "provenance": "test"},
                data_file.relative_to(tmp_path),
            )
        ),
        encoding="utf-8",
    )
    loader = JsonlManifestEvaluationDatasetLoader(manifest_file)
    with pytest.raises(DatasetManifestMismatch, match="hash mismatch"):
        loader.load("data")


def test_unknown_dataset_id_raises(tmp_path: Path) -> None:
    """Loading a dataset id not in the manifest raises UnknownDatasetError."""
    manifest_file = tmp_path / "MANIFEST.json"
    manifest_file.write_text(
        json.dumps({"schema_version": "1.0.0", "datasets": []}),
        encoding="utf-8",
    )
    loader = JsonlManifestEvaluationDatasetLoader(manifest_file)
    with pytest.raises(UnknownDatasetError):
        loader.load("missing")


def test_missing_file_raises(tmp_path: Path) -> None:
    """A manifest referencing a missing file raises DatasetLoadError."""
    manifest_file = tmp_path / "MANIFEST.json"
    manifest_file.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "datasets": [
                    {
                        "id": "data",
                        "file": "nope.jsonl",
                        "sha256": "0" * 64,
                        "provenance": "test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    loader = JsonlManifestEvaluationDatasetLoader(manifest_file)
    with pytest.raises(DatasetLoadError):
        loader.load("data")


def test_malformed_manifest_raises(tmp_path: Path) -> None:
    """A manifest that is not valid JSON raises DatasetLoadError."""
    manifest_file = tmp_path / "MANIFEST.json"
    manifest_file.write_text("not json", encoding="utf-8")
    loader = JsonlManifestEvaluationDatasetLoader(manifest_file)
    with pytest.raises(DatasetLoadError):
        loader.load("data")


def test_duplicate_record_ids_raise(tmp_path: Path) -> None:
    """Duplicate record ids are rejected as a dataset integrity error."""
    data_file = tmp_path / "data.jsonl"
    data_file.write_text(
        '{"question_id":"q1","x":1}\n{"question_id":"q1","x":2}\n',
        encoding="utf-8",
    )
    sha = hashlib.sha256(data_file.read_bytes()).hexdigest()
    manifest_file = tmp_path / "MANIFEST.json"
    manifest_file.write_text(
        json.dumps(_manifest({"id": "data", "sha256": sha}, data_file)),
        encoding="utf-8",
    )
    loader = JsonlManifestEvaluationDatasetLoader(manifest_file)
    with pytest.raises(DatasetLoadError, match="duplicate"):
        loader.load("data")


def test_yaml_dataset_loads(tmp_path: Path) -> None:
    """The loader supports YAML list datasets (resolution fixture)."""
    data_file = tmp_path / "data.yaml"
    records = [{"id": "p1", "label": "same"}, {"id": "p2", "label": "different"}]
    data_file.write_text(yaml.safe_dump(records), encoding="utf-8")
    sha = hashlib.sha256(data_file.read_bytes()).hexdigest()
    manifest_file = tmp_path / "MANIFEST.json"
    manifest_file.write_text(
        json.dumps(_manifest({"id": "data", "sha256": sha}, data_file)),
        encoding="utf-8",
    )
    loader = JsonlManifestEvaluationDatasetLoader(manifest_file)
    dataset = loader.load("data")
    assert dataset.dataset_id == "data"
    assert len(dataset.records) == 2


def test_list_datasets_returns_ids(tmp_path: Path) -> None:
    """list_datasets returns every declared dataset id."""
    manifest_file = tmp_path / "MANIFEST.json"
    manifest_file.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "datasets": [
                    {"id": "a", "file": "a.jsonl", "sha256": "0" * 64, "provenance": "p"},
                    {"id": "b", "file": "b.jsonl", "sha256": "1" * 64, "provenance": "p"},
                ],
            }
        ),
        encoding="utf-8",
    )
    loader = JsonlManifestEvaluationDatasetLoader(manifest_file)
    assert loader.list_datasets() == ("a", "b")
