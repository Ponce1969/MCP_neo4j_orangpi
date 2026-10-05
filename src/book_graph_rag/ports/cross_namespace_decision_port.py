"""CrossNamespaceDecisionPort — persistence contract for the decision registry.

The application/CLI boundary for T9a: append-only writes plus a full read;
the R5a consumers (audit rule, enqueue detection) derive their exclusion
list through :func:`separate_entity_ids` over ``read_all()``.
"""

from __future__ import annotations

import abc

from book_graph_rag.domain.cross_namespace_decision_models import CrossNamespaceDecision


class CrossNamespaceDecisionPort(abc.ABC):
    """Append-only store of human cross-namespace decisions."""

    @abc.abstractmethod
    def read_all(self) -> list[CrossNamespaceDecision]:
        """Return every decision in insertion order.

        A missing backing file reads as an empty list (harmless no-op for
        every consumer); an unreadable record raises a typed error instead
        of being skipped.
        """

    @abc.abstractmethod
    def append(self, decision: CrossNamespaceDecision) -> bool:
        """Append ``decision`` as one record.

        Appending the exact same decision twice is a no-op (returns
        ``False`` and writes nothing); a different decision for the same
        ``(seq, candidate_id)`` key is a normal append (the latest record
        wins). Returns ``True`` when a new line was written.
        """
