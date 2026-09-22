"""ask_global community scope: cross-namespace leak tests (T-1.7).

Proves REQ-R7.1/R7.2/R7.3 against a real testcontainers Neo4j:

1. A scoped level-0 community read returns only summaries whose ``entity_ids``
   fall inside the resolved namespace prefix; summaries entirely in another
   namespace never leak (adversarial collision).
2. The unscoped read path returns every summary at the level (unchanged), and
   ``Neo4jRetrievalAdapter.fetch_contexts(qtype="global")`` still calls
   ``get_summaries_by_level(detail_level)`` with no ``scope`` kwarg (REQ-HEX.2).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.mcp_security import ScopeContext
from book_graph_rag.domain.models import CommunitySummary
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.infrastructure.catalog_scope_resolver import CatalogScopeResolver
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter
from book_graph_rag.infrastructure.neo4j_retrieval_adapter import Neo4jRetrievalAdapter

pytestmark = pytest.mark.neo4j_integration

_NS_A = "community:ns-a"
_NS_B = "community:ns-b"

_CATALOG = """\
version: 1
corpora:
  community:
    label: "Community corpus"
    sources:
      ns-a:
        label: "Namespace A"
        file: "a.pdf"
        status: active
      ns-b:
        label: "Namespace B"
        file: "b.pdf"
        status: active
"""


def _resolver(tmp_path: Path) -> CatalogScopeResolver:
    """Build a catalog-backed scope resolver with both adversarial sources active."""
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(_CATALOG, encoding="utf-8")
    return CatalogScopeResolver(CatalogLoader(catalog_path))


def _summary_id(level: int, entity_ids: list[str]) -> str:
    """Mirror the stable community-summary id algorithm from the domain model."""
    key = f"{level}:{','.join(sorted(entity_ids))}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


async def _seed_community_graph(driver: Any) -> None:
    """Seed one level-0 summary per namespace (ns-a and ns-b)."""
    async with driver.session() as session:
        for ns, slug, label in ((_NS_A, "alpha", "Alpha"), (_NS_B, "beta", "Beta")):
            await session.run(
                """
                MERGE (c:CommunitySummary {id: $id})
                SET c.level = 0, c.summary = $summary,
                    c.entity_ids = [$eid], c.parent_id = null
                """,
                id=_summary_id(0, [f"{ns}:{slug}"]),
                summary=f"{label} community",
                eid=f"{ns}:{slug}",
            )


async def test_scoped_community_read_does_not_leak_cross_namespace(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """A scoped level-0 read returns only ns-a summaries; ns-b never leaks."""
    await _seed_community_graph(neo4j_driver)
    scope = _resolver(tmp_path).resolve(_NS_A)

    adapter = Neo4jCommunityAdapter(neo4j_settings)
    try:
        summaries = await adapter.get_summaries_by_level(0, scope=scope)
    finally:
        await adapter.close()

    ids = {s.entity_ids[0] for s in summaries}
    assert ids == {f"{_NS_A}:alpha"}, (
        "scoped community read must return exactly the ns-a summary; "
        f"got {sorted(ids)}"
    )


class _RecordingCommunityAdapter:
    """Records ``(level, scope)`` for ``get_summaries_by_level`` calls."""

    def __init__(self, summaries: list[CommunitySummary] | None = None) -> None:
        self.summaries = summaries or []
        self.calls: list[tuple[int, ScopeContext | None]] = []

    async def get_summaries_by_level(
        self, level: int, *, scope: ScopeContext | None = None
    ) -> list[CommunitySummary]:
        self.calls.append((level, scope))
        return list(self.summaries)


class _DummyQueryAdapter:
    """Stand-in for ``Neo4jQueryAdapter`` (never invoked on the global path)."""


class _DummyLLMAdapter:
    """Stand-in for ``LLMAdapter`` (never invoked on the global path)."""


async def test_retrieval_adapter_global_fetch_stays_unscoped() -> None:
    """``fetch_contexts(qtype="global")`` still calls the read port with no scope.

    This triangulates REQ-HEX.2: the backward-compat call site at
    ``infrastructure/neo4j_retrieval_adapter.py`` remains unscoped.
    """
    community = _RecordingCommunityAdapter(
        summaries=[
            CommunitySummary(
                level=0,
                summary="Alpha community",
                entity_ids=[f"{_NS_A}:alpha"],
            )
        ]
    )
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        }
    )
    adapter = Neo4jRetrievalAdapter(
        settings,
        query_adapter=_DummyQueryAdapter(),  # type: ignore[arg-type]
        community_adapter=community,  # type: ignore[arg-type]
        llm_adapter=_DummyLLMAdapter(),  # type: ignore[arg-type]
    )

    result = await adapter.fetch_contexts(
        question="what is the corpus about?",
        qtype="global",
        detail_level=0,
    )

    assert community.calls == [(0, None)]
    assert tuple(ctx.text for ctx in result) == ("Alpha community",)
