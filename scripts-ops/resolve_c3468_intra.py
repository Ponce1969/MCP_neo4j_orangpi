"""Resolución quirúrgica intra-namespace del piloto GraphRAG agéntico (c3468).

Fusiona los 47 grupos de duplicados lógicos del source knowledge:graphrag-agentic
usando ApplyMergeUseCase: soft-delete merged_into + re-point MENTIONS/RELATED +
fold aliases + ledger chained con approver human:gonzalo.
Canonical = id más corto (regla), con excepción semántica graphrag-framework
(el genérico graph-retrieval-augmented-generation-framework manda sobre
microsoft-graphrag-framework). Solo toca este namespace.
Generado desde /tmp/audit_c3468.json el 2026-09-24.
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

NS = "knowledge:graphrag-agentic"

GROUPS: list[tuple[str, list[str]]] = [
    ("add-memory-component", ["add-memory-method-component"]),
    ("agents-as-tools-pattern", ["agents-as-tools-pattern-pattern"]),
    ("amazon-ec2-tool", ["amazon-elastic-compute-cloud-tool"]),
    (
        "atom-framework",
        [
            "adaptive-and-optimized-dynamic-temporal-knowledge-graph-"
            "construction-framework-framework",
        ],
    ),
    ("colt-framework", ["collaborative-tool-retrieval-framework"]),
    ("create-edge-component", ["create-edge-method-component"]),
    ("cugraph-tool", ["nvidia-cugraph-tool"]),
    ("cypher-tool", ["cypher-query-language-tool"]),
    ("entity-resolution-concept", ["entity-resolution-for-infrastructure-identity-concept"]),
    ("entity-resolution-component", ["entity-resolution-method-component"]),
    ("entity-component", ["entity-node-component"]),
    ("evolve-connected-memories-component", ["evolve-connected-memories-method-component"]),
    ("expand-search-context-component", ["expand-search-context-method-component"]),
    ("extract-cognify-load-pipeline-component", ["extract-cognify-load-ecl-pipeline-component"]),
    ("extract-entities-component", ["extract-entities-method-component"]),
    ("gateway-pattern-pattern", ["the-gateway-pattern-pattern"]),
    ("gpu-component", ["graphics-processing-unit-component"]),
    ("graph-memory-concept", ["agentic-graph-memory-concept"]),
    ("graphmemory-component", ["graphmemory-class-component"]),
    # Excepción semántica: el genérico GraphRAG framework manda sobre Microsoft GraphRAG
    ("graph-retrieval-augmented-generation-framework", ["microsoft-graphrag-framework"]),
    ("graphrag-pattern", ["graph-retrieval-augmented-generation-pattern"]),
    ("iam-roles-component", ["identity-and-access-management-roles-component"]),
    ("incremental-updates-pattern", ["incremental-graph-updates-pattern"]),
    ("inference-concept", ["inference-capabilities-concept"]),
    ("letta-leaderboard-tool", ["letta-leaderboard-for-benchmarking-agentic-memory-tool"]),
    ("lineage-concept", ["data-lineage-concept"]),
    ("llm-agent", ["large-language-model-agent"]),
    ("llm-component", ["large-language-model-component"]),
    ("llm-concept", ["large-language-model-concept"]),
    ("mcp-gateway-component", ["writer-s-mcp-gateway-solution-component"]),
    ("mem1-pattern", ["mem1-approach-pattern"]),
    ("multimodal-rag-pattern", ["multimodal-retrieval-augmented-generation-pattern"]),
    ("organizational-vocabulary-concept", ["organizational-vocabularies-concept"]),
    ("pick-list-concept", ["pick-lists-concept"]),
    ("property-graphs-concept", ["labeled-property-graphs-concept"]),
    ("querymetricsapi-tool", ["devops-querymetricsapi-tool-tool"]),
    ("rag-mcp-framework", ["rag-mcp-framework-framework"]),
    ("senzing-tool", ["senzing-entity-resolution-sdk-tool"]),
    ("skos-framework", ["simple-knowledge-organization-system-framework"]),
    ("sre-agent", ["site-reliability-engineer-agent"]),
    ("structured-output-pattern", ["structured-output-for-api-integration-pattern"]),
    ("taxonomy-concept", ["taxonomies-concept"]),
    ("temporaledge-component", ["temporal-edge-component"]),
    ("thesaurus-concept", ["thesauruses-concept"]),
    ("three-graph-architecture-pattern", ["three-graph-architecture-for-agent-knowledge-pattern"]),
    ("userintent-concept", ["user-intent-concept"]),
    ("vllm-tool", ["virtual-llm-tool"]),
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
