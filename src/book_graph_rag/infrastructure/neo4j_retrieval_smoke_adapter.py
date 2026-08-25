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

    Only ``entity_lookup`` is fully supported in this slice; every other kind
    fails closed to ``unknown`` so the policy routes to instrumentation instead
    of inventing a pass.
    """

    def __init__(self, query_port: GraphQueryPort) -> None:
        self._query = query_port

    async def run_case(self, case: SmokeCase) -> SmokeResult:
        if case.kind == "entity_lookup":
            return await self._entity_lookup(case)
        return SmokeResult(
            case_id=case.case_id,
            status=SmokeOutcome.UNKNOWN,
            request_fingerprint=fingerprint_request(case.request),
            query_port=case.kind,
            matched_entity_ids=(),
            matched_chunks=(),
            evidence_ref="smoke://" + case.case_id,
        )

    async def _entity_lookup(self, case: SmokeCase) -> SmokeResult:
        name = str(
            case.request.get("entity_id")
            or case.request.get("text")
            or case.request.get("query")
            or ""
        )
        entities = await self._query.find_entity(name, None)
        matched_ids = tuple(entity.entity.id for entity in entities)
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
