"""CLI contract for ``book-graph-rag ledger rollback`` (T8c, no graph).

Every test stubs the two wiring builders (``build_plan_rollback_use_case`` and
``build_rollback_apply_use_case``) plus ``Settings``, so the assertions run
against the real Click command only:

* read-only by default: no §7.2 gate, no apply builder, nothing written;
* ``--apply`` refuses without a fresh backup, without an ``approve`` file and
  with a wrong ``--expect-fingerprint`` — each refusal BEFORE any use-case
  call (the gate runs first, the fingerprint check runs before the mutation);
* the happy path applies the reviewed plan and prints the before/after census,
  failing with a non-zero exit on any post-apply mismatch.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from book_graph_rag.domain.merge_ledger_models import (
    EdgeInverseMap,
    MergeBand,
    MergeLedgerEntry,
)
from book_graph_rag.domain.rollback_plan_models import (
    ActualEntryOutcome,
    EdgeCensus,
    RelatedEdgeObservation,
    RelatedEdgeProbe,
    RollbackMeasurement,
    RollbackPlan,
    build_entry_plan,
    build_rollback_plan,
)
from book_graph_rag.main import cli

_CANON = "graphrag-agentic:api-calls-tool"
_DUP = "agentic-patterns:api-calls-tool"
_OTHER = "graphrag-agentic:tool-invocation"
_CHUNK = "graphrag-agentic:chunk-7"
_SEQ = 305


def _entry() -> MergeLedgerEntry:
    return MergeLedgerEntry(
        seq=_SEQ,
        candidate_ids=[_DUP],
        canonical_id=_CANON,
        band=MergeBand.EXACT,
        evidence=[],
        aliases_folded=[],
        edge_inverse_map=[
            EdgeInverseMap(
                edge_kind="MENTIONS",
                duplicate_entity_id=_DUP,
                original_other_endpoint_id=_CHUNK,
                edge_properties={"source_page": 3},
            ),
            EdgeInverseMap(
                edge_kind="RELATED",
                duplicate_entity_id=_DUP,
                original_other_endpoint_id=_OTHER,
                edge_properties={"type": "requires", "source_page": 4},
            ),
        ],
        approver="auto:bypass",
        applied_at=datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC),
    )


def _plan() -> RollbackPlan:
    entry_plan = build_entry_plan(
        _entry(),
        [
            RelatedEdgeObservation(
                seq=_SEQ,
                map_index=1,
                probe=RelatedEdgeProbe(a_to_b=True, b_to_a=False),
            )
        ],
    )
    return build_rollback_plan([entry_plan])


def _census(*, after: bool) -> EdgeCensus:
    """Census consistent with rolling back the single planned entry."""
    if after:
        return EdgeCensus(
            mentions_by_entity={_DUP: 1, _CANON: 0},
            related_by_entity={_DUP: 1, _CANON: 0},
            total_mentions=1,
            total_related=1,
            merged_into_count=0,
        )
    return EdgeCensus(
        mentions_by_entity={_DUP: 0, _CANON: 1},
        related_by_entity={_DUP: 0, _CANON: 1},
        total_mentions=1,
        total_related=1,
        merged_into_count=1,
    )


def _measurement(
    *,
    mirrors_created: int = 0,
    related_restored: int = 1,
) -> RollbackMeasurement:
    return RollbackMeasurement(
        outcomes=[
            ActualEntryOutcome(
                seq=_SEQ,
                related_restored=related_restored,
                mirrors_created=mirrors_created,
            )
        ],
        census=_census(after=True),
    )


class _StubPlanUseCase:
    """Stands in for the real plan use case (planning, census, measurement)."""

    def __init__(
        self,
        plan: RollbackPlan,
        measurement: RollbackMeasurement,
    ) -> None:
        self.plan_result = plan
        self.measurement_result = measurement
        self.plan_calls: list[tuple[int, ...]] = []

    async def plan(self, seqs: Sequence[int]) -> RollbackPlan:
        self.plan_calls.append(tuple(seqs))
        return self.plan_result

    async def read_census(self, plan: RollbackPlan) -> EdgeCensus:
        return _census(after=False)

    async def measure(self, plan: RollbackPlan) -> RollbackMeasurement:
        return self.measurement_result


class _StubApplyUseCase:
    """Stands in for RollbackMergeUseCase: records the seqs it would roll back."""

    def __init__(self) -> None:
        self.rolled: list[int] = []

    async def rollback(self, *, seq: int) -> None:
        self.rolled.append(seq)


class _Harness:
    def __init__(self, measurement: RollbackMeasurement) -> None:
        self.plan_builder_calls = 0
        self.apply_builder_calls = 0
        self.plan_use_case = _StubPlanUseCase(_plan(), measurement)
        self.apply_use_case = _StubApplyUseCase()


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    measurement: RollbackMeasurement | None = None,
    forbid_gate: bool = False,
) -> _Harness:
    harness = _Harness(measurement or _measurement())

    class _StubSettings:
        @classmethod
        def model_validate(cls, data: object) -> _StubSettings:
            return cls()

    def _fake_build_plan(settings: object) -> tuple[_StubPlanUseCase, list[object]]:
        harness.plan_builder_calls += 1
        return harness.plan_use_case, []

    def _fake_build_apply(
        settings: object, plan: RollbackPlan
    ) -> tuple[_StubApplyUseCase, list[object]]:
        harness.apply_builder_calls += 1
        return harness.apply_use_case, []

    monkeypatch.setattr("book_graph_rag.main.Settings", _StubSettings)
    monkeypatch.setattr("book_graph_rag.main.build_plan_rollback_use_case", _fake_build_plan)
    monkeypatch.setattr("book_graph_rag.main.build_rollback_apply_use_case", _fake_build_apply)

    if forbid_gate:

        def _gate_forbidden(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("the §7.2 gate must not run for a read-only plan")

        monkeypatch.setattr("book_graph_rag.main._validate_approve_gates", _gate_forbidden)

    return harness


def _gate_files(tmp_path: Path) -> tuple[Path, Path]:
    backup = tmp_path / "backup.json"
    backup.write_text("{}", encoding="utf-8")
    approval = tmp_path / "approval.txt"
    approval.write_text("please approve this", encoding="utf-8")
    return backup, approval


def test_dry_run_is_read_only_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No --apply: no gate, no apply builder, plan + census + fingerprint printed."""
    harness = _install(monkeypatch, forbid_gate=True)

    result = CliRunner().invoke(cli, ["ledger", "rollback", "--seq", str(_SEQ)])

    assert result.exit_code == 0, result.output
    assert "read-only" in result.output
    assert f"fingerprint: {harness.plan_use_case.plan_result.fingerprint}" in result.output
    assert "predicted mirrors: 0" in result.output
    assert "single-direction 1" in result.output
    assert "next: book-graph-rag ledger rollback" in result.output
    assert harness.plan_use_case.plan_calls == [(_SEQ,)]
    assert harness.apply_builder_calls == 0
    assert harness.apply_use_case.rolled == []


