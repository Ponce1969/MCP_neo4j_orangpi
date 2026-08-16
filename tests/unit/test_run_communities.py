"""Tests for the offline community-summary script."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import CommunitySummary, Entity, Relationship
from book_graph_rag.ports.llm_summary_port import LLMSummaryPort

_SCRIPT_PATH = Path("scripts/run_communities.py")


def _load_script_module() -> Any:
    """Load ``scripts/run_communities.py`` as a module for testing."""
    spec = importlib.util.spec_from_file_location("run_communities", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_communities"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def run_communities() -> Any:
    return _load_script_module()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.chdir(tmp_path)
    for var in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    return Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "community_max_calls": 100,
            "summary_max_concurrency": 2,
        }
    )


class _FakeReadPort:
    def __init__(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
    ) -> None:
        self._entities = entities
        self._relationships = relationships
        self.load_calls = 0

    async def load_entity_graph(self) -> tuple[list[Entity], list[Relationship]]:
        self.load_calls += 1
        return self._entities, self._relationships

    async def get_summaries_by_level(self, level: int) -> list[CommunitySummary]:
        return []

    async def count_summaries(self) -> int:
        return 0


class _FakeWritePort:
    def __init__(self) -> None:
        self.cleared = False
        self.upserted: list[CommunitySummary] = []
        self.single_upserted: list[CommunitySummary] = []

    async def clear_summaries(self) -> None:
        self.cleared = True

    async def upsert_summaries(self, summaries: list[CommunitySummary]) -> None:
        self.upserted = summaries

    async def upsert_summary(self, summary: CommunitySummary) -> None:
        self.single_upserted.append(summary)


class _FakeLLMPort:
    def __init__(self) -> None:
        self.calls: list[tuple[int, list[str]]] = []

    async def generate_community_summary(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
        level: int,
    ) -> str:
        self.calls.append((level, [e.id for e in entities]))
        return f"Summary for level {level} with {len(entities)} entities"

    async def score_community(self, question: str, summary: CommunitySummary) -> int:
        return 50

    async def compose_answer(
        self, question: str, ranked: list[tuple[CommunitySummary, int]]
    ) -> str:
        return "answer"

    async def generate_summary_from_children(self, child_summaries: list[str], level: int) -> str:
        self.calls.append((level, child_summaries))
        return f"Summary for level {level} from {len(child_summaries)} children"


def test_build_cli_help(run_communities: Any) -> None:
    """The CLI exposes a ``run`` command with --help."""
    runner = CliRunner()
    result = runner.invoke(run_communities.cli, ["run", "--help"])

    assert result.exit_code == 0
    assert "run" in result.output


async def test_run_communities_clears_and_upserts(
    run_communities: Any,
    settings: Settings,
) -> None:
    """The orchestration clears old summaries and persists new ones."""
    entities = [
        Entity(id="a", name="A", type="agent"),
        Entity(id="b", name="B", type="agent"),
    ]
    relationships = [
        Relationship(source_entity_id="a", target_entity_id="b", type="requires")
    ]
    read_port = _FakeReadPort(entities, relationships)
    write_port = _FakeWritePort()
    llm_port = _FakeLLMPort()

    await run_communities._run_communities(
        read_port, write_port, llm_port, settings, fresh=True
    )

    assert write_port.cleared
    assert len(write_port.single_upserted) >= 1
    assert all(isinstance(s, CommunitySummary) for s in write_port.single_upserted)
    assert any(s.level == 0 for s in write_port.single_upserted)


async def test_run_communities_aborts_when_max_calls_exceeded(
    run_communities: Any,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the total community count exceeds ``community_max_calls``, abort."""
    entities = [Entity(id=f"e{i}", name=f"E{i}", type="agent") for i in range(50)]
    relationships: list[Relationship] = []
    read_port = _FakeReadPort(entities, relationships)
    write_port = _FakeWritePort()
    llm_port = _FakeLLMPort()

    settings = settings.model_copy(update={"community_max_calls": 1})

    def fake_run_leiden(graph: Any, resolution: float, backend: str) -> list[list[str]]:
        return [["e0"], ["e1"], ["e2"]]

    monkeypatch.setattr(run_communities, "run_leiden", fake_run_leiden)

    with pytest.raises(run_communities.CommunityDetectionError):
        await run_communities._run_communities(
            read_port, write_port, llm_port, settings
        )

    assert not write_port.cleared
    assert not write_port.upserted


