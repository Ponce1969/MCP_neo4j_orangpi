"""Blind interactive labeling of extraction-fidelity chunks from local files only."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from book_graph_rag.domain.extraction_labels import (
    LabeledChunk,
    LabeledEntity,
    load_labeled_chunks,
    normalize_surface,
)
from book_graph_rag.domain.models import EntityType

ENTITY_TYPES: tuple[EntityType, ...] = (
    "pattern",
    "agent",
    "component",
    "concept",
    "tool",
    "framework",
    "mcp",
    "llmops",
    "risk",
)
DEFAULT_LABEL_PATH = Path("data/extraction_labels_v1.jsonl")
InputFn = Callable[[str], str]


def load_label_file(path: Path) -> list[LabeledChunk]:
    """Read JSONL and validate every row through the domain label loader."""
    rows: list[Mapping[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"blank JSONL row at line {line_number}")
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"JSONL row at line {line_number} must be an object")
        rows.append(row)
    return load_labeled_chunks(rows)


def load_text_file(path: Path, chunks: list[LabeledChunk]) -> dict[str, str]:
    """Load only the offline chunk-id-to-text mapping; predictions are not accepted."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or any(
        not isinstance(chunk_id, str) or not isinstance(text, str)
        for chunk_id, text in value.items()
    ):
        raise ValueError("texts JSON must be an object mapping chunk ids to text strings")
    missing = [chunk.chunk_id for chunk in chunks if chunk.chunk_id not in value]
    if missing:
        raise ValueError(f"texts JSON is missing chunk ids: {', '.join(missing)}")
    return value


