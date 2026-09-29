"""Behavior tests for the SQLite routing telemetry adapter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from book_graph_rag.domain.routing_models import route_cache_key
from book_graph_rag.infrastructure.sqlite_routing_telemetry import (
    SqliteRoutingTelemetryAdapter,
)
from book_graph_rag.ports.routing_telemetry_port import (
    CacheEntry,
    CorrectionLabel,
    RoutingEvent,
)


def _cache_entry(key: str, *, expires_at: datetime | None = None) -> CacheEntry:
    return CacheEntry(
        key=key,
        question_fingerprint="fp-1",
        predicted_namespace="knowledge:book-a",
        top_score=0.9,
        confidence=0.9,
        model_id="m",
        profile_version="1.0.0",
        catalog_version="1",
        graph_snapshot="s",
        expires_at=expires_at,
    )


def _event(
    *,
    query_fingerprint: str = "fp-1",
    correction: CorrectionLabel | None = None,
    raw_question: str | None = None,
    raw_answer: str | None = None,
    evidence_ids: tuple[str, ...] = ("e1", "e2"),
) -> RoutingEvent:
    return RoutingEvent(
        query_fingerprint=query_fingerprint,
        predicted_namespace="knowledge:book-a",
        score=0.9,
        margin=0.3,
        route_kind="single",
        model_id="m",
        profile_version="1.0.0",
        catalog_version="1",
        graph_snapshot="s",
        evidence_ids=evidence_ids,
        correction=correction,
        raw_question=raw_question,
        raw_answer=raw_answer,
    )


async def test_cache_round_trip(tmp_path: Path) -> None:
    adapter = SqliteRoutingTelemetryAdapter(tmp_path / "router.db")
    await adapter.open()

    await adapter.put_cache(_cache_entry("key-1"))
    entry = await adapter.get_cache("key-1")

    assert entry is not None
    assert entry.predicted_namespace == "knowledge:book-a"
    await adapter.close()


async def test_cache_miss_returns_none(tmp_path: Path) -> None:
    adapter = SqliteRoutingTelemetryAdapter(tmp_path / "router.db")
    await adapter.open()

    assert await adapter.get_cache("missing") is None
    await adapter.close()


async def test_cache_expired_is_deleted(tmp_path: Path) -> None:
    adapter = SqliteRoutingTelemetryAdapter(tmp_path / "router.db")
    await adapter.open()
    past = datetime.now(UTC) - timedelta(seconds=1)

    await adapter.put_cache(_cache_entry("key-1", expires_at=past))
    entry = await adapter.get_cache("key-1")

    assert entry is None
    await adapter.close()


async def test_future_expiry_keeps_entry(tmp_path: Path) -> None:
    adapter = SqliteRoutingTelemetryAdapter(tmp_path / "router.db")
    await adapter.open()
    future = datetime.now(UTC) + timedelta(hours=1)

    await adapter.put_cache(_cache_entry("key-1", expires_at=future))
    entry = await adapter.get_cache("key-1")

    assert entry is not None
    await adapter.close()


async def test_event_round_trip_and_correction_golden_gate(tmp_path: Path) -> None:
    adapter = SqliteRoutingTelemetryAdapter(tmp_path / "router.db")
    await adapter.open()

    await adapter.record_event(_event())
    await adapter.record_event(_event(correction="correct"))

    all_events = await adapter.list_events()
    golden = await adapter.list_events(golden_eligible_only=True)

    assert len(all_events) == 2
    assert len(golden) == 1
    assert golden[0].correction == "correct"
    assert golden[0].is_golden_eligible()
    await adapter.close()


async def test_unlabeled_event_is_not_golden_eligible(tmp_path: Path) -> None:
    event = _event()

    assert not event.is_golden_eligible()


async def test_raw_fields_are_gated_off_by_default(tmp_path: Path) -> None:
    adapter = SqliteRoutingTelemetryAdapter(tmp_path / "router.db")
    await adapter.open()

    await adapter.record_event(_event(raw_question="secret question", raw_answer="answer"))

    records = await adapter.list_events()

    assert records[0].raw_question is None
    assert records[0].raw_answer is None
    await adapter.close()


async def test_raw_fields_only_when_opted_in(tmp_path: Path) -> None:
    adapter = SqliteRoutingTelemetryAdapter(
        tmp_path / "router.db",
        enable_raw=True,
    )
    await adapter.open()

    await adapter.record_event(_event(raw_question="question", raw_answer="answer"))

    records = await adapter.list_events()

    assert records[0].raw_question == "question"
    assert records[0].raw_answer == "answer"
    await adapter.close()


async def test_evidence_ids_round_trip(tmp_path: Path) -> None:
    adapter = SqliteRoutingTelemetryAdapter(tmp_path / "router.db")
    await adapter.open()

    await adapter.record_event(_event(evidence_ids=("a", "b", "c")))
    records = await adapter.list_events()

    assert records[0].evidence_ids == ("a", "b", "c")
    await adapter.close()


async def test_retention_purges_old_events_on_reopen(tmp_path: Path) -> None:
    path = tmp_path / "router.db"
    adapter = SqliteRoutingTelemetryAdapter(path)
    await adapter.open()
    await adapter.record_event(_event())
    await adapter.close()

    adapter = SqliteRoutingTelemetryAdapter(path, retention_days=0)
    await adapter.open()

    assert await adapter.list_events() == ()
    await adapter.close()


async def test_concurrent_writes_do_not_corrupt(tmp_path: Path) -> None:
    adapter = SqliteRoutingTelemetryAdapter(tmp_path / "router.db")
    await adapter.open()

    async def _write(index: int) -> None:
        await adapter.record_event(_event(query_fingerprint=f"fp-{index}"))

    await asyncio.gather(*(_write(index) for index in range(20)))

    records = await adapter.list_events(limit=50)
    assert len(records) == 20
    assert len({event.query_fingerprint for event in records}) == 20
    await adapter.close()


async def test_cache_key_derived_outside_and_used(tmp_path: Path) -> None:
    adapter = SqliteRoutingTelemetryAdapter(tmp_path / "router.db")
    await adapter.open()
    key = route_cache_key(
        "fp-1",
        namespace="knowledge:book-a",
        model_id="m",
        profile_version="1.0.0",
        catalog_version="1",
        graph_snapshot="s",
    )

    await adapter.put_cache(_cache_entry(key))
    assert (await adapter.get_cache(key)) is not None
    await adapter.close()
