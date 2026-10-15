"""Tests for GraphRetrievalPort (Slice B, T-B.4)."""

from __future__ import annotations

import asyncio

import pytest

from book_graph_rag.domain.evaluation_models import GlobalRetrievalAnswer, RetrievalContext
from book_graph_rag.ports.graph_retrieval_port import GraphRetrievalPort


def test_graph_retrieval_port_is_abstract() -> None:
    """GraphRetrievalPort cannot be instantiated directly."""
    with pytest.raises(TypeError, match="abstract"):
        GraphRetrievalPort()  # type: ignore[abstract]


def test_graph_retrieval_port_fetch_contexts_returns_ordered() -> None:
    """A fake adapter implements fetch_contexts and returns ordered contexts."""

    class FakeRetrieval(GraphRetrievalPort):
        async def fetch_contexts(
            self,
            *,
            question: str,
            qtype: str,
            detail_level: int,
        ) -> tuple[RetrievalContext, ...]:
            return (
                RetrievalContext(chunk_id=None, text="ctx1"),
                RetrievalContext(chunk_id=None, text="ctx2"),
            )

        async def compose_answer(
            self,
            *,
            question: str,
            contexts: tuple[RetrievalContext, ...],
        ) -> str:
            return "answer"

        async def answer_global(
            self,
            *,
            question: str,
            detail_level: int,
        ) -> GlobalRetrievalAnswer:
            return GlobalRetrievalAnswer(answer="answer", contexts=(), citation_ids=())

    port = FakeRetrieval()
    contexts = asyncio.run(
        port.fetch_contexts(
            question="what is MCP?",
            qtype="global",
            detail_level=1,
        )
    )
    assert contexts == (
        RetrievalContext(chunk_id=None, text="ctx1"),
        RetrievalContext(chunk_id=None, text="ctx2"),
    )


def test_graph_retrieval_port_compose_answer_returns_str() -> None:
    """A fake adapter implements compose_answer and returns a string."""

    class FakeRetrieval(GraphRetrievalPort):
        async def fetch_contexts(
            self,
            *,
            question: str,
            qtype: str,
            detail_level: int,
        ) -> tuple[RetrievalContext, ...]:
            return ()

        async def compose_answer(
            self,
            *,
            question: str,
            contexts: tuple[RetrievalContext, ...],
        ) -> str:
            return "MCP is a protocol."

        async def answer_global(
            self,
            *,
            question: str,
            detail_level: int,
        ) -> GlobalRetrievalAnswer:
            return GlobalRetrievalAnswer(
                answer="MCP is a protocol.",
                contexts=(RetrievalContext(chunk_id=None, text="ctx1"),),
                citation_ids=("summary-1",),
            )

    port = FakeRetrieval()
    answer = asyncio.run(
        port.compose_answer(
            question="what is MCP?",
            contexts=(RetrievalContext(chunk_id=None, text="ctx1"),),
        )
    )
    assert answer == "MCP is a protocol."


def test_graph_retrieval_port_answer_global_pairs_the_answer_with_its_ids() -> None:
    """The global contract returns the answer with the evidence ids, aligned by index.

    The composer production runs cites the summaries by id, so the ids must travel next to
    the contexts they belong to: a length mismatch is a programming error, not a case to
    paper over.
    """

    class FakeRetrieval(GraphRetrievalPort):
        async def fetch_contexts(
            self,
            *,
            question: str,
            qtype: str,
            detail_level: int,
        ) -> tuple[RetrievalContext, ...]:
            return ()

        async def compose_answer(
            self,
            *,
            question: str,
            contexts: tuple[RetrievalContext, ...],
        ) -> str:
            return ""

        async def answer_global(
            self,
            *,
            question: str,
            detail_level: int,
        ) -> GlobalRetrievalAnswer:
            return GlobalRetrievalAnswer(
                answer="ranked answer",
                contexts=(
                    RetrievalContext(chunk_id=None, text="best summary"),
                    RetrievalContext(chunk_id=None, text="second summary"),
                ),
                citation_ids=("summary-a", "summary-b"),
            )

    result = asyncio.run(FakeRetrieval().answer_global(question="q", detail_level=1))

    assert result.answer == "ranked answer"
    assert [context.text for context in result.contexts] == ["best summary", "second summary"]
    assert result.citation_ids == ("summary-a", "summary-b")
    assert len(result.citation_ids) == len(result.contexts)
