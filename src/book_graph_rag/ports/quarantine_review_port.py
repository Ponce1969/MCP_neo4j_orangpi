"""QuarantineReviewPort — read-only graph facts for the review sheet (T6).

The application layer must not import ``infrastructure``: every graph read
that feeds ``quarantine list``/``render`` crosses this port.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence

from book_graph_rag.domain.quarantine_review_models import (
    CrossNamespaceCandidateGroup,
    PairReviewFacts,
)


class QuarantineReviewPort(abc.ABC):
    """Read-only facts for one entity pair's quarantine decision sheet."""

    @abc.abstractmethod
    async def read_pair_facts(self, anchor_id: str, candidate_id: str) -> PairReviewFacts:
        """Return both entities with mention/neighbour sets, the first
        mentioning chunk's text per entity, their shared neighbours and any
        prior ledger merge between the two ids.

        Raises ``LookupError`` when either entity id does not exist.
        """

    @abc.abstractmethod
    async def read_descriptions(self, entity_ids: Sequence[str]) -> dict[str, str]:
        """Return ``{entity_id: description}`` for the ids found in the graph."""

    @abc.abstractmethod
    async def find_cross_namespace_candidate_groups(
        self,
    ) -> list[CrossNamespaceCandidateGroup]:
        """Detect the cross-namespace duplicate population (T6b producer).

        One entry per group: active entities sharing ``toLower(trim(name))``
        + ``type`` across at least two namespaces — the same detection as the
        ``DUPLICATE_ENTITY_CROSS_NAMESPACE`` audit rule so the enqueue count
        and the audit count agree. The Cypher lives in the adapter; the use
        case never carries a query of its own.
        """

    async def count_label_namespaces(self, labels: Sequence[str]) -> dict[str, int]:
        """Batched corpus coverage: ``{label: distinct corpus:source namespaces}``.

        One read for every label the caller shows (T6c correction): the
        namespace component derives exactly like the detection query
        (``split(id, ':')``) and labels group with the audit's
        ``toLower(trim(name))`` expression — the T9 unification with
        ``normalize_key`` still applies. Labels matching nothing are absent
        from the result so callers can fall back to the record's own pair.

        Non-abstract on purpose: adapters serving ``list``/``render`` must
        override it, while stubs that never risk-marker (the enqueue tests)
        keep implementing only the abstract reads; the default raises rather
        than silently reporting a wrong corpus count.
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot count label namespaces; override count_label_namespaces"
        )

    @abc.abstractmethod
    async def close(self) -> None:
        """Release the underlying resources."""
