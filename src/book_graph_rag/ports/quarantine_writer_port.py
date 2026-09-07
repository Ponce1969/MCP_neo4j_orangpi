"""QuarantineWriterPort — persistence contract for the human-review queue."""

from __future__ import annotations

import abc
from datetime import datetime

from book_graph_rag.domain.quarantine_models import QuarantineDecision, QuarantineRecord


class QuarantineWriterPort(abc.ABC):
    """Append-only quarantine store with atomic decision updates."""

    @abc.abstractmethod
    def append(self, record: QuarantineRecord) -> None:
        """Append a new quarantine record to the store."""

    @abc.abstractmethod
    def read_all(self) -> list[QuarantineRecord]:
        """Return every record in insertion order."""

    @abc.abstractmethod
    def read_pending(self) -> list[QuarantineRecord]:
        """Return only records awaiting human review."""

    @abc.abstractmethod
    def update_decision(
        self,
        seq: int,
        decision: QuarantineDecision,
        reviewed_by: str,
        reviewed_at: datetime,
    ) -> None:
        """Atomically update the decision of the record with ``seq``.

        Implementations MUST use temp-file + ``os.replace`` so a crash mid-write
        leaves the original file intact.
        """
