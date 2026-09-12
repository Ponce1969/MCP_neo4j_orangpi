"""ScopeResolver port: resolve a validated ScopeContext from the namespace catalog.

This port belongs to the application boundary; it imports only domain models and
raises the domain security error ``InvalidScopeError`` so callers can fail closed.
"""

from __future__ import annotations

import abc

from book_graph_rag.domain.mcp_security import ScopeContext


class ScopeResolverPort(abc.ABC):
    """Abstract contract for resolving a ``ScopeContext`` from a request.

    Implementations are catalog-backed: only sources known to the catalog may
    produce a scope. Unknown, malformed, or inactive sources raise
    :class:`InvalidScopeError`.
    """

    @abc.abstractmethod
    def resolve(
        self,
        source_id: str,
        *,
        book_ids: tuple[str, ...] = (),
        entity_types: tuple[str, ...] = (),
        relationship_types: tuple[str, ...] = (),
    ) -> ScopeContext:
        """Resolve ``source_id`` into a validated ``ScopeContext``.

        Args:
            source_id: Catalog namespace identifier in ``corpus:source`` form.
            book_ids: Optional tuple of book identifiers to scope to.
            entity_types: Optional tuple of entity type names to scope to.
            relationship_types: Optional tuple of relationship type names to scope to.

        Returns:
            A frozen, validated ``ScopeContext``.

        Raises:
            InvalidScopeError: If the source id is malformed, unknown, or inactive.
        """
        ...
