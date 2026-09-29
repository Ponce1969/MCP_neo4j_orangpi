"""Port for persisting and loading namespace routing profiles."""

from __future__ import annotations

import abc

from book_graph_rag.domain.routing_models import NamespaceProfile


class NamespaceProfileStorePort(abc.ABC):
    """Contract for a local, immutable profile artifact store."""

    @abc.abstractmethod
    async def save_all(self, profiles: tuple[NamespaceProfile, ...]) -> None:
        """Persist ``profiles`` atomically and deterministically."""
        ...

    @abc.abstractmethod
    async def load_all(self) -> tuple[NamespaceProfile, ...]:
        """Return the persisted profiles, or an empty tuple when absent."""
        ...
