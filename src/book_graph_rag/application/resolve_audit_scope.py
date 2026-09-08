"""Fail-fast scope resolution against the catalog before any graph connection."""

from __future__ import annotations

import re

from book_graph_rag.domain.audit_models import AuditScope
from book_graph_rag.domain.namespaces import Catalog, UnknownNamespaceError

_SCOPE_RE = re.compile(r"^[a-z0-9_-]+(?::[a-z0-9_-]+)?$")


def resolve_audit_scope(raw: str, catalog: Catalog) -> AuditScope:
    """Parse and validate a ``corpus[:source]`` scope string.

    Raises ``ValueError`` for malformed input or unknown corpus/source.
    The exception is raised before any Neo4j adapter is constructed.
    """
    if not raw:
        raise ValueError("scope cannot be empty")
    if not _SCOPE_RE.match(raw):
        raise ValueError(f"Malformed scope: {raw!r}")

    parts = raw.split(":")
    if len(parts) == 1:
        corpus = parts[0]
        if corpus not in catalog.corpora:
            raise ValueError(f"Unknown corpus: {corpus!r}")
        return AuditScope(corpus=corpus)

    if len(parts) == 2:
        corpus, source = parts
        if not corpus or not source:
            raise ValueError("corpus and source cannot be empty")
        try:
            catalog.resolve_source(corpus, source)
        except UnknownNamespaceError as exc:
            raise ValueError(str(exc)) from exc
        return AuditScope(corpus=corpus, source=source)

    raise ValueError(f"Malformed scope: {raw!r}")
