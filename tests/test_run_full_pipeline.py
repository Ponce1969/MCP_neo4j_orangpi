"""Tests for scripts/run_full_pipeline.py."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import scripts.run_full_pipeline as run_full_pipeline
from click.testing import CliRunner

from tests.test_neo4j_command_adapter import (
    _FakeDriver,
    _FakeRecord,
    _FakeResult,
    _FakeSession,
)

# ── Fakes ────────────────────────────────────────────────────────────────────


class _AsyncFakeResult(_FakeResult):
    """Async-iterable result for the stateful backup/restore fake."""

    def __aiter__(self) -> _AsyncFakeResult:
        self._async_iter = iter(self._records)
        return self

    async def __anext__(self) -> _FakeRecord:
        try:
            return next(self._async_iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _make_fake_settings_class(calls: list[Any]) -> type:
    from pydantic import SecretStr

    class FakeSettings:
        def __init__(self) -> None:
            calls.append("settings")
            self.llm_max_concurrency = 2
            self.processing_batch_size = 3
            self.dead_letter_path = Path("data/dead_letter.log")
            self.relationship_orphan_policy = "log_orphan"
            self.neo4j_uri = "bolt://localhost:7687"
            self.neo4j_user = "neo4j"
            self.neo4j_password = SecretStr("secret")

    return FakeSettings


def _make_fake_pdf_adapter(calls: list[Any]) -> type:
    class FakePDFAdapter:
        def __init__(self, settings: object) -> None:
            calls.append("pdf_adapter")

        def extract_chunks(self, pdf_path: str) -> Any:
            from book_graph_rag.domain.models import Book, KnowledgeGraphChunk, PageRef

            calls.append(("extract_chunks", pdf_path))
            for idx in range(2):
                yield KnowledgeGraphChunk(
                    text=f"chunk {idx}",
                    chunk_index=idx,
                    book=Book(id="book", title="Book", pdf_path=pdf_path, page_count=1),
                    page_ref=PageRef(start=1, end=1),
                )

    return FakePDFAdapter


def _make_fake_llm_adapter(calls: list[Any]) -> type:
    class FakeLLMAdapter:
        def __init__(self, settings: object) -> None:
            calls.append("llm_adapter")

    return FakeLLMAdapter


def _make_fake_neo4j_adapter(
    calls: list[Any], counts: dict[str, int | list[int]] | None = None
) -> type:
    _counts: dict[str, int | list[int]] = {"chunks": 2, "entities": 5, "mentions": 3}
    if counts:
        _counts.update(counts)

    class FakeNeo4jCommandAdapter:
        def __init__(self, settings: object) -> None:
            calls.append("neo4j_adapter")
            self._entity_calls = 0

        def _value(self, key: str) -> int:
            value = _counts[key]
            if isinstance(value, list):
                idx = min(self._entity_calls if key == "entities" else 0, len(value) - 1)
                return value[idx]
            return value

        async def count_chunks(self) -> int:
            calls.append("count_chunks")
            return self._value("chunks")

        async def count_entities(self) -> int:
            calls.append("count_entities")
            result = self._value("entities")
            self._entity_calls += 1
            return result

        async def count_mentions(self) -> int:
            calls.append("count_mentions")
            return self._value("mentions")

        async def clear_index(self) -> None:
            calls.append("clear_index")

        async def close(self) -> None:
            calls.append("close")

    return FakeNeo4jCommandAdapter


def _make_fake_use_case(calls: list[Any]) -> type:
    class FakeUseCase:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            calls.append("use_case")

        async def execute(self, pdf_path: str) -> None:
            calls.append(("execute", pdf_path))

    return FakeUseCase


class _StatefulFakeSession(_FakeSession):
    """Extends the shared fake with state tracking for restore idempotency."""

    def __init__(
        self,
        nodes: list[dict[str, Any]] | None = None,
        relationships: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self._node_ids: set[tuple[str, frozenset[tuple[str, Any]]]] = set()
        self._relationships: set[
            tuple[str, frozenset[tuple[str, Any]], frozenset[tuple[str, Any]], str]
        ] = set()
        self._nodes_source = nodes or []
        self._relationships_source = relationships or []

    def _extract_merge(self, query: str) -> tuple[str, dict[str, Any]] | None:
        label_match = re.search(r"\(\s*n\s*:\s*(\w+)\s*\{", query)
        if not label_match:
            return None
        props_match = re.search(r"\{\s*([^}]+)\s*\}", query)
        if not props_match:
            return None
        keys = {
            pair.split(":", 1)[0].strip().lstrip("$"): None
            for pair in props_match.group(1).split(",")
            if pair.strip()
        }
        return label_match.group(1), keys

    def _extract_relationship(
        self, query: str
    ) -> tuple[str, str, dict[str, Any], str, dict[str, Any]] | None:
        if "MERGE (a)-[r:" not in query:
            return None
        rel_match = re.search(r"\[r\s*:\s*(\w+)\s*\]", query)
        if not rel_match:
            return None
        rel_type = rel_match.group(1)
        labels: dict[str, str] = {}
        keys: dict[str, dict[str, Any]] = {}
        for var in ("a", "b"):
            m = re.search(rf"MATCH \({var}\s*:\s*(\w+)\s*\{{([^}}]+)\}}\)", query)
            if not m:
                return None
            labels[var] = m.group(1)
            keys[var] = {
                k.strip().lstrip("$"): None
                for k in m.group(2).split(",")
                if k.strip()
            }
        return rel_type, labels["a"], keys["a"], labels["b"], keys["b"]

    async def run(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> _AsyncFakeResult:
        self.calls.append((query, parameters))

        if "MATCH (n) WHERE n:" in query:
            return _AsyncFakeResult(
                [
                    _FakeRecord(
                        {"labels": n["labels"], "properties": n["properties"]}
                    )
                    for n in self._nodes_source
                ]
            )
        if "MATCH (a)-[r]->(b)" in query:
            return _AsyncFakeResult(
                [
                    _FakeRecord(
                        {
                            "type": r["type"],
                            "properties": r.get("properties", {}),
                            "start_labels": r["start_labels"],
                            "start_props": r["start_props"],
                            "end_labels": r["end_labels"],
                            "end_props": r["end_props"],
                        }
                    )
                    for r in self._relationships_source
                ]
            )
        if query.strip().startswith("MERGE (n:"):
            parsed = self._extract_merge(query)
            if parsed:
                label, keys = parsed
                params = parameters or {}
                key_vals = tuple(
                    sorted((k, params.get(f"${k}", params.get(k))) for k in keys)
                )
                self._node_ids.add((label, frozenset(key_vals)))
            return _AsyncFakeResult()
        if "MERGE (a)-[r:" in query:
            rel_parsed = self._extract_relationship(query)
            if rel_parsed:
                rel_type, start_label, start_keys, end_label, end_keys = rel_parsed
                start_vals = frozenset(
                    (k, parameters.get(f"${k}", parameters.get(k))) for k in start_keys  # type: ignore[union-attr]
                )
                end_vals = frozenset(
                    (k, parameters.get(f"${k}", parameters.get(k))) for k in end_keys  # type: ignore[union-attr]
                )
                self._relationships.add(
                    (rel_type, frozenset(start_vals), frozenset(end_vals), end_label)
                )
            return _AsyncFakeResult()
        if "MATCH (c:CommunitySummary) RETURN count(c)" in query:
            count = parameters.get("count_value", 1) if parameters else 1
            return _AsyncFakeResult([_FakeRecord({"count": count})])

        return _AsyncFakeResult()


class _FakeGraphDatabase:
    def __init__(self, session: _FakeSession | None = None) -> None:
        self.driver_instance = _FakeDriver(session)

    def driver(self, *args: Any, **kwargs: Any) -> _FakeDriver:
        return self.driver_instance


@pytest.fixture
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[Any], Path]:
    calls: list[Any] = []
    monkeypatch.setattr(run_full_pipeline, "Settings", _make_fake_settings_class(calls))
    monkeypatch.setattr(run_full_pipeline, "PDFAdapter", _make_fake_pdf_adapter(calls))
    monkeypatch.setattr(run_full_pipeline, "LLMAdapter", _make_fake_llm_adapter(calls))
    monkeypatch.setattr(
        run_full_pipeline, "Neo4jCommandAdapter", _make_fake_neo4j_adapter(calls)
    )
    monkeypatch.setattr(run_full_pipeline, "IndexBookUseCase", _make_fake_use_case(calls))
    monkeypatch.setattr(
        run_full_pipeline,
        "_run_communities",
        lambda fresh=False: calls.append(("communities", fresh)),
    )
    monkeypatch.setattr(
        run_full_pipeline,
        "AsyncGraphDatabase",
        _FakeGraphDatabase(_StatefulFakeSession()),
    )
    monkeypatch.setattr(run_full_pipeline, "_BACKUP_DIR", tmp_path / "backups")

    pdf = tmp_path / "book.pdf"
    pdf.write_text("fake pdf")
    return calls, pdf


# ── RED tests for CLI wiring (tasks 3.1, 3.2) ────────────────────────────────


def test_full_pipeline_help_shows_flags_and_restore() -> None:
    """AC-IP.2: help shows pipeline flags and restore option."""
    runner = CliRunner()
    result = runner.invoke(run_full_pipeline.cli, ["--help"])

    assert result.exit_code == 0
    assert "--dry-run" in result.output
    assert "--fresh" in result.output
    assert "--with-communities" in result.output
    assert "--restore" in result.output


def test_default_run_indexes_and_verifies(fake_env: tuple[list[Any], Path]) -> None:
    """AC-IP.1/2: default run wires use case, captures pre-count, verifies post-counts."""
    calls, pdf = fake_env
    runner = CliRunner()
    result = runner.invoke(run_full_pipeline.cli, [str(pdf)])

    assert result.exit_code == 0, result.output
    assert "settings" in calls
    assert "pdf_adapter" in calls
    assert "llm_adapter" in calls
    assert "neo4j_adapter" in calls
    assert "use_case" in calls
    assert ("execute", str(pdf)) in calls
    assert "count_entities" in calls
    pre_idx = calls.index("count_entities")
    exe_idx = calls.index(("execute", str(pdf)))
    assert pre_idx < exe_idx, "pre_entity_count must be captured before indexing"
    assert "[done] chunks=2 entities=5 mentions=3" in result.output


# ── RED tests for --dry-run (task 3.8) ───────────────────────────────────────


def test_dry_run_skips_writes_and_pre_counts(fake_env: tuple[list[Any], Path]) -> None:
    """AC-IP.3: --dry-run previews chunk count without any writes or pre-count."""
    calls, pdf = fake_env
    runner = CliRunner()
    result = runner.invoke(run_full_pipeline.cli, [str(pdf), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert ("execute", str(pdf)) not in calls
    assert "clear_index" not in calls
    assert "count_entities" not in calls, "dry-run must skip pre_entity_count capture"
    assert "pdf_chunks=2" in result.output


# ── RED tests for --fresh backup/clear (tasks 3.4, 3.6, 3.9) ──────────────────


def test_fresh_creates_backup_then_clears(fake_env: tuple[list[Any], Path]) -> None:
    """AC-IP.4/5: --fresh dumps a JSON backup before clearing."""
    calls, pdf = fake_env
    runner = CliRunner()
    result = runner.invoke(run_full_pipeline.cli, [str(pdf), "--fresh"])

    assert result.exit_code == 0, result.output
    assert "clear_index" in calls
    assert ("execute", str(pdf)) in calls
    clear_idx = calls.index("clear_index")
    exe_idx = calls.index(("execute", str(pdf)))
    assert clear_idx < exe_idx


def test_fresh_backup_failure_aborts_before_clear(
    fake_env: tuple[list[Any], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-IP.5: backup failure exits 2 before any clear."""
    calls, pdf = fake_env

    def _exploding_backup(*args: Any, **kwargs: Any) -> Path:
        raise RuntimeError("disk full")

    monkeypatch.setattr(run_full_pipeline, "_backup", _exploding_backup)

    runner = CliRunner()
    result = runner.invoke(run_full_pipeline.cli, [str(pdf), "--fresh"])

    assert result.exit_code == 2, result.output
    assert "clear_index" not in calls
    assert "disk full" in result.output


