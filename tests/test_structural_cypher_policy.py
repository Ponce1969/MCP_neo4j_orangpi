"""Adversarial unit tests for the structural dynamic-Cypher policy (T-E.1).

The validator is the security decision: accept only the approved read-only subset
and fail closed on every disallowed construct. Each rejected construct has its
own test, and each accepted query exercises a distinct parse path.
"""

from __future__ import annotations

import pytest

from book_graph_rag.domain.mcp_security import (
    McpSecurityError,
    ScopeProof,
    StructuralPolicyViolationError,
    UnsupportedQueryError,
)
from book_graph_rag.infrastructure.structural_cypher_policy import StructuralCypherPolicy
from book_graph_rag.ports.mcp_security_port import (
    StructuralCypherValidator,
    StructuralValidationResult,
)


def _validate(query: str) -> None:
    StructuralCypherPolicy().validate(query)


def _validate_result(
    query: str, *, require_scope_proof: bool = False
) -> StructuralValidationResult:
    return StructuralCypherPolicy().validate(
        query, require_scope_proof=require_scope_proof
    )


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


# ── T-E.2: bound WHERE, scope proof, and the EXPLAIN gate ──────────────────────


def test_accepts_where_scope_equality() -> None:
    result = _validate_result("MATCH (n:Chunk) WHERE n.book_id = $book_id RETURN n")
    assert result.explain_required is True
    assert result.scope_bound is True
    assert len(result.scope_proofs) == 1
    proof = result.scope_proofs[0]
    assert proof.variable == "n"
    assert proof.label == "Chunk"
    assert proof.property == "book_id"
    assert proof.parameter == "$book_id"
    assert proof.operator == "="


def test_accepts_where_scope_in() -> None:
    result = _validate_result("MATCH (e:Entity) WHERE e.id IN $scope_ids RETURN e")
    assert result.scope_bound is True
    assert len(result.scope_proofs) == 1
    assert result.scope_proofs[0].property == "id"
    assert result.scope_proofs[0].operator == "IN"
    assert result.scope_proofs[0].parameter == "$scope_ids"


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n:Chunk) WHERE n.book_id = $book_id RETURN n",
        "MATCH (n:Chunk) WHERE n.book_id <> $book_id RETURN n",
        "MATCH (n:Chunk) WHERE n.book_id < $book_id RETURN n",
        "MATCH (n:Chunk) WHERE n.book_id <= $book_id RETURN n",
        "MATCH (n:Chunk) WHERE n.book_id > $book_id RETURN n",
        "MATCH (n:Chunk) WHERE n.book_id >= $book_id RETURN n",
        "MATCH (e:Entity) WHERE e.id IN $scope_ids RETURN e",
    ],
)
def test_accepts_multiple_where_comparison_forms(query: str) -> None:
    result = StructuralCypherPolicy().validate(query)
    assert result.scope_bound is True
    assert len(result.scope_proofs) == 1


def test_accepts_scoped_query_when_scope_proof_required() -> None:
    result = _validate_result(
        "MATCH (n:Chunk) WHERE n.book_id = $book_id RETURN n",
        require_scope_proof=True,
    )
    assert result.scope_bound is True


def test_rejects_query_without_scope_predicate_when_required() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="scope"):
        _validate_result(
            "MATCH (n:Chunk) RETURN n",
            require_scope_proof=True,
        )


def test_rejects_where_string_literal() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="literal"):
        _validate("MATCH (n:Chunk) WHERE n.book_id = 'x' RETURN n")


def test_rejects_where_numeric_literal() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="literal"):
        _validate("MATCH (n:Chunk) WHERE n.book_id = 5 RETURN n")


def test_rejects_where_nested_and() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="AND"):
        _validate("MATCH (n:Chunk) WHERE n.book_id = $a AND n.book_id = $b RETURN n")


def test_rejects_where_nested_or() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="OR"):
        _validate("MATCH (n:Chunk) WHERE n.book_id = $a OR n.book_id = $b RETURN n")


def test_rejects_where_parenthesized_expression() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="WHERE"):
        _validate("MATCH (n:Chunk) WHERE (n.book_id = $a) RETURN n")


def test_rejects_where_function_call_rhs() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="function"):
        _validate("MATCH (n:Chunk) WHERE n.book_id = toString($book_id) RETURN n")


def test_rejects_where_function_call_lhs() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="function"):
        _validate("MATCH (n:Chunk) WHERE toLower(n.name) = $x RETURN n")


def test_rejects_where_unknown_property() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="scope key"):
        _validate("MATCH (n:Chunk) WHERE n.name = $name RETURN n")


def test_rejects_where_dynamic_property() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="WHERE"):
        _validate("MATCH (n:Chunk) WHERE n[$key] = $value RETURN n")


def test_rejects_where_missing_param_binding() -> None:
    with pytest.raises(StructuralPolicyViolationError, match="param"):
        _validate("MATCH (n:Chunk) WHERE n.book_id = other_var RETURN n")


def test_require_explain_rejects_when_not_applied() -> None:
    policy = StructuralCypherPolicy()
    with pytest.raises(StructuralPolicyViolationError, match="EXPLAIN"):
        policy.require_explain(
            "MATCH (n:Chunk) WHERE n.book_id = $book_id RETURN n",
            explain_applied=False,
        )


def test_require_explain_accepts_when_applied() -> None:
    policy = StructuralCypherPolicy()
    policy.require_explain(
        "MATCH (n:Chunk) WHERE n.book_id = $book_id RETURN n",
        explain_applied=True,
    )


def test_scope_proofs_shape_for_chunk_book_id_equality() -> None:
    """Chunk.book_id = $book_id yields one ScopeProof with the exact shape (R8)."""
    result = _validate_result("MATCH (c:Chunk) WHERE c.book_id = $book_id RETURN c")
    assert result.scope_proofs == (
        ScopeProof(
            variable="c",
            label="Chunk",
            property="book_id",
            parameter="$book_id",
            operator="=",
        ),
    )


def test_scope_proofs_shape_for_chunk_book_id_in() -> None:
    """Chunk.book_id IN $ids yields one ScopeProof with operator IN (R8)."""
    result = _validate_result("MATCH (c:Chunk) WHERE c.book_id IN $ids RETURN c")
    assert result.scope_proofs == (
        ScopeProof(
            variable="c",
            label="Chunk",
            property="book_id",
            parameter="$ids",
            operator="IN",
        ),
    )


def test_starts_with_remains_outside_operator_allowlist() -> None:
    """STARTS WITH is not in the allowlist, keeping the R7/R8 paths independent."""
    with pytest.raises(StructuralPolicyViolationError, match="operator"):
        _validate("MATCH (e:Entity) WHERE e.id STARTS WITH $scope_prefix RETURN e")
