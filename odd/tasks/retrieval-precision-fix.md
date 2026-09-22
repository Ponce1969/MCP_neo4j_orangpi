# Fix retrieval precision@k: chunk_id-aware matching + real dataset ids

## Context

`precision@k` is structurally zero. The retrieval dataset
(`data/evaluation/retrieval_dataset.jsonl`, 20 hand-crafted questions) uses
synthetic `reference_context_ids` (`react-summary`, `langgraph-example`), and
`EvaluateRetrievalLayerUseCase` matches with `ref_id in ctx` — substring inside
the natural chunk text. Real chunks are identified `{book_id}:{chunk_index}`
(`knowledge:agentic-architectural-patterns:1335`) and their text never contains
`react-summary`, so `matched=0` always and the metric is permanent noise
(verified on OrangePi 2026-09-21: relevant chunks ARE returned, matching is
broken).

The Phase-5 retrieval threshold cannot be finalized on top of this metric.

## Diagnostic findings (2026-09-21, OrangePi, read-only)

1. **Fixed — search_chunks row multiplication** (`neo4j_query_adapter.py`):
   both OPTIONAL MATCH clauses multiplied rows along variable-length ancestor
   paths and `ORDER BY score DESC LIMIT` ran after the multiplication —
   "Tool use in agents" returned `:363` x6 + `:366` x4 (only 2 unique chunks in
   10 rows; raw fulltext returned 10 unique). Fix collapses each OPTIONAL MATCH
   product with `head(collect(...))` so one row per Chunk, LIMIT counts unique
   nodes. Regression test `tests/integration/test_neo4j_query_adapter_row_dup.py`
   (RED: assert 6 == 3 → GREEN). This also corrupted MCP chunk search + retrieval
   evaluation.
2. **Content duplication in the corpus (NOT code)**: identical text blocks in
   distinct chunks (e.g. `[881,884,887,890,893]`, promo pages "Subscribe for a
   free eBook" etc.) come from the source PDF. Follow-up decision, no code fix.
3. **Dataset anclaje**: 11/20 questions have real lexical anchors in the book;
   9 (GraphRAG, RAGAS, AutoGen, merge ledger, readiness gate, entity
   resolution…) belong to future books (GraphRAG PDFs present in data/ but NOT
   registered in catalog.yaml — only `agentic-architectural-patterns` active).
   Plan: keep questions, flag as future-corpus, filter from metric until the
   book is indexed; register the GraphRAG book as `status: inactive`.

## Fix contract (remaining)

1. `GraphRetrievalPort.fetch_contexts` returns `tuple[RetrievalContext, ...]`
   where `RetrievalContext(chunk_id: str | None, text: str)` lives in
   `domain/retrieval_models.py` (or evaluation_models).
2. `Neo4jRetrievalAdapter._local_contexts` builds `RetrievalContext` with the
   real `chunk_id` from the `search_chunks` payload (already available).
   Global qtype keeps `chunk_id=None` with the summary text.
3. `EvaluateRetrievalLayerUseCase` matches exact `ref_id == ctx.chunk_id`
   (contexts without chunk_id contribute 0), falling back to substring over
   text ONLY when the reference id is not a chunk-style id (keeps legacy test
   semantics explicit).
4. Regenerate `retrieval_dataset.jsonl` with real chunk ids: for each of the
   20 questions, use `search_chunks` top hits on the OrangePi graph, curated
   by human review of snippets.
5. Update `data/evaluation/MANIFEST.json` sha256 for retrieval_dataset (the
   loader validates it) — compute with the loader's own tooling.
6. Unit tests updated to the new contract; add a case proving chunk_id exact
   match yields precision 1.0.

## Tasks

- [ ] T1: probe real chunk ids per question (read-only search_chunks on Pi, curate dataset)
- [ ] T2: add RetrievalContext model + change GraphRetrievalPort contract
- [ ] T3: update Neo4jRetrievalAdapter to emit chunk_id contexts
- [ ] T4: update evaluator matching to exact chunk_id (with legacy text fallback flag)
- [ ] T5: regenerate retrieval_dataset.jsonl + MANIFEST sha256
- [ ] T6: update unit tests + new exact-match test; gates + commit

## Gates

```bash
uv run ruff check .
uv run mypy .
uv run python scripts/validate_architecture.py
uv run pytest tests/ -q --ignore=tests/integration
```

## Evidence

- Diagnostic obs 1324 (structural zero, verified on Pi 2026-09-21).