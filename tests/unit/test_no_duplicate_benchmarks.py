"""Regression guard against misleading duplicate benchmark files (R10)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

BENCHMARK_DIR = Path("docs/benchmarks")
CANONICAL_BASELINE = Path("data/evaluation/generation_baseline.json")


def _tracked_benchmark_json_files() -> list[Path]:
    """Return tracked JSON files under ``docs/benchmarks``.

    Using ``git ls-files`` keeps the guard focused on committed benchmark
    artifacts; untracked ad-hoc script outputs are not treated as duplicates.
    """
    result = subprocess.run(
        ["git", "ls-files", "docs/benchmarks/*.json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [Path(line) for line in result.stdout.splitlines() if line]


def test_no_identical_before_after_pair() -> None:
    """No two benchmark files may present identical content as a comparison pair."""
    files = _tracked_benchmark_json_files()
    seen: dict[str, Path] = {}
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen:
            pytest.fail(
                f"Benchmark files have identical content: {seen[digest]} and {path}"
            )
        seen[digest] = path


def test_gr3_after_removed_or_distinct() -> None:
    """The misleading gr3_after.json duplicate must not remain identical to gr3_baseline."""
    after = BENCHMARK_DIR / "gr3_after.json"
    baseline = BENCHMARK_DIR / "gr3_baseline.json"

    if after.exists() and baseline.exists():
        assert (
            after.read_bytes() != baseline.read_bytes()
        ), "gr3_after.json is identical to gr3_baseline.json"

    assert not after.exists(), (
        "docs/benchmarks/gr3_after.json is a misleading duplicate and must be removed"
    )


def test_generation_baseline_is_canonical() -> None:
    """The canonical generation baseline lives in data/evaluation/ with provenance."""
    assert CANONICAL_BASELINE.exists(), f"Canonical baseline missing: {CANONICAL_BASELINE}"

    data = json.loads(CANONICAL_BASELINE.read_text(encoding="utf-8"))

    required_keys = {
        "schema_version",
        "layer",
        "dataset_id",
        "dataset_sha256",
        "committed_at",
        "code_commit",
        "model_ids",
        "metrics",
        "thresholds_finalized",
    }
    missing = required_keys - set(data.keys())
    assert not missing, f"Canonical baseline missing provenance keys: {missing}"

    assert data["layer"] == "generation"
    assert data["dataset_id"], "dataset_id must be populated"
    assert data["dataset_sha256"], "dataset_sha256 must be populated"
    assert data["code_commit"], "code_commit must be populated"
    assert data["model_ids"], "model_ids must be populated"
    assert isinstance(data["metrics"], dict), "metrics must be a dict"
    assert data["metrics"], "metrics must be populated"
