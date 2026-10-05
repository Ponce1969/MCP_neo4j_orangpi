"""JSONL-backed implementation of CrossNamespaceDecisionPort (T9a).

One decision per line in ``Settings.cross_namespace_decisions_path``
(default ``data/resolution/cross_namespace_decisions.jsonl``); the file is
created on first append together with its parent directories. Reads are
fail-closed: a line that does not parse raises ``InvalidDecisionRecord``
(never a silently skipped decision).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from book_graph_rag.domain.cross_namespace_decision_models import (
    CrossNamespaceDecision,
    DecisionKey,
    InvalidDecisionRecord,
    latest_by_key,
)
from book_graph_rag.ports.cross_namespace_decision_port import CrossNamespaceDecisionPort


class JSONLCrossNamespaceDecisions(CrossNamespaceDecisionPort):
    """Append-only JSONL decision registry with a semantic no-op."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read_all(self) -> list[CrossNamespaceDecision]:
        """Parse every JSONL line; a missing file reads as empty."""
        if not self._path.exists():
            return []
        records: list[CrossNamespaceDecision] = []
        with self._path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(CrossNamespaceDecision.model_validate_json(line))
                except ValidationError as exc:
                    raise InvalidDecisionRecord(
                        f"{self._path}:{line_number}: invalid decision record: {exc}"
                    ) from exc
        return records

    @staticmethod
    def _semantic_payload(decision: CrossNamespaceDecision) -> tuple[object, ...]:
        """The fields that make a decision a decision (stamps excluded)."""
        return (
            decision.canonical_id,
            decision.decision,
            decision.reason,
            decision.batch,
        )

    def append(self, decision: CrossNamespaceDecision) -> bool:
        """Append one JSON line unless the latest record for the key repeats it.

        The no-op is SEMANTIC: the latest record for ``(seq, candidate_id)``
        is compared on ``(canonical_id, decision, reason, batch)`` and
        ``decided_at``/``decided_by`` are ignored — the CLI stamps a fresh
        timestamp (and may name the reviewer) on every run, so whole-model
        equality would never trigger through the CLI. A changed reason or
        decision for the same key is NOT a no-op: it appends and supersedes.
        """
        latest = latest_by_key(self.read_all())
        key: DecisionKey = (decision.seq, decision.candidate_id)
        existing = latest.get(key)
        if existing is not None and self._semantic_payload(existing) == self._semantic_payload(
            decision
        ):
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(decision.model_dump_json() + "\n")
        return True
