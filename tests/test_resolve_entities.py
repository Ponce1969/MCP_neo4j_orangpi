"""Tests for scripts/resolve_entities.py.

Covers the pure clustering/canonicalization logic and the Neo4j write path
(re-point :RELATED/:MENTIONS, delete duplicates) using a fake driver — no live
Neo4j connection.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from scripts import resolve_entities as resolve

from book_graph_rag.domain.models import Entity


def _entity(
    entity_id: str,
    name: str,
    entity_type: str = "framework",
    *,
    description: str = "",
    aliases: list[str] | None = None,
    canonical_name: str | None = None,
    source_page: int | None = None,
) -> Entity:
    return Entity(
        id=entity_id,
        name=name,
        type=entity_type,  # type: ignore[arg-type]
        description=description,
        source_page=source_page,
        aliases=aliases or [],
        canonical_name=canonical_name,
    )


# ── name_similarity ───────────────────────────────────────────────────────────


def test_name_similarity_suffix_variant_is_not_auto_merged() -> None:
    """A generic qualifier suffix is not a strong-enough merge signal.

    'LangGraph framework' is structurally identical to 'agent orchestration'
    (shorter name contained in the longer), so auto-merging it would also
    collapse genuinely distinct concepts. Stay conservative: exact, spacing and
    typo variants merge; suffix/prefix variants are left for a higher-level
    decision.
    """
    assert resolve.name_similarity("LangGraph", "LangGraph framework") < 0.9


def test_name_similarity_spacing_variant_is_high() -> None:
    """'LangGraph' vs 'Lang Graph' must be treated as the same entity."""
    assert resolve.name_similarity("LangGraph", "Lang Graph") >= 0.9


def test_name_similarity_exact_match_is_one() -> None:
    assert resolve.name_similarity("LangGraph", "LangGraph") == 1.0


def test_name_similarity_plural_variant_is_not_auto_merged() -> None:
    """A trailing 's' (plural) is not enough to merge — too close to 'agent1'/'agent2'."""
    assert resolve.name_similarity("LangGraph", "LangGraphs") < 0.9


def test_name_similarity_prefix_within_word_is_low() -> None:
    """'agent' is not a word-boundary prefix of 'agentic' — must stay separate."""
    assert resolve.name_similarity("agent", "agentic") < 0.9


def test_name_similarity_prefix_meaning_change_is_not_merged() -> None:
    """'centralized' vs 'decentralized' are opposite concepts, not duplicates."""
    assert resolve.name_similarity("centralized control", "decentralized control") < 0.9


def test_name_similarity_token_reorder_is_high() -> None:
    """The same multi-token set in a different order is the same concept."""
    assert resolve.name_similarity("Multi-Agent System", "System Multi-Agent") >= 0.9


def test_name_similarity_punctuation_is_preserved() -> None:
    """'C++' and 'C#' are distinct — compaction must not collapse them."""
    assert resolve.name_similarity("C++", "C#") < 0.9


def test_name_similarity_unrelated_is_low() -> None:
    assert resolve.name_similarity("router", "memory") < 0.9


def test_name_similarity_empty_names_are_zero() -> None:
    assert resolve.name_similarity("", "LangGraph") == 0.0
    assert resolve.name_similarity("   ", "") == 0.0


# ── build_merge_plan / pick_canonical ────────────────────────────────────────


def test_build_merge_plan_groups_near_duplicates_within_type() -> None:
    """Near-duplicate names of the same type collapse into one merge group."""
    entities = [
        _entity("a", "LangGraph"),
        _entity("b", "Lang Graph"),
        _entity("c", "Next.js"),
    ]
    groups = resolve.build_merge_plan(entities)

    assert len(groups) == 1
    group = groups[0]
    assert set(group.duplicate_ids) == {"b"}
    assert group.canonical_id == "a"
    assert "Lang Graph" in group.aliases


def test_build_merge_plan_does_not_merge_across_types() -> None:
    """Identical names with different types are distinct nodes — never merged."""
    entities = [
        _entity("a", "LangGraph", "framework"),
        _entity("b", "LangGraph", "concept"),
    ]
    assert resolve.build_merge_plan(entities) == []


def test_build_merge_plan_picks_canonical_with_canonical_name() -> None:
    """The entity that already carries canonical_name wins."""
    entities = [
        _entity("long-id", "Lang Graph"),
        _entity(
            "short-id",
            "LangGraph",
            canonical_name="Lang Graph",
        ),
    ]
    (group,) = resolve.build_merge_plan(entities)
    assert group.canonical_id == "short-id"
    assert group.canonical_name == "Lang Graph"


def test_build_merge_plan_prefers_shortest_name_without_canonical() -> None:
    """Without canonical_name, the shorter (cleaner) name becomes canonical."""
    entities = [
        _entity("long-id", "Lang Graph"),
        _entity("short-id", "LangGraph"),
    ]
    (group,) = resolve.build_merge_plan(entities)
    assert group.canonical_id == "short-id"


def test_build_merge_plan_folds_duplicate_names_into_aliases() -> None:
    """Merged variants stay searchable via the canonical node's alias list."""
    entities = [
        _entity("a", "LangGraph", aliases=["LG"]),
        _entity("b", "Lang Graph", description="spacing variant"),
    ]
    (group,) = resolve.build_merge_plan(entities)

    assert "LG" in group.aliases
    assert "Lang Graph" in group.aliases
    assert "LangGraph" not in group.aliases  # canonical's own name is excluded
    assert group.description == "spacing variant"


