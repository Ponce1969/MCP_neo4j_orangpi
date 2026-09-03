# 06 — Evaluation and Readiness

> **Status: Target (extends existing).** Datasets and result files exist; there is no
> unified, project-owned baseline + readiness gate yet.

## 1. Current state `[VERIFIED]`

- Evaluation artifacts exist: `evaluation_dataset.jsonl`, `evaluation_dataset_dedup.jsonl`,
  `evaluation_results.jsonl`, `docs/benchmarks/gr3_baseline.json`,
  `docs/benchmarks/gr3_after.json`.
- Scripts exist: `scripts/generate_ragas_dataset.py`, `scripts/deduplicate_dataset.py`,
  `scripts/run_ragas_evaluation.py`.
- RAGAS is a dev dependency (`pyproject.toml` `[dependency-groups].dev`).
- There is **no** committed, versioned, project-owned baseline that gates CI, and **no**
  readiness gate tying evaluation to audit (04) or exposure (07).

## 2. Guiding rule on external benchmarks `[TARGET]`

No external metric, benchmark, or library (RAGAS, DeepEval, TruLens, etc.) is treated as
a universal standard without verification. A project-owned baseline is defined **first**
(from the project's own dataset and metrics), and external tools are used only to
**complement** that baseline, never to replace it. Any external metric adopted as a gate
must be shown to correlate with the project's own success criteria on labeled data.

## 3. Evaluation layers `[TARGET]`

Evaluate each layer independently; never report a single blended score across layers.

| Layer | What it measures | Primary metrics | Source of truth |
|-------|------------------|-----------------|-----------------|
| **Graph structure** | Well-formedness, uniqueness, provenance | Audit findings/severities (04) | `book-graph-rag audit` |
| **Extraction fidelity** | Did extraction capture the right entities/relations? | Precision/recall/F1 vs. labeled extraction | Labeled extraction dataset |
| **Entity resolution** | Are same/different entity decisions correct? | F1, over-merge, under-merge (03 §4) | Labeled resolution dataset |
| **Retrieval** | Are retrieved chunks/entities relevant to the question? | Context relevance / precision@k | Retrieval dataset |
| **Generation** | Are answers faithful to retrieved evidence and relevant? | Faithfulness, answer relevance | Generation dataset |

Graph structure (layer 1) is the **audit** domain (04). Layers 2–5 are quality-of-RAG and
belong here. Do not cross the boundary: audit ≠ retrieval/generation quality.

## 4. Datasets and baselines `[TARGET]`

- **Committed, versioned, deduplicated** datasets with a hash and a schema, stored in the
  repo (JSONL is acceptable; add a small `README`/manifest describing labels, fields, and
  provenance).
- **Baselines are run and committed before** any model/pipeline change that affects a
  layer, so every change has a before/after. The existing `gr3_baseline.json` /
  `gr3_after.json` are examples of this pattern; formalize it for all layers.
- A **baseline run is reproducible**: dataset version + code commit + model id + date are
  recorded with every result file.

## 5. Metrics and thresholds `[TARGET]`

- Define, per layer, the metrics that gate readiness and a threshold each. Proposed
  defaults (to be validated against the committed baseline, not assumed):

  | Layer | Gate metric | Default threshold (candidate) |
  |-------|-------------|-------------------------------|
  | Graph structure | BLOCKING findings | 0; no FAILED/UNREACHABLE audit |
  | Extraction fidelity | F1 | ≥ committed baseline |
  | Entity resolution | over-merge on `hard` set | 0 for auto-merge; F1 ≥ baseline |
  | Retrieval | context relevance / precision@k | ≥ baseline |
  | Generation | faithfulness | ≥ baseline; no regression vs. baseline |

- Thresholds are **project-owned** and must be set from measured baselines, not imported.

## 6. Regression policy `[TARGET]`

- Any change that can affect a layer (ingest, resolution, retrieval, prompting, schema)
  MUST run the affected layer's evaluation and compare to the committed baseline.
- A regression (below threshold or below baseline where a threshold exists) blocks merge
  until remediated or explicitly waived with rationale and approval.
- Evaluation runs are part of the gate sequence (00 §3): after test/lint/type/audit,
  before exposure.

## 7. Readiness gate `[TARGET]`

A single, named, versioned readiness gate composes:

- Audit result (04): no BLOCKING, no FAILED/UNREACHABLE.
- Evaluation results (this spec): all gate metrics at/above threshold.
- Optional retrieval smoke (already exists via `validate_graph_use_case` /
  `Neo4jRetrievalSmokeAdapter`; `[VERIFIED]` `application/validate_graph_use_case.py`).

The gate outputs a pass/fail + stable exit code (reusing 04's convention) and a report
naming each metric, its measured value, its threshold, and pass/fail. `expose-mcp` (05)
and any production change (07) require the readiness gate to pass.

## 8. Monitoring and rollback `[TARGET]`

- **Monitoring (production only):** sample live queries, score retrieval/generation over
  time, and detect drift (question distribution, chunk/summary quality, score decay).
  Alerts require a remediation action (re-index, re-run resolution, re-run communities).
- **Rollback:** every evaluation-affecting change must be reversible — a schema/model/
  prompt change records its prior version so the previous baseline's configuration can be
  restored. Rollback points are declared in `roadmap.md`.

## 9. Acceptance criteria

- [ ] A project-owned, versioned, reproducible baseline exists for each layer.
- [ ] No external metric gates CI without a project-owned baseline and correlation
  evidence.
- [ ] The readiness gate deterministically reports every gate metric + threshold + result
  and blocks exposure when any layer fails.
- [ ] A regression in any layer blocks merge unless waived with approval.
- [ ] Evaluation runs are reproducible from committed dataset + code + model id.

## 10. Tests

- **Unit:** threshold evaluation; readiness-gate policy; regression comparison logic.
- **Evaluation harness:** run each layer's dataset end-to-end and assert the reported
  metrics match a committed reference run (determinism guard where applicable).
- **CI:** a dry-run readiness gate exercises all layers and fails on seeded violations.

## 11. Open decisions

- `[OPEN]` Exact threshold values (set after measuring the committed baseline).
- `[OPEN]` Whether external tools (RAGAS/DeepEval) are adopted as secondary signals or
  excluded from gates entirely.
- `[OPEN]` Which layers are required vs. optional for the first `expose-mcp` gate.
