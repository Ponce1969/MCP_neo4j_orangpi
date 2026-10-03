"""Quarantine review surface (T6): ``quarantine list`` / ``quarantine render``.

Seeds a testcontainers Neo4j with three cross-namespace pairs — (a) a
same-language pair with overlapping descriptions, (b) a cross-language pair
and (c) a single-word generic pair — plus chunks whose text feeds the mention
snippets, shared ``RELATED`` neighbours, pending quarantine records and a
ledger entry, then asserts through the use case (and the Click runner for the
CLI) that:

* the sheet carries both members' facts and snippets,
* the evidence labels are right (primary vs structural), no S4 band is claimed
  and routing is always quarantine (R6.2),
* ``list --generic-only`` returns only the generic pair,
* ``--json`` output is valid and complete.

Fixture note: the seeded records carry ``band=medium`` (the maintainer's
approved ``--band medium|high`` surface); same-name cross-namespace pairs in
production may be recorded as ``exact``-band by the S0 short-circuit — a
pre-existing nuance reported with this task.

These are testcontainers integration tests and never touch production.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from neo4j import GraphDatabase

from book_graph_rag.application.review_quarantine_use_case import (
    ReviewQuarantineUseCase,
)
from book_graph_rag.config import Settings
from book_graph_rag.domain.merge_ledger_models import MergeBand, MergeLedgerEntry
from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.quarantine_models import QuarantineDecision, QuarantineRecord
from book_graph_rag.domain.quarantine_review_models import (
    EvidenceReading,
    format_sheet,
)
from book_graph_rag.domain.resolution_models import ConfidenceBand
from book_graph_rag.domain.s0_normalization import s0_match
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
C1_ID = "knowledge:alpha:agent"
C2_ID = "knowledge:essential:agent"
D1_ID = "knowledge:essential:shared-memory"
D2_ID = "knowledge:beta:shared-memory"

_A1_CONTEXT = (
    "Vector Embeddings\nRepresentación vectorial de texto\npermite buscar por significado."
)
_A1_CONTEXT_COLLAPSED = (
    "Vector Embeddings Representación vectorial de texto permite buscar por significado."
)

_LIST_ROW_KEYS = {
    "seq",
    "band",
    "entity_type",
    "label",
    "namespaces",
    "description_overlap",
    "anchor_id",
    "candidate_id",
    "generic",
    "decision",
}

_SHEET_KEYS = {
    "seq",
    "band",
    "decision",
    "created_at",
    "label",
    "entity_type",
    "generic_label",
    "cross_namespace",
    "routing",
    "members",
    "evidence",
    "shared_neighbors",
    "prior_merge",
    "thresholds",
}

# ── Seed data ───────────────────────────────────────────────────────────────

_ENTITY_ROWS: list[dict[str, Any]] = [
    {
        "id": A1_ID,
        "name": "Vector Embeddings",
        "type": "concept",
        "description": (
            "Representación vectorial de texto que permite buscar documentos por "
            "significado y medir similitud semántica."
        ),
        "source_page": 122,
        "aliases": ["Representación vectorial", "Embedding vectorial"],
    },
    {
        "id": A2_ID,
        "name": "Vector Embeddings",
        "type": "concept",
        "description": (
            "Representación vectorial de texto que permite buscar documentos por "
            "significado y comparar similitud semántica."
        ),
        "source_page": 41,
        "aliases": ["Dense vector"],
    },
    {
        "id": B1_ID,
        "name": "Retrieval Pipeline",
        "type": "concept",
        "description": (
            "Pipeline de recuperación que fusiona grafo y vector para responder "
            "preguntas complejas del dominio."
        ),
        "source_page": 10,
        "aliases": [],
    },
    {
        "id": B2_ID,
        "name": "Retrieval Pipeline",
        "type": "concept",
        "description": (
            "Dense vector representation of text used for nearest neighbour search "
            "in large retrieval systems."
        ),
        "source_page": 44,
        "aliases": [],
    },
    {
        "id": C1_ID,
        "name": "Agent",
        "type": "component",
        "description": (
            "Entidad que utiliza herramientas y planificación para cumplir un objetivo."
        ),
        "source_page": 7,
        "aliases": ["Agente"],
    },
    {
        "id": C2_ID,
        "name": "Agent",
        "type": "component",
        "description": (
            "An AI agent whose capabilities are defined and measured by benchmark evaluation."
        ),
        "source_page": 61,
        "aliases": [],
    },
    {
        "id": D1_ID,
        "name": "Shared Memory",
        "type": "concept",
        "description": (
            "Memoria compartida entre agentes para coordinar trabajo conjunto y "
            "estado a largo plazo."
        ),
        "source_page": 3,
        "aliases": [],
    },
    {
        "id": D2_ID,
        "name": "Shared Memory",
        "type": "concept",
        "description": (
            "Memoria compartida entre agentes para coordinar trabajo conjunto y estado persistente."
        ),
        "source_page": 9,
        "aliases": [],
    },
]

# (source_id, chunk_index, text, entity_id): chunk_index ranges are disjoint
# per source so the (source_id, chunk_index) MERGE keys never collide.
_CHUNKS: list[tuple[str, int, str, str]] = [
    ("knowledge:alpha", 1, _A1_CONTEXT, A1_ID),
    ("knowledge:alpha", 2, "Segunda mención de embeddings en el libro alpha.", A1_ID),
    ("knowledge:alpha", 3, "Tercera mención de embeddings en el libro alpha.", A1_ID),
    ("knowledge:beta", 1, "Vector Embeddings: representación densa en el libro beta.", A2_ID),
    ("knowledge:beta", 2, "Otra mención de embeddings en el libro beta.", A2_ID),
    ("knowledge:alpha", 11, "Pipeline de recuperación que fusiona grafo y vector.", B1_ID),
    ("knowledge:essential", 11, "Retrieval pipeline overview in the essential book.", B2_ID),
    ("knowledge:alpha", 21, "El agente planifica y utiliza herramientas.", C1_ID),
    ("knowledge:essential", 21, "An agent is evaluated with benchmarks.", C2_ID),
    ("knowledge:essential", 1, "Memoria compartida entre agentes para coordinación.", D1_ID),
    ("knowledge:beta", 11, "Memoria compartida para coordinar agentes.", D2_ID),
]

_SHARED_NEIGHBOURS = [f"knowledge:alpha:shared-neighbour-{i}" for i in range(1, 4)]
_RELATED: list[tuple[str, str]] = [
    *[(A1_ID, neighbour) for neighbour in _SHARED_NEIGHBOURS],
    *[(A2_ID, neighbour) for neighbour in _SHARED_NEIGHBOURS],
    *[(A1_ID, f"knowledge:alpha:alpha-only-{i}") for i in range(1, 5)],
    *[(A2_ID, f"knowledge:beta:beta-only-{i}") for i in range(1, 7)],
]

_NEIGHBOUR_IDS = sorted({right for _, right in _RELATED})

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
    """Cypher statements that build the seeded graph (async or sync runners)."""
    statements: list[tuple[str, dict[str, Any]]] = [
        (_MERGE_ENTITY, dict(row)) for row in _ENTITY_ROWS
    ]
    for neighbour_id in _NEIGHBOUR_IDS:
        statements.append(
            (
                _MERGE_ENTITY,
                {
                    "id": neighbour_id,
                    "name": neighbour_id.rsplit(":", 1)[-1],
                    "type": "concept",
                    "description": "Vecino del grafo usado para el solape de neighborhood.",
                    "source_page": 1,
                    "aliases": [],
                },
            )
        )
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


def _entities() -> list[Entity]:
    return [Entity.model_validate(row) for row in _ENTITY_ROWS]


def _seed_quarantine(path: Path) -> dict[int, QuarantineRecord]:
    """Write one pending record per seeded pair (seq 1..3), band medium."""
    entities = _entities()
    pairs = [(entities[0], entities[1]), (entities[2], entities[3]), (entities[4], entities[5])]
    writer = JSONLQuarantineWriter(path)
    records: dict[int, QuarantineRecord] = {}
    for seq, (anchor, candidate) in enumerate(pairs, start=1):
        record = QuarantineRecord(
            seq=seq,
            anchor_id=anchor.id,
            candidate_id=candidate.id,
            canonical_id=anchor.id,
            band=ConfidenceBand.MEDIUM,
            evidence=s0_match(anchor, candidate),
            created_at=datetime(2026, 10, 3, 18, 22, tzinfo=UTC),
            decision=QuarantineDecision.PENDING,
        )
        writer.append(record)
        records[seq] = record
    return records


def _seed_ledger(path: Path, record: QuarantineRecord) -> None:
    """Record a prior merge between the pair of ``record`` (seq 1, dated)."""
    JSONLMergeLedger(path).append(
        MergeLedgerEntry(
            seq=1,
            candidate_ids=[record.candidate_id],
            canonical_id=record.canonical_id or record.anchor_id,
            band=MergeBand.EXACT,
            evidence=[record.evidence],
            aliases_folded=[],
            edge_inverse_map=[],
            approver="test:retro-audit",
            applied_at=datetime(2026, 9, 30, 10, 5, tzinfo=UTC),
        )
    )


def _make_use_case(
    driver: Any, quarantine_path: Path, ledger_path: Path
) -> ReviewQuarantineUseCase:
    review = Neo4jQuarantineReviewAdapter(driver, JSONLMergeLedger(ledger_path))
    return ReviewQuarantineUseCase(
        quarantine=JSONLQuarantineWriter(quarantine_path),
        review=review,
    )


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "quarantine.jsonl", tmp_path / "merge_ledger.jsonl"


# ── Use-case level assertions ───────────────────────────────────────────────


@pytest.mark.neo4j_integration
async def test_render_sheet_carries_member_facts_snippets_and_identity_evidence(
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """The sheet carries both members' facts, snippets and labelled evidence."""
    await _seed_async(neo4j_driver)
    quarantine_path, ledger_path = _paths(tmp_path)
    records = _seed_quarantine(quarantine_path)
    _seed_ledger(ledger_path, records[1])
    use_case = _make_use_case(neo4j_driver, quarantine_path, ledger_path)

    sheet = await use_case.render_sheet(seq=1)

    anchor, candidate = sheet.members
    assert sheet.seq == 1
    assert sheet.band is ConfidenceBand.MEDIUM
    assert sheet.decision is QuarantineDecision.PENDING
    assert anchor.entity_id == A1_ID
    assert candidate.entity_id == A2_ID
    assert (anchor.namespace, candidate.namespace) == ("knowledge:alpha", "knowledge:beta")
    assert (anchor.source_page, candidate.source_page) == (122, 41)
    assert (anchor.alias_count, candidate.alias_count) == (2, 1)
    assert (anchor.mention_count, candidate.mention_count) == (3, 2)
    assert (anchor.related_count, candidate.related_count) == (7, 9)
    # Mention snippets: first mentioning chunk by chunk order, newlines collapsed.
    assert anchor.mention_context == _A1_CONTEXT_COLLAPSED
    assert "\n" not in anchor.mention_context
    assert candidate.mention_context
    assert anchor.description
    assert candidate.description

    # Evidence model: description_overlap is primary, no model ran, no S4 band.
    assert sheet.evidence.description_overlap >= 0.50
    assert sheet.evidence.reading is EvidenceReading.IDENTITY
    assert sheet.evidence.primary_signal == "description_overlap"
    assert sheet.evidence.structural_signals == ("mentions_jaccard", "related_jaccard")
    assert sheet.evidence.cosine_computed is False
    assert sheet.evidence.s4_band_claimed is False
    assert sheet.routing == "SIEMPRE cuarentena (spec 03 §2.4 / R6.2)"
    assert sheet.prior_merge is not None
    assert sheet.prior_merge.seq == 1

    text = format_sheet(sheet)
    assert "<- primaria (mismo idioma)" in text
    assert text.count("(estructural: sólo != 0 después de un merge)") == 2
    assert "cosine: no computado" in text
    assert "SIEMPRE cuarentena (spec 03 §2.4 / R6.2)" in text
    assert "3 chunks · knowledge:alpha" in text
    assert "2 chunks · knowledge:beta" in text
    assert "vecinos compartidos (3)" in text
    assert "seq 1 · 2026-09-30T10:05Z" in text


