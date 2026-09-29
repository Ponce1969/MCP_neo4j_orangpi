"""Read-only Neo4j source material for namespace routing profiles.

This adapter never mutates the graph: every statement is a parameterized
``MATCH`` executed through a session restricted to the read database in
``READ_ACCESS`` mode.
"""

from __future__ import annotations

from typing import Any

from neo4j import READ_ACCESS, AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.ports.namespace_profile_source_port import (
    NamespaceProfileSourcePort,
    NamespaceSourceTexts,
)

_CHAPTER_TITLES = """
MATCH (ch:Chapter)
WHERE ch.book_id = $source_id
RETURN ch.title AS title
ORDER BY ch.number
"""

_SECTION_TITLES = """
MATCH (s:Section)
WHERE s.book_id = $source_id
RETURN s.title AS title
ORDER BY s.page_start
LIMIT $cap
"""

_CHUNK_TEXTS = """
MATCH (k:Chunk)
WHERE k.book_id = $source_id
RETURN k.text AS text
ORDER BY k.chunk_index
LIMIT $cap
"""

_ENTITY_SUPPLEMENTS = """
MATCH (e:Entity)
WHERE e.id STARTS WITH $prefix
  AND (e.merged_into IS NULL OR e.merged_into = '')
RETURN e.name AS name, e.description AS description
LIMIT $cap
"""

_COMMUNITY_SUPPLEMENTS = """
MATCH (c:CommunitySummary)
WHERE ANY(x IN c.entity_ids WHERE x STARTS WITH $prefix)
RETURN c.summary AS summary
LIMIT $cap
"""


class Neo4jNamespaceProfileSource(NamespaceProfileSourcePort):
    """Read-only source adapter backed by Neo4j and the versioned catalog."""

    def __init__(
        self,
        settings: Settings,
        catalog_loader: CatalogLoader,
        *,
        cap_chunk_texts: int = 200,
        cap_titles: int = 500,
        cap_entities: int = 500,
        cap_summaries: int = 100,
        driver: Any | None = None,
    ) -> None:
        self._settings = settings
        self._catalog = catalog_loader.load()
        self._cap_chunk_texts = cap_chunk_texts
        self._cap_titles = cap_titles
        self._cap_entities = cap_entities
        self._cap_summaries = cap_summaries
        self._driver: Any = driver or AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
        )

    async def close(self) -> None:
        """Close the underlying Neo4j driver."""
        await self._driver.close()

    def _read_session(self) -> Any:
        """Open a session restricted to the read database in READ_ACCESS mode."""
        return self._driver.session(
            database=self._settings.neo4j_read_database,
            default_access_mode=READ_ACCESS,
        )

    @staticmethod
    def _active_namespaces(catalog: Any) -> tuple[SourceNamespace, ...]:
        """Return the active catalog namespaces in deterministic order."""
        namespaces: list[SourceNamespace] = []
        for corpus, corpus_data in sorted(catalog.corpora.items()):
            for source, source_data in sorted(corpus_data.sources.items()):
                if source_data.status == "active":
                    namespaces.append(SourceNamespace(corpus=corpus, source=source))
        return tuple(namespaces)

    async def load_source_texts(self) -> tuple[NamespaceSourceTexts, ...]:
        """Load source material for every active catalog namespace, read-only."""
        result: list[NamespaceSourceTexts] = []
        async with self._read_session() as session:
            for namespace in self._active_namespaces(self._catalog):
                source_id = namespace.source_id
                prefix = f"{source_id}:"
                chapter_titles = await self._collect_titles(
                    session,
                    _CHAPTER_TITLES,
                    {"source_id": source_id},
                )
                section_titles = await self._collect_titles(
                    session,
                    _SECTION_TITLES,
                    {"source_id": source_id, "cap": self._cap_titles},
                )
                chunk_texts = await self._collect_texts(
                    session,
                    _CHUNK_TEXTS,
                    {"source_id": source_id, "cap": self._cap_chunk_texts},
                )
                entity_names, entity_descriptions = await self._collect_entities(
                    session,
                    _ENTITY_SUPPLEMENTS,
                    {"prefix": prefix, "cap": self._cap_entities},
                )
                community_summaries = await self._collect_texts(
                    session,
                    _COMMUNITY_SUPPLEMENTS,
                    {"prefix": prefix, "cap": self._cap_summaries},
                )
                result.append(
                    NamespaceSourceTexts(
                        namespace=namespace,
                        chapter_titles=chapter_titles,
                        section_titles=section_titles,
                        chunk_texts=chunk_texts,
                        entity_names=entity_names,
                        entity_descriptions=entity_descriptions,
                        community_summaries=community_summaries,
                    )
                )
        return tuple(result)

    async def _collect_titles(
        self,
        session: Any,
        query: str,
        params: dict[str, Any],
    ) -> tuple[str, ...]:
        result = await session.run(query, params)
        return tuple([record["title"] async for record in result])

    async def _collect_texts(
        self,
        session: Any,
        query: str,
        params: dict[str, Any],
    ) -> tuple[str, ...]:
        result = await session.run(query, params)
        return tuple([record["text"] async for record in result])

    async def _collect_entities(
        self,
        session: Any,
        query: str,
        params: dict[str, Any],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        result = await session.run(query, params)
        names: list[str] = []
        descriptions: list[str] = []
        async for record in result:
            names.append(record["name"])
            descriptions.append(record["description"])
        return tuple(names), tuple(descriptions)
