"""Resolución quirúrgica intra-namespace del libro completo Essential GraphRAG.

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
    (
        "movie-concept",
        ["movie-label-concept", "movie-node-concept", "movie-node-label-concept"],
    ),
    (
        "person-concept",
        ["person-label-concept", "person-node-concept", "person-node-label-concept"],
    ),
    (
        "cuad-dataset-component",
        [
            "contract-understanding-atticus-dataset-component",
            "cuad-contract-understanding-atticus-dataset-component",
        ],
    ),
    (
        "graphrag-framework",
        [
            "graph-retrieval-augmented-generation-framework",
            "microsoft-s-graphrag-implementation-framework",
        ],
    ),
    (
        "graphrag-pattern",
        ["graph-retrieval-augmented-generation-pattern", "graphrag-pattern-pattern"],
    ),
    ("acted-in-concept", ["acted-in-relationship-type-concept"]),
    ("apoc-tool", ["awesome-procedures-on-cypher-tool"]),
    ("cuad-dataset-concept", ["contract-understanding-atticus-dataset-concept"]),
    ("directed-concept", ["directed-relationship-type-concept"]),
    ("database-schema-concept", ["graph-database-schema-concept"]),
    ("enum-concept", ["enumeration-concept"]),
    ("finetuned-llm-component", ["finetuned-large-language-model-component"]),
    ("gpt-4o-tool", ["openai-gpt-4o-tool"]),
    ("gutenberg-project-tool", ["project-gutenberg-tool"]),
    ("json-concept", ["javascript-object-notation-concept"]),
    ("llm-agent", ["large-language-model-agent"]),
    ("llm-component", ["large-language-model-component"]),
    ("llm-concept", ["large-language-model-concept"]),
    ("full-text-search-index-component", ["pdfchunkfulltext-component"]),
    ("rag-concept", ["retrieval-augmented-generation-concept"]),
    ("rag-application-concept", ["retrieval-augmented-generation-application-concept"]),
    ("rag-application-pattern", ["retrieval-augmented-generation-application-pattern"]),
    (
        "rag-application-evaluation-concept",
        ["retrieval-augmented-generation-application-evaluation-concept"],
    ),
    ("react-pattern", ["react-synergizing-reasoning-and-acting-in-language-models-pattern"]),
    ("retriever-agent-agent", ["retriever-agents-agent"]),
    ("sql-concept", ["structured-query-language-concept"]),
    ("structured-outputs-pattern", ["structured-outputs-model-definition-pattern"]),
    ("wrote-concept", ["wrote-relationship-concept"]),
    ("chunk-text-component", ["text-chunking-function-component"]),
    ("route-question-component", ["retriever-router-function-component"]),
    ("text2cypher-tool", ["text-to-cypher-tool"]),
    ("agentic-rag-pattern", ["agentic-retrieval-augmented-generation-pattern"]),
    ("cypher-query-language-tool", ["cypher-query-tool"]),
    ("large-language-model-component", ["llm-component"]),
    ("large-language-model-concept", ["llm-concept"]),
    ("rag-pipeline-pattern", ["retrieval-augmented-generation-pipeline-pattern"]),
    ("retriever-tool", ["retriever-tool-tool"]),
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
