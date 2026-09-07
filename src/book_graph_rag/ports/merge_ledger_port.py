"""MergeLedgerPort — persistence contract for the tamper-evident merge ledger."""

from __future__ import annotations

import abc

from book_graph_rag.domain.merge_ledger_models import MergeLedgerEntry


class MergeLedgerPort(abc.ABC):
    """Append-only ledger with per-entry SHA-256 and chained hashes."""

    @abc.abstractmethod
    def append(self, entry: MergeLedgerEntry) -> None:
        """Append a new entry; compute and record its chained hash."""

    @abc.abstractmethod
    def read_all(self) -> list[MergeLedgerEntry]:
        """Return every ledger entry in insertion order."""

    @abc.abstractmethod
    def read_by_seq(self, seq: int) -> MergeLedgerEntry | None:
        """Return the entry with ``seq`` or ``None`` if absent."""

    @abc.abstractmethod
    def verify_chain(self) -> None:
        """Walk the ledger from genesis; raise ``LedgerChainBroken`` on tampering."""