def test_pick_canonical_is_deterministic_on_tie() -> None:
    """Equal candidates resolve to the lexicographically smallest id."""
    a = _entity("z-id", "LangGraph")
    b = _entity("a-id", "LangGraph")
    assert resolve.pick_canonical([a, b]).id == "a-id"


# ── Fake driver ───────────────────────────────────────────────────────────────


class _FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


class _FakeResult:
    def __init__(self, records: list[_FakeRecord] | None = None) -> None:
        self._records = records or []

    def __aiter__(self) -> _FakeResult:
        self._iter = iter(self._records)
        return self

    async def __anext__(self) -> _FakeRecord:
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeTransaction:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session
        self.committed = False
        self.rolled_back = False

    async def run(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> _FakeResult:
        self._session.calls.append((query, parameters))
        return _FakeResult([])

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeSession:
    def __init__(self, records: list[_FakeRecord] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.transactions: list[_FakeTransaction] = []
        self._records = records or []

    async def run(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> _FakeResult:
        self.calls.append((query, parameters))
        return _FakeResult(list(self._records))

    async def begin_transaction(self) -> _FakeTransaction:
        tx = _FakeTransaction(self)
        self.transactions.append(tx)
        return tx

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class _FakeDriver:
    def __init__(self, session: _FakeSession | None = None) -> None:
        self._session = session or _FakeSession()

    def session(self) -> _FakeSession:
        return self._session

    async def close(self) -> None:
        pass


def _entity_record(entity_id: str, name: str, entity_type: str = "framework") -> _FakeRecord:
    return _FakeRecord(
        {
            "id": entity_id,
            "name": name,
            "type": entity_type,
            "description": "",
            "source_page": None,
            "aliases": [],
            "canonical_name": None,
        }
    )


# ── load_entities ─────────────────────────────────────────────────────────────


async def test_load_entities_maps_records() -> None:
    session = _FakeSession(
        records=[_entity_record("a", "LangGraph"), _entity_record("b", "Next.js")]
    )
    driver = _FakeDriver(session)

    entities = await resolve.load_entities(driver)

    assert [e.id for e in entities] == ["a", "b"]
    assert [e.name for e in entities] == ["LangGraph", "Next.js"]
    assert len(session.calls) == 1
    assert "MATCH (n:Entity)" in session.calls[0][0]


# ── apply_merges ──────────────────────────────────────────────────────────────


async def test_apply_merges_issues_repoint_and_delete() -> None:
    group = resolve.MergeGroup(
        canonical_id="canon",
        duplicate_ids=["dup1", "dup2"],
        aliases=["LangGraph framework"],
        canonical_name=None,
        description="merged",
    )
    session = _FakeSession()
    driver = _FakeDriver(session)

    await resolve.apply_merges(driver, [group])

    queries = [q for q, _ in session.calls]
    assert len(session.calls) == 5

    assert "SET canonical.aliases = $aliases" in queries[0]
    assert "-[r:RELATED]->(dup:Entity)" in queries[1]  # incoming
    assert "(dup:Entity)-[r:RELATED]->" in queries[2]  # outgoing
    assert "MERGE (c)-[m2:MENTIONS]->(canonical)" in queries[3]
    assert "DETACH DELETE dup" in queries[4]

    delete_params = session.calls[4][1]
    assert delete_params is not None
    assert delete_params["canonical_id"] == "canon"
    assert delete_params["dup_ids"] == ["dup1", "dup2"]

    enrich_params = session.calls[0][1]
    assert enrich_params is not None
    assert enrich_params["aliases"] == ["LangGraph framework"]
    assert enrich_params["description"] == "merged"


async def test_apply_merges_commits_each_group_transaction() -> None:
    group = resolve.MergeGroup(
        canonical_id="canon",
        duplicate_ids=["dup1"],
        aliases=[],
        canonical_name=None,
        description="",
    )
    session = _FakeSession()
    driver = _FakeDriver(session)

    await resolve.apply_merges(driver, [group])

    assert len(session.transactions) == 1
    assert session.transactions[0].committed is True
    assert session.transactions[0].rolled_back is False


async def test_apply_merges_no_groups_is_noop() -> None:
    session = _FakeSession()
    driver = _FakeDriver(session)

    await resolve.apply_merges(driver, [])

    assert session.calls == []


# ── run_resolution ────────────────────────────────────────────────────────────


async def test_run_resolution_dry_run_does_not_write() -> None:
    session = _FakeSession(
        records=[
            _entity_record("a", "LangGraph"),
            _entity_record("b", "Lang Graph"),
        ]
    )
    driver = _FakeDriver(session)

    report = await resolve.run_resolution(driver, dry_run=True)

    assert report.total_entities == 2
    assert report.duplicate_count == 1
    assert report.group_count == 1
    assert report.dry_run is True
    # Only the load query was issued; no writes.
    assert len(session.calls) == 1


async def test_run_resolution_applies_when_not_dry_run() -> None:
    session = _FakeSession(
        records=[
            _entity_record("a", "LangGraph"),
            _entity_record("b", "Lang Graph"),
        ]
    )
    driver = _FakeDriver(session)

    report = await resolve.run_resolution(driver, dry_run=False)

    assert report.duplicate_count == 1
    # 1 load + 5 writes (enrich, incoming, outgoing, mentions, delete).
    assert len(session.calls) == 6


async def test_run_resolution_no_duplicates_issues_only_load() -> None:
    session = _FakeSession(records=[_entity_record("a", "LangGraph")])
    driver = _FakeDriver(session)

    report = await resolve.run_resolution(driver)

    assert report.duplicate_count == 0
    assert report.group_count == 0
    assert len(session.calls) == 1


# ── run_full_pipeline integration ─────────────────────────────────────────────


def test_resolve_entities_runs_after_index_before_communities(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.run_full_pipeline as rfp

    from tests.test_run_full_pipeline import (
        _FakeGraphDatabase,
        _make_fake_llm_adapter,
        _make_fake_neo4j_adapter,
        _make_fake_pdf_adapter,
        _make_fake_settings_class,
        _make_fake_use_case,
        _StatefulFakeSession,
    )

    calls: list[Any] = []
    monkeypatch.setattr(rfp, "Settings", _make_fake_settings_class(calls))
    monkeypatch.setattr(rfp, "PDFAdapter", _make_fake_pdf_adapter(calls))
    monkeypatch.setattr(rfp, "LLMAdapter", _make_fake_llm_adapter(calls))
    monkeypatch.setattr(rfp, "Neo4jCommandAdapter", _make_fake_neo4j_adapter(calls))
    monkeypatch.setattr(rfp, "IndexBookUseCase", _make_fake_use_case(calls))

    class _FakeResolveModule:
        async def run_resolution(
            self, driver: Any, threshold: float = 0.9, dry_run: bool = False
        ) -> SimpleNamespace:
            calls.append(("resolve", threshold, dry_run))
            return SimpleNamespace(
                total_entities=5, duplicate_count=2, group_count=1, dry_run=dry_run
            )

    monkeypatch.setattr(rfp, "resolve_entities", _FakeResolveModule())

    async def _fake_communities(fresh: bool = False) -> None:
        calls.append(("communities", fresh))

    monkeypatch.setattr(rfp, "_run_communities", _fake_communities)
    monkeypatch.setattr(rfp, "AsyncGraphDatabase", _FakeGraphDatabase(_StatefulFakeSession()))
    monkeypatch.setattr(rfp, "_BACKUP_DIR", tmp_path / "backups")

    pdf = tmp_path / "book.pdf"
    pdf.write_text("fake pdf")

    runner = CliRunner()
    result = runner.invoke(
        rfp.cli, [str(pdf), "--resolve-entities", "--with-communities"]
    )

    assert result.exit_code == 0, result.output
    exe_idx = calls.index(("execute", str(pdf)))
    resolve_idx = calls.index(("resolve", 0.9, False))
    comm_idx = calls.index(("communities", True))
    assert exe_idx < resolve_idx < comm_idx
    assert "Entity resolution: merged 2 duplicates into 1 groups." in result.output
