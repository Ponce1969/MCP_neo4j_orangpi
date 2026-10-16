"""Read redacted checkpoint error metadata back from a Neo4j node (R5).

Neo4j properties are scalars, so the keyed fingerprint is stored flattened as a hex digest plus
the id of the key that produced it. Both checkpoint readers go through this one function: the
mapping is the inverse of what the writers do, and two copies of it would drift.
"""

from __future__ import annotations

from typing import Any

from book_graph_rag.domain.mcp_security import QueryFingerprint


def error_metadata_from_node(
    node: Any,
) -> tuple[str | None, int | None, QueryFingerprint | None]:
    """Return ``(error_type, error_length, error_fingerprint)`` for a checkpoint node.

    A legacy ``error_message`` written before this rule is deliberately **not** read: the raw
    text of a failure has no reader anywhere in the service.
    """
    fingerprint_hex = node.get("error_fingerprint_hex")
    fingerprint_key_id = node.get("error_fingerprint_key_id")
    fingerprint = (
        QueryFingerprint(key_id=fingerprint_key_id, fingerprint_hex=fingerprint_hex)
        if fingerprint_hex and fingerprint_key_id
        else None
    )
    return node.get("error_type"), node.get("error_length"), fingerprint
