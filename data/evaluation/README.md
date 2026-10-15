# Evaluation datasets and baselines

This directory contains committed, versioned evaluation artifacts for the
`book-graph-rag` readiness gate (Phase 5).

## Layout

| File | Purpose |
|------|---------|
| `MANIFEST.json` | Catalog of every dataset with sha256, provenance, and model id. |
| `generation_dataset.jsonl` | Generation-layer questions with reference answers. |
| `retrieval_dataset.jsonl` | Retrieval-layer questions with reference context ids. |
| `namespace_routing_dataset.jsonl` | Unit 0 namespace-routing labels for single-book, multi-book, ambiguous, and out-of-domain questions. |
| `resolution_baseline.json` | Measured layer-3 baseline (F1, over-merge). |
| `generation_baseline.json` | Measured layer-5 baseline (faithfulness threshold, finalized). |
| `retrieval_baseline.json` | Measured layer-4 baseline (precision@k threshold, finalized). |

## Manifest contract

`MANIFEST.json` is validated at load time by `JsonlManifestEvaluationDatasetLoader`.
A sha256 mismatch, missing file, or unknown dataset id fails fast before any
evaluation runs.

## Baseline mechanism-first contract

Baseline reports carry `thresholds_finalized: true` with numeric thresholds derived from the
measured mechanism (generation faithfulness, resolution F1 and over-merge, retrieval
precision@k). While a report still has `thresholds_finalized: false`, the readiness gate
reports `INCOMPLETE` (exit 11) even if the measured metrics pass.

The 2026-10-15 re-baseline changed the generation instrument: `metrics.faithfulness` now
records the project-owned claims metric that the gate actually compares — `0.7913` over the
35 questions of `generation_dataset` against `faithfulness_min = 0.65` — instead of a RAGAS
number the layer's feed cannot reproduce (`odd/backlog.md`, E1). That threshold is
deliberately **kept, not re-derived**: keeping it widens the margin from 4.8 % (the old
`0.6825` pair) to 21.7 %, and re-deriving it from the new measurement is a decision for the
maintainer, not something a re-baseline may do silently. The retrieval baseline was
re-measured on the shipping configuration in the same pass: `precision@k = 0.1250` against a
`precision_at_k_min` of `0.1175` (the same ~6 % margin the previous pair used).

## Metrics that must not be trusted

**`answer_relevancy` (RAGAS) is broken in this configuration and must not be tuned against.**
Measured on 2026-10-10: it returns a deterministic `0.0` even when the answer *is* the
question echoed back, in English and in Spanish alike. Each candidate cause was ruled out by
measurement rather than by argument: citation markers, flakiness (two runs over the same real
answer), the embedding model (English-Spanish cosine is `0.6201` with `all-MiniLM-L6-v2` and
`0.8903` with the multilingual one, so language is not the cause), and the question-generation
step (the model does produce the question in Spanish for an English input).

Its replacement is deterministic and costs no model call:
`book_graph_rag.domain.answer_grounding.grounding` reports `cited_context_share` (how many of
the provided contexts the answer cites) and `term_grounding` (the share of the answer's
content terms that appear in those contexts). Both are proxies — a paraphrase sharing no
vocabulary scores low and copying a context scores high — so they are a floor, not a verdict.
Faithfulness and context precision remain the metrics that carry quality decisions.
