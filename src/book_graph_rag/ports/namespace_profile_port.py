"""Port for reading namespace routing profiles."""

from __future__ import annotations

import abc

from book_graph_rag.domain.routing_models import NamespaceProfile


class NamespaceProfilePort(abc.ABC):
    """Contract for loading immutable namespace routing profiles.

    Implementations live in infrastructure and may read from a local artifact
    or from namespaced graph data; the application only sees profiles.
    """

    @abc.abstractmethod
    async def load_profiles(self) -> tuple[NamespaceProfile, ...]:
        """Return the active profiles in deterministic order."""
        ...
