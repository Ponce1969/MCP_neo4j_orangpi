"""T9a: the human-decision registry for cross-namespace review.

No database and no real decisions file: the read model and the JSONL adapter
run over ``tmp_path`` files, the ``decisions`` CLI drives the real
``JSONLMergeLedger`` over a tmp ledger (Settings stubbed to tmp paths), and
the audit/enqueue parameter plumbing runs against fake driver sessions — so
every "the rule was executed with exactly this parameter" claim is asserted
on what was actually passed, not on what the code intends.

Covered contract:

* read model — the LATEST record per ``(seq, candidate_id)`` wins and a
  record with a different decision supersedes the older one; the aggregates
  (``separate_entity_ids``, ``keep_pair_keys``, ``decided_pair_keys``) only
  ever look at the latest record per key, and ``separate_entity_ids`` keys
  that resolution by ``candidate_id`` alone (sorted, unique, and an entity
  whose latest decision is ``keep`` is never excluded);
* JSONL adapter — append creates parent directories, round-trips, reads a
  missing file as empty, treats a semantically identical repeat (the latest
  record for the key matching on canonical/decision/reason/batch, ignoring
  ``decided_at``/``decided_by``) as a no-op, allows a changed reason or
  decision for the same key to supersede, and raises a typed error on
  invalid JSON;
* ``decisions record`` — happy path appends a decision whose canonical comes
  from the ledger entry; an unknown seq, a non-candidate and a compensating
  entry are refused with a non-zero exit and NOTHING appended (no §7.2 gate,
  no graph);
* ``decisions list`` — prints key/decision/reviewer/reason/batch (text and
  ``--json``);
* parameter plumbing — ``DUPLICATE_ENTITY_CROSS_NAMESPACE`` (global and
  scoped) is executed with ``decided_separate_ids`` and every other rule is
  not; an empty decisions file passes an empty list (a no-op);
* anti-drift — the enqueue detection query carries the byte-identical
  exclusion clause and passes the same parameter where it is executed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import SecretStr, ValidationError

from book_graph_rag.config import Settings
from book_graph_rag.domain.audit_models import AuditScope, AuditTarget
from book_graph_rag.domain.cross_namespace_decision_models import (
    CrossNamespaceDecision,
    DecisionKind,
    InvalidDecisionRecord,
    decided_pair_keys,
    keep_pair_keys,
    latest_by_key,
    separate_entity_ids,
)
from book_graph_rag.domain.merge_ledger_models import MergeBand, MergeLedgerEntry
from book_graph_rag.infrastructure.jsonl_cross_namespace_decisions import (
    JSONLCrossNamespaceDecisions,
)
from book_graph_rag.infrastructure.jsonl_merge_ledger import JSONLMergeLedger
from book_graph_rag.infrastructure.neo4j_audit_adapter import (
    CROSS_NAMESPACE_DECISION_EXCLUSION,
    QUERY_PLAN,
    Neo4jAuditAdapter,
)
from book_graph_rag.infrastructure.neo4j_quarantine_review_adapter import (
    _QUERY_CROSS_NAMESPACE_GROUPS,
    Neo4jQuarantineReviewAdapter,
)
from book_graph_rag.main import cli
from book_graph_rag.ports.cross_namespace_decision_port import CrossNamespaceDecisionPort
from book_graph_rag.ports.merge_ledger_port import MergeLedgerPort

_SEQ = 7
_CANON = "knowledge:alpha:pattern-agent"
_CAND = "knowledge:beta:pattern-agent-alias"
_OTHER = "knowledge:delta:vector-store"

_NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def _decision(
    *,
    seq: int = _SEQ,
    candidate_id: str = _CAND,
    canonical_id: str = _CANON,
    decision: DecisionKind = DecisionKind.SEPARATE,
    decided_at: datetime = _NOW,
    reason: str = "distinct concepts",
    batch: str | None = None,
) -> CrossNamespaceDecision:
    return CrossNamespaceDecision(
        seq=seq,
        candidate_id=candidate_id,
        canonical_id=canonical_id,
        decision=decision,
        decided_by="human:tester",
        decided_at=decided_at,
        reason=reason,
        batch=batch,
    )


def _ledger_entry(
    seq: int,
    candidate_ids: list[str],
    canonical_id: str = _CANON,
    rollback_of: int | None = None,
) -> MergeLedgerEntry:
    return MergeLedgerEntry(
        seq=seq,
        candidate_ids=candidate_ids,
        canonical_id=canonical_id,
        band=MergeBand.EXACT,
        evidence=[],
        aliases_folded=[],
        edge_inverse_map=[],
        approver="human:tester",
        applied_at=_NOW,
        rollback_of=rollback_of,
    )


def _write_ledger(path: Path, entries: list[MergeLedgerEntry]) -> None:
    ledger = JSONLMergeLedger(path)
    for entry in entries:
        ledger.append(entry)


# ── read model: latest record per (seq, candidate_id) wins ──────────────────


def test_latest_record_per_key_wins_and_supersedes_the_older_decision() -> None:
    first = _decision(decision=DecisionKind.KEEP, reason="same concept")
    second = _decision(decision=DecisionKind.SEPARATE)

    latest = latest_by_key([first, second])

    assert set(latest) == {(_SEQ, _CAND)}
    assert latest[(_SEQ, _CAND)] is second
    # The aggregates only look at the winning record.
    assert separate_entity_ids([first, second]) == [_CAND]
    assert keep_pair_keys([first, second]) == set()
    assert decided_pair_keys([first, second]) == {(_SEQ, _CAND)}


def test_read_model_aggregates_each_key_independently() -> None:
    keep_other = _decision(
        seq=8,
        candidate_id=_OTHER,
        canonical_id="knowledge:alpha:vector-store",
        decision=DecisionKind.KEEP,
        reason="same concept under different phrasing",
        batch="2B",
    )
    records = [
        _decision(decision=DecisionKind.KEEP, reason="first thought"),
        keep_other,
        _decision(),  # supersedes the first record of (_SEQ, _CAND) only
    ]

    latest = latest_by_key(records)
    assert set(latest) == {(_SEQ, _CAND), (8, _OTHER)}
    assert latest[(8, _OTHER)] is keep_other
    assert separate_entity_ids(records) == [_CAND]
    assert keep_pair_keys(records) == {(8, _OTHER)}
    assert decided_pair_keys(records) == {(_SEQ, _CAND), (8, _OTHER)}


def test_read_model_over_no_records_is_empty() -> None:
    assert latest_by_key([]) == {}
    assert separate_entity_ids([]) == []
    assert keep_pair_keys([]) == set()
    assert decided_pair_keys([]) == set()


def test_a_later_keep_cancels_an_older_separate_for_the_same_candidate() -> None:
    """Latest-per-candidate (file order): a newer keep un-suppresses the id."""
    older_separate = _decision(seq=_SEQ, candidate_id=_CAND, decision=DecisionKind.SEPARATE)
    newer_keep = _decision(
        seq=8, candidate_id=_CAND, decision=DecisionKind.KEEP, reason="same concept"
    )

    assert separate_entity_ids([older_separate, newer_keep]) == []
    # The winning record per key is unaffected: the keep still stands.
    assert keep_pair_keys([older_separate, newer_keep]) == {(8, _CAND)}


def test_a_later_separate_still_excludes_after_an_older_keep() -> None:
    older_keep = _decision(
        seq=_SEQ, candidate_id=_CAND, decision=DecisionKind.KEEP, reason="same concept"
    )
    newer_separate = _decision(seq=8, candidate_id=_CAND, decision=DecisionKind.SEPARATE)

    assert separate_entity_ids([older_keep, newer_separate]) == [_CAND]


def test_separate_entity_ids_has_no_duplicates_when_records_share_a_candidate_id() -> None:
    """Same candidate under two ledger entries: one id, not a doubled list."""
    records = [
        _decision(seq=_SEQ, candidate_id=_CAND, decision=DecisionKind.SEPARATE),
        _decision(seq=8, candidate_id=_CAND, decision=DecisionKind.SEPARATE),
    ]

    assert separate_entity_ids(records) == [_CAND]


def test_reason_is_mandatory_for_both_kinds() -> None:
    for kind in (DecisionKind.KEEP, DecisionKind.SEPARATE):
        for empty in ("", "   "):
            with pytest.raises(ValidationError, match="reason"):
                _decision(decision=kind, reason=empty)


def test_decision_record_is_frozen_and_carries_batch_none_by_default() -> None:
    record = _decision()
    assert record.batch is None
    assert record.schema_version
    with pytest.raises(ValidationError, match="frozen"):
        record.decision = DecisionKind.KEEP  # pydantic frozen: runtime refusal


# ── JSONL adapter: append / round trip / no-op / typed error ────────────────


def test_append_creates_parent_directories_and_round_trips(tmp_path: Path) -> None:
    store = JSONLCrossNamespaceDecisions(tmp_path / "nested" / "dir" / "decisions.jsonl")
    record = _decision(batch="2A")

    assert store.append(record) is True

    lines = (tmp_path / "nested" / "dir" / "decisions.jsonl").read_text(encoding="utf-8")
    assert lines.count("\n") == 1
    assert json.loads(lines.strip())["decision"] == "separate"
    assert store.read_all() == [record]


def test_missing_file_reads_as_empty(tmp_path: Path) -> None:
    store = JSONLCrossNamespaceDecisions(tmp_path / "absent.jsonl")
    assert store.read_all() == []
    assert store.append(_decision()) is True  # first append materializes the file


def test_appending_the_exact_same_decision_twice_is_a_noop(tmp_path: Path) -> None:
    store = JSONLCrossNamespaceDecisions(tmp_path / "decisions.jsonl")
    record = _decision()

    assert store.append(record) is True
    before = (tmp_path / "decisions.jsonl").read_bytes()

    assert store.append(record) is False
    assert (tmp_path / "decisions.jsonl").read_bytes() == before
    assert store.read_all() == [record]


def test_appending_a_semantically_identical_repeat_is_a_noop_despite_a_fresh_stamp(
    tmp_path: Path,
) -> None:
    """The CLI stamps decided_at (and may name the reviewer) on every run."""
    store = JSONLCrossNamespaceDecisions(tmp_path / "decisions.jsonl")
    first = _decision()
    repeat = first.model_copy(
        update={"decided_at": _NOW + timedelta(minutes=5), "decided_by": "human:other"}
    )

    assert store.append(first) is True
    assert store.append(repeat) is False
    before = (tmp_path / "decisions.jsonl").read_bytes()
    assert store.append(repeat) is False
    assert (tmp_path / "decisions.jsonl").read_bytes() == before
    assert store.read_all() == [first]


def test_a_changed_reason_supersedes_the_latest_record(tmp_path: Path) -> None:
    store = JSONLCrossNamespaceDecisions(tmp_path / "decisions.jsonl")

    assert store.append(_decision(reason="first pass")) is True
    assert store.append(_decision(reason="reconsidered after review")) is True

    records = store.read_all()
    assert len(records) == 2
    assert all((record.seq, record.candidate_id) == (_SEQ, _CAND) for record in records)
    assert latest_by_key(records)[(_SEQ, _CAND)].reason == "reconsidered after review"


def test_a_changed_decision_supersedes_the_latest_record(tmp_path: Path) -> None:
    store = JSONLCrossNamespaceDecisions(tmp_path / "decisions.jsonl")

    assert store.append(_decision(decision=DecisionKind.KEEP, reason="same concept")) is True
    assert store.append(_decision(decision=DecisionKind.SEPARATE, reason="same concept")) is True

    records = store.read_all()
    assert len(records) == 2
    assert latest_by_key(records)[(_SEQ, _CAND)].decision is DecisionKind.SEPARATE
    assert separate_entity_ids(records) == [_CAND]


def test_a_different_decision_for_the_same_key_is_appended_and_wins(
    tmp_path: Path,
) -> None:
    store = JSONLCrossNamespaceDecisions(tmp_path / "decisions.jsonl")
    store.append(_decision(decision=DecisionKind.KEEP, reason="same concept"))
    store.append(_decision(decision=DecisionKind.SEPARATE))  # same key, later record

    records = store.read_all()
    assert len(records) == 2
    assert all((record.seq, record.candidate_id) == (_SEQ, _CAND) for record in records)
    latest = latest_by_key(records)
    assert latest[(_SEQ, _CAND)].decision is DecisionKind.SEPARATE
    assert separate_entity_ids(records) == [_CAND]


def test_invalid_json_raises_a_typed_error(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    path.write_text('{"seq": 7, "candidate_id": "x"\n', encoding="utf-8")

    with pytest.raises(InvalidDecisionRecord, match="decisions.jsonl"):
        JSONLCrossNamespaceDecisions(path).read_all()


def test_valid_json_with_wrong_fields_is_also_a_typed_error(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    path.write_text('{"seq": "not-an-int"}\n', encoding="utf-8")

    with pytest.raises(InvalidDecisionRecord):
        JSONLCrossNamespaceDecisions(path).read_all()


# ── CLI ``decisions record``: validated against the ledger, fail closed ─────


def _install_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """Point the CLI at a tmp ledger + tmp decisions file (never the repo)."""
    ledger_path = tmp_path / "merge_ledger.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"

    class _StubSettings:
        merge_ledger_path: Path
        cross_namespace_decisions_path: Path

        @classmethod
        def model_validate(cls, data: object) -> _StubSettings:
            self = cls()
            self.merge_ledger_path = ledger_path
            self.cross_namespace_decisions_path = decisions_path
            return self

    monkeypatch.setattr("book_graph_rag.main.Settings", _StubSettings)
    return ledger_path, decisions_path


def test_cli_record_appends_a_decision_with_the_ledger_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path, decisions_path = _install_settings(monkeypatch, tmp_path)
    _write_ledger(ledger_path, [_ledger_entry(_SEQ, [_CAND])])

    result = CliRunner().invoke(
        cli,
        [
            "decisions",
            "record",
            "--seq",
            str(_SEQ),
            "--candidate",
            _CAND,
            "--decision",
            "separate",
            "--reason",
            "different concepts after rollback",
            "--batch",
            "2B",
            "--reviewer",
            "human:tester",
        ],
    )

    assert result.exit_code == 0, result.output
    records = JSONLCrossNamespaceDecisions(decisions_path).read_all()
    assert len(records) == 1
    stored = records[0]
    assert stored.seq == _SEQ
    assert stored.candidate_id == _CAND
    assert stored.canonical_id == _CANON  # taken from the ledger entry
    assert stored.decision is DecisionKind.SEPARATE
    assert stored.decided_by == "human:tester"
    assert stored.reason == "different concepts after rollback"
    assert stored.batch == "2B"
    assert "recorded" in result.output
    # No §7.2 gate: the happy path needed no --backup/--approval flags.


def test_cli_record_defaults_the_reviewer_to_the_current_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path, decisions_path = _install_settings(monkeypatch, tmp_path)
    _write_ledger(ledger_path, [_ledger_entry(_SEQ, [_CAND])])

    result = CliRunner().invoke(
        cli,
        [
            "decisions",
            "record",
            "--seq",
            str(_SEQ),
            "--candidate",
            _CAND,
            "--decision",
            "keep",
            "--reason",
            "same concept, different phrasing",
        ],
    )

    assert result.exit_code == 0, result.output
    stored = JSONLCrossNamespaceDecisions(decisions_path).read_all()[0]
    assert stored.decided_by.startswith("human:")
    assert stored.decision is DecisionKind.KEEP
    assert stored.batch is None


def test_cli_record_twice_with_identical_arguments_appends_one_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI re-stamps decided_at: the semantic no-op must catch it."""
    ledger_path, decisions_path = _install_settings(monkeypatch, tmp_path)
    _write_ledger(ledger_path, [_ledger_entry(_SEQ, [_CAND])])
    args = [
        "decisions",
        "record",
        "--seq",
        str(_SEQ),
        "--candidate",
        _CAND,
        "--decision",
        "separate",
        "--reason",
        "different concepts after rollback",
    ]

    first = CliRunner().invoke(cli, args)
    second = CliRunner().invoke(cli, args)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "already recorded (no-op)" in second.output
    assert len(JSONLCrossNamespaceDecisions(decisions_path).read_all()) == 1


