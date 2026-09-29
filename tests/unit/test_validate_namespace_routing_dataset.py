"""Behavior tests for the Unit 0 namespace-routing dataset validator."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).parents[2]
_VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "validate_namespace_routing_dataset",
    _ROOT / "scripts/validate_namespace_routing_dataset.py",
)
assert _VALIDATOR_SPEC is not None
assert _VALIDATOR_SPEC.loader is not None
_VALIDATOR: ModuleType = importlib.util.module_from_spec(_VALIDATOR_SPEC)
_VALIDATOR_SPEC.loader.exec_module(_VALIDATOR)
NamespaceRoutingDatasetError = _VALIDATOR.NamespaceRoutingDatasetError
validate_dataset = _VALIDATOR.validate_dataset
_DATASET = _ROOT / "data/evaluation/namespace_routing_dataset.jsonl"
_CATALOG = _ROOT / "catalog.yaml"
_MANIFEST = _ROOT / "data/evaluation/MANIFEST.json"


def _copy_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    dataset = tmp_path / "namespace_routing_dataset.jsonl"
    manifest = tmp_path / "MANIFEST.json"
    shutil.copyfile(_DATASET, dataset)
    shutil.copyfile(_MANIFEST, manifest)
    return dataset, manifest


def test_unit_0_dataset_matches_catalog_and_manifest() -> None:
    count, digest = validate_dataset(_DATASET, _CATALOG, _MANIFEST)

    assert count == 31
    assert len(digest) == 64


def test_unknown_namespace_is_rejected(tmp_path: Path) -> None:
    dataset, manifest = _copy_artifacts(tmp_path)
    records = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines()]
    records[0]["expected_namespaces"] = ["knowledge:not-a-book"]
    dataset.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    with pytest.raises(NamespaceRoutingDatasetError, match="unknown namespaces"):
        validate_dataset(dataset, _CATALOG, manifest)


def test_manifest_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    dataset, manifest = _copy_artifacts(tmp_path)
    raw_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    entry = next(
        item for item in raw_manifest["datasets"] if item["id"] == "namespace_routing_dataset"
    )
    entry["sha256"] = "0" * 64
    manifest.write_text(json.dumps(raw_manifest), encoding="utf-8")

    with pytest.raises(NamespaceRoutingDatasetError, match="SHA-256"):
        validate_dataset(dataset, _CATALOG, manifest)


def test_single_route_requires_exactly_one_namespace(tmp_path: Path) -> None:
    dataset, manifest = _copy_artifacts(tmp_path)
    records = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines()]
    records[0]["expected_namespaces"] = []
    dataset.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    with pytest.raises(NamespaceRoutingDatasetError, match="invalid namespace count"):
        validate_dataset(dataset, _CATALOG, manifest)
