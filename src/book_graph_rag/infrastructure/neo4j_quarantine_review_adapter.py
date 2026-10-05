"""Neo4j implementation of QuarantineReviewPort (T6).

Read-only adapter: every Cypher statement is a ``MATCH`` (plus read of the
JSONL merge ledger through ``MergeLedgerPort``) — it never mutates the graph.
The first mentioning chunk per entity is chosen deterministically by
``chunk_index`` (ties broken by source id); shared neighbours are the
intersection of both live neighbourhoods, sorted lexicographically.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from book_graph_rag.domain.cross_namespace_decision_models import separate_entity_ids
from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.quarantine_review_models import (
    CrossNamespaceCandidateGroup,
    EntityReviewFacts,
    PairReviewFacts,
    PriorMergeFacts,
)
from book_graph_rag.infrastructure.neo4j_audit_adapter import CROSS_NAMESPACE_DECISION_EXCLUSION
from book_graph_rag.ports.cross_namespace_decision_port import CrossNamespaceDecisionPort
from book_graph_rag.ports.merge_ledger_port import MergeLedgerPort
from book_graph_rag.ports.quarantine_review_port import QuarantineReviewPort

BATCH_SIZE = 500

_QUERY_ENTITIES = """
MATCH (e:Entity)
WHERE e.id IN $ids
RETURN e.id AS id, e.name AS name, e.type AS type,
       coalesce(e.description, '') AS description,
       e.source_page AS source_page,
       coalesce(e.aliases, []) AS aliases,
       e.canonical_name AS canonical_name
"""

_QUERY_MENTION_SOURCES = """
MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
WHERE e.id IN $ids
RETURN e.id AS id, coalesce(c.source_id, c.book_id) AS source_id, count(*) AS c
ORDER BY id, source_id
"""

_QUERY_MENTION_CONTEXT = """
MATCH (c:Chunk)-[:MENTIONS]->(e:Entity {id: $entity_id})
RETURN c.text AS text
ORDER BY c.chunk_index ASC, coalesce(c.source_id, c.book_id) ASC
LIMIT 1
"""

_QUERY_NEIGHBORS = """
MATCH (e:Entity)-[:RELATED]-(other:Entity)
WHERE e.id IN $ids AND coalesce(other.merged_into, '') = ''
RETURN DISTINCT e.id AS id, other.id AS neighbor_id
"""

_QUERY_DESCRIPTIONS = """
MATCH (e:Entity)
WHERE e.id IN $ids
RETURN e.id AS id, coalesce(e.description, '') AS description
"""

# Cross-namespace duplicate detection (T6b producer): the SAME grouping
# expression as the audit rule DUPLICATE_ENTITY_CROSS_NAMESPACE
# (infrastructure/neo4j_audit_adapter.py) — toLower(trim(n.name)) + type over
# active entities, keeping groups spanning ≥2 namespaces — so the enqueue
# count and the audit count agree by construction. T9a: both also concatenate
# the SAME decision exclusion (CROSS_NAMESPACE_DECISION_EXCLUSION, imported
# from the audit adapter so this clause cannot drift) right after the shared
# active-entity predicate and pass ``decided_separate_ids`` where the query
# runs — an empty list keeps the pre-T9a behaviour. Coordination point for T9:
# the audit's Cypher grouping is looser than the Python normalize_key
# (NFKC + whitespace collapse); unify both when R5b lands.
_QUERY_CROSS_NAMESPACE_GROUPS = (
    """
MATCH (n:Entity)
WHERE (n.merged_into IS NULL OR n.merged_into = '') AND n.id IS NOT NULL """
    + CROSS_NAMESPACE_DECISION_EXCLUSION
    + """
WITH n, split(n.id, ':') AS parts
WHERE size(parts) >= 2
WITH n, parts ORDER BY coalesce(n.id, '')
WITH n, parts[0] + ':' + parts[1] AS namespace, toLower(trim(n.name)) AS name, n.type AS kind
WITH name, kind, collect(DISTINCT namespace) AS namespaces, collect(n) AS members
WHERE size(namespaces) > 1
RETURN coalesce(name, '') AS group_key,
       coalesce(kind, '') AS entity_type,
       [x IN members | coalesce(x.id, '')] AS member_ids,
       namespaces AS namespaces