def test_cli_record_refuses_an_unknown_seq_without_appending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path, decisions_path = _install_settings(monkeypatch, tmp_path)
    _write_ledger(ledger_path, [_ledger_entry(_SEQ, [_CAND])])

    result = CliRunner().invoke(
        cli,
        [
            "decisions",
            "record",
            "--seq",
            "999",
            "--candidate",
            _CAND,
            "--decision",
            "separate",
            "--reason",
            "because",
        ],
    )

    assert result.exit_code != 0
    assert "not in the merge ledger" in result.output
    assert not decisions_path.exists()


def test_cli_record_refuses_a_non_candidate_without_appending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path, decisions_path = _install_settings(monkeypatch, tmp_path)
    _write_ledger(ledger_path, [_ledger_entry(_SEQ, [_CAND])])

    result = CliRunner().invoke(
        cli,
        [
            "decisions",
            "record",
            "--seq",
            str(_SEQ),
            "--candidate",
            _OTHER,
            "--decision",
            "separate",
            "--reason",
            "because",
        ],
    )

    assert result.exit_code != 0
    assert "is not a candidate of seq" in result.output
    assert not decisions_path.exists()


def test_cli_record_refuses_a_compensating_entry_without_appending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path, decisions_path = _install_settings(monkeypatch, tmp_path)
    _write_ledger(
        ledger_path,
        [_ledger_entry(_SEQ, [_CAND]), _ledger_entry(8, [_CAND], rollback_of=_SEQ)],
    )

    result = CliRunner().invoke(
        cli,
        [
            "decisions",
            "record",
            "--seq",
            "8",
            "--candidate",
            _CAND,
            "--decision",
            "separate",
            "--reason",
            "because",
        ],
    )

    assert result.exit_code != 0
    assert "compensating entry" in result.output
    assert not decisions_path.exists()


