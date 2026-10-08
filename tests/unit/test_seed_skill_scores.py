"""Unit tests for the skill seeding script (skill-quality-gating, Unit 4.1).

The script's job is to turn deterministic inputs into gate-eligible scores, so these tests
pin the four documented rules and the two safety properties that matter: the catalog covers
every registered tool, and a skill with no evidence is gated out rather than trusted.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from click.testing import CliRunner

from book_graph_rag.domain.tool_tier_registry import TOOL_TIERS

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "seed_skill_scores", _ROOT / "scripts/seed_skill_scores.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
_SEED: ModuleType = importlib.util.module_from_spec(_SPEC)
# Register before executing: dataclasses resolves ``cls.__module__`` through sys.modules.
sys.modules["seed_skill_scores"] = _SEED
_SPEC.loader.exec_module(_SEED)

_MIN_QUALITY = 0.60


def _log(tmp_path: Path, records: list[dict[str, object]]) -> Path:
    path = tmp_path / "mcp_queries.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    return path


def _record(tool_name: str, error_code: str | None = None) -> dict[str, object]:
    return {"schema_version": 2, "tool_name": tool_name, "error_code": error_code}


def test_catalog_covers_every_registered_tool() -> None:
    """A tool nobody can reach through a skill would silently disappear from the surface."""
    covered = {name for seed in _SEED.SKILL_CATALOG for name in seed.tool_names}

    assert covered == set(TOOL_TIERS)


def test_the_shipped_top_k_covers_the_shipped_catalog() -> None:
    """The cap must fit the catalog: a smaller one drops tools by score, not by policy.

    This is the regression guard for what the real-chain verification found: with
    `skill_top_k = 3`, `rag-answer` passed the threshold at 0.8429 and still fell outside the
    cap as the fourth skill, taking `search_rag` and `ask_global` out of the surface with it.
    """
    from book_graph_rag.config import Settings

    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        }
    )

    assert len(_SEED.SKILL_CATALOG) <= settings.skill_top_k, (
        "skill_top_k must cover the catalog: a smaller cap silently removes the bound tools "
        "of the lowest-scoring capability"
    )


def test_safety_is_the_minimum_over_the_bound_tiers() -> None:
    """The riskiest bound tool dominates the skill's safety."""
    assert _SEED.safety_for(("find_entity", "count_entities")) == 1.0
    assert _SEED.safety_for(("find_entity", "search_rag")) == 0.7
    assert _SEED.safety_for(("find_entity", "query_cypher")) == 0.4


def test_cost_awareness_tracks_the_real_budgets() -> None:
    """HIGH tier enforces 20 rows against LOW's 200, so it scores higher."""
    assert _SEED.cost_awareness_for(("query_cypher",)) == pytest.approx(0.90)
    assert _SEED.cost_awareness_for(("search_rag",)) == pytest.approx(0.50)
    assert _SEED.cost_awareness_for(("find_entity",)) == pytest.approx(0.00)


def test_executability_is_fail_closed_without_records(tmp_path: Path) -> None:
    """No evidence scores zero: an unproven tool is never assumed healthy."""
    assert _SEED.executability_for("find_entity", {}) == 0.0
    assert _SEED.load_log_stats(tmp_path / "absent.jsonl") == {}


def test_executability_uses_the_success_rate_when_records_exist(tmp_path: Path) -> None:
    """Three calls with one error is two thirds, and errors are read per tool."""
    path = _log(
        tmp_path,
        [
            _record("find_entity"),
            _record("find_entity"),
            _record("find_entity", error_code="query_timeout"),
            _record("count_entities"),
        ],
    )

    stats = _SEED.load_log_stats(path)

    assert stats == {"find_entity": (3, 1), "count_entities": (1, 0)}
    assert _SEED.executability_for("find_entity", stats) == pytest.approx(2 / 3)
    assert _SEED.executability_for("count_entities", stats) == 1.0


def test_malformed_log_lines_are_skipped(tmp_path: Path) -> None:
    """One broken line must not throw away the evidence around it."""
    path = tmp_path / "mcp_queries.jsonl"
    path.write_text(
        "\n".join([json.dumps(_record("find_entity")), "{not json", "", "{}"]),
        encoding="utf-8",
    )

    assert _SEED.load_log_stats(path) == {"find_entity": (1, 0)}


def test_a_skill_with_no_evidence_is_gated_out() -> None:
    """The fail-closed intent, end to end: a fresh skill scores below the threshold."""
    skills = {skill.id: skill for skill in _SEED.build_skills({})}

    assert skills["skill:entity-lookup:v1"].quality_score < _MIN_QUALITY
    assert skills["skill:raw-cypher:v1"].quality_score < _MIN_QUALITY


def test_evidence_backed_skills_clear_the_threshold(tmp_path: Path) -> None:
    """With a clean log the low-tier capabilities become eligible, the HIGH-tier one capped."""
    path = _log(
        tmp_path,
        [_record(name) for name in TOOL_TIERS],
    )
    skills = {skill.id: skill for skill in _SEED.build_skills(_SEED.load_log_stats(path))}

    assert skills["skill:entity-lookup:v1"].quality_score >= _MIN_QUALITY
    assert skills["skill:chunk-search:v1"].quality_score >= _MIN_QUALITY
    # query_cypher is clean too, yet the HIGH tier keeps its safety low on purpose.
    assert skills["skill:raw-cypher:v1"].scores.safety == 0.4


def test_dry_run_prints_the_plan_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dry-run needs no database at all."""
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    log_path = _log(tmp_path, [_record("find_entity")])

    result = CliRunner().invoke(_SEED.main, ["--dry-run", "--query-log", str(log_path)])

    assert result.exit_code == 0
    assert "skill:entity-lookup:v1" in result.output
    assert "dry-run: nothing written" in result.output
    assert "provenance: seed_skill_scores.py" in result.output


def test_apply_without_an_approval_file_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write path is gated exactly like the Phase 2 administrative flags."""
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    log_path = _log(tmp_path, [_record("find_entity")])

    result = CliRunner().invoke(_SEED.main, ["--apply", "--query-log", str(log_path)])

    assert result.exit_code != 0
    assert "--apply requires --approval" in result.output


def test_apply_with_an_approval_file_missing_the_word_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that does not say 'approve' is not an approval."""
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    log_path = _log(tmp_path, [_record("find_entity")])
    approval = tmp_path / "approval.txt"
    approval.write_text("looks fine to me", encoding="utf-8")

    result = CliRunner().invoke(
        _SEED.main, ["--apply", "--approval", str(approval), "--query-log", str(log_path)]
    )

    assert result.exit_code != 0
    assert "must contain the word 'approve'" in result.output


async def test_write_skills_merges_one_node_per_skill_with_provenance() -> None:
    """The write carries every score, the weights, the materialized score and provenance."""

    class _Session:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def run(self, query: str, parameters: dict[str, object]) -> None:
            self.calls.append(parameters)

    session = _Session()
    skills = _SEED.build_skills({})

    written = await _SEED.write_skills(session, skills, "provenance-line")

    assert written == len(skills)
    assert [call["id"] for call in session.calls] == [skill.id for skill in skills]
    first = session.calls[0]
    assert first["provenance"] == "provenance-line"
    assert first["quality_score"] == pytest.approx(skills[0].quality_score)
    assert first["weight_safety"] == 2.0
