"""Tests for the MCP console script entry point (T-07.8)."""

from __future__ import annotations

import importlib.metadata

from book_graph_rag.main import main as cli_main
from book_graph_rag.mcp_server_main import main as mcp_main


def _entry_point(name: str) -> importlib.metadata.EntryPoint | None:
    entry_points = importlib.metadata.entry_points(group="console_scripts")
    return next((ep for ep in entry_points if ep.name == name), None)


def test_book_graph_rag_mcp_console_script_exists() -> None:
    """pyproject.toml exposes `book-graph-rag-mcp` pointing to mcp_server_main:main."""
    mcp_entry = _entry_point("book-graph-rag-mcp")

    assert mcp_entry is not None
    assert mcp_entry.value == "book_graph_rag.mcp_server_main:main"


def test_book_graph_rag_mcp_console_script_loads_main() -> None:
    """The installed console script resolves to the mcp_server_main.main function."""
    mcp_entry = _entry_point("book-graph-rag-mcp")
    assert mcp_entry is not None

    loaded = mcp_entry.load()

    assert loaded is mcp_main
    assert callable(loaded)


def test_book_graph_rag_cli_console_script_unchanged() -> None:
    """The original CLI console script remains intact after adding the MCP entry."""
    cli_entry = _entry_point("book-graph-rag")

    assert cli_entry is not None
    assert cli_entry.value == "book_graph_rag.main:main"
    assert cli_entry.load() is cli_main
