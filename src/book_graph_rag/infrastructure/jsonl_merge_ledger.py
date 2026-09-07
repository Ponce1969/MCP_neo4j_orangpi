"""JSONL-backed implementation of MergeLedgerPort (Slice E2)."""

from __future__ import annotations

from pathlib import Path

from book_graph_rag.domain.merge_ledger_models import (
    MergeLedgerEntry,
    chained_hash,
    compute_entry_sha256,
)
from book_graph_rag.domain.resolution_errors import LedgerChainBroken
from book_graph_rag.ports.merge_ledger_port import MergeLedgerPort


class JSONLMergeLedger(MergeLedgerPort):
    """Append-only JSONL merge ledger with chained SHA-256."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, entry: MergeLedgerEntry) -> None:
        """Chain the entry to the previous one and append one JSONL line."""
        prev = self._last_entry_sha256_or_genesis()
        chained = chained_hash(entry, prev)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(chained.model_dump_json() + "\n")

    def read_all(self) -> list[MergeLedgerEntry]:
        """Parse every JSONL line into a ``MergeLedgerEntry``."""
        if not self._path.exists():
            return []
        records: list[MergeLedgerEntry] = []
        with self._path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                records.append(MergeLedgerEntry.model_validate_json(line))
        return records

    def read_by_seq(self, seq: int) -> MergeLedgerEntry | None:
        """Return the first entry whose ``seq`` matches."""
        for entry in self.read_all():
            if entry.seq == seq:
                return entry
        return None

    def verify_chain(self) -> None:
        """Recompute every hash and verify the chain linkage."""
        if not self._path.exists():
            return
        prev = "0" * 64
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            entry = MergeLedgerEntry.model_validate_json(line)
            recomputed = compute_entry_sha256(entry)
            if entry.entry_sha256 != recomputed:
                raise LedgerChainBroken(
                    "entry_sha256 mismatch",
                    seq=entry.seq,
                    expected=recomputed,
                    actual=entry.entry_sha256,
                )
            if entry.prev_seq_sha256 != prev:
                raise LedgerChainBroken(
                    "prev_seq_sha256 mismatch",
                    seq=entry.seq,
                    expected=prev,
                    actual=entry.prev_seq_sha256,
                )
            prev = entry.entry_sha256

    def _last_entry_sha256_or_genesis(self) -> str:
        entries = self.read_all()
        if not entries:
            return "0" * 64
        return entries[-1].entry_sha256