def test_cli_record_rejects_an_empty_reason_before_touching_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path, decisions_path = _install_settings(monkeypatch, tmp_path)
    _write_ledger(ledger_path, [_ledger_entry(_SEQ, [_CAND])])

    result = CliRunner().invoke(
        cli,
        [
            "decisions",
            "record",
            "--seq",
            str(_SEQ),
            "--candidate",
            _CAND,
            "--decision",
            "keep",
            "--reason",
            "   ",
        ],
    )

    assert result.exit_code != 0
    assert "--reason" in result.output
    assert not decisions_path.exists()


# ── CLI ``decisions list`` ──────────────────────────────────────────────────


def test_cli_list_prints_key_decision_reviewer_reason_and_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, decisions_path = _install_settings(monkeypatch, tmp_path)
    store = JSONLCrossNamespaceDecisions(decisions_path)
    store.append(_decision(decision=DecisionKind.KEEP, reason="same concept", batch="2A"))
    store.append(
        _decision(
            seq=8,
            candidate_id=_OTHER,
            canonical_id="knowledge:alpha:vector-store",
            reason="distinct concepts",
        )
    )

    result = CliRunner().invoke(cli, ["decisions", "list"])

    assert result.exit_code == 0, result.output
    first_line = next(line for line in result.output.splitlines() if f"{_SEQ}|" in line)
    assert f"{_SEQ}|{_CAND}" in first_line
    assert "keep" in first_line
    assert "human:tester" in first_line
    assert "same concept" in first_line
    assert "2A" in first_line
    second_line = next(line for line in result.output.splitlines() if "8|" in line)
    assert f"8|{_OTHER}" in second_line
    assert "separate" in second_line
    assert "distinct concepts" in second_line


