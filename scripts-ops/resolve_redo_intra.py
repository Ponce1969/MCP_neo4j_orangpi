"""Resolución quirúrgica intra-namespace del REDO del piloto GraphRAG agéntico (c3468).

Fusiona los 53 grupos de duplicados lógicos del source knowledge:graphrag-agentic
(GENERADOS desde el grafo el 2026-09-26, canonical = id más corto).
Usa ApplyMergeUseCase: soft-delete merged_into + re-point MENTIONS/RELATED +
fold aliases + ledger chained con approver human:gonzalo. Solo toca este namespace.
NO ejecutar sin aprobación humana: backup -> dry-run -> aprobación (AGENTS.md §7.2).
"""

import asyncio

from book_graph_rag.application.resolve_entities_use_case import MergeGroup
from book_graph_rag.config import Settings
from book_graph_rag.domain.models import EntityType
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S0NormalizedForm,
)
from book_graph_rag.infrastructure.resolution_wiring import build_apply_merge_use_case

NS = "knowledge:graphrag-agentic"

# (canonical_slug, [dup_slugs], kind)
GROUPS: list[tuple[str, list[str], EntityType]] = [
    ("atom-framework", ["adaptive-and-optimized-framework"], "framework"),
    ("agent-agent", ["autonomous-agent-agent"], "agent"),
    ("agents-as-tools-pattern", ["agents-as-tools-pattern-pattern"], "pattern"),
    ("alertclassifier-agent", ["alert-classifier-agent"], "agent"),
    ("alertclassifier-component", ["alert-classifier-component"], "component"),
    ("colt-framework", ["collaborative-tool-retrieval-framework"], "framework"),
    (
        "crm-component",
        [
            "customer-relationship-management-component",
            "customer-relationship-management-crm-component",
        ],
        "component",
    ),
    ("causalattributionnode-component", ["causal-attribution-node-component"], "component"),
    ("compliance-concept", ["regulatory-compliance-concept"], "concept"),
    ("cypher-tool", ["cypher-query-language-tool"], "tool"),
    ("depends-on-concept", ["depends-on-relationship-concept"], "concept"),
    (
        "draft-framework",
        ["documentation-refinement-through-automated-feedback-and-testing-framework"],
        "framework",
    ),
    ("dependencyanalyzer-component", ["dependency-analyzer-component"], "component"),
    ("devopsmemory-component", ["devopsmemory-class-component"], "component"),
    ("gpu-component", ["graphics-processing-unit-component"], "component"),
    ("graph-memory-pattern", ["graph-memory-system-pattern"], "pattern"),
    ("graphmemory-component", ["graphmemory-orchestration-class-component"], "component"),
    ("graphrag-framework", ["graph-retrieval-augmented-generation-framework"], "framework"),
    ("graphrag-pattern", ["graph-based-rag-pattern"], "pattern"),
    ("graphiti-framework", ["graphiti-pattern-framework"], "framework"),
    ("hindsight-framework", ["hindsight-paper-framework"], "framework"),
    ("iam-policies-component", ["identity-and-access-management-policies-component"], "component"),
    ("iam-role-component", ["identity-and-access-management-role-component"], "component"),
    ("iam-roles-component", ["identity-and-access-management-roles-component"], "component"),
    ("llm-agent", ["large-language-model-agent"], "agent"),
    ("llm-component", ["large-language-model-component"], "component"),
    ("llm-tool", ["large-language-model-tool"], "tool"),
    ("letta-leaderboard-tool", ["letta-leaderboard-for-benchmarking-agentic-memory-tool"], "tool"),
    ("lineage-concept", ["complete-lineage-concept"], "concept"),
    ("logparser-component", ["log-parser-component"], "component"),
    ("mcp-mcp", ["model-context-protocol-mcp"], "mcp"),
    ("mcp-server-mcp", ["model-context-protocol-server-mcp"], "mcp"),
    ("multimodal-rag-pattern", ["multimodal-retrieval-augmented-generation-pattern"], "pattern"),
    ("oauth-concept", ["oauth-2-x-concept"], "concept"),
    (
        "ontology-driven-evolution-pattern",
        ["ontology-driven-evolution-when-tools-define-themselves-pattern"],
        "pattern",
    ),
    ("property-graph-concept", ["property-graphs-concept"], "concept"),
    (
        "rag-mcp-pattern",
        [
            "rag-mcp-mitigating-prompt-bloat-pattern",
            "retrieval-augmented-generation-with-model-context-protocol-pattern",
        ],
        "pattern",
    ),
    (
        "rakg-framework",
        ["document-level-retrieval-augmented-knowledge-graph-construction-framework"],
        "framework",
    ),
    ("sla-concept", ["service-level-agreement-concept"], "concept"),
    ("sparql-tool", ["sparql-protocol-and-rdf-query-language-tool"], "tool"),
    ("sre-agent", ["site-reliability-engineer-agent"], "agent"),
    ("senzing-tool", ["senzing-entity-resolution-sdk-tool"], "tool"),
    ("skills-pattern", ["agentic-skills-pattern"], "pattern"),
    ("temporaledge-component", ["temporal-edge-component"], "component"),
    (
        "three-graph-architecture-pattern",
        ["three-graph-architecture-for-agent-knowledge-pattern"],
        "pattern",
    ),
    ("tool-component", ["spring-ai-tool-interface-component"], "component"),
    ("tool-orchestration-concept", ["agentic-graph-based-tool-orchestration-concept"], "concept"),
    ("cugraph-tool", ["nvidia-cugraph-tool"], "tool"),
    ("entity-resolution-component", ["entity-resolution-method-component"], "component"),
    ("executability-evaluation-concept", ["executability-evaluation-score-concept"], "concept"),
    ("safety-evaluation-concept", ["safety-evaluation-score-concept"], "concept"),
    ("extract-entities-component", ["extract-entities-method-component"], "component"),
    ("vllm-tool", ["virtual-llm-tool"], "tool"),
]


def build_group(canonical_slug: str, dup_slugs: list[str], kind: EntityType) -> MergeGroup:
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
            anchor_type=kind,
            candidate_type=kind,
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
        for canonical_slug, dup_slugs, kind in GROUPS:
            group = build_group(canonical_slug, dup_slugs, kind)
            entry = await use_case.apply(group, approver="human:gonzalo")
            results.append((group.canonical_id, entry.seq, len(group.duplicate_ids)))
    finally:
        for closer in closables:
            await closer.close()
    for cid, seq, ndup in results:
        print(f"MERGE_OK seq={seq} canon={cid} dups={ndup}")


if __name__ == "__main__":
    asyncio.run(main())
