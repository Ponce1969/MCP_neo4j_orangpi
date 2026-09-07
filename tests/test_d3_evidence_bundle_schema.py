"""Schema validation for the committed OrangePi D3 evidence bundle.

This test is read-only: it parses the evidence bundle that the maintainer
committed after running the OrangePi verification, and asserts the fields
required by design §0.14 / spec D3. It never connects to the OrangePi host.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

BUNDLE_PATH = Path("evidence-bundles/d3-orangepi-verification-2026-09-07.json")


def _load_bundle(path: Path) -> dict[str, Any]:
    """Parse and lightly validate the top-level evidence bundle shape."""
    if not path.exists():
        raise FileNotFoundError(f"D3 evidence bundle not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("D3 evidence bundle must be a JSON object")
    return raw


def test_bundle_exists_and_has_required_top_level_fields() -> None:
    """The committed bundle has the schema version, host, checks, and conclusion."""
    bundle = _load_bundle(BUNDLE_PATH)

    assert bundle.get("schema_version") == "1.0.0"
    assert bundle.get("task")
    assert bundle.get("change") == "phase-3-semantic-entity-resolution"
    assert bundle.get("executed_at")
    assert isinstance(bundle.get("host"), dict)
    assert isinstance(bundle.get("checks"), list)
    assert bundle.get("checks")
    assert isinstance(bundle.get("conclusion"), str)
    assert "D3-VERIFICATION-PASS" in bundle["conclusion"]


def test_bundle_host_has_required_fields() -> None:
    """Host metadata captures arch, Python, uv, repo head/path."""
    bundle = _load_bundle(BUNDLE_PATH)
    host = bundle["host"]

    assert host.get("address")
    assert host.get("arch") == "aarch64"
    assert host.get("python")
    assert host.get("uv")
    assert host.get("repo_head")
    assert host.get("repo_path")


def test_bundle_has_embedding_round_trip_pass() -> None:
    """At least one check is the embedding round-trip and it passed."""
    bundle = _load_bundle(BUNDLE_PATH)
    checks = {c["name"]: c for c in bundle["checks"]}

    assert "embedding round-trip" in checks
    assert checks["embedding round-trip"]["result"] == "pass"


def test_bundle_reports_model_dim_384() -> None:
    """The OrangePi verification recorded the expected 384-dimensional model."""
    bundle = _load_bundle(BUNDLE_PATH)
    model_check = next(
        (c for c in bundle["checks"] if c["name"].startswith("model load")),
        None,
    )
    assert model_check is not None, "missing model load check"
    detail = model_check.get("detail", "")
    match = re.search(r"dim[=:]?\s*(\d+)", detail)
    assert match is not None, f"could not find dim in detail: {detail!r}"
    assert int(match.group(1)) == 384


def test_bundle_has_cosine_sanity_values() -> None:
    """The multilingual sanity check reports positive cosine for related strings."""
    bundle = _load_bundle(BUNDLE_PATH)
    sanity = next(
        (c for c in bundle["checks"] if c["name"].startswith("multilingual sanity")),
        None,
    )
    assert sanity is not None, "missing multilingual sanity check"
    assert sanity["result"] == "pass"
    detail = sanity.get("detail", "")
    cosines = [float(m) for m in re.findall(r"cos\([^)]+\)=(-?\d+\.?\d*)", detail)]
    assert cosines, f"no cosine values found in detail: {detail!r}"
    assert any(c > 0.0 for c in cosines)


def test_bundle_rejects_malformed_bundle(tmp_path: Path) -> None:
    """A bundle missing required fields fails the schema assertions."""
    bad_path = tmp_path / "bad-bundle.json"
    bad_path.write_text(json.dumps({"schema_version": "1.0.0"}), encoding="utf-8")

    bundle = _load_bundle(bad_path)
    with pytest.raises(AssertionError):
        assert bundle.get("checks")
