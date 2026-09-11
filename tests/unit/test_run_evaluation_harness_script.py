"""Tests for scripts/run_evaluation_harness.py --read-only-snapshot (Slice B, T-B.10)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import run_evaluation_harness as harness_module

from book_graph_rag.domain.evaluation_models import EvaluationMetrics


class _FakeEmbedding:
    def __init__(self, settings: Any) -> None:
        pass


class _FakeRetrieval:
    pass


class _FakeHarness:
    def __init__(self, **kwargs: Any) -> None:
        pass

    async def evaluate(self, *, model_id: str, input_variant: str) -> EvaluationMetrics:
        return EvaluationMetrics(
            model_id=model_id,
            input_variant="A",
            retrieval_precision=0.5,
            retrieval_recall=0.5,
            retrieval_f1=0.5,
            multilingual_under_merge_rate=0.0,
            multilingual_f1=1.0,
            hard_over_merge_rate=0.0,
            auto_merge_count=0,
            auto_merge_correct=0,
        )

    def compare_to_baseline(self, metrics: EvaluationMetrics) -> Any:
        class Gate:
            passed = True
        return Gate()


@pytest.fixture
def patched_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness_module, "SentenceTransformerAdapter", _FakeEmbedding)
    monkeypatch.setattr(harness_module, "BruteForceCandidateRetrieval", _FakeRetrieval)
    monkeypatch.setattr(harness_module, "EvaluationHarness", _FakeHarness)
    monkeypatch.setattr(harness_module, "BandThresholds", lambda: None)


def test_read_only_snapshot_prints_count(
    patched_module: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--read-only-snapshot prints a node count before/after the harness run."""
    exit_code = harness_module.main(["--model", "model", "--read-only-snapshot"])
    captured = capsys.readouterr()
    assert exit_code == 0, captured.out + captured.err
    assert "BEFORE" in captured.out
    assert "AFTER" in captured.out
