"""Baseline harness for the existing slug+token resolver.

Captures the current resolver's metrics on the labeled dataset before any
hybrid pipeline work.  The report is the comparison floor for slices C–F.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import yaml
from scripts.resolve_entities import name_similarity

from book_graph_rag.domain.dataset_models import DatasetManifest, LabeledPair
from book_graph_rag.domain.evaluation_models import BaselineReport


class BaselineHarness:
    """Evaluate the existing slug+token resolver on a labeled dataset."""

    def __init__(
        self,
        dataset_path: Path,
        manifest_path: Path,
        output_path: Path,
        threshold: float = 0.9,
    ) -> None:
        self._dataset_path = dataset_path
        self._manifest_path = manifest_path
        self._output_path = output_path
        self._threshold = threshold
        self._manifest: DatasetManifest | None = None

    def _load_manifest(self) -> DatasetManifest:
        manifest = DatasetManifest.model_validate_json(
            self._manifest_path.read_text(encoding="utf-8")
        )
        manifest.require_hash_match(self._dataset_path.read_bytes())
        return manifest

    def _load_pairs(self) -> list[LabeledPair]:
        raw = yaml.safe_load(self._dataset_path.read_bytes()) or []
        return [LabeledPair.model_validate(item) for item in raw]

    def _write_fixture_files(self, pairs: list[LabeledPair]) -> None:
        """Test helper: materialize a temporary pairs.yaml + manifest.json."""
        self._dataset_path.parent.mkdir(parents=True, exist_ok=True)
        self._dataset_path.write_text(
            yaml.dump(
                [pair.model_dump(mode="json") for pair in pairs],
                sort_keys=False,
                allow_unicode=True,
                line_break="\n",
            ),
            encoding="utf-8",
        )
        content = self._dataset_path.read_bytes()
        manifest = DatasetManifest(
            pairs_file_sha256=__import__("hashlib").sha256(content).hexdigest(),
            pair_count=len(pairs),
            same_count=sum(1 for p in pairs if p.label.value == "same"),
            different_count=sum(1 for p in pairs if p.label.value == "different"),
            hard_count=sum(1 for p in pairs if p.hard),
            multilingual_count=sum(1 for p in pairs if p.multilingual),
        )
        self._manifest_path.write_text(
            json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        self._manifest = manifest

    @staticmethod
    def _metrics(
        pairs: list[LabeledPair],
        predictions: list[Literal["same", "different"]],
    ) -> tuple[float, float, float]:
        tp = sum(
            1
            for pair, pred in zip(pairs, predictions, strict=True)
            if pair.label.value == "same" and pred == "same"
        )
        fp = sum(
            1
            for pair, pred in zip(pairs, predictions, strict=True)
            if pair.label.value == "different" and pred == "same"
        )
        fn = sum(
            1
            for pair, pred in zip(pairs, predictions, strict=True)
            if pair.label.value == "same" and pred == "different"
        )
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )
        return precision, recall, f1

    @staticmethod
    def _subset_metrics(
        pairs: list[LabeledPair],
        predictions: list[Literal["same", "different"]],
        *,
        in_subset: Callable[[LabeledPair], bool],
    ) -> tuple[float, float, float]:
        subset_pairs = [p for p in pairs if in_subset(p)]
        subset_preds = [pred for p, pred in zip(pairs, predictions, strict=True) if in_subset(p)]
        return BaselineHarness._metrics(subset_pairs, subset_preds)

    def run(self) -> BaselineReport:
        """Run the slug+token resolver on the dataset and return a report."""
        self._manifest = self._load_manifest()
        pairs = self._load_pairs()
        predictions: list[Literal["same", "different"]] = []
        for pair in pairs:
            score = name_similarity(pair.entity_a.name, pair.entity_b.name)
            predictions.append("same" if score >= self._threshold else "different")

        precision, recall, f1 = self._metrics(pairs, predictions)
        _, _, multilingual_f1 = self._subset_metrics(
            pairs, predictions, in_subset=lambda p: p.multilingual
        )
        multilingual_same_count = sum(
            1 for p in pairs if p.multilingual and p.label.value == "same"
        )
        multilingual_fn = sum(
            1
            for p, pred in zip(pairs, predictions, strict=True)
            if p.multilingual and p.label.value == "same" and pred == "different"
        )
        multilingual_under_merge_rate = (
            multilingual_fn / multilingual_same_count if multilingual_same_count > 0 else 0.0
        )
        hard_fp = sum(
            1
            for p, pred in zip(pairs, predictions, strict=True)
            if p.hard and p.label.value == "different" and pred == "same"
        )
        hard_count = sum(1 for p in pairs if p.hard)
        hard_over_merge_rate = hard_fp / hard_count if hard_count > 0 else 0.0

        report = BaselineReport(
            retrieval_precision=precision,
            retrieval_recall=recall,
            retrieval_f1=f1,
            multilingual_under_merge_rate=multilingual_under_merge_rate,
            multilingual_f1=multilingual_f1,
            hard_over_merge_rate=hard_over_merge_rate,
            dataset_manifest_sha256=self._manifest.pairs_file_sha256,
        )

        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._output_path.write_text(
            json.dumps(report.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the slug+token baseline report")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("tests/fixtures/resolution/pairs.yaml"),
        help="Path to pairs.yaml",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("tests/fixtures/resolution/manifest.json"),
        help="Path to manifest.json",
    )
    parser.add_argument(
        "--emit",
        type=Path,
        default=Path("tests/fixtures/resolution/baseline_report.json"),
        help="Path to write baseline_report.json",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.9,
        help="Name-similarity threshold for the baseline resolver",
    )
    args = parser.parse_args(argv)

    harness = BaselineHarness(
        dataset_path=args.dataset,
        manifest_path=args.manifest,
        output_path=args.emit,
        threshold=args.threshold,
    )
    report = harness.run()
    print(f"baseline F1={report.retrieval_f1:.4f} report written to {args.emit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