def test_dry_run_json_emits_the_structured_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json prints the plan payload with the inferred direction and fingerprint."""
    harness = _install(monkeypatch, forbid_gate=True)

    result = CliRunner().invoke(cli, ["ledger", "rollback", "--seq", str(_SEQ), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["fingerprint"] == harness.plan_use_case.plan_result.fingerprint
    inferred = payload["plans"][0]["inferred_entry"]["edge_inverse_map"][1]
    assert inferred["direction"] == "out"
    original = payload["plans"][0]["entry"]["edge_inverse_map"][1]
    assert original.get("direction") is None
    assert payload["predicted"]["predicted_mirrors"] == 0
    assert harness.apply_builder_calls == 0


def test_rollback_requires_at_least_one_seq() -> None:
    """Explicit --seq only: there is never an 'all' flag (AGENTS.md §7.x)."""
    result = CliRunner().invoke(cli, ["ledger", "rollback"])
    assert result.exit_code != 0
    assert "--seq" in result.output


def test_apply_gate_refusals_happen_before_any_use_case_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate first: missing/wordless approval, missing backup, stale backup."""
    harness = _install(monkeypatch)
    backup, approval = _gate_files(tmp_path)
    runner = CliRunner()

    missing_approval = runner.invoke(
        cli,
        ["ledger", "rollback", "--seq", str(_SEQ), "--apply", "--backup", str(backup)],
    )
    assert missing_approval.exit_code == 1
    assert "APPROVE abortado: falta --approval" in missing_approval.output

    wordless = tmp_path / "wordless.txt"
    wordless.write_text("looks good to me", encoding="utf-8")
    wordless_result = runner.invoke(
        cli,
        [
            "ledger",
            "rollback",
            "--seq",
            str(_SEQ),
            "--apply",
            "--backup",
            str(backup),
            "--approval",
            str(wordless),
        ],
    )
    assert wordless_result.exit_code == 1
    assert "debe contener la palabra 'approve'" in wordless_result.output

    missing_backup = runner.invoke(
        cli,
        ["ledger", "rollback", "--seq", str(_SEQ), "--apply", "--approval", str(approval)],
    )
    assert missing_backup.exit_code == 1
    assert "APPROVE abortado: falta --backup" in missing_backup.output

    stale = datetime.now(UTC).timestamp() - 25 * 3600
    os.utime(backup, (stale, stale))
    stale_result = runner.invoke(
        cli,
        [
            "ledger",
            "rollback",
            "--seq",
            str(_SEQ),
            "--apply",
            "--backup",
            str(backup),
            "--approval",
            str(approval),
        ],
    )
    assert stale_result.exit_code == 1
    assert "APPROVE abortado: el backup tiene" in stale_result.output
    assert "--allow-stale-backup" in stale_result.output

    # Not one builder ran: the gate validates before any use-case call.
    assert harness.plan_builder_calls == 0
    assert harness.apply_builder_calls == 0
    assert harness.apply_use_case.rolled == []


