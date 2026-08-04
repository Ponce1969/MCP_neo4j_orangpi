"""Tests for community-summary ports (PR1 Foundation)."""

from __future__ import annotations

import inspect
import typing

import pytest

from book_graph_rag.domain.models import CommunitySummary, Entity, Relationship
from book_graph_rag.ports.community_read_port import CommunityReadPort
from book_graph_rag.ports.community_write_port import CommunityWritePort
from book_graph_rag.ports.llm_summary_port import LLMSummaryPort


def test_llm_summary_port_is_abstract() -> None:
    """LLMSummaryPort cannot be instantiated directly."""
    with pytest.raises(TypeError):
        LLMSummaryPort()


def test_llm_summary_port_methods_are_async() -> None:
    """All LLMSummaryPort methods are declared async."""
    assert inspect.iscoroutinefunction(LLMSummaryPort.generate_community_summary)
    assert inspect.iscoroutinefunction(LLMSummaryPort.score_community)
    assert inspect.iscoroutinefunction(LLMSummaryPort.compose_answer)


def test_llm_summary_port_method_signature() -> None:
    """The port receives entities, relationships and level, returning a string."""
    signature = inspect.signature(LLMSummaryPort.generate_community_summary)
    parameters = [
        name for name in signature.parameters if name not in ("self", "cls")
    ]
    evaluated = typing.get_type_hints(LLMSummaryPort.generate_community_summary)

    assert parameters == ["entities", "relationships", "level"]
    assert evaluated["return"] is str


def test_llm_summary_port_score_community_signature() -> None:
    """The port receives a question and a summary, returning an integer score."""
    signature = inspect.signature(LLMSummaryPort.score_community)
    parameters = [
        name for name in signature.parameters if name not in ("self", "cls")
    ]
    evaluated = typing.get_type_hints(LLMSummaryPort.score_community)

    assert parameters == ["question", "summary"]
    assert evaluated["return"] is int


def test_llm_summary_port_compose_answer_signature() -> None:
    """The port receives a question and ranked summaries, returning an answer string."""
    signature = inspect.signature(LLMSummaryPort.compose_answer)
    parameters = [
        name for name in signature.parameters if name not in ("self", "cls")
    ]
    evaluated = typing.get_type_hints(LLMSummaryPort.compose_answer)

    assert parameters == ["question", "ranked"]
    assert evaluated["return"] is str


class _CompleteLLMSummaryPort(LLMSummaryPort):
    async def generate_community_summary(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
        level: int,
    ) -> str:
        return "summary"

    async def score_community(self, question: str, summary: CommunitySummary) -> int:
        return 50

    async def compose_answer(
        self, question: str, ranked: list[tuple[CommunitySummary, int]]
    ) -> str:
        return "answer"

    async def generate_summary_from_children(
        self, child_summaries: list[str], level: int
    ) -> str:
        return "summary"


def test_llm_summary_port_complete_subclass_can_be_instantiated() -> None:
    """A subclass implementing the methods can be instantiated."""
    adapter = _CompleteLLMSummaryPort()
    assert adapter is not None


class _IncompleteLLMSummaryPort(LLMSummaryPort):
    async def generate_community_summary(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
        level: int,
    ) -> str:
        return "summary"


def test_llm_summary_port_missing_method_cannot_be_instantiated() -> None:
    """A subclass missing a method is still abstract."""
    with pytest.raises(TypeError):
        _IncompleteLLMSummaryPort()


def test_community_read_port_is_abstract() -> None:
    """CommunityReadPort cannot be instantiated directly."""
    with pytest.raises(TypeError):
        CommunityReadPort()


def test_community_read_port_methods_are_async() -> None:
    """All read port methods are declared async."""
    assert inspect.iscoroutinefunction(CommunityReadPort.load_entity_graph)
    assert inspect.iscoroutinefunction(CommunityReadPort.get_summaries_by_level)
    assert inspect.iscoroutinefunction(CommunityReadPort.count_summaries)


def test_community_read_port_load_entity_graph_signature() -> None:
    """The port loads the base entity graph and returns entities + relationships."""
    signature = inspect.signature(CommunityReadPort.load_entity_graph)
    parameters = [
        name for name in signature.parameters if name not in ("self", "cls")
    ]
    evaluated = typing.get_type_hints(CommunityReadPort.load_entity_graph)

    assert parameters == []
    assert evaluated["return"] == tuple[list[Entity], list[Relationship]]


