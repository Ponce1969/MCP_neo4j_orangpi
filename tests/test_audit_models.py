from datetime import UTC, datetime  # noqa: I001
import pytest  # noqa: I001
from pydantic import ValidationError
from book_graph_rag.domain.audit_models import (
    RULE_CATALOG, AuditFinding, AuditReport, AuditTarget, FindingSample,
    InventoryMetric, OverallState, QueryState, Severity, duplicate_group_id, normalize_key,
)
def target() -> AuditTarget:
    return AuditTarget(selector="bookgraph-neo4j", database="neo4j", scheme="bolt", host="db", uri="bolt://db")
def test_target_is_frozen_safe_and_rejects_userinfo() -> None:
    value = target()
    assert "uri" not in value.model_dump()
    with pytest.raises((ValidationError, TypeError)):
        value.database = "other"
    with pytest.raises(ValueError, match="userinfo"):
        AuditTarget(selector="bookgraph-neo4j", database="neo4j", scheme="bolt", host="db", uri="bolt://neo4j:secret@db")
def test_normalization_and_namespaced_ids_are_stable() -> None:
    assert normalize_key("  Cafe\u0301\u00a0 NAME ") == "café name"
    assert normalize_key(None) == "<empty>"
    assert duplicate_group_id("entity", " A  ") == duplicate_group_id("entity", "a")
    assert duplicate_group_id("entity", "a") != duplicate_group_id("relationship", "a")
def test_closed_states_distinguish_evaluated_zero() -> None:
    assert InventoryMetric(value=0, state="evaluated").value == 0
    with pytest.raises(ValueError, match="evaluated"):
        InventoryMetric(value=0, state="unsupported")
    assert tuple(sorted(RULE_CATALOG)) == RULE_CATALOG
    assert QueryState.NOT_APPLICABLE.value == "not_applicable"
    with pytest.raises(ValueError, match="clean"):
        OverallState("clean")
def test_samples_are_sorted_bounded_and_secret_safe() -> None:
    finding = AuditFinding(  # noqa: E501
        rule_id="DUPLICATE_ENTITY_LOGICAL", category="duplicates", severity=Severity.WARNING, total=2,  # noqa: E501
        sample_limit=1, samples=(FindingSample(key="z", properties={"password": "secret", "text": "body"}), FindingSample(key="a")),  # noqa: E501
    )
    report = AuditReport(target=target(), state=OverallState.PASSED, findings=(finding,), executed_at=datetime(2024, 1, 1, tzinfo=UTC))  # noqa: E501
    payload = report.canonical_json()
    assert "secret" not in payload
    assert '"text"' not in payload
    assert report.findings[0].samples[0].key == "a"
    assert report.findings[0].sample_truncated
    assert report.summary.warning_total == 2
def test_nested_properties_are_recursively_secret_safe() -> None:
    sample = FindingSample(key="nested", properties={"details": {"password": "nested-password", "items": [{"token": "nested-token", "label": "safe"}, {"endpoint": "bolt://user:password@db:7687"}]}})  # noqa: E501
    assert all(secret not in sample.model_dump_json() for secret in ("nested-password", "nested-token", "bolt://user:password@db:7687"))  # noqa: E501

def test_nested_properties_are_bounded_at_each_level() -> None:
    properties = {"levels": [{f"entry-{index}": {"value": index} for index in range(1001)} for _ in range(1001)]}  # noqa: E501
    levels = FindingSample(key="large", properties=properties).properties["levels"]
    assert isinstance(levels, tuple)
    assert len(levels) <= 20
    assert all(isinstance(level, dict) and len(level) <= 20 for level in levels)
