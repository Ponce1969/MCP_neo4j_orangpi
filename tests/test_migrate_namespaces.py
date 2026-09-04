"""Tests for scripts/migrate_namespaces.py.

Covers the pure id-mapping / plan logic and the Neo4j write path (in-place id
rewrite, one atomic transaction per node) using a fake driver — no live Neo4j
connection.
"""

from __future__ import annotations

from typing import Any

from scripts import migrate_namespaces as migrate

from book_graph_rag.domain.models import CommunitySummary, Entity
from book_graph_rag.domain.namespaces import SourceNamespace


def _namespace() -> SourceNamespace:
    return SourceNamespace(corpus="knowledge", source="agentic-architectural-patterns")


def _entity(entity_id: str, name: str, entity_type: str = "concept") -> Entity:
    return Entity(
        id=entity_id,
        name=name,
        type=entity_type,  # type: ignore[arg-type]
        description="",
        source_page=None,
        aliases=[],
        canonical_name=None,
    )


def _summary(level: int, entity_ids: list[str], parent_id: str | None = None) -> CommunitySummary:
    return CommunitySummary(
        level=level,
        summary=f"summary level {level}",
        entity_ids=entity_ids,
        parent_id=parent_id,
    )


# ── map_entity_id ───────────────────────────────────────────────────────────────


def test_map_entity_id_builds_namespaced_id() -> None:
    """A global ``slug-type`` id becomes ``corpus:source:slug-type``."""
    mapping = migrate.map_entity_id(_namespace(), "agent-concept", "concept")
    assert mapping.status == "migrate"
    assert mapping.new_id == "knowledge:agentic-architectural-patterns:agent-concept"
    assert mapping.slug == "agent"


def test_map_entity_id_preserves_hyphenated_slug() -> None:
    """A slug that already contains hyphens is not split by the id builder."""
    mapping = migrate.map_entity_id(_namespace(), "multi-agent-system-concept", "concept")
    assert mapping.status == "migrate"
    assert mapping.slug == "multi-agent-system"
    assert mapping.new_id == "knowledge:agentic-architectural-patterns:multi-agent-system-concept"


def test_map_entity_id_round_trips_through_parse() -> None:
    """The namespaced id parses back to the namespace, slug, and type."""
    mapping = migrate.map_entity_id(_namespace(), "agent-concept", "concept")
    assert mapping.new_id is not None
    parsed_ns, slug, entity_type = SourceNamespace.parse_entity_id(mapping.new_id)
    assert parsed_ns == _namespace()
    assert slug == "agent"
    assert entity_type == "concept"


def test_map_entity_id_already_namespaced_is_noop() -> None:
    """An id that is already in the target namespace is left untouched."""
    mapping = migrate.map_entity_id(
        _namespace(), "knowledge:agentic-architectural-patterns:agent-concept", "concept"
    )
    assert mapping.status == "already-namespaced"
    assert mapping.new_id is None


def test_map_entity_id_wrong_namespace_is_unresolved() -> None:
    """An id already namespaced under a different source must not be guessed."""
    mapping = migrate.map_entity_id(_namespace(), "other:source:agent-concept", "concept")
    assert mapping.status == "unresolved"


def test_map_entity_id_suffix_mismatch_is_unresolved() -> None:
    """A global id whose suffix does not match the stored type is unresolved."""
    mapping = migrate.map_entity_id(_namespace(), "agent-tool", "concept")
    assert mapping.status == "unresolved"


def test_map_entity_id_empty_slug_is_unresolved() -> None:
    """A global id that collapses to an empty slug is unresolved."""
    mapping = migrate.map_entity_id(_namespace(), "-concept", "concept")
    assert mapping.status == "unresolved"


# ── map_book_id ─────────────────────────────────────────────────────────────────


def test_map_book_id_builds_namespaced_id() -> None:
    """Any global book id maps to the single namespaced ``corpus:source`` id."""
    mapping = migrate.map_book_id(_namespace(), "agentic-architectural-patterns")
    assert mapping.status == "migrate"
    assert mapping.new_id == "knowledge:agentic-architectural-patterns"


def test_map_book_id_already_namespaced_is_noop() -> None:
    mapping = migrate.map_book_id(_namespace(), "knowledge:agentic-architectural-patterns")
    assert mapping.status == "already-namespaced"
    assert mapping.new_id is None


