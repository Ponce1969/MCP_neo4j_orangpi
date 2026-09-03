"""Knowledge namespaces: corpus-scoped identity for books and entities.

Every indexed book and entity carries a namespace prefix so that equal slugs in
different corpora or sources never collide. Id formats:

* Book:    ``corpus:source``
* Entity:  ``corpus:source:slug-type``

``corpus`` and ``source`` are catalog-managed slugs; the source is always present
(separation by default). This module is pure domain: stdlib + pydantic only.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

SEPARATOR = ":"


class UnknownNamespaceError(ValueError):
    """Raised when a ``(corpus, source)`` pair is absent from the catalog."""


class SourceNamespace(BaseModel):
    """A ``(corpus, source)`` pair that scopes book and entity ids."""

    model_config = ConfigDict(frozen=True)

    corpus: str
    source: str

    @property
    def source_id(self) -> str:
        """Return ``corpus:source`` — also the stable ``Book.id``."""
        return f"{self.corpus}{SEPARATOR}{self.source}"

    def book_id(self) -> str:
        """Return the namespaced book id ``corpus:source``."""
        return self.source_id

    def entity_id(self, slug: str, entity_type: str) -> str:
        """Return the namespaced entity id ``corpus:source:slug-type``."""
        return f"{self.source_id}{SEPARATOR}{slug}-{entity_type}"

    @classmethod
    def parse_book_id(cls, book_id: str) -> SourceNamespace:
        """Recover the namespace from a book id (inverse of ``book_id``)."""
        parts = book_id.split(SEPARATOR)
        if len(parts) != 2 or not all(parts):
            raise UnknownNamespaceError(f"Malformed book id: {book_id!r}")
        return cls(corpus=parts[0], source=parts[1])

    @classmethod
    def parse_entity_id(cls, entity_id: str) -> tuple[SourceNamespace, str, str]:
        """Recover ``(namespace, slug, type)`` from an entity id.

        Inverse of ``entity_id``: ``corpus:source:slug-type``.
        """
        parts = entity_id.split(SEPARATOR)
        if len(parts) != 3:
            raise UnknownNamespaceError(f"Malformed entity id: {entity_id!r}")
        corpus, source, slug_type = parts
        slug, _, entity_type = slug_type.rpartition("-")
        if not slug or not entity_type:
            raise UnknownNamespaceError(f"Malformed entity id: {entity_id!r}")
        return cls(corpus=corpus, source=source), slug, entity_type


class Source(BaseModel):
    """A single source (book) entry in the catalog."""

    model_config = ConfigDict(frozen=True)

    label: str
    file: str
    status: str = "active"


class Corpus(BaseModel):
    """A corpus (collection of sources) in the catalog."""

    model_config = ConfigDict(frozen=True)

    label: str
    sources: dict[str, Source]


class Catalog(BaseModel):
    """The versioned catalog of corpora and sources."""

    model_config = ConfigDict(frozen=True)

    version: int
    corpora: dict[str, Corpus]

    def resolve_source(self, corpus: str, source: str) -> SourceNamespace:
        """Resolve a ``(corpus, source)`` pair into a namespace.

        Raises ``UnknownNamespaceError`` when either the corpus or the source is
        unknown, so unknown namespaces fail fast before indexing.
        """
        corpus_model = self.corpora.get(corpus)
        if corpus_model is None:
            raise UnknownNamespaceError(f"Unknown corpus: {corpus!r}")
        source_model = corpus_model.sources.get(source)
        if source_model is None:
            raise UnknownNamespaceError(f"Unknown source {source!r} in corpus {corpus!r}")
        return SourceNamespace(corpus=corpus, source=source)
