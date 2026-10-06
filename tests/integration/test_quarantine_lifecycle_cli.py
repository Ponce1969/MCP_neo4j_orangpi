"""T6b quarantine lifecycle: enqueue → list/render → approve (§7.2 gate) → reject.

Testcontainers Neo4j only; never touches production. The seed is two
cross-namespace duplicate groups exactly as the audit rule sees them (same
``toLower(trim(name))`` + type, active entities, ≥2 namespaces):

* ``vector embeddings`` — high description overlap → the approve target;
* ``retrieval pipeline`` — cross-language, ~0 overlap → the reject target.

Two tests, deliberately split: the use-case test runs async against the shared
``neo4j_driver`` fixture, while the CLI test is synchronous because the Click
commands call ``asyncio.run`` internally (a running event loop would reject
it). The CLI test prints the ``quarantine enqueue`` output for the handoff
report.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from neo4j import GraphDatabase

from book_graph_rag.application.enqueue_cross_namespace_quarantine_use_case import (
    EnqueueCrossNamespaceQuarantineUseCase,
)
from book_graph_rag.application.review_quarantine_use_case import ReviewQuarantineUseCase
from book_graph_rag.config import Settings
from book_graph_rag.domain.duplicate_grouping import choose_canonical_id
from book_graph_rag.domain.quarantine_models import QuarantineDecision
from book_graph_rag.domain.resolution_models import ConfidenceBand
from book_graph_rag.infrastructure.jsonl_merge_ledger import JSONLMergeLedger
from book_graph_rag.infrastructure.jsonl_quarantine_writer import JSONLQuarantineWriter
from book_graph_rag.infrastructure.neo4j_quarantine_review_adapter import (
    Neo4jQuarantineReviewAdapter,
)
from book_graph_rag.main import cli

A1_ID = "knowledge:alpha:vector-embeddings"
A2_ID = "knowledge:beta:vector-embeddings"
B1_ID = "knowledge:alpha:retrieval-pipeline"
B2_ID = "knowledge:essential:retrieval-pipeline"
NEIGHBOUR_ID = "knowledge:alpha:shared-neighbour"

_REJECT_REASON = "label collision: author-specific framings"

_ENTITY_ROWS: list[dict[str, Any]] = [
    {
        "id": A1_ID,
        "name": "Vector Embeddings",
        "type": "concept",
        "description": (
            "Representación vectorial de texto que permite buscar documentos por significado."
        ),
        "source_page": 122,
        "aliases": ["Embedding vectorial"],
    },
    {
        "id": A2_ID,
        "name": "Vector Embeddings",
        "type": "concept",
        "description": (
            "Representación vectorial de texto que permite buscar documentos por significado "
            "y comparar similitud semántica."
        ),
        "source_page": 41,
        "aliases": ["Dense vector"],
    },
    {
        "id": B1_ID,
        "name": "Retrieval Pipeline",
        "type": "concept",
        "description": (
            "Pipeline de recuperación que fusiona grafo y vector para responder preguntas."
        ),
        "source_page": 10,
        "aliases": [],
    },
    {
        "id": B2_ID,
        "name": "Retrieval Pipeline",
        "type": "concept",
        "description": "Dense vector representation of text used for nearest neighbour search.",
        "source_page": 44,
        "aliases": [],
    },
    {
        "id": NEIGHBOUR_ID,
        "name": "Shared Neighbour",
        "type": "concept",
        "description": "Vecino compartido para el solape de neighborhood.",
        "source_page": 1,
        "aliases": [],
    },
]

_CHUNKS: list[tuple[str, int, str, str]] = [
    ("knowledge:alpha", 1, "Vector embeddings: representación vectorial de texto.", A1_ID),
    ("knowledge:beta", 1, "Vector embeddings for dense retrieval and search.", A2_ID),
    ("knowledge:alpha", 11, "Pipeline de recuperación grafo-vectorial.", B1_ID),
    ("knowledge:essential", 11, "Retrieval pipeline overview in the essential book.", B2_ID),
]

_RELATED: list[tuple[str, str]] = [
    (A1_ID, NEIGHBOUR_ID),
    (A2_ID, NEIGHBOUR_ID),
]

_MERGE_ENTITY = """
MERGE (e:Entity {id: $id})
SET e.name = $name, e.type = $type, e.description = $description,
    e.source_page = $source_page, e.aliases = $aliases
