"""Unit tests for the shared checkpoint error-metadata mapping (R5).

The adapters write the keyed fingerprint flattened, because Neo4j properties are scalars, and both
readers rebuild it through :func:`error_metadata_from_node`. These tests pin the inverse mapping
itself: the live-Neo4j integration test covers the round trip through the driver, and a mapping that
silently returned nothing would still pass that one only when the server is reachable.
"""

from __future__ import annotations

from book_graph_rag.domain.mcp_security import QueryFingerprint
from book_graph_rag.infrastructure.checkpoint_error_metadata import error_metadata_from_node

_RAW = "driver said: 401 for api_key=secret-token-abc"


def _fingerprint() -> QueryFingerprint:
    return QueryFingerprint.from_canonical("mcp-log-v1", "a" * 64, {"error_message": _RAW})


def test_the_flattened_properties_are_rebuilt_including_the_fingerprint() -> None:
    """What the writer flattened, the reader rebuilds — hex, key id and length together."""
    fingerprint = _fingerprint()
    node = {
        "error_type": "RuntimeError",
        "error_length": len(_RAW),
        "error_fingerprint_hex": fingerprint.fingerprint_hex,
        "error_fingerprint_key_id": fingerprint.key_id,
    }

    error_type, error_length, rebuilt = error_metadata_from_node(node)

    assert error_type == "RuntimeError"
    assert error_length == len(_RAW)
    assert rebuilt == fingerprint


def test_a_legacy_node_never_surfaces_its_raw_message() -> None:
    """A node written before this rule keeps ``error_message`` on disk; the reader ignores it."""
    error_type, error_length, rebuilt = error_metadata_from_node(
        {"error_type": "RuntimeError", "error_message": _RAW}
    )

    assert error_type == "RuntimeError"
    assert error_length is None
    assert rebuilt is None


def test_a_partial_fingerprint_is_not_half_rebuilt() -> None:
    """Hex without its key id (or the reverse) names no key, so it is not a usable fingerprint."""
    hex_only = {"error_fingerprint_hex": "ab" * 32}
    key_id_only = {"error_fingerprint_key_id": "mcp-log-v1"}

    assert error_metadata_from_node(hex_only)[2] is None
    assert error_metadata_from_node(key_id_only)[2] is None


def test_a_checkpoint_that_never_failed_reports_nothing() -> None:
    """The healthy case is the common one: no properties at all."""
    assert error_metadata_from_node({}) == (None, None, None)
