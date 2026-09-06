"""Unit tests for resolution dataset domain models."""

from __future__ import annotations

import hashlib

import pytest
import yaml

from book_graph_rag.domain.dataset_models import DatasetManifest, LabeledPair
from book_graph_rag.domain.resolution_errors import DatasetManifestMismatch


def test_labeled_pair_yaml_round_trip() -> None:
    """A labeled pair loads from YAML with the expected schema version."""
    raw = """
schema_version: "1.0.0"
id: pair-001
label: same
entity_a:
  name: LangGraph
  type: framework
entity_b:
  name: LangGraph framework
  type: framework
"""
    data = yaml.safe_load(raw)
    pair = LabeledPair.model_validate(data)
    assert pair.schema_version == "1.0.0"
    assert pair.label.value == "same"
    assert pair.entity_a.name == "LangGraph"
    assert pair.entity_b.name == "LangGraph framework"
    assert pair.entity_a.type == "framework"
    assert not pair.hard
    assert not pair.multilingual


def test_manifest_rejects_hash_mismatch() -> None:
    """DatasetManifest must raise when the file hash drifts."""
    manifest = DatasetManifest(
        pairs_file_sha256="a" * 64,
        pair_count=1,
        same_count=1,
        different_count=0,
        hard_count=0,
        multilingual_count=0,
    )
    with pytest.raises(DatasetManifestMismatch):
        manifest.require_hash_match(b"changed content")


def test_manifest_accepts_matching_hash() -> None:
    """DatasetManifest must accept bytes that match the recorded hash."""
    content = b"stable dataset content"
    expected = hashlib.sha256(content).hexdigest()
    manifest = DatasetManifest(
        pairs_file_sha256=expected,
        pair_count=1,
        same_count=1,
        different_count=0,
        hard_count=0,
        multilingual_count=0,
    )
    manifest.require_hash_match(content)