ORDER BY group_key, entity_type
"""
)

# Batched corpus coverage for the T6c risk marker: one query per ``list`` /
# ``render`` call for every label shown. Same active-entity predicate and
# same namespace-component derivation as the detection query above
# (split(n.id, ':')), grouped with the audit's toLower(trim(name))
# expression; labels are passed in as given (normalize_key outputs on the
# caller side) and unmatched labels stay absent so the caller can fall
# back — the Cypher/Python normalization divergence remains a T9 item.
_QUERY_LABEL_NAMESPACES = """
MATCH (n:Entity)
WHERE (n.merged_into IS NULL OR n.merged_into = '') AND n.id IS NOT NULL
  AND toLower(trim(n.name)) IN $labels
WITH n, split(n.id, ':') AS parts
WHERE size(parts) >= 2
WITH toLower(trim(n.name)) AS label, parts[0] + ':' + parts[1] AS namespace
RETURN label, collect(DISTINCT namespace) AS namespaces
ORDER BY label
"""


def _batched(items: list[str]) -> list[list[str]]:
    return [items[i : i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]


class Neo4jQuarantineReviewAdapter(QuarantineReviewPort):
    """Read-only review facts from Neo4j plus the merge ledger file.

    ``decisions`` is the T9a decision registry (supplied by the wiring from
    ``Settings.cross_namespace_decisions_path``); when absent — legacy test
    wiring — the detection query runs with an empty exclusion list, which is
    a no-op.
    """

    def __init__(
        self,
        driver: Any,
        ledger: MergeLedgerPort,
        decisions: CrossNamespaceDecisionPort | None = None,
    ) -> None:
        self._driver = driver
        self._ledger = ledger
        self._decisions = decisions

    async def read_pair_facts(self, anchor_id: str, candidate_id: str) -> PairReviewFacts:
        ids = [anchor_id, candidate_id]
        entities = await self._load_entities(ids)
        mentions = await self._load_mention_sources(ids)
        neighbors = await self._load_neighbors(ids)
        contexts = {entity_id: await self._load_mention_context(entity_id) for entity_id in ids}

        anchor_neighbors = neighbors.get(anchor_id, frozenset())
        candidate_neighbors = neighbors.get(candidate_id, frozenset())
        shared = sorted(anchor_neighbors & candidate_neighbors)

        return PairReviewFacts(
            anchor=self._facts(anchor_id, entities, mentions, neighbors, contexts),
            candidate=self._facts(candidate_id, entities, mentions, neighbors, contexts),
            shared_neighbor_ids=tuple(shared),
            prior_merge=self._find_prior_merge(anchor_id, candidate_id),
        )

    async def read_descriptions(self, entity_ids: Sequence[str]) -> dict[str, str]:
        descriptions: dict[str, str] = {}
        unique = list(dict.fromkeys(entity_ids))
        for batch in _batched(unique):
            async with self._driver.session() as session:
                result = await session.run(_QUERY_DESCRIPTIONS, ids=batch)
                async for record in result:
                    descriptions[str(record["id"])] = str(record["description"])
        return descriptions

    async def find_cross_namespace_candidate_groups(
        self,
    ) -> list[CrossNamespaceCandidateGroup]:
        """Run the R5a detection query; read-only (MATCH only).

        Passes ``decided_separate_ids`` (T9a) exactly like the audit rule, so
        the enqueue count and the audit count keep agreeing by construction.
        """
        separate_ids = (
            separate_entity_ids(self._decisions.read_all()) if self._decisions is not None else []
        )
        groups: list[CrossNamespaceCandidateGroup] = []
        async with self._driver.session() as session:
            result = await session.run(
                _QUERY_CROSS_NAMESPACE_GROUPS, decided_separate_ids=separate_ids
            )
            async for record in result:
                groups.append(
                    CrossNamespaceCandidateGroup(
                        group_key=str(record["group_key"]),
                        entity_type=str(record["entity_type"]),
                        member_ids=tuple(str(member) for member in record["member_ids"]),
                        namespaces=tuple(str(ns) for ns in record["namespaces"]),
                    )
                )
        return groups

    async def count_label_namespaces(self, labels: Sequence[str]) -> dict[str, int]:
        """Corpus namespace count per label — one batched read, read-only."""
        unique = sorted({label.strip() for label in labels if label.strip()})
        if not unique:
            return {}
        counts: dict[str, int] = {}
        async with self._driver.session() as session:
            result = await session.run(_QUERY_LABEL_NAMESPACES, labels=unique)
            async for record in result:
                counts[str(record["label"])] = len(record["namespaces"])
        return counts

    async def close(self) -> None:
        """Close the underlying Neo4j driver."""
        await self._driver.close()

    async def _load_entities(self, ids: list[str]) -> dict[str, Entity]:
        entities: dict[str, Entity] = {}
        async with self._driver.session() as session:
            result = await session.run(_QUERY_ENTITIES, ids=ids)
            async for record in result:
                row = record.data()
                try:
                    entity = Entity.model_validate(row)
                except ValidationError:
                    continue  # legacy type outside the EntityType contract
                entities[entity.id] = entity
        missing = [entity_id for entity_id in ids if entity_id not in entities]
        if missing:
            raise LookupError(f"Entity id(s) not found in the graph: {', '.join(missing)}")
        return entities

    async def _load_mention_sources(self, ids: list[str]) -> dict[str, tuple[int, tuple[str, ...]]]:
        """Per entity: total mentioning chunks and the sorted distinct sources."""
        counts: dict[str, int] = dict.fromkeys(ids, 0)
        sources: dict[str, set[str]] = {entity_id: set() for entity_id in ids}
        async with self._driver.session() as session:
            result = await session.run(_QUERY_MENTION_SOURCES, ids=ids)
            async for record in result:
                entity_id = str(record["id"])
                counts[entity_id] = counts.get(entity_id, 0) + int(record["c"])
                source_id = record["source_id"]
                if source_id:
                    sources.setdefault(entity_id, set()).add(str(source_id))
        return {
            entity_id: (counts.get(entity_id, 0), tuple(sorted(sources.get(entity_id, ()))))
            for entity_id in counts
        }

    @staticmethod
    def _facts(
        entity_id: str,
        entities: dict[str, Entity],
        mentions: dict[str, tuple[int, tuple[str, ...]]],
        neighbors: dict[str, frozenset[str]],
        contexts: dict[str, str],
    ) -> EntityReviewFacts:
        chunk_count, source_ids = mentions.get(entity_id, (0, ()))
        return EntityReviewFacts(
            entity=entities[entity_id],
            mention_source_ids=source_ids,
            mention_chunk_count=chunk_count,
            neighbor_ids=tuple(sorted(neighbors.get(entity_id, frozenset()))),
            mention_context=contexts.get(entity_id, ""),
        )

    async def _load_neighbors(self, ids: list[str]) -> dict[str, frozenset[str]]:
        neighbors: dict[str, set[str]] = {entity_id: set() for entity_id in ids}
        async with self._driver.session() as session:
            result = await session.run(_QUERY_NEIGHBORS, ids=ids)
            async for record in result:
                neighbors.setdefault(str(record["id"]), set()).add(str(record["neighbor_id"]))
        return {key: frozenset(value) for key, value in neighbors.items()}

    async def _load_mention_context(self, entity_id: str) -> str:
        async with self._driver.session() as session:
            result = await session.run(_QUERY_MENTION_CONTEXT, entity_id=entity_id)
            async for record in result:
                text = record["text"]
                return "" if text is None else str(text)
        return ""

    def _find_prior_merge(self, left_id: str, right_id: str) -> PriorMergeFacts | None:
        wanted = {left_id, right_id}
        entries = self._ledger.read_all()
        for entry in entries:
            involved = {entry.canonical_id, *entry.candidate_ids}
            if wanted <= involved:
                rolled_back = any(other.rollback_of == entry.seq for other in entries)
                return PriorMergeFacts(
                    seq=entry.seq,
                    applied_at=entry.applied_at,
                    rolled_back=rolled_back,
                )
        return None