async def test_run_communities_llm_calls_respect_concurrency(
    run_communities: Any,
    settings: Settings,
) -> None:
    """The LLM port is called once per community with bounded concurrency."""
    entities = [
        Entity(id="a", name="A", type="agent"),
        Entity(id="b", name="B", type="agent"),
    ]
    relationships = [
        Relationship(source_entity_id="a", target_entity_id="b", type="requires")
    ]
    read_port = _FakeReadPort(entities, relationships)
    write_port = _FakeWritePort()
    llm_port = _FakeLLMPort()

    await run_communities._run_communities(
        read_port, write_port, llm_port, settings
    )

    assert len(llm_port.calls) == len(write_port.single_upserted)
    assert all(call[0] in {0, 1, 2, 3} for call in llm_port.calls)


async def test_run_communities_logs_counts(
    run_communities: Any,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The script logs level counts and the base graph size."""
    entities = [
        Entity(id="a", name="A", type="agent"),
        Entity(id="b", name="B", type="agent"),
    ]
    relationships = [
        Relationship(source_entity_id="a", target_entity_id="b", type="requires")
    ]
    read_port = _FakeReadPort(entities, relationships)
    write_port = _FakeWritePort()
    llm_port = _FakeLLMPort()

    await run_communities._run_communities(
        read_port, write_port, llm_port, settings
    )

    captured = capsys.readouterr()
    assert "entities" in captured.out
    assert "relationships" in captured.out
    assert "Level" in captured.out


def test_cli_run_invokes_orchestration(
    run_communities: Any,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI command wires Settings and ports into the orchestration."""
    monkeypatch.chdir(tmp_path)
    for var in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    class _FakeSettingsType:
        @classmethod
        def model_validate(cls, data: object) -> Settings:
            return settings

    monkeypatch.setattr(run_communities, "Settings", _FakeSettingsType)

    entities = [
        Entity(id="a", name="A", type="agent"),
        Entity(id="b", name="B", type="agent"),
    ]
    relationships = [
        Relationship(source_entity_id="a", target_entity_id="b", type="requires")
    ]

    calls: list[str] = []

    class FakeAdapter:
        def __init__(self, settings: Settings) -> None:
            self._settings = settings
            calls.append("adapter")

        async def ensure_indexes(self) -> None:
            calls.append("ensure_indexes")

        async def close(self) -> None:
            calls.append("close")

        async def load_entity_graph(self) -> tuple[list[Entity], list[Relationship]]:
            return entities, relationships

        async def clear_summaries(self) -> None:
            calls.append("clear")

        async def upsert_summaries(self, summaries: list[CommunitySummary]) -> None:
            calls.append("upsert")

        async def upsert_summary(self, summary: CommunitySummary) -> None:
            calls.append("upsert_summary")

        async def get_summaries_by_level(self, level: int) -> list[CommunitySummary]:
            return []

        async def count_summaries(self) -> int:
            return 0

    class FakeLLM(LLMSummaryPort):
        def __init__(self, settings: Settings) -> None:
            self._settings = settings

        async def generate_community_summary(
            self, entities: list[Entity], relationships: list[Relationship], level: int
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

    monkeypatch.setattr(run_communities, "Neo4jCommunityAdapter", FakeAdapter)
    monkeypatch.setattr(run_communities, "LLMAdapter", FakeLLM)

    runner = CliRunner()
    result = runner.invoke(run_communities.cli, ["run", "--fresh"])

    assert result.exit_code == 0, result.output
    assert "adapter" in calls
    assert "ensure_indexes" in calls
    assert "clear" in calls
    assert "upsert_summary" in calls
    assert "close" in calls
