"""Seed the skill quality gate's ``:Skill`` nodes from deterministic inputs (Unit 4.1).

Nothing here calls an LLM and nothing here reads the graph: the five dimensions are
computed from configuration and from the MCP query log, and the write path runs only behind
``--apply`` plus an approval file.

Rules (approved 2026-10-07; see ``openspec/changes/skill-quality-gating/tasks.md``):

- **catalog**: five capabilities covering the eight registered tools. Namespaces are not a
  dimension of a skill: every tool already takes ``source_id``, so a per-namespace split
  would expose exactly the same tool set.
- **safety**: the riskiest bound tool dominates, so it is the minimum over the tools' tier
  scores (LOW 1.0, MEDIUM 0.7, HIGH 0.4). The gate then applies the HIGH-tier ceiling on top.
- **executability**: from the MCP query log — a tool with records and no ``error_code``
  scores 1.0, otherwise successes/total, and a tool with no records scores 0.0 (fail-closed,
  never optimistic). A skill takes the minimum over its tools, so its weakest link decides.
- **completeness**: 1.0 for every seeded skill. The design's "declared capability vs skill
  contract" diff has nothing to compare yet, because ``Skill`` carries no expected-output
  contract; a future delta adds the field and this becomes a real check.
- **maintainability**: 1.0 at seeding (age 0, reviewed through the ``--approval`` gate); a
  future delta adds age/churn decay.
- **cost_awareness**: ``1 - max_rows / loosest_max_rows`` over the bound tools'
  ``ResourcePolicy``, so it tracks the budgets the adapter actually enforces (HIGH 20 rows
  -> 0.90, MEDIUM 100 -> 0.50, LOW 200 -> 0.00).
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from book_graph_rag.config import Settings
from book_graph_rag.domain.mcp_security import ToolRiskTier
from book_graph_rag.domain.skill_models import Skill, SkillQualityScores, SkillQualityWeights
from book_graph_rag.domain.tool_tier_registry import TOOL_TIERS, policy_for_tool, tier_for

_SAFETY_BY_TIER: dict[ToolRiskTier, float] = {
    ToolRiskTier.LOW: 1.0,
    ToolRiskTier.MEDIUM: 0.7,
    ToolRiskTier.HIGH: 0.4,
}

#: One ``:Skill`` node per MERGE, with the score the gate recomputes from the dimensions.
_SEED_SKILL = """
MERGE (s:Skill {id: $id})
SET s.name = $name,
    s.version = $version,
    s.status = 'active',
    s.tool_names = $tool_names,
    s.safety = $safety,
    s.executability = $executability,
    s.completeness = $completeness,
    s.maintainability = $maintainability,
    s.cost_awareness = $cost_awareness,
    s.weight_safety = $weight_safety,
    s.weight_executability = $weight_executability,
    s.weight_completeness = $weight_completeness,
    s.weight_maintainability = $weight_maintainability,
    s.weight_cost_awareness = $weight_cost_awareness,
    s.quality_score = $quality_score,
    s.provenance = $provenance,
    s.updated_at = datetime()