def test_map_book_id_different_namespace_is_unresolved() -> None:
    mapping = migrate.map_book_id(_namespace(), "other:source")
    assert mapping.status == "unresolved"


# ── fold_aliases ────────────────────────────────────────────────────────────────


def test_fold_aliases_appends_old_id() -> None:
    """The old canonical id is folded into the alias list for full-text lookups."""
    assert migrate.fold_aliases("langgraph-framework", ["LG"]) == [
        "LG",
        "langgraph-framework",
    ]


def test_fold_aliases_dedupes_case_insensitive() -> None:
    assert migrate.fold_aliases("LangGraph-Framework", ["langgraph-framework"]) == [
        "langgraph-framework"
    ]


# ── build_migration_plan ────────────────────────────────────────────────────────


def test_build_migration_plan_maps_entities_and_folds_aliases() -> None:
    """Migrated entities get a new id and the old id folded into aliases."""
    entities = [
        _entity("agent-concept", "Agent", "concept"),
        _entity("multi-agent-system-pattern", "Multi-Agent System", "pattern"),
    ]
    plan = migrate.build_migration_plan(_namespace(), entities, [], [], [], set())

    assert len(plan.entities) == 2
    assert plan.review == []
    by_old_id = {e.old_id: e for e in plan.entities}
    assert (
        by_old_id["agent-concept"].new_id
        == "knowledge:agentic-architectural-patterns:agent-concept"
    )
    assert "agent-concept" in by_old_id["agent-concept"].aliases


def test_build_migration_plan_maps_books_and_chunks() -> None:
    """A single global book id migrates both the Book node and Chunk.book_id."""
    plan = migrate.build_migration_plan(
        _namespace(),
        [],
        ["agentic-architectural-patterns"],
        [(1, "agentic-architectural-patterns"), (2, "agentic-architectural-patterns")],
        [],
        set(),
    )

    assert len(plan.books) == 1
    assert plan.books[0].new_id == "knowledge:agentic-architectural-patterns"
    assert {c.chunk_index for c in plan.chunks} == {1, 2}
    assert all(c.new_book_id == "knowledge:agentic-architectural-patterns" for c in plan.chunks)


def test_build_migration_plan_routes_multiple_book_ids_to_review() -> None:
    """Two distinct global book ids cannot be merged automatically."""
    plan = migrate.build_migration_plan(_namespace(), [], ["slug-a", "slug-b"], [], [], set())

    assert plan.books == []
    assert len(plan.review) == 2
    assert {i.identifier for i in plan.review} == {"slug-a", "slug-b"}
    assert all(i.kind == "book" for i in plan.review)


def test_build_migration_plan_routes_unresolved_entity_to_review() -> None:
    """An entity whose id cannot be parsed is never silently dropped."""
    plan = migrate.build_migration_plan(
        _namespace(), [_entity("agent-tool", "Agent", "concept")], [], [], [], set()
    )

    assert plan.entities == []
    assert any(i.kind == "entity" and i.identifier == "agent-tool" for i in plan.review)


def test_build_migration_plan_routes_dangling_relationship_endpoint_to_review() -> None:
    """A relationship endpoint with no matching entity node is flagged."""
    plan = migrate.build_migration_plan(
        _namespace(), [_entity("agent-concept", "Agent", "concept")], [], [], [], {"missing-id"}
    )

    assert any(
        i.kind == "relationship-endpoint" and i.identifier == "missing-id" for i in plan.review
    )


def test_build_migration_plan_maps_summary_entity_ids_and_remaps_parent() -> None:
    """Community summary entity ids are namespaced and parent ids re-linked."""
    entities = [
        _entity("agent-concept", "Agent", "concept"),
        _entity("multi-agent-system-pattern", "Multi-Agent System", "pattern"),
    ]
    level0 = _summary(0, ["agent-concept", "multi-agent-system-pattern"])
    level1 = _summary(1, ["agent-concept"], parent_id=level0.id)
    plan = migrate.build_migration_plan(_namespace(), entities, [], [], [level0, level1], set())

    assert len(plan.summaries) == 2
    by_old_id = {s.old_id: s for s in plan.summaries}
    level0_plan = by_old_id[level0.id]
    level1_plan = by_old_id[level1.id]
    assert level0_plan.entity_ids == [
        "knowledge:agentic-architectural-patterns:agent-concept",
        "knowledge:agentic-architectural-patterns:multi-agent-system-pattern",
    ]
    assert level0_plan.new_id != level0.id
    assert level1_plan.parent_id == level0_plan.new_id


