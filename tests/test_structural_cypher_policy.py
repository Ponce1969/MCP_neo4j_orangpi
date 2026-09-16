"""Adversarial unit tests for the structural dynamic-Cypher policy (T-E.1).

The validator is the security decision: accept only the approved read-only subset
and fail closed on every disallowed construct. Each rejected construct has its
own test, and each accepted query exercises a distinct parse path.
"""

from __future__ import annotations

import pytest

from book_graph_rag.domain.mcp_security import (
    McpSecurityError,
    StructuralPolicyViolationError,
    UnsupportedQueryError,
)
from book_graph_rag.infrastructure.structural_cypher_policy import StructuralCypherPolicy
from book_graph_rag.ports.mcp_security_port import StructuralCypherValidator


def _validate(query: str) -> None:
    StructuralCypherPolicy().validate(query)


def test_structural_policy_error_is_unsupported_query_subtype() -> None:
    assert issubclass(StructuralPolicyViolationError, UnsupportedQueryError)
    assert issubclass(StructuralPolicyViolationError, McpSecurityError)
    assert StructuralPolicyViolationError("x").error_code == "structural_policy_violation"


def test_policy_implements_structural_cypher_validator_port() -> None:
    assert isinstance(StructuralCypherPolicy(), StructuralCypherValidator)


def test_accepts_basic_match_return() -> None:
    _validate("MATCH (n) RETURN n")


def test_accepts_labeled_node_with_limit() -> None:
    _validate("MATCH (n:Entity) RETURN n LIMIT 10")


def test_accepts_optional_match() -> None:
    _validate("OPTIONAL MATCH (n)-[r:RELATED]->(m) RETURN n, m")


def test_accepts_relationship_and_order_by() -> None:
    _validate("MATCH (a:Entity)-[:RELATED]->(b:Entity) RETURN a, b ORDER BY a.name LIMIT 20")


def test_accepts_with_clause() -> None:
    _validate("MATCH (n) WITH n.name AS name RETURN name")


def test_accepts_bound_parameters() -> None:
    _validate("MATCH (n:Entity {id: $id}) RETURN n")


def test_accepts_multiple_labels() -> None:
    _validate("MATCH (n:Entity:Pattern) RETURN n")


def test_accepts_fixed_depth_relationship() -> None:
    _validate("MATCH (a:Entity)-[:RELATED*2]->(b) RETURN b")


def test_accepts_bounded_variable_length_relationship() -> None:
    _validate("MATCH (a:Entity)-[:RELATED*1..3]->(b:Entity) RETURN b")


def test_accepts_upper_bound_only_relationship() -> None:
    _validate("MATCH (a:Entity)-[:RELATED*..3]->(b:Entity) RETURN b")


def test_accepts_approved_aggregate_function() -> None:
    _validate("MATCH (n:Entity) RETURN count(n) AS total")


def test_accepts_return_star() -> None:
    _validate("MATCH (n) RETURN *")


@pytest.mark.parametrize(
    ("query", "fragment"),
    [
        ("MATCH (n) CREATE (m:Entity) RETURN m", "CREATE"),
        ("MATCH (n) SET n.name = 'x' RETURN n", "SET"),
        ("MATCH (n) DELETE n", "DELETE"),
        ("MERGE (n:Entity {id: $id}) RETURN n", "MERGE"),
        ("MATCH (n) DETACH DELETE n", "DETACH"),
    ],
)
def test_rejects_write_clauses(query: str, fragment: str) -> None:
    with pytest.raises(StructuralPolicyViolationError, match=fragment):
        _validate(query)


@pytest.mark.parametrize(
    "query",
    ["CALL db.labels()", "CALL apoc.meta.data()", "CALL my.custom.procedure()"],
)
def test_rejects_procedure_calls(query: str) -> None:
    with pytest.raises(StructuralPolicyViolationError, match="CALL"):
        _validate(query)


def test_rejects_call_subquery() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="CALL"):
        _validate("CALL { MATCH (n) RETURN n } RETURN n")


def test_rejects_exists_subquery() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="EXISTS"):
        _validate("MATCH (n) RETURN EXISTS { MATCH (n)--(m) }")


def test_rejects_load_csv() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="LOAD CSV"):
        _validate("LOAD CSV FROM 'file:///x.csv' AS row RETURN row")


def test_rejects_foreach() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="FOREACH"):
        _validate("FOREACH (x IN $items | CREATE (n))")


def test_rejects_where_clause() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="WHERE"):
        _validate("MATCH (n) WHERE n.name = 'x' RETURN n")


def test_rejects_unwind_clause() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="UNWIND"):
        _validate("MATCH (n) UNWIND n AS x RETURN x")


def test_rejects_unbounded_bare_star() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="unbounded"):
        _validate("MATCH (a)-[:RELATED*]->(b) RETURN b")


def test_rejects_unbounded_open_upper_bound() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="unbounded"):
        _validate("MATCH (a)-[:RELATED*1..]->(b) RETURN b")


def test_rejects_dynamic_label() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="dynamic label"):
        _validate("MATCH (n:$label) RETURN n")


def test_rejects_dynamic_relationship_type() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="dynamic relationship type"):
        _validate("MATCH (a)-[r:$type]->(b) RETURN b")


def test_rejects_string_literal_in_property_map() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="literal"):
        _validate("MATCH (n:Entity {name: 'alice'}) RETURN n")


def test_rejects_numeric_literal_in_property_map() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="literal"):
        _validate("MATCH (n:Entity {count: 5}) RETURN n")


def test_rejects_empty_query() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="empty"):
        _validate("")


def test_rejects_unterminated_string_literal() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="unterminated"):
        _validate("MATCH (n) RETURN 'unterminated")


def test_rejects_unknown_function() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="unsupported function"):
        _validate("MATCH (n) RETURN myfunc(n)")


def test_rejects_backtick_escaped_identifier() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="unsupported character"):
        _validate("MATCH (n:`Dynamic`) RETURN n")


def test_rejects_unsupported_clause_keyword() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="unsupported clause"):
        _validate("MATCH (n) YIELD n RETURN n")