def save_label_file(path: Path, chunks: list[LabeledChunk]) -> None:
    """Atomically rewrite validated label rows as UTF-8 JSONL with LF endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(chunk.model_dump(mode="json"), ensure_ascii=False) for chunk in chunks]
    contents = "\n".join(lines) + ("\n" if lines else "")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(contents.encode("utf-8"))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _copy_chunk(chunk: LabeledChunk, **updates: object) -> LabeledChunk:
    return LabeledChunk.model_validate({**chunk.model_dump(mode="python"), **updates})


def _render_chunk(
    chunk: LabeledChunk,
    *,
    processed: int,
    total: int,
    texts: Mapping[str, str] | None,
    output_stream: TextIO,
) -> None:
    print(f"Chunk {chunk.chunk_id} — {processed} / {total} processed", file=output_stream)
    if texts is None:
        print("Hint: pass --texts PATH to display the chunk text.", file=output_stream)
    else:
        print("Chunk text:", file=output_stream)
        print(texts[chunk.chunk_id], file=output_stream)

    print("Entities:", file=output_stream)
    for index, entity in enumerate(chunk.entities, start=1):
        print(
            f"  {index}. {entity.canonical} [{entity.type}, {entity.confidence}] "
            f"surfaces={entity.surfaces!r}",
            file=output_stream,
        )
    print(
        "Menu: [a] add entity, [b] delete last entity, [v] mark empty, "
        "[s] save and advance, [q] save progress and quit",
        file=output_stream,
    )


def _ask_value(
    input_fn: InputFn,
    prompt: str,
    previous: str | None,
) -> str:
    suffix = f" [{previous}]" if previous is not None else ""
    value = input_fn(f"{prompt}{suffix}: ")
    if value == "" and previous is not None:
        return previous
    return value.strip()


def _collect_entity(input_fn: InputFn, output_stream: TextIO) -> LabeledEntity:
    canonical: str | None = None
    variants_input: str | None = None
    type_number: str | None = None
    confidence = "high"

    while True:
        canonical = _ask_value(input_fn, "Canonical name", canonical)
        variants_input = _ask_value(
            input_fn, "Variants (comma-separated, optional)", variants_input
        )
        print("Entity type:", file=output_stream)
        for number, entity_type in enumerate(ENTITY_TYPES, start=1):
            print(f"  {number}. {entity_type}", file=output_stream)
        type_number = _ask_value(input_fn, "Type number (1-9)", type_number)
        try:
            selected_type_index = int(type_number) - 1
            if not 0 <= selected_type_index < len(ENTITY_TYPES):
                raise ValueError
        except ValueError:
            print("Type must be a number from 1 to 9.", file=output_stream)
            continue

        confidence = _ask_value(input_fn, "Confidence (high/low)", confidence)
        variants = [variant.strip() for variant in variants_input.split(",") if variant.strip()]
        surfaces = [canonical]
        surfaces.extend(variant for variant in variants if variant != canonical)
        try:
            return LabeledEntity.model_validate(
                {
                    "surfaces": surfaces,
                    "canonical": canonical,
                    "type": ENTITY_TYPES[selected_type_index],
                    "confidence": confidence,
                }
            )
        except ValidationError as exc:
            print(f"Invalid entity: {exc}", file=output_stream)
            print(
                "Please re-enter the entity; Enter keeps each previous value.",
                file=output_stream,
            )


def _insert_entity(chunk: LabeledChunk, entity: LabeledEntity) -> LabeledChunk:
    entities = list(chunk.entities)
    new_key = normalize_surface(entity.canonical)
    insert_at = next(
        (
            index
            for index, current in enumerate(entities)
            if normalize_surface(current.canonical) > new_key
        ),
        len(entities),
    )
    entities.insert(insert_at, entity)
    return _copy_chunk(chunk, entities=entities)


def run_labeler(
    path: Path,
    *,
    input_fn: InputFn,
    output_stream: TextIO,
    texts_path: Path | None = None,
) -> int:
    """Run the resumable menu loop using injected I/O for tests and non-TTY use."""
    chunks = load_label_file(path)
    pending_indices = [index for index, chunk in enumerate(chunks) if not chunk.completed]
    if not pending_indices:
        print("No pending chunks.", file=output_stream)
        return 0
    texts = load_text_file(texts_path, chunks) if texts_path is not None else None

    for index in pending_indices:
        chunk = chunks[index]
        while True:
            _render_chunk(
                chunk,
                processed=index + 1,
                total=len(chunks),
                texts=texts,
                output_stream=output_stream,
            )
            action = input_fn("Action [a/b/v/s/q]: ").strip().casefold()

            if action == "a":
                entity = _collect_entity(input_fn, output_stream)
                chunk = _insert_entity(chunk, entity)
                chunks[index] = chunk
            elif action == "b":
                if not chunk.entities:
                    print("There is no entity to delete.", file=output_stream)
                    continue
                chunk = _copy_chunk(chunk, entities=chunk.entities[:-1])
                chunks[index] = chunk
            elif action == "v":
                chunk = _copy_chunk(chunk, entities=[], completed=True)
                chunks[index] = chunk
                save_label_file(path, chunks)
                print(f"Saved empty chunk {chunk.chunk_id}.", file=output_stream)
                break
            elif action == "s":
                chunk = _copy_chunk(chunk, completed=True)
                chunks[index] = chunk
                save_label_file(path, chunks)
                print(f"Saved chunk {chunk.chunk_id}; advancing.", file=output_stream)
                break
            elif action == "q":
                save_label_file(path, chunks)
                print("Saved progress; quitting.", file=output_stream)
                return 0
            else:
                print("Choose one of a, b, v, s, or q.", file=output_stream)

    print("No pending chunks.", file=output_stream)
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_LABEL_PATH,
        help=f"label JSONL path (default: {DEFAULT_LABEL_PATH})",
    )
    parser.add_argument(
        "--texts",
        type=Path,
        metavar="PATH",
        help="optional JSON mapping of chunk ids to text; no predictions are read",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    input_fn: InputFn | None = None,
    output_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
) -> int:
    args = _parse_args(argv)
    output = output_stream if output_stream is not None else sys.stdout
    errors = error_stream if error_stream is not None else output_stream or sys.stderr
    try:
        return run_labeler(
            args.path,
            input_fn=input_fn if input_fn is not None else input,
            output_stream=output,
            texts_path=args.texts,
        )
    except (OSError, ValueError, ValidationError) as exc:
        print(f"Error: {exc}", file=errors)
        return 2


if __name__ == "__main__":
    sys.exit(main())
