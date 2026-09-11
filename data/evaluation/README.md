# Evaluation datasets and baselines

This directory contains committed, versioned evaluation artifacts for the
`book-graph-rag` readiness gate (Phase 5).

## Layout

| File | Purpose |
|------|---------|
| `MANIFEST.json` | Catalog of every dataset with sha256, provenance, and model id. |
| `generation_dataset.jsonl` | Generation-layer questions with reference answers. |
| `retrieval_dataset.jsonl` | Retrieval-layer questions with reference context ids. |
| `resolution_baseline.json` | Measured layer-3 baseline (F1, over-merge). |
| `generation_baseline.json` | Reserved for layer-5 baseline (committed in Slice B). |

## Manifest contract

`MANIFEST.json` is validated at load time by `JsonlManifestEvaluationDatasetLoader`.
A sha256 mismatch, missing file, or unknown dataset id fails fast before any
evaluation runs.

## Baseline mechanism-first contract

Baseline reports carry `thresholds_finalized: false` until a follow-up Phase 5
delta fixes numeric thresholds. While `thresholds_finalized` is false, the
readiness gate reports `INCOMPLETE` (exit 11) even if measured metrics pass.
