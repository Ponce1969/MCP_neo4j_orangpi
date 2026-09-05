"""Contract tests for the DeadLetterPort extension."""

from __future__ import annotations

import inspect
import typing
from typing import Any

from book_graph_rag.ports.dead_letter_port import DeadLetterPort


def test_dead_letter_port_has_write_failed_chunk() -> None:
    """write_failed_chunk is an abstract method accepting a dict record."""
    assert hasattr(DeadLetterPort, "write_failed_chunk")
    method = DeadLetterPort.write_failed_chunk
    assert getattr(method, "__isabstractmethod__", False)

    sig = inspect.signature(method)
    hints = typing.get_type_hints(method)
    assert "record" in sig.parameters
    assert hints["record"] == dict[str, Any]
