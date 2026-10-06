"""RollbackPlanPort — read-only graph probes behind a hexagonal port (T8c).

Everything the direction-aware rollback planner needs from the graph:

* :meth:`probe_related_edges` — which ``a -[type]-> b`` / ``b -[type]-> a``
  edges exist between the two entities **and each observed edge's
  properties** (the provenance rule matches the loser's captured
  ``chunk_index``/``source_page`` against them). The planner calls it with
  ``a`` = canonical (pre-apply inference) and the post-apply measurement
  calls it with ``a`` = duplicate;
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
        """Return the RELATED edges of ``edge_type`` between a and b, both ways.

        ``a_to_b`` is ``a -[type]-> b`` and ``b_to_a`` the reverse;
        ``a_to_b_edges`` / ``b_to_a_edges`` carry each observed edge's
        ``properties(r)`` so the planner can run the provenance rule. If
        either entity is missing from the graph the probe is
        ``(False, False, [], [])`` so the planner reports ``unknown`` (legacy
        fallback) instead of guessing. Read-only by contract: the
        implementation must use MATCH-only reads.
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