"""
_MERGE_CHUNK = "MERGE (c:Chunk {source_id: $source, chunk_index: $idx}) SET c.text = $text"
_LINK_MENTION = """
MATCH (c:Chunk {source_id: $source, chunk_index: $idx}), (e:Entity {id: $eid})
MERGE (c)-[:MENTIONS]->(e)
"""
_LINK_RELATED = """
MATCH (a:Entity {id: $a}), (b:Entity {id: $b})
MERGE (a)-[:RELATED {type: 'related'}]->(b)
"""


def _seed_statements() -> list[tuple[str, dict[str, Any]]]:
    statements: list[tuple[str, dict[str, Any]]] = [
        (_MERGE_ENTITY, dict(row)) for row in _ENTITY_ROWS
    ]
    for source, idx, text, entity_id in _CHUNKS:
        statements.append((_MERGE_CHUNK, {"source": source, "idx": idx, "text": text}))
        statements.append((_LINK_MENTION, {"source": source, "idx": idx, "eid": entity_id}))
    for left, right in _RELATED:
        statements.append((_LINK_RELATED, {"a": left, "b": right}))
    return statements


async def _seed_async(driver: Any) -> None:
    async with driver.session() as session:
        for cypher, params in _seed_statements():
            await session.run(cypher, params)


def _seed_sync(settings: Settings) -> None:
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        with driver.session() as session:
            for cypher, params in _seed_statements():
                session.run(cypher, params).consume()
    finally:
        driver.close()


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "quarantine.jsonl", tmp_path / "merge_ledger.jsonl"


def _pair_of(records: list[Any], first_id: str, second_id: str) -> Any:
    wanted = {first_id, second_id}
    return next(record for record in records if {record.anchor_id, record.candidate_id} == wanted)


def _merged_into(settings: Settings, entity_id: str) -> str | None:
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


# ── Use-case level: detection, seq range, idempotence, list, render ─────────


@pytest.mark.neo4j_integration
async def test_enqueue_detects_audit_groups_reports_seq_range_and_is_idempotent(
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """Detection agrees with R5a, records land with a seq range, rerun adds none."""
    await _seed_async(neo4j_driver)
    quarantine_path, ledger_path = _paths(tmp_path)
    review = Neo4jQuarantineReviewAdapter(neo4j_driver, JSONLMergeLedger(ledger_path))
    enqueue_use_case = EnqueueCrossNamespaceQuarantineUseCase(
        quarantine=JSONLQuarantineWriter(quarantine_path),
        review=review,
    )
    review_use_case = ReviewQuarantineUseCase(
        quarantine=JSONLQuarantineWriter(quarantine_path),
        review=review,
    )

    first = await enqueue_use_case.enqueue()

    assert first.groups_found == 2  # matches DUPLICATE_ENTITY_CROSS_NAMESPACE groups
    assert first.pairs_found == 2
    assert first.enqueued == 2
    assert first.skipped_existing == 0
    assert (first.seq_first, first.seq_last) == (1, 2)
    assert first.cosine_computed is False

    records = JSONLQuarantineWriter(quarantine_path).read_all()
    assert len(records) == 2
    # Highest description overlap first: the vector-embeddings pair leads.
    assert {records[0].anchor_id, records[0].candidate_id} == {A1_ID, A2_ID}
    expected_canonical = choose_canonical_id([A1_ID, A2_ID])
    for record in records:
        assert record.decision is QuarantineDecision.PENDING
        assert record.band is ConfidenceBand.MEDIUM
        assert record.canonical_id == choose_canonical_id([record.anchor_id, record.candidate_id])
        assert record.candidate_id != record.canonical_id  # never a self-merge
        assert record.evidence.s1 is None  # cosine never computed
        assert record.evidence.s2 is not None
        assert record.evidence.s3 is not None
        assert record.evidence.composite_score is not None
        assert record.evidence.cross_namespace is True
    assert _pair_of(records, A1_ID, A2_ID).canonical_id == expected_canonical

    second = await enqueue_use_case.enqueue()

    assert second.enqueued == 0
    assert second.skipped_existing == 2
    assert len(JSONLQuarantineWriter(quarantine_path).read_all()) == 2

    rows = await review_use_case.list_records()
    assert [row.seq for row in rows] == [1, 2]
    assert rows[0].label == "vector embeddings"
    assert rows[0].decision is QuarantineDecision.PENDING

    sheet = await review_use_case.render_sheet(seq=1)
    assert sheet.band is ConfidenceBand.MEDIUM
    assert sheet.decision is QuarantineDecision.PENDING
    assert sheet.cross_namespace is True
    assert sheet.evidence.cosine_computed is False
    assert {member.entity_id for member in sheet.members} == {A1_ID, A2_ID}


# ── CLI level: enqueue output, gated approve mutates, reject persists ───────


def test_cli_lifecycle_enqueue_approve_and_reject(
    neo4j_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full producer/decision loop through the Click runner (real graph)."""
    _seed_sync(neo4j_settings)
    quarantine_path, ledger_path = _paths(tmp_path)
    cli_settings = Settings.model_validate(
        {
            "neo4j_uri": neo4j_settings.neo4j_uri,
            "neo4j_user": neo4j_settings.neo4j_user,
            "neo4j_password": neo4j_settings.neo4j_password.get_secret_value(),
            "quarantine_path": str(quarantine_path),
            "merge_ledger_path": str(ledger_path),
        }
    )

    class _StubSettings:
        @classmethod
        def model_validate(cls, data: object) -> Settings:
            return cli_settings

    monkeypatch.setattr("book_graph_rag.main.Settings", _StubSettings)
    runner = CliRunner()

    # 1. Producer: records written, counts and seq range reported.
    result = runner.invoke(cli, ["quarantine", "enqueue", "--cross-namespace"])
    assert result.exit_code == 0, result.output
    print("\n--- `book-graph-rag quarantine enqueue --cross-namespace` ---")
    print(result.output)

    assert "groups found: 2" in result.output
    assert "candidate pairs: 2" in result.output
    assert "enqueued: 2" in result.output
    assert "seq 1–2" in result.output
    assert "cosine: not computed" in result.output

    writer = JSONLQuarantineWriter(quarantine_path)
    records = writer.read_all()
    assert len(records) == 2

    # 2. Idempotence: a second run records nothing new.
    result = runner.invoke(cli, ["quarantine", "enqueue", "--cross-namespace"])
    assert result.exit_code == 0, result.output
    assert "skipped (already recorded): 2" in result.output
    assert "enqueued: 0" in result.output
    assert len(writer.read_all()) == 2

    # 3. list shows the records; render --seq shows the decision sheet.
    result = runner.invoke(cli, ["quarantine", "list", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [row["seq"] for row in rows] == [1, 2]
    assert all(row["decision"] == "pending" for row in rows)

    result = runner.invoke(cli, ["quarantine", "render", "--seq", "1"])
    assert result.exit_code == 0, result.output
    assert "entity id" in result.output
    assert "cosine: no computado" in result.output

    # 4. Approve behind the §7.2 gate: the merge applies for real.
    record_a = _pair_of(records, A1_ID, A2_ID)
    backup = tmp_path / "backup.json"
    backup.write_text("{}", encoding="utf-8")
    approval = tmp_path / "approval.txt"
    approval.write_text("approve\n", encoding="utf-8")

    result = runner.invoke(
        cli,
        [
            "quarantine",
            "approve",
            "--seq",
            str(record_a.seq),
            "--backup",
            str(backup),
            "--approval",
            str(approval),
            "--reviewer",
            "human:tester",
        ],
    )
    assert result.exit_code == 0, result.output
    print("\n--- `book-graph-rag quarantine approve` ---")
    print(result.output)
    assert f"seq {record_a.seq}: approved" in result.output
    assert "ledger seq 1" in result.output
    assert "scoped" in result.output
    assert "global" in result.output

    ledger = JSONLMergeLedger(ledger_path).read_all()
    assert len(ledger) == 1
    expected_canonical = choose_canonical_id([A1_ID, A2_ID])
    merged_id = A2_ID if expected_canonical == A1_ID else A1_ID
    assert ledger[0].seq == 1
    assert ledger[0].canonical_id == expected_canonical
    assert ledger[0].candidate_ids == [merged_id]

    stored = writer.read_all()
    approved = _pair_of(stored, A1_ID, A2_ID)
    assert approved.decision is QuarantineDecision.APPROVED
    assert approved.reviewed_by == "human:tester"
    assert approved.reviewed_at is not None

    assert _merged_into(neo4j_settings, merged_id) == expected_canonical
    assert _merged_into(neo4j_settings, expected_canonical) is None

    # 5. Reject a separate pair through the CLI: REJECTED + reason persisted.
    record_b = _pair_of(records, B1_ID, B2_ID)
    result = runner.invoke(
        cli,
        [
            "quarantine",
            "reject",
            "--seq",
            str(record_b.seq),
            "--reason",
            _REJECT_REASON,
            "--reviewer",
            "human:tester",
        ],
    )
    assert result.exit_code == 0, result.output
    print("\n--- `book-graph-rag quarantine reject` ---")
    print(result.output)
    assert f"seq {record_b.seq}: rejected" in result.output

    rejected = _pair_of(writer.read_all(), B1_ID, B2_ID)
    assert rejected.decision is QuarantineDecision.REJECTED
    assert rejected.reviewed_by == "human:tester"
    assert rejected.review_note == _REJECT_REASON

    # 6. A later enqueue skips the rejected pair (and the merged group is gone).
    result = runner.invoke(cli, ["quarantine", "enqueue", "--cross-namespace"])
    assert result.exit_code == 0, result.output
    print("\n--- `book-graph-rag quarantine enqueue` after decisions ---")
    print(result.output)
    assert "groups found: 1" in result.output  # vector-embeddings group merged away
    assert "skipped (already recorded): 1" in result.output
    assert "enqueued: 0" in result.output
    assert len(writer.read_all()) == 2  # still exactly two records
