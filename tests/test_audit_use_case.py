import pytest  # noqa: I001
from book_graph_rag.application.audit_graph_use_case import AuditGraphUseCase, build_audit_target  # noqa: I001
from book_graph_rag.domain.audit_models import (
    AuditFinding, AuditQueryExecution, AuditSnapshot, AuditTarget, OverallState, QueryState, Severity,  # noqa: E501
)
from book_graph_rag.ports.graph_audit_port import GraphIntegrityAuditPort
class FakePort(GraphIntegrityAuditPort):
    def __init__(self, result: AuditSnapshot | Exception) -> None:
        self.result, self.calls = result, 0
    async def collect_snapshot(self, target: AuditTarget, sample_limit: int) -> AuditSnapshot:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result
def target() -> AuditTarget:
    return build_audit_target("bookgraph-neo4j", "bolt://db:7687", "neo4j")
@pytest.mark.parametrize(("snapshot", "expected"), [
    (AuditSnapshot(queries=(AuditQueryExecution(name="x", state=QueryState.UNREACHABLE),)), OverallState.UNREACHABLE),  # noqa: E501
    (AuditSnapshot(queries=(AuditQueryExecution(name="x", state=QueryState.FAILED),)), OverallState.FAILED),  # noqa: E501
    (AuditSnapshot(provenance_incomplete=True), OverallState.INCOMPLETE),
    (AuditSnapshot(findings=(AuditFinding(rule_id="HIERARCHY_SECTION_PARENT", category="hierarchy", severity=Severity.BLOCKING, total=1),)), OverallState.VIOLATIONS),  # noqa: E501
    (AuditSnapshot(findings=(AuditFinding(rule_id="DUPLICATE_ENTITY_LOGICAL", category="duplicates", severity=Severity.WARNING, total=1),)), OverallState.PASSED),  # noqa: E501
])
async def test_state_precedence(snapshot: AuditSnapshot, expected: OverallState) -> None:
    port = FakePort(snapshot)
    report = await AuditGraphUseCase(port).execute(target(), 2)
    assert report.state == expected
    assert report.execution is not None
    assert report.execution.exit_code == {"passed": 0, "violations": 10, "incomplete": 11, "unreachable": 12, "failed": 13}[expected]  # noqa: E501
    assert port.calls == 1
async def test_invalid_target_is_rejected_before_port() -> None:
    port = FakePort(AuditSnapshot())
    with pytest.raises(ValueError, match="not allowed"):
        await AuditGraphUseCase(port).execute(target().model_copy(update={"selector": "other"}), 2)
    assert port.calls == 0
    with pytest.raises(ValueError, match="userinfo"):
        build_audit_target("bookgraph-neo4j", "bolt://neo4j:secret@db:7687", "neo4j")
    with pytest.raises(ValueError, match="path"):
        build_audit_target("bookgraph-neo4j", "bolt://db:7687/alternate", "neo4j")
@pytest.mark.parametrize(("error", "expected"), [(ConnectionError(), OverallState.UNREACHABLE), (RuntimeError(), OverallState.FAILED)])  # noqa: E501
async def test_port_failure_is_non_clean(error: Exception, expected: OverallState) -> None:
    report = await AuditGraphUseCase(FakePort(error)).execute(target())
    assert report.state == expected
    assert report.inventory == {}
    assert report.execution is not None
    assert report.execution.exit_code != 0
