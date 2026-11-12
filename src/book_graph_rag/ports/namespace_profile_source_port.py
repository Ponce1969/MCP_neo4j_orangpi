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
    def identified_profile_texts(self) -> tuple[tuple[str, str], ...]:
        """``(identifier, text)`` pairs in the canonical profile order.

        The identifier is ``kind:ordinal`` with a 0-based ordinal inside its
        kind (S1), so any prototype can be traced back to the exact text that
        produced it. Ordinals are stable across rebuilds because they follow
        the adapter's deterministic query order (chapter number, section page,
        chunk index, entity id, community id).
        """
        return tuple(
            (f"{kind}:{ordinal}", text)
            for kind, texts in (
                ("chapter", self.chapter_titles),
                ("section", self.section_titles),
                ("chunk", self.chunk_texts),
                ("entity_name", self.entity_names),
                ("entity_desc", self.entity_descriptions),
                ("summary", self.community_summaries),
            )
            for ordinal, text in enumerate(texts)
        )

    @property
    def ordered_profile_texts(self) -> tuple[str, ...]:
        """Primary signals first (book structure), supplements after."""
        return tuple(text for _identifier, text in self.identified_profile_texts)

    @property
    def ordered_profile_text_ids(self) -> tuple[str, ...]:
        """Identifiers parallel to ``ordered_profile_texts``, same order."""
        return tuple(identifier for identifier, _text in self.identified_profile_texts)


class NamespaceProfileSourcePort(abc.ABC):
    """Contract for reading read-only source material per active namespace."""

    @abc.abstractmethod
    async def load_source_texts(self) -> tuple[NamespaceSourceTexts, ...]:
        """Return source texts for every active catalog namespace.

        Implementations must be read-only with respect to Neo4j and return a
        deterministic order.
        """
        ...
