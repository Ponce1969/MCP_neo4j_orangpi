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

from book_graph_rag.domain.extraction_labels import LabeledChunk
from book_graph_rag.domain.models import EntityType

_ROOT = Path(__file__).parents[2]
_SCRIPT_PATH = _ROOT / "scripts/label_interactive.py"
_SPEC = importlib.util.spec_from_file_location("label_interactive", _SCRIPT_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
_MODULE: ModuleType = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _entity(canonical: str, entity_type: EntityType = "pattern") -> dict[str, Any]:
    return {
        "surfaces": [canonical],
        "canonical": canonical,
        "type": entity_type,
        "confidence": "high",
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


def _drive(path: Path, answers: list[str], *, texts: Path | None = None) -> tuple[int, str]:
    prompts: list[str] = []
    iterator = iter(answers)

    def input_fn(prompt: str) -> str:
        prompts.append(prompt)
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
    assert output.getvalue() == "No pending chunks.\n"
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