def test_community_read_port_get_summaries_by_level_signature() -> None:
    """The port receives a level and returns a list of CommunitySummary."""
    signature = inspect.signature(CommunityReadPort.get_summaries_by_level)
    parameters = [
        name for name in signature.parameters if name not in ("self", "cls")
    ]
    evaluated = typing.get_type_hints(CommunityReadPort.get_summaries_by_level)

    assert parameters == ["level"]
    assert evaluated["return"] == list[CommunitySummary]


def test_community_read_port_count_summaries_signature() -> None:
    """The port returns the total number of persisted summaries."""
    signature = inspect.signature(CommunityReadPort.count_summaries)
    parameters = [
        name for name in signature.parameters if name not in ("self", "cls")
    ]
    evaluated = typing.get_type_hints(CommunityReadPort.count_summaries)

    assert parameters == []
    assert evaluated["return"] is int


class _CompleteCommunityReadPort(CommunityReadPort):
    async def load_entity_graph(self) -> tuple[list[Entity], list[Relationship]]:
        return [], []

    async def get_summaries_by_level(self, level: int) -> list[CommunitySummary]:
        return []

    async def count_summaries(self) -> int:
        return 0


def test_community_read_port_complete_subclass_can_be_instantiated() -> None:
    """A subclass implementing the methods can be instantiated."""
    adapter = _CompleteCommunityReadPort()
    assert adapter is not None


class _IncompleteCommunityReadPort(CommunityReadPort):
    async def get_summaries_by_level(self, level: int) -> list[CommunitySummary]:
        return []


def test_community_read_port_missing_method_cannot_be_instantiated() -> None:
    """A subclass missing any method is still abstract."""
    with pytest.raises(TypeError):
        _IncompleteCommunityReadPort()


def test_community_write_port_is_abstract() -> None:
    """CommunityWritePort cannot be instantiated directly."""
    with pytest.raises(TypeError):
        CommunityWritePort()


def test_community_write_port_methods_are_async() -> None:
    """Both write methods are declared async."""
    assert inspect.iscoroutinefunction(CommunityWritePort.upsert_summaries)
    assert inspect.iscoroutinefunction(CommunityWritePort.clear_summaries)


def test_community_write_port_method_signatures() -> None:
    """upsert_summaries takes a list of CommunitySummary; clear_summaries takes none."""
    upsert_sig = inspect.signature(CommunityWritePort.upsert_summaries)
    clear_sig = inspect.signature(CommunityWritePort.clear_summaries)
    upsert_hints = typing.get_type_hints(CommunityWritePort.upsert_summaries)
    clear_hints = typing.get_type_hints(CommunityWritePort.clear_summaries)

    assert [
        name for name in upsert_sig.parameters if name not in ("self", "cls")
    ] == ["summaries"]
    assert upsert_hints["return"] is type(None)
    assert [
        name for name in clear_sig.parameters if name not in ("self", "cls")
    ] == []
    assert clear_hints["return"] is type(None)


class _CompleteCommunityWritePort(CommunityWritePort):
    async def upsert_summaries(self, summaries: list[CommunitySummary]) -> None:
        return None

    async def upsert_summary(self, summary: CommunitySummary) -> None:
        return None

    async def clear_summaries(self) -> None:
        return None


def test_community_write_port_complete_subclass_can_be_instantiated() -> None:
    """A subclass implementing both methods can be instantiated."""
    adapter = _CompleteCommunityWritePort()
    assert adapter is not None


class _IncompleteCommunityWritePort(CommunityWritePort):
    async def upsert_summaries(self, summaries: list[CommunitySummary]) -> None:
        return None


def test_community_write_port_missing_method_cannot_be_instantiated() -> None:
    """A subclass missing one method is still abstract."""
    with pytest.raises(TypeError):
        _IncompleteCommunityWritePort()


def test_community_ports_are_exported_from_package() -> None:
    """The three new ports are reachable from the ports package."""
    import book_graph_rag.ports as ports

    assert hasattr(ports, "LLMSummaryPort")
    assert hasattr(ports, "CommunityReadPort")
    assert hasattr(ports, "CommunityWritePort")