def test_cli_list_json_emits_the_rows_and_reads_missing_file_as_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, decisions_path = _install_settings(monkeypatch, tmp_path)

    empty = CliRunner().invoke(cli, ["decisions", "list", "--json"])
    assert empty.exit_code == 0, empty.output
    assert json.loads(empty.output) == []
    assert not decisions_path.exists()

    JSONLCrossNamespaceDecisions(decisions_path).append(
        _decision(decision=DecisionKind.KEEP, reason="same concept", batch="2B")
    )

    result = CliRunner().invoke(cli, ["decisions", "list", "--json"])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert rows == [
        {
            "key": f"{_SEQ}|{_CAND}",
            "decision": "keep",
            "reviewer": "human:tester",
            "reason": "same concept",
            "batch": "2B",
        }
    ]


# ── Fake Neo4j sessions (parameter plumbing, no container) ──────────────────


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        async def _rows() -> AsyncIterator[dict[str, Any]]:
            for row in self.rows:
                yield row

        return _rows()


class _Transaction:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def run(self, query: str, params: dict[str, Any]) -> _Result:
        self._session.queries.append((query, params))
        if "dbms.components" in query:
            return _Result([{"version": "5.23", "edition": "community"}])
        if "UNWIND ['Book','Chapter'" in query:
            return _Result([{"label": "Entity", "total": 2}])
        if "UNWIND ['CONTAINS'" in query:
            return _Result([{"rel_type": "RELATED", "total": 3}])
        return _Result([{"total": 0, "samples": []}])