def test_build_migration_plan_routes_summary_with_unknown_entity_to_review() -> None:
    """A summary referencing a missing entity id is routed to review, not dropped."""
    summary = _summary(0, ["missing-entity"])
    plan = migrate.build_migration_plan(_namespace(), [], [], [], [summary], set())

    assert plan.summaries == []
    assert any(i.kind == "summary-entity" and i.identifier == "missing-entity" for i in plan.review)


def test_build_migration_plan_is_idempotent() -> None:
    """Re-running on already-namespaced data is a no-op with no review items."""
    entities = [
        _entity("knowledge:agentic-architectural-patterns:agent-concept", "Agent", "concept")
    ]
    summary = _summary(0, ["knowledge:agentic-architectural-patterns:agent-concept"])
    plan = migrate.build_migration_plan(
        _namespace(),
        entities,
        [_namespace().book_id()],
        [(0, _namespace().book_id())],
        [summary],
        set(),
    )

    assert plan.is_noop is True
    assert plan.review == []


# ── Fake driver ─────────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self._records = records or []

    def __aiter__(self) -> _FakeResult:
        self._iter = iter(self._records)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeTransaction:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session
        self.committed = False
        self.rolled_back = False

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        self._session.calls.append((query, parameters))
        return _FakeResult()

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.transactions: list[_FakeTransaction] = []

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        self.calls.append((query, parameters))
        return _FakeResult()

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


# ── apply_migrations ────────────────────────────────────────────────────────────


async def test_apply_migrations_commits_each_item_in_own_transaction() -> None:
    """Each node rewrite is parameterized and committed in its own transaction."""
    plan = migrate.MigrationPlan(
        entities=[
            migrate.EntityMigration(
                old_id="agent-concept",
                new_id="knowledge:agentic-architectural-patterns:agent-concept",
                aliases=["agent-concept"],
            )
        ],
        books=[],
        chunks=[],
        summaries=[],
        review=[],
    )
    session = _FakeSession()
    driver = _FakeDriver(session)

    await migrate.apply_migrations(driver, plan)

    assert len(session.transactions) == 1
    assert session.transactions[0].committed is True
    assert session.transactions[0].rolled_back is False

    query, params = session.calls[0]
    assert "MATCH (n:Entity {id: $old_id})" in query
    # Parameterized Cypher — the old/new ids are never interpolated into the query.
    assert "agent-concept" not in query
    assert params is not None
    assert params["old_id"] == "agent-concept"
    assert params["new_id"] == "knowledge:agentic-architectural-patterns:agent-concept"
    assert params["aliases"] == ["agent-concept"]


async def test_apply_migrations_issues_all_rewrite_kinds() -> None:
    """Books, chunks, and summaries are each rewritten with their own query."""
    plan = migrate.MigrationPlan(
        entities=[],
        books=[
            migrate.BookMigration(
                old_id="slug", new_id="knowledge:agentic-architectural-patterns"
            )
        ],
        chunks=[
            migrate.ChunkMigration(
                chunk_index=1,
                old_book_id="slug",
                new_book_id="knowledge:agentic-architectural-patterns",
            )
        ],
        summaries=[
            migrate.SummaryMigration(
                old_id="old-summary",
                new_id="new-summary",
                entity_ids=["new-entity"],
                parent_id=None,
            )
        ],
        review=[],
    )
    session = _FakeSession()
    driver = _FakeDriver(session)

    await migrate.apply_migrations(driver, plan)

    queries = [q for q, _ in session.calls]
    assert any("MATCH (b:Book {id: $old_id})" in q for q in queries)
    assert any(
        "MATCH (c:Chunk {chunk_index: $chunk_index, book_id: $old_book_id})" in q
        for q in queries
    )
    assert any("MATCH (c:CommunitySummary {id: $old_id})" in q for q in queries)


async def test_apply_migrations_noop_plan_writes_nothing() -> None:
    plan = migrate.MigrationPlan(entities=[], books=[], chunks=[], summaries=[], review=[])
    session = _FakeSession()
    driver = _FakeDriver(session)

    await migrate.apply_migrations(driver, plan)

    assert session.calls == []
    assert session.transactions == []
