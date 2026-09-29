"""Runtime namespace profile reader backed by the local JSON artifact."""

from __future__ import annotations

from book_graph_rag.domain.routing_models import NamespaceProfile
from book_graph_rag.infrastructure.json_namespace_profile_store import (
    JsonNamespaceProfileStore,
)
from book_graph_rag.ports.namespace_profile_port import NamespaceProfilePort


class JsonNamespaceProfileReader(NamespaceProfilePort):
    """Expose persisted profiles to the runtime router."""

    def __init__(self, store: JsonNamespaceProfileStore) -> None:
        self._store = store

    async def load_profiles(self) -> tuple[NamespaceProfile, ...]:
        """Return the persisted profiles (empty tuple when no artifact yet)."""
        return await self._store.load_all()
