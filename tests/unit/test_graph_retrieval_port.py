"""Tests for GraphRetrievalPort (Slice B, T-B.4)."""

from __future__ import annotations

import asyncio

import pytest

from book_graph_rag.ports.graph_retrieval_port import GraphRetrievalPort


def test_graph_retrieval_port_is_abstract() -> None:
    """GraphRetrievalPort cannot be instantiated directly."""
    with pytest.raises(TypeError, match="abstract"):
        GraphRetrievalPort()  # type: ignore[abstract]


def test_graph_retrieval_port_fetch_contexts_returns_ordered() -> None:
    """A fake adapter implements fetch_contexts and returns ordered contexts."""

    class FakeRetrieval(GraphRetrievalPort):
        async def fetch_contexts(
            self, *, question: str, qtype: str, detail_level: int,
        ) -> tuple[str, ...]:
            return ("ctx1", "ctx2")

        async def compose_answer(
            self, *, question: str, contexts: tuple[str, ...],
        ) -> str:
            return "answer"

    port = FakeRetrieval()
    contexts = asyncio.run(port.fetch_contexts(
        question="what is MCP?",
        qtype="global",
        detail_level=1,
    ))
    assert contexts == ("ctx1", "ctx2")


def test_graph_retrieval_port_compose_answer_returns_str() -> None:
    """A fake adapter implements compose_answer and returns a string."""

    class FakeRetrieval(GraphRetrievalPort):
        async def fetch_contexts(
            self, *, question: str, qtype: str, detail_level: int,
        ) -> tuple[str, ...]:
            return ()

        async def compose_answer(
            self, *, question: str, contexts: tuple[str, ...],
        ) -> str:
            return "MCP is a protocol."

    port = FakeRetrieval()
    answer = asyncio.run(port.compose_answer(
        question="what is MCP?",
        contexts=("ctx1",),
    ))
    assert answer == "MCP is a protocol."
