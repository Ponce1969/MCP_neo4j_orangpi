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
- [x] **T2b** Cross-namespace evidence model unified (design §4.1): the shared domain model is now the only one —
      `scripts-ops/render_cross_namespace_sample.py` imports `reading_for`, `label_risk`/`format_risk_marker` and the
      primary/structural signal constants, and keeps no threshold or reading logic of its own, so the audit, the CLI
      and the ad-hoc analysis cannot drift apart. The headline reading grades **`description_overlap`**, not the mean
      composite, and the payload declares which signal is primary (schema `/1` → `/2`). Grouping expression untouched:
      the Python `normalize_key` here versus Cypher `toLower(trim(name))` in the audit and CLI stays documented as the
      **T9** coordination point, with both currently yielding 456 groups.
      **Anti-drift**: `tests/unit/test_render_cross_namespace_sample.py` (7 tests, RED `6 failed, 1 passed` → GREEN)
      parses the script's AST to assert it imports the shared model, that no threshold literal (`0.50`/`0.10`) or
      retired reading vocabulary survives anywhere in its source, and that the payload equals the shared model's
      output — re-divergence fails statically and numerically.
      **Re-render of the same production sample (2026-10-03, evidence
      `evidence-bundles/cross-namespace-sample-render-unified-20261003.json`)**: 456 groups, 212 rendered, 242 pairs,
      and the distribution moved from *strong/identity 0 · undecided 6 · no context 236* to **identity 1 · undecided
      42 · no context 199** — the composite was drowning the signal in 36 pairs that are now flagged for reading.
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
- [x] **T6** Quarantine review surface. **T6a**: `quarantine list` (filters `--all/--band/--namespace/--limit`,
      **`--generic-only`**, `--json`) and `quarantine render` (`--seq` for a record or **`--pair`** for two ids, which
      the retro-audit of the 302 needs), over a read-only port + Neo4j adapter, with pure domain sheet models and a
      pure formatter. The sheet carries the **mention context** snippet, the three overlaps with the structural
      labels, the shared neighbours and the ledger history, and it never claims a band or a cosine.
      **T6b**: `enqueue --cross-namespace [--limit/--generic-only/--namespace/--dry-run/--force/--json]` (the producer:
      nothing wrote pending records for this class before), `approve --seq … --backup … --approval … [--reviewer]`
      behind the §7.2 gate (validated before any write, explicit `--seq` only, never "all") and
      `reject --seq … --reason` (mandatory non-empty, stored in the new `review_note` field, omitted from the JSONL
      when unset so the existing file stays byte-compatible). Idempotence: a pair that already has a record — pending,
      approved or rejected — is skipped unless `--force`, so a rejected pair does not come back on its own.
      Band rule fixed: a cross-namespace pair short-circuits to `exact` at S0, so a record crossing namespaces is
      accepted on that ground regardless of band (R6.2). Tests: 21 + 5 (T6a) and 25 + 2 (T6b) green, CLI regression
      33 green.
      **Real production run (2026-10-03)**: `enqueue --limit 20` found **456 groups / 514 candidate pairs**, wrote
      **20 pending records (seq 1–20)** into a newly created `data/resolution/quarantine.jsonl`, evidence-ordered
      (`description_overlap` 0.636 down to 0.240). No graph mutation.
