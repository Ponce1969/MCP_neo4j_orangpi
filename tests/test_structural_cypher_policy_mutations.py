"""Mutation matrix over the accepted structural Cypher grammar (T-E.3 part 1).

The structural validator is the security decision for the dynamic-query path, so
its acceptance is only meaningful if adversarial mutations of accepted queries
fail closed. This module is a data-driven mutation matrix: it takes a set of
canonical accepted queries covering every parse path, applies deterministic
syntactic mutations that try to smuggle in a disallowed construct, and asserts
each mutation is rejected with the expected reason. It also proves that every
scoped accepted query fails closed once its scope-proof WHERE clause is stripped
under ``require_scope_proof=True``.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from book_graph_rag.domain.mcp_security import StructuralPolicyViolationError
from book_graph_rag.infrastructure.structural_cypher_policy import StructuralCypherPolicy

_POLICY = StructuralCypherPolicy()

# ── Canonical accepted queries (one per parse path) ───────────────────────────
#
# Each query below is accepted by ``validate()``; the scoped ones (with a WHERE
# scope predicate) are additionally accepted by ``validate(require_scope_proof=True)``.
_ACCEPTED_QUERIES: tuple[str, ...] = (
    "MATCH (n) RETURN n",                                       # bare node
    "MATCH (n:Entity) RETURN n LIMIT 10",                       # label + LIMIT
    "OPTIONAL MATCH (n)-[r:RELATED]->(m) RETURN n, m",          # OPTIONAL MATCH
    "MATCH (a:Entity)-[:RELATED]->(b:Entity) "
    "RETURN a, b ORDER BY a.name LIMIT 20",                     # rel + ORDER BY + LIMIT
    "MATCH (n) WITH n.name AS name RETURN name",                # WITH projection
    "MATCH (n:Entity {id: $id}) RETURN n",                      # bound property map
    "MATCH (n:Entity:Pattern) RETURN n",                        # multiple labels
    "MATCH (a:Entity)-[:RELATED*2]->(b) RETURN b",              # fixed depth
    "MATCH (a:Entity)-[:RELATED*1..3]->(b:Entity) RETURN b",    # bounded depth
    "MATCH (a:Entity)-[:RELATED*..3]->(b:Entity) RETURN b",     # upper-bound only
    "MATCH (n:Entity) RETURN count(n) AS total",                # aggregate function
    "MATCH (n) RETURN *",                                       # star projection
    "MATCH (c:Chunk) WHERE c.book_id = $book_id RETURN c",      # WHERE scope =
    "MATCH (e:Entity) WHERE e.id IN $scope_ids RETURN e",       # WHERE scope IN
)

# Scoped accepted queries used to prove fail-closed scope stripping.
_SCOPED_QUERIES: tuple[str, ...] = (
    "MATCH (c:Chunk) WHERE c.book_id = $book_id RETURN c LIMIT $limit",
    "MATCH (e:Entity) WHERE e.id IN $scope_ids RETURN e",
    "MATCH (c:Chunk) WHERE c.book_id <> $book_id RETURN c",
)

# ── Deterministic syntactic mutations ────────────────────────────────────────
#
# Each mutation takes an accepted query and returns a mutated query that tries to
# smuggle in one disallowed construct. Mutations use string surgery on the query
# so the matrix stays deterministic and auditable.

_SCOPED_CHUNK = "MATCH (c:Chunk) WHERE c.book_id = $book_id RETURN c LIMIT $limit"
_PROP_MAP = "MATCH (n:Entity {id: $id}) RETURN n"
_DEPTH = "MATCH (a:Entity)-[:RELATED*2]->(b:Entity) RETURN b"


def _smuggle_create(query: str) -> str:
    """Inject a CREATE clause before RETURN."""
    return query.replace("RETURN", "CREATE (x:Entity) RETURN", 1)


def _smuggle_merge(query: str) -> str:
    """Inject a MERGE clause before RETURN."""
    return query.replace("RETURN", "MERGE (x:Entity {id: $id}) RETURN", 1)


def _smuggle_call(query: str) -> str:
    """Prepend a procedure CALL."""
    return "CALL db.labels() YIELD label " + query


def _smuggle_load_csv(query: str) -> str:
    """Prepend a LOAD CSV clause."""
    return "LOAD CSV FROM 'file:///x.csv' AS row " + query


def _smuggle_foreach(query: str) -> str:
    """Prepend a FOREACH clause with a nested write."""
    return "FOREACH (x IN $items | CREATE (n)) " + query


def _smuggle_dynamic_label(query: str) -> str:
    """Turn the static label into a dynamic ``$param`` label."""
    return query.replace(":Entity", ":$label", 1)


def _smuggle_literal_property_map(query: str) -> str:
    """Replace the bound parameter with a string literal in a property map."""
    return query.replace("$id", "'alice'", 1)


def _smuggle_literal_where(query: str) -> str:
    """Replace the bound parameter with a string literal in WHERE."""
    return query.replace("$book_id", "'x'", 1)


def _smuggle_nested_and(query: str) -> str:
    """Append a second predicate joined with AND."""
    return query.replace("RETURN", "AND c.book_id = $other RETURN", 1)


def _smuggle_nested_or(query: str) -> str:
    """Append a second predicate joined with OR."""
    return query.replace("RETURN", "OR c.book_id = $other RETURN", 1)


def _smuggle_where_function(query: str) -> str:
    """Wrap the bound parameter in a function call on the WHERE right-hand side."""
    return query.replace("$book_id", "toString($book_id)", 1)


def _smuggle_unbounded_path(query: str) -> str:
    """Replace a bounded relationship with an unbounded ``[*]`` relationship."""
    return query.replace("[:RELATED*2]", "[*]", 1)


def _smuggle_bare_star_depth(query: str) -> str:
    """Strip the depth number leaving a bare ``*`` relationship depth."""
    return query.replace("[:RELATED*2]", "[:RELATED*]", 1)


def _smuggle_unknown_where_variable(query: str) -> str:
    """Reference an undeclared variable inside WHERE."""
    return query.replace("c.book_id", "x.book_id", 1)


# ── Data-driven mutation matrix ───────────────────────────────────────────────

Mutation = Callable[[str], str]

# (label, accepted_query, mutation, expected_reject_reason_fragment)
_MUTATION_MATRIX: tuple[tuple[str, str, Mutation, str], ...] = (
    ("write-keyword-create", _SCOPED_CHUNK, _smuggle_create, "CREATE"),
    ("write-keyword-merge", _SCOPED_CHUNK, _smuggle_merge, "MERGE"),
    ("procedure-call", _SCOPED_CHUNK, _smuggle_call, "CALL"),
    ("load-csv", _SCOPED_CHUNK, _smuggle_load_csv, "LOAD CSV"),
    ("foreach", _SCOPED_CHUNK, _smuggle_foreach, "FOREACH"),
    ("dynamic-label", _PROP_MAP, _smuggle_dynamic_label, "dynamic label"),
    ("literal-property-map", _PROP_MAP, _smuggle_literal_property_map, "literal"),
    ("literal-where", _SCOPED_CHUNK, _smuggle_literal_where, "literal"),
    ("nested-and-where", _SCOPED_CHUNK, _smuggle_nested_and, "AND"),
    ("nested-or-where", _SCOPED_CHUNK, _smuggle_nested_or, "OR"),
    ("function-call-where", _SCOPED_CHUNK, _smuggle_where_function, "function"),
    ("unbounded-path", _DEPTH, _smuggle_unbounded_path, "unbounded"),
    ("bare-star-depth", _DEPTH, _smuggle_bare_star_depth, "unbounded"),
    ("unknown-where-variable", _SCOPED_CHUNK, _smuggle_unknown_where_variable, "unknown variable"),
)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_accepted_queries_cover_every_parse_path() -> None:
    """Every canonical accepted query passes the plain structural validation."""
    for query in _ACCEPTED_QUERIES:
        result = _POLICY.validate(query)
        assert result.explain_required is True


def test_scoped_queries_pass_when_scope_proof_required() -> None:
    """The scoped accepted queries all satisfy ``require_scope_proof=True``."""
    for query in _SCOPED_QUERIES:
        result = _POLICY.validate(query, require_scope_proof=True)
        assert result.scope_bound is True
        assert len(result.scope_proofs) == 1


@pytest.mark.parametrize(
    ("label", "accepted_query", "mutation", "fragment"),
    _MUTATION_MATRIX,
    ids=[case[0] for case in _MUTATION_MATRIX],
)
def test_mutation_is_rejected(
    label: str,
    accepted_query: str,
    mutation: Mutation,
    fragment: str,
) -> None:
    """Every syntactic mutation of an accepted query must be rejected."""
    # Sanity: the unmutated query really is accepted before we mutate it.
    _POLICY.validate(accepted_query)

    mutated = mutation(accepted_query)
    assert mutated != accepted_query, f"mutation {label!r} did not change the query"

    with pytest.raises(StructuralPolicyViolationError, match=re.escape(fragment)):
        _POLICY.validate(mutated)


_WHERE_STRIP_RE = re.compile(
    r"\s+WHERE\s+.+?(?=\s+(?:RETURN|WITH|LIMIT|ORDER)\b)", re.IGNORECASE
)


def _strip_scope(query: str) -> str:
    """Remove the WHERE scope predicate clause from a query."""
    return _WHERE_STRIP_RE.sub("", query, count=1)


@pytest.mark.parametrize("scoped_query", _SCOPED_QUERIES)
def test_scoped_query_fails_closed_without_scope_proof(scoped_query: str) -> None:
    """Stripping the scope-proof WHERE fails closed under require_scope_proof."""
    stripped = _strip_scope(scoped_query)
    assert stripped != scoped_query
    assert "WHERE" not in stripped

    with pytest.raises(StructuralPolicyViolationError, match="scope"):
        _POLICY.validate(stripped, require_scope_proof=True)
