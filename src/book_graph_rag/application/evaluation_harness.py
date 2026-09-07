"""Evaluation harness for the hybrid semantic entity resolver (Slice F-a).

The harness runs the S0–S4 resolution stages over the committed labeled dataset
using only domain logic and ports. It is intentionally decoupled from concrete
infrastructure so the same code can be driven by the brute-force eval adapter on
Windows or the Neo4j vector adapter on OrangePi.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import yaml

from book_graph_rag.domain.dataset_models import DatasetLabel, DatasetManifest, LabeledPair
from book_graph_rag.domain.evaluation_models import (
    BaselineReport,
    EvaluationGate,
    EvaluationMetrics,
    ThresholdSweepResult,
)
from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.resolution_models import ConfidenceBand, ResolutionEvidence
from book_graph_rag.domain.resolution_policy import decide
from book_graph_rag.domain.s0_normalization import s0_match
from book_graph_rag.domain.s2_type_gate import s2_type_gate
from book_graph_rag.domain.s3_context_scoring import (
    description_overlap,
    s3_context_score,
)
from book_graph_rag.domain.s4_band_assignment import BandThresholds, assign_band
from book_graph_rag.ports.candidate_retrieval_port import (
    CandidateHit,
    CandidateRetrievalPort,
    CandidateRetrievalRequest,
)
from book_graph_rag.ports.embedding_provider_port import (
    EmbeddingProviderPort,
    EmbeddingRequest,
)


class EvaluationHarness:
    """Evaluate a model + input variant against the labeled dataset."""

    def __init__(
        self,
        embedding: EmbeddingProviderPort,
        retrieval: CandidateRetrievalPort,
        thresholds: BandThresholds,
        dataset_path: Path,
        manifest_path: Path,
        baseline_path: Path,
        output_path: Path,
        top_k: int = 10,
        min_similarity: float = 0.0,
    ) -> None:
        self._embedding = embedding
        self._retrieval = retrieval
        self._thresholds = thresholds
        self._dataset_path = dataset_path
        self._manifest_path = manifest_path
        self._baseline_path = baseline_path
        self._output_path = output_path
        self._top_k = top_k
        self._min_similarity = min_similarity

    def load_dataset(self) -> tuple[list[LabeledPair], DatasetManifest]:
        """Validate the manifest hash and return the labeled pairs + manifest."""
        manifest = DatasetManifest.model_validate_json(
            self._manifest_path.read_text(encoding="utf-8")
        )
        manifest.require_hash_match(self._dataset_path.read_bytes())
        raw = yaml.safe_load(self._dataset_path.read_bytes()) or []
        pairs = [LabeledPair.model_validate(item) for item in raw]
        return pairs, manifest

    def _load_baseline(self) -> BaselineReport:
        return BaselineReport.model_validate_json(
            self._baseline_path.read_text(encoding="utf-8")
        )

    @staticmethod
    def _entity_for(
        pair: LabeledPair, side: Literal["a", "b"]
    ) -> tuple[Entity, str]:
        """Build a synthetic graph entity and namespace from one side of a pair."""
        entity = pair.entity_a if side == "a" else pair.entity_b
        namespace = entity.namespace or "dataset:default"
        return (
            Entity(
                # Namespaced 3-part id (corpus:source:slug) so Phase 1
                # namespace derivation sees one namespace on both sides.
                id=f"{namespace}:{pair.id}-{side}",
                name=entity.name,
                type=entity.type,
                aliases=list(entity.aliases),
                description=entity.description,
                canonical_name=None,
                source_page=None,
            ),
            namespace,
        )

    @staticmethod
    def _anchor_text(entity: Entity, input_variant: Literal["A", "B"]) -> str:
        """Build the embedding input text for a synthetic entity."""
        parts = [entity.name] + list(entity.aliases)
        if input_variant == "B":
            parts.append(entity.description or "")
        return " ".join(p for p in parts if p)

    async def _embed_entities(
        self,
        pairs: list[LabeledPair],
        input_variant: Literal["A", "B"],
        model_id: str,
    ) -> tuple[dict[str, Entity], dict[str, str]]:
        """Embed every synthetic entity and upsert it into the retrieval adapter."""
        entity_by_id: dict[str, Entity] = {}
        namespace_by_id: dict[str, str] = {}
        texts: list[str] = []
        order: list[str] = []

        for pair in pairs:
            for side in ("a", "b"):
                entity, namespace = self._entity_for(pair, side)
                entity_by_id[entity.id] = entity
                namespace_by_id[entity.id] = namespace
                text = self._anchor_text(entity, input_variant)
                texts.append(text)
                order.append(entity.id)

        if not texts:
            return entity_by_id, namespace_by_id

        batch = await self._embedding.embed(
            EmbeddingRequest(texts=tuple(texts), model_id=model_id)
        )
        for entity_id, vector in zip(order, batch.vectors, strict=True):
            entity = entity_by_id[entity_id]
            namespace = namespace_by_id[entity_id]
            await self._retrieval.upsert_entity_embedding(entity_id, vector)
            # Brute-force eval adapters need type/namespace metadata injected
            # separately; production adapters read this from the graph.
            if hasattr(self._retrieval, "upsert_entity_metadata"):
                self._retrieval.upsert_entity_metadata(
                    entity_id, entity.type, namespace
                )

        return entity_by_id, namespace_by_id

    async def evaluate(
        self,
        *,
        model_id: str,
        input_variant: Literal["A", "B"],
    ) -> EvaluationMetrics:
        """Run the hybrid pipeline over the dataset and produce metrics."""
        pairs, _manifest = self.load_dataset()
        entity_by_id, namespace_by_id = await self._embed_entities(
            pairs, input_variant, model_id
        )

        retrieval_predictions: list[Literal["same", "different"]] = []
        auto_merge_predictions: list[Literal["same", "different"]] = []

        tp = fp = fn = 0
        auto_merge_count = 0
        auto_merge_correct = 0
        hard_fp = 0
        hard_count = 0
        multilingual_fn = 0
        multilingual_same_count = 0

        for pair in pairs:
            anchor = entity_by_id[self._entity_for(pair, "a")[0].id]
            candidate = entity_by_id[self._entity_for(pair, "b")[0].id]

            request = CandidateRetrievalRequest(
                anchor_id=anchor.id,
                anchor_text=self._anchor_text(anchor, input_variant),
                anchor_type=anchor.type,
                anchor_namespace=namespace_by_id[anchor.id],
                top_k=self._top_k,
                min_similarity=self._min_similarity,
            )
            hits = await self._retrieval.retrieve(request)
            hit_info = {h.candidate_id: (rank, h) for rank, h in enumerate(hits, start=1)}
            retrieved = candidate.id in hit_info
            rank, hit = hit_info.get(candidate.id, (0, None))

            prediction: Literal["same", "different"] = "same" if retrieved else "different"
            retrieval_predictions.append(prediction)

            if pair.label == DatasetLabel.SAME and retrieved:
                tp += 1
            if pair.label == DatasetLabel.DIFFERENT and retrieved:
                fp += 1
            if pair.label == DatasetLabel.SAME and not retrieved:
                fn += 1

            evidence = self._evaluate_pair(
                anchor,
                candidate,
                hit,
                rank,
                input_variant,
                model_id,
            )
            decision = decide(evidence, self._thresholds)
            auto_merged = decision.action.value == "auto_merge"

            auto_merge_predictions.append("same" if auto_merged else "different")
            if auto_merged:
                auto_merge_count += 1
                if pair.label == DatasetLabel.SAME:
                    auto_merge_correct += 1

            if pair.hard and pair.label == DatasetLabel.DIFFERENT and auto_merged:
                hard_fp += 1
            if pair.hard:
                hard_count += 1

            if pair.multilingual and pair.label == DatasetLabel.SAME:
                multilingual_same_count += 1
                # Under-merge mirrors baseline harness semantics (spec R7.4:
                # multilingual pairs 'resolve' = retrieved), measured at
                # candidate-retrieval level, not auto-merge level.
                if not retrieved:
                    multilingual_fn += 1

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )

        hard_over_merge_rate = hard_fp / hard_count if hard_count > 0 else 0.0
        multilingual_under_merge_rate = (
            multilingual_fn / multilingual_same_count
            if multilingual_same_count > 0
            else 0.0
        )
        multilingual_f1 = self._subset_f1(
            pairs, retrieval_predictions, lambda p: p.multilingual
        )

        metrics = EvaluationMetrics(
            model_id=model_id,
            input_variant=input_variant,
            retrieval_precision=precision,
            retrieval_recall=recall,
            retrieval_f1=f1,
            multilingual_under_merge_rate=multilingual_under_merge_rate,
            multilingual_f1=multilingual_f1,
            hard_over_merge_rate=hard_over_merge_rate,
            auto_merge_count=auto_merge_count,
            auto_merge_correct=auto_merge_correct,
        )

        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._output_path.write_text(
            json.dumps(metrics.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        return metrics

    def _subset_f1(
        self,
        pairs: list[LabeledPair],
        predictions: list[Literal["same", "different"]],
        in_subset: Callable[[LabeledPair], bool],
    ) -> float:
        """Compute F1 over a subset of the pairs using per-pair predictions."""
        subset_pairs = [p for p in pairs if in_subset(p)]
        if not subset_pairs:
            return 0.0
        subset_preds = [
            pred for p, pred in zip(pairs, predictions, strict=True) if in_subset(p)
        ]
        tp = sum(
            1 for p, pred in zip(subset_pairs, subset_preds, strict=True)
            if p.label == DatasetLabel.SAME and pred == "same"
        )
        fp = sum(
            1 for p, pred in zip(subset_pairs, subset_preds, strict=True)
            if p.label == DatasetLabel.DIFFERENT and pred == "same"
        )
        fn = sum(
            1 for p, pred in zip(subset_pairs, subset_preds, strict=True)
            if p.label == DatasetLabel.SAME and pred == "different"
        )
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        if precision + recall == 0.0:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    def _evaluate_pair(
        self,
        anchor: Entity,
        candidate: Entity,
        hit: CandidateHit | None,
        rank: int,
        input_variant: Literal["A", "B"],
        model_id: str,
    ) -> ResolutionEvidence:
        """Run S0–S4 over a single labeled-pair side."""
        from book_graph_rag.domain.resolution_models import S1EmbeddingSignal

        evidence = s0_match(anchor, candidate)
        if evidence.s0_matched_field != "none":
            return evidence

        cosine = hit.cosine_similarity if hit is not None else 0.0
        s2 = s2_type_gate(anchor.type, candidate.type)
        evidence = evidence.model_copy(
            update={
                "s1": S1EmbeddingSignal(
                    cosine_similarity=cosine,
                    candidate_rank=rank if rank >= 1 else 1,
                    input_variant=input_variant,
                    model_id=model_id,
                ),
                "s2": s2,
            }
        )

        if s2.passed:
            desc_o = description_overlap(anchor.description or "", candidate.description or "")
            s3 = s3_context_score(
                mentions_j=0.0,
                related_j=0.0,
                desc_o=desc_o,
                cosine=cosine,
                mentions_source_count=0,
                mentions_shared_count=0,
                related_neighbor_count=0,
                related_shared_count=0,
            )
            composite = desc_o / 3.0
            band = assign_band(
                s1_cosine=cosine,
                s3=s3,
                s0_match_field=evidence.s0_matched_field,
                thresholds=self._thresholds,
            )
            evidence = evidence.model_copy(
                update={
                    "s3": s3,
                    "composite_score": composite,
                    "band": band,
                }
            )
        else:
            evidence = evidence.model_copy(
                update={
                    "composite_score": 0.0,
                    "band": ConfidenceBand.LOW,
                }
            )

        return evidence

    def compare_to_baseline(self, metrics: EvaluationMetrics) -> EvaluationGate:
        """Return a gate verdict comparing ``metrics`` to the committed baseline."""
        baseline = self._load_baseline()
        beats_baseline_f1 = metrics.retrieval_f1 > baseline.retrieval_f1
        hard_over_merge_zero = metrics.hard_over_merge_rate == 0.0
        multilingual_under_merge_ok = (
            metrics.multilingual_under_merge_rate
            <= baseline.multilingual_under_merge_rate + 1e-9
        )
        passed = beats_baseline_f1 and hard_over_merge_zero and multilingual_under_merge_ok
        return EvaluationGate(
            beats_baseline_f1=beats_baseline_f1,
            hard_over_merge_zero=hard_over_merge_zero,
            multilingual_under_merge_ok=multilingual_under_merge_ok,
            passed=passed,
        )

    async def threshold_sweep(
        self,
        *,
        model_id: str,
        input_variant: Literal["A", "B"],
        high_cosine_grid: list[float],
        medium_cosine_grid: list[float],
    ) -> ThresholdSweepResult | None:
        """Search for the most conservative thresholds that beat baseline.

        The "most conservative" interval is the one with the highest
        ``high_cosine``, then the highest ``medium_cosine`` (still strictly
        below ``high_cosine``), that satisfies all three gates:

        1. hard over-merge rate == 0
        2. multilingual under-merge rate <= baseline
        3. retrieval F1 > baseline F1
        """
        original_thresholds = self._thresholds

        for high in sorted(high_cosine_grid, reverse=True):
            for medium in sorted(medium_cosine_grid, reverse=True):
                if medium >= high:
                    continue
                self._thresholds = BandThresholds(
                    high_cosine=high,
                    medium_cosine=medium,
                    high_context=original_thresholds.high_context,
                    conflict_floor=original_thresholds.conflict_floor,
                )
                metrics = await self.evaluate(
                    model_id=model_id,
                    input_variant=input_variant,
                )
                gate = self.compare_to_baseline(metrics)
                if gate.passed:
                    return ThresholdSweepResult(
                        high_cosine=high,
                        medium_cosine=medium,
                        metrics=metrics,
                        gate=gate,
                    )

        self._thresholds = original_thresholds
        return None


if __name__ == "__main__":
    # Keep the application module architecture-clean by delegating CLI wiring
    # (which imports infrastructure adapters) to a script outside src/book_graph_rag.
    from scripts.run_evaluation_harness import main

    raise SystemExit(main())
