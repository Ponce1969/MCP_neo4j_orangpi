"""Typed read-only port for graph-integrity snapshots."""
from __future__ import annotations  # noqa: I001
from abc import ABC, abstractmethod
from book_graph_rag.domain.audit_models import AuditSnapshot, AuditTarget
class GraphIntegrityAuditPort(ABC):
    """High-level audit boundary; no raw Cypher, writes, or lifecycle methods."""
    @abstractmethod
    async def collect_snapshot(self, target: AuditTarget, sample_limit: int) -> AuditSnapshot:
        """Collect a typed snapshot from the already validated target."""
        ...
