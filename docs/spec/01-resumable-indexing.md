# 01 — Resumable Indexing

> **Status: Implemented (Phase 2).** The pipeline persists per-chunk `:Checkpoint`
> nodes in Neo4j and can resume, replay dead letters, and backfill legacy graphs.
> One known gap remains: `checkpoint_max_attempts` is configured but not yet
> enforced as a hard stop for retries (see §2.3).

## 1. Current state `[VERIFIED]`

- Ingestion is orchestrated by `application/index_book_use_case.py`: it materializes all
  PDF chunks, runs LLM extraction under an `asyncio.Semaphore`, and flushes mini-batches
  (`PROCESSING_BATCH_SIZE`, default 5) to Neo4j.
- Writes are idempotent via parameterized `MERGE`: `infrastructure/neo4j_command_adapter.py`
  (`upsert_book`, `upsert_entities`, `upsert_relationships`, `upsert_mentions`,
  `upsert_editorial_structure`).
- Failed chunks are appended to a **file** dead-letter (`data/dead_letter.log`) with
  `chunk_index`, `page_ref`, `error_type`, `error_message`, timestamp
  (`index_book_use_case.py::_write_dead_letter`). Orphan relationships go to a separate
  JSONL (`infrastructure/dead_letter.py`).
- LLM calls retry with exponential backoff via `tenacity` (`infrastructure/llm_adapter.py`).
- **Gap:** there is **no per-chunk checkpoint in Neo4j**. A killed run re-processes
  already-extracted chunks (re-spending LLM tokens) and cannot resume at the failure
  point. There is no source/pipeline/model/schema version tracked against chunks.

## 2. Target requirements

### 2.1 Chunk identity `[IMPLEMENTED]`

- A chunk's durable identity is `(source_id, chunk_index)`, not `chunk_index` alone.
  Today `chunk_index` is an integer scoped only implicitly to a single PDF, and
  `book_id` is stored on `:Chunk` as a property. `[VERIFIED]`
  `neo4j_command_adapter.py::upsert_editorial_structure` merges on
  `{chunk_index, book_id}`.
- `source_id` MUST be stable and derived from the source document (see 02 for the
  namespace model). `chunk_index` MUST be stable under re-chunking of the same source
  version; if the chunker changes, the source **pipeline version** changes, not the
  chunk index (see §2.3).

### 2.2 Source / pipeline / model / schema versioning `[IMPLEMENTED]`

Every processed chunk MUST record, at minimum:

| Dimension | Example value | Purpose |
|-----------|---------------|---------|
| `source_id` | namespaced book/corpus id | Which document |
| `source_version` | hash of (title, author, page count, PDF bytes) | Detect source changes |
| `pipeline_version` | chunker + prompt template version | Invalidate on chunking/prompt change |
| `model_version` | graph LLM model id + date | Invalidate on extraction-model change |
| `schema_version` | graph schema revision | Invalidate on schema change |

A change to any dimension makes previously-checkpointed chunks **stale** (see §2.5).
This is `[TARGET]`; no such versions are persisted today.

### 2.3 Checkpoint lifecycle `[TARGET]`

> Gap: `checkpoint_max_attempts` is stored in `Settings` and passed to
> `IndexBookUseCase`, but the use case does not yet skip chunks whose stored
> `attempt` count has reached the limit. Retries are effectively unbounded today.

- Checkpoint state lives in Neo4j (or an equivalent transactional store), keyed by
  `(source_id, chunk_index)`, with `status ∈ {PENDING, PROCESSING, PROCESSED, FAILED,
  STALE}`.
- A chunk is marked `PROCESSED` **only after** its entities/relationships are persisted
  in the **same atomic transaction** as the checkpoint update. This is the core
  anti-wasted-token invariant.
- `PROCESSING` is a lease (with timestamp); a crash leaves `PROCESSING` chunks that the
  next run can detect and re-claim after a stale-lease timeout.
- `FAILED` chunks are retried up to a configured limit, then routed to the dead letter
  with the failure context and the checkpoint left in `FAILED`.

### 2.4 Stale processing recovery `[IMPLEMENTED]`

