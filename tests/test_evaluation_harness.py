"""Unit tests for the hybrid evaluation harness (Slice F-a).

All embedding and retrieval dependencies are faked in memory so these tests
never load real sentence-transformers weights or connect to Neo4j.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
import yaml

from book_graph_rag.application.evaluation_harness import EvaluationHarness
from book_graph_rag.domain.dataset_models import (
    DatasetLabel,
    DatasetManifest,
    DatasetPairEntity,
    LabeledPair,
)
from book_graph_rag.domain.evaluation_models import BaselineReport, EvaluationMetrics
from book_graph_rag.domain.s4_band_assignment import BandThresholds
from book_graph_rag.infrastructure.brute_force_candidate_retrieval import (
    BruteForceCandidateRetrieval,
)
from book_graph_rag.ports.embedding_provider_port import (
    EmbeddingBatch,
    EmbeddingProviderPort,
    EmbeddingRequest,
    EmbeddingVector,
)


class _FakeEmbeddingProvider(EmbeddingProviderPort):
    """Deterministic embedding provider driven by a text-to-vector map."""

    def __init__(
        self,
        vectors_by_text: dict[str, tuple[float, ...]],
        dim: int,
    ) -> None:
        self._vectors = vectors_by_text
        self._dim = dim

    async def embed(self, request: EmbeddingRequest) -> EmbeddingBatch:
        vectors: list[EmbeddingVector] = []
        for text in request.texts:
            values = self._vectors.get(text, (0.0,) * self._dim)
            vectors.append(EmbeddingVector(values=values, model_id=request.model_id))
        return EmbeddingBatch(model_id=request.model_id, vectors=vectors)

    def model_dim(self, model_id: str) -> int:
        return self._dim


def _make_pair(
    pid: str,
    label: DatasetLabel,
    name_a: str,
    name_b: str,
    *,
    hard: bool = False,
    multilingual: bool = False,
    aliases_a: tuple[str, ...] = (),
    aliases_b: tuple[str, ...] = (),
    desc_a: str = "",
    desc_b: str = "",
) -> LabeledPair:
    return LabeledPair(
        id=pid,
        label=label,
        hard=hard,
        multilingual=multilingual,
        entity_a=DatasetPairEntity(
            name=name_a,
            type="framework",
            aliases=aliases_a,
            description=desc_a,
            namespace="book:ch1",
        ),
        entity_b=DatasetPairEntity(
            name=name_b,
            type="framework",
            aliases=aliases_b,
            description=desc_b,
            namespace="book:ch1",
        ),
    )


def _text_for(entity: DatasetPairEntity, variant: Literal["A", "B"]) -> str:
    """Mirror the harness text-building logic for test fixtures."""
    parts = [entity.name] + list(entity.aliases)
    if variant == "B":
        parts.append(entity.description or "")
    return " ".join(p for p in parts if p)


def _write_dataset(
    pairs: list[LabeledPair],
    pairs_path: Path,
    manifest_path: Path,
) -> DatasetManifest:
    """Materialize a labeled dataset and its manifest for the harness."""
    pairs_path.parent.mkdir(parents=True, exist_ok=True)
    pairs_path.write_text(
        yaml.dump(
            [pair.model_dump(mode="json") for pair in pairs],
            sort_keys=False,
            allow_unicode=True,
            line_break="\n",
        ),
        encoding="utf-8",
    )
    content = pairs_path.read_bytes()
    manifest = DatasetManifest(
        pairs_file_sha256=__import__("hashlib").sha256(content).hexdigest(),
        pair_count=len(pairs),
        same_count=sum(1 for p in pairs if p.label == DatasetLabel.SAME),
        different_count=sum(1 for p in pairs if p.label == DatasetLabel.DIFFERENT),
        hard_count=sum(1 for p in pairs if p.hard),
        multilingual_count=sum(1 for p in pairs if p.multilingual),
    )
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


@pytest.fixture
def mini_dataset(tmp_path: Path) -> tuple[Path, Path, list[LabeledPair]]:
    """A 4-pair fixture: 2 same (1 multilingual), 2 different (1 hard)."""
    pairs: list[LabeledPair] = [
        _make_pair(
            "p1",
            DatasetLabel.SAME,
            "LangGraph",
            "Lang Graph",
            multilingual=True,
            aliases_a=("Lang Graph",),
        ),
        _make_pair(
            "p2",
            DatasetLabel.SAME,
            "ReAct Pattern",
            "ReAct",
            aliases_b=("ReAct Pattern",),
        ),
        _make_pair(
            "p3",
            DatasetLabel.DIFFERENT,
            "Agent",
            "Agent-1",
            hard=True,
        ),
        _make_pair(
            "p4",
            DatasetLabel.DIFFERENT,
            "LangGraph",
            "LangChain",
        ),
    ]
    pairs_path = tmp_path / "pairs.yaml"
    manifest_path = tmp_path / "manifest.json"
    _write_dataset(pairs, pairs_path, manifest_path)
    return pairs_path, manifest_path, pairs


def _build_embedding_map(
    pairs: list[LabeledPair],
    variant: Literal["A", "B"],
) -> dict[str, tuple[float, ...]]:
    """Assign vectors so same-labeled sides collide and different-labeled sides don't.

    Same-labeled pair sides share one unique basis vector (high cosine).
    Different-labeled pair sides get two distinct basis vectors (cosine 0).
    """
    mapping: dict[str, tuple[float, ...]] = {}
    basis = [
        (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
    ]
    idx = 0
    for pair in pairs:
        if pair.label == DatasetLabel.SAME:
            vec = basis[idx]
            mapping[_text_for(pair.entity_a, variant)] = vec
            mapping[_text_for(pair.entity_b, variant)] = vec
            idx += 1
        else:
            mapping[_text_for(pair.entity_a, variant)] = basis[idx]
            mapping[_text_for(pair.entity_b, variant)] = basis[idx + 1]
            idx += 2
    return mapping


def _make_harness(
    pairs_path: Path,
    manifest_path: Path,
    baseline_path: Path,
    output_path: Path,
    variant: Literal["A", "B"] = "A",
) -> tuple[EvaluationHarness, _FakeEmbeddingProvider, BruteForceCandidateRetrieval]:
    pairs = [
        LabeledPair.model_validate(item)
        for item in yaml.safe_load(pairs_path.read_bytes()) or []
    ]
    mapping = _build_embedding_map(pairs, variant)
    embedding = _FakeEmbeddingProvider(mapping, dim=8)
    retrieval = BruteForceCandidateRetrieval()
    harness = EvaluationHarness(
        embedding=embedding,
        retrieval=retrieval,
        thresholds=BandThresholds(),
        dataset_path=pairs_path,
        manifest_path=manifest_path,
        baseline_path=baseline_path,
        output_path=output_path,
        top_k=10,
        min_similarity=0.5,
    )
    return harness, embedding, retrieval


def _write_baseline(
    path: Path,
    retrieval_f1: float,
    multilingual_under_merge: float,
    hard_over_merge: float = 0.0,
    manifest_sha256: str = "",
) -> BaselineReport:
    path.parent.mkdir(parents=True, exist_ok=True)
    report = BaselineReport(
        retrieval_precision=retrieval_f1,
        retrieval_recall=retrieval_f1,
        retrieval_f1=retrieval_f1,
        multilingual_under_merge_rate=multilingual_under_merge,
        multilingual_f1=retrieval_f1,
        hard_over_merge_rate=hard_over_merge,
        dataset_manifest_sha256=manifest_sha256,
        captured_at=datetime.now(UTC),
    )
    path.write_text(json.dumps(report.model_dump(mode="json"), indent=2) + "\n")
    return report


async def test_load_dataset_validates_manifest_and_returns_pairs(
    mini_dataset: tuple[Path, Path, list[LabeledPair]],
    tmp_path: Path,
) -> None:
    """load_dataset() validates the pairs.yaml hash against the manifest."""
    pairs_path, manifest_path, expected_pairs = mini_dataset
    baseline_path = tmp_path / "baseline.json"
    output_path = tmp_path / "metrics.json"
    _write_baseline(baseline_path, retrieval_f1=0.5, multilingual_under_merge=0.5)

    harness, _, _ = _make_harness(pairs_path, manifest_path, baseline_path, output_path)
    pairs, manifest = harness.load_dataset()

    assert len(pairs) == len(expected_pairs)
    assert manifest.pair_count == len(expected_pairs)


async def test_load_dataset_fails_on_drift(
    mini_dataset: tuple[Path, Path, list[LabeledPair]],
    tmp_path: Path,
) -> None:
    """If pairs.yaml is edited after the manifest, load_dataset() raises."""
    pairs_path, manifest_path, _ = mini_dataset
    baseline_path = tmp_path / "baseline.json"
    output_path = tmp_path / "metrics.json"
    _write_baseline(baseline_path, retrieval_f1=0.5, multilingual_under_merge=0.5)

    harness, _, _ = _make_harness(pairs_path, manifest_path, baseline_path, output_path)
    pairs_path.write_text("- id: p1\n  label: same\n", encoding="utf-8")

    with pytest.raises(Exception):  # noqa: B017, PT011
        harness.load_dataset()


async def test_evaluate_computes_metrics_on_mini_dataset(
    mini_dataset: tuple[Path, Path, list[LabeledPair]],
    tmp_path: Path,
) -> None:
    """The harness computes retrieval F1, auto-merge counts, and subset rates."""
    pairs_path, manifest_path, pairs = mini_dataset
    baseline_path = tmp_path / "baseline.json"
    output_path = tmp_path / "metrics.json"
    manifest = DatasetManifest.model_validate_json(manifest_path.read_text())
    _write_baseline(
        baseline_path,
        retrieval_f1=0.5,
        multilingual_under_merge=0.5,
        manifest_sha256=manifest.pairs_file_sha256,
    )

    harness, _, _ = _make_harness(pairs_path, manifest_path, baseline_path, output_path)
    metrics = await harness.evaluate(
        model_id="fake-model",
        input_variant="A",
    )

    assert isinstance(metrics, EvaluationMetrics)
    assert metrics.model_id == "fake-model"
    assert metrics.input_variant == "A"
    assert metrics.retrieval_precision == pytest.approx(1.0)
    assert metrics.retrieval_recall == pytest.approx(1.0)
    assert metrics.retrieval_f1 == pytest.approx(1.0)
    assert metrics.hard_over_merge_rate == pytest.approx(0.0)
    assert metrics.multilingual_under_merge_rate == pytest.approx(0.0)
    assert metrics.auto_merge_count == 2
    assert metrics.auto_merge_correct == 2
    assert output_path.exists()


async def test_compare_to_baseline_returns_gate_verdict(
    mini_dataset: tuple[Path, Path, list[LabeledPair]],
    tmp_path: Path,
) -> None:
    """compare_to_baseline() reflects F1, hard over-merge, and multilingual gates."""
    pairs_path, manifest_path, _ = mini_dataset
    baseline_path = tmp_path / "baseline.json"
    output_path = tmp_path / "metrics.json"
    _write_baseline(baseline_path, retrieval_f1=0.5, multilingual_under_merge=0.5)

    harness, _, _ = _make_harness(pairs_path, manifest_path, baseline_path, output_path)
    metrics = await harness.evaluate(model_id="fake-model", input_variant="A")
    gate = harness.compare_to_baseline(metrics)

    assert gate.beats_baseline_f1 is True
    assert gate.hard_over_merge_zero is True
    assert gate.multilingual_under_merge_ok is True
    assert gate.passed is True


async def test_compare_to_baseline_blocks_when_hard_over_merge_present(
    mini_dataset: tuple[Path, Path, list[LabeledPair]],
    tmp_path: Path,
) -> None:
    """A non-zero hard over-merge rate makes the gate fail closed."""
    pairs_path, manifest_path, _ = mini_dataset
    baseline_path = tmp_path / "baseline.json"
    output_path = tmp_path / "metrics.json"
    _write_baseline(baseline_path, retrieval_f1=0.5, multilingual_under_merge=0.5)

    harness, _, _ = _make_harness(pairs_path, manifest_path, baseline_path, output_path)
    metrics = await harness.evaluate(model_id="fake-model", input_variant="A")
    metrics = metrics.model_copy(update={"hard_over_merge_rate": 0.1})
    gate = harness.compare_to_baseline(metrics)

    assert gate.hard_over_merge_zero is False
    assert gate.passed is False


async def test_threshold_sweep_picks_conservative_interval(
    mini_dataset: tuple[Path, Path, list[LabeledPair]],
    tmp_path: Path,
) -> None:
    """The sweep returns the most conservative threshold pair that beats baseline."""
    pairs_path, manifest_path, _ = mini_dataset
    baseline_path = tmp_path / "baseline.json"
    output_path = tmp_path / "metrics.json"
    _write_baseline(baseline_path, retrieval_f1=0.5, multilingual_under_merge=0.5)

    harness, _, _ = _make_harness(pairs_path, manifest_path, baseline_path, output_path)
    result = await harness.threshold_sweep(
        model_id="fake-model",
        input_variant="A",
        high_cosine_grid=[0.90, 0.95],
        medium_cosine_grid=[0.50, 0.80],
    )

    assert result is not None
    assert result.high_cosine == pytest.approx(0.95)
    assert result.medium_cosine == pytest.approx(0.80)
    assert result.gate.passed is True


async def test_threshold_sweep_returns_none_when_no_interval_passes(
    mini_dataset: tuple[Path, Path, list[LabeledPair]],
    tmp_path: Path,
) -> None:
    """If baseline F1 is unreachable, the sweep reports no viable interval."""
    pairs_path, manifest_path, _ = mini_dataset
    baseline_path = tmp_path / "baseline.json"
    output_path = tmp_path / "metrics.json"
    _write_baseline(baseline_path, retrieval_f1=1.01, multilingual_under_merge=0.0)

    harness, _, _ = _make_harness(pairs_path, manifest_path, baseline_path, output_path)
    result = await harness.threshold_sweep(
        model_id="fake-model",
        input_variant="A",
        high_cosine_grid=[0.90],
        medium_cosine_grid=[0.80],
    )

    assert result is None
