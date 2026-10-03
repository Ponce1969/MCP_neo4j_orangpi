# Backlog — agentic-book-graph

Follow-ups that are real, evidenced and deliberately **out of** the work unit that discovered them.
Each item states what it is, the evidence that it exists, and the block it belongs to. Engram mirror:
`debt/merge-adapter-and-audit-followups` (obs 1455). The reusable diagnosis for the closed debt is
`pattern/merged-into-cycle-diagnosis` (obs 1457).

## Block A — next session: semantic resolution phase

- **A1 (R5a) Cross-namespace duplicates** (same concept in two books). Must go through quarantine
  (AGENTS.md §7.2: fresh backup → dry-run → human approval → apply → scoped + global audit). The intra-namespace
  tooling is generic and reusable: `scripts-ops/resolve_intra_ns.py` (namespace, limit, backup, approval) plus
  `scripts-ops/gen_cross_groups.py` for the cross-namespace candidate set.
- **A2 (R5b) No audit rule for self-loops.** `(:Entity)-[:RELATED]->(:Entity)` with the same node on both ends:
  5 were deleted on 2026-10-01 with explicit approval, but the extractor recreates them (2 of the 5 came from LLM
  extraction in a freshly re-indexed namespace, not from a merge) and nothing flags them today. The merge path no
  longer creates them (verified after the adapter fix).
- **A3 (R5c) `duplicates_entity` is case-sensitive.** It groups by exact `n.name`, so `alucinación` and
  `Alucinación` (same concept) are never reported: "0 duplicates" means 0 *exact-name* duplicates. Belongs to the
  semantic pass, which should key on normalized names (the `normalize_key` helper already exists in
  `domain/audit_models.py`).

## Block B — hardening

- **B1 (R4) Crash/formatting debt: 16 of 381 tracked Python files carry CRLF and fail `ruff format --check`.**
  Any edit to one of them is uncommittable under the pre-commit format gate without a normalization commit, which
  pollutes the review diff (it already happened twice: `neo4j_graph_merge_adapter.py`, and
  `audit_models.py` + `neo4j_audit_adapter.py`, where reformatting `RULE_CATALOG` also orphaned its
  `# noqa: E501,SIM905`). Normalize them in one dedicated commit, then keep the gate honest.
- **B2 Retrieval warning.** The readiness gate passes but warns `low precision@k: 0.0478` (threshold 0.045,
  retrieval layer is informative/optional). Worth an analysis pass before the number drifts: is it a dataset
  artifact (8 future-corpus records skipped) or real retrieval degradation?
- **B3 Five legacy Book 1 chunks without a `:Checkpoint`** (indices 57, 1026, 1302, 1499, 1501; graph predates
  Phase 2). Cover them with `--backfill-checkpoints --dry-run` and then the approval-gated apply. The other three
  books are at 100%.

## Merge adapter debts (from the 2026-10-02 cycle-break work)

- **R1 `rollback_merge` rebuilds mirror directions.** The inverse map is undirected
  (`(dup)-[r:RELATED]-(other)`) and rollback runs **both** restore statements for every RELATED entry, so it
  recreates a direction that never existed (measured: 1 original edge → 2 after rollback). Fixing it needs a
  direction field in `EdgeInverseMap`, which changes the tamper-evident ledger schema — check the chain hash
  before touching the model. Until then, the regression test asserts presence, not cardinality.
- **R2 The ledger does not record the canonical's prior marker.** `apply_merge` now clears the canonical's
  leftover `merged_into`/`merged_at` (it must, or round-robin merges create mutual pairs), but rollback cannot
  restore it. Declared limit (D9), pinned by test.
- **R3 `_DELETE_INTRA_GROUP_RELATED` has no reversibility test.** It deletes any group-to-group RELATED edge
  while rollback restores only what the inverse map captured; fidelity depends on capture completeness and is not
  exercised by any test.

## Closed (for reference)

- The 286 dangling edges of `knowledge:essential-graphrag` (90 MENTIONS + 196 RELATED): closed 2026-10-02 —
  two mutual merge cycles, fixed by clearing the winners' markers; audits `passed` 0/0/0; the class is now covered
  by `ENDPOINT_MENTIONS_MERGED_INVALID` and `ENDPOINT_RELATED_MERGED_INVALID`. See
  `odd/specs/tech-debt-essential-graphrag-endpoints.md` and
  `evidence-bundles/repoint-break-cycles-20261002.json`.
