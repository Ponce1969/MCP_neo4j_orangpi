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
``provenance``), the plan predicts 0 mirrors and apply restores exactly the
duplicate's original direction.

The plan is produced twice to prove the fingerprint is stable across runs
(that is what ``--expect-fingerprint`` binds to the reviewed review).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from neo4j import GraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.domain.merge_ledger_models import (
    EdgeInverseMap,
    MergeBand,
    MergeLedgerEntry,
)
from book_graph_rag.infrastructure.jsonl_merge_ledger import JSONLMergeLedger
from book_graph_rag.main import cli

pytestmark = pytest.mark.neo4j_integration

_CANON = "graphrag-agentic:api-calls-tool"
_DUP = "agentic-patterns:api-calls-tool"
_OTHER = "graphrag-agentic:tool-invocation"
_CHUNK = "graphrag-agentic:chunk-7"
_SEQ = 305
_EDGE_TYPE = "requires"
#: The loser's captured chunk_index in the provenance case (case 3).
_PROVENANCE_CHUNK = 12


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


def _mention_target(settings: Settings) -> str | None:
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        with driver.session() as session:
            record = session.run(
                "MATCH (k:Chunk {id: $chunk})-[:MENTIONS]->(e) RETURN e.id AS id",
                chunk=_CHUNK,
            ).single()
            return None if record is None else str(record["id"])
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
) -> dict[str, Any]:
    """Dry-run ``ledger rollback --seq 305 --json`` and return the plan payload."""

    class _StubSettings:
        @classmethod
        def model_validate(cls, data: object) -> Settings:
            return settings

    monkeypatch.setattr("book_graph_rag.main.Settings", _StubSettings)
    result = runner.invoke(cli, ["ledger", "rollback", "--seq", str(_SEQ), "--json"])
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
    mirror in either direction.
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
    assert "mentions restored: 1 (predicted 1)" in result.output
    assert "related restored: 1 (predicted 1)" in result.output
    assert "mirrors created: 0 (predicted 0)" in result.output
    assert "drift: none" in result.output

    # 3. Graph: exactly the duplicate's direction, no mirror counted either way.
    assert _related_count(settings, source=_DUP, target=_OTHER) == 1
    assert _related_count(settings, source=_OTHER, target=_DUP) == 0, (
        "mirror created: the provenance decision must restore one direction only"
    )
    assert _related_count(settings, source=_CANON, target=_OTHER) == 0
    assert _related_count(settings, source=_OTHER, target=_CANON) == 0, (
        "the unchanged canonical-edge removal consumes the reverse live edge"
    )
    assert _mention_target(settings) == _DUP
    assert _merged_into(settings) is None

    # 4. Ledger: compensating entry records the provenance-decided direction.
    entries = ledger.read_all()
    assert len(entries) == 2
    assert entries[1].rollback_of == _SEQ
    assert entries[1].edge_inverse_map[1].direction == "out"
    ledger.verify_chain()
