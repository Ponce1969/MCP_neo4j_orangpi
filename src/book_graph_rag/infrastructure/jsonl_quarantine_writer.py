"""JSONL-backed implementation of QuarantineWriterPort (Slice E1)."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from book_graph_rag.domain.quarantine_models import QuarantineDecision, QuarantineRecord
from book_graph_rag.ports.quarantine_writer_port import QuarantineWriterPort


class JSONLQuarantineWriter(QuarantineWriterPort):
    """Append-only JSONL quarantine file with atomic decision updates."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, record: QuarantineRecord) -> None:
        """Append one record as a single JSONL line."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")

    def read_all(self) -> list[QuarantineRecord]:
        """Parse every JSONL line into a ``QuarantineRecord``."""
        if not self._path.exists():
            return []
        records: list[QuarantineRecord] = []
        with self._path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                records.append(QuarantineRecord.model_validate_json(line))
        return records

    def read_pending(self) -> list[QuarantineRecord]:
        """Return only records whose decision is ``PENDING``."""
        return [r for r in self.read_all() if r.decision == QuarantineDecision.PENDING]

    def update_decision(
        self,
        seq: int,
        decision: QuarantineDecision,
        reviewed_by: str,
        reviewed_at: datetime,
    ) -> None:
        """Rewrite the file atomically after updating the matching record."""
        self._rewrite(seq, decision, reviewed_by, reviewed_at, review_note=None)

    def update_decision_with_note(
        self,
        seq: int,
        decision: QuarantineDecision,
        reviewed_by: str,
        reviewed_at: datetime,
        review_note: str | None = None,
    ) -> None:
        """Persist decision + ``review_note`` in one atomic rewrite (T6b).

        ``review_note=None`` behaves exactly like ``update_decision`` and
        leaves any existing note untouched; a real note replaces it.
        """
        self._rewrite(seq, decision, reviewed_by, reviewed_at, review_note=review_note)

    def _rewrite(
        self,
        seq: int,
        decision: QuarantineDecision,
        reviewed_by: str,
        reviewed_at: datetime,
        *,
        review_note: str | None,
    ) -> None:
        """Update the record with ``seq`` and rewrite every line atomically."""
        records = self.read_all()
        updated: list[QuarantineRecord] = []
        found = False
        for record in records:
            if record.seq == seq:
                update: dict[str, object] = {
                    "decision": decision,
                    "reviewed_by": reviewed_by,
                    "reviewed_at": reviewed_at,
                }
                if review_note is not None:
                    update["review_note"] = review_note
                updated.append(record.model_copy(update=update))
                found = True
            else:
                updated.append(record)
        if not found:
            raise ValueError(f"Quarantine record with seq={seq} not found")

        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for record in updated:
                f.write(record.model_dump_json() + "\n")
        os.replace(tmp, self._path)
