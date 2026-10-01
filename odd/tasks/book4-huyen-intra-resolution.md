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

- [ ] T1: pure domain module `domain/duplicate_grouping.py` (shortest-id canonical policy) with TDD RED->GREEN tests
- [ ] T2: `scripts-ops/resolve_intra_ns.py` — generic namespace resolver: groups generated from the audit query, dry-run default, `--apply --backup --approval`
- [ ] T3: local gates (ruff, mypy, validate_architecture) + work-unit commit on a feature branch
- [ ] T4: Pi — retry the 2 FAILED checkpoints (chunks 967/968) and verify PROCESSED = 989
- [ ] T5: Pi — fresh graph backup before any mutation
- [ ] T6: Pi — dry-run with the real script (must reproduce 355 groups / 410 duplicates)
- [ ] T7: Pi — `--apply` with approval file (410 merges, ledger seq 605+)
- [ ] T8: Pi — scoped re-audit (expect `DUPLICATE_ENTITY_LOGICAL` 0, state `passed`) + integrity checks
- [ ] T9: Pi — measure the self-loop(s) and report; cleanup only after explicit approval
- [ ] T10: close — Engram anchor + outcome report

## Evidence

(filled per task as the work unit closes)
