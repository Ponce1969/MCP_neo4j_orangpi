"""Tests for evaluation ports (Slice A)."""

from __future__ import annotations

import pytest

from book_graph_rag.domain.evaluation_models import (
    DatasetLoadError,
    DatasetManifestMismatch,
    EvaluationBaselineReport,
    EvaluationDataset,
    UnknownDatasetError,
)
from book_graph_rag.ports.evaluation_baseline_port import EvaluationBaselinePort
from book_graph_rag.ports.evaluation_dataset_port import EvaluationDatasetPort


def test_evaluation_dataset_port_is_abstract() -> None:
    """EvaluationDatasetPort cannot be instantiated directly."""
    with pytest.raises(TypeError, match="abstract"):
        EvaluationDatasetPort()  # type: ignore[abstract]


def test_evaluation_dataset_port_contract() -> None:
    """A fake adapter implements load/list_datasets and raises the domain exceptions."""

    class FakeLoader(EvaluationDatasetPort):
        def load(self, dataset_id: str) -> EvaluationDataset:
            if dataset_id == "missing":
                raise UnknownDatasetError(dataset_id)
            if dataset_id == "bad-hash":
                raise DatasetManifestMismatch("bad-hash", "abc", "def")
            if dataset_id == "malformed":
                raise DatasetLoadError("boom")
            return EvaluationDataset(dataset_id=dataset_id, records=())

        def list_datasets(self) -> tuple[str, ...]:
            return ("a", "b")

    port = FakeLoader()
    assert port.load("ok") == EvaluationDataset(dataset_id="ok", records=())
    with pytest.raises(UnknownDatasetError):
        port.load("missing")
    with pytest.raises(DatasetManifestMismatch):
        port.load("bad-hash")
    with pytest.raises(DatasetLoadError):
        port.load("malformed")
    assert port.list_datasets() == ("a", "b")


def test_evaluation_baseline_port_is_abstract() -> None:
    """EvaluationBaselinePort cannot be instantiated directly."""
    with pytest.raises(TypeError, match="abstract"):
        EvaluationBaselinePort()  # type: ignore[abstract]


def test_evaluation_baseline_port_contract() -> None:
    """A fake adapter returns a baseline or None."""

    class FakeBaseline(EvaluationBaselinePort):
        def load(self, layer: str) -> EvaluationBaselineReport | None:
            if layer == "missing":
                return None
            return EvaluationBaselineReport(
                layer="resolution", dataset_id="r", dataset_sha256="0" * 64
            )

    port = FakeBaseline()
    assert port.load("missing") is None
    baseline = port.load("resolution")
    assert baseline is not None
    assert baseline.layer == "resolution"
