"""Contract tests for the CheckpointPort ABC."""

from __future__ import annotations

import inspect
import typing
from typing import Any

import pytest

from book_graph_rag.domain.checkpoint_models import Checkpoint, LeaseResult, VersionDimensions
from book_graph_rag.ports.checkpoint_port import CheckpointPort

_REQUIRED_METHODS: dict[str, dict[str, Any]] = {
    "acquire_lease": {
        "source_id": str,
        "chunk_index": int,
        "versions": VersionDimensions,
        "return": LeaseResult,
    },
    "release_lease_to_failed": {
        "source_id": str,
        "chunk_index": int,
        "error_type": str,
        "error_message": str,
        "attempt": int,
        "return": Checkpoint,
    },
    "mark_stale_and_reset": {
        "source_id": str,
        "expected_versions": VersionDimensions,
        "return": int,
    },
    "fetch_state": {
        "source_id": str,
        "chunk_indices": list[int],
        "return": dict[int, Checkpoint],
    },
    "reclaim_stale_leases": {
        "source_id": str,
        "now": "datetime",
        "stale_seconds": int,
        "return": int,
    },
}


def test_checkpoint_port_is_abstract() -> None:
    """The port cannot be instantiated directly."""
    with pytest.raises(TypeError):
        CheckpointPort()  # type: ignore[abstract]


@pytest.mark.parametrize("name", list(_REQUIRED_METHODS))
def test_checkpoint_port_exposes_required_methods(name: str) -> None:
    """All five checkpoint lifecycle methods are abstract on the port."""
    assert hasattr(CheckpointPort, name)
    method = getattr(CheckpointPort, name)
    assert getattr(method, "__isabstractmethod__", False)
    hints = typing.get_type_hints(method)
    sig = inspect.signature(method)
    expected = _REQUIRED_METHODS[name]
    for param_name, param_type in expected.items():
        if param_name == "return":
            assert hints["return"] == param_type
        elif param_type == "datetime":
            assert "datetime" in str(hints[param_name])
        else:
            assert hints[param_name] == param_type
    # Every required parameter must appear in the signature.
    for param_name in expected:
        if param_name != "return":
            assert param_name in sig.parameters