RETURN s.id AS id
"""


@dataclass(frozen=True)
class SkillSeed:
    """One capability of the catalog, before its dimensions are computed."""

    id: str
    name: str
    version: str
    tool_names: tuple[str, ...]


#: The catalog: five capabilities covering all eight registered tools.
SKILL_CATALOG: tuple[SkillSeed, ...] = (
    SkillSeed(
        id="skill:entity-lookup:v1",
        name="Entity lookup",
        version="v1",
        tool_names=("find_entity", "list_entities", "count_entities"),
    ),
    SkillSeed(
        id="skill:relationship-traversal:v1",
        name="Relationship traversal",
        version="v1",
        tool_names=("traverse_relationships",),
    ),
    SkillSeed(
        id="skill:chunk-search:v1",
        name="Chunk search",
        version="v1",
        tool_names=("search_chunks",),
    ),
    SkillSeed(
        id="skill:rag-answer:v1",
        name="RAG answer",
        version="v1",
        tool_names=("search_rag", "ask_global"),
    ),
    SkillSeed(
        id="skill:raw-cypher:v1",
        name="Raw Cypher (high tier)",
        version="v1",
        tool_names=("query_cypher",),
    ),
)


def safety_for(tool_names: tuple[str, ...]) -> float:
    """The riskiest bound tool dominates."""
    return min(_SAFETY_BY_TIER[tier_for(name)] for name in tool_names)


def cost_awareness_for(tool_names: tuple[str, ...]) -> float:
    """Tighter row budgets score higher, normalized against the loosest tier."""
    loosest = max(policy_for_tool(name).max_rows for name in TOOL_TIERS)
    tightest = min(policy_for_tool(name).max_rows for name in tool_names)
    return 1.0 - tightest / loosest


def load_log_stats(path: Path) -> dict[str, tuple[int, int]]:
    """Return ``{tool_name: (calls, errors)}`` from the MCP query log.

    A missing or unreadable log is an empty mapping, which makes every skill score 0.0 on
    executability: no evidence is not the same as a clean record.
    """
    stats: dict[str, list[int]] = {}
    if not path.exists():
        return {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        tool_name = record.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            continue
        entry = stats.setdefault(tool_name, [0, 0])
        entry[0] += 1
        if record.get("error_code"):
            entry[1] += 1
    return {name: (calls, errors) for name, (calls, errors) in stats.items()}


def executability_for(tool_name: str, log_stats: dict[str, tuple[int, int]]) -> float:
    """Fail-closed success rate for one tool: no records means no credit."""
    calls, errors = log_stats.get(tool_name, (0, 0))
    if calls == 0:
        return 0.0
    return 1.0 - errors / calls


def build_skills(log_stats: dict[str, tuple[int, int]]) -> tuple[Skill, ...]:
    """Build the catalog as domain models, with the documented deterministic scores."""
    skills: list[Skill] = []
    for seed in SKILL_CATALOG:
        skills.append(
            Skill(
                id=seed.id,
                name=seed.name,
                version=seed.version,
                status="active",
                tool_names=seed.tool_names,
                scores=SkillQualityScores(
                    safety=safety_for(seed.tool_names),
                    executability=min(
                        executability_for(name, log_stats) for name in seed.tool_names
                    ),
                    completeness=1.0,
                    maintainability=1.0,
                    cost_awareness=cost_awareness_for(seed.tool_names),
                ),
                weights=SkillQualityWeights(),
            )
        )
    return tuple(skills)


def provenance_line(query_log: Path, skills: tuple[Skill, ...]) -> str:
    """Ops evidence reference: what produced these scores, when, and from which log."""
    stamp = datetime.now(tz=UTC).isoformat(timespec="seconds")
    return f"seed_skill_scores.py run={stamp} query_log={query_log} skills={len(skills)}"


async def write_skills(session: Any, skills: tuple[Skill, ...], provenance: str) -> int:
    """MERGE one ``:Skill`` node per skill; returns how many were written."""
    for skill in skills:
        await session.run(
            _SEED_SKILL,
            {
                "id": skill.id,
                "name": skill.name,
                "version": skill.version,
                "tool_names": list(skill.tool_names),
                "safety": skill.scores.safety,
                "executability": skill.scores.executability,
                "completeness": skill.scores.completeness,
                "maintainability": skill.scores.maintainability,
                "cost_awareness": skill.scores.cost_awareness,
                "weight_safety": skill.weights.safety,
                "weight_executability": skill.weights.executability,
                "weight_completeness": skill.weights.completeness,
                "weight_maintainability": skill.weights.maintainability,
                "weight_cost_awareness": skill.weights.cost_awareness,
                "quality_score": skill.quality_score,
                "provenance": provenance,
            },
        )
    return len(skills)


def _validate_approval(approval_path: Path | None) -> None:
    """``--apply`` is gated: the approval file must contain the word ``approve``."""
    if approval_path is None:
        raise click.UsageError("--apply requires --approval <file> (AGENTS.md §7.1)")
    if "approve" not in approval_path.read_text(encoding="utf-8").lower():
        raise click.UsageError(f"approval file {approval_path} must contain the word 'approve'")


def _echo_plan(skills: tuple[Skill, ...], provenance: str, *, dry_run: bool) -> None:
    click.echo(
        "skill, tool_names, safety, executability, completeness, maintainability, "
        "cost_awareness, quality_score"
    )
    for skill in skills:
        click.echo(
            f"{skill.id}, {','.join(skill.tool_names)}, {skill.scores.safety:.2f}, "
            f"{skill.scores.executability:.2f}, {skill.scores.completeness:.2f}, "
            f"{skill.scores.maintainability:.2f}, {skill.scores.cost_awareness:.2f}, "
            f"{skill.quality_score:.4f}"
        )
    click.echo(f"provenance: {provenance}")
    click.echo("dry-run: nothing written" if dry_run else "apply: writing the nodes above")


async def _apply(skills: tuple[Skill, ...], provenance: str, settings: Settings) -> int:
    """Write the nodes with the driver this command owns, and close it."""
    from neo4j import AsyncGraphDatabase  # local import: the dry-run never needs a driver

    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        async with driver.session() as session:
            return await write_skills(session, skills, provenance)
    finally:
        await driver.close()


@click.command()
@click.option(
    "--dry-run/--apply",
    default=False,
    show_default=True,
    help="Dry-run prints the computed plan; --apply writes :Skill nodes (needs --approval).",
)
@click.option(
    "--approval",
    "approval_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Approval file for the destructive write; its contents must include 'approve'.",
)
@click.option(
    "--query-log",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="MCP query log used as executability evidence (defaults to MCP_LOG_PATH).",
)
def main(dry_run: bool, approval_path: Path | None, query_log: Path | None) -> None:
    """Seed the skill quality gate's :Skill nodes from deterministic inputs."""
    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    log_path = query_log if query_log is not None else settings.mcp_log_path
    log_stats = load_log_stats(log_path)
    skills = build_skills(log_stats)
    provenance = provenance_line(log_path, skills)

    if dry_run:
        _echo_plan(skills, provenance, dry_run=True)
        return

    _validate_approval(approval_path)
    _echo_plan(skills, provenance, dry_run=False)
    written = asyncio.run(_apply(skills, provenance, settings))
    click.echo(f"wrote {written} :Skill node(s)")


if __name__ == "__main__":
    main()
