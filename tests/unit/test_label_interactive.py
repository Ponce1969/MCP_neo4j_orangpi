"""Behavior tests for the file-only interactive extraction labeler."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
from types import ModuleType
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.extraction_labels import LabeledChunk
from book_graph_rag.domain.models import EntityType

_ROOT = Path(__file__).parents[2]
_SCRIPT_PATH = _ROOT / "scripts/label_interactive.py"
_SPEC = importlib.util.spec_from_file_location("label_interactive", _SCRIPT_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
_MODULE: ModuleType = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _entity(
    canonical: str,
    entity_type: EntityType = "pattern",
    *,
    surfaces: list[str] | None = None,
    confidence: str = "high",
) -> dict[str, Any]:
    return {
        "surfaces": surfaces if surfaces is not None else [canonical],
        "canonical": canonical,
        "type": entity_type,
        "confidence": confidence,
    }


def _row(
    chunk_index: int,
    *,
    entities: list[dict[str, Any]] | None = None,
    completed: bool = False,
) -> dict[str, Any]:
    book_id = "knowledge:book-a"
    return {
        "chunk_id": f"{book_id}:{chunk_index}",
        "book_id": book_id,
        "chunk_index": chunk_index,
        "entities": entities or [],
        "notes": "",
        "completed": completed,
    }


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> bytes:
    content = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
    encoded = content.encode("utf-8")
    path.write_bytes(encoded)
    return encoded


def _drive(
    path: Path,
    answers: list[str],
    *,
    texts: Path | None = None,
    recorded_prompts: list[str] | None = None,
) -> tuple[int, str]:
    iterator = iter(answers)

    def input_fn(prompt: str) -> str:
        if recorded_prompts is not None:
            recorded_prompts.append(prompt)
        return next(iterator)

    output = io.StringIO()
    argv = ["--path", str(path)]
    if texts is not None:
        argv.extend(["--texts", str(texts)])
    result = _MODULE.main(argv, input_fn=input_fn, output_stream=output)
    return result, output.getvalue()


def test_labeled_chunk_defaults_to_pending() -> None:
    chunk = LabeledChunk(
        chunk_id="knowledge:book-a:1",
        book_id="knowledge:book-a",
        chunk_index=1,
        entities=[],
        notes="",
    )

    assert chunk.completed is False


def test_type_menu_is_the_closed_entity_type_tuple_in_model_order() -> None:
    assert get_args(EntityType) == _MODULE.ENTITY_TYPES
    assert len(_MODULE.ENTITY_TYPES) == 9


def test_add_validates_and_inserts_entities_in_normalized_order(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    _write_rows(path, [_row(3, entities=[_entity("Zebra")])])

    result, output = _drive(
        path,
        ["a", "Agent", "Agent Framework", "2", "", "s"],
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    chunk = LabeledChunk.model_validate(saved)
    assert result == 0
    assert [entity.canonical for entity in chunk.entities] == ["Agent", "Zebra"]
    assert chunk.entities[0].surfaces == ["Agent", "Agent Framework"]
    assert chunk.entities[0].type == "agent"
    assert chunk.entities[0].confidence == "high"
    assert chunk.completed is True
    assert "knowledge:book-a:3" in output
    assert "1 / 1" in output


def test_rejected_entity_reasks_without_mutating_or_writing_the_file(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    original = _write_rows(path, [_row(3)])
    answers = iter(["a", "GraphRAG", "graph-rag", "1", ""])
    canonical_prompts = 0

    def input_fn(prompt: str) -> str:
        nonlocal canonical_prompts
        if prompt.startswith("Canonical name"):
            canonical_prompts += 1
            if canonical_prompts == 2:
                assert prompt == "Canonical name [GraphRAG]: "
                assert path.read_bytes() == original
                raise EOFError("stop after observing the retry")
        return next(answers)

    output = io.StringIO()
    with pytest.raises(EOFError):
        _MODULE.run_labeler(path, input_fn=input_fn, output_stream=output)

    assert canonical_prompts == 2
    assert "duplicate normalized" in output.getvalue()
    assert path.read_bytes() == original


def test_delete_removes_the_last_sorted_entity(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    _write_rows(path, [_row(1, entities=[_entity("Agent"), _entity("Zebra")])])

    result, _output = _drive(path, ["b", "s"])

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert result == 0
    assert [entity["canonical"] for entity in saved["entities"]] == ["Agent"]
    assert saved["completed"] is True


def test_empty_action_writes_a_completed_empty_chunk(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    _write_rows(path, [_row(1, entities=[_entity("Agent")])])

    result, _output = _drive(path, ["v"])

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert result == 0
    assert saved["entities"] == []
    assert saved["completed"] is True


def test_save_marks_completed_and_advances_to_the_next_pending_chunk(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    _write_rows(path, [_row(1), _row(2)])

    result, output = _drive(path, ["s", "q"])

    saved = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert result == 0
    assert [row["completed"] for row in saved] == [True, False]
    assert "knowledge:book-a:1" in output
    assert "knowledge:book-a:2" in output


def test_quit_persists_partial_progress_without_completing_current_chunk(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    legacy_row = _row(1, entities=[_entity("ReAct")])
    del legacy_row["completed"]
    _write_rows(path, [legacy_row])

    result, output = _drive(path, ["q"])

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert result == 0
    assert saved["entities"] == [_entity("ReAct")]
    assert saved["completed"] is False
    assert "saved" in output.casefold()


def test_all_completed_rows_exit_without_prompting(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    original = _write_rows(path, [_row(1, completed=True), _row(2, completed=True)])
    output = io.StringIO()

    result = _MODULE.main(
        ["--path", str(path)],
        input_fn=lambda _prompt: pytest.fail("must not prompt when no rows are pending"),
        output_stream=output,
    )

    assert result == 0
    assert "No pending chunks." in output.getvalue()
    assert "cannot be traced to a sample" in output.getvalue()
    assert path.read_bytes() == original


def test_resume_skips_completed_rows_and_starts_at_first_pending(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    _write_rows(path, [_row(1, completed=True), _row(2), _row(3, completed=True)])

    result, output = _drive(path, ["q"])

    assert result == 0
    assert "knowledge:book-a:2" in output
    assert "knowledge:book-a:1" not in output
    assert "knowledge:book-a:3" not in output


def test_atomic_rewrite_uses_lf_and_preserves_completed_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "labels.jsonl"
    completed = _row(1, entities=[_entity("Café")], completed=True)
    pending = _row(2)
    _write_rows(path, [completed, pending])
    replace = _MODULE.os.replace
    replaced: list[tuple[Path, Path]] = []

    def recording_replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        replaced.append((source_path, destination_path))
        assert source_path.parent == destination_path.parent
        replace(source, destination)

    monkeypatch.setattr(_MODULE.os, "replace", recording_replace)
    result, _output = _drive(path, ["q"])

    data = path.read_bytes()
    saved = [json.loads(line) for line in data.decode("utf-8").splitlines()]
    assert result == 0
    assert b"\r" not in data
    assert b"Caf\xc3\xa9" in data
    assert b"Caf\\u00e9" not in data
    assert saved[0] == completed
    assert saved[1]["completed"] is False
    assert len(replaced) == 1
    assert replaced[0][1] == path


def test_corrupt_jsonl_aborts_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    original = b'{"chunk_id":\n'
    path.write_bytes(original)
    output = io.StringIO()

    result = _MODULE.main(
        ["--path", str(path)],
        input_fn=lambda _prompt: pytest.fail("must not prompt"),
        output_stream=output,
    )

    assert result != 0
    assert "error" in output.getvalue().casefold()
    assert path.read_bytes() == original


def test_chunk_text_is_rendered_when_provided_and_hint_is_shown_when_absent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "labels.jsonl"
    _write_rows(path, [_row(1)])
    texts = tmp_path / "texts.json"
    texts.write_text(
        json.dumps({"knowledge:book-a:1": "Texto con ReAct y GraphRAG."}, ensure_ascii=False),
        encoding="utf-8",
    )

    result_with_text, with_text = _drive(path, ["q"], texts=texts)
    result_without_text, without_text = _drive(path, ["q"])

    assert result_with_text == result_without_text == 0
    assert "Texto con ReAct y GraphRAG." in with_text
    assert "--texts PATH" in without_text


def _capture_suggestions(path: Path, canonical: str) -> str:
    output = io.StringIO()

    def input_fn(prompt: str) -> str:
        if prompt.startswith("Action"):
            return "a"
        if prompt.startswith("Canonical name"):
            return canonical
        raise EOFError("stop after displaying history suggestions")

    with pytest.raises(EOFError):
        _MODULE.run_labeler(path, input_fn=input_fn, output_stream=output)
    return output.getvalue()


def _completed_rows_for(canonical: str, count: int, *, start: int) -> list[dict[str, Any]]:
    return [
        _row(index, entities=[_entity(canonical)], completed=True)
        for index in range(start, start + count)
    ]


def test_history_suggestions_order_exact_then_frequency_then_alphabetically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "labels.jsonl"
    rows = (
        _completed_rows_for("GraphRAG", 1, start=1)
        + _completed_rows_for("GraphRAG Able", 2, start=2)
        + _completed_rows_for("GraphRAG Beta", 3, start=4)
        + _completed_rows_for("GraphRAG Alpha", 3, start=7)
        + [_row(10)]
    )
    _write_rows(path, rows)

    output = _capture_suggestions(path, "GraphRAG")

    suggestion_lines = [
        line.strip()
        for line in output.splitlines()
        if line.strip().startswith(tuple(f"{number}." for number in range(1, 10)))
    ]
    assert [
        line.split(" [", maxsplit=1)[0].split(". ", maxsplit=1)[1] for line in suggestion_lines
    ] == [
        "GraphRAG",
        "GraphRAG Alpha",
        "GraphRAG Beta",
        "GraphRAG Able",
    ]
    assert "[pattern]" in suggestion_lines[0]
    assert "1 time" in suggestion_lines[0]
    assert "3 times" in suggestion_lines[1]


def test_history_suggestions_are_capped_at_five(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    names = [f"Graph Entity {letter}" for letter in "ABCDEFGH"]
    rows = [
        _row(index, entities=[_entity(name)], completed=True)
        for index, name in enumerate(names, start=1)
    ] + [_row(20)]
    _write_rows(path, rows)

    output = _capture_suggestions(path, "Graph")

    suggestion_lines = [
        line.strip()
        for line in output.splitlines()
        if line.strip().startswith(tuple(f"{number}." for number in range(1, 10)))
    ]
    assert len(suggestion_lines) == 5
    assert [
        line.split(" [", maxsplit=1)[0].split(". ", maxsplit=1)[1] for line in suggestion_lines
    ] == names[:5]


def test_reusing_history_prefills_fields_and_allows_an_extra_variant(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    previous = _entity(
        "GraphRAG",
        "tool",
        surfaces=["GraphRAG", "Reasoning and Acting"],
        confidence="low",
    )
    _write_rows(path, [_row(1, entities=[previous], completed=True), _row(2)])

    prompts: list[str] = []
    result, output = _drive(
        path,
        [
            "a",
            "GraphRAG",
            "1",
            "",
            "Reasoning and Acting, Added Variant",
            "",
            "",
            "s",
        ],
        recorded_prompts=prompts,
    )

    saved = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    reused = saved[1]["entities"][0]
    assert result == 0
    assert reused == {
        "surfaces": ["GraphRAG", "Reasoning and Acting", "Added Variant"],
        "canonical": "GraphRAG",
        "type": "tool",
        "confidence": "low",
    }
    assert "Canonical name [GraphRAG]: " in prompts
    assert "Variants (comma-separated, optional) [Reasoning and Acting]: " in prompts
    assert "Type number (1-9) [5]: " in prompts
    assert "Confidence (high/low) [low]: " in prompts


def test_non_numeric_history_choice_is_a_new_canonical_name(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    _write_rows(
        path,
        [_row(1, entities=[_entity("Graph Retrieval")], completed=True), _row(2)],
    )

    result, _output = _drive(
        path,
        ["a", "Graph", "Brand New Entity", "", "1", "", "s"],
    )

    saved = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert result == 0
    assert saved[1]["entities"] == [_entity("Brand New Entity")]


def test_empty_history_keeps_the_original_prompt_flow(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    _write_rows(path, [_row(1)])
    prompts: list[str] = []
    answers = iter(["a", "GraphRAG", "", "1", "", "s"])

    def input_fn(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    output = io.StringIO()
    result = _MODULE.run_labeler(path, input_fn=input_fn, output_stream=output)

    assert result == 0
    assert prompts == [
        "Action [a/b/v/s/q]: ",
        "Canonical name: ",
        "Variants (comma-separated, optional): ",
        "Type number (1-9): ",
        "Confidence (high/low) [high]: ",
        "Action [a/b/v/s/q]: ",
    ]
    assert "History suggestions" not in output.getvalue()


def test_suggestions_exclude_entities_from_pending_rows(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    rows = [
        _row(1),
        _row(2, entities=[_entity("GraphRAG History")], completed=True),
        _row(3, entities=[_entity("GraphRAG Pending")]),
    ]
    _write_rows(path, rows)

    output = _capture_suggestions(path, "Graph")

    suggestions = output.split("History suggestions:", maxsplit=1)[1]
    assert "GraphRAG History" in suggestions
    assert "GraphRAG Pending" not in suggestions


def test_reused_entity_is_validated_and_inserted_in_normalized_order(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    previous = _entity(
        "ReAct",
        surfaces=["ReAct", "Reasoning and Acting"],
        confidence="low",
    )
    _write_rows(
        path,
        [_row(1, entities=[previous], completed=True), _row(2, entities=[_entity("Zulu")])],
    )

    result, _output = _drive(path, ["a", "ReAct", "1", "", "", "", "", "s"])

    saved = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    chunk = LabeledChunk.model_validate(saved[1])
    assert result == 0
    assert [entity.canonical for entity in chunk.entities] == ["ReAct", "Zulu"]
    assert chunk.entities[0].surfaces == ["ReAct", "Reasoning and Acting"]
    assert chunk.entities[0].confidence == "low"


def test_invalid_history_reuse_reports_and_falls_back_to_manual_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "labels.jsonl"
    _write_rows(
        path,
        [_row(1, entities=[_entity("GraphRAG")], completed=True), _row(2)],
    )
    original_validate = _MODULE.LabeledEntity.model_validate
    should_reject_reuse = True
    action_count = 0

    def input_fn(prompt: str) -> str:
        nonlocal should_reject_reuse, action_count
        if prompt.startswith("Action"):
            action_count += 1
            return "a" if action_count == 1 else "q"
        if prompt == "Canonical name: ":
            return "GraphRAG"
        if prompt.startswith("Suggestion number"):

            def reject_first_reuse(cls: type[Any], /, value: Any, **kwargs: Any) -> Any:
                nonlocal should_reject_reuse
                if (
                    should_reject_reuse
                    and isinstance(value, dict)
                    and value.get("canonical") == "GraphRAG"
                ):
                    should_reject_reuse = False
                    raise ValidationError.from_exception_data(
                        "LabeledEntity",
                        [
                            {
                                "type": "value_error",
                                "loc": ("canonical",),
                                "input": "GraphRAG",
                                "ctx": {"error": ValueError("history entry is invalid")},
                            }
                        ],
                    )
                return original_validate(value, **kwargs)

            monkeypatch.setattr(
                _MODULE.LabeledEntity,
                "model_validate",
                classmethod(reject_first_reuse),
            )
            return "1"
        if prompt.startswith("Variants"):
            return ""
        if prompt.startswith("Type number"):
            return "1"
        if prompt.startswith("Confidence"):
            return ""
        raise AssertionError(f"Unexpected prompt: {prompt}")

    output = io.StringIO()
    result = _MODULE.run_labeler(path, input_fn=input_fn, output_stream=output)

    saved = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert result == 0
    assert "could not be reused" in output.getvalue()
    assert saved[1]["entities"] == [_entity("GraphRAG")]


def test_rejected_reuse_does_not_change_file_bytes(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    previous = _entity(
        "GraphRAG",
        surfaces=["GraphRAG", "Reasoning and Acting"],
        confidence="low",
    )
    original = _write_rows(path, [_row(1, entities=[previous], completed=True), _row(2)])
    canonical_prompts = 0

    def input_fn(prompt: str) -> str:
        nonlocal canonical_prompts
        if prompt.startswith("Action"):
            return "a"
        if prompt.startswith("Canonical name"):
            canonical_prompts += 1
            if canonical_prompts == 1:
                return "GraphRAG"
            if canonical_prompts == 2:
                assert prompt == "Canonical name [GraphRAG]: "
                return ""
            assert prompt == "Canonical name [GraphRAG]: "
            assert path.read_bytes() == original
            raise EOFError("stop after rejecting the invalid extra variant")
        if prompt.startswith("Suggestion number"):
            return "1"
        if prompt.startswith("Variants"):
            return "graph-rag"
        if prompt.startswith("Type number") or prompt.startswith("Confidence"):
            return ""
        raise AssertionError(f"Unexpected prompt: {prompt}")

    output = io.StringIO()
    with pytest.raises(EOFError):
        _MODULE.run_labeler(path, input_fn=input_fn, output_stream=output)

    assert "duplicate normalized" in output.getvalue()
    assert path.read_bytes() == original


def test_reuse_warns_when_history_gives_one_canonical_two_types(tmp_path: Path) -> None:
    """A silent first-occurrence pick would turn type_agreement into an artifact of order."""
    path = tmp_path / "labels.jsonl"
    _write_rows(
        path,
        [
            _row(1, entities=[_entity("RAG", "pattern")], completed=True),
            _row(2, entities=[_entity("RAG", "concept")], completed=True),
            _row(3, entities=[]),
        ],
    )

    result, output = _drive(path, ["a", "RAG", "1", "", "", "", "", "q"])

    saved = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    chunk = LabeledChunk.model_validate(saved[2])
    assert result == 0
    assert "more than one type" in output
    assert chunk.entities[0].type == "pattern", "reuse still takes the first occurrence"


def _write_sample(
    path: Path,
    pairs: list[list[str | int]],
    *,
    sample_version: str = "extraction-fidelity-v2",
) -> Path:
    companion = path.with_suffix(".sample.json")
    record = {"sample_version": sample_version, "pairs": pairs}
    companion.write_bytes((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
    return companion


def test_matching_sample_starts_at_first_pending_chunk(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    rows = [_row(1, completed=True), _row(2), _row(3, completed=True)]
    _write_rows(path, rows)
    _write_sample(path, [["knowledge:book-a", index] for index in (1, 2, 3)])

    result, output = _drive(path, ["q"])

    assert result == 0
    assert "Chunk knowledge:book-a:2" in output
    assert "Chunk knowledge:book-a:1" not in output
    assert "Chunk knowledge:book-a:3" not in output


def test_wrong_sample_count_refuses_before_prompt_and_preserves_file(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    rows = [_row(index, completed=True) for index in range(48)]
    original = _write_rows(path, rows)
    _write_sample(
        path,
        [["knowledge:book-a", index] for index in range(24)],
        sample_version="extraction-fidelity-v1",
    )
    output = io.StringIO()

    result = _MODULE.main(
        ["--path", str(path)],
        input_fn=lambda _prompt: pytest.fail("sample mismatch must be checked before prompts"),
        output_stream=output,
    )

    message = output.getvalue()
    assert result != 0
    assert "24 pairs in the sample, 48 rows in the file" in message
    assert "extraction-fidelity-v1" in message
    assert "regenerate the skeleton" in message.casefold()
    assert "--ignore-sample" in message
    assert path.read_bytes() == original


def test_same_count_with_different_chunk_id_refuses_and_names_differences(
    tmp_path: Path,
) -> None:
    path = tmp_path / "labels.jsonl"
    _write_rows(path, [_row(1), _row(2)])
    original = path.read_bytes().replace(b",", b", ")
    path.write_bytes(original)
    _write_sample(
        path,
        [["knowledge:book-a", 1], ["knowledge:book-a", 99]],
        sample_version="legacy-v1",
    )
    output = io.StringIO()

    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return "q"

    result = _MODULE.main(
        ["--path", str(path)],
        input_fn=answer,
        output_stream=output,
    )

    message = output.getvalue()
    assert result != 0
    assert prompts == []
    assert "knowledge:book-a:99" in message
    assert "knowledge:book-a:2" in message
    assert "legacy-v1" in message
    assert "regenerate the skeleton" in message.casefold()
    assert "--ignore-sample" in message
    assert path.read_bytes() == original


def test_missing_sample_companion_warns_and_proceeds(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    _write_rows(path, [_row(1)])

    result, output = _drive(path, ["q"])

    assert result == 0
    assert "cannot be traced to a sample" in output


def test_ignore_sample_proceeds_with_mismatch_and_warns_untraceable(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    _write_rows(path, [_row(1)])
    _write_sample(path, [["knowledge:book-a", 99]], sample_version="legacy-v1")
    output = io.StringIO()

    result = _MODULE.main(
        ["--path", str(path), "--ignore-sample"],
        input_fn=lambda _prompt: "q",
        output_stream=output,
    )

    assert result == 0
    assert "not be traceable to the recorded sample" in output.getvalue()
