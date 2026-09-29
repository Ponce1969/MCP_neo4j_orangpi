"""Port for reading namespace profile source material."""

from __future__ import annotations

import abc

from pydantic import BaseModel, ConfigDict

from book_graph_rag.domain.namespaces import SourceNamespace


class NamespaceSourceTexts(BaseModel):
    """Representative texts for one namespace, grouped by source kind."""

    model_config = ConfigDict(frozen=True)

    namespace: SourceNamespace
    chapter_titles: tuple[str, ...] = ()
    section_titles: tuple[str, ...] = ()
    chunk_texts: tuple[str, ...] = ()
    entity_names: tuple[str, ...] = ()
    entity_descriptions: tuple[str, ...] = ()
    community_summaries: tuple[str, ...] = ()

    @property
    def ordered_profile_texts(self) -> tuple[str, ...]:
        """Primary signals first (book structure), supplements after."""
        return (
            *(title for title in self.chapter_titles),
            *(title for title in self.section_titles),
            *(text for text in self.chunk_texts),
            *(name for name in self.entity_names),
            *(description for description in self.entity_descriptions),
            *(summary for summary in self.community_summaries),
        )


class NamespaceProfileSourcePort(abc.ABC):
    """Contract for reading read-only source material per active namespace."""

    @abc.abstractmethod
    async def load_source_texts(self) -> tuple[NamespaceSourceTexts, ...]:
        """Return source texts for every active catalog namespace.

        Implementations must be read-only with respect to Neo4j and return a
        deterministic order.
        """
        ...
