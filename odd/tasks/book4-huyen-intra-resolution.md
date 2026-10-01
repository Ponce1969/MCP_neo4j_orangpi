# Book 4 (Chip Huyen) — intra-namespace entity resolution + finish the full index

## Context

The full-PDF re-index of `knowledge:ai-engineering-huyen` (889-page ES edition of
*AI Engineering*, Chip Huyen) finished on the OrangePi on 2026-10-01 18:28 local:
**987 PROCESSED + 2 FAILED checkpoints** (chunks 967/968, LLM flakiness, not a code
defect). The scoped audit reports `passed` with **0 blocking / 0 incomplete**, and
the only warning with volume is **`DUPLICATE_ENTITY_LOGICAL` = 355 groups**.

Verification and measurements live in Engram (project `mcp_neo4j_orangpi`):
obs 1446 (index verified), obs 1447 (resolution dry-run), obs 1110 (ops anchor).

The 355 groups are EN<->ES synonyms of the same concept (the PDF is a translation),
same `name` + `type`, inside one namespace. Per AGENTS.md §7.2 a merge changes the
entity graph and therefore requires: backup -> dry-run -> explicit human approval.

Read-only dry-run already done (obs 1447): 355 groups / 765 entities / **410 duplicates
to fold** / 4.875 edges to re-point (1.488 MENTIONS + 3.387 RELATED) / 778 aliases.

## Contract

1. **Grouping criterion is not re-invented**: the planned groups must come from the
   audit's own rule (`neo4j_audit_adapter.py:53`, `duplicates_entity`) = non-merged
   entities with non-null id, grouped by exact `name` + `type`, scoped by
   `n.id STARTS WITH $scope_prefix`, `size(members) > 1`.
2. **Canonical = shortest entity id** (ties broken lexicographically), the recipe
   already used by `scripts-ops/resolve_full_intra.py` / `resolve_round2_intra.py`.
   Confirmed by the maintainer on 2026-10-01. Note: this differs from the audit's
   `members[0]` (lexicographic-first) in 176/355 groups.
3. **Merge mechanics stay inside `ApplyMergeUseCase`** (soft-delete `merged_into`
   + re-point MENTIONS/RELATED + fold aliases + chained ledger entry). No raw
   `DETACH DELETE`, no hand-rolled Cypher writes.
4. **Approval gate**: the script is dry-run by default; writing requires
   `--apply --backup <file> --approval <file containing "approve">`.
5. **Self-loop risk is measured, not silently created**: the merge adapter re-points
   with `MERGE (canon)-[r2:RELATED]->(other)` without excluding `other == canonical`,
   so an intra-group RELATED edge becomes a self-loop. Read-only measurement: exactly
   **1** such edge in this namespace ("LoRA cuantizado"|pattern). Deleting anything is
   a separate, explicitly approved step.

## Tasks

- [x] T1: pure domain module `domain/duplicate_grouping.py` (shortest-id canonical policy) with TDD RED->GREEN tests
- [x] T2: `scripts-ops/resolve_intra_ns.py` — generic namespace resolver: groups generated from the audit query, dry-run default, `--apply --backup --approval`
- [x] T3: local gates (ruff, mypy, validate_architecture) + work-unit commit on a feature branch
- [x] T4: Pi — retry the 2 FAILED checkpoints (chunks 967/968) and verify PROCESSED = 989
- [x] T5: Pi — fresh graph backup before any mutation
- [x] T6: Pi — dry-run with the real script (must reproduce 355 groups / 410 duplicates)
- [x] T7: Pi — `--apply` with approval file (410 merges, ledger seq 605+)
- [x] T8: Pi — scoped re-audit (expect `DUPLICATE_ENTITY_LOGICAL` 0, state `passed`) + integrity checks
- [x] T9: Pi — measure the self-loop(s) and report; cleanup only after explicit approval
- [ ] T10: close — Engram anchor + outcome report

**T6 deviation (measured, not assumed)**: the audit reported 355 groups before the retry and the
real dry-run reproduced 354 groups / 409 duplicates afterwards. Differential evidence: group
sizes 318x2 -> 317x2 (one 2-member group dissolved) and type mix concept 194->193,
pattern 53->54, risk 13->12. The 28 entities mentioned by the re-processed chunks 967/968 are also
mentioned by other chunks; the retry re-extracted them inside the same run. Hypothesis (not yet
proven, follow-up candidate): the resumable writer sets `name`/`type` last-write-wins, so a
re-extraction can mutate pre-existing entities. The 8 recorded `(name, kind)` pairs from obs 1447
were re-checked and are intact.

## Evidence

- Dry-run measurements (read-only, before the script existed): Engram obs 1447.
- Index verification: Engram obs 1446.
- **T1 + T2 + T3 (2026-10-01)**: commits `300de9e` (domain planner + 12 tests) and `74bc801`
  (ops runner) on branch `feat/book4-huyen-intra-resolution`, **not pushed**.
  Gates: ruff clean · mypy 376 files OK · `validate_architecture.py` OK · `ruff format` OK
  (pre-commit hooks passed on both commits) · `tests/unit` 527 passed.
- **T3 follow-up commit `36ca578`**: the intra-group RELATED metric now ignores soft-deleted
  members (it reported a stale 1 right after the apply).
- **T4 (Pi)**: resume re-index of the same PDF, `setsid` + stdin closed, PID 675976.
  Result: checkpoints `knowledge:ai-engineering-huyen` = **989 PROCESSED, 0 FAILED, 0 PROCESSING**.
- **T5 (Pi)**: `~/backups_neo4j/bookgraph_backup_20261001T225024Z.json` (167 MB) taken after the
  retry, before any mutation. Used as the `--backup` for the apply.
- **T6 (Pi)**: dry-run 354 groups / 409 duplicates / 1.486 MENTIONS + 3.378 RELATED to re-point /
  1 intra-group RELATED. Delta vs the pre-retry audit explained above.
- **T7 (Pi)**: canary `--limit 3` -> seq 605-607, 0 failures (verified separately: 14 duplicates
  soft-deleted, 0 MENTIONS left pointing at them, aliases folded). Then the full run:
  **351 merges, 0 failures, APPLY_OK** -> ledger seq 608-958. Total **354 merges / 409 duplicates**.
- **T8 (Pi)**: scoped audit `state=passed`, **blocking 0 / warning 0 / incomplete 0**,
  `DUPLICATE_ENTITY_LOGICAL = 0`. Inventory: 1 Book · 10 Chapters · 125 Sections · 989 Chunks ·
  6.963 active Entities · 12.664 MENTIONS · 12.991 RELATED. **Global audit: passed 0/0/0** (no
  regression in the other three books). Ledger chain reads back clean: 958 entries, 354 of them in
  this namespace. `merged_into` dangling targets 0. MENTIONS to merged entities inside this
  namespace 0. Re-run dry-run: **0 groups** (idempotent).
- **T9 (Pi)**: self-loops unchanged (5 graph-wide, 2 in this namespace, both LLM-generated during
  the index; the merge added none). One RELATED edge now points at a soft-deleted duplicate
  (`flop-s-concept` <- `operaciones-de-punto-flotante-por-segundo-concept`): the intra-group edge the
  adapter cannot re-point onto a different endpoint. Pre-existing debt measured in
  `knowledge:essential-graphrag` (September resolution): 90 MENTIONS + 196 RELATED edges touching
  merged entities. Nothing deleted.