# ── RED tests for --with-communities (task 3.10) ─────────────────────────────


def test_with_communities_runs_communities_fresh(
    fake_env: tuple[list[Any], Path]
) -> None:
    """AC-IP.6: --with-communities triggers community regeneration with fresh=True."""
    calls, pdf = fake_env
    runner = CliRunner()
    result = runner.invoke(run_full_pipeline.cli, [str(pdf), "--with-communities"])

    assert result.exit_code == 0, result.output
    assert ("communities", True) in calls
    assert calls.count(("communities", True)) == 1


# ── RED tests for verification warnings (tasks 3.3, 3.12) ─────────────────────


def test_chunk_drift_warning_emitted(fake_env: tuple[list[Any], Path]) -> None:
    """AC-PV.3: chunk count outside tolerance emits a warning but exits 0."""
    calls, pdf = fake_env
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        run_full_pipeline,
        "Neo4jCommandAdapter",
        _make_fake_neo4j_adapter(calls, {"chunks": 100, "entities": 5, "mentions": 3}),
    )
    runner = CliRunner()
    result = runner.invoke(run_full_pipeline.cli, [str(pdf)])
    monkeypatch.undo()

    assert result.exit_code == 0, result.output
    assert "chunk count drift" in result.output.lower()


def test_entity_decrease_warning_emitted(fake_env: tuple[list[Any], Path]) -> None:
    """AC-PV.4: post-entity < pre-entity emits a warning in default MERGE path."""
    calls, pdf = fake_env
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        run_full_pipeline,
        "Neo4jCommandAdapter",
        _make_fake_neo4j_adapter(calls, {"chunks": 2, "entities": [5, 2], "mentions": 3}),
    )
    runner = CliRunner()
    result = runner.invoke(run_full_pipeline.cli, [str(pdf)])
    monkeypatch.undo()

    assert result.exit_code == 0, result.output
    assert "entity count decreased" in result.output.lower()


