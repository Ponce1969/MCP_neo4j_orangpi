"""Bounded SQLite store for routing cache and telemetry.

This adapter is separate from the MCP ``QueryLoggerPort``. Raw question and
answer columns are disabled by default (``enable_raw=False``) and are never
persisted when the opt-in flag is off, so the metadata-only contract of the
MCP query log is preserved.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from book_graph_rag.ports.routing_telemetry_port import (
    CacheEntry,
    RoutingEvent,
    RoutingTelemetryPort,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS routing_cache (
    key TEXT PRIMARY KEY,
    question_fingerprint TEXT NOT NULL,
    predicted_namespace TEXT,
    top_score REAL NOT NULL,
    confidence REAL NOT NULL,
    model_id TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    graph_snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS routing_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_fingerprint TEXT NOT NULL,
    predicted_namespace TEXT,
    corrected_namespace TEXT,
    score REAL NOT NULL,
    margin REAL NOT NULL,
    route_kind TEXT NOT NULL,
    fallback_used INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    graph_snapshot TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    answer_reference TEXT,
    evidence_ids TEXT NOT NULL,
    correction TEXT,
    raw_question TEXT,
    raw_answer TEXT,
    created_at TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SqliteRoutingTelemetryAdapter(RoutingTelemetryPort):
    """Async facade over a local SQLite database."""

    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_ms: int = 3000,
        retention_days: int = 90,
        enable_raw: bool = False,
    ) -> None:
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._retention_days = retention_days
        self._enable_raw = enable_raw
        self._lock = threading.Lock()
        self._conn: Any | None = None
        self._closed = False

    async def open(self) -> None:
        """Create the schema, WAL mode, and retention purge (concurrent-safe)."""
        await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Serialized by ``self._lock``; check_same_thread=False lets the
            # asyncio executor pool share one connection safely.
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            with self._conn:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
                self._conn.executescript(_SCHEMA)
            self._purge_sync(datetime.now(UTC))

    async def put_cache(self, entry: CacheEntry) -> None:
        """Upsert ``entry`` by its bound key."""
        await asyncio.to_thread(self._put_cache_sync, entry)

    def _put_cache_sync(self, entry: CacheEntry) -> None:
        with self._lock:
            assert self._conn is not None
            with self._conn:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO routing_cache (
                        key, question_fingerprint, predicted_namespace,
                        top_score, confidence, model_id, profile_version,
                        catalog_version, graph_snapshot, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.key,
                        entry.question_fingerprint,
                        entry.predicted_namespace,
                        entry.top_score,
                        entry.confidence,
                        entry.model_id,
                        entry.profile_version,
                        entry.catalog_version,
                        entry.graph_snapshot,
                        entry.created_at.isoformat(),
                        entry.expires_at.isoformat() if entry.expires_at else None,
                    ),
                )

    async def get_cache(self, key: str) -> CacheEntry | None:
        """Return the cached entry, deleting and ignoring expired ones."""
        return await asyncio.to_thread(self._get_cache_sync, key)

    def _get_cache_sync(self, key: str) -> CacheEntry | None:
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM routing_cache WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            expires_at = row["expires_at"]
            if expires_at is not None:
                expires = datetime.fromisoformat(expires_at)
                if expires <= datetime.now(UTC):
                    with self._conn:
                        self._conn.execute(
                            "DELETE FROM routing_cache WHERE key = ?",
                            (key,),
                        )
                    return None
            return CacheEntry(
                key=row["key"],
                question_fingerprint=row["question_fingerprint"],
                predicted_namespace=row["predicted_namespace"],
                top_score=float(row["top_score"]),
                confidence=float(row["confidence"]),
                model_id=row["model_id"],
                profile_version=row["profile_version"],
                catalog_version=row["catalog_version"],
                graph_snapshot=row["graph_snapshot"],
                created_at=datetime.fromisoformat(row["created_at"]),
                expires_at=expires_at,
            )

    async def record_event(self, event: RoutingEvent) -> None:
        """Append ``event``; raw fields are gated by the opt-in flag."""
        await asyncio.to_thread(self._record_event_sync, event)

    def _record_event_sync(self, event: RoutingEvent) -> None:
        raw_question = event.raw_question if self._enable_raw else None
        raw_answer = event.raw_answer if self._enable_raw else None
        with self._lock:
            assert self._conn is not None
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO routing_events (
                        query_fingerprint, predicted_namespace, corrected_namespace,
                        score, margin, route_kind, fallback_used, model_id,
                        profile_version, catalog_version, graph_snapshot,
                        latency_ms, answer_reference, evidence_ids, correction,
                        raw_question, raw_answer, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.query_fingerprint,
                        event.predicted_namespace,
                        event.corrected_namespace,
                        event.score,
                        event.margin,
                        event.route_kind,
                        int(event.fallback_used),
                        event.model_id,
                        event.profile_version,
                        event.catalog_version,
                        event.graph_snapshot,
                        event.latency_ms,
                        event.answer_reference,
                        json.dumps(event.evidence_ids, separators=(",", ":")),
                        event.correction,
                        raw_question,
                        raw_answer,
                        event.created_at.isoformat(),
                    ),
                )

    async def list_events(
        self,
        *,
        limit: int = 100,
        golden_eligible_only: bool = False,
    ) -> tuple[RoutingEvent, ...]:
        """Return recent events, optionally only golden-eligible ones."""
        return await asyncio.to_thread(
            self._list_events_sync,
            limit,
            golden_eligible_only,
        )

    def _list_events_sync(
        self,
        limit: int,
        golden_eligible_only: bool,
    ) -> tuple[RoutingEvent, ...]:
        with self._lock:
            assert self._conn is not None
            query = (
                "SELECT * FROM routing_events"
                + (" WHERE correction IS NOT NULL" if golden_eligible_only else "")
                + " ORDER BY id DESC LIMIT ?"
            )
            rows = self._conn.execute(query, (int(limit),)).fetchall()
            events: list[RoutingEvent] = []
            for row in rows:
                events.append(
                    RoutingEvent(
                        query_fingerprint=row["query_fingerprint"],
                        predicted_namespace=row["predicted_namespace"],
                        corrected_namespace=row["corrected_namespace"],
                        score=float(row["score"]),
                        margin=float(row["margin"]),
                        route_kind=row["route_kind"],
                        fallback_used=bool(row["fallback_used"]),
                        model_id=row["model_id"],
                        profile_version=row["profile_version"],
                        catalog_version=row["catalog_version"],
                        graph_snapshot=row["graph_snapshot"],
                        latency_ms=float(row["latency_ms"]),
                        answer_reference=row["answer_reference"],
                        evidence_ids=tuple(json.loads(row["evidence_ids"])),
                        correction=row["correction"],
                        raw_question=row["raw_question"],
                        raw_answer=row["raw_answer"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                    )
                )
            return tuple(events)

    def _purge_sync(self, cutoff: datetime) -> None:
        if self._retention_days < 0:
            return
        assert self._conn is not None
        with self._conn:
            self._conn.execute(
                "DELETE FROM routing_events WHERE created_at < ?",
                ((cutoff - timedelta(days=self._retention_days)).isoformat(),),
            )

    async def close(self) -> None:
        """Close the underlying connection if open."""
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._conn is not None:
                self._conn.close()
                self._conn = None
