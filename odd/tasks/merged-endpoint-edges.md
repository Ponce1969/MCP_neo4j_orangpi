# ODD feature: merged-endpoint edges debt (`essential-graphrag`)

- **Branch**: `feat/merged-endpoint-edges` (from `main` @ `af7b47a`)
- **Spec**: `odd/specs/tech-debt-essential-graphrag-endpoints.md` (normative for this work)
- **Engram**: `odd/merged-endpoint-edges/tasks` (mirror of this document), obs 1451 (debt record),
  obs 1449 (`defect/edge-integrity-and-audit-gaps`), obs 1110 rev 17 (current anchor)
- **Opened**: 2026-10-02
- **Production target**: `bookgraph-neo4j` on the OrangePi (read-only until the approval gate)

## Goal

Remove the 286 edges that touch soft-deleted (`merged_into`) entities in
`knowledge:essential-graphrag`, fix the adapter defect that keeps creating them, and make the
class visible to the audit so it cannot regress silently.

## Frozen decisions

| Id | Decision | Reason |
|----|----------|--------|
| D1 | **Re-point, do not delete** (spec §3): move the merged endpoint to its terminal canonical with `MERGE ... ON CREATE/ON MATCH SET += props` | Preserves knowledge; duplicates collapse under MERGE. Deletion is correct only when both endpoints collapse onto the same node |
| D2 | Canonical resolution is **transitive** with a cycle guard | Measured: 15 merged entities point at another merged entity; 4 form a direct cycle. A single-step read would leave dangling edges and can loop |
| D3 | Cycles and unresolvable chains are **reported and excluded**, never guessed | No canonical exists for a cycle; picking one silently would rewrite knowledge wrongly |
| D4 | Adapter fix: the two RELATED re-point batches **must exclude same-group members**, and intra-group RELATED edges are deleted in the same transaction | Root cause of the growth rate (1 per group with intra-group edges); an intra-group edge collapses to a self-loop, which is garbage |
| D5 | **Two** new audit rules, not one: `ENDPOINT_MENTIONS_MERGED_INVALID` and `ENDPOINT_RELATED_MERGED_INVALID` | A single rule cannot carry both scope predicates (MENTIONS scope filters by `book_id`, RELATED by entity prefix). Spec §5 named one rule as an example; the split is recorded as an amendment there |
| D6 | Audit rules land **after** the production cleanup | They inherit category `endpoints` → severity `BLOCKING`, and `gates.yaml` requires `endpoints: pass`; landing them first would make the audit gate report violations by design |

## Measured baseline (OrangePi, read-only, 2026-10-02)

| Metric | Value |
|--------|-------|
| `(:Chunk)-[:MENTIONS]->(merged)` | **90** (all in `knowledge:essential-graphrag`) |
| `(:Entity)-[r:RELATED]-(merged)` | **196** (all `essential-graphrag`; types: enables 64, requires 59, depends_on 49, extends 14, contrasts_with 6, composes 3, alternative_to 1) |
| Edges with **both** endpoints merged | 0 |
| Entities with `merged_into` (global) | 933 — of which **15** point at another merged entity and **4** form a direct cycle |
| RELATED → distinct (canonical, type, other) pairs, 1-step | 184 (12 collapse); 0 collapse to a self-loop at 1 step |
| RELATED destination edges already present | 0 (no property conflicts to resolve) |
| MENTIONS → distinct (chunk, canonical) pairs, 1-step | 90; destination MENTIONS already present: 0 |
| Provenance on the dangling RELATED edges | all 196 carry `type`, `chunk_index`, `source_page` (must survive the re-point) |
| Merge ledger | 958 entries (`data/resolution/merge_ledger.jsonl`) — must stay untouched |

## Tasks

- [x] **T1** Branch + this document + Engram mirror + visible todo list (before the first source write)
- [x] **T2** TDD RED→GREEN: fix `neo4j_graph_merge_adapter` intra-group RELATED handling
      (`_REPOINT_RELATED_OUT_BATCH` / `_REPOINT_RELATED_IN_BATCH` exclude same-group members;
      new intra-group purge; rollback still restores the deleted intra-group edge). Commit `ffbc9f5`,
      preceded by a format-only commit `bd52d48`. RED: `assert 1 == 0` on dangling edges.