# ── RED test for stale-summary warning (task 3.13) ───────────────────────────


def test_omitting_communities_warns_stale_summaries(
    fake_env: tuple[list[Any], Path]
) -> None:
    """AC-IP.6: omitting --with-communities warns that summaries may be stale."""
    calls, pdf = fake_env
    runner = CliRunner()
    result = runner.invoke(run_full_pipeline.cli, [str(pdf)])

    assert result.exit_code == 0, result.output
    assert "stale" in result.output.lower()


# ── RED tests for backup/restore helpers (tasks 3.4, 3.5, 3.11) ───────────────


async def test_backup_writes_json_dump(tmp_path: Path) -> None:
    """Backup produces a JSON file with the required schema."""
    session = _StatefulFakeSession(
        nodes=[{"labels": ["Entity"], "properties": {"id": "e1", "name": "Agent"}}],
        relationships=[
            {
                "type": "MENTIONS",
                "start_labels": ["Chunk"],
                "start_props": {"chunk_index": 0, "book_id": "book"},
                "end_labels": ["Entity"],
                "end_props": {"id": "e1"},
            }
        ],
    )
    driver = _FakeDriver(session)

    path = await run_full_pipeline._backup(driver, tmp_path / "dump.json")

    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "captured_at_utc" in data
    assert len(data["nodes"]) == 1
    assert len(data["relationships"]) == 1
    assert data["relationships"][0]["type"] == "MENTIONS"


