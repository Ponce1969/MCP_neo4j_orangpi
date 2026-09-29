"""Validate the Unit 0 namespace-routing dataset and manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

_ROUTE_KINDS = frozenset({"single", "multi", "abstain", "out_of_domain"})
_REQUIRED_FIELDS = frozenset(
    {
        "question_id",
        "question",
        "language",
        "route_kind",
        "ambiguity",
        "expected_namespaces",
        "provenance",
    }
)


class NamespaceRoutingDatasetError(ValueError):
    """Raised when a routing dataset violates its published contract."""


def _active_namespaces(catalog_path: Path) -> set[str]:
    """Return active ``corpus:source`` ids from the versioned catalog."""
    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("corpora"), dict):
        raise NamespaceRoutingDatasetError("catalog must contain a corpora mapping")

    namespaces: set[str] = set()
    for corpus, corpus_data in raw["corpora"].items():
        if not isinstance(corpus, str) or not isinstance(corpus_data, dict):
            raise NamespaceRoutingDatasetError("catalog contains an invalid corpus entry")
        sources = corpus_data.get("sources")
        if not isinstance(sources, dict):
            raise NamespaceRoutingDatasetError(f"catalog corpus {corpus!r} has no sources")
        for source, source_data in sources.items():
            if not isinstance(source, str) or not isinstance(source_data, dict):
                raise NamespaceRoutingDatasetError("catalog contains an invalid source entry")
            if source_data.get("status") == "active":
                namespaces.add(f"{corpus}:{source}")
    return namespaces


def _load_records(dataset_path: Path) -> list[dict[str, Any]]:
    """Parse and validate record shape and route cardinality."""
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(dataset_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise NamespaceRoutingDatasetError(
                f"invalid JSON at line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(record, dict):
            raise NamespaceRoutingDatasetError(f"record at line {line_number} must be an object")
        missing = _REQUIRED_FIELDS - record.keys()
        if missing:
            raise NamespaceRoutingDatasetError(
                f"record at line {line_number} is missing fields: {sorted(missing)}"
            )
        record_id = record["question_id"]
        if not isinstance(record_id, str) or not record_id:
            raise NamespaceRoutingDatasetError(f"record at line {line_number} has an invalid id")
        if record_id in seen_ids:
            raise NamespaceRoutingDatasetError(f"duplicate question_id: {record_id}")
        seen_ids.add(record_id)
        route_kind = record["route_kind"]
        if route_kind not in _ROUTE_KINDS:
            raise NamespaceRoutingDatasetError(f"unknown route_kind: {route_kind!r}")
        namespaces = record["expected_namespaces"]
        if not isinstance(namespaces, list) or any(
            not isinstance(namespace, str) or not namespace for namespace in namespaces
        ):
            raise NamespaceRoutingDatasetError(
                f"record {record_id} expected_namespaces must be a list of strings"
            )
        expected_count = {
            "single": 1,
            "multi": 2,
            "abstain": 0,
            "out_of_domain": 0,
        }[route_kind]
        if (route_kind == "multi" and len(namespaces) < expected_count) or (
            route_kind != "multi" and len(namespaces) != expected_count
        ):
            raise NamespaceRoutingDatasetError(
                f"record {record_id} has invalid namespace count for {route_kind!r}"
            )
        records.append(record)
    if not records:
        raise NamespaceRoutingDatasetError("dataset must contain at least one record")
    return records


def validate_dataset(
    dataset_path: Path,
    catalog_path: Path,
    manifest_path: Path,
) -> tuple[int, str]:
    """Validate records, active namespaces, and the manifest binding."""
    records = _load_records(dataset_path)
    active = _active_namespaces(catalog_path)
    unknown = sorted(
        {
            namespace
            for record in records
            for namespace in record["expected_namespaces"]
            if namespace not in active
        }
    )
    if unknown:
        raise NamespaceRoutingDatasetError(
            f"dataset references inactive or unknown namespaces: {unknown}"
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    datasets = manifest.get("datasets") if isinstance(manifest, dict) else None
    if not isinstance(datasets, list):
        raise NamespaceRoutingDatasetError("manifest must contain a datasets list")
    entry = next(
        (
            item
            for item in datasets
            if isinstance(item, dict) and item.get("id") == "namespace_routing_dataset"
        ),
        None,
    )
    if entry is None:
        raise NamespaceRoutingDatasetError("manifest entry namespace_routing_dataset is missing")
    digest = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    if entry.get("file") != dataset_path.name:
        raise NamespaceRoutingDatasetError("manifest dataset file does not match")
    if entry.get("sha256") != digest:
        raise NamespaceRoutingDatasetError("manifest SHA-256 does not match dataset")
    if entry.get("record_count") != len(records):
        raise NamespaceRoutingDatasetError("manifest record_count does not match dataset")
    return len(records), digest


def main() -> int:
    """Validate the default Unit 0 artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("data/evaluation/namespace_routing_dataset.jsonl"),
    )
    parser.add_argument("--catalog", type=Path, default=Path("catalog.yaml"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/evaluation/MANIFEST.json"),
    )
    args = parser.parse_args()
    try:
        count, digest = validate_dataset(args.dataset, args.catalog, args.manifest)
    except (OSError, NamespaceRoutingDatasetError, json.JSONDecodeError, yaml.YAMLError) as exc:
        parser.error(str(exc))
    print(f"validated {count} namespace-routing records; sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