@pytest.mark.neo4j_integration
async def test_render_sheet_cross_language_and_generic_pairs_read_no_shared_context(
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """Cross-language and generic pairs read no shared context; generic flagged."""
    await _seed_async(neo4j_driver)
    quarantine_path, ledger_path = _paths(tmp_path)
    _seed_quarantine(quarantine_path)
    use_case = _make_use_case(neo4j_driver, quarantine_path, ledger_path)

    cross_language = await use_case.render_sheet(seq=2)
    assert cross_language.evidence.description_overlap < 0.10
    assert cross_language.evidence.reading is EvidenceReading.NO_SHARED_CONTEXT
    assert cross_language.generic_label is False
    assert cross_language.label == "retrieval pipeline"

    generic = await use_case.render_sheet(seq=3)
    assert generic.label == "agent"
    assert generic.generic_label is True
    assert generic.evidence.reading is EvidenceReading.NO_SHARED_CONTEXT


@pytest.mark.neo4j_integration
async def test_render_pair_without_record_is_honest_about_missing_record(
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """``--pair`` renders for ids with no record: no seq, no band, no decision."""
    await _seed_async(neo4j_driver)
    quarantine_path, ledger_path = _paths(tmp_path)
    _seed_quarantine(quarantine_path)
    use_case = _make_use_case(neo4j_driver, quarantine_path, ledger_path)

    sheet = await use_case.render_sheet(pair=(D1_ID, D2_ID))

    assert sheet.seq is None
    assert sheet.band is None
    assert sheet.decision is None
    assert sheet.members[0].entity_id == D1_ID
    assert sheet.members[1].entity_id == D2_ID
    assert sheet.evidence.s4_band_claimed is False
    text = format_sheet(sheet)
    assert text.splitlines()[0] == ("  seq — · band — · cross-namespace · sin registro · created —")
    assert "sin merge previo entre estos dos ids" in text


@pytest.mark.neo4j_integration
async def test_list_records_filters_and_computes_fresh_evidence(
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """Rows are seq-ordered with fresh description_overlap; filters are exact."""
    await _seed_async(neo4j_driver)
    quarantine_path, ledger_path = _paths(tmp_path)
    _seed_quarantine(quarantine_path)
    use_case = _make_use_case(neo4j_driver, quarantine_path, ledger_path)

    rows = await use_case.list_records()
    assert [row.seq for row in rows] == [1, 2, 3]
    assert rows[0].label == "vector embeddings"
    assert rows[0].namespaces == ("knowledge:alpha", "knowledge:beta")
    assert rows[0].description_overlap is not None
    assert rows[0].description_overlap >= 0.50  # fresh from the graph, not stored
    assert rows[0].generic is False

    generic_only = await use_case.list_records(generic_only=True)
    assert [(row.seq, row.label) for row in generic_only] == [(3, "agent")]
    assert generic_only[0].generic is True

    in_beta = await use_case.list_records(namespace="knowledge:beta")
    assert [row.seq for row in in_beta] == [1]
    assert [row.seq for row in await use_case.list_records(band=ConfidenceBand.MEDIUM)] == [
        1,
        2,
        3,
    ]
    assert await use_case.list_records(band=ConfidenceBand.HIGH) == []
    assert [row.seq for row in await use_case.list_records(limit=1)] == [1]
    assert len(await use_case.list_records(show_all=True)) == 3


# ── CLI level (Click runner, real wiring against the testcontainer) ─────────


def test_cli_quarantine_list_and_render(
    neo4j_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI invocations: list/JSON/filters, render text/JSON, honest absences."""
    _seed_sync(neo4j_settings)
    quarantine_path, ledger_path = _paths(tmp_path)
    records = _seed_quarantine(quarantine_path)
    _seed_ledger(ledger_path, records[1])

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

    result = runner.invoke(cli, ["quarantine", "list", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [row["seq"] for row in rows] == [1, 2, 3]
    assert set(rows[0]) == _LIST_ROW_KEYS
    assert rows[0]["description_overlap"] >= 0.50
    assert rows[0]["band"] == "medium"
    assert rows[0]["namespaces"] == ["knowledge:alpha", "knowledge:beta"]
    assert rows[2]["label"] == "agent"
    assert rows[2]["generic"] is True

    result = runner.invoke(cli, ["quarantine", "list", "--generic-only", "--json"])
    assert result.exit_code == 0, result.output
    generic_rows = json.loads(result.output)
    assert [row["label"] for row in generic_rows] == ["agent"]

    result = runner.invoke(cli, ["quarantine", "list", "--band", "high", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []

    result = runner.invoke(
        cli, ["quarantine", "list", "--namespace", "knowledge:beta", "--limit", "1", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert [row["seq"] for row in json.loads(result.output)] == [1]

    result = runner.invoke(cli, ["quarantine", "list"])
    assert result.exit_code == 0, result.output
    assert result.output.count("⚠ genérica") == 1

    result = runner.invoke(cli, ["quarantine", "render", "--seq", "1"])
    assert result.exit_code == 0, result.output
    assert "entity id" in result.output
    assert "mention context" in result.output
    assert "vecinos compartidos (3)" in result.output
    assert "cosine: no computado" in result.output

    result = runner.invoke(cli, ["quarantine", "render", "--seq", "1", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) >= _SHEET_KEYS
    assert payload["evidence"]["s4_band_claimed"] is False
    assert payload["evidence"]["reading"] == "identity"
    assert len(payload["members"]) == 2

    result = runner.invoke(cli, ["quarantine", "render", "--pair", D1_ID, D2_ID])
    assert result.exit_code == 0, result.output
    assert "sin registro" in result.output
    assert "band —" in result.output

    # T6b: enqueue, list, render, approve and reject are all registered now,
    # so there is no longer an unimplemented subcommand to assert absent.
    # Keep the intent — no command silently pretends to work: `approve`
    # guards itself and refuses to run without an explicit --seq.
    result = runner.invoke(cli, ["quarantine", "approve"])
    assert result.exit_code != 0
    assert "Provide at least one --seq" in result.output
    # Exactly one of --seq / --pair is required.
    result = runner.invoke(cli, ["quarantine", "render"])
    assert result.exit_code != 0
    result = runner.invoke(cli, ["quarantine", "render", "--seq", "1", "--pair", A1_ID, A2_ID])
    assert result.exit_code != 0