async def test_restore_replays_json_dump_idempotently(tmp_path: Path) -> None:
    """Restore replays a backup; second run keeps the same node/edge count."""
    backup = {
        "captured_at_utc": "2026-01-01T00:00:00Z",
        "nodes": [
            {"labels": ["Entity"], "properties": {"id": "e1", "name": "Agent"}}
        ],
        "relationships": [
            {
                "type": "MENTIONS",
                "start_labels": ["Chunk"],
                "start_props": {"chunk_index": 0, "book_id": "book"},
                "end_labels": ["Entity"],
                "end_props": {"id": "e1"},
            }
        ],
    }
    dump_path = tmp_path / "dump.json"
    dump_path.write_text(json.dumps(backup), encoding="utf-8")

    session = _StatefulFakeSession()
    driver = _FakeDriver(session)
    await run_full_pipeline._restore(driver, dump_path)
    first_nodes = len(session._node_ids)
    first_rels = len(session._relationships)

    await run_full_pipeline._restore(driver, dump_path)

    assert len(session._node_ids) == first_nodes
    assert len(session._relationships) == first_rels


def test_restore_command_invokes_restore(fake_env: tuple[list[Any], Path]) -> None:
    """The --restore option replays a backup file."""
    backup = {
        "captured_at_utc": "2026-01-01T00:00:00Z",
        "nodes": [],
        "relationships": [],
    }
    dump_path = fake_env[1].parent / "dump.json"
    dump_path.write_text(json.dumps(backup), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(run_full_pipeline.cli, ["--restore", str(dump_path)])

    assert result.exit_code == 0, result.output
    assert "restored" in result.output.lower()
