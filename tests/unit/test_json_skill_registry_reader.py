"""Unit tests for the JSON skill registry mirror (skill-quality-gating, Unit 2.3).

The mirror lets the gate be exercised deterministically without a graph. It follows the
``JsonNamespaceProfileReader`` convention: a missing artifact is an empty snapshot, not an
error, and an entry that cannot be trusted is dropped instead of exposed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from book_graph_rag.infrastructure.json_skill_registry_reader import JsonSkillRegistryReader

_ENTRY: dict[str, Any] = {
    "id": "skill:react-lookup:1.0.0",
    "name": "React pattern lookup",
    "version": "1.0.0",
    "status": "active",
    "tool_names": ["find_entity"],
    "safety": 0.9,
    "executability": 0.8,
    "completeness": 0.7,
    "maintainability": 0.6,
    "cost_awareness": 0.5,
    "weight_safety": 2.0,
    "weight_executability": 2.0,
    "weight_completeness": 1.0,
    "weight_maintainability": 1.0,
    "weight_cost_awareness": 1.0,
}


def _write(path: Path, skills: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps({"skills": skills}), encoding="utf-8")
    return path


async def test_reads_active_skills_from_a_json_artifact(tmp_path: Path) -> None:
    """A complete entry becomes a ``Skill`` with the weighted score."""
    store = _write(tmp_path / "skills.json", [dict(_ENTRY)])

    skills = await JsonSkillRegistryReader(store).load_active()

    assert [skill.id for skill in skills] == ["skill:react-lookup:1.0.0"]
    assert skills[0].tool_names == ("find_entity",)
    assert skills[0].quality_score == 5.2 / 7


async def test_missing_artifact_is_an_empty_snapshot(tmp_path: Path) -> None:
    """No artifact yet is not an error: the gate simply has nothing to expose."""
    reader = JsonSkillRegistryReader(tmp_path / "absent.json")

    assert await reader.load_active() == ()


async def test_incomplete_entry_is_dropped_fail_closed(tmp_path: Path) -> None:
    """REQ-SK-01: an entry missing a score never reaches the gate."""
    incomplete = {key: value for key, value in _ENTRY.items() if key != "completeness"}
    store = _write(tmp_path / "skills.json", [incomplete, dict(_ENTRY)])

    skills = await JsonSkillRegistryReader(store).load_active()

    assert [skill.id for skill in skills] == ["skill:react-lookup:1.0.0"]


async def test_entry_missing_a_weight_is_dropped_fail_closed(tmp_path: Path) -> None:
    """REQ-SK-01 names the weights too: a node must carry all five."""
    incomplete = {key: value for key, value in _ENTRY.items() if key != "weight_safety"}
    store = _write(tmp_path / "skills.json", [incomplete])

    assert await JsonSkillRegistryReader(store).load_active() == ()


async def test_inactive_entry_is_not_returned(tmp_path: Path) -> None:
    """The mirror filters by status exactly like the Neo4j read does."""
    retired = dict(_ENTRY, id="skill:retired:1.0.0", status="retired")
    store = _write(tmp_path / "skills.json", [retired, dict(_ENTRY)])

    skills = await JsonSkillRegistryReader(store).load_active()

    assert [skill.id for skill in skills] == ["skill:react-lookup:1.0.0"]


async def test_unknown_tool_in_the_artifact_is_rejected(tmp_path: Path) -> None:
    """An unregistered tool cannot travel inside a skill, mirror or not (REQ-SK-05)."""
    store = _write(tmp_path / "skills.json", [dict(_ENTRY, tool_names=["not_a_registered_tool"])])

    reader = JsonSkillRegistryReader(store)
    skills = await reader.load_active()

    assert skills == (), "the mirror must not hand an unregistered tool to the gate"
