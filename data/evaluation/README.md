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

The 2026-10-15 re-baseline changed the generation instrument: `metrics.faithfulness` records
the project-owned claims metric that the gate actually compares, instead of a RAGAS number the
layer's feed cannot reproduce (`odd/backlog.md`, E1). It was measured twice that day, and the
second measurement is the one committed: after the layer stopped fetching every community summary
and started answering through the production path, faithfulness is **`0.8988` over the 35
questions (593 claims)**. The earlier `0.7913`/`0.65` pair belongs to the question-blind
instrument. The retrieval baseline was re-measured on the shipping configuration in the same pass:
`precision@k = 0.1250` against a `precision_at_k_min` of `0.1175` (the same ~6 % margin the
previous pair used).

**The generation threshold is calibrated against the observed spread, not against an assumed one.**
A first derivation set `faithfulness_min = 0.85` — about 5 % below the measurement, and ≈ 3.9 σ if
the 593 claim verdicts are treated as independent — and **the next gate run refuted it**: the same
instrument, same graph and same model produced `0.8363` over 623 claims, so `0.85` failed on a
healthy system. Claim verdicts correlate within one answer, which is why that binomial standard
error (`0.012`) understated the spread by roughly five times. The committed `faithfulness_min` is
therefore **`0.80`**, chosen from the two observations on record — mean `0.868`, range
`0.836–0.899` — about 8 % below the mean and 4.5 % below the lower sample. No third run was bought:
the dispersion was already demonstrated, and the point is that a threshold must come from
observations rather than from an estimate. Drift can still be told apart from this noise because
the `pairwise_win_rate` and the grounding signals were **stable** across both runs (34/35 each).

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