class _Session:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def execute_read(self, callback: Any, *args: Any) -> Any:
        return await callback(_Transaction(self), *args)

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _Driver:
    def __init__(self, session: _Session) -> None:
        self._session = session

    def session(self, **kwargs: Any) -> _Session:
        return self._session


class _ReviewSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, query: str, **kwargs: Any) -> _Result:
        self.calls.append((query, kwargs))
        return _Result([])

    async def __aenter__(self) -> _ReviewSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _ReviewDriver:
    def __init__(self, session: _ReviewSession) -> None:
        self._session = session

    def session(self, **kwargs: Any) -> _ReviewSession:
        return self._session


class _EmptyLedger(MergeLedgerPort):
    def append(self, entry: MergeLedgerEntry) -> None:
        raise NotImplementedError("the detection query never writes the ledger")

    def read_all(self) -> list[MergeLedgerEntry]:
        return []

    def read_by_seq(self, seq: int) -> MergeLedgerEntry | None:
        return None

    def verify_chain(self) -> None:
        return None


def _settings() -> Settings:
    return Settings(
        neo4j_uri="bolt://db:7687",
        neo4j_user="neo4j",
        neo4j_password=SecretStr("secret"),
        neo4j_database="neo4j",
    )


def _target() -> AuditTarget:
    return AuditTarget(
        selector="bookgraph-neo4j",
        database="neo4j",
        scheme="bolt",
        host="db",
        port=7687,
        uri="bolt://db:7687",
    )


