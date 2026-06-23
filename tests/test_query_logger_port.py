"""Tests for the QueryLoggerPort (REQ-07.6)."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from book_graph_rag.domain.models import QueryLogEntry
from book_graph_rag.ports.query_logger_port import QueryLoggerPort


class _FakeQueryLoggerPort(QueryLoggerPort):
    """Every abstract method is implemented so instantiation should succeed."""

    def __init__(self) -> None:
        self.logged_entries: list[QueryLogEntry] = []

    async def log_query(self, entry: QueryLogEntry) -> None:
        self.logged_entries.append(entry)

    async def close(self) -> None:
        return None


class _IncompleteQueryLoggerPort(QueryLoggerPort):
    """Missing close so instantiation should fail."""

    async def log_query(self, entry: QueryLogEntry) -> None:  # pragma: no cover
        return None

    # close intentionally omitted


def test_query_logger_port_is_abstract() -> None:
    """QueryLoggerPort cannot be instantiated directly."""
    with pytest.raises(TypeError):
        QueryLoggerPort()


def test_query_logger_port_complete_subclass_can_be_instantiated() -> None:
    """A subclass implementing all methods can be instantiated."""
    logger = _FakeQueryLoggerPort()

    assert logger is not None


def test_query_logger_port_missing_method_cannot_be_instantiated() -> None:
    """A subclass missing a method is still abstract."""
    with pytest.raises(TypeError):
        _IncompleteQueryLoggerPort()


@pytest.mark.parametrize("method_name", ["log_query", "close"])
def test_query_logger_port_methods_are_async(method_name: str) -> None:
    """Every port method is declared async."""
    method = getattr(QueryLoggerPort, method_name)

    assert inspect.iscoroutinefunction(method)


async def test_fake_query_logger_port_logs_entry() -> None:
    """The fake implementation stores entries for test inspection."""
    logger = _FakeQueryLoggerPort()
    entry = QueryLogEntry(
        timestamp=datetime.now(tz=UTC),
        tool_name="find_entity",
        query_type="entity",
        query_params={"name": "MCP"},
        result_count=0,
        zero_results=True,
        entity_not_found=True,
        duration_ms=10.0,
    )

    await logger.log_query(entry)

    assert logger.logged_entries == [entry]
