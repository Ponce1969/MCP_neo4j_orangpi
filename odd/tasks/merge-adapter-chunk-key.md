# Merge adapter: support production Chunk key (book_id) in MENTIONS re-pointing

## Context

Production Neo4j Chunks carry `book_id` + `chunk_index` (1520/1520) and DO NOT
have `id` nor `source_id`. `Neo4jGraphMergeAdapter` queries assumed
`c.id` / `c.source_id + ':chunk-' + chunk_index`, so `_CAPTURE_EDGES` produced
`other_id = None` → `_build_inverse_mapping` skipped the edge → the MENTIONS
re-point never ran for production merges (orphan mentions pointing at
soft-deleted entities, and incomplete inverse mapping breaks rollback).

Observed live on 2026-09-21: `ApplyMergeUseCase` could not be used for the
"Fairness check" → "Fairness Review" merge; a manual script had to replicate
the transaction with the correct chunk key. This change fixes the adapter so
the real use case works in production.

## Root cause (files)

- `src/book_graph_rag/infrastructure/neo4j_graph_merge_adapter.py`:
  - `_CAPTURE_EDGES` CASE only tries `c.id`, then `c.source_id + ...`.
  - `_REPOINT_MENTIONS_BATCH` WHERE only matches `c.id` /
    `c.source_id + ...`.
  - `_ROLLBACK_REMOVE_CANON_MENTIONS` / `_ROLLBACK_RESTORE_MENTIONS` use the
    same legacy WHERE.

## Fix contract

- Chunk endpoint identity must resolve to `book_id:chunk-<index>` when
  `book_id` exists (production), falling back to legacy `source_id[:...]`
  / `c.id` forms (testcontainers). Both formats must keep passing.
- `capture_inverse_mapping` must include MENTIONS edges for book_id chunks.
- `apply_merge` must reliably re-point MENTIONS onto the canonical entity.
- `rollback_merge` must restore the original MENTIONS for book_id chunks.

## Tasks

- [x] T1: write integration test reproducing book_id chunks (capture → apply → rollback) — RED first
- [x] T2: fix `_CAPTURE_EDGES` chunk-key resolution (book_id first, legacy fallback)
- [x] T3: fix `_REPOINT_MENTIONS_BATCH` WHERE to match book_id endpoint ids
- [x] T4: fix `_ROLLBACK_REMOVE_CANON_MENTIONS` + `_ROLLBACK_RESTORE_MENTIONS` WHERE
- [x] T5: run integration tests + gates (ruff, mypy, validate_architecture), commit work unit

## Evidence

- Commit: `7664b19` (main, no pusheado).
- Gates: ruff clean · mypy 303 files OK · architecture OK · 1388 unit + 8 integration (3 new book_id + 5 legacy).
- Backup: `~/backups_neo4j/bookgraph_backup_20260921T232007Z.json` (Pi).
- Ledger seq=1 entry 7884fd08… (manual merge, MEDIUM, human:gonzalo).