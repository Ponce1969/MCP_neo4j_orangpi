# ODD feature: cross-namespace semantic resolution (Block A)

- **Branch**: `feat/cross-namespace-semantic-resolution` (from `main` @ `e5a7aad`)
- **Design**: `odd/specs/cross-namespace-semantic-resolution-design.md` (normative for this feature)
- **Engram**: `odd/cross-namespace-semantic-resolution/tasks` + `design/cross-namespace-semantic-resolution`
- **Opened**: 2026-10-03
- **Decisions approved by the maintainer (2026-10-03)**: D-A1 three-way model, `EQUIVALENT_TO` discarded;
  D-A2 namespace guard inside `ApplyMergeUseCase`; D-A3 R5a first for visibility, then use it to audit the 302
  applied merges before any new merge; D-A4 R5a warning, R5c blocking, R5b only after the 64 case-only groups are
  resolved.

## Goal

Make cross-namespace duplicates **visible**, **reviewable** and **safe**: close the policy bypass, give the
maintainer a decision sheet per candidate, and merge only what the evidence supports — never by label alone.

## Ground truth (measured 2026-10-03, production, read-only)

456 candidate groups / 941 entities · 302 cross-namespace merges already applied (31% of the 958 ledger entries)
· 302 entities already multi-book · 64 case-only groups / 128 entities · generic labels colliding across three
namespaces (`llm`, `agent`, `evaluation`, `embeddings`, `human`, `modularity`, `adapters`, `event`, `fine-tuning`).

## Execution order (why this order)

1. Visibility first (R5a): without a rule, nothing measures progress and the retro-audit has no tool.
2R1 (rollback direction) must land **before any apply**, because cross-namespace merges have the largest inverse
maps and debt R1 corrupts exactly that population.
3. The guard must land **before** the first new merge, so the bypass cannot be used again.
4. R5b (case-insensitive) changes existing totals, so it waits for the 64-group cleanup (D-A4).

## Tasks

- [ ] **T1** Branch + this document + Engram mirror + visible todo list (before the first source write)
- [x] **T2** Read-only scoring render of a representative sample of the 456 groups
      (`scripts-ops/render_cross_namespace_sample.py`, 722 lines): reuses `mentions_jaccard`, `related_jaccard`,
      `description_overlap`, the S0 initial-evidence builder and the S2 type gate — no scoring logic of its own.
      Deterministic stratified sample (all single-word labels, highest-degree pairs, seeded type mix); console
      decision sheet + full JSON payload. **Ran against production, read-only**: 456 groups found, 212 rendered,
      242 pairs, evidence in `evidence-bundles/cross-namespace-sample-render-20261003.json`
      (sha256 `eb5da6995248a5b03944b899…`). Finding: `mentions_jaccard` fires on 3/242 pairs and
      `related_jaccard` on 9/242 (both structurally ≈0 pre-merge), `description_overlap` on 113/242, and the mean
      composite reaches ≥0.50 on **0/242** — see design §4.1.
- [ ] **T2b** Fix the cross-namespace evidence model (design §4.1): drop the three-signal mean composite and the
      `high_context` threshold from the cross-namespace path, lead the sheet with description overlap, mark
      `mentions_/related_jaccard` as uninformative for proposing a merge (keep them for re-auditing an applied one),
      and add unit tests over synthetic pairs (same-language duplicate, cross-language duplicate, generic-label
      collision, already-merged pair). Re-render the same sample to show the corrected ranking before the
      retro-audit (T8).
      **Decision (maintainer, 2026-10-03)**: keep the renderer **lexical** for the whole population; embeddings
      (cross-lingual cosine) are an **optional** tool reserved for the shortlist of doubtful candidates the human
      picks — never a bulk pass over the 456 groups.
- [x] **T3** R5a audit rule `DUPLICATE_ENTITY_CROSS_NAMESPACE` (severity **warning**, category `duplicates`)
      with namespace-aware samples. Counts **groups** (the `duplicates_entity` convention: a group is a decision
      unit; the measured 941 entities live in 456 groups). Dedicated scoped branch: the generic injection would
      land before the grouping and report zero, so the scoped query keeps the cross-namespace condition and then
      keeps the groups with at least one member inside `$scope_prefix`. Samples carry both member ids plus the
      namespace list under `namespaces`, a key `safe_properties` does not redact (keys containing `source` are
      dropped). Catalog 21 → 23; sibling suite's hardcoded size bumped.
      **Coordination point for T9**: the rule groups in Cypher with `toLower(trim(name))` while the renderer groups
      with the stricter Python `normalize_key` (NFKC + whitespace collapse); unify them when R5b lands.
- [x] **T4** R5c audit rule for self-loops, named `ENDPOINT_SELF_LOOP_INVALID` so the `ENDPOINT_` prefix maps it to
      `endpoints` and `severity_for_category` makes it **blocking** with no bespoke mapping (the design draft's
      `ENTITY_SELF_LOOP_INVALID` would have needed a manual `RULE_CATEGORY` entry; recorded here as the deviation).
      Tests: seeded self-loop → 1, without it → 0, scoped variant → 0 outside the namespace.
      **Gate finding (verified in code, `application/evaluate_gate_use_case.py`)**: a WARNING never fails a required
      dimension — the evaluator derives `min_rank` from `max_severity` (blocking → 3) and skips lower ranks — so
      R5a's 456 groups appear in the report without turning the `uniqueness` dimension or the `expose-mcp` gate
      red. Only a blocking finding fails it, and self-loops are at zero today. This closes the open question of
      T9 about warnings.
