"""Tests for evaluation-related Settings extensions (Slice C, T-C.3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from book_graph_rag.config import Settings


def test_evaluation_dir_default() -> None:
    """Default evaluation paths point to data/evaluation."""
    settings = Settings.model_validate({})
    assert settings.evaluation_dir == Path("data/evaluation")
    assert settings.evaluation_manifest_path == Path("data/evaluation/MANIFEST.json")
    assert settings.evaluation_baseline_dir == Path("data/evaluation")


def test_claim_extractor_model_defaults_to_query_llm() -> None:
    """Empty claim_extractor_model falls back to query_llm_model_name."""
    settings = Settings.model_validate(
        {"query_llm_model_name": "gpt-4o", "claim_extractor_model": ""}
    )
    assert settings.effective_claim_extractor_model == "gpt-4o"


def test_pairwise_judge_model_defaults_to_query_llm() -> None:
    """Empty pairwise_judge_model falls back to query_llm_model_name."""
    settings = Settings.model_validate(
        {"query_llm_model_name": "gpt-4o", "pairwise_judge_model": ""}
    )
    assert settings.effective_pairwise_judge_model == "gpt-4o"


def test_explicit_claim_extractor_model_used() -> None:
    """A non-empty claim_extractor_model is respected."""
    settings = Settings.model_validate(
        {
            "query_llm_model_name": "gpt-4o",
            "claim_extractor_model": "custom-extractor",
        }
    )
    assert settings.effective_claim_extractor_model == "custom-extractor"


def test_ragas_enabled_default_true() -> None:
    """RAGAS is enabled by default."""
    settings = Settings.model_validate({})
    assert settings.ragas_enabled is True


def test_missing_manifest_path_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A nonexistent evaluation manifest path aborts at startup."""
    missing = tmp_path / "missing" / "MANIFEST.json"
    with pytest.raises(ValidationError, match="evaluation_manifest_path"):
        Settings.model_validate({"evaluation_manifest_path": str(missing)})


def test_evaluation_baseline_dir_must_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A nonexistent baseline dir aborts at startup."""
    missing = tmp_path / "missing"
    with pytest.raises(ValidationError, match="evaluation_baseline_dir"):
        Settings.model_validate({"evaluation_baseline_dir": str(missing)})
