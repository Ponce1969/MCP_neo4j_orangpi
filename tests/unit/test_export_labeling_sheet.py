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


def _install_fake_graph(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]] | None = None
) -> _FakeGraph:
    graph = _FakeGraph(_fake_graph_rows() if rows is None else rows)
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


async def test_single_book_filter_yields_exactly_six_pairs_from_that_book(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_graph(monkeypatch)

    result = await _MODULE.main(["--json", "--book", "knowledge:book-a"])
    captured = capsys.readouterr()
    pairs = json.loads(captured.out)

    assert result == 0
    assert len(pairs) == 6
    assert {pair[0] for pair in pairs} == {"knowledge:book-a"}
    # The operator-facing census and effective selection describe only the
    # filtered frame, not the whole corpus.
    assert "Effective selection: 6 chunks" in captured.err
    assert "knowledge:book-b" not in captured.err
    assert "knowledge:book-c" not in captured.err
    assert "knowledge:book-d" not in captured.err


async def test_two_book_filters_yield_twelve_pairs_six_per_book(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_graph(monkeypatch)

    result = await _MODULE.main(
        ["--json", "--book", "knowledge:book-b", "--book", "knowledge:book-d"]
    )
    pairs = json.loads(capsys.readouterr().out)

    assert result == 0
    assert len(pairs) == 12
    assert Counter(pair[0] for pair in pairs) == Counter(
        {"knowledge:book-b": 6, "knowledge:book-d": 6}
    )


async def test_naming_every_book_is_identical_to_the_unfiltered_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_graph(monkeypatch)

    assert await _MODULE.main(["--json"]) == 0
    unfiltered = capsys.readouterr().out

    filter_args = ["--json"]
    for book_id in _BOOKS:
        filter_args.extend(["--book", book_id])
    assert await _MODULE.main(filter_args) == 0
    filtered = capsys.readouterr().out

    assert filtered == unfiltered


async def test_unknown_requested_book_fails_naming_the_observed_books(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_graph(monkeypatch)

    with pytest.raises(ValueError, match="knowledge:not-a-book") as excinfo:
        await _MODULE.main(["--json", "--book", "knowledge:not-a-book"])

    message = str(excinfo.value)
    positions = [message.index(book_id) for book_id in sorted(_BOOKS)]
    assert positions == sorted(positions), "observed books must be named in sorted order"
    assert "knowledge:not-a-book" in message


async def test_whole_corpus_guard_is_unchanged_without_the_book_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    three_books = [row for row in _fake_graph_rows() if row["book_id"] in _BOOKS[:3]]
    _install_fake_graph(monkeypatch, rows=three_books)

    with pytest.raises(ValueError, match="Expected the 4-book active frame; observed 3 books"):
        await _MODULE.main([])


async def test_filtered_json_texts_and_companion_agree_on_the_same_six_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_graph(monkeypatch)
    book_id = "knowledge:book-c"

    assert await _MODULE.main(["--json", "--book", book_id]) == 0
    pairs = json.loads(capsys.readouterr().out)

    texts_path = tmp_path / "chunk_texts.json"
    assert await _MODULE.main(["--texts", str(texts_path), "--book", book_id]) == 0
    capsys.readouterr()
    texts = json.loads(texts_path.read_text(encoding="utf-8"))

    skeleton_path = tmp_path / "labels.jsonl"
    assert await _MODULE.main(["--emit-skeleton", str(skeleton_path), "--book", book_id]) == 0
    capsys.readouterr()
    companion = json.loads(skeleton_path.with_suffix(".sample.json").read_text(encoding="utf-8"))

    json_ids = [f"{pair[0]}:{pair[1]}" for pair in pairs]
    assert len(json_ids) == 6
    assert json_ids == list(texts)
    assert json_ids == [f"{pair[0]}:{pair[1]}" for pair in companion["pairs"]]


async def test_filtered_companion_records_only_the_filtered_pairs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_graph(monkeypatch)
    destination = tmp_path / "extraction_dataset.jsonl"
    companion = destination.with_suffix(".sample.json")

    result = await _MODULE.main(["--emit-skeleton", str(destination), "--book", "knowledge:book-b"])

    record = json.loads(companion.read_bytes())
    assert result == 0
    assert len(record["pairs"]) == 6
    assert {pair[0] for pair in record["pairs"]} == {"knowledge:book-b"}


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


def _write_label_file(path: Path, chunk_ids: list[str]) -> Path:
    """Write a label JSONL whose rows carry exactly the given chunk ids."""
    path.write_text(
        "".join(
            json.dumps(
                {
                    "chunk_id": chunk_id,
                    "book_id": chunk_id.rsplit(":", 1)[0],
                    "chunk_index": int(chunk_id.rsplit(":", 1)[1]),
                    "entities": [],
                    "notes": "",
                    "completed": True,
                }
            )
            + "\n"
            for chunk_id in chunk_ids
        ),
        encoding="utf-8",
        newline="\n",
    )
    return path


async def test_sample_size_without_a_book_filter_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _install_fake_graph(monkeypatch)

    with pytest.raises(ValueError, match="--sample-size requires") as excinfo:
        await _MODULE.main(["--json", "--sample-size", "8"])

    message = str(excinfo.value)
    assert "--sample-size" in message
    assert "--book" in message
    assert "filter" in message
    # Fail fast: an invalid invocation must not open the graph at all.
    assert graph.queries == []


async def test_sample_size_zero_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _install_fake_graph(monkeypatch)

    with pytest.raises(ValueError, match="--sample-size must be at least 1"):
        await _MODULE.main(["--json", "--sample-size", "0", "--book", "knowledge:book-a"])

    assert graph.queries == []


async def test_sample_size_non_integer_value_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_graph(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        await _MODULE.main(["--json", "--sample-size", "eight", "--book", "knowledge:book-a"])

    assert excinfo.value.code == 2


async def test_sample_size_eight_on_one_book_yields_eight_pairs_and_companion_agrees(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_graph(monkeypatch)
    book_id = "knowledge:book-a"
    flags = ["--sample-size", "8", "--book", book_id]

    assert await _MODULE.main(["--json", *flags]) == 0
    captured = capsys.readouterr()
    pairs = json.loads(captured.out)

    assert len(pairs) == 8
    assert {pair[0] for pair in pairs} == {book_id}
    assert "Effective selection: 8 chunks" in captured.err

    skeleton_path = tmp_path / "labels.jsonl"
    assert await _MODULE.main(["--emit-skeleton", str(skeleton_path), *flags]) == 0
    companion = json.loads(skeleton_path.with_suffix(".sample.json").read_text(encoding="utf-8"))
    skeleton_ids = [
        json.loads(line)["chunk_id"]
        for line in skeleton_path.read_text(encoding="utf-8").splitlines()
    ]
    json_ids = [f"{book}:{index}" for book, index in pairs]
    assert len(skeleton_ids) == 8
    assert skeleton_ids == json_ids
    assert [f"{book}:{index}" for book, index in companion["pairs"]] == json_ids
    # A different size must not silently change the selection version.
    assert companion["sample_version"] == _MODULE.SAMPLE_VERSION


async def test_sample_size_eight_and_texts_agree_with_json_and_companion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_graph(monkeypatch)
    book_id = "knowledge:book-c"
    flags = ["--sample-size", "8", "--book", book_id]

    assert await _MODULE.main(["--json", *flags]) == 0
    pairs = json.loads(capsys.readouterr().out)

    texts_path = tmp_path / "chunk_texts.json"
    assert await _MODULE.main(["--texts", str(texts_path), *flags]) == 0
    capsys.readouterr()
    texts = json.loads(texts_path.read_text(encoding="utf-8"))

    skeleton_path = tmp_path / "labels.jsonl"
    assert await _MODULE.main(["--emit-skeleton", str(skeleton_path), *flags]) == 0
    capsys.readouterr()
    companion = json.loads(skeleton_path.with_suffix(".sample.json").read_text(encoding="utf-8"))
    skeleton_ids = [
        json.loads(line)["chunk_id"]
        for line in skeleton_path.read_text(encoding="utf-8").splitlines()
    ]

    json_ids = [f"{book}:{index}" for book, index in pairs]
    assert len(json_ids) == 8
    assert json_ids == list(texts)
    assert json_ids == skeleton_ids
    assert json_ids == [f"{book}:{index}" for book, index in companion["pairs"]]


async def test_exclude_labeled_removes_the_pilot_chunks_and_prints_the_excluded_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_graph(monkeypatch)
    book_id = "knowledge:book-a"
    pilot_sample = _MODULE.select_sample([chunk for chunk in _chunks(16) if chunk[0] == book_id])
    # One of the six chunks the default sample would draw is already labeled.
    labeled_id = f"{pilot_sample[0][0]}:{pilot_sample[0][1]}"
    exclusion_path = _write_label_file(tmp_path / "pilot.jsonl", [labeled_id])

    result = await _MODULE.main(
        ["--json", "--book", book_id, "--exclude-labeled", str(exclusion_path)]
    )
    captured = capsys.readouterr()
    pairs = json.loads(captured.out)
    selected_ids = [f"{book}:{index}" for book, index in pairs]

    assert result == 0
    assert len(selected_ids) == 6
    assert labeled_id not in selected_ids
    assert "Excluded labeled chunks per book:" in captured.err
    # The printed per-book count matches the chunk actually removed.
    assert f"  {book_id}: 1" in captured.err


async def test_exclusion_leaving_too_few_candidates_fails_naming_book_and_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_graph(monkeypatch)
    book_id = "knowledge:book-a"
    # 16 candidates minus 10 labeled = 6 remaining, fewer than the 8 requested.
    labeled_ids = [f"{book_id}:{index}" for index in range(10)]
    exclusion_path = _write_label_file(tmp_path / "big_pilot.jsonl", labeled_ids)

    with pytest.raises(ValueError, match=book_id) as excinfo:
        await _MODULE.main(
            [
                "--json",
                "--sample-size",
                "8",
                "--book",
                book_id,
                "--exclude-labeled",
                str(exclusion_path),
            ]
        )

    message = str(excinfo.value)
    assert book_id in message
    assert "6" in message, "the message must say how many candidates remain"
    assert "8" in message, "the message must say how many were requested"


async def test_sample_version_v3_differs_from_the_default_and_companion_records_v3(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_graph(monkeypatch)

    assert await _MODULE.main(["--json"]) == 0
    default_ids = {f"{book}:{index}" for book, index in json.loads(capsys.readouterr().out)}

    assert await _MODULE.main(["--json", "--sample-version", "extraction-fidelity-v3"]) == 0
    v3_ids = {f"{book}:{index}" for book, index in json.loads(capsys.readouterr().out)}

    assert len(v3_ids) == 24
    assert v3_ids != default_ids, "v3 must select a different set than the default version"

    skeleton_path = tmp_path / "labels.jsonl"
    assert (
        await _MODULE.main(
            [
                "--emit-skeleton",
                str(skeleton_path),
                "--sample-version",
                "extraction-fidelity-v3",
            ]
        )
        == 0
    )
    companion = json.loads(skeleton_path.with_suffix(".sample.json").read_text(encoding="utf-8"))
    assert companion["sample_version"] == "extraction-fidelity-v3"
    assert {f"{book}:{index}" for book, index in companion["pairs"]} == v3_ids


async def test_four_book_guard_still_runs_without_a_filter_with_new_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    three_books = [row for row in _fake_graph_rows() if row["book_id"] in _BOOKS[:3]]
    _install_fake_graph(monkeypatch, rows=three_books)
    exclusion_path = _write_label_file(tmp_path / "pilot.jsonl", ["knowledge:book-a:0"])
    guard = "Expected the 4-book active frame; observed 3 books"

    with pytest.raises(ValueError, match=guard):
        await _MODULE.main(["--json", "--exclude-labeled", str(exclusion_path)])
    with pytest.raises(ValueError, match=guard):
        await _MODULE.main(["--json", "--sample-version", "extraction-fidelity-v3"])


async def test_human_sheet_reflects_the_chosen_sample_size(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_graph(monkeypatch)

    result = await _MODULE.main(["--sample-size", "8", "--book", "knowledge:book-a"])
    output = capsys.readouterr().out

    assert result == 0
    assert output.count("chunk_id: ") == 8
    assert "Effective selection: 8 chunks" in output


def test_post_selection_disjointness_assertion_names_the_offending_chunk() -> None:
    sample = [("knowledge:book-a", 3, "text")]

    _MODULE._assert_sample_disjoint(sample, set())
    _MODULE._assert_sample_disjoint(sample, {"knowledge:book-a:4"})

    with pytest.raises(ValueError, match="knowledge:book-a:3") as excinfo:
        _MODULE._assert_sample_disjoint(sample, {"knowledge:book-a:3"})

    assert "knowledge:book-a:3" in str(excinfo.value)
