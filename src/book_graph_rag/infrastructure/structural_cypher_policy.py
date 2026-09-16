"""Structural allowlist validator for dynamic Cypher (T-E.1).

The security decision for the dynamic-query path: a restricted tokenizer plus a
recursive-descent grammar that only recognises the approved read-only subset
(MATCH / OPTIONAL MATCH / WITH / RETURN / ORDER BY / LIMIT, literal labels and
relationship types, bound-parameter property maps, bounded relationship depths,
and a fixed read-function allowlist). Anything it cannot parse or prove fails
closed via ``StructuralPolicyViolationError``; no regex denylist, no third-party
parser.
"""

from __future__ import annotations

from dataclasses import dataclass

from book_graph_rag.domain.mcp_security import (
    SCOPE_KEYS_BY_LABEL,
    ScopeProof,
    StructuralPolicyViolationError,
)
from book_graph_rag.ports.mcp_security_port import (
    StructuralCypherValidator,
    StructuralValidationResult,
)

_APPROVED_FUNCTIONS = frozenset(
    {
        "COUNT", "COLLECT", "SUM", "AVG", "MIN", "MAX", "SIZE", "HEAD", "LAST",
        "LABELS", "TYPE", "KEYS", "PROPERTIES", "TOSTRING", "COALESCE", "ID",
        "ELEMENTID", "NODES", "RELATIONSHIPS",
    }
)

_WRITE_CLAUSES = frozenset({"CREATE", "SET", "DELETE", "MERGE", "DETACH", "REMOVE", "DROP"})
_PUNCTUATION = frozenset("(){}[],.:<>-*=+|")


@dataclass(frozen=True)
class _Token:
    """A single lexical token with kind, raw value, and source position."""

    kind: str
    value: str
    pos: int


def _tokenize(query: str) -> list[_Token]:
    """Tokenize ``query``; any unrecognized character fails closed."""
    tokens: list[_Token] = []
    i = 0
    length = len(query)
    while i < length:
        char = query[i]
        if char.isspace():
            i += 1
            continue
        if char in ("'", '"'):
            quote = char
            j = i + 1
            while j < length and query[j] != quote:
                j += 2 if query[j] == "\\" else 1
            if j >= length:
                raise StructuralPolicyViolationError("unterminated string literal")
            tokens.append(_Token("STRING", query[i : j + 1], i))
            i = j + 1
            continue
        if char == "$":
            j = i + 1
            if j < length and (query[j].isalpha() or query[j] == "_"):
                j += 1
                while j < length and (query[j].isalnum() or query[j] == "_"):
                    j += 1
            else:
                raise StructuralPolicyViolationError(f"malformed parameter at position {i}")
            tokens.append(_Token("PARAM", query[i:j], i))
            i = j
            continue
        if char.isdigit():
            j = i
            while j < length and query[j].isdigit():
                j += 1
            tokens.append(_Token("NUMBER", query[i:j], i))
            i = j
            continue
        if char.isalpha() or char == "_":
            j = i
            while j < length and (query[j].isalnum() or query[j] == "_"):
                j += 1
            tokens.append(_Token("IDENT", query[i:j], i))
            i = j
            continue
        if query.startswith("..", i):
            tokens.append(_Token("RANGE", "..", i))
            i += 2
            continue
        if char in _PUNCTUATION:
            tokens.append(_Token("PUNCT", char, i))
            i += 1
            continue
        raise StructuralPolicyViolationError(f"unsupported character {char!r} at position {i}")
    tokens.append(_Token("EOF", "", length))
    return tokens


def _validate_clause_order(clauses: list[str]) -> None:
    """Enforce read clauses first and a single trailing RETURN."""
    if not clauses:
        raise StructuralPolicyViolationError("empty query")
    if clauses[0] not in ("MATCH", "OPTIONAL MATCH"):
        raise StructuralPolicyViolationError("query must start with MATCH or OPTIONAL MATCH")
    if clauses.count("RETURN") != 1:
        raise StructuralPolicyViolationError("query must contain exactly one RETURN clause")
    return_idx = clauses.index("RETURN")
    if "WITH" in clauses and clauses.index("WITH") > return_idx:
        raise StructuralPolicyViolationError("WITH must precede RETURN")
    if "ORDER BY" in clauses and clauses.index("ORDER BY") < return_idx:
        raise StructuralPolicyViolationError("ORDER BY must follow RETURN")
    if "LIMIT" in clauses and clauses.index("LIMIT") < return_idx:
        raise StructuralPolicyViolationError("LIMIT must follow RETURN")
    if (
        "ORDER BY" in clauses
        and "LIMIT" in clauses
        and clauses.index("LIMIT") < clauses.index("ORDER BY")
    ):
        raise StructuralPolicyViolationError("LIMIT must follow ORDER BY")
    if "WHERE" in clauses:
        where_idx = clauses.index("WHERE")
        if where_idx == 0:
            raise StructuralPolicyViolationError("WHERE must follow MATCH")
        for clause in clauses[:where_idx]:
            if clause not in ("MATCH", "OPTIONAL MATCH"):
                raise StructuralPolicyViolationError(
                    "WHERE must directly follow MATCH or OPTIONAL MATCH"
                )
        if return_idx < where_idx:
            raise StructuralPolicyViolationError("WHERE must precede RETURN")


