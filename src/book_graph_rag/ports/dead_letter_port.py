"""Port for writing structured dead-letter records.

The dead letter is an audit sink, not a primary datastore. Implementations
MUST be append-only and thread-/async-safe for concurrent writes.
"""

from __future__ import annotations

import abc
from typing import Any


class DeadLetterPort(abc.ABC):
    """Contract for structured dead-letter persistence."""

    @abc.abstractmethod
    async def write_orphan_relationship(self, record: dict[str, Any]) -> None:
        """Append an orphan relationship record to the dead-letter log.

        The caller is responsible for supplying ``type``, ``source_entity_id``,
        ``target_entity_id``, ``description``, ``source_page``,
        ``missing_endpoint``, ``chunk_index`` and ``reason``. Implementations
        SHOULD add a ``timestamp`` field before persisting.
        """
        ...
