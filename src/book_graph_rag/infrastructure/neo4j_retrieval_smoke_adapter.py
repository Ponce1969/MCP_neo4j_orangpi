"""Read-only retrieval smoke adapter over the existing graph query port."""
from __future__ import annotations

from book_graph_rag.domain.validation_models import (
    SmokeCase,
    SmokeOutcome,
    SmokeResult,
    fingerprint_request,
)
from book_graph_rag.ports.graph_query_port import GraphQueryPort
from book_graph_rag.ports.retrieval_smoke_port import RetrievalSmokePort


class Neo4jRetrievalSmokeAdapter(RetrievalSmokePort):
    """Run deterministic retrieval cases through the read-only query port.

    ``entity_lookup``, ``relationship_lookup``, and ``two_hop_path`` are
    supported because the underlying query port exposes stable identities.
    ``chunk_search`` fails closed to ``unknown``: the current ``search_chunks``
    returns only text/page/score, not the ``chunk_id``/``book_id`` provenance
    required by a deterministic provenance-required case.
    """

    def __init__(self, query_port: GraphQueryPort) -> None:
        self._query = query_port

    async def run_case(self, case: SmokeCase) -> SmokeResult:
        if case.kind == "entity_lookup":
            return await self._entity_lookup(case)
        if case.kind == "relationship_lookup":
            return await self._relationship_lookup(case)
        if case.kind == "two_hop_path":
            return await self._two_hop_path(case)
        return self._unknown(case)

    async def _entity_lookup(self, case: SmokeCase) -> SmokeResult:
        name = str(
            case.request.get("entity_id")
            or case.request.get("text")
            or case.request.get("query")
            or ""
        )
        entities = await self._query.find_entity(name, None)
        matched_ids = tuple(str(entity.entity.id) for entity in entities)
        status = SmokeOutcome.PASS if matched_ids else SmokeOutcome.FAIL
        return SmokeResult(
            case_id=case.case_id,
            status=status,
            request_fingerprint=fingerprint_request(case.request),
            query_port="entity_lookup",
            matched_entity_ids=matched_ids,
            matched_chunks=(),
            evidence_ref="smoke://" + case.case_id,
        )

    async def _relationship_lookup(self, case: SmokeCase) -> SmokeResult:
        source = str(case.request.get("source_entity_id") or "")
        target = str(case.request.get("target_entity_id") or "")
        _, relationships = await self._query.traverse_relationships(source, None, 1)
        triples = tuple(
            (str(rel.source_entity_id), str(rel.type), str(rel.target_entity_id))
            for rel in relationships
            if not target or rel.target_entity_id == target
        )
        status = SmokeOutcome.PASS if triples else SmokeOutcome.FAIL
        return SmokeResult(
            case_id=case.case_id,
            status=status,
            request_fingerprint=fingerprint_request(case.request),
            query_port="relationship_lookup",
            matched_entity_ids=(),
            matched_relationship_triples=triples,
            matched_chunks=(),
            evidence_ref="smoke://" + case.case_id,
        )

    async def _two_hop_path(self, case: SmokeCase) -> SmokeResult:
        start = str(case.request.get("source_entity_id") or "")
        end = str(case.request.get("target_entity_id") or "")
        paths = await self._query.find_path(start, end, 2)
        matched_paths = tuple(
            tuple(
                (str(rel.source_entity_id), str(rel.type), str(rel.target_entity_id))
                for rel in path.relationships
            )
            for path in paths
        )
        status = SmokeOutcome.PASS if matched_paths else SmokeOutcome.FAIL
        return SmokeResult(
            case_id=case.case_id,
            status=status,
            request_fingerprint=fingerprint_request(case.request),
            query_port="two_hop_path",
            matched_entity_ids=(),
            matched_paths=matched_paths,
            matched_chunks=(),
            evidence_ref="smoke://" + case.case_id,
        )

    def _unknown(self, case: SmokeCase) -> SmokeResult:
        return SmokeResult(
            case_id=case.case_id,
            status=SmokeOutcome.UNKNOWN,
            request_fingerprint=fingerprint_request(case.request),
            query_port=case.kind,
            matched_entity_ids=(),
            matched_chunks=(),
            evidence_ref="smoke://" + case.case_id,
        )
