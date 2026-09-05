"""Contract tests for the GraphDatabasePort extension."""

from __future__ import annotations

import inspect
import typing

import pytest

from book_graph_rag.domain.checkpoint_models import Checkpoint, VersionDimensions
from book_graph_rag.domain.models import KnowledgeGraphChunk
from book_graph_rag.ports.graph_db_port import GraphDatabasePort


@pytest.fixture
def expected_signature() -> dict[str, object]:
    return {
        "chunk": KnowledgeGraphChunk,
        "entity_ids": list[str],
        "versions": VersionDimensions,
        "attempt": int,
        "lease_owned": bool,
        "return": Checkpoint,
    }


def test_graph_db_port_has_commit_chunk_atomic(expected_signature: dict[str, object]) -> None:
    """commit_chunk_atomic is an abstract method with the design §2.1 signature."""
    assert hasattr(GraphDatabasePort, "commit_chunk_atomic")
    method = GraphDatabasePort.commit_chunk_atomic
    assert getattr(method, "__isabstractmethod__", False)

    sig = inspect.signature(method)
    hints = typing.get_type_hints(method)
    for name, expected_type in expected_signature.items():
        if name == "return":
            assert hints["return"] == expected_type
            continue
        assert name in sig.parameters, f"missing parameter {name}"
        assert hints[name] == expected_type, f"{name} annotation mismatch"

    # Keyword-only flags.
    assert sig.parameters["attempt"].default == 1
    assert sig.parameters["lease_owned"].default is True