def test_apply_refuses_when_the_expected_fingerprint_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewed plan is bound to the mutation: a mismatch refuses, no writes."""
    harness = _install(monkeypatch)
    backup, approval = _gate_files(tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "ledger",
            "rollback",
            "--seq",
            str(_SEQ),
            "--apply",
            "--backup",
            str(backup),
            "--approval",
            str(approval),
            "--expect-fingerprint",
            "0" * 64,
        ],
    )

    assert result.exit_code == 1
    assert "FINGERPRINT abortado" in result.output
    assert "0" * 64 in result.output
    assert harness.plan_use_case.plan_result.fingerprint in result.output
    # The plan was recomputed (that is what "recomputed" means) but never applied.
    assert harness.plan_builder_calls == 1
    assert harness.apply_builder_calls == 0
    assert harness.apply_use_case.rolled == []


def test_apply_runs_the_reviewed_plan_and_prints_the_census(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: gate + fingerprint ok, rollback per seq, census matches."""
    harness = _install(monkeypatch)
    backup, approval = _gate_files(tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "ledger",
            "rollback",
            "--seq",
            str(_SEQ),
            "--apply",
            "--backup",
            str(backup),
            "--approval",
            str(approval),
            "--expect-fingerprint",
            harness.plan_use_case.plan_result.fingerprint,
            "--reviewer",
            "human:alice",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "gate §7.2 ok" in result.output
    assert f"fingerprint ok: {harness.plan_use_case.plan_result.fingerprint}" in result.output
    assert harness.apply_use_case.rolled == [_SEQ]
    assert "reviewer: human:alice" in result.output
    assert "mentions restored: 1 (predicted 1)" in result.output
    assert "related restored: 1 (predicted 1)" in result.output
    assert "mirrors created: 0 (predicted 0)" in result.output
    assert "merged_into: 1 -> 0" in result.output
    assert "drift: none" in result.output
    assert "Reminder: run the scoped and global audits afterwards" in result.output


def test_post_apply_mismatch_exits_non_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mirror beyond the prediction is drift: printed and non-zero exit."""
    _install(monkeypatch, measurement=_measurement(mirrors_created=1))
    backup, approval = _gate_files(tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "ledger",
            "rollback",
            "--seq",
            str(_SEQ),
            "--apply",
            "--backup",
            str(backup),
            "--approval",
            str(approval),
        ],
    )

    assert result.exit_code == 1
    assert "mirrors created: 1 (predicted 0)" in result.output
    assert "MISMATCH" in result.output
