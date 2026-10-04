"""Catalog-backed adapter for the ScopeResolver port (T-B.1).

Loads ``catalog.yaml`` via ``CatalogLoader`` and resolves ``corpus:source`` ids
into frozen ``ScopeContext`` values. Unknown corpora, unknown sources, inactive
sources, and malformed ids are rejected with ``InvalidScopeError``.
"""

from __future__ import annotations

from book_graph_rag.domain.mcp_security import InvalidScopeError, ScopeContext
from book_graph_rag.domain.namespaces import SourceNamespace, UnknownNamespaceError
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.ports.scope_resolver_port import ScopeResolverPort


class CatalogScopeResolver(ScopeResolverPort):
    """Resolve scopes using the authoritative ``catalog.yaml``."""

    def __init__(self, catalog_loader: CatalogLoader) -> None:
        self._catalog = catalog_loader.load()

    def _valid_values_hint(self) -> str:
        """List the active ``corpus:source`` ids so errors can teach the contract."""
        active = sorted(
            f"{corpus}:{slug}"
            for corpus, corpus_model in self._catalog.corpora.items()
            for slug, source_model in corpus_model.sources.items()
            if source_model.status == "active"
        )
        if not active:
            return ""
        return f" Valid source_id values (grammar corpus:source): {', '.join(active)}."

    def resolve(
        self,
        source_id: str,
        *,
        book_ids: tuple[str, ...] = (),
        entity_types: tuple[str, ...] = (),
        relationship_types: tuple[str, ...] = (),
    ) -> ScopeContext:
        hint = self._valid_values_hint()
        try:
            namespace = SourceNamespace.parse_book_id(source_id)
        except UnknownNamespaceError as exc:
            raise InvalidScopeError(f"Malformed scope source id: {source_id!r}.{hint}") from exc

        corpus_model = self._catalog.corpora.get(namespace.corpus)
        if corpus_model is None:
            raise InvalidScopeError(f"Unknown corpus in scope source id: {source_id!r}.{hint}")

        source_model = corpus_model.sources.get(namespace.source)
        if source_model is None:
            raise InvalidScopeError(
                f"Unknown source {namespace.source!r} in scope source id: {source_id!r}.{hint}"
            )

        if source_model.status != "active":
            raise InvalidScopeError(
                f"Source {source_id!r} is not active (status={source_model.status!r}).{hint}"
            )

        return ScopeContext(
            source=namespace,
            book_ids=book_ids,
            entity_types=entity_types,
            relationship_types=relationship_types,
        )
