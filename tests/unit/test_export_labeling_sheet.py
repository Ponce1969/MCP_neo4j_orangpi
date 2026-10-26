"""Behavior tests for the blind extraction labeling-sheet exporter."""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "export_extraction_labeling_sheet",
    _ROOT / "scripts-ops/export_extraction_labeling_sheet.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MODULE: ModuleType = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

Chunk = tuple[str, int, str]
_BOOKS = (
    "knowledge:book-a",
    "knowledge:book-b",
    "knowledge:book-c",
    "knowledge:book-d",
)
_PREDICTION_NAME = "PREDICTION_ONLY_SECRET_ENTITY"


def _chunks(per_book: int = 20) -> list[Chunk]:
    return [
        (book_id, chunk_index, f"Labeling text for {book_id} chunk {chunk_index}.")
        for book_id in _BOOKS
        for chunk_index in range(per_book)
    ]


def _fake_graph_rows() -> list[dict[str, Any]]:
    return [
        {
            "book_id": book_id,
            "chunk_index": chunk_index,
            "text": f"Blind labeling text for {book_id} chunk {chunk_index}.",
            "entities": [{"name": _PREDICTION_NAME}],
        }
        for book_id, chunk_index, _ in _chunks(16)
    ]


class _StubSettings:
    neo4j_read_max_rows = 1000

    @classmethod
    def model_validate(cls, _data: dict[str, Any]) -> _StubSettings:
        return cls()


