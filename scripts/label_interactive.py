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


def _read_sample_companion(path: Path) -> tuple[str, list[tuple[str, int]]]:
    """Read and validate the version and pair list in a sample companion."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"sample companion {path} must contain a JSON object")

    sample_version = value.get("sample_version")
    if not isinstance(sample_version, str) or not sample_version:
        raise ValueError(f"sample companion {path} must have a non-empty sample_version")

    raw_pairs = value.get("pairs")
    if not isinstance(raw_pairs, list):
        raise ValueError(f"sample companion {path} must contain a pairs list")

    pairs: list[tuple[str, int]] = []
    for position, raw_pair in enumerate(raw_pairs, start=1):
        if not isinstance(raw_pair, list) or len(raw_pair) != 2:
            raise ValueError(f"sample companion pair {position} must be [book_id, chunk_index]")
        book_id, chunk_index = raw_pair
        if (
            not isinstance(book_id, str)
            or not book_id
            or isinstance(chunk_index, bool)
            or not isinstance(chunk_index, int)
        ):
            raise ValueError(
                f"sample companion pair {position} must contain a non-empty book_id "
                "and integer chunk_index"
            )
        pairs.append((book_id, chunk_index))
    return sample_version, pairs


def _format_chunk_ids(pairs: list[tuple[str, int]]) -> str:
    rendered = [f"{book_id}:{chunk_index}" for book_id, chunk_index in pairs[:5]]
    if len(pairs) > 5:
        rendered.append(f"... ({len(pairs) - 5} more)")
    return ", ".join(rendered)


def _check_sample_companion(
    path: Path,
    chunks: list[LabeledChunk],
    *,
    ignore_sample: bool,
    output_stream: TextIO,
) -> None:
    """Refuse labels whose recorded sample does not match, before any prompting."""
    companion_path = path.with_suffix(".sample.json")
    if not companion_path.exists():
        print(
            f"Warning: no sample companion at {companion_path}; "
            "this label file cannot be traced to a sample.",
            file=output_stream,
        )
        return

    sample_version, sample_pairs = _read_sample_companion(companion_path)
    file_pairs = [(chunk.book_id, chunk.chunk_index) for chunk in chunks]
    sample_ids = set(sample_pairs)
    file_ids = set(file_pairs)
    if len(sample_pairs) == len(file_pairs) and sample_ids == file_ids:
        return

    if ignore_sample:
        print(
            "Warning: --ignore-sample: the label file will not be traceable to the "
            f"recorded sample (sample_version={sample_version!r}).",
            file=output_stream,
        )
        return

    if len(sample_pairs) != len(file_pairs):
        difference = f"{len(sample_pairs)} pairs in the sample, {len(file_pairs)} rows in the file"
    else:
        sample_only = sorted(sample_ids - file_ids)
        file_only = sorted(file_ids - sample_ids)
        difference = (
            f"chunk ids differ; present only in sample: {_format_chunk_ids(sample_only)}; "
            f"present only in file: {_format_chunk_ids(file_only)}"
        )

    raise ValueError(
        f"Label sample mismatch (sample_version={sample_version!r}): {difference}. "
        "Regenerate the skeleton, or pass --ignore-sample deliberately."
    )


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


_HistorySuggestion = tuple[LabeledEntity, int]


def _conflicting_types(chunks: list[LabeledChunk], canonical: str) -> set[EntityType]:
    """Return every type this canonical was labeled with in completed rows.

    More than one means the file contradicts itself. Reuse then silently picks the
    first occurrence, which would make ``type_agreement`` reflect the order chunks
    were labeled instead of the labeler's judgment — so the caller warns instead.
    """
    key = normalize_surface(canonical)
    return {
        entity.type
        for chunk in chunks
        if chunk.completed
        for entity in chunk.entities
        if normalize_surface(entity.canonical) == key
    }


def _history_suggestions(chunks: list[LabeledChunk], canonical: str) -> list[_HistorySuggestion]:
    """Find up to five canonical entities used by completed rows in this file."""
    query = normalize_surface(canonical)
    if not query:
        return []

    counts: dict[str, int] = {}
    examples: dict[str, LabeledEntity] = {}
    for chunk in chunks:
        if not chunk.completed:
            continue
        for entity in chunk.entities:
            key = normalize_surface(entity.canonical)
            counts[key] = counts.get(key, 0) + 1
            examples.setdefault(key, entity)

    matches = [
        (examples[key], count)
        for key, count in counts.items()
        if key == query or key.startswith(query)
    ]
    matches.sort(
        key=lambda suggestion: (
            normalize_surface(suggestion[0].canonical) != query,
            -suggestion[1],
            normalize_surface(suggestion[0].canonical),
            suggestion[0].canonical.casefold(),
        )
    )
    return matches[:5]


def _collect_entity(
    input_fn: InputFn,
    output_stream: TextIO,
    chunks: list[LabeledChunk],
) -> LabeledEntity:
    canonical: str = _ask_value(input_fn, "Canonical name", None)
    variants_input: str | None = None
    type_number: str | None = None
    confidence = "high"
    prompt_canonical = False

    suggestions = _history_suggestions(chunks, canonical)
    if suggestions:
        print("History suggestions:", file=output_stream)
        for number, (history_entity, count) in enumerate(suggestions, start=1):
            label_count = "1 time" if count == 1 else f"{count} times"
            print(
                f"  {number}. {history_entity.canonical} [{history_entity.type}] "
                f"— labeled {label_count}",
                file=output_stream,
            )

        choice = input_fn(f"Suggestion number (or new canonical name) [{canonical}]: ").strip()
        try:
            selected_number = int(choice)
        except ValueError:
            selected_number = 0

        if 1 <= selected_number <= len(suggestions):
            suggestion, _count = suggestions[selected_number - 1]
            try:
                reused = LabeledEntity.model_validate(suggestion.model_dump(mode="python"))
            except ValidationError as exc:
                print(
                    f"History suggestion {selected_number} could not be reused: {exc}",
                    file=output_stream,
                )
                print("Falling back to manual entry.", file=output_stream)
            else:
                conflicts = _conflicting_types(chunks, reused.canonical)
                if len(conflicts) > 1:
                    print(
                        f"Warning: {reused.canonical!r} is labeled with more than one type "
                        f"({', '.join(sorted(conflicts))}); reusing {reused.type!r}. "
                        "Type agreement measures this, so if it is a real inconsistency, "
                        "fix it deliberately rather than inheriting the first one seen.",
                        file=output_stream,
                    )
                canonical = reused.canonical
                variants_input = ", ".join(
                    surface
                    for surface in reused.surfaces
                    if normalize_surface(surface) != normalize_surface(reused.canonical)
                )
                type_number = str(ENTITY_TYPES.index(reused.type) + 1)
                confidence = reused.confidence
                prompt_canonical = True
        elif choice:
            canonical = choice

    while True:
        if prompt_canonical:
            canonical = _ask_value(input_fn, "Canonical name", canonical)
            prompt_canonical = False
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
            prompt_canonical = True
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
            prompt_canonical = True


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
    ignore_sample: bool = False,
) -> int:
    """Run the resumable menu loop using injected I/O for tests and non-TTY use."""
    chunks = load_label_file(path)
    _check_sample_companion(
        path,
        chunks,
        ignore_sample=ignore_sample,
        output_stream=output_stream,
    )
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
                entity = _collect_entity(input_fn, output_stream, chunks)
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
    parser.add_argument(
        "--ignore-sample",
        action="store_true",
        help=(
            "deliberately bypass a companion mismatch; labels will not be traceable to that sample"
        ),
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
            ignore_sample=args.ignore_sample,
        )
    except (OSError, ValueError, ValidationError) as exc:
        print(f"Error: {exc}", file=errors)
        return 2


if __name__ == "__main__":
    sys.exit(main())