- [x] **T3** Gates for T2 (focused pytest 9/9 + ruff + mypy + architecture validator) + commits
- [x] **T4+T5** (merged: one tool, two modes — the resolution logic is identical in inventory and apply,
      and duplicating it is exactly the bug class we are fixing) `src/book_graph_rag/domain/merged_endpoint_resolution.py`
      (pure planner, 23 unit tests) + `scripts-ops/repoint_merged_endpoint_edges.py` (read-only inventory and
      evidence bundle by default; gated `--apply`). Commit `ee9e770`
- [x] **T6** Gates for T4/T5 (23 passed, ruff, mypy, architecture validator; local dry-run smoke)
- [x] **T7** OrangePi: fresh backup (`bookgraph_backup_20261002T131026Z.json`, 175 MB) + dry-run — the dry-run
      **excluded all 286 edges as `cycle`** (see the T7 finding below) → **decision taken 2026-10-02**: break the
      two mutual cycles by keeping the knowledge-carrying member, and fix the adapter root cause with TDD.
      Commits `03e030a` (cycle planner + `--mode break-cycles`), `7813d67` (`_CLEAR_CANONICAL_MARKER`),
      `45d96c3` (test hardening), `8e0b6a`-class (ambiguity guard). The `--apply` path was rehearsed first on an
      isolated seeded Neo4j, which is how the ambiguous-tie hazard was found.
- [x] **T8** OrangePi `--apply` (`--mode break-cycles`, backup 2.2 h old + approval file) → **MENTIONS to merged
      90 → 0, RELATED 196 → 0, merged entities 933 → 931, ledger 958 unchanged, audits scoped and global
      `passed` 0/0/0 exit 0**. `--mode repoint` then read 0 edges: nothing left to re-point. Evidence bundle
      committed at `evidence-bundles/repoint-break-cycles-20261002.json` (`686f124179d8eb2f0ef9ba11c587da3b9242375237d582893f2c41dde8f264a1`)
- [x] **T9** Audit rules `ENDPOINT_MENTIONS_MERGED_INVALID` and `ENDPOINT_RELATED_MERGED_INVALID` in
      `RULE_CATALOG` (19 → 21), `_QUERY_PLAN`, `_RULE_NAME` and both scope families, plus a testcontainers suite
      (global and scoped counts, live-only zero, no overlap with the shape rules). Commit `658cb40`
- [ ] **T10** Final gates (full suite) + spec status flip + docs + close-out (commit, Engram anchor, report)

## T7 finding — the 286 edges hang off two mutual merge cycles (blocks the apply)

The production dry-run read all 286 edges and **excluded every one of them as `cycle`** (0 re-pointed,
0 deletes). The cycle guard behaved as designed (D3: never guess); the data is the problem.

- All 286 edges touch exactly **two** entities: `knowledge:essential-graphrag:large-language-model-component`
  and `knowledge:essential-graphrag:large-language-model-concept`.
- Each sits in a **mutual merge pair**: `large-language-model-component.merged_into = llm-component`
  **and** `llm-component.merged_into = large-language-model-component` (same for `…-concept`/`llm-concept`).
  These are the 4 direct cycles measured before. Re-pointing onto either member leaves the edge pointing at
  a node that carries `merged_into`, so the acceptance criterion (0 edges to merged) is unreachable without
  breaking the pair.
- **The ledger decides the tie** (`data/resolution/merge_ledger.jsonl`): three rounds in opposite directions
  over the same ids — seq 6/7 (22:48Z on 23-Sep, canonical = long name), seq 32/33 (00:50:37Z on 24-Sep,
  canonical = short alias), seq **49/50** (00:50:52Z on 24-Sep, canonical = **long name** again, 163 and 123
  captured edges). Last applied wins: `large-language-model-component` and `large-language-model-concept`.
- The knowledge agrees with the ledger: the long names hold 55 MENTIONS + 108 RELATED and 35 + 88; the short
  aliases are shells with 0 and 0.
- **New root-cause defect**: `_MARK_MERGED_INTO` sets `merged_into` on the candidates but never clears or
  validates the canonical's own marker, so a round-robin merge leaves the canonical marked forever, producing
  mutual pairs and cyclic chains. Recorded in Engram as `defect/mutual-merge-cycles-canonical-marker`.