- [x] **T6c** Two ergonomics refinements the real enqueue exposed: (a) the risk marker combines single-word labels
      with namespace coverage (high = single word **and** ≥3 namespaces, medium = exactly one of the two, with the
      reason printed) and gains a `--risk` filter while `--generic-only` keeps its approved meaning; (b) `render`
      cross-references the **sibling records** of the same group with the composed `approve --seq …` command, so a
      3-member group is decided once and applied together, and `render --pair` reports an existing pending record
      instead of always `sin registro`.
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
- [~] **T8** Retro-audit of the 302 applied merges.
      **T8a done**: read-only tool `scripts-ops/audit_applied_cross_namespace.py` (ledger-driven selection, graph
      recompute through the shared model, matrix + lists; 8 unit tests). First live run: 302 entries / 322 pairs,
      **every selected entry stored `band=exact` with `s3 is None`** (the bypass left no scoring at all), strong 0,
      ambiguous 143, none 156, silent 23; suspicious 176.
      **T8b done**: language-aware strata with a conservative corpus-scoped detector in the shared model (weak, mixed
      or short evidence → `unknown`, never a guess) and the same caveat on the operative sheet. Authoritative run
      over full descriptions: **0 cross-language pairs**, 18 silent *same-language* (they moved to suspicious:
      176 → **194**) and 5 of unknown language (**needs_reading 23 → 5**). So the silence was never a language
      artefact: those pairs disagree conceptually.
      **Cosine calibration done** (T8b's follow-up, `scripts-ops/calibrate_cross_namespace_cosine.py`, local
      multilingual model, 20 stratified pairs / 40 embeddings, evidence
      `evidence-bundles/cosine-calibration-20261003.json`, sha256 `4d1ca37ef3149d6de665a0ca`):
      matrix → strong 0 · medium_cosine 1 · low_cosine 19; verdicts → **`cosine rescues it` 0**, `confirms the doubt`
      19, `agree` 1. Headline: of the 15 sampled pairs with no lexical evidence, **none** reaches `high_cosine`, so
      the lexical model is **not** a false-negative factory for this population: most of those historical merges
      really joined different concepts (samples read: an insurance-domain `api-calls-tool` merged with a smart-home
      one; `model-component` about location errors merged with one about ambiguous phrasings). Catch worth keeping:
      the one clear identity in the sample (`knowledge-graph-concept`, both sides "structured representation of
      entities and relationships… GraphRAG") scores cosine **0.882** and lexical 0.381 — and the same-concept
      `docker-tool` pair ("Containerization platform used to deploy applications" vs "…to package Agent A, B, C into
      isolated runtime containers") scores 0.720, below the project's 0.80 `medium_cosine`: those thresholds were set
      for **name+alias** embeddings, not for full descriptions, so a description-based cosine needs its own band.
      **T8c (pending, needs the maintainer's go-ahead)**: the pilot. `RollbackMergeUseCase` exists but **no CLI
      exposes it** (the same gap `approve` had before T6b), so the pilot needs `book-graph-rag ledger rollback --seq …
      --backup … --approval …` reusing the §7.2 gate helper and printing a before/after edge census. Direction
      inference: these entries are **pre-R1** (`direction=None` in every inverse map, verified) and the legacy
      both-ways restore would rebuild mirrors — but the original direction is recoverable from the canonical's
      current edge for each inverse entry, so the tool should infer it and restore exactly one direction, falling
      back to both-ways only when no matching edge exists (and reporting that).
      **Short list for the pilot (indisputable wrong merges, cheapest first)**:
      | seq | canonical (live) | rolled candidate | inverse map | mirror exposure |
      |-----|------------------|------------------|-------------|-----------------|
      | 305 | `graphrag-agentic:api-calls-tool` | `agentic-patterns:api-calls-tool` | 1 MENTIONS + 1 RELATED | ≤1 |
      | 501 | `essential-graphrag:recall-concept` | `agentic-patterns:recall-concept` | 2 + 3 | ≤3 |
      | 342 | `graphrag-agentic:agent-b-agent` | `agentic-patterns:summarization-agent-b-agent` | 21 + 32 | ≤32 |
      Start with 305 and 501 (the cheap ones, where the restored edges and any mirror are countable by hand) and leave
      342 for a second phase. **Nothing is mutated until the maintainer approves the pilot.**
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
| T6a/T6b/T6c | `4cda365`…`6bec401` | CLI surface + real enqueue (20 pending records, graph untouched) + corpus risk marker and siblings |
| T2b | `d95cbc6` | renderer imports the shared model; AST anti-drift; re-render moves the sample from 6 to 43 readable pairs |
| T8a | (this commit) | read-only retro-audit tool; 302 entries / 322 pairs; strong 0 · ambiguous 143 · silent 23 · none 156; suspicious 176 |
