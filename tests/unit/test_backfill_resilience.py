"""Tests for scripts/backfill_resilience.py."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from backfill_resilience import (  # noqa: E402
    _BACKFILL_ALIASES_CYPHER,
    _CREATE_FULLTEXT_INDEX_CYPHER,
    _ENTITY_ID_COLLISION_GUARD_CYPHER,
    _LEGACY_ENTITIES_CYPHER,
    _MIGRATE_ENTITY_CYPHER,
    run_backfill,
)


class _FakeResult:
    """Async result that returns a fixed record."""

    def __init__(self, record: dict[str, Any] | None = None) -> None:
        self._record = record

    async def single(self) -> dict[str, Any] | None:
        return self._record

    async def data(self) -> list[dict[str, Any]]:
        return []


class _FakeSession:
    """Records Cypher runs for assertions."""

    def __init__(
        self,
        updated_count: int = 0,
        record: dict[str, Any] | None = None,
    ) -> None:
        self._updated_count = updated_count
        self._record = record
        self.runs: list[str] = []

    async def run(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> _FakeResult:
        self.runs.append(cypher.strip())
        record = self._record if self._record is not None else {"updated": self._updated_count}
        return _FakeResult(record)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: Any, **kwargs: Any) -> None:
        pass


class _FakeDriver:
    """Fake async Neo4j driver that yields configured sessions."""

    def __init__(self, updated_count: int = 0) -> None:
        self.updated_count = updated_count
        self.sessions: list[_FakeSession] = []
        self.closed = False

    def session(self) -> _FakeSession:
        session = _FakeSession(updated_count=self.updated_count)
        self.sessions.append(session)
        return session

    async def close(self) -> None:
        self.closed = True


async def test_run_backfill_is_idempotent() -> None:
    """Second run executes the same Cypher and reports the same counts."""
    driver = _FakeDriver(updated_count=42)

    summary1 = await run_backfill(driver, dry_run=False)
    summary2 = await run_backfill(driver, dry_run=False)

    assert summary1["entities_updated"] == 42
    assert summary2["entities_updated"] == 42
    assert summary1["dry_run"] is False
    assert summary2["dry_run"] is False

    assert len(driver.sessions) == 2
    assert driver.sessions[0].runs == driver.sessions[1].runs
    for session in driver.sessions:
        assert _BACKFILL_ALIASES_CYPHER.strip() in session.runs
        assert _CREATE_FULLTEXT_INDEX_CYPHER.strip() in session.runs


async def test_run_backfill_dry_run_does_not_write() -> None:
    """--dry-run returns zero updates and never calls session.run."""
    driver = _FakeDriver(updated_count=999)

    summary = await run_backfill(driver, dry_run=True)

    assert summary["dry_run"] is True
    assert summary["entities_updated"] == 0
    assert summary["mentions_reconstructible"] is False
    assert len(driver.sessions) == 1
    assert driver.sessions[0].runs == []


async def test_run_backfill_reports_zero_entities_when_no_records() -> None:
    """If the backfill query returns no record, entities_updated is zero."""
    driver = _FakeDriver(updated_count=0)
    driver.sessions = []
    session = _FakeSession(updated_count=0, record=None)

    def fake_session() -> _FakeSession:
        driver.sessions.append(session)
        return session

    driver.session = fake_session  # type: ignore[method-assign]

    summary = await run_backfill(driver, dry_run=False)

    assert summary["entities_updated"] == 0


class _MigrationResult(_FakeResult):
    """Fake result for migration-path records and collision checks."""

    def __init__(
        self,
        records: list[dict[str, Any]] | None = None,
        collision: bool = False,
        updated: int = 0,
        is_guard: bool = False,
    ) -> None:
        super().__init__({"updated": updated})
        self._records = records or []
        self._collision = collision
        self._updated = updated
        self._is_guard = is_guard

    async def data(self) -> list[dict[str, Any]]:
        return self._records

    async def single(self) -> dict[str, Any] | None:
        if self._is_guard:
            return {"collision": self._collision}
        return {"updated": self._updated}


class _MigrationSession(_FakeSession):
    """Fake session that models legacy entities and target-id collisions."""

    def __init__(self, records: list[dict[str, Any]], existing_ids: set[str]) -> None:
        super().__init__()
        self.records = records
        self.existing_ids = existing_ids
        self.migrations: list[dict[str, Any]] = []

    async def run(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> _MigrationResult:
        self.runs.append(cypher.strip())
        if cypher == _LEGACY_ENTITIES_CYPHER:
            return _MigrationResult(records=self.records)
        if cypher == _ENTITY_ID_COLLISION_GUARD_CYPHER:
            assert parameters is not None
            collision = (
                parameters["new_id"] in self.existing_ids
                and parameters["new_id"] != parameters["old_id"]
            )
            return _MigrationResult(collision=collision, is_guard=True)
        if cypher == _MIGRATE_ENTITY_CYPHER:
            assert parameters is not None
            self.migrations.append(parameters)
            return _MigrationResult()
        return _MigrationResult(updated=0)


class _MigrationDriver(_FakeDriver):
    """Fake driver for migration-path tests."""

    def __init__(self, session: _MigrationSession) -> None:
        super().__init__()
        self._migration_session = session

    def session(self) -> _MigrationSession:
        self.sessions.append(self._migration_session)
        return self._migration_session


async def test_run_backfill_skips_collisions_and_migrates_clean_entities(
    capsys: Any,
) -> None:
    """A collision is skipped while an unrelated legacy entity still migrates."""
    records = [
        {"id": "react", "name": "ReAct", "type": "pattern"},
        {"id": "react-pattern", "name": "ReAct", "type": "pattern"},
        {"id": "other", "name": "Other", "type": "pattern"},
    ]
    session = _MigrationSession(records, {"react-pattern"})

    summary = await run_backfill(_MigrationDriver(session), dry_run=False)

    assert summary["entity_id_collisions"] == 1
    assert session.migrations == [{"old_id": "other", "new_id": "other-pattern"}]
    output = capsys.readouterr()
    assert "react -> react-pattern" in output.err


async def test_run_backfill_migrates_clean_entity_and_backfills_aliases() -> None:
    """A free target id executes migration and alias backfill normally."""
    records = [{"id": "react", "name": "ReAct", "type": "pattern"}]
    session = _MigrationSession(records, set())

    summary = await run_backfill(_MigrationDriver(session), dry_run=False)

    assert summary["entity_id_collisions"] == 0
    assert session.migrations == [{"old_id": "react", "new_id": "react-pattern"}]
    assert _BACKFILL_ALIASES_CYPHER.strip() in session.runs


def test_migration_cypher_uses_isolated_call_subqueries() -> None:
    """Parallel :RELATED edges must not crash the migration (regression guard).

    Deleting relationships inside a FOREACH over an OPTIONAL MATCH in the same
    statement re-visits the deleted edge and raises
    ``Neo.ClientError.Statement.EntityNotFound`` when an entity has duplicate
    parallel edges to the same target. Each re-pointing block must run in its
    own ``CALL (old, new) { ... }`` subquery so row scopes stay isolated.
    """
    assert _MIGRATE_ENTITY_CYPHER.count("CALL (old, new) {") == 3
    assert "DETACH DELETE old" in _MIGRATE_ENTITY_CYPHER
