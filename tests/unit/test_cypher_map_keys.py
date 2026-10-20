"""Guard: Cypher map keys must be identifiers or backtick-escaped.

The production error was a parse error, not a wrong value:

    CypherSyntaxError: Invalid input ''fulltext.analyzer'': expected an
    identifier or '}'

Cypher map literals accept bare identifiers (``indexConfig``) or backtick
escaped keys (```vector.dimensions````); a dotted key quoted with single quotes
is a syntax error. The repository already had the right pattern in
``_neo4j_vector_cypher._ENSURE_VECTOR_INDEX`` and the wrong one in the chunk
fulltext DDL, and no test could tell them apart: the pins compared the DDL with
itself, so a syntactically invalid statement stayed byte-identical to its own
copy and every gate went green.

This is the general property, applied to every statement the repository can emit
with an ``OPTIONS``/``indexConfig`` map. It is deliberately not a pin on one
string: the next DDL someone adds inherits the check.
"""

from __future__ import annotations

import re

import pytest

from book_graph_rag.infrastructure._neo4j_vector_cypher import _ENSURE_VECTOR_INDEX
from book_graph_rag.infrastructure.neo4j_query_adapter import chunk_text_index_ddl

# A single-quoted string used as a map key, e.g. ``{'key': value}``. Valid in a
# Python dict literal, a syntax error in Cypher.
_QUOTED_MAP_KEY = re.compile(r"'[^']*'\s*:")

_MAKES_AND_DDL = {
    "vector_index": _ENSURE_VECTOR_INDEX,
    "chunk_fulltext_index": chunk_text_index_ddl("standard-no-stop-words"),
    "chunk_fulltext_index_alt": chunk_text_index_ddl("simple"),
}


@pytest.mark.parametrize("name", sorted(_MAKES_AND_DDL))
def test_ddl_has_no_single_quoted_map_key(name: str) -> None:
    ddl = _MAKES_AND_DDL[name]

    assert "indexConfig" in ddl, f"{name}: this statement is supposed to carry an OPTIONS map"
    assert _QUOTED_MAP_KEY.search(ddl) is None, (
        f"{name}: a Cypher map key is single-quoted, which Neo4j rejects at parse "
        f"time (use backticks): {ddl!r}"
    )


def test_chunk_fulltext_ddl_uses_backticks_for_the_dotted_key() -> None:
    """The exact key that failed in production, asserted as text.

    The property above would also pass a DDL that dropped the analyzer, so this
    one names the key and its quoting: production accepted
    ``OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard'}}``.
    """
    ddl = chunk_text_index_ddl("standard-no-stop-words")

    assert "`fulltext.analyzer`" in ddl
    assert "'fulltext.analyzer'" not in ddl
