"""Seed the T9a cross-namespace decision registry from evidence (no graph).

The append-only registry (``Settings.cross_namespace_decisions_path``) answers
"did a human already judge this pair?" for R5a and for the consolidated
retro-audit. This seeder backfills it from the two evidence sources that
already exist, without ever connecting to Neo4j:

* ``separate`` — derived MECHANICALLY from the merge ledger's COMPENSATING
  entries (``rollback_of`` set). For each compensating entry, one record per
  ``candidate_id`` it records, keyed by the ORIGINAL seq (``rollback_of``),
  with the canonical read from the original entry, a reason naming the
  compensating seq (the human-approved rollback IS the record of the
  separation) and ``batch = "backfill-rollbacks"``.
* ``keep`` — authored from ``--keep-evidence``
  (default ``evidence-bundles/lote2b-decisions-keep-20261004.json``, schema
  ``cross-namespace-keep-decisions/1``): the eight pairs the maintainer kept
  merged in review batches 2A / 2B-A. The ids in that file ARE the evidence
  (they match the batch decision sheets committed alongside it).

Safety contract:

* EVERY record is validated against the merge ledger BEFORE anything is
  appended — the seq must exist, the candidate must be one of that entry's
  ``candidate_ids`` and the canonical must equal the entry's canonical. Any
  failure prints every problem and exits non-zero with NOTHING appended.
* ``--dry-run`` is the default: the plan is printed grouped by decision
  kind and no file is touched. ``--apply`` appends through the
  shared ``JSONLCrossNamespaceDecisions`` adapter, whose SEMANTIC no-op makes
  a re-run safe — a second ``--apply`` reports ``no changes`` and writes no
  second line.
* Read-only on the graph: this script never imports a Neo4j driver.

Usage (from the repo root):
    uv run python scripts-ops/seed_cross_namespace_decisions.py
    uv run python scripts-ops/seed_cross_namespace_decisions.py --apply
    uv run python scripts-ops/seed_cross_namespace_decisions.py \\
        --ledger /path/to/merge_ledger.jsonl --decisions /path/to/decisions.jsonl --apply
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from book_graph_rag.config import Settings
from book_graph_rag.domain.cross_namespace_decision_models import (
    CrossNamespaceDecision,
    DecisionKind,
)
from book_graph_rag.domain.merge_ledger_models import MergeLedgerEntry
from book_graph_rag.infrastructure.jsonl_cross_namespace_decisions import (
    JSONLCrossNamespaceDecisions,
)
from book_graph_rag.infrastructure.jsonl_merge_ledger import JSONLMergeLedger

#: Documented schema of the authored keep-evidence JSON.
KEEP_EVIDENCE_SCHEMA = "cross-namespace-keep-decisions/1"
#: The eight 2A / 2B-A kept pairs live here (the review verdicts' evidence).
DEFAULT_KEEP_EVIDENCE = Path("evidence-bundles/lote2b-decisions-keep-20261004.json")
#: Batch label for the mechanically derived ``separate`` records.
SEPARATE_BATCH = "backfill-rollbacks"
#: Reviewer stamp for derived records: the seeder writes them, the human
#: decision is the approved rollback itself (reason names its seq).
ROLLBACK_DERIVED_REVIEWER = "seed:backfill-rollbacks"


class SeedError(Exception):
    """Evidence that cannot be turned into decisions (fail closed, exit 2)."""


class KeepEvidenceRow(BaseModel):
    """One authored keep pair: the reviewed ids plus the verdict wording."""

    model_config = ConfigDict(extra="forbid")

    seq: int
    canonical_id: str
    candidate_id: str
    reason: str
    batch: str | None = None


class KeepEvidenceFile(BaseModel):
    """The documented keep-evidence JSON (schema ``cross-namespace-keep-decisions/1``).

    Top level: ``schema`` (this schema id), ``decided_by`` / ``decided_at``
    (the review verdict's stamp, shared by every record) and ``decisions``
    (the eight reviewed pairs). Extra keys are refused so a typo cannot be
    silently dropped.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    evidence_schema: str = Field(alias="schema")
    decided_by: str
    decided_at: datetime
    decisions: list[KeepEvidenceRow]
    source: str | None = None


def derive_separate_decisions(entries: Sequence[MergeLedgerEntry]) -> list[CrossNamespaceDecision]:
    """One ``separate`` record per candidate a COMPENSATING entry reverted.

    ``seq`` is the ORIGINAL entry (``rollback_of``), the canonical is read
    from that original (never from the compensator), the reason names the
    compensating seq — the rollback is the record of the separation — and
    ``batch`` is ``backfill-rollbacks``. ``decided_at`` is the compensating
    entry's ``applied_at`` (evidence-derived, deterministic across runs).
    """
    by_seq = {entry.seq: entry for entry in entries}
    records: list[CrossNamespaceDecision] = []
    for entry in entries:
        if entry.rollback_of is None:
            continue
        original = by_seq.get(entry.rollback_of)
        if original is None:
            raise SeedError(
                f"compensating entry seq={entry.seq} references unknown original "
                f"seq={entry.rollback_of}"
            )
        for candidate_id in entry.candidate_ids:
            records.append(
                CrossNamespaceDecision(
                    seq=entry.rollback_of,
                    candidate_id=candidate_id,
                    canonical_id=original.canonical_id,
                    decision=DecisionKind.SEPARATE,
                    decided_by=ROLLBACK_DERIVED_REVIEWER,
                    decided_at=entry.applied_at,
                    reason=(
                        "separated by merge-ledger rollback: compensating entry "
                        f"seq={entry.seq} records the reversal"
                    ),
                    batch=SEPARATE_BATCH,
                )
            )
    return records


def load_keep_decisions(path: Path) -> list[CrossNamespaceDecision]:
    """Parse the authored keep-evidence JSON into ``keep`` decision records."""
    if not path.exists():
        raise SeedError(f"keep evidence file not found: {path}")
    try:
        payload = KeepEvidenceFile.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise SeedError(
            f"keep evidence file {path} does not match {KEEP_EVIDENCE_SCHEMA}: {exc}"
        ) from exc
    if payload.evidence_schema != KEEP_EVIDENCE_SCHEMA:
        raise SeedError(
            f"keep evidence file {path} declares schema {payload.evidence_schema!r}, "
            f"expected {KEEP_EVIDENCE_SCHEMA!r}"
        )
    if not payload.decisions:
        raise SeedError(f"keep evidence file {path} carries no decisions")
    return [
        CrossNamespaceDecision(
            seq=row.seq,
            candidate_id=row.candidate_id,
            canonical_id=row.canonical_id,
            decision=DecisionKind.KEEP,
            decided_by=payload.decided_by,
            decided_at=payload.decided_at,
            reason=row.reason,
            batch=row.batch,
        )
        for row in payload.decisions
    ]


def validate_against_ledger(
    records: Sequence[CrossNamespaceDecision],
    by_seq: dict[int, MergeLedgerEntry],
) -> list[str]:
    """Every ledger precondition of EVERY record; ``[]`` means safe to append.

    Checked in order per record: the seq exists in the ledger, the candidate
    is one of that entry's ``candidate_ids`` and the canonical equals the
    entry's canonical. All problems are collected (never fail on the first)
    so one run reports everything that needs fixing.
    """
    problems: list[str] = []
    for record in records:
        entry = by_seq.get(record.seq)
        if entry is None:
            problems.append(f"seq {record.seq} ({record.candidate_id}): not in the merge ledger")
            continue
        if record.candidate_id not in entry.candidate_ids:
            problems.append(
                f"seq {record.seq}: candidate {record.candidate_id} is not a candidate "
                f"of the entry (candidates: {', '.join(entry.candidate_ids)})"
            )
            continue
        if record.canonical_id != entry.canonical_id:
            problems.append(
                f"seq {record.seq}: canonical {record.canonical_id} does not match the "
                f"ledger entry canonical {entry.canonical_id}"
            )
    return problems


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="merge ledger path (default: merge_ledger_path de Settings)",
    )
    parser.add_argument(
        "--decisions",
        type=Path,
        default=None,
        help="decision registry path (default: cross_namespace_decisions_path de Settings)",
    )
    parser.add_argument(
        "--keep-evidence",
        type=Path,
        default=DEFAULT_KEEP_EVIDENCE,
        help=f"authored keep-evidence JSON (default: {DEFAULT_KEEP_EVIDENCE})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="append the records through the JSONL adapter (default: dry-run)",
    )
    return parser.parse_args(argv)


