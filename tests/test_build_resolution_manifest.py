"""Tests for the resolution dataset manifest generator."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from scripts.build_resolution_manifest import build_manifest

from book_graph_rag.domain.dataset_models import DatasetManifest
from book_graph_rag.domain.resolution_errors import DatasetManifestMismatch

FIXTURES_DIR = Path("tests/fixtures/resolution")
PAIRS_PATH = FIXTURES_DIR / "pairs.yaml"
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"


MINIMUMS = {
    "pair_count": 100,
    "same_count": 50,
    "different_count": 50,
    "hard_count": 20,
    "multilingual_count": 20,
}


def test_manifest_generator_meets_strata_minimums() -> None:
    """The generated manifest reports the required stratum counts."""
    manifest = build_manifest(PAIRS_PATH)
    for key, minimum in MINIMUMS.items():
        assert getattr(manifest, key) >= minimum, f"{key} below minimum {minimum}"


def test_manifest_hash_matches_pairs_file() -> None:
    """The manifest SHA-256 must match the pairs.yaml bytes."""
    manifest = build_manifest(PAIRS_PATH)
    content = PAIRS_PATH.read_bytes()
    assert manifest.pairs_file_sha256 == hashlib.sha256(content).hexdigest()


def test_manifest_json_round_trips() -> None:
    """The committed manifest.json must parse as DatasetManifest."""
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest = DatasetManifest.model_validate(raw)
    manifest.require_hash_match(PAIRS_PATH.read_bytes())


def test_manifest_rejects_tampered_pairs(tmp_path: Path) -> None:
    """Changing pairs.yaml without regenerating the manifest is detected."""
    pairs = tmp_path / "pairs.yaml"
    manifest_path = tmp_path / "manifest.json"
    pairs.write_text(
        "- id: pair-x\n"
        "  label: same\n"
        "  entity_a:\n"
        "    name: A\n"
        "    type: pattern\n"
        "  entity_b:\n"
        "    name: B\n"
        "    type: pattern\n"
    )
    build_manifest(pairs, manifest_path)

    manifest = DatasetManifest.model_validate_json(manifest_path.read_text())
    pairs.write_text(
        "- id: pair-y\n"
        "  label: different\n"
        "  entity_a:\n"
        "    name: A\n"
        "    type: pattern\n"
        "  entity_b:\n"
        "    name: B\n"
        "    type: pattern\n"
    )
    with pytest.raises(DatasetManifestMismatch):
        manifest.require_hash_match(pairs.read_bytes())
