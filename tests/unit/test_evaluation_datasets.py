"""Tests for committed evaluation datasets and manifest (Slice A)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from book_graph_rag.infrastructure.evaluation_dataset_loader import (
    JsonlManifestEvaluationDatasetLoader,
)


def test_manifest_references_real_files_with_matching_hashes() -> None:
    """Every dataset file exists and its sha256 matches the manifest."""
    loader = JsonlManifestEvaluationDatasetLoader(Path("data/evaluation/MANIFEST.json"))
    for dataset_id in loader.list_datasets():
        dataset = loader.load(dataset_id)
        assert dataset.dataset_id == dataset_id
        assert len(dataset.records) > 0


def test_generation_dataset_has_at_least_thirty_unique_records() -> None:
    """generation_dataset.jsonl contains >=30 deduplicated records."""
    loader = JsonlManifestEvaluationDatasetLoader(Path("data/evaluation/MANIFEST.json"))
    dataset = loader.load("generation_dataset")
    assert len(dataset.records) >= 30
    ids = {r["question_id"] for r in dataset.records}
    assert len(ids) == len(dataset.records)


def test_retrieval_dataset_has_at_least_twenty_unique_records() -> None:
    """retrieval_dataset.jsonl contains >=20 deduplicated records."""
    loader = JsonlManifestEvaluationDatasetLoader(Path("data/evaluation/MANIFEST.json"))
    dataset = loader.load("retrieval_dataset")
    assert len(dataset.records) >= 20
    ids = {r["question_id"] for r in dataset.records}
    assert len(ids) == len(dataset.records)


def test_resolution_dataset_registered_without_duplicating_pairs() -> None:
    """resolution_dataset points at the existing fixture; no duplicate records."""
    loader = JsonlManifestEvaluationDatasetLoader(Path("data/evaluation/MANIFEST.json"))
    dataset = loader.load("resolution_dataset")
    assert len(dataset.records) == 110
    ids = {r["id"] for r in dataset.records}
    assert len(ids) == len(dataset.records)


def test_resolution_baseline_matches_dataset_sha256() -> None:
    """resolution_baseline.json names the same dataset hash as the manifest."""
    manifest_text = Path("data/evaluation/MANIFEST.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    resolution_record = next(
        r for r in manifest["datasets"] if r["id"] == "resolution_dataset"
    )
    baseline_text = Path("data/evaluation/resolution_baseline.json").read_text(
        encoding="utf-8"
    )
    baseline = json.loads(baseline_text)
    assert baseline["dataset_id"] == "resolution_dataset"
    assert baseline["dataset_sha256"] == resolution_record["sha256"]
    pairs_sha = hashlib.sha256(
        Path("tests/fixtures/resolution/pairs.yaml").read_bytes()
    ).hexdigest()
    assert baseline["dataset_sha256"] == pairs_sha
