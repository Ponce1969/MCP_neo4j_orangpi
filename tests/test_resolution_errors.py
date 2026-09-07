"""Tests for the resolution exception hierarchy (Slice B)."""

from __future__ import annotations

import pytest

from book_graph_rag.domain.resolution_errors import (
    DatasetManifestMismatch,
    InvalidNormalizationInput,
    LedgerChainBroken,
    MergeNotReversible,
    ResolutionError,
    RollbackTargetInvalid,
)


@pytest.mark.parametrize(
    "exception_cls",
    [
        InvalidNormalizationInput,
        DatasetManifestMismatch,
        LedgerChainBroken,
        RollbackTargetInvalid,
        MergeNotReversible,
    ],
)
def test_resolution_errors_subclass_base(exception_cls: type[ResolutionError]) -> None:
    """Every resolution error is a subclass of ResolutionError."""
    assert issubclass(exception_cls, ResolutionError)


def test_dataset_manifest_mismatch_carries_message() -> None:
    with pytest.raises(DatasetManifestMismatch, match="hash mismatch"):
        raise DatasetManifestMismatch("hash mismatch")


def test_ledger_chain_broken_carries_diagnostic_attributes() -> None:
    exc = LedgerChainBroken("chain broken", seq=3, expected="abc", actual="def")
    assert exc.seq == 3
    assert exc.expected == "abc"
    assert exc.actual == "def"
    assert "chain broken" in str(exc)