def _print_group(
    kind: DecisionKind, records: Sequence[CrossNamespaceDecision], source: str
) -> None:
    """One dry-run block per decision kind (the plan, never a write)."""
    print(f"{kind.value} ({len(records)}): {source}")
    for record in records:
        print(
            f"  seq {record.seq} | {record.canonical_id} -> {record.candidate_id} "
            f"| batch {record.batch} | {record.reason}"
        )


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)

    ledger_path: Path | None = args.ledger
    decisions_path: Path | None = args.decisions
    if ledger_path is None or decisions_path is None:
        # Only touch Settings when a path was not given explicitly (tests
        # and hermetic runs pass every path and never load the environment).
        settings = Settings.model_validate({})
        ledger_path = ledger_path if ledger_path is not None else settings.merge_ledger_path
        decisions_path = (
            decisions_path
            if decisions_path is not None
            else settings.cross_namespace_decisions_path
        )

    entries = JSONLMergeLedger(ledger_path).read_all()
    by_seq = {entry.seq: entry for entry in entries}

    try:
        separates = derive_separate_decisions(entries)
        keeps = load_keep_decisions(args.keep_evidence)
    except SeedError as exc:
        print(f"ERROR: seed aborted, nothing appended: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    records = [*separates, *keeps]
    problems = validate_against_ledger(records, by_seq)
    if problems:
        print("ERROR: seed aborted, nothing appended:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        raise SystemExit(2)

    if not args.apply:
        print("-- DRY RUN (default): nothing appended; re-run with --apply --")
        print(f"  ledger: {ledger_path} · registry: {decisions_path}")
        _print_group(
            DecisionKind.SEPARATE,
            separates,
            "derived from the merge-ledger compensating entries",
        )
        _print_group(DecisionKind.KEEP, keeps, f"from {args.keep_evidence}")
        return

    store = JSONLCrossNamespaceDecisions(decisions_path)
    appended = {DecisionKind.SEPARATE: 0, DecisionKind.KEEP: 0}
    for record in records:
        if store.append(record):
            appended[record.decision] += 1
    total = sum(appended.values())
    if total == 0:
        print(f"no changes: all {len(records)} decisions already recorded ({decisions_path})")
        return
    print(
        f"appended {total} of {len(records)} decisions: "
        f"separate {appended[DecisionKind.SEPARATE]} · keep {appended[DecisionKind.KEEP]} "
        f"-> {decisions_path}"
    )


if __name__ == "__main__":
    main()
