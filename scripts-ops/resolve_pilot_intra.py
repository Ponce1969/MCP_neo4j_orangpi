"""Resolución quirúrgica intra-namespace del piloto (Opción A aprobada).

Fusiona los 14 grupos de duplicados lógicos del source
knowledge:essential-graphrag (mismo nombre exacto, mismo tipo) usando
ApplyMergeUseCase: soft-delete merged_into + re-point MENTIONS/RELATED +
fold aliases + ledger chained con approver human:gonzalo.
Solo toca ids del namespace del piloto. NO toca el libro 1.
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

# canonical = id más corto/descriptivo; duplicados = el resto (mismo slug real)
GROUPS = [
    ("agentic-rag-pattern", ["agentic-retrieval-augmented-generation-pattern"]),
    ("cypher-query-language-tool", ["cypher-query-tool"]),
    ("gpt-4o-tool", ["openai-gpt-4o-tool"]),
    ("graphrag-framework", ["microsoft-graphrag-framework"]),
    ("large-language-model-component", ["llm-component"]),
    ("large-language-model-concept", ["llm-concept"]),
    ("large-language-model-tool", ["llm-tool"]),
    (
        "microsoft-graphrag-implementation-framework",
        [
            "microsoft-s-graphrag-framework",
            "microsoft-s-graphrag-implementation-framework",
        ],
    ),
    ("neo4j-graph-database-tool", ["neo4j-tool"]),
    ("rag-pipeline-pattern", ["retrieval-augmented-generation-pipeline-pattern"]),
    ("retriever-tool", ["retriever-tool-tool"]),
    ("answer-given-tool", ["answer-given-tool-tool"]),
    ("extract-entities-component", ["extract-entities-function-component"]),
    ("text2cypher-tool", ["text-to-cypher-tool"]),
]


def build_group(canonical_slug: str, dup_slugs: list[str]) -> MergeGroup:
    canonical_id = f"{NS}:{canonical_slug}"
    dup_ids = [f"{NS}:{s}" for s in dup_slugs]

    def _s0(entity_id: str) -> S0NormalizedForm:
        raw = entity_id.rsplit(":", 1)[-1].replace("-", " ")
        return S0NormalizedForm(
            original=raw, nfkc=raw, casefold=raw.lower(),
            compact="".join(raw.lower().split()), tokens=tuple(raw.lower().split()),
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