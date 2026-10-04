"""T8c pilot: ``ledger rollback`` over legacy (pre-R1) cross-namespace entries.

Testcontainers only; never touches production. Two legacy-shaped situations
are seeded directly in their post-merge state (exactly what the 302 historical
cross-namespace merges left behind): a canonical with a re-pointed MENTIONS and
RELATED edge, a soft-deleted duplicate, and a ledger entry whose inverse map
carries ``direction=None`` (the serializer omits the field, as the old lines
did).

Case 1 (inferred ``out``): the canonical shows only ``canonical -> other``, so
the planner infers the original ``dup -> other`` orientation; apply must
restore that single direction, create **no mirror** (counted both ways), clear
``merged_into``, append a compensating entry (chain still verifies) and report
a post-apply census that matches the prediction (exit 0, ``drift: none``).

Case 2 (fallback ``both``): the canonical shows both directions, so the plan
falls back to the documented legacy both-ways restore and must *report* the
mirror it creates (predicted 1, actual 1, still exit 0).

Case 3 (provenance ``out``): mirrors the production batch-1 shape — the
canonical holds both directions of the same type while only
``canonical -> other`` carries the duplicate's captured ``chunk_index``. The
geometric probe says ``both``, the provenance rule resolves ``out`` (rule
``provenance``), the plan predicts 0 mirrors, and apply restores exactly the
duplicate's original direction while the canonical **keeps its own reverse
edge** (the removal is direction-aware: it consumes only the re-pointed
direction).

Case 4 (legacy ``direction=None`` in the same batch-1 shape): the direction is
undecidable (no usable chunk_index), so the documented limit applies — BOTH
live canonical directions are removed, the duplicate regains both (one
reported mirror) and the compensating entry stays direction-less. That limit
is pinned on purpose, not by accident.

Case 5 (bidirectional original): the duplicate held the SAME (other, type)
relation in BOTH directions before the merge, so the ledger carries TWO
inverse entries for the same pair with different ``chunk_index`` values
(the production shape behind ``acted-in-concept [requires]`` 260/237).
Provenance decides one ``out`` and one ``in``; the restore recreates both
directions and the census must expect exactly two restored edges and ZERO
mirrors — the second direction is the other entry's restore, not a mirror —
so ``--apply`` must report ``drift: none``.

The plan is produced twice to prove the fingerprint is stable across runs
(that is what ``--expect-fingerprint`` binds to the reviewed review).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from neo4j import AsyncGraphDatabase, GraphDatabase

from book_graph_rag.application.plan_rollback_use_case import planned_entry_ledger
from book_graph_rag.application.rollback_merge_use_case import RollbackMergeUseCase
from book_graph_rag.config import Settings
from book_graph_rag.domain.merge_ledger_models import (
    EdgeInverseMap,
    FoldedAlias,
    MergeBand,
    MergeLedgerEntry,
)
from book_graph_rag.domain.rollback_plan_models import (
    RollbackPlan,
    build_entry_plan,
    build_rollback_plan,
)
from book_graph_rag.infrastructure.jsonl_merge_ledger import JSONLMergeLedger
from book_graph_rag.infrastructure.neo4j_graph_merge_adapter import Neo4jGraphMergeAdapter
from book_graph_rag.main import cli

pytestmark = pytest.mark.neo4j_integration

_CANON = "graphrag-agentic:api-calls-tool"
_DUP = "agentic-patterns:api-calls-tool"
_DUP_B = "essential-graphrag:api-calls-tool"
_OTHER = "graphrag-agentic:tool-invocation"
_OTHER_B = "graphrag-agentic:tool-router"
_CHUNK = "graphrag-agentic:chunk-7"
_CHUNK_A = "graphrag-agentic:chunk-1"
_CHUNK_B = "graphrag-agentic:chunk-2"
_SEQ = 305
_EDGE_TYPE = "requires"
#: The loser's captured chunk_index in the provenance case (case 3).
_PROVENANCE_CHUNK = 12
#: The OTHER direction's loser chunk in the bidirectional case (case 5).
_BIDIRECTIONAL_OTHER_CHUNK = 99


def _seed(settings: Settings, *, both_directions: bool) -> None:
    """Seed the post-merge graph shape of a legacy cross-namespace merge."""
    extra = (
        "CREATE (o)-[:RELATED {type: $rel_type, source_page: 5}]->(c)" if both_directions else ""
    )
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        with driver.session() as session:
            session.run(
                """
                CREATE (c:Entity {id: $canon, name: 'API Calls Tool', type: 'tool',
                                  aliases: ['API calls']})
                CREATE (d:Entity {id: $dup, name: 'API Calls Tool', type: 'tool',
                                  merged_into: $canon, merged_at: datetime()})
                CREATE (o:Entity {id: $other, name: 'Tool Invocation', type: 'tool'})
                CREATE (k:Chunk {id: $chunk, source_id: 'graphrag-agentic',
                                 chunk_index: 7, text: 'legacy chunk'})
                CREATE (k)-[:MENTIONS {source_page: 3}]->(c)
                CREATE (c)-[:RELATED {type: $rel_type, source_page: 4}]->(o)
                """
                + extra,
                {
                    "canon": _CANON,
                    "dup": _DUP,
                    "other": _OTHER,
                    "chunk": _CHUNK,
                    "rel_type": _EDGE_TYPE,
                },
            ).consume()
    finally:
        driver.close()


def _seed_provenance_case(settings: Settings) -> None:
    """Batch-1 production shape: both directions live, only one carries the loser.

    ``canonical -> other`` carries the duplicate's captured ``chunk_index`` (the
    re-point copied ``properties(r)``), while ``other -> canonical`` is a
    different edge (the canonical's own), so the geometric verdict is ``both``
    but the provenance rule can resolve the loser's original direction.
    """
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        with driver.session() as session:
            session.run(
                """
                CREATE (c:Entity {id: $canon, name: 'API Calls Tool', type: 'tool',
                                  aliases: ['API calls']})
                CREATE (d:Entity {id: $dup, name: 'API Calls Tool', type: 'tool',
                                  merged_into: $canon, merged_at: datetime()})
                CREATE (o:Entity {id: $other, name: 'Tool Invocation', type: 'tool'})
                CREATE (k:Chunk {id: $chunk, source_id: 'graphrag-agentic',
                                 chunk_index: 7, text: 'legacy chunk'})
                CREATE (k)-[:MENTIONS {source_page: 3}]->(c)
                CREATE (c)-[:RELATED {type: $rel_type, chunk_index: $loser_chunk,
                                      source_page: 4}]->(o)
                CREATE (o)-[:RELATED {type: $rel_type, chunk_index: 99,
                                      source_page: 5}]->(c)
                """,
                {
                    "canon": _CANON,
                    "dup": _DUP,
                    "other": _OTHER,
                    "chunk": _CHUNK,
                    "rel_type": _EDGE_TYPE,
                    "loser_chunk": _PROVENANCE_CHUNK,
                },
            ).consume()
    finally:
        driver.close()


def _legacy_entry(*, related_properties: dict[str, Any] | None = None) -> MergeLedgerEntry:
    """Pre-R1 ledger entry: every RELATED inverse entry has ``direction=None``.

    ``related_properties`` overrides the RELATED inverse entry's captured
    ``edge_properties`` (the provenance case carries the loser's chunk_index).
    """
    related_edge_properties = (
        related_properties
        if related_properties is not None
        else {"type": _EDGE_TYPE, "source_page": 4}
    )
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
                edge_properties=related_edge_properties,
            ),
        ],
        approver="auto:bypass",
        applied_at=datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC),
    )


def _cli_settings(neo4j_settings: Settings, tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "neo4j_uri": neo4j_settings.neo4j_uri,
            "neo4j_user": neo4j_settings.neo4j_user,
            "neo4j_password": neo4j_settings.neo4j_password.get_secret_value(),
            "merge_ledger_path": str(tmp_path / "merge_ledger.jsonl"),
            "quarantine_path": str(tmp_path / "quarantine.jsonl"),
        }
    )


def _count(settings: Settings, cypher: str, **params: Any) -> int:
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        with driver.session() as session:
            record = session.run(cypher, params).single()
            assert record is not None
            return int(record["c"])
    finally:
        driver.close()


def _related_count(settings: Settings, *, source: str, target: str) -> int:
    return _count(
        settings,
        """
        MATCH (a:Entity {id: $source})-[r:RELATED]->(b:Entity {id: $target})
        WHERE r.type = $rel_type
        RETURN count(r) AS c
        """,
        source=source,
        target=target,
        rel_type=_EDGE_TYPE,
    )


def _mention_target(settings: Settings, *, chunk_id: str = _CHUNK) -> str | None:
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        with driver.session() as session:
            record = session.run(
                "MATCH (k:Chunk {id: $chunk})-[:MENTIONS]->(e) RETURN e.id AS id",
                chunk=chunk_id,
            ).single()
            return None if record is None else str(record["id"])
    finally:
        driver.close()


def _merged_into_of(settings: Settings, entity_id: str) -> str | None:
    """The ``merged_into`` marker of ANY entity (partial rollbacks need it per id)."""
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        with driver.session() as session:
            record = session.run(
                "MATCH (n:Entity {id: $id}) RETURN n.merged_into AS merged",
                id=entity_id,
            ).single()
            assert record is not None, f"entity {entity_id} missing from the graph"
            merged = record["merged"]
            return None if merged is None else str(merged)
    finally:
        driver.close()


def _canonical_aliases(settings: Settings) -> list[str]:
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        with driver.session() as session:
            record = session.run(
                "MATCH (n:Entity {id: $id}) RETURN n.aliases AS aliases",
                id=_CANON,
            ).single()
            assert record is not None, "canonical missing from the graph"
            return [str(alias) for alias in (record["aliases"] or [])]
    finally:
        driver.close()


def _merged_into(settings: Settings) -> str | None:
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        with driver.session() as session:
            record = session.run(
                "MATCH (n:Entity {id: $id}) RETURN n.merged_into AS merged",
                id=_DUP,
            ).single()
            assert record is not None, "duplicate missing from the graph"
            merged = record["merged"]
            return None if merged is None else str(merged)
    finally:
        driver.close()


def _plan_cli(
    runner: CliRunner,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidates: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Dry-run ``ledger rollback --seq 305 --json`` (optionally partial) and return the payload."""

    class _StubSettings:
        @classmethod
        def model_validate(cls, data: object) -> Settings:
            return settings

    monkeypatch.setattr("book_graph_rag.main.Settings", _StubSettings)
    args = ["ledger", "rollback", "--seq", str(_SEQ), "--json"]
    for candidate in candidates:
        args.extend(["--candidate", candidate])
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.output)
    return payload


def test_legacy_entry_plan_apply_restores_the_original_orientation(
    neo4j_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inference says ``out``: one direction restored, zero mirrors, census matches."""
    settings = _cli_settings(neo4j_settings, tmp_path)
    _seed(settings, both_directions=False)

    ledger_path = tmp_path / "merge_ledger.jsonl"
    ledger = JSONLMergeLedger(ledger_path)
    ledger.append(_legacy_entry())
    assert len(ledger.read_all()) == 1

    backup = tmp_path / "backup.json"
    backup.write_text("{}", encoding="utf-8")
    approval = tmp_path / "approval.txt"
    approval.write_text("approve", encoding="utf-8")

    runner = CliRunner()

    # 1. Plan (read-only): direction inferred, original entry untouched.
    plan = _plan_cli(runner, settings, monkeypatch)
    assert plan["predicted"] == {
        "mentions_restored": 1,
        "related_restored": 1,
        "directions_inferred": 1,
        "fallback_both": 0,
        "fallback_unknown": 0,
        "predicted_mirrors": 0,
    }
    inferred_related = plan["plans"][0]["inferred_entry"]["edge_inverse_map"][1]
    assert inferred_related["direction"] == "out"
    original_related = plan["plans"][0]["entry"]["edge_inverse_map"][1]
    assert "direction" not in original_related, "legacy copy must stay direction-less"
    fingerprint = plan["fingerprint"]
    assert len(fingerprint) == 64

    # Fingerprint stability: a second plan of the same state must agree.
    plan_again = _plan_cli(runner, settings, monkeypatch)
    assert plan_again["fingerprint"] == fingerprint

    # 2. Apply behind the §7.2 gate with the reviewed fingerprint.
    result = runner.invoke(
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
            fingerprint,
        ],
    )
    assert result.exit_code == 0, result.output
    assert f"fingerprint ok: {fingerprint}" in result.output
    assert "seq 305: rollback applied (compensating ledger entry appended)" in result.output
    assert "mentions restored: 1 (predicted 1)" in result.output
    assert "related restored: 1 (predicted 1)" in result.output
    assert "mirrors created: 0 (predicted 0)" in result.output
    assert "drift: none" in result.output
    assert "Reminder: run the scoped and global audits afterwards" in result.output

    # 3. Graph: original direction only, no mirror counted either way.
    assert _related_count(settings, source=_DUP, target=_OTHER) == 1
    assert _related_count(settings, source=_OTHER, target=_DUP) == 0, (
        "mirror created: the inferred rollback must restore one direction only"
    )
    assert _related_count(settings, source=_CANON, target=_OTHER) == 0
    assert _mention_target(settings) == _DUP
    assert _merged_into(settings) is None

    # 4. Ledger: compensating entry appended, chain still verifies.
    entries = ledger.read_all()
    assert len(entries) == 2
    compensating = entries[1]
    assert compensating.rollback_of == _SEQ
    assert compensating.candidate_ids == [_DUP]
    # The compensating entry records the inferred direction too.
    assert compensating.edge_inverse_map[1].direction == "out"
    ledger.verify_chain()


def test_legacy_entry_fallback_both_reports_the_legacy_mirror(
    neo4j_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambiguous canonical (both directions): legacy both-ways restore, mirror reported."""
    settings = _cli_settings(neo4j_settings, tmp_path)
    _seed(settings, both_directions=True)

    ledger_path = tmp_path / "merge_ledger.jsonl"
    ledger = JSONLMergeLedger(ledger_path)
    ledger.append(_legacy_entry())

    backup = tmp_path / "backup.json"
    backup.write_text("{}", encoding="utf-8")
    approval = tmp_path / "approval.txt"
    approval.write_text("approve", encoding="utf-8")

    runner = CliRunner()

    plan = _plan_cli(runner, settings, monkeypatch)
    assert plan["predicted"] == {
        "mentions_restored": 1,
        "related_restored": 2,
        "directions_inferred": 0,
        "fallback_both": 1,
        "fallback_unknown": 0,
        "predicted_mirrors": 1,
    }
    inference = plan["plans"][0]["inferences"][0]
    assert inference["fallback"] is True
    assert inference["applied_direction"] is None
    assert "legacy both-ways restore" in inference["reason"]
    fingerprint = plan["fingerprint"]

    result = runner.invoke(
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
            fingerprint,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "related restored: 2 (predicted 2)" in result.output
    assert "mirrors created: 1 (predicted 1)" in result.output
    assert "drift: none" in result.output

    # Legacy behaviour pinned: BOTH directions end up on the duplicate and the
    # mirror is exactly the one the plan predicted and reported.
    assert _related_count(settings, source=_DUP, target=_OTHER) == 1
    assert _related_count(settings, source=_OTHER, target=_DUP) == 1
    assert _related_count(settings, source=_CANON, target=_OTHER) == 0
    assert _related_count(settings, source=_OTHER, target=_CANON) == 0
    assert _mention_target(settings) == _DUP
    assert _merged_into(settings) is None

    entries = ledger.read_all()
    assert len(entries) == 2
    assert entries[1].rollback_of == _SEQ
    ledger.verify_chain()


def test_provenance_decides_out_and_restores_one_direction_without_a_mirror(
    neo4j_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch-1 shape: geometric ``both``, provenance resolves ``out``, 0 mirrors.

    The canonical already holds both directions of the same type while only
    ``canonical -> other`` carries the duplicate's captured ``chunk_index``.
    The plan must record the ``provenance`` rule, predict zero mirrors and —
    once applied — restore exactly the duplicate's original direction with no
    mirror, while the canonical **keeps its own reverse edge** (the removal
    is direction-aware and consumes only the re-pointed direction).
    """
    settings = _cli_settings(neo4j_settings, tmp_path)
    _seed_provenance_case(settings)

    ledger_path = tmp_path / "merge_ledger.jsonl"
    ledger = JSONLMergeLedger(ledger_path)
    ledger.append(
        _legacy_entry(
            related_properties={
                "type": _EDGE_TYPE,
                "chunk_index": _PROVENANCE_CHUNK,
                "source_page": 4,
            }
        )
    )
    assert len(ledger.read_all()) == 1

    backup = tmp_path / "backup.json"
    backup.write_text("{}", encoding="utf-8")
    approval = tmp_path / "approval.txt"
    approval.write_text("approve", encoding="utf-8")

    runner = CliRunner()

    # 1. Plan: geometry was both, provenance decides out, zero mirrors predicted.
    plan = _plan_cli(runner, settings, monkeypatch)
    assert plan["predicted"] == {
        "mentions_restored": 1,
        "related_restored": 1,
        "directions_inferred": 1,
        "fallback_both": 0,
        "fallback_unknown": 0,
        "predicted_mirrors": 0,
    }
    inference = plan["plans"][0]["inferences"][0]
    assert inference["observed_canonical_to_other"] is True
    assert inference["observed_other_to_canonical"] is True, "geometric input must be both"
    assert inference["rule"] == "provenance"
    assert inference["verdict"] == "out"
    assert inference["applied_direction"] == "out"
    assert inference["fallback"] is False
    assert str(_PROVENANCE_CHUNK) in inference["reason"]
    inferred_related = plan["plans"][0]["inferred_entry"]["edge_inverse_map"][1]
    assert inferred_related["direction"] == "out"
    fingerprint = plan["fingerprint"]
    assert len(fingerprint) == 64

    # Fingerprint stability: a second plan of the same state must agree.
    plan_again = _plan_cli(runner, settings, monkeypatch)
    assert plan_again["fingerprint"] == fingerprint

    # 2. Graph counted BEFORE the apply: both canonical directions are live.
    canon_to_other_before = _related_count(settings, source=_CANON, target=_OTHER)
    other_to_canon_before = _related_count(settings, source=_OTHER, target=_CANON)
    assert canon_to_other_before == 1
    assert other_to_canon_before == 1

    # 3. Apply behind the §7.2 gate with the reviewed fingerprint.
    result = runner.invoke(
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
            fingerprint,
        ],
    )
    assert result.exit_code == 0, result.output
    assert f"fingerprint ok: {fingerprint}" in result.output
    assert "mentions restored: 1 (predicted 1)" in result.output
    assert "related restored: 1 (predicted 1)" in result.output
    assert "mirrors created: 0 (predicted 0)" in result.output
    assert "drift: none" in result.output

    # 4. Graph: exactly the duplicate's direction, no mirror anywhere, and the
    # canonical's own reverse edge is preserved (never the merge's business).
    # (a) The duplicate regains its original direction only.
    assert _related_count(settings, source=_DUP, target=_OTHER) == 1
    assert _related_count(settings, source=_OTHER, target=_DUP) == 0, (
        "mirror created: the provenance decision must restore one direction only"
    )
    # (b) The canonical keeps its own reverse edge: the direction-aware removal
    # consumed only the re-pointed (loser's) direction.
    assert _related_count(settings, source=_CANON, target=_OTHER) == 0
    assert _related_count(settings, source=_OTHER, target=_CANON) == other_to_canon_before == 1, (
        "the canonical's own reverse edge must be preserved, not consumed"
    )
    # (c) No mirror was created: each direction appears at most once and the
    # total across each pair is exactly the one edge that belongs to it.
    duplicate_pair = _related_count(settings, source=_DUP, target=_OTHER) + _related_count(
        settings, source=_OTHER, target=_DUP
    )
    canonical_pair = _related_count(settings, source=_CANON, target=_OTHER) + _related_count(
        settings, source=_OTHER, target=_CANON
    )
    assert duplicate_pair == 1, f"duplicate pair holds {duplicate_pair} edges, expected 1"
    assert canonical_pair == 1, f"canonical pair holds {canonical_pair} edges, expected 1"
    assert _mention_target(settings) == _DUP
    assert _merged_into(settings) is None

    # 5. Ledger: compensating entry records the provenance-decided direction.
    entries = ledger.read_all()
    assert len(entries) == 2
    assert entries[1].rollback_of == _SEQ
    assert entries[1].edge_inverse_map[1].direction == "out"
    ledger.verify_chain()


def test_legacy_directionless_entry_still_deletes_both_canonical_directions(
    neo4j_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documented limit: ``direction=None`` removes BOTH canonical directions.

    Same batch-1 graph shape as the provenance case, but the ledger entry was
    written before direction tracking and carries no usable ``chunk_index``,
    so the direction is undecidable: the rollback cannot tell which of the two
    live canonical directions is the duplicate's and must keep the legacy
    both-directions removal. The canonical loses BOTH edges, the duplicate
    regains both (one mirror, predicted and reported) and the compensating
    entry stays direction-less. Pinned on purpose: this is the documented
    limit, not an accident.
    """
    settings = _cli_settings(neo4j_settings, tmp_path)
    _seed_provenance_case(settings)

    ledger_path = tmp_path / "merge_ledger.jsonl"
    ledger = JSONLMergeLedger(ledger_path)
    ledger.append(_legacy_entry())  # direction=None, no chunk_index -> undecidable
    assert len(ledger.read_all()) == 1

    backup = tmp_path / "backup.json"
    backup.write_text("{}", encoding="utf-8")
    approval = tmp_path / "approval.txt"
    approval.write_text("approve", encoding="utf-8")

    runner = CliRunner()

    # 1. Plan: geometry both, direction undecidable -> legacy both-ways fallback.
    plan = _plan_cli(runner, settings, monkeypatch)
    assert plan["predicted"] == {
        "mentions_restored": 1,
        "related_restored": 2,
        "directions_inferred": 0,
        "fallback_both": 1,
        "fallback_unknown": 0,
        "predicted_mirrors": 1,
    }
    inference = plan["plans"][0]["inferences"][0]
    assert inference["fallback"] is True
    assert inference["applied_direction"] is None
    assert "legacy both-ways restore" in inference["reason"]
    fingerprint = plan["fingerprint"]

    # 2. Apply behind the §7.2 gate with the reviewed fingerprint.
    result = runner.invoke(
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
            fingerprint,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "related restored: 2 (predicted 2)" in result.output
    assert "mirrors created: 1 (predicted 1)" in result.output
    assert "drift: none" in result.output

    # 3. The documented limit, pinned: BOTH canonical directions are gone and
    # the duplicate holds BOTH directions (one restore + one legacy mirror).
    assert _related_count(settings, source=_CANON, target=_OTHER) == 0
    assert _related_count(settings, source=_OTHER, target=_CANON) == 0, (
        "direction=None must keep the legacy both-directions removal (documented limit)"
    )
    assert _related_count(settings, source=_DUP, target=_OTHER) == 1
    assert _related_count(settings, source=_OTHER, target=_DUP) == 1
    assert _mention_target(settings) == _DUP
    assert _merged_into(settings) is None

    # 4. Ledger: the compensating entry stays direction-less too.
    entries = ledger.read_all()
    assert len(entries) == 2
    assert entries[1].rollback_of == _SEQ
    assert entries[1].edge_inverse_map[1].direction is None
    ledger.verify_chain()


def _legacy_bidirectional_entry() -> MergeLedgerEntry:
    """Pre-R1 entry with TWO inverse entries for the same (other, type) pair.

    Production shape: the duplicate held ``dup -[requires]-> other`` AND
    ``other -[requires]-> dup`` before the merge, so the inverse map captured
    one entry per direction with a different ``chunk_index`` (260/237 in
    production; 12/99 in the seeded graph). Both entries carry
    ``direction=None`` as the legacy lines did.
    """
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
                edge_properties={
                    "type": _EDGE_TYPE,
                    "chunk_index": _PROVENANCE_CHUNK,
                    "source_page": 4,
                },
            ),
            EdgeInverseMap(
                edge_kind="RELATED",
                duplicate_entity_id=_DUP,
                original_other_endpoint_id=_OTHER,
                edge_properties={
                    "type": _EDGE_TYPE,
                    "chunk_index": _BIDIRECTIONAL_OTHER_CHUNK,
                    "source_page": 5,
                },
            ),
        ],
        approver="auto:bypass",
        applied_at=datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC),
    )


def test_bidirectional_original_restores_both_directions_without_drift(
    neo4j_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two entries, one ``out`` + one ``in``: 2 restored edges, 0 mirrors, no drift.

    The duplicate held the relation in BOTH directions before the merge, so
    the ledger carries two inverse entries for the same pair with different
    ``chunk_index`` values (the production ``acted-in-concept [requires]``
    260/237 shape). The provenance rule resolves one ``out`` and one ``in``,
    the restore faithfully recreates both directions, and the census must
    count the pair ONCE PER DIRECTION: two restored edges, zero mirrors —
    the second direction is the other entry's legitimate restore, never a
    mirror of the first. ``--apply`` must therefore report ``drift: none``.
    """
    settings = _cli_settings(neo4j_settings, tmp_path)
    # The same seeded graph as the provenance case: canonical holds BOTH
    # directions, each carrying its own loser chunk (12 forward, 99 reverse).
    _seed_provenance_case(settings)

    ledger_path = tmp_path / "merge_ledger.jsonl"
    ledger = JSONLMergeLedger(ledger_path)
    ledger.append(_legacy_bidirectional_entry())
    assert len(ledger.read_all()) == 1

    backup = tmp_path / "backup.json"
    backup.write_text("{}", encoding="utf-8")
    approval = tmp_path / "approval.txt"
    approval.write_text("approve", encoding="utf-8")

    runner = CliRunner()

    # 1. Plan: provenance decides one out and one in; the pair predicts TWO
    #    restored edges and ZERO mirrors.
    plan = _plan_cli(runner, settings, monkeypatch)
    assert plan["predicted"] == {
        "mentions_restored": 1,
        "related_restored": 2,
        "directions_inferred": 2,
        "fallback_both": 0,
        "fallback_unknown": 0,
        "predicted_mirrors": 0,
    }
    inferences = plan["plans"][0]["inferences"]
    assert [(i["rule"], i["applied_direction"]) for i in inferences] == [
        ("provenance", "out"),
        ("provenance", "in"),
    ]
    fingerprint = plan["fingerprint"]
    plan_again = _plan_cli(runner, settings, monkeypatch)
    assert plan_again["fingerprint"] == fingerprint

    # 2. Apply behind the §7.2 gate with the reviewed fingerprint.
    result = runner.invoke(
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
            fingerprint,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "mentions restored: 1 (predicted 1)" in result.output
    assert "related restored: 2 (predicted 2)" in result.output
    assert "mirrors created: 0 (predicted 0)" in result.output
    assert "drift: none" in result.output

    # 3. Graph: the duplicate regained BOTH original directions, the canonical
    #    keeps neither (both of its directions were the re-pointed ones), and
    #    no direction was double-counted anywhere.
    assert _related_count(settings, source=_DUP, target=_OTHER) == 1
    assert _related_count(settings, source=_OTHER, target=_DUP) == 1
    assert _related_count(settings, source=_CANON, target=_OTHER) == 0
    assert _related_count(settings, source=_OTHER, target=_CANON) == 0
    assert _mention_target(settings) == _DUP
    assert _merged_into(settings) is None

    # 4. Ledger: the compensating entry records BOTH decided directions.
    entries = ledger.read_all()
    assert len(entries) == 2
    assert entries[1].rollback_of == _SEQ
    assert entries[1].edge_inverse_map[1].direction == "out"
    assert entries[1].edge_inverse_map[2].direction == "in"
    ledger.verify_chain()


# ── T8f: partial rollback of one candidate inside a 2-candidate entry ────────


def _seed_two_candidate_case(settings: Settings) -> None:
    """Post-merge shape of a 2-candidate legacy merge (entry with 2 losers).

    ``dA`` and ``dB`` are both soft-deleted onto the canonical; the canonical
    holds A's re-pointed ``c -[requires]-> oa`` and B's re-pointed
    ``ob -[requires]-> c``, plus both re-pointed MENTIONS. The ledger entry
    carries ``direction=None`` on every RELATED entry (legacy line).
    """
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        with driver.session() as session:
            session.run(
                """
                CREATE (c:Entity {id: $canon, name: 'API Calls Tool', type: 'tool',
                                  aliases: ['Alias A', 'Alias B']})
                CREATE (da:Entity {id: $dup_a, name: 'API Calls Tool', type: 'tool',
                                   merged_into: $canon, merged_at: datetime()})
                CREATE (db:Entity {id: $dup_b, name: 'API Calls Tool', type: 'tool',
                                   merged_into: $canon, merged_at: datetime()})
                CREATE (oa:Entity {id: $other_a, name: 'Tool Invocation', type: 'tool'})
                CREATE (ob:Entity {id: $other_b, name: 'Tool Router', type: 'tool'})
                CREATE (ka:Chunk {id: $chunk_a, source_id: 'graphrag-agentic',
                                  chunk_index: 1, text: 'chunk a'})
                CREATE (kb:Chunk {id: $chunk_b, source_id: 'graphrag-agentic',
                                  chunk_index: 2, text: 'chunk b'})
                CREATE (ka)-[:MENTIONS {source_page: 3}]->(c)
                CREATE (kb)-[:MENTIONS {source_page: 5}]->(c)
                CREATE (c)-[:RELATED {type: $rel_type, source_page: 4}]->(oa)
                CREATE (ob)-[:RELATED {type: $rel_type, source_page: 6}]->(c)
                """,
                {
                    "canon": _CANON,
                    "dup_a": _DUP,
                    "dup_b": _DUP_B,
                    "other_a": _OTHER,
                    "other_b": _OTHER_B,
                    "chunk_a": _CHUNK_A,
                    "chunk_b": _CHUNK_B,
                    "rel_type": _EDGE_TYPE,
                },
            ).consume()
    finally:
        driver.close()


def _two_candidate_entry() -> MergeLedgerEntry:
    """Legacy-shaped entry holding TWO candidates (the seq 480 production shape)."""
    return MergeLedgerEntry(
        seq=_SEQ,
        candidate_ids=[_DUP, _DUP_B],
        canonical_id=_CANON,
        band=MergeBand.EXACT,
        evidence=[],
        aliases_folded=[
            FoldedAlias(from_entity_id=_DUP, alias_value="Alias A"),
            FoldedAlias(from_entity_id=_DUP_B, alias_value="Alias B"),
        ],
        edge_inverse_map=[
            EdgeInverseMap(
                edge_kind="MENTIONS",
                duplicate_entity_id=_DUP,
                original_other_endpoint_id=_CHUNK_A,
                edge_properties={"source_page": 3},
            ),
            EdgeInverseMap(
                edge_kind="RELATED",
                duplicate_entity_id=_DUP,
                original_other_endpoint_id=_OTHER,
                edge_properties={"type": _EDGE_TYPE, "source_page": 4},
            ),
            EdgeInverseMap(
                edge_kind="MENTIONS",
                duplicate_entity_id=_DUP_B,
                original_other_endpoint_id=_CHUNK_B,
                edge_properties={"source_page": 5},
            ),
            EdgeInverseMap(
                edge_kind="RELATED",
                duplicate_entity_id=_DUP_B,
                original_other_endpoint_id=_OTHER_B,
                edge_properties={"type": _EDGE_TYPE, "source_page": 6},
            ),
        ],
        approver="auto:bypass",
        applied_at=datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC),
    )


def _apply_partial(
    runner: CliRunner,
    *,
    backup: Path,
    approval: Path,
    candidate: str,
    fingerprint: str,
) -> Any:
    """Dry-run-shaped apply of ONE candidate behind the full §7.2 gate."""
    return runner.invoke(
        cli,
        [
            "ledger",
            "rollback",
            "--seq",
            str(_SEQ),
            "--candidate",
            candidate,
            "--apply",
            "--backup",
            str(backup),
            "--approval",
            str(approval),
            "--expect-fingerprint",
            fingerprint,
        ],
    )


def _run_candidate_aware_noop(
    settings: Settings,
    ledger: JSONLMergeLedger,
    plan: RollbackPlan,
) -> None:
    """Drive ``RollbackMergeUseCase.rollback`` for a partial plan directly.

    The CLI refuses a repeat of compensated candidates at PLAN time (defect 1
    fix), so the candidate-aware idempotence UNION inside the use case is the
    defensive backstop, reachable only when the use case is driven directly
    (this helper) or the ledger changes between plan and apply. The ledger and
    graph are asserted unchanged afterwards.
    """
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )

    async def _run() -> None:
        try:
            use_case = RollbackMergeUseCase(
                ledger=planned_entry_ledger(ledger, plan),
                graph_merge=Neo4jGraphMergeAdapter(driver),
            )
            await use_case.rollback(seq=_SEQ)
        finally:
            await driver.close()

    asyncio.run(_run())


def test_partial_rollback_by_candidate_revives_one_loser_at_a_time(
    neo4j_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T8f: roll back candidate A, then B, then A again (backstop no-op).

    After partial A: A alive (mention, related, alias restored), B still
    merged, canonical alive, compensating entry records only ``[A]`` and the
    chain verifies. A following full ``--seq`` (no ``--candidate``) is REFUSED
    at plan time — dry-run and ``--apply`` both exit 1 and write nothing —
    because A is already compensated. After partial B: both losers alive.
    Repeating partial A through the CLI is refused the same way; the
    candidate-aware no-op backstop is then driven directly through
    ``RollbackMergeUseCase``: no third compensating entry, ledger unchanged.
    """
    settings = _cli_settings(neo4j_settings, tmp_path)
    _seed_two_candidate_case(settings)

    ledger_path = tmp_path / "merge_ledger.jsonl"
    ledger = JSONLMergeLedger(ledger_path)
    ledger.append(_two_candidate_entry())
    assert len(ledger.read_all()) == 1

    backup = tmp_path / "backup.json"
    backup.write_text("{}", encoding="utf-8")
    approval = tmp_path / "approval.txt"
    approval.write_text("approve", encoding="utf-8")

    runner = CliRunner()

    # 1. Partial dry-run for A: only A's surfaces are planned.
    plan_a = _plan_cli(runner, settings, monkeypatch, candidates=(_DUP,))
    assert plan_a["plans"][0]["selected_candidates"] == [_DUP]
    assert plan_a["plans"][0]["inferred_entry"]["candidate_ids"] == [_DUP]
    assert plan_a["predicted"] == {
        "mentions_restored": 1,
        "related_restored": 1,
        "directions_inferred": 1,
        "fallback_both": 0,
        "fallback_unknown": 0,
        "predicted_mirrors": 0,
    }
    fingerprint_a = plan_a["fingerprint"]
    # The partial fingerprint is NOT the full entry's fingerprint.
    plan_full = _plan_cli(runner, settings, monkeypatch)
    assert plan_full["fingerprint"] != fingerprint_a
    assert plan_full["plans"][0]["selected_candidates"] is None

    result = _apply_partial(
        runner,
        backup=backup,
        approval=approval,
        candidate=_DUP,
        fingerprint=fingerprint_a,
    )
    assert result.exit_code == 0, result.output
    assert f"fingerprint ok: {fingerprint_a}" in result.output
    assert "drift: none" in result.output

    # Graph: A revived, B still merged, canonical untouched, B's side intact.
    assert _merged_into_of(settings, _DUP) is None
    assert _merged_into_of(settings, _DUP_B) == _CANON, "B must stay merged after A's rollback"
    assert _merged_into_of(settings, _CANON) is None, "canonical must stay alive"
    assert _related_count(settings, source=_DUP, target=_OTHER) == 1
    assert _related_count(settings, source=_CANON, target=_OTHER) == 0
    assert _related_count(settings, source=_OTHER_B, target=_CANON) == 1, "B's edge untouched"
    assert _mention_target(settings, chunk_id=_CHUNK_A) == _DUP
    assert _mention_target(settings, chunk_id=_CHUNK_B) == _CANON, "B's mention untouched"
    assert _canonical_aliases(settings) == ["Alias B"], "only A's alias is unfolded"

    # Ledger: one compensating entry recording ONLY candidate A.
    entries = ledger.read_all()
    assert len(entries) == 2
    compensating_a = entries[1]
    assert compensating_a.rollback_of == _SEQ
    assert compensating_a.candidate_ids == [_DUP]
    assert [m.duplicate_entity_id for m in compensating_a.edge_inverse_map] == [_DUP, _DUP]
    assert compensating_a.edge_inverse_map[1].direction == "out"
    assert compensating_a.aliases_folded == [
        FoldedAlias(from_entity_id=_DUP, alias_value="Alias A")
    ]
    ledger.verify_chain()

    # 1b. Defect 1: an overlapping FULL request is refused at PLAN time,
    # BEFORE any write — dry-run and --apply alike (exit 1 via
    # MergeNotReversible, the message points at the explicit --candidate
    # selection for the remaining candidate).
    full_dry = runner.invoke(cli, ["ledger", "rollback", "--seq", str(_SEQ)])
    assert full_dry.exit_code == 1, full_dry.output
    assert _DUP in full_dry.output, "the overlap/compensated candidate must be named"
    assert "--candidate" in full_dry.output, "the operator must be told how to proceed"

    full_apply = runner.invoke(
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
    assert full_apply.exit_code == 1, full_apply.output
    assert _DUP in full_apply.output
    # Nothing was written: same ledger, no second compensating entry, B still
    # merged, A's restore untouched.
    entries = ledger.read_all()
    assert len(entries) == 2, "the refused request must not append anything"
    assert [e.candidate_ids for e in entries[1:]] == [[_DUP]]
    assert _merged_into_of(settings, _DUP_B) == _CANON, "B must stay merged"
    assert _merged_into_of(settings, _DUP) is None, "A's revival must be untouched"
    assert _related_count(settings, source=_DUP, target=_OTHER) == 1

    # 2. Partial dry-run + apply for B: the complementary candidate proceeds.
    plan_b = _plan_cli(runner, settings, monkeypatch, candidates=(_DUP_B,))
    assert plan_b["plans"][0]["selected_candidates"] == [_DUP_B]
    assert plan_b["predicted"] == {
        "mentions_restored": 1,
        "related_restored": 1,
        "directions_inferred": 1,
        "fallback_both": 0,
        "fallback_unknown": 0,
        "predicted_mirrors": 0,
    }
    result = _apply_partial(
        runner,
        backup=backup,
        approval=approval,
        candidate=_DUP_B,
        fingerprint=plan_b["fingerprint"],
    )
    assert result.exit_code == 0, result.output
    assert "drift: none" in result.output

    # Both losers alive now; B regained its own direction (in: other -> dup),
    # with no mirror (dup -> other would be one).
    assert _merged_into_of(settings, _DUP) is None
    assert _merged_into_of(settings, _DUP_B) is None
    assert _merged_into_of(settings, _CANON) is None
    assert _related_count(settings, source=_OTHER_B, target=_DUP_B) == 1
    assert _related_count(settings, source=_DUP_B, target=_OTHER_B) == 0, (
        "mirror created: B's original direction was other -> dup only"
    )
    assert _related_count(settings, source=_OTHER_B, target=_CANON) == 0
    assert _mention_target(settings, chunk_id=_CHUNK_B) == _DUP_B
    assert _canonical_aliases(settings) == []

    entries = ledger.read_all()
    assert len(entries) == 3
    compensating_b = entries[2]
    assert compensating_b.rollback_of == _SEQ
    assert compensating_b.candidate_ids == [_DUP_B]
    assert [m.duplicate_entity_id for m in compensating_b.edge_inverse_map] == [_DUP_B, _DUP_B]
    ledger.verify_chain()

    # 3. Re-requesting A is refused at PLAN time now (defect 1: the plan must
    # always equal the mutation), while the use-case backstop — reachable only
    # when RollbackMergeUseCase is driven directly or the ledger changes
    # between plan and apply — still no-ops without touching anything.
    repeat = runner.invoke(
        cli,
        ["ledger", "rollback", "--seq", str(_SEQ), "--candidate", _DUP],
    )
    assert repeat.exit_code == 1, repeat.output
    assert _DUP in repeat.output, "the already-compensated candidate must be named"
    assert len(ledger.read_all()) == 3, "the refused repeat must not append anything"

    backstop_plan = build_rollback_plan(
        [build_entry_plan(_two_candidate_entry(), [], selected_candidates=[_DUP])]
    )
    _run_candidate_aware_noop(settings, ledger, backstop_plan)

    assert len(ledger.read_all()) == 3, "the repeat must not append a compensating entry"
    ledger.verify_chain()
    assert _merged_into_of(settings, _DUP) is None
    assert _merged_into_of(settings, _DUP_B) is None
    assert _related_count(settings, source=_DUP, target=_OTHER) == 1, "A's edges stay restored"
    assert _related_count(settings, source=_OTHER_B, target=_DUP_B) == 1
    assert _mention_target(settings, chunk_id=_CHUNK_A) == _DUP
    assert _mention_target(settings, chunk_id=_CHUNK_B) == _DUP_B
