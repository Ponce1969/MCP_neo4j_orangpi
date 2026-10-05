"""Seeder tests for the T9a decision registry (TDD, RED first).

``scripts-ops/seed_cross_namespace_decisions.py`` backfills the append-only
decision registry (``Settings.cross_namespace_decisions_path``) from two
evidence sources, without ever touching Neo4j:

* **separate** — derived mechanically from the merge ledger's COMPENSATING
  entries (``rollback_of`` set): one record per ``candidate_id`` the
  compensating entry records, keyed by the ORIGINAL seq (``rollback_of``),
  with the canonical read from the original entry, a reason naming the
  compensating seq (the rollback IS the record of the separation) and
  ``batch = "backfill-rollbacks"``;
* **keep** — authored from ``evidence-bundles/lote2b-decisions-keep-20261004.json``
  (the eight pairs the maintainer kept merged in batches 2A / 2B-A; the ids
  are the evidence and are pinned here verbatim).

Contract under test:

* the derived ``separate`` records match the compensating entries, including
  a multi-candidate entry (one record per candidate) and the canonical being
  read from the ORIGINAL entry;
* the keep evidence JSON parses and its eight records validate against a
  ledger carrying exactly the reviewed entries;
* validation runs over EVERY record BEFORE any append: a wrong canonical or
  a non-candidate aborts with a non-zero exit and NOTHING appended;
* ``--dry-run`` is the default (prints grouped by decision kind, writes
  nothing) and ``--apply`` twice is idempotent (the JSONL adapter's semantic
  no-op means no second line and a "no changes" report).

No database, no production ledger, no production decisions file: everything
runs over ``tmp_path`` ledgers and the committed evidence JSON.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from book_graph_rag.domain.cross_namespace_decision_models import DecisionKind
from book_graph_rag.domain.merge_ledger_models import MergeBand, MergeLedgerEntry
from book_graph_rag.infrastructure.jsonl_merge_ledger import JSONLMergeLedger

_ROOT = Path(__file__).parents[2]
_SCRIPT_PATH = _ROOT / "scripts-ops" / "seed_cross_namespace_decisions.py"
_KEEP_EVIDENCE_PATH = _ROOT / "evidence-bundles" / "lote2b-decisions-keep-20261004.json"

#: Documented schema of the authored keep-evidence JSON.
_KEEP_SCHEMA = "cross-namespace-keep-decisions/1"

#: The eight kept pairs exactly as the batch 2A / 2B-A verdicts record them
#: (seq, canonical_id, candidate_id, batch) — the ids ARE the evidence.
_EXPECTED_KEEPS: tuple[tuple[int, str, str, str], ...] = (
    (
        413,
        "knowledge:graphrag-agentic:databases-component",
        "knowledge:agentic-architectural-patterns:databases-component",
        "2A",
    ),
    (
        475,
        "knowledge:graphrag-agentic:microservices-concept",
        "knowledge:agentic-architectural-patterns:microservices-concept",
        "2A",
    ),
    (
        569,
        "knowledge:graphrag-agentic:graphrag-concept",
        "knowledge:essential-graphrag:graph-retrieval-augmented-generation-concept",
        "2B-A",
    ),
    (
        572,
        "knowledge:graphrag-agentic:hallucination-risk",
        "knowledge:essential-graphrag:hallucination-risk",
        "2B-A",
    ),
    (
        351,
        "knowledge:graphrag-agentic:chatgpt-tool",
        "knowledge:essential-graphrag:chatgpt-tool",
        "2B-A",
    ),
    (
        494,
        "knowledge:graphrag-agentic:policy-concept",
        "knowledge:agentic-architectural-patterns:policy-concept",
        "2B-A",
    ),
    (
        573,
        "knowledge:graphrag-agentic:instructions-concept",
        "knowledge:essential-graphrag:instructions-concept",
        "2B-A",
    ),
    (
        565,
        "knowledge:graphrag-agentic:extract-entities-component",
        "knowledge:essential-graphrag:extract-entities-component",
        "2B-A",
    ),
)


def _load_script() -> ModuleType:
    """Import the ops script as a module (no ledger, no graph, no CLI args)."""
    spec = importlib.util.spec_from_file_location("seed_cross_namespace_decisions", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["seed_cross_namespace_decisions"] = module
    spec.loader.exec_module(module)
    return module


def _entry(
    seq: int,
    candidate_ids: list[str],
    canonical_id: str,
    *,
    rollback_of: int | None = None,
    applied_at: datetime | None = None,
) -> MergeLedgerEntry:
    return MergeLedgerEntry(
        seq=seq,
        candidate_ids=list(candidate_ids),
        canonical_id=canonical_id,
        band=MergeBand.EXACT,
        evidence=[],
        aliases_folded=[],
        edge_inverse_map=[],
        approver="auto:rollback" if rollback_of is not None else "auto:bypass",
        applied_at=applied_at or datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC),
        rollback_of=rollback_of,
    )


def _write_ledger(path: Path, entries: list[MergeLedgerEntry]) -> None:
    ledger = JSONLMergeLedger(path)
    for entry in entries:
        ledger.append(entry)


def _write_keep_evidence(
    path: Path,
    rows: list[dict[str, object]],
    *,
    schema: str = _KEEP_SCHEMA,
    decided_by: str = "human:maintainer",
    decided_at: str = "2026-10-04T00:00:00+00:00",
) -> None:
    payload = {
        "schema": schema,
        "decided_by": decided_by,
        "decided_at": decided_at,
        "decisions": rows,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# ── derived ``separate`` records ─────────────────────────────────────────────


def test_derived_separate_records_match_the_compensating_entries() -> None:
    """One record per candidate the compensating entry records, key = original.

    The multi-candidate compensating entry yields two records; the canonical
    comes from the ORIGINAL entry (proven here by giving one compensating
    entry a divergent canonical — the derived record must still carry the
    original's).
    """
    module = _load_script()
    applied = datetime(2026, 10, 4, 16, 26, 18, tzinfo=UTC)
    original_a = _entry(
        1, ["ns-b:src:tool-concept", "ns-c:src:tool-concept"], "ns-a:src:tool-concept"
    )
    compensating_a = _entry(
        11,
        ["ns-b:src:tool-concept", "ns-c:src:tool-concept"],
        "ns-a:src:tool-concept",
        rollback_of=1,
        applied_at=applied,
    )
    original_b = _entry(2, ["ns-b:src:other-concept"], "ns-a:src:other-concept")
    compensating_b = _entry(
        12,
        ["ns-b:src:other-concept"],
        "ns-z:src:divergent-canonical",  # the original is the source of truth
        rollback_of=2,
    )

    records = module.derive_separate_decisions(
        [original_a, compensating_a, original_b, compensating_b]
    )

    assert len(records) == 3, "one record per compensating candidate; originals derive nothing"
    by_key = {(record.seq, record.candidate_id): record for record in records}
    assert set(by_key) == {
        (1, "ns-b:src:tool-concept"),
        (1, "ns-c:src:tool-concept"),
        (2, "ns-b:src:other-concept"),
    }
    for record in records:
        assert record.decision is DecisionKind.SEPARATE
        assert record.batch == "backfill-rollbacks"
    # The reason names the compensating seq that records the separation.
    assert "seq=11" in by_key[(1, "ns-b:src:tool-concept")].reason
    assert "seq=11" in by_key[(1, "ns-c:src:tool-concept")].reason
    assert "seq=12" in by_key[(2, "ns-b:src:other-concept")].reason
    # Canonical read from the ORIGINAL entry, never from the compensator.
    assert by_key[(2, "ns-b:src:other-concept")].canonical_id == "ns-a:src:other-concept"
    # decided_at is the compensating entry's applied_at (evidence-derived).
    assert by_key[(1, "ns-b:src:tool-concept")].decided_at == applied
    # All derived records pass their own ledger validation.
    by_seq = {entry.seq: entry for entry in (original_a, compensating_a, original_b)}
    assert module.validate_against_ledger(records, by_seq) == []


# ── keep evidence JSON ───────────────────────────────────────────────────────


def test_the_keep_evidence_json_parses_and_validates_its_eight_records() -> None:
    """The authored evidence file carries exactly the eight reviewed pairs."""
    module = _load_script()
    payload = json.loads(_KEEP_EVIDENCE_PATH.read_text(encoding="utf-8"))
    assert payload["schema"] == _KEEP_SCHEMA
    assert len(payload["decisions"]) == 8, "the eight 2A/2B-A kept pairs are the evidence"

    records = module.load_keep_decisions(_KEEP_EVIDENCE_PATH)

    assert len(records) == 8
    # The ledger side: one original entry per reviewed pair, ids verbatim.
    by_seq = {
        seq: _entry(seq, [candidate_id], canonical_id)
        for seq, canonical_id, candidate_id, _ in _EXPECTED_KEEPS
    }
    assert module.validate_against_ledger(records, by_seq) == []

    keyed = {(record.seq, record.candidate_id): record for record in records}
    assert set(keyed) == {(seq, candidate_id) for seq, _, candidate_id, _ in _EXPECTED_KEEPS}
    for seq, canonical_id, candidate_id, batch in _EXPECTED_KEEPS:
        record = keyed[(seq, candidate_id)]
        assert record.decision is DecisionKind.KEEP
        assert record.canonical_id == canonical_id
        assert record.batch == batch
        assert record.reason.strip(), "reason is mandatory for keep too"
        assert record.decided_by
    # The verdict wording: 2A's phrasing verdict and the GraphRAG definition.
    assert "phrasing" in keyed[(413, _EXPECTED_KEEPS[0][2])].reason
    graphrag = keyed[(569, _EXPECTED_KEEPS[2][2])]
    assert "GraphRAG" in graphrag.reason
    assert "definition" in graphrag.reason


# ── validation runs before ANY append ────────────────────────────────────────


def test_a_wrong_canonical_aborts_with_nothing_appended(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A keep record whose canonical disagrees with the ledger appends zero."""
    module = _load_script()
    ledger_path = tmp_path / "merge_ledger.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    _write_ledger(ledger_path, [_entry(413, [_EXPECTED_KEEPS[0][2]], _EXPECTED_KEEPS[0][1])])
    keep_path = tmp_path / "keep.json"
    _write_keep_evidence(
        keep_path,
        [
            {
                "seq": 413,
                "canonical_id": "knowledge:graphrag-agentic:wrong-concept",
                "candidate_id": _EXPECTED_KEEPS[0][2],
                "reason": "same concept, different phrasing",
                "batch": "2A",
            }
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        module.main(
            [
                "--ledger",
                str(ledger_path),
                "--decisions",
                str(decisions_path),
                "--keep-evidence",
                str(keep_path),
                "--apply",
            ]
        )

    assert excinfo.value.code != 0
    assert not decisions_path.exists(), "validation failure must append NOTHING"
    err = capsys.readouterr().err
    assert "canonical" in err


def test_a_non_candidate_aborts_with_nothing_appended(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A keep record naming an id outside the entry's candidates appends zero."""
    module = _load_script()
    ledger_path = tmp_path / "merge_ledger.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    _write_ledger(ledger_path, [_entry(413, [_EXPECTED_KEEPS[0][2]], _EXPECTED_KEEPS[0][1])])
    keep_path = tmp_path / "keep.json"
    _write_keep_evidence(
        keep_path,
        [
            {
                "seq": 413,
                "canonical_id": _EXPECTED_KEEPS[0][1],
                "candidate_id": "knowledge:ghost:not-a-candidate",
                "reason": "same concept, different phrasing",
                "batch": "2A",
            }
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        module.main(
            [
                "--ledger",
                str(ledger_path),
                "--decisions",
                str(decisions_path),
                "--keep-evidence",
                str(keep_path),
                "--apply",
            ]
        )

    assert excinfo.value.code != 0
    assert not decisions_path.exists()
    assert "not a candidate" in capsys.readouterr().err


def test_an_unknown_seq_aborts_with_nothing_appended(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A record whose seq is not in the ledger appends zero."""
    module = _load_script()
    ledger_path = tmp_path / "merge_ledger.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    _write_ledger(ledger_path, [_entry(1, ["ns-b:src:tool-concept"], "ns-a:src:tool-concept")])
    keep_path = tmp_path / "keep.json"
    _write_keep_evidence(
        keep_path,
        [
            {
                "seq": 999,
                "canonical_id": "ns-a:src:tool-concept",
                "candidate_id": "ns-b:src:tool-concept",
                "reason": "same concept, different phrasing",
                "batch": "2A",
            }
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        module.main(
            [
                "--ledger",
                str(ledger_path),
                "--decisions",
                str(decisions_path),
                "--keep-evidence",
                str(keep_path),
                "--apply",
            ]
        )

    assert excinfo.value.code != 0
    assert not decisions_path.exists()
    assert "not in the merge ledger" in capsys.readouterr().err


# ── dry-run default and idempotent apply ─────────────────────────────────────


def test_dry_run_is_the_default_and_prints_grouped_by_decision_kind(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No flag means no write: the plan is printed grouped by kind."""
    module = _load_script()
    ledger_path = tmp_path / "merge_ledger.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    _write_ledger(
        ledger_path,
        [
            _entry(1, ["ns-b:src:tool-concept"], "ns-a:src:tool-concept"),
            _entry(
                11,
                ["ns-b:src:tool-concept"],
                "ns-a:src:tool-concept",
                rollback_of=1,
            ),
            _entry(413, [_EXPECTED_KEEPS[0][2]], _EXPECTED_KEEPS[0][1]),
        ],
    )
    keep_path = tmp_path / "keep.json"
    _write_keep_evidence(
        keep_path,
        [
            {
                "seq": 413,
                "canonical_id": _EXPECTED_KEEPS[0][1],
                "candidate_id": _EXPECTED_KEEPS[0][2],
                "reason": "same concept, different phrasing",
                "batch": "2A",
            }
        ],
    )

    module.main(
        [
            "--ledger",
            str(ledger_path),
            "--decisions",
            str(decisions_path),
            "--keep-evidence",
            str(keep_path),
        ]
    )

    out = capsys.readouterr().out
    assert not decisions_path.exists(), "the default must append nothing"
    assert "separate (1):" in out, "grouped by decision kind"
    assert "keep (1):" in out
    assert "ns-b:src:tool-concept" in out
    assert "--apply" in out, "the dry-run must say how to actually append"


def test_apply_twice_is_idempotent_no_second_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The semantic no-op makes a re-run safe: one line per decision, ever."""
    module = _load_script()
    ledger_path = tmp_path / "merge_ledger.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    _write_ledger(
        ledger_path,
        [
            _entry(1, ["ns-b:src:tool-concept"], "ns-a:src:tool-concept"),
            _entry(11, ["ns-b:src:tool-concept"], "ns-a:src:tool-concept", rollback_of=1),
            _entry(413, [_EXPECTED_KEEPS[0][2]], _EXPECTED_KEEPS[0][1]),
        ],
    )
    keep_path = tmp_path / "keep.json"
    _write_keep_evidence(
        keep_path,
        [
            {
                "seq": 413,
                "canonical_id": _EXPECTED_KEEPS[0][1],
                "candidate_id": _EXPECTED_KEEPS[0][2],
                "reason": "same concept, different phrasing",
                "batch": "2A",
            }
        ],
    )
    argv = [
        "--ledger",
        str(ledger_path),
        "--decisions",
        str(decisions_path),
        "--keep-evidence",
        str(keep_path),
        "--apply",
    ]

    module.main(argv)
    first = capsys.readouterr().out
    lines_after_first = decisions_path.read_text(encoding="utf-8").splitlines()
    assert len(lines_after_first) == 2, "one separate + one keep"

    module.main(argv)
    second = capsys.readouterr().out

    assert len(decisions_path.read_text(encoding="utf-8").splitlines()) == 2, (
        "the second --apply must not append a second line"
    )
    assert "no changes" in second, "the re-run must report no changes"
    assert "appended 2" in first
