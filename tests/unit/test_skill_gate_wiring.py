"""Unit tests for the skill gate wiring in the MCP composition root (Unit 3.1-3.3).

The gate is off by default, so the server keeps exposing every registered tool. With it on,
the exposed set is exactly what the gate selected, and an unseeded registry exposes
**nothing** rather than falling back to everything — the fail-closed property that makes the
seeding order (Unit 4) matter before any deploy.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from book_graph_rag.config import Settings
from book_graph_rag.domain.skill_models import Skill, SkillQualityScores
from book_graph_rag.mcp_server_main import _resolve_tool_names
from book_graph_rag.ports.skill_registry_port import SkillRegistryPort


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": SecretStr("fake"),  # pragma: allowlist secret
    }
    return Settings.model_validate({**base, **overrides})


def _skill(skill_id: str, tool_names: tuple[str, ...], score: float = 0.9) -> Skill:
    """A skill whose weighted score equals ``score`` (every dimension set to it)."""
    return Skill(
        id=skill_id,
        name=skill_id,
        version="1.0.0",
        status="active",
        tool_names=tool_names,
        scores=SkillQualityScores(
            safety=score,
            executability=score,
            completeness=score,
            maintainability=score,
            cost_awareness=score,
        ),
    )


class _FakeRegistry(SkillRegistryPort):
    def __init__(self, skills: tuple[Skill, ...]) -> None:
        self._skills = skills

    async def load_active(self) -> tuple[Skill, ...]:
        return self._skills


async def test_gate_off_keeps_every_tool() -> None:
    """``None`` means "all eight": the default keeps the historical behaviour."""
    registry = _FakeRegistry((_skill("s:one", ("find_entity",)),))

    assert await _resolve_tool_names(_settings(), registry) is None


async def test_gate_on_exposes_exactly_the_selected_tool_names() -> None:
    """The union of the selected skills' tools is the model-facing set."""
    registry = _FakeRegistry(
        (
            _skill("s:a", ("find_entity", "count_entities")),
            _skill("s:b", ("search_rag",)),
        )
    )

    result = await _resolve_tool_names(_settings(skill_gate_enabled=True), registry)

    assert result == frozenset({"find_entity", "count_entities", "search_rag"})


async def test_gate_on_with_an_unseeded_registry_exposes_nothing() -> None:
    """Fail-closed: an empty registry must not fall back to every tool."""
    result = await _resolve_tool_names(_settings(skill_gate_enabled=True), _FakeRegistry(()))

    assert result == frozenset()


async def test_gate_on_gates_out_a_skill_below_the_configured_threshold() -> None:
    """``skill_min_quality`` from settings reaches the wiring, not only the gate's tests."""
    registry = _FakeRegistry((_skill("s:low", ("find_entity",), score=0.59),))

    result = await _resolve_tool_names(_settings(skill_gate_enabled=True), registry)

    assert result == frozenset()


async def test_top_k_from_settings_caps_how_many_skills_contribute_tools() -> None:
    """``skill_top_k`` is honoured through the wiring, with a deterministic order."""
    names = (
        "find_entity",
        "traverse_relationships",
        "search_chunks",
        "list_entities",
        "count_entities",
    )
    registry = _FakeRegistry(
        tuple(_skill(f"s:{n}", (name,), score=0.9 - n / 100) for n, name in enumerate(names))
    )

    result = await _resolve_tool_names(_settings(skill_gate_enabled=True, skill_top_k=3), registry)

    assert result == frozenset(names[:3])
