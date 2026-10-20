"""Tests for ``scripts-ops/recreate_chunk_text_index.py`` (S5 of the phase-2 task).

The recreation is a gated DDL operation: dry-run by default, ``--apply`` needs
an approval file containing ``approve`` and an ``--expect-fingerprint`` that
ties the mutation to the reviewed plan (same contract as
``scripts-ops/resolve_intra_ns.py``). Nothing here touches a graph.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "recreate_chunk_text_index",
    _ROOT / "scripts-ops/recreate_chunk_text_index.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MODULE: ModuleType = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

create_statement = _MODULE.create_statement
build_plan = _MODULE.build_plan
plan_fingerprint = _MODULE.plan_fingerprint
parse_args = _MODULE._parse_args
validate_apply_gates = _MODULE.validate_apply_gates
DECLARED_ANALYZER: str = _MODULE.DECLARED_ANALYZER


def test_create_statement_is_byte_identical_to_ensure_indexes_ddl() -> None:
    """One literal, two owners: the script and the adapter must never drift.

    ``ensure_indexes`` pins the same string in
    ``tests/test_neo4j_query_adapter.py``; if either side changes alone, one
    of the two tests fails — which is what "the repository owns the analyzer"
    means concretely (S5a).
    """
    expected = (
        "CREATE FULLTEXT INDEX chunk_text_index IF NOT EXISTS FOR (n:Chunk) ON EACH [n.text] "
        "OPTIONS {indexConfig: {'fulltext.analyzer': 'standard-no-stop-words'}}"
    )

    assert DECLARED_ANALYZER == "standard-no-stop-words"
    assert create_statement(DECLARED_ANALYZER) == expected


def test_create_statement_rejects_an_analyzer_that_is_not_an_identifier() -> None:
    """The analyzer lands inside DDL text: only ``[a-z0-9-]+`` may pass."""
    with pytest.raises(ValueError, match="analyzer"):
        create_statement("standard; DROP INDEX entity_id")


def test_plan_fingerprint_is_deterministic_and_content_addressed() -> None:
    """Same plan ⇒ same hash; any field the mutation depends on ⇒ different hash."""
    base = {"analyzer": DECLARED_ANALYZER, "chunk_count": 3468, "index_state": "absent"}
    plan_a = build_plan(**base)
    plan_b = build_plan(**base)
    plan_other_count = build_plan(
        analyzer=DECLARED_ANALYZER, chunk_count=3469, index_state="absent"
    )
    plan_other_analyzer = build_plan(analyzer="simple", chunk_count=3468, index_state="absent")

    assert plan_fingerprint(plan_a) == plan_fingerprint(plan_b)
    assert plan_fingerprint(plan_a) != plan_fingerprint(plan_other_count)
    assert plan_fingerprint(plan_a) != plan_fingerprint(plan_other_analyzer)


def test_dry_run_is_the_default() -> None:
    """No flags ⇒ read-only. A mutating run must ask for it explicitly."""
    args = parse_args([])

    assert args.apply is False


def test_apply_requires_an_approval_file_containing_approve(tmp_path: Path) -> None:
    """The gated-operation word gate, mirrored from resolve_intra_ns."""
    args = argparse.Namespace(approval=None, expect_fingerprint=None)
    with pytest.raises(SystemExit, match="approval"):
        validate_apply_gates(args, computed="abc")

    refused = tmp_path / "approval.txt"
    refused.write_text("listo, dale", encoding="utf-8")
    args.approval = refused
    with pytest.raises(SystemExit, match="approve"):
        validate_apply_gates(args, computed="abc")


def test_apply_requires_the_reviewed_fingerprint(tmp_path: Path) -> None:
    """Missing or mismatched ``--expect-fingerprint`` aborts — rejected, not warned."""
    approval = tmp_path / "approval.txt"
    approval.write_text("approve", encoding="utf-8")
    args = argparse.Namespace(approval=approval, expect_fingerprint=None)
    with pytest.raises(SystemExit, match="expect-fingerprint"):
        validate_apply_gates(args, computed="abc")

    args.expect_fingerprint = "different"
    with pytest.raises(SystemExit, match="abc"):
        validate_apply_gates(args, computed="abc")

    args.expect_fingerprint = "abc"
    validate_apply_gates(args, computed="abc")  # accepted: no raise
