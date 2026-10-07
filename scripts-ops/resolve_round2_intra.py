"""Resolución round 2 intra-namespace (14 grupos) Essential GraphRAG.

Fusiona los 45 grupos de duplicados lógicos (mismo nombre exacto, mismo tipo)
del source knowledge:essential-graphrag usando ApplyMergeUseCase:
soft-delete merged_into + re-point MENTIONS/RELATED + fold aliases +
ledger chained con approver human:gonzalo.
Canonical = id más corto (el slug más canónico). Solo toca este namespace.
"""

import asyncio

from book_graph_rag.application.resolve_entities_use_case import MergeGroup
from book_graph_rag.config import Settings
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S0NormalizedForm,
)
from book_graph_rag.infrastructure.resolution_wiring import build_apply_merge_use_case

NS = "knowledge:essential-graphrag"

# (canonical_slug, [dup_slugs]) — generado desde el audit el 2026-09-23
GROUPS: list[tuple[str, list[str]]] = [
    ("apoc-awesome-procedures-on-cypher-plugin-tool", ["awesome-procedures-on-cypher-plugin-tool"]),
    ("loading-via-cypher-pattern", ["loading-via-cypher-pattern-pattern"]),
    ("graphrag-local-search-pattern", ["local-search-pattern"]),
    ("merge-clause-component", ["merge-component"]),
    ("merge-clause-concept", ["merge-concept"]),
    ("neo4j-database-tool", ["neo4j-tool"]),
    ("neo4j-framework", ["neo4j-graph-database-framework"]),
    ("neo4j-aura-tool", ["neo4j-auradb-tool"]),
    ("neo4j-browser-concept", ["neo4j-browser-configuration-concept"]),
    ("neo4j-desktop-concept", ["neo4j-desktop-installation-concept"]),
    ("neo4j-docker-concept", ["neo4j-docker-installation-concept"]),
    ("on-create-set-clause-component", ["on-create-set-component"]),
    ("produced-concept", ["produced-relationship-concept"]),
    ("parent-document-retriever-pattern", ["parent-document-retriever-strategy-pattern"]),
]


def build_group(canonical_slug: str, dup_slugs: list[str]) -> MergeGroup:
    canonical_id = f"{NS}:{canonical_slug}"
    dup_ids = [f"{NS}:{s}" for s in dup_slugs]

    def _s0(entity_id: str) -> S0NormalizedForm:
        raw = entity_id.rsplit(":", 1)[-1].replace("-", " ")
        return S0NormalizedForm(
            original=raw,
            nfkc=raw,
            casefold=raw.lower(),
            compact="".join(raw.lower().split()),
            tokens=tuple(raw.lower().split()),
        )

    evidence = [
        ResolutionEvidence(
            anchor_id=canonical_id,
            candidate_id=d,
            anchor_type="concept",
            candidate_type="concept",
            anchor_namespace=NS,
            candidate_namespace=NS,
            anchor_normalized=_s0(canonical_id),
            candidate_normalized=_s0(d),
            s0_matched_field="id",
            band=ConfidenceBand.EXACT,
            cross_namespace=False,
            cross_type=False,
            composite_score=1.0,
        )
        for d in dup_ids
    ]
    return MergeGroup(
        canonical_id=canonical_id,
        duplicate_ids=dup_ids,
        band=ConfidenceBand.EXACT,
        evidence=evidence,
    )


async def main() -> None:
    settings = Settings.model_validate({})
    use_case, closables = await build_apply_merge_use_case(settings)
    results: list[tuple[str, int, int]] = []
    try:
        for canonical_slug, dup_slugs in GROUPS:
            group = build_group(canonical_slug, dup_slugs)
            entry = await use_case.apply(group, approver="human:gonzalo")
            results.append((group.canonical_id, entry.seq, len(group.duplicate_ids)))
    finally:
        for closer in closables:
            await closer.close()
    for cid, seq, ndup in results:
        print(f"MERGE_OK seq={seq} canon={cid} dups={ndup}")


if __name__ == "__main__":
    asyncio.run(main())
