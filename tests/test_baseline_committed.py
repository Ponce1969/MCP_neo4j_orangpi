"""Tests for the committed slug+token baseline report."""

from __future__ import annotations

import json
from pathlib import Path

from book_graph_rag.domain.dataset_models import DatasetManifest
from book_graph_rag.domain.evaluation_models import BaselineReport

FIXTURES_DIR = Path("tests/fixtures/resolution")
REPORT_PATH = FIXTURES_DIR / "baseline_report.json"
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"


def test_committed_baseline_report_exists_and_is_valid() -> None:
    """The committed baseline report must parse and match the dataset manifest."""
    assert REPORT_PATH.exists(), "baseline_report.json must be committed"
    raw = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    report = BaselineReport.model_validate(raw)
    assert report.resolver == "slug_token"

    manifest = DatasetManifest.model_validate_json(MANIFEST_PATH.read_text())
    assert report.dataset_manifest_sha256 == manifest.pairs_file_sha256
