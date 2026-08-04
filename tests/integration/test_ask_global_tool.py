"""Integration tests for the ask_global MCP tool (REQ-GR.1 PR3)."""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.infrastructure.mcp.mcp_server_adapter import McpServerAdapter
from book_graph_rag.ports.community_read_port import CommunityReadPort
from book_graph_rag.ports.llm_summary_port import LLMSummaryPort
from book_graph_rag.ports.query_logger_port import QueryLoggerPort


class _FakeCommunityReadPort(CommunityReadPort):
    async def load_entity_graph(self) -> tuple[list[Any], list[Any]]:
        return [], []

    async def get_summaries_by_level(self, level: int) -> list[Any]:
        return []

    async def count_summaries(self) -> int:
        return 0


class _FakeLLMSummaryPort(LLMSummaryPort):
    async def generate_community_summary(
        self, entities: list[Any], relationships: list[Any], level: int
    ) -> str:
        return "summary"

    async def score_community(self, question: str, summary: Any) -> int:
        return 50

    async def compose_answer(self, question: str, ranked: list[Any]) -> str:
        return "answer"

    async def generate_summary_from_children(self, child_summaries: list[Any], level: int) -> str:
        return "summary"


class _FakeGlobalQueryUseCase:
    """Configurable GlobalQueryUseCase stand-in for the MCP adapter."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.response: dict[str, Any] = {
            "answer": "MCP is a protocol.",
            "citations": ["a1b2c3d4e5f6a7b8"],
        }

    async def ask(self, question: str, detail_level: int) -> dict[str, Any]:
        self.calls.append((question, detail_level))
        return self.response


class _FakeQueryLoggerPort(QueryLoggerPort):
    async def log_query(self, entry: Any) -> None:
        pass

    async def close(self) -> None:
        pass


class _FakeGraphQueryPort:
    async def find_entity(self, name: str, entity_type: Any | None) -> list[Any]:
        return []

    async def find_entities_batch(self, ids: list[str]) -> list[Any]:
        return []

    async def traverse_relationships(
        self, source_id: str, rel_type: Any | None, depth: int
    ) -> tuple[list[Any], list[Any]]:
        return [], []

    async def find_path(self, start_id: str, end_id: str, max_depth: int) -> list[Any]:
        return []

    async def search_chunks(self, query: str, limit: int) -> list[Any]:
        return []

    async def count_entities(self, entity_type: str | None) -> int:
        return 0

    async def list_entities(
        self, cursor: int, page_size: int
    ) -> tuple[list[Any], int]:
        return [], 0

    async def ensure_indexes(self) -> None:
        pass


class _FakeText2CypherPort:
    async def generate_and_run(self, question: str) -> Any:
        return None


@pytest.fixture
def global_use_case() -> _FakeGlobalQueryUseCase:
    return _FakeGlobalQueryUseCase()


@pytest.fixture
def adapter(global_use_case: _FakeGlobalQueryUseCase) -> McpServerAdapter:
    return McpServerAdapter(
        graph_query_port=_FakeGraphQueryPort(),
        query_logger=_FakeQueryLoggerPort(),
        text2cypher_port=_FakeText2CypherPort(),
        global_query_use_case=global_use_case,
    )


async def test_ask_global_is_registered_in_server(
    adapter: McpServerAdapter,
) -> None:
    """create_server exposes the ask_global tool alongside the existing tools."""
    server = adapter.create_server()
    tools = await server.list_tools()
    names = {tool.name for tool in tools}

    assert "ask_global" in names


async def test_ask_global_rejects_invalid_detail_level(adapter: McpServerAdapter) -> None:
    """detail_level outside [0, 3] is rejected before the use case is called."""
    server = adapter.create_server()

    with pytest.raises(Exception, match="detail_level"):
        await server.call_tool("ask_global", {"question": "what is MCP?", "detail_level": 5})


async def test_ask_global_returns_answer_with_citations(
    adapter: McpServerAdapter,
    global_use_case: _FakeGlobalQueryUseCase,
) -> None:
    """A valid ask_global call returns the use-case answer and citations."""
    server = adapter.create_server()
    global_use_case.response = {
        "answer": "MCP is a protocol [Data: CommunitySummary(a1b2c3d4e5f6a7b8)].",
        "citations": ["a1b2c3d4e5f6a7b8"],
    }

    result = await server.call_tool(
        "ask_global", {"question": "what is MCP?", "detail_level": 2}
    )

    content = result[0]
    assert len(content) == 1
    assert content[0].type == "text"
    assert "MCP is a protocol" in content[0].text
    assert "a1b2c3d4e5f6a7b8" in content[0].text


async def test_ask_global_passes_question_and_detail_level_to_use_case(
    adapter: McpServerAdapter,
    global_use_case: _FakeGlobalQueryUseCase,
) -> None:
    """The tool forwards the question and detail_level to the use case."""
    server = adapter.create_server()

    await server.call_tool("ask_global", {"question": "patterns?", "detail_level": 1})

    assert global_use_case.calls == [("patterns?", 1)]


async def test_ask_global_returns_error_when_no_summaries(
    adapter: McpServerAdapter,
    global_use_case: _FakeGlobalQueryUseCase,
) -> None:
    """When summaries are missing, the response is a distinct error message."""
    server = adapter.create_server()
    global_use_case.response = {
        "answer": "Run scripts/run_communities.py first",
        "citations": [],
    }

    result = await server.call_tool("ask_global", {"question": "what?", "detail_level": 0})

    assert "Run scripts/run_communities.py first" in result[0][0].text
