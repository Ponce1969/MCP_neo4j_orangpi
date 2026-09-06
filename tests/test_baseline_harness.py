"""Tests for the slug+token baseline harness."""

from __future__ import annotations

from pathlib import Path

import pytest

from book_graph_rag.application.baseline_harness import BaselineHarness
from book_graph_rag.domain.dataset_models import DatasetLabel, DatasetPairEntity, LabeledPair
from book_graph_rag.domain.evaluation_models import BaselineReport


def _make_pair(pid: str, label: DatasetLabel, name_a: str, name_b: str) -> LabeledPair:
    return LabeledPair(
        id=pid,
        label=label,
        entity_a=DatasetPairEntity(name=name_a, type="pattern"),
        entity_b=DatasetPairEntity(name=name_b, type="pattern"),
    )


def test_baseline_harness_produces_valid_report(tmp_path: Path) -> None:
    """The harness returns a BaselineReport tied to the dataset manifest hash."""
    pairs = [
        _make_pair("p1", DatasetLabel.SAME, "LangGraph", "LangGraph framework"),
        _make_pair("p2", DatasetLabel.SAME, "ReAct Pattern", "ReAct pattern"),
        _make_pair("p3", DatasetLabel.DIFFERENT, "LangGraph", "LangChain"),
        _make_pair("p4", DatasetLabel.DIFFERENT, "Agent", "Agent-1"),
    ]
    pairs_path = tmp_path / "pairs.yaml"
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "baseline_report.json"

    # Write the fixture files directly; the harness validates the manifest.
    harness = BaselineHarness(
        dataset_path=pairs_path,
        manifest_path=manifest_path,
        output_path=output_path,
        threshold=0.9,
    )
    harness._write_fixture_files(pairs)  # noqa: SLF001

    report = harness.run()
    assert isinstance(report, BaselineReport)
    assert report.retrieval_f1 > 0.0
    assert report.dataset_manifest_sha256 == harness._manifest.pairs_file_sha256  # type: ignore[union-attr]  # noqa: SLF001


def test_baseline_harness_fails_on_manifest_drift(tmp_path: Path) -> None:
    """The harness refuses to run if the pairs file has drifted."""
    pairs_path = tmp_path / "pairs.yaml"
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "baseline_report.json"

    harness = BaselineHarness(
        dataset_path=pairs_path,
        manifest_path=manifest_path,
        output_path=output_path,
    )
    pairs = [_make_pair("p1", DatasetLabel.SAME, "A", "A")]
    harness._write_fixture_files(pairs)  # noqa: SLF001
    # Tamper with the pairs file after manifest generation.
    pairs_path.write_text(
        "- id: p1\n"
        "  label: same\n"
        "  entity_a:\n"
        "    name: A\n"
        "    type: pattern\n"
        "  entity_b:\n"
        "    name: B\n"
        "    type: pattern\n"
    )

    with pytest.raises(Exception):  # noqa: B017, PT011
        harness.run()
