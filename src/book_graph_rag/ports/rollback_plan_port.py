"""RollbackPlanPort — read-only graph probes behind a hexagonal port (T8c).

Everything the direction-aware rollback planner needs from the graph:

* :meth:`probe_related_edges` — does ``a -[type]-> b`` / ``b -[type]-> a``
  exist? The planner calls it with ``a`` = canonical (pre-apply inference) and
  the post-apply measurement calls it with ``a`` = duplicate;
* :meth:`read_edge_census` — MENTIONS/RELATED counts per affected entity plus
  the db-wide MENTIONS/RELATED totals and the ``merged_into`` count, for the
  before/after census the CLI prints and compares.

Read-only by contract: no method may mutate the graph.
"""

from __future__ import annotations

import abc

from book_graph_rag.domain.rollback_plan_models import EdgeCensus, RelatedEdgeProbe


class RollbackPlanPort(abc.ABC):
    """Read-only graph observations for rollback planning and verification."""

    @abc.abstractmethod
    async def probe_related_edges(
        self,
        *,
        a_id: str,
        b_id: str,
        edge_type: str,
    ) -> RelatedEdgeProbe:
        """Return which RELATED directions of ``edge_type`` exist between a and b.

        ``a_to_b`` is ``a -[type]-> b`` and ``b_to_a`` the reverse. If either
        entity is missing from the graph the probe is ``(False, False)`` so the
        planner reports ``unknown`` (legacy fallback) instead of guessing.
        """
        ...

    @abc.abstractmethod
    async def read_edge_census(self, entity_ids: list[str]) -> EdgeCensus:
        """Read the edge census for ``entity_ids`` plus db-wide totals.

        Per-entity counts cover MENTIONS (inbound) and RELATED (both
        directions); totals cover every MENTIONS/RELATED edge in the database
        and the number of entities carrying ``merged_into``.
        """
        ...