class _FakeGraph:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    async def execute_read(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        assert parameters is not None
        self.queries.append(query)
        offset = int(parameters["offset"])
        limit = int(parameters["limit"])
        return self.rows[offset : offset + limit]

    async def close(self) -> None:
        return None


def _install_fake_graph(monkeypatch: pytest.MonkeyPatch) -> _FakeGraph:
    graph = _FakeGraph(_fake_graph_rows())
    monkeypatch.setattr(_MODULE, "Settings", _StubSettings, raising=False)
    monkeypatch.setattr(_MODULE, "Neo4jQueryAdapter", lambda _settings: graph, raising=False)
    return graph


def test_selection_is_deterministic() -> None:
    chunks = _chunks()

    first = _MODULE.select_sample(chunks)
    second = _MODULE.select_sample(chunks)

    assert first == second


def test_selection_allocates_exactly_six_chunks_per_book() -> None:
    selected = _MODULE.select_sample(_chunks())

    assert len(selected) == 24
    assert Counter(book_id for book_id, _index, _text in selected) == Counter(
        dict.fromkeys(_BOOKS, 6)
    )


def test_v2_selection_differs_from_the_v1_sample() -> None:
    chunks = _chunks(40)

    version_one = _MODULE.select_sample(chunks, version="extraction-fidelity-v1")
    version_two = _MODULE.select_sample(chunks, version="extraction-fidelity-v2")

    assert _MODULE.SAMPLE_VERSION == "extraction-fidelity-v2"
    assert _MODULE.select_sample(chunks) == version_two
    assert version_one != version_two


def test_selection_order_is_stable_and_independent_of_frame_order() -> None:
    chunks = _chunks()

    selected = _MODULE.select_sample(chunks)
    reversed_selection = _MODULE.select_sample(list(reversed(chunks)))

    assert selected == reversed_selection
    assert selected == sorted(selected, key=lambda chunk: (chunk[0], chunk[1]))


def test_skeleton_matches_protocol_schema_and_has_one_record_per_sampled_chunk() -> None:
    selected = _MODULE.select_sample(_chunks())

    skeleton = _MODULE.render_skeleton(selected)
    records = [json.loads(line) for line in skeleton.splitlines()]

    assert len(records) == 24
    assert all(
        list(record) == ["chunk_id", "book_id", "chunk_index", "entities", "notes", "completed"]
        for record in records
    )
    assert all(
        record
        == {
            "chunk_id": f"{record['book_id']}:{record['chunk_index']}",
            "book_id": record["book_id"],
            "chunk_index": record["chunk_index"],
            "entities": [],
            "notes": "",
            "completed": False,
        }
        for record in records
    )


async def test_blind_sheet_never_queries_or_emits_predictions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    graph = _install_fake_graph(monkeypatch)

    result = await _MODULE.main([])
    output = capsys.readouterr().out

    assert result == 0
    assert len(graph.queries) == 1
    query = graph.queries[0]
    assert ":Chunk" in query
    assert "book_id" in query
    assert "chunk_index" in query
    assert "text" in query
    assert "entity" not in query.casefold()
    assert "mentions" not in query.casefold()
    assert _PREDICTION_NAME not in output
    sampled_chunk = _MODULE.select_sample(
        [(row["book_id"], row["chunk_index"], row["text"]) for row in _fake_graph_rows()]
    )[0]
    assert sampled_chunk[2] in output
    assert "Observed :Chunk census per book:" in output
    assert "Sample pairs (book_id, chunk_index):" in output


async def test_human_sheet_is_identical_on_repeated_runs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_graph(monkeypatch)

    assert await _MODULE.main([]) == 0
    first = capsys.readouterr().out
    assert await _MODULE.main([]) == 0
    second = capsys.readouterr().out

    assert first == second


async def test_emit_skeleton_writes_protocol_jsonl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_graph(monkeypatch)
    destination = tmp_path / "extraction_dataset.jsonl"

    result = await _MODULE.main(["--emit-skeleton", str(destination)])

    records = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
    assert result == 0
    assert len(records) == 24
    assert all(
        set(record) == {"chunk_id", "book_id", "chunk_index", "entities", "notes", "completed"}
        for record in records
    )
    assert all(
        record["entities"] == [] and record["notes"] == "" and not record["completed"]
        for record in records
    )


async def test_text_export_writes_only_sampled_chunk_text_in_sample_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_graph(monkeypatch)
    destination = tmp_path / "chunk_texts.json"

    result = await _MODULE.main(["--texts", str(destination)])

    texts = json.loads(destination.read_text(encoding="utf-8"))
    graph_chunks = [(row["book_id"], row["chunk_index"], row["text"]) for row in _fake_graph_rows()]
    sample = _MODULE.select_sample(graph_chunks)
    expected = {f"{book_id}:{chunk_index}": text for book_id, chunk_index, text in sample}
    assert result == 0
    assert texts == expected
    assert list(texts) == list(expected)
    assert _PREDICTION_NAME not in destination.read_text(encoding="utf-8")


async def test_json_mode_outputs_only_the_deterministic_pair_list(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_graph(monkeypatch)

    result = await _MODULE.main(["--json"])
    pairs = json.loads(capsys.readouterr().out)

    assert result == 0
    assert len(pairs) == 24
    assert all(isinstance(pair, list) and len(pair) == 2 for pair in pairs)
    assert pairs == [
        [book_id, index] for book_id, index, _text in _MODULE.select_sample(_chunks(16))
    ]


async def test_emit_skeleton_writes_sorted_lf_sample_companion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_graph(monkeypatch)
    destination = tmp_path / "extraction_dataset.jsonl"
    companion = destination.with_suffix(".sample.json")

    result = await _MODULE.main(["--emit-skeleton", str(destination)])

    data = companion.read_bytes()
    record = json.loads(data)
    expected_pairs = [
        [book_id, chunk_index]
        for book_id, chunk_index, _text in _MODULE.select_sample(
            [(row["book_id"], row["chunk_index"], row["text"]) for row in _fake_graph_rows()]
        )
    ]
    assert result == 0
    assert b"\r" not in data
    assert record == {"sample_version": _MODULE.SAMPLE_VERSION, "pairs": expected_pairs}
    assert list(record) == ["sample_version", "pairs"]
    assert expected_pairs == sorted(expected_pairs, key=lambda pair: (pair[0], pair[1]))


async def test_emit_skeleton_refuses_to_overwrite_existing_sample_companion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_graph(monkeypatch)
    destination = tmp_path / "extraction_dataset.jsonl"
    companion = destination.with_suffix(".sample.json")
    stale_record = b'{"sample_version":"old-v1","pairs":[]}\n'
    companion.write_bytes(stale_record)

    with pytest.raises(FileExistsError):
        await _MODULE.main(["--emit-skeleton", str(destination)])

    assert companion.read_bytes() == stale_record