def _patch_audit_driver(monkeypatch: pytest.MonkeyPatch, driver: _Driver) -> None:
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_audit_adapter.AsyncGraphDatabase.driver",
        lambda *args, **kwargs: driver,
    )


_CROSS_MARKER = "size(namespaces) > 1"


def _run_audit(
    monkeypatch: pytest.MonkeyPatch,
    decisions: CrossNamespaceDecisionPort,
    scope: AuditScope | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    session = _Session()
    _patch_audit_driver(monkeypatch, _Driver(session))
    adapter = Neo4jAuditAdapter(_settings(), decisions=decisions)
    snapshot = asyncio.run(adapter.collect_snapshot(_target(), 10, scope=scope))
    assert snapshot.failure_state is None, snapshot.model_dump()
    return session.queries


def test_audit_executes_the_cross_namespace_rule_with_decided_separate_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exclusion parameter reaches the R5a rule — and only that rule."""
    store = JSONLCrossNamespaceDecisions(tmp_path / "decisions.jsonl")
    store.append(_decision())

    queries = _run_audit(monkeypatch, store)

    cross_runs = [(q, p) for q, p in queries if _CROSS_MARKER in q]
    other_runs = [(q, p) for q, p in queries if _CROSS_MARKER not in q]
    assert cross_runs, "the R5a rule must have been executed"
    assert len(other_runs) >= 20, "every other catalog rule must have run"
    for query, params in cross_runs:
        assert params["decided_separate_ids"] == [_CAND]
        assert CROSS_NAMESPACE_DECISION_EXCLUSION in query
    for _, params in other_runs:
        assert "decided_separate_ids" not in params


def test_audit_scoped_variant_keeps_the_exclusion_parameter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JSONLCrossNamespaceDecisions(tmp_path / "decisions.jsonl")
    store.append(_decision())

    queries = _run_audit(monkeypatch, store, scope=AuditScope(corpus="knowledge", source="alpha"))

    cross_runs = [(q, p) for q, p in queries if _CROSS_MARKER in q]
    assert cross_runs
    for query, params in cross_runs:
        assert params["decided_separate_ids"] == [_CAND]
        assert params["scope_prefix"] == "knowledge:alpha:"
        assert CROSS_NAMESPACE_DECISION_EXCLUSION in query
        assert "$scope_prefix" in query
    for _, params in ((q, p) for q, p in queries if _CROSS_MARKER not in q):
        assert "decided_separate_ids" not in params


def test_audit_passes_an_empty_list_when_there_are_no_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing decisions file is harmless: the rule gets an empty no-op list."""
    queries = _run_audit(monkeypatch, JSONLCrossNamespaceDecisions(tmp_path / "absent.jsonl"))

    cross_runs = [params for query, params in queries if _CROSS_MARKER in query]
    assert cross_runs
    assert all(params["decided_separate_ids"] == [] for params in cross_runs)


# ── Anti-drift: the enqueue detection shares the exclusion ──────────────────


def test_both_consumers_ship_the_byte_identical_exclusion_clause() -> None:
    audit_query = dict(QUERY_PLAN)["duplicates_entity_cross_namespace"]
    assert CROSS_NAMESPACE_DECISION_EXCLUSION in audit_query
    assert CROSS_NAMESPACE_DECISION_EXCLUSION in _QUERY_CROSS_NAMESPACE_GROUPS
    # Same position: right after the shared active-entity predicate.
    predicate = "AND n.id IS NOT NULL "
    assert audit_query.count(predicate + CROSS_NAMESPACE_DECISION_EXCLUSION) == 1
    assert _QUERY_CROSS_NAMESPACE_GROUPS.count(predicate + CROSS_NAMESPACE_DECISION_EXCLUSION) == 1


async def test_enqueue_detection_passes_the_same_parameter_where_it_runs(
    tmp_path: Path,
) -> None:
    store = JSONLCrossNamespaceDecisions(tmp_path / "decisions.jsonl")
    store.append(_decision())
    session = _ReviewSession()
    adapter = Neo4jQuarantineReviewAdapter(_ReviewDriver(session), _EmptyLedger(), decisions=store)

    groups = await adapter.find_cross_namespace_candidate_groups()

    assert groups == []
    assert len(session.calls) == 1
    query, kwargs = session.calls[0]
    assert CROSS_NAMESPACE_DECISION_EXCLUSION in query
    assert kwargs == {"decided_separate_ids": [_CAND]}


async def test_enqueue_detection_without_a_decisions_source_passes_an_empty_list() -> None:
    """Legacy wiring (no decisions port) is a no-op, not a crash."""
    session = _ReviewSession()
    adapter = Neo4jQuarantineReviewAdapter(_ReviewDriver(session), _EmptyLedger())

    await adapter.find_cross_namespace_candidate_groups()

    _, kwargs = session.calls[0]
    assert kwargs == {"decided_separate_ids": []}
