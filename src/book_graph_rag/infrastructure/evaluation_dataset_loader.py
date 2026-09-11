"""Infrastructure adapter for loading evaluation datasets from MANIFEST.json."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from book_graph_rag.domain.evaluation_models import (
    DatasetLoadError,
    DatasetManifestMismatch,
    EvaluationDataset,
    EvaluationDatasetManifest,
    UnknownDatasetError,
)
from book_graph_rag.ports.evaluation_dataset_port import EvaluationDatasetPort


class JsonlManifestEvaluationDatasetLoader(EvaluationDatasetPort):
    """Load evaluation datasets from a MANIFEST.json with sha256 validation."""

    def __init__(self, manifest_path: Path) -> None:
        self._manifest_path = manifest_path
        self._manifest_dir = manifest_path.parent

    def _load_manifest(self) -> EvaluationDatasetManifest:
        try:
            text = self._manifest_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DatasetLoadError(
                f"cannot read manifest {self._manifest_path}: {exc}"
            ) from exc
        try:
            return EvaluationDatasetManifest.model_validate_json(text)
        except Exception as exc:
            raise DatasetLoadError(
                f"malformed manifest {self._manifest_path}: {exc}"
            ) from exc

    def _find_record(self, manifest: EvaluationDatasetManifest, dataset_id: str) -> Any:
        for record in manifest.datasets:
            if record.id == dataset_id:
                return record
        raise UnknownDatasetError(dataset_id)

    def _read_records(self, data_path: Path) -> tuple[dict[str, Any], ...]:
        suffix = data_path.suffix.lower()
        try:
            if suffix in {".jsonl"}:
                records: list[dict[str, Any]] = []
                for line in data_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    records.append(json.loads(line))
                return tuple(records)
            if suffix in {".yaml", ".yml"}:
                raw = yaml.safe_load(data_path.read_bytes())
                if not isinstance(raw, list):
                    raise DatasetLoadError(f"YAML dataset {data_path} must be a list")
                return tuple(raw)
        except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
            raise DatasetLoadError(f"cannot parse dataset {data_path}: {exc}") from exc
        raise DatasetLoadError(f"unsupported dataset format: {data_path}")

    @staticmethod
    def _record_key(record: dict[str, Any]) -> str:
        for key in ("id", "question_id"):
            value = record.get(key)
            if value:
                return f"{key}:{value}"
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"))
        return f"content:{hashlib.sha256(payload.encode()).hexdigest()}"

    def load(self, dataset_id: str) -> EvaluationDataset:
        manifest = self._load_manifest()
        record = self._find_record(manifest, dataset_id)
        data_path = self._manifest_dir / record.file
        if not data_path.exists():
            raise DatasetLoadError(f"dataset file not found: {data_path}")
        actual_sha = hashlib.sha256(data_path.read_bytes()).hexdigest()
        if actual_sha != record.sha256:
            raise DatasetManifestMismatch(
                dataset_id=dataset_id,
                expected=record.sha256,
                actual=actual_sha,
            )
        records = self._read_records(data_path)
        seen: set[str] = set()
        for item in records:
            key = self._record_key(item)
            if key in seen:
                raise DatasetLoadError(
                    f"dataset {dataset_id} contains duplicate record key: {key}"
                )
            seen.add(key)
        return EvaluationDataset(dataset_id=dataset_id, records=records)

    def list_datasets(self) -> tuple[str, ...]:
        manifest = self._load_manifest()
        return tuple(r.id for r in manifest.datasets)