class _Parser:
    """Recursive-descent walker over the token stream (the allowlist grammar)."""

    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self._pos = 0
        self._node_vars: dict[str, set[str]] = {}
        self._where_predicates: list[tuple[str, str, str, str, str]] = []

    def peek(self) -> _Token:
        return self._tokens[self._pos]

    def advance(self) -> _Token:
        token = self._tokens[self._pos]
        self._pos += 1
        return token

    def _accept_punct(self, value: str) -> bool:
        if self.peek().kind == "PUNCT" and self.peek().value == value:
            self._pos += 1
            return True
        return False

    def _expect_punct(self, value: str) -> None:
        token = self.peek()
        if token.kind != "PUNCT" or token.value != value:
            raise StructuralPolicyViolationError(f"expected {value!r} at position {token.pos}")
        self._pos += 1

    def _accept_ident(self, value: str) -> bool:
        if self.peek().kind == "IDENT" and self.peek().value.upper() == value:
            self._pos += 1
            return True
        return False

    def _expect_ident(self, value: str) -> None:
        token = self.peek()
        if token.kind != "IDENT" or token.value.upper() != value:
            raise StructuralPolicyViolationError(f"expected {value} at position {token.pos}")
        self._pos += 1

    def parse(self) -> list[tuple[str, str, str, str, str]]:
        clauses: list[str] = []
        while self.peek().kind != "EOF":
            clauses.append(self._parse_clause())
        _validate_clause_order(clauses)
        return self._where_predicates

    def _parse_clause(self) -> str:
        token = self.peek()
        if token.kind != "IDENT":
            raise StructuralPolicyViolationError(f"expected a clause at position {token.pos}")
        keyword = token.value.upper()
        if keyword == "MATCH":
            self.advance()
            self._parse_pattern_list()
            return "MATCH"
        if keyword == "OPTIONAL":
            self.advance()
            self._expect_ident("MATCH")
            self._parse_pattern_list()
            return "OPTIONAL MATCH"
        if keyword == "WITH":
            self.advance()
            self._parse_projection()
            return "WITH"
        if keyword == "RETURN":
            self.advance()
            self._parse_projection()
            return "RETURN"
        if keyword == "ORDER":
            self.advance()
            self._expect_ident("BY")
            self._parse_order_items()
            return "ORDER BY"
        if keyword == "LIMIT":
            self.advance()
            self._parse_limit()
            return "LIMIT"
        if keyword in _WRITE_CLAUSES:
            raise StructuralPolicyViolationError(f"disallowed write clause {keyword!r}")
        if keyword == "CALL":
            raise StructuralPolicyViolationError(
                "CALL is not allowed (procedures and subqueries are rejected)"
            )
        if keyword == "LOAD":
            raise StructuralPolicyViolationError("LOAD CSV is not allowed")
        if keyword == "FOREACH":
            raise StructuralPolicyViolationError("FOREACH is not allowed")
        if keyword == "WHERE":
            self.advance()
            self._parse_where()
            return "WHERE"
        if keyword == "UNWIND":
            raise StructuralPolicyViolationError("UNWIND clause is outside the approved subset")
        raise StructuralPolicyViolationError(f"unsupported clause {keyword!r}")

    def _parse_where(self) -> None:
        var_token = self.peek()
        if var_token.kind != "IDENT":
            raise StructuralPolicyViolationError(
                f"WHERE must compare a node property at position {var_token.pos}"
            )
        lookahead = self._tokens[self._pos + 1]
        if lookahead.kind == "PUNCT" and lookahead.value == "(":
            raise StructuralPolicyViolationError("function calls are not allowed in WHERE")
        variable = var_token.value
        labels = self._node_vars.get(variable)
        if labels is None:
            raise StructuralPolicyViolationError(
                f"WHERE references unknown variable {variable!r} at position {var_token.pos}"
            )
        self.advance()
        if not self._accept_punct("."):
            raise StructuralPolicyViolationError(
                f"WHERE must reference a property of {variable!r} (dynamic access is not allowed)"
            )
        prop_token = self.peek()
        if prop_token.kind != "IDENT":
            raise StructuralPolicyViolationError(
                f"WHERE property access must be a static identifier at position {prop_token.pos}"
            )
        prop = prop_token.value
        self.advance()
        if not labels:
            raise StructuralPolicyViolationError(
                f"WHERE variable {variable!r} has no label; cannot prove a scope key"
            )
        allowed_labels = sorted(
            label for label in labels
            if prop in SCOPE_KEYS_BY_LABEL.get(label, frozenset())
        )
        if not allowed_labels:
            raise StructuralPolicyViolationError(
                f"WHERE property {prop!r} is not an allowed scope key for {variable!r}"
            )
        label = allowed_labels[0]
        operator = self._parse_where_operator()
        rhs = self.peek()
        if rhs.kind in ("STRING", "NUMBER"):
            raise StructuralPolicyViolationError(
                "WHERE literal values are not allowed (bind a $param)"
            )
        if rhs.kind == "IDENT":
            lookahead = self._tokens[self._pos + 1]
            if lookahead.kind == "PUNCT" and lookahead.value == "(":
                raise StructuralPolicyViolationError("function calls are not allowed in WHERE")
            raise StructuralPolicyViolationError(
                f"WHERE comparison must bind a $param at position {rhs.pos}"
            )
        if rhs.kind != "PARAM":
            raise StructuralPolicyViolationError(
                f"WHERE comparison must bind a $param at position {rhs.pos}"
            )
        self.advance()
        self._where_predicates.append((variable, label, prop, operator, rhs.value))
        lookahead = self.peek()
        if lookahead.kind == "IDENT" and lookahead.value.upper() in ("AND", "OR"):
            raise StructuralPolicyViolationError(
                f"nested boolean expressions ({lookahead.value.upper()}) are not allowed in WHERE"
            )

    def _parse_where_operator(self) -> str:
        token = self.peek()
        if token.kind == "IDENT" and token.value.upper() == "IN":
            self.advance()
            return "IN"
        if token.kind != "PUNCT":
            raise StructuralPolicyViolationError(
                f"unsupported comparison operator at position {token.pos}"
            )
        if token.value == "=":
            self.advance()
            return "="
        if token.value == "<":
            self.advance()
            nxt = self.peek()
            if nxt.kind == "PUNCT" and nxt.value in ("=", ">"):
                self.advance()
                return f"<{nxt.value}"
            return "<"
        if token.value == ">":
            self.advance()
            nxt = self.peek()
            if nxt.kind == "PUNCT" and nxt.value == "=":
                self.advance()
                return ">="
            return ">"
        raise StructuralPolicyViolationError(
            f"unsupported comparison operator at position {token.pos}"
        )

    def _parse_pattern_list(self) -> None:
        self._parse_path_pattern()
        while self._accept_punct(","):
            self._parse_path_pattern()

    def _parse_path_pattern(self) -> None:
        self._parse_node_pattern()
        while self.peek().kind == "PUNCT" and self.peek().value in ("-", "<"):
            self._parse_relationship_pattern()
            self._parse_node_pattern()

    def _parse_node_pattern(self) -> None:
        self._expect_punct("(")
        variable: str | None = None
        if self.peek().kind == "IDENT":
            variable = self.advance().value
        labels: list[str] = []
        while self._accept_punct(":"):
            labels.append(self._parse_label("label"))
        if self._accept_punct("{"):
            self._parse_property_map()
        self._expect_punct(")")
        if variable is not None:
            self._node_vars.setdefault(variable, set()).update(labels)

    def _parse_label(self, context: str) -> str:
        token = self.peek()
        if token.kind == "PARAM":
            raise StructuralPolicyViolationError(f"dynamic {context} is not allowed")
        if token.kind != "IDENT":
            raise StructuralPolicyViolationError(f"expected a {context} at position {token.pos}")
        self.advance()
        return token.value

    def _parse_relationship_pattern(self) -> None:
        left = self._accept_punct("<")
        self._expect_punct("-")
        if self._accept_punct("["):
            self._parse_relationship_detail()
            self._expect_punct("]")
        self._expect_punct("-")
        right = self._accept_punct(">")
        if left and right:
            raise StructuralPolicyViolationError("invalid relationship direction")

    def _parse_relationship_detail(self) -> None:
        if self.peek().kind == "IDENT":
            self.advance()
        while self._accept_punct(":"):
            self._parse_label("relationship type")
        if self._accept_punct("*"):
            self._parse_depth()
        if self._accept_punct("{"):
            self._parse_property_map()

    def _parse_depth(self) -> None:
        # Only explicit-upper-bound forms are allowed: *n, *n..m, *..m.
        if self.peek().kind == "NUMBER":
            self._consume_int()
            if self.peek().kind != "RANGE":
                return
            self.advance()
        elif self.peek().kind == "RANGE":
            self.advance()
        else:
            raise StructuralPolicyViolationError("unbounded relationship paths are not allowed")
        if self.peek().kind != "NUMBER":
            raise StructuralPolicyViolationError("unbounded relationship paths are not allowed")
        self._consume_int()

    def _consume_int(self) -> int:
        token = self.advance()
        if token.kind != "NUMBER":
            raise StructuralPolicyViolationError(f"expected an integer at position {token.pos}")
        return int(token.value)

    def _parse_property_map(self) -> None:
        if self._accept_punct("}"):
            return
        while True:
            self._parse_property_entry()
            if not self._accept_punct(","):
                break
        self._expect_punct("}")

    def _parse_property_entry(self) -> None:
        key = self.peek()
        if key.kind != "IDENT":
            raise StructuralPolicyViolationError(f"expected property key at position {key.pos}")
        self.advance()
        self._expect_punct(":")
        if self.peek().kind != "PARAM":
            raise StructuralPolicyViolationError(
                "literal values are not allowed in property maps (use a parameter)"
            )
        self.advance()

    def _parse_projection(self) -> None:
        self._accept_ident("DISTINCT")
        self._parse_projection_item()
        while self._accept_punct(","):
            self._parse_projection_item()

    def _parse_projection_item(self) -> None:
        if self._accept_punct("*"):
            return
        self._parse_expression()
        if self._accept_ident("AS"):
            token = self.peek()
            if token.kind != "IDENT":
                raise StructuralPolicyViolationError(
                    f"expected an alias at position {token.pos}"
                )
            self.advance()

    def _parse_expression(self) -> None:
        token = self.peek()
        if token.kind == "PARAM":
            self.advance()
            return
        if token.kind == "IDENT":
            name = token.value
            self.advance()
            if name.upper() == "EXISTS":
                raise StructuralPolicyViolationError("EXISTS is not allowed")
            if self._accept_punct("("):
                if name.upper() not in _APPROVED_FUNCTIONS:
                    raise StructuralPolicyViolationError(f"unsupported function {name!r}")
                self._parse_function_args()
                return
            if self._accept_punct("."):
                prop = self.peek()
                if prop.kind != "IDENT":
                    raise StructuralPolicyViolationError(
                        f"expected a property name at position {prop.pos}"
                    )
                self.advance()
                if self._accept_punct("."):
                    raise StructuralPolicyViolationError("nested property access is not allowed")
            return
        if token.kind in ("STRING", "NUMBER"):
            raise StructuralPolicyViolationError(
                "literal values are not allowed in expressions (use a parameter)"
            )
        raise StructuralPolicyViolationError(f"unsupported expression at position {token.pos}")

    def _parse_function_args(self) -> None:
        self._accept_ident("DISTINCT")
        if self._accept_punct("*"):
            self._expect_punct(")")
            return
        if self._accept_punct(")"):
            raise StructuralPolicyViolationError("empty function argument list is not allowed")
        self._parse_expression()
        while self._accept_punct(","):
            self._parse_expression()
        self._expect_punct(")")

    def _parse_order_items(self) -> None:
        while True:
            self._parse_expression()
            if not self._accept_ident("ASC"):
                self._accept_ident("DESC")
            if not self._accept_punct(","):
                break

    def _parse_limit(self) -> None:
        token = self.peek()
        if token.kind == "NUMBER":
            self._consume_int()
            return
        if token.kind == "PARAM":
            self.advance()
            return
        raise StructuralPolicyViolationError(
            f"expected an integer or parameter LIMIT at position {token.pos}"
        )


class StructuralCypherPolicy(StructuralCypherValidator):
    """Fail-closed structural validator for the approved read-only Cypher subset."""

    def validate(
        self,
        query: str,
        *,
        require_scope_proof: bool = False,
    ) -> StructuralValidationResult:
        if not isinstance(query, str):
            raise StructuralPolicyViolationError("query must be a string")
        where_predicates = _Parser(_tokenize(query)).parse()
        scope_proofs = tuple(
            ScopeProof(
                variable=variable,
                label=label,
                property=prop,
                parameter=parameter,
                operator=op,
            )
            for (variable, label, prop, op, parameter) in where_predicates
        )
        if require_scope_proof and not scope_proofs:
            raise StructuralPolicyViolationError(
                "query has no scope predicate (a WHERE binding a node property to "
                "a $param is required)"
            )
        return StructuralValidationResult(
            explain_required=True,
            scope_bound=bool(scope_proofs),
            scope_proofs=scope_proofs,
        )

    def require_explain(self, query: str, *, explain_applied: bool) -> None:
        if not isinstance(query, str):
            raise StructuralPolicyViolationError("query must be a string")
        if not explain_applied:
            raise StructuralPolicyViolationError(
                "EXPLAIN is required before execution (the query must be planned, not run, first)"
            )