- [x] **T5** Debt **R1** fixed (`merge_ledger_models.py`, `neo4j_graph_merge_adapter.py`, both suites): the inverse
      map now captures the RELATED orientation, apply re-points only it, and rollback restores exactly it; entries
      with `direction=None` (the 958 already in the ledger) keep the documented both-ways restore.
      **The investigation is the interesting part**: the chain digest is computed over the **canonical dump of the
      parsed model** (`merge_ledger_models.py:109`), so a plain defaulted field would have changed every existing
      entry and broken the chain. The fix is a `field_serializer` that **omits** the field when unknown, which keeps
      old lines hashing byte-identically while a known direction stays inside the authenticated payload (a tampered
      direction is rejected as `LedgerChainBroken`).
      **Verified against the real ledger, read-only**: 958 entries read with the new model, **0 recomputed-hash
      mismatches, 0 broken links**. Open cosmetic note: `schema_version` stayed `1.0.0` (per-line metadata, no
      consumer branches on it) — decide a bump policy when the next field lands.
- [ ] **T6** Quarantine review surface: `book-graph-rag quarantine list|render <seq>` (decision sheet: names, types,
      descriptions, pages, mentions per book, shared neighbours, the three overlaps, composite) and the evidence
      enrichment (neighbour-overlap sample) in the quarantine record.
- [x] **T7** **Guard implemented** in `ApplyMergeUseCase` (commits with T6/T7): new frozen domain credential
      `MergeApproval` (`quarantine_seq`, `approved_by`, `approved_at`, `canonical_id`, `candidate_ids`) and
      `CrossNamespaceApprovalRequired`. The guard runs **before the band check and before any port**: it derives the
      namespaces with the public `namespace_from_id` and, if any duplicate crosses, demands an approval whose
      canonical matches and whose candidate set covers **exactly** the crossing candidates (missing or extra is
      refused). A same-namespace group **ignores** a supplied approval (documented: rejecting would let a stale
      approval block a legitimate merge and cannot widen the empty crossing set). Discovered nuance, recorded as
      policy: ids without a colon share a namespace-less bucket, so only both-unqualified ids compare equal;
      qualified-vs-unqualified is a crossing and fires. `ApproveQuarantineUseCase` builds the credential from the
      record it approves and refuses to apply when `canonical_id` is `None` — before it mutates the record.
      The bypass script `scripts-ops/resolve_cross_namespace.py` is now disabled by design with a message pointing at
      the quarantine flow. Tests: 14 unit (stub ports, including the historical exact-band bypass and the
      wrong/missing/extra candidate cases) + 2 hypothesis properties (no band and no forged evidence crosses).
      **Remaining part of T7** (the `quarantine approve` command itself, behind the AGENTS.md §7.2 gate with explicit
      `--seq`) ships with the T6 CLI surface, whose mockup is awaiting the maintainer's visual approval.
- [ ] **T8** Retro-audit of the 302 applied merges with the T2 renderer + R5a, stratified by label genericity;
      read-only report first, then per-case human decisions (keep / rollback via ledger).
- [ ] **T9** R5b: case-insensitive grouping in `duplicates_entity` via `normalize_key`, together with the cleanup of
      the 64 case-only groups (approval-gated merge), and the confirmation of how the `uniqueness` gate dimension
      treats warnings.
- [ ] **T10** Cleanup batch for the approved identity merges (apply, scoped + global audits, multi-book count check),
      docs (spec 03 amendment, spec 04 rules, AGENTS.md §7.2 cross-ref) and close-out (commits, Engram, report).

## Rules in force

1. **No graph mutation** until T7's guard and the approval CLI exist; every mutation goes through the §7.2 gate.
2. Reuse the domain scoring (`domain/s3_context_scoring.py`, `s4_band_assignment.py`, `s0_normalization.py`,
   `s2_type_gate.py`); never reimplement thresholds or signals.
3. The routing decision for a cross-namespace pair is **always quarantine** (spec 03 §2.4 / policy R6.2); the
   scoring exists to give the human evidence, not to auto-merge.
4. English artifacts; Conventional Commits; work-unit commits; `uv`; the pre-commit hook runs
   ruff/mypy/architecture validation.
5. Never print secrets; `.env` is now `600` and the service reads it through its unit's `EnvironmentFile`.

## Evidence log

| Task | Commit | Evidence |
|------|--------|----------|
| T1 | — | branch `feat/cross-namespace-semantic-resolution`; this document; Engram mirror |
| T2 | `refactor` + `feat(ops)` commits | renderer 722 lines; production render read-only; 456 groups / 212 rendered / 242 pairs; JSON evidence sha256 `eb5da6995248a5b03944b899…`; 567 unit tests green, ruff/mypy/architecture green |
| T3+T4 | (this commit) | RED `7 failed` → GREEN `7 passed` (80 s) + regression `34 passed`; catalog 23; ruff/mypy/architecture green |
| T5 | (this commit) | hash investigation with quoted lines; RED unit `5 failed` / integration `4 failed, 6 passed` → GREEN `5 passed` + `10 passed`; real ledger read-only: 958 entries, 0 mismatches, 0 broken links |
