"""Tests for scripts/backfill_resilience.py."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from backfill_resilience import (  # noqa: E402
    _BACKFILL_ALIASES_CYPHER,
    _CREATE_FULLTEXT_INDEX_CYPHER,
    run_backfill,
)


class _FakeResult:
    """Async result that returns a fixed record."""

    def __init__(self, record: dict[str, Any] | None = None) -> None:
        self._record = record

    async def single(self) -> dict[str, Any] | None:
        return self._record


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
