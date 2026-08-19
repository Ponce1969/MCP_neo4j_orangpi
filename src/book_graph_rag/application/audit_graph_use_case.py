"""Pure target validation and report orchestration."""
from __future__ import annotations  # noqa: I001
import re
from typing import Literal, cast
from urllib.parse import urlsplit
from book_graph_rag.domain.audit_models import (  # noqa: E501
    AuditExecution, AuditQueryExecution, AuditReport, AuditSnapshot, AuditTarget,
    OverallState, QueryState, exit_code,
)
from book_graph_rag.ports.graph_audit_port import GraphIntegrityAuditPort
_SAFE_DB = re.compile(r"[A-Za-z0-9_.-]+\Z")
_SCHEMES = {"bolt", "neo4j", "neo4j+s", "neo4j+ssc"}
def build_audit_target(selector: str, uri: str, database: str) -> AuditTarget:
    if selector != "bookgraph-neo4j" or not _SAFE_DB.fullmatch(database):
        raise ValueError("audit target or database is not allowed")
    parsed = urlsplit(uri)
    if parsed.scheme not in _SCHEMES or not parsed.hostname:
        raise ValueError("configured URI is not a supported Neo4j endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise ValueError("userinfo, query, fragment, and path are not allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("configured URI has an invalid port") from exc
    return AuditTarget(  # noqa: E501
        selector="bookgraph-neo4j", database=database, scheme=cast(Literal["bolt", "neo4j", "neo4j+s", "neo4j+ssc"], parsed.scheme),  # noqa: E501
        host=parsed.hostname, port=port, uri=uri,
    )
def _failure_snapshot(error: Exception) -> AuditSnapshot:
    name = type(error).__name__.lower()
    state = OverallState.UNREACHABLE if any(  # noqa: E501
        x in name for x in ("connection", "timeout", "auth", "network", "unavailable")
    ) else OverallState.FAILED
    query_state = QueryState.UNREACHABLE if state == OverallState.UNREACHABLE else QueryState.FAILED
    return AuditSnapshot(  # noqa: E501
        queries=(AuditQueryExecution(name="audit", state=query_state, error="redacted"),),
        failure_state=state,
    )
class AuditGraphUseCase:
    """Assemble a report from a typed port without importing infrastructure."""
    def __init__(self, port: GraphIntegrityAuditPort) -> None:
        self._port = port
    async def execute(self, target: AuditTarget, sample_limit: int = 50) -> AuditReport:
        self._validate_target(target)
        if not isinstance(sample_limit, int) or isinstance(sample_limit, bool) or sample_limit < 0:
            raise ValueError("sample_limit must be a non-negative integer")
        try:
            snapshot = await self._port.collect_snapshot(target, sample_limit)
        except Exception as error:  # provider details never enter the report
            snapshot = _failure_snapshot(error)
        state = self._classify(snapshot)
        return AuditReport(  # noqa: E501
            target=target, state=state, inventory=snapshot.inventory, findings=snapshot.findings,
            runtime=snapshot.runtime, executed_at=snapshot.executed_at,
            execution=AuditExecution(state=state, exit_code=exit_code(state), queries=snapshot.queries),  # noqa: E501
        )
    @staticmethod
    def _validate_target(target: AuditTarget) -> None:
        parsed = urlsplit(target.uri)
        if (
            target.selector != "bookgraph-neo4j"
            or not _SAFE_DB.fullmatch(target.database)
            or parsed.scheme not in _SCHEMES
            or parsed.hostname != target.host
            or any((parsed.username, parsed.password, parsed.query, parsed.fragment, parsed.path))
        ):
            raise ValueError("audit target is not allowed")
    @staticmethod
    def _classify(snapshot: AuditSnapshot) -> OverallState:
        if snapshot.failure_state in (OverallState.UNREACHABLE, OverallState.FAILED):
            return snapshot.failure_state
        if any(q.state == QueryState.UNREACHABLE for q in snapshot.queries):
            return OverallState.UNREACHABLE
        if any(q.state == QueryState.FAILED for q in snapshot.queries):
            return OverallState.FAILED
        if snapshot.provenance_incomplete or any(  # noqa: E501
            q.state == QueryState.UNSUPPORTED for q in snapshot.queries
        ):
            return OverallState.INCOMPLETE
        if any(f.severity == "blocking" and f.total for f in snapshot.findings):
            return OverallState.VIOLATIONS
        return OverallState.PASSED