**Decision needed (maintainer)**: closing the debt requires clearing `merged_into` + `merged_at` on the two
long-named keepers (a node-property mutation that the spec's non-goals exclude, so the spec needs an explicit
amendment) and only then re-pointing the 286 edges. Options presented to the maintainer, with consequences.

## Decision record (2026-10-02, maintainer)

| Id | Decision | Reason |
|----|----------|--------|
| D7 | Break the mutual cycles by keeping the **knowledge-carrying member**: clear `merged_into`/`merged_at` on the winner of each cycle (highest live degree; ties by shortest id, then lexicographic) | Any re-point onto a member of a mutual pair still leaves a dangling edge, so the acceptance criteria are unreachable without breaking the pair. The ledger's last applied round (seq 49/50) and the degree evidence agree on the long-named nodes |
| D8 | Fix the root cause in the adapter, with TDD: `apply_merge` now clears the canonical's own leftover marker inside the same transaction | The defect that produced the mutual pairs and the cyclic chains; without it the class returns on the next book |
| D9 | Declared limit: rollback cannot restore the canonical's prior marker, because the ledger does not record it. Pinned by test | Extending the ledger model would change the tamper-evident chain schema; out of scope here and registered as debt |
| D10 | D3 ("cycles are reported and excluded") is superseded for cycle-breaking: cycles are now resolved by a documented deterministic rule, still never guessed | Approved by the maintainer; the spec amendment lands with the close-out |

## Follow-up debts registered by this work

1. **Rollback mirrors directions**: the inverse map is undirected and `rollback_merge` runs both restore statements for every RELATED entry, so it rebuilds a direction that never existed. Pinned by a presence assertion in the strengthened regression test.
2. **Ledger does not capture the canonical's prior marker** (D9).
3. **Adapter delete fidelity**: `_DELETE_INTRA_GROUP_RELATED` deletes any group-to-group RELATED edge, while rollback restores only what the inverse map captured; not exercised by a test.

## Rules in force for this work

1. Production is **read-only** until T7's explicit approval; every mutation goes through the §7.2 gate.
2. Never `DETACH DELETE` nodes; never touch `merged_into`, the ledger, or community summaries.
3. Local integration tests use **testcontainers only** (Docker 29.1.5 available locally).
4. No connection from tests or scripts to `bolt://100.106.85.109:7687` except the explicit T7/T8 runs.
5. Conventional Commits, English artifacts, `uv` for everything.

## Evidence log

| Task | Commit | Evidence |
|------|--------|----------|
| T1 | — | branch `feat/merged-endpoint-edges`; this document; Engram mirror |
| T2 | `bd52d48`, `ffbc9f5` | adapter fix; RED `assert 1 == 0`; focused suite 9/9; independent verifier confirmed PASS + flagged the test was not mutation-resistant |
| T3 | `ffbc9f5` | pre-commit hook: ruff check, mypy, architecture validator, ruff format check all green |
| T4+T5 | `ee9e770` | domain planner + 23 unit tests (RED: ModuleNotFoundError; GREEN: 23 passed); ops script; mypy 381 files clean |
| T6 | `ee9e770` | local dry-run smoke exit 0; bundle schema verified |
| T7 | `03e030a`, `7813d67`, `45d96c3` | production dry-run (286 read / 286 excluded as `cycle`); decisions D7-D10 below; cycle planner 37 unit tests; adapter marker fix RED→GREEN with 2 new integration tests; regression suite 9/9; isolated rehearsal (`repoint-rehearsal` container, `bookgraph-neo4j` untouched) found the ambiguous-tie hazard → guard added |
| T8 | `62780cc` | apply on production: 90→0 and 196→0, merged 933→931, ledger 958, `APPLY_OK`; audits scoped and global `passed` 0/0/0 exit 0; bundle committed and hashed |
| T9 | `658cb40` | 21 rules; new suite RED `4 failed, 1 passed` → GREEN `5 passed`; regression 29 + 8 passed; ruff, mypy (383 files), architecture green |
| T10 | — | spec 04 rule counts flipped 19 → 21; debt spec closed with an Outcome section; task doc closed; Engram anchor updated |
| T7 | — | fresh backup `bookgraph_backup_20261002T131026Z.json` (175 MB) + production dry-run: 286 edges read, **286 excluded as `cycle`**, 0 to re-point → **maintainer decision required** |