- On startup, the pipeline MUST detect chunks whose recorded version dimensions no
  longer match the current run's dimensions and mark them `STALE`.
- `STALE` chunks are re-processed (not skipped) because their content may no longer be
  valid under the new pipeline/model/schema.
- Re-processing is scoped to the affected source(s); it must not silently wipe
  unrelated sources.

### 2.5 Atomic graph writes `[IMPLEMENTED]`

- The unit of atomicity is **one chunk's full write set**: its entities, relationships,
  editorial structure, mentions, and the checkpoint row commit together or not at all.
- Today batch flushes span multiple chunks and are not a single transaction
  (`index_book_use_case.py::_flush_batch`); this is acceptable as an interim idempotency
  strategy but does not satisfy the resumable atomicity requirement.
- `MERGE` remains the write primitive; atomicity is provided by wrapping the chunk's
  writes + checkpoint in one Neo4j transaction.

### 2.6 Retries and dead letters `[IMPLEMENTED]`

- Transport retries remain on `tenacity` with exponential backoff and a cap
  (`LLM_MAX_RETRIES`, `LLM_RETRY_WAIT_MAX`). `[VERIFIED]` `config.py`.
- Dead-letter entries MUST be re-addressable: each record carries `source_id`,
  `chunk_index`, version dimensions, `error_type`, `error_message`, and timestamp, so a
  replay command can re-process exactly those chunks. Today the file dead letter lacks
  `source_id`/version dimensions. `[VERIFIED]` `index_book_use_case.py::_write_dead_letter`.
- A replay command (`index --resume` / `index --replay-dead-letter`) is required.

## 3. Invariants

1. `input_chunks == processed ∪ processing ∪ failed ∪ skipped` (no silent loss).
2. `processed == chunks whose checkpoint is PROCESSED and whose writes committed`.
3. `relationship_endpoints == input == persisted + dead_lettered_orphans`
   (zero-silent-drop; already documented in `neo4j_command_adapter.py::upsert_relationships`).
4. Re-running with unchanged versions is a **no-op** for already-PROCESSED chunks
   (idempotent resume, zero redundant LLM tokens).
5. A version change on one dimension forces re-processing only of the affected chunks.

## 4. Acceptance criteria

- [x] Interrupting a run mid-way and restarting resumes without re-extracting
  already-`PROCESSED` chunks (verified by counting LLM calls in a test).
- [x] A chunk's writes and its `PROCESSED` checkpoint are atomic (kill between them
  leaves no half-written chunk counted as processed).
- [x] Changing `pipeline_version` (e.g. prompt text) marks matching chunks `STALE` and
  re-processes them; unrelated sources are untouched.
- [x] Dead-letter records are replayable end-to-end.
- [x] Re-running with identical versions is a no-op.

## 5. Tests

- **Unit:** checkpoint state machine transitions; stale-lease reclaim; version-diff
  staleness predicate; dead-letter record shape.
- **Integration (Neo4j container):** kill-switch resume; atomic chunk+checkpoint commit;
  orphan-dead-letter invariant; replay command.
- **Property test:** fuzz interruption points and assert invariant §3.1 holds.

## 6. Migration / rollback

- Migration is additive: introduce checkpoint nodes/table without removing existing
  MERGE behavior. Existing graphs (no checkpoints) are treated as "fully unprocessed"
  on first resumable run; a `--backfill-checkpoints` dry-run reports what would be marked.
- Rollback: checkpoint state is derived metadata; deleting it (with approval) returns the
  system to today's re-run-everything behavior without corrupting graph data.
- `clear_index` (`neo4j_command_adapter.py`) remains a destructive, approval-gated
  operation and must also clear checkpoints atomically if introduced.

## 7. Open decisions

- `[OPEN]` Checkpoint store location: in-graph nodes (`:Checkpoint`) vs. a separate table
  vs. an external store. In-graph is preferred for atomicity with writes; final choice is
  implementation detail for Phase 2 (checkpoint keys need `source_id`).
- **Decided (2026-09-02):** `source_id` is the namespaced source id from 02
  (`corpus:source`), not a separate document id. Resolved by 02 before 01 is implemented.
