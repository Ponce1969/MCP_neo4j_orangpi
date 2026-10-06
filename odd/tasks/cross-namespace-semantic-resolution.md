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
- [x] **T8** Retro-audit of the 302 applied merges.
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
      **T8c done and the pilot applied (2026-10-04)**: `book-graph-rag ledger rollback` (commits `f9737b7`/`5c9ed12`)
      is read-only by default, infers the RELATED orientation from the canonical's live edge for entries written
      before the direction field, prints the predicted census and a **fingerprint** of the plan, and its `--apply`
      carries the §7.2 gate plus `--expect-fingerprint` (refuses when the recomputed plan differs from the reviewed
      one). It reuses the untouched `RollbackMergeUseCase` by handing it the entry copy whose directions were filled in.
      Applied to **seq 305** (`api-calls-tool`) and **seq 501** (`recall-concept`) with backup
      `bookgraph_backup_20261004T062517Z.json`: `mentions restored 3 (predicted 3)`, `related restored 4 (predicted
      4)`, **`mirrors created 0 (predicted 0)`**, `merged_into 931 → 929`, `drift: none`.
      **Raw verification**: all four nodes live again; each side's degree arithmetic balances exactly
      (`graphrag-agentic:api-calls-tool` 2/2 → 1/1 while its loser regained 1/1; `essential-graphrag:recall-concept`
      5/7 → 3/4 while its loser regained 2/3); the four restored edges appear **once each in their original
      direction** (zero mirrors); the ledger is at **960 entries with 0 hash inconsistencies**; and the four audits are
      `passed` (global 0 blocking / 457 warnings). The restored neighbourhoods corroborate the verdict — the revived
      `api-calls-tool` is enabled by its own book's `smart-home-agent-agent`, and the revived `recall-concept` relates
      to `stepscore-pattern` and `f1-score-concept`.
      **Why R5a moved 456 → 457 (groups, not pairs)**: the revived `api-calls-tool` created the `api calls|tool` group
      (2 namespaces), while `recall|concept` **already existed** with three namespaces and merely gained a fourth
      member. Verified with an independent query that also returns 457 groups, matching the rule.
      **seq 342 remains for the next pass** (maintainer decision).
      **New debt found by the pilot**: the audit **does not consult the quarantine decisions**, so a pair the
      maintainer rejected keeps counting in the warning total forever. R5a (or its successor) should subtract pairs
      whose quarantine record is `REJECTED`, otherwise the metric never reaches zero and stops being a metric.
      **Lote 1 closed (2026-10-04)**: **10 of the 302** cross-namespace merges rolled back (pilot 305/501, clean
      subset 536/391/307/360/404, final 584/342/443) with the rollback tool's direction inference. 0 mirrors in 8 of
      them; the other 2 (342 `agent b`, 443 `generation`) keep **2 symmetric intra-book edges each, accepted by the
      maintainer** (option (a): keeping different concepts merged is worse than tolerating 2 symmetric edges).
      Verification: shared neighbours **0** on the reverted pairs, 47/47 mentions restored, every entity alive again,
      ledger **968 entries / 0 hash inconsistencies**, global audit `passed` (0 blocking). The rollback restored 8
      RELATED edges into already-merged endpoints; the audit rule `ENDPOINT_RELATED_MERGED_INVALID` flagged them as
      8 blocking and `scripts-ops/repoint_merged_endpoint_edges.py --mode repoint` closed them (0/0).
      **Tools matured in the process**: `both` fallbacks are resolved by **provenance inference** (the ledger's
      inverse map keeps the loser's `chunk_index`/`source_page` and the re-point copied them to the canonical);
      `unknown` fallbacks are an honest limit (the canonical's edge was already deleted).
- [x] **T8d** Two measurement fixes the real batches exposed.
      (a) The rollback census predicted one edge per `(other, type)` pair, but a bidirectional original stores two
      inverse entries with different chunk indexes, so the restore faithfully recreated both and the census counted
      the second as a mirror. It now groups by pair and counts the union of observed directions.
      (b) The retro-audit selected the compensating entries the rollback appends (also cross-namespace), so its guard
      aborted with 312 against the 302 ground truth; it now separates them, keeps the compensated originals flagged
      as rolled back, and stratifies only the still-applied population.
      Evidence: `evidence-bundles/applied-cross-namespace-audit-consolidated-20261004.json` — 968 entries / 302
      cross-namespace / 10 compensating / **292 still applied**; suspicious **185**, needs_reading 5, high risk 7;
      live global audit `passed` (blocking 0 / warning 465).
- [~] **T8e** Lote 2: roll back the remaining suspicious pairs in review batches.
      Flow: cosine ascending shortlist (join `cosine-all-pairs` with the consolidated suspicious list) -> human picks
      a batch -> read-only decision sheets (`quarantine render --pair`) -> maintainer keep/rollback per pair ->
      `ledger rollback` dry-run with fingerprint -> §7.2 gate (fresh backup + approval + `--expect-fingerprint`) ->
      post-apply census + shared-neighbour check + scoped/global audits.
      **Batch 2A closed (2026-10-04)**: maintainer picked `cosine < 0.25` (6 entries). Rollback applied to **375**
      `Automation`, **489** `Peer Review`, **503** `redundancy`, **480** `Node` (multi-candidate); **kept** 413
      `Databases` and 475 `Microservices` (same concept, different phrasing). Two applies: the three single-candidate
      entries (fingerprint `714dfd57…`, 0 mirrors) and **480** alone (fingerprint `4b89c961…`, **1 accepted mirror** —
      an `unknown` fallback whose canonical `composes` edge was already gone). Census: MENTIONS 12/12, RELATED 20/20,
      1 mirror (predicted), `merged_into` 921 -> 916, drift none. Ledger **972 entries / 0 hash inconsistencies**.
      All revived entities alive; shared neighbours **0**. The 480 revival put 2 `composes` edges on a merged endpoint
      -> audit `violations` with 2 blocking in essential-graphrag; `repoint_merged_endpoint_edges.py --apply` closed
      them (2 edges, 0 collapses) and the global audit is back to **`passed` (blocking 0 / warning 469)**.
      Backups: `bookgraph_backup_20261004T162259Z.json`, `…T162618Z.json`, `…T163031Z.json`.
      Evidence: `evidence-bundles/batch2a-decision-sheets-20261004.txt`,
      `evidence-bundles/lote2a-rollback-20261004.json`.
      **Batch 2B plan A closed (2026-10-04)**: the 16 pairs of the recommended batch (cosine < 0.30, excluding the
      pairs decided in 2A and the retained `Prompt`) were reviewed; **6 kept as identity** (569 GraphRAG, 572
      Hallucination, 351 ChatGPT, 494 Policy, 573 Instructions, 565 extract_entities) and **9 rolled back** (**492**
      Planning, **455** Indexing, **423** Edge, **583** Normalization, **532** Summarization, **491** Pipeline, **347**
      User, **537** Timestamp, **451** high latency). Fingerprint `b53a925d…`; census MENTIONS 21/21, RELATED 41/41,
      **1 accepted mirror** (an `unknown` fallback on 455), `merged_into` 916 -> 907, drift none; ledger **981 entries
      / 0 hash inconsistencies**; all 18 entities alive; shared neighbours **0**. The mirror put 2 `requires` edges on
      a merged endpoint -> `repoint --apply` closed them (2 edges, 0 collapses) -> global audit **`passed` (blocking 0
      / warning 477)**. Backups `bookgraph_backup_20261004T231028Z.json`, `…T231230Z.json`.
      **Deferred in 2B**: `seq 508` `Retrieval` is the first partial rollback (revert
      `agentic-architectural-patterns:retrieval-concept`, keep the `essential-graphrag` sibling at cosine 0.560), but
      the production checkout is `main` @ `56ca06c` (9 behind `origin/main`) and carries no T8f code, so the partial
      waits for the branch deploy. Queue after 2B-A: **171 suspicious** (298 stratified), silent_same_language 9,
      needs_reading 5, high risk 7. Evidence: `evidence-bundles/batch2b-decision-sheets-20261004.txt`,
      `evidence-bundles/applied-cross-namespace-audit-consolidated-after2b-20261004.json`,
      `evidence-bundles/lote2b-plan-20261004.json`.
      **Batch 2C closed (2026-10-04)**: short tier `cosine < 0.32`, four pairs reviewed. Rolled back **590** `Question`
      and **319** `Jupyter notebook` (fingerprint `6405aa70…`: MENTIONS 4/4, RELATED 5/5, **0 mirrors**, no repoint
      needed, `merged_into` 906 -> 904, drift none); **kept** 403 `Controller` and 512 `Retry` (same concept — the
      blackboard controller and the R⁵ model are confirmed by the canonical's neighbours). All four decisions were
      recorded through the new `decisions record` CLI (which refused a candidate-id typo before appending — the
      fail-closed guard working). Verification: shared neighbours **0**, ledger **984 / 26 compensating / chain OK**,
      all scoped audits `passed`. **T9a demonstrated live**: the two rollbacks revived two duplicate pairs that would
      have pushed R5a from 456 to 458, and the `separate` decisions held it at **456**; the queue went **162 -> 158**
      with `decided_keep_pairs: 10`. Evidence: `evidence-bundles/batch2c-decision-sheets-20261004.txt`,
      `evidence-bundles/cross-namespace-decisions-20261005.jsonl` (the registry snapshot: 37 decisions),
      `evidence-bundles/applied-cross-namespace-audit-consolidated-after2c-20261004.json`. Backup
      `bookgraph_backup_20261005T032656Z.json`.
      **Batch 2C-B closed (2026-10-04)**: the next tier `0.32 <= cosine < 0.35`, six pairs. Rolled back **371**
      `Alignment` (ontology mapping vs alignment with intended goals) and **415** `Decomposition` (DAG problem
      decomposition vs a chain-of-thought prompt mutation) with fingerprint `4e386d98…`: MENTIONS 6/6, RELATED 6/6,
      **0 mirrors** (no repoint), `merged_into` 904 -> 902, drift none; kept 355 `GPT-4o`, 332 `OpenAI`, 551 `Workflow`
      and 526 `Stability` (same entity or concept, cosine low). All six decisions recorded through the CLI (registry now
      **43**: 29 separate / 14 keep). Verification: both pairs alive, shared neighbours **0**, ledger **986 / 28
      compensating / chain OK**, global audit `passed` (R5a held at **456** by the separate decisions, R5b 64). Queue
      **158 -> 152** (279 stratified). Evidence:
      `evidence-bundles/batch2cb-decision-sheets-20261004.txt`,
      `evidence-bundles/applied-cross-namespace-audit-consolidated-after2cb-20261004.json`,
      `evidence-bundles/cross-namespace-decisions-20261005.jsonl` (the 43-decision snapshot). Backup
      `bookgraph_backup_20261005T055151Z.json`.
      **Batch 2C-C1 closed (2026-10-05)**: the tier `0.35 <= cosine < 0.40` holds **24 pairs** (not the ~11 estimated
      before the queue moved), so it was split at `0.37`: this batch is the first 7. Rolled back **502**
      `Recommendation` (a graph-database recommendation workload vs the system ability to suggest) with fingerprint
      `d1c5e113…`: MENTIONS 1/1, RELATED 3/3, **0 mirrors** (no repoint), `merged_into` 902 -> 901, drift none. Kept
      323 `LangChain`, 350 `APOC`, 382 `Branching`, 543 `Tools`, 544 `Traceability` and 431 `Evaluation` (same
      framework/tool/concept at a higher cosine — six keeps, the opposite ratio of the low tiers). Seven decisions
      recorded (registry now **50**: 30 separate / 20 keep). Verification: both entities alive, shared neighbours **0**,
      ledger **987 / 29 compensating / chain OK**, global audit `passed` (R5a held at 456, R5b 64). Queue
      **152 -> 145** (272 stratified). Evidence: `evidence-bundles/batch2cc1-decision-sheets-20261005.txt`,
      `evidence-bundles/applied-cross-namespace-audit-consolidated-after2cc1-20261005.json`.
      **Branch deployed to the production host + 2B-B partial closed (2026-10-04)**: the host's dirty tree (33 files
      staged from an older feature snapshot + 1 modified + 7 untracked) was snapshotted into the throwaway branch
      `host-dirty-backup-20261004` (`bc1f358`, patches in `/tmp/host_staged_before.patch`), keeping the previously
      existing stashes; the local branch `feat/cross-namespace-semantic-resolution` was then checked out at `174e18d`
      (the host's `main` @ `56ca06c` is an ancestor of the branch, so nothing from main was removed; dependencies are
      identical, so no `uv sync`; the gitignored ledger was never at risk) and the 7 untracked files were restored
      from the backup branch. The MCP service was deliberately **not** restarted. Then `ledger rollback --seq 508
      --candidate agentic-architectural-patterns:retrieval-concept` (fingerprint `d84a8a48…`) applied the **first
      partial rollback in production**: census MENTIONS 2/2, RELATED 5/5, 1 accepted mirror (the same `unknown`
      fallback family on `vector-database-component`), `merged_into` 907 -> 906, drift none; the reverted candidate is
      alive while the **non-selected sibling stays `merged_into`** and the compensating entry 982 records only the
      subset. `repoint --apply` closed the 2 blocking edges the mirror created; global audit **`passed` (0 blocking /
      477 warnings)**. The candidate-aware audit reports its first partial: `full 23 · partially_compensated 1 ·
      compensated_pairs 25 · stratified 297 · suspicious 170`. Evidence:
      `evidence-bundles/applied-cross-namespace-audit-consolidated-after508-20261004.json`. Backups
      `bookgraph_backup_20261004T232545Z.json`, `…T232650Z.json`.
      **Remaining**: **180 suspicious pairs** (185 - the 5 rolled-back pairs), needs_reading 5, high risk 7, silent_same_language 14; consolidated queue
      `evidence-bundles/applied-cross-namespace-audit-consolidated-after2a-20261004.json` (972 entries / 302 cross-namespace /
      14 compensating / **288 still applied**). `seq 496` `Prompt` still deliberately retained.
- [x] **T8f** Partial rollback by candidate (`ledger rollback --candidate ENTITY_ID`).
      **Delivered (commit `f7fb0ef`)**: `RollbackEntryPlan.selected_candidates`, the selection-aware
      `build_entry_plan`, the plan-time overlap refusal, the CLI option (repeatable, exactly one `--seq`, partial `n/m`
      report, `next:` hint carrying the flags), the candidate-aware idempotence plus the same refusal in
      `RollbackMergeUseCase` as the backstop, and the alias-value collision refusal. Independently verified: the
      partial-then-full overlap can no longer mutate (plan-time refusal, proven end to end) and full-rollback
      behaviour is equivalent with `selected_candidates=None`.
      **Why (finding from batch 2A)**: a ledger entry can hold **several candidates** (`seq 480` = 2 losers) but
      `RollbackMergeUseCase.rollback(seq)` reverses the whole entry, so a distinct concept merged next to an identity
      one cannot be separated without reviving both and then re-merging (or repointing) the identity one. **16 of the
      remaining suspicious seqs hold 2 pairs**, so this recurs in every remaining batch.
      **Design**:
      - `RollbackEntryPlan` gains `selected_candidates: list[str] | None = None` (`None` = the whole entry, the current
        behaviour).
      - `build_entry_plan(entry, observations, selected_candidates=None)`: validate the selection as a non-empty subset
        of `entry.candidate_ids` (order taken from the entry; a non-candidate raises `ValueError`) and, when partial,
        filter `edge_inverse_map` by `duplicate_entity_id`, `aliases_folded` by `from_entity_id`, `candidate_ids` and
        `affected_entities`. Inferences and the predicted census cover only the selection.
      - `PlanRollbackUseCase.plan(seqs, candidates=None)`: when `candidates` is given, require exactly ONE seq (else
        `RollbackTargetInvalid`) and probe only the selected candidates' RELATED entries.
      - CLI `--candidate ENTITY_ID` (repeatable): requires exactly one `--seq` (else `click.UsageError`, exit 2); the
        dry-run marks the entry `partial (n/m)` and the `next:` hint repeats the `--candidate` flags so `--apply`
        carries the same selection (and therefore the same fingerprint).
      - `RollbackMergeUseCase.rollback` idempotence becomes **candidate-aware**: an existing compensating entry for
        `seq` covers only the candidates it recorded, so the no-op happens only when the union of prior compensations
        covers the requested set; a complementary partial rollback of the same seq proceeds normally.
      - The compensating entry records the selected `candidate_ids`, the filtered map and the filtered aliases, so the
        ledger stays append-only and chain-verifiable.
      - **No adapter change**: `Neo4jGraphMergeAdapter.rollback_merge(entry)` already reverses exactly the entry it is
        given (`edge_inverse_map` + `candidate_ids` + `aliases_folded`).
      - **Overlap rule (fail closed at plan time)**: `PlanRollbackUseCase.plan` unions the `candidate_ids` of every
        prior compensating entry for the seq and raises `MergeNotReversible` BEFORE any probe when the request
        overlaps that set (naming the overlap and the remaining `--candidate` flags). Without it, a full `--seq` after
        a partial A would re-reverse A, append a second compensating entry claiming A again and only fail later in
        `compare_census` — after the graph was mutated. The same refusal lives in `RollbackMergeUseCase` as the
        backstop for a direct drive or a ledger that changed between plan and apply, so both layers enforce one rule.
      - **Alias-value collision (fail closed)**: `build_entry_plan` refuses a partial selection whose folded alias
        VALUE also appears under a non-selected candidate of the same entry. The ledger records folded aliases by
        value and `_ROLLBACK_REMOVE_ALIASES` removes them from the canonical by value, so a partial rollback would
        delete the still-merged candidate's alias (data loss). Such entries can only be rolled back whole; the
        limitation is surfaced, never silent, and is a data-model constraint (T8f does not change the adapter).
      **Tests (TDD, RED first)**:
      - unit — the selection filters the model/census/aliases; a non-candidate is refused; selecting every candidate
        equals the full plan; the fingerprint differs from the full plan; `plan()` validates the single-seq rule.
      - unit — idempotence: partial A then partial B proceeds; partial A then partial A is a no-op; a partially
        overlapping direct request is refused without touching the graph.
      - unit — `plan()` refuses an overlapping request before probing (the probe port is never called) and proceeds
        for the complementary candidate; the alias-value collision is refused while whole-entry and all-selected plans
        stay lossless.
      - CLI — `--candidate` without exactly one `--seq` exits 2; an unknown candidate exits non-zero before any write.
      - integration (testcontainers, `neo4j_integration`) — a 2-candidate entry: roll back A only (A alive, B still
        merged, canonical alive, compensating entry `[A]`, chain valid), a following full `--seq` refused in BOTH
        dry-run and `--apply` with the ledger and B untouched, then B (all alive), then the backstop no-op driven
        directly (no second compensating entry).
      **Notes**: adding a field to `RollbackEntryPlan` changes every plan fingerprint; the guard fails closed, which is
      the correct behaviour (a reviewed plan must be re-reviewed after a tool change). The consolidated retro-audit in
      `scripts-ops/audit_applied_cross_namespace.py` still treats a partially compensated entry as fully rolled back,
      so **T8f.2** is required before the next batch uses partial rollbacks.
- [x] **T8f.2** Candidate-aware compensation in the consolidated retro-audit.
      **Delivered**: `_compensated_candidates` (seq -> union of the compensated candidate ids), `_applied_entries`
      keeping an entry until every crossing candidate is compensated, `_build_pairs` flagging `rolled_back` per
      candidate, `_history_row(entry, compensated_candidate_ids)` reporting partial vs full, `_stratifiable_pairs`,
      and the guard changed from `applied + compensating == 302` to **`original population == 302`** plus
      `_assert_compensations_reference_originals` (unknown target, canonical mismatch, unknown candidate and duplicate
      compensation all abort with exit 2 before any graph query and before writing `--out`). The report and payload
      expose `fully_compensated_entries`, `partially_compensated_entries`, `compensated_pairs` and
      `rolled_back_candidates`. Read-only production run after the change: 302 originals / 14 compensating / 288
      applied / 15 compensated pairs / 322 pairs / 307 stratified / 180 suspicious — identical to the pre-change
      numbers. Independently verified; the verifier's counterexample (a count-only guard no longer catching a
      duplicate compensation) is closed by the reference check.
- [x] **T9a** Human-decision discount for the cross-namespace noise (approved 2026-10-04).
      **Delivered (commit `0bdf694`)**: `domain/cross_namespace_decision_models.py` (`DecisionKind keep|separate`, the
      frozen decision and the pure read model where the latest record per `(seq, candidate_id)` wins and
      `separate_entity_ids` resolves the latest record per ENTITY), `ports/cross_namespace_decision_port.py`,
      `infrastructure/jsonl_cross_namespace_decisions.py` (append-only `data/resolution/cross_namespace_decisions.jsonl`,
      semantic no-op), `Settings.cross_namespace_decisions_path`, the `decisions record|list` CLI (three refusals before
      any append; canonical only from the ledger; no graph access), the shared exclusion clause
      `AND NOT n.id IN $decided_separate_ids` in the R5a rule (global + scoped, passed only for that rule) reused
      byte-identically by `quarantine enqueue --cross-namespace`, and `scripts-ops/seed_cross_namespace_decisions.py`.
      **Production (2026-10-04)**: the seeder appended 33 of 33 decisions — **25 `separate`** derived from the ledger's
      compensating entries (seq 480 contributes two) and **8 `keep`** from the authored evidence — and a second
      `--apply` reported `no changes: all 33 decisions already recorded` (semantic no-op). Result: **R5a 477 -> 456**
      (exactly the 21 groups whose members are all rollback-revived; the 3 mixed groups keep warning, as designed) and
      the consolidated queue **170 -> 162 suspicious** with `decided_keep_pairs: 8` flagged (351/413/475/494/565/569/572/573),
      289 stratified. Global audit `passed` (blocking 0). Evidence:
      `evidence-bundles/lote2b-decisions-keep-20261004.json`,
      `evidence-bundles/applied-cross-namespace-audit-consolidated-after-t9a-20261004.json`. Independently verified; the
      verifier's gap (an exclusion keyed by entity but decisions by `(seq, candidate)`) is closed by the per-entity
      latest-wins rule, re-proved with the exact counterexample.
      **Why**: R5a (477 active groups) carries **21 groups whose members are ALL rollback-revived entities** — the human
      already judged those pairs as different concepts — and the consolidated queue still lists the **8 pairs kept** in
      2A/2B. `quarantine approve` cannot record a retroactive keep: it applies a merge (it would re-append the ledger
      and break the 302 guard); `reject` records a decision but means "do not merge".
      **Design** (no graph mutation):
      - `domain/cross_namespace_decision_models.py`: `DecisionKind` (`keep`/`separate`) and a frozen
        `CrossNamespaceDecision` (`seq`, `candidate_id`, `canonical_id`, `decision`, `decided_by`, `decided_at`,
        `reason` required and non-empty for both kinds, `batch`), plus the pure read model (`latest_by_key`,
        `separate_entity_ids`, `keep_pair_keys`, `decided_pair_keys`) where the LATEST record per
        `(seq, candidate_id)` wins.
      - `ports/cross_namespace_decision_port.py` + `infrastructure/jsonl_cross_namespace_decisions.py`: append-only
        `data/resolution/cross_namespace_decisions.jsonl` (`Settings.cross_namespace_decisions_path`); appending the
        same decision twice is a no-op; a different decision for the same key is allowed (the latest wins).
      - CLI `book-graph-rag decisions record --seq N --candidate ID --decision keep|separate --reason TEXT
        [--batch X] [--reviewer NAME]` — fail closed when `(seq, candidate)` is not a candidate of that ledger entry or
        the canonical does not match; no graph mutation and no §7.2 gate (like `quarantine reject`) — and
        `decisions list [--json]`.
      - **R5a**: the cross-namespace rule's Cypher excludes the `separate` entity ids from the grouping
        (`AND NOT n.id IN $decided_separate_ids`, always passed; an empty list is a no-op) so a group whose members
        are all decided stops warning while a group with remaining namespaces keeps warning. Applies to the global and
        the scoped variant.
      - **Anti-drift**: `quarantine enqueue --cross-namespace` uses the same duplicated grouping expression, so it
        gets the same exclusion (the commented invariant that both counts agree by construction must keep holding).
      - **Consolidated queue**: `scripts-ops/audit_applied_cross_namespace.py` excludes the `keep` pairs from the
        suspicious population and reports them as decided.
      - **Seeding**: `separate` is derived from the ledger's compensating entries (each records the reverted subset);
        the 8 `keep` pairs are authored from the batch evidence. A seeder validates every record against the ledger
        before appending.
      **Tests (TDD)**: domain read model (latest wins + aggregates); the JSONL adapter (append / no-op / invalid); the
      CLI (fail closed on a non-candidate, no graph write); the audit rule's parameter plumbing and filtering (unit
      with a fake session plus integration with testcontainers asserting R5a drops the decided group); the enqueue
      anti-drift; and the consolidator's keep exclusion.
- [x] **T9b** R5b: case-insensitive grouping in `duplicates_entity` (commit `82746d1`).
      **Measurement that drove the design (production, read-only, 2026-10-04)**: grouping active entities by exact
      `n.name` finds **0** intra-namespace groups; `toLower(trim(n.name))` finds **64 groups over 128 entities**; the
      Python `normalize_key` (NFKC + casefold + whitespace collapse) finds **the same 64 / 128**. So the Cypher
      expression is a faithful equivalent on this corpus (NFKC cannot be expressed in Cypher; a corpus with NFKC or
      internal-whitespace divergences would need a Python-side rule) — recorded in the adapter comment.
      **Change**: `duplicates_entity` now groups on `toLower(trim(n.name))`, the same expression R5a uses, and the
      comment's wrong claim that `total` counted entities was corrected (it counts GROUPS, as the integration test
      proves: a 2-member group reports 1).
      **Gate question answered empirically**: `DUPLICATE_ENTITY_LOGICAL` is a `duplicates` warning, and the
      `uniqueness` dimension only counts findings at or above `max_severity` (`blocking` in `gates.yaml`), so
      `expose-mcp` reports `passed: True` with `uniqueness: {finding_total: 0, satisfied: True}`.
      **Production after the change**: `DUPLICATE_ENTITY_LOGICAL` **0 -> 64**, R5a unchanged at 456, global warning
      total 456 -> **520**, audit `passed` 0 blocking. RED evidence: with the exact-name grouping the case-only pair
      forms **0** groups (`assert 0 == 1`); tests: the static grouping contract plus a testcontainers case. 808 unit + 26
      integration green.
      **Remaining (T10)**: the 64 groups (128 entities) are now visible and need the approval-gated merge cleanup.
- [x] **T10** Cleanup batch for the approved identity merges (commit `3d807e8` hardening, `5154506` sequential
      census; **applied 2026-10-05**).
      **Delivered**: `scripts-ops/resolve_intra_ns.py` hardened — the grouping is built from the shared
      `DUPLICATE_GROUP_KEY_EXPRESSION` (imported, the rule's query byte-identical), so the executor can no longer go
      blind after T9b; the canonical is chosen by **richness** (mentions → degree → shortest id → lexicographic,
      which disagrees with the historical shortest-id rule on 20 of the 64 groups); `--apply` requires
      `--expect-fingerprint` and REFUSES a missing or mismatched value before the first write; and the census predicts
      every loss the adapter performs (MENTIONS collapses, intra-group deletions, re-point collapses) by
      **simulating the plan order** over the pre-apply edge multiset (a replay proven confluent: the reversed plan
      yields the same losses).
      **Applied in three per-namespace batches**: `essential-graphrag` 7 groups (fingerprint `ed4ce4d0…`),
      `ai-engineering-huyen` 28 (`861662ba…`) and `graphrag-agentic` 29 (`30aa5e39…`) — **64 groups, 0 failures**,
      `merged_into` 901 -> **965**, active entities 20492 -> **20428**, ledger **1051** (64 new entries, all
      `band=exact`, 47 with folded aliases) with the chain verified.
      **Result: `DUPLICATE_ENTITY_LOGICAL` 64 -> 0**, R5a untouched at **456**, self-loops **0**, global warning total
      520 -> **456**, `expose-mcp` still `passed`. The third batch reported **`census drift: none`** (predicted
      RELATED -9, measured -9), validating the sequential simulation; the first two batches exposed the two
      unmodelled losses it now covers.
      **Docs**: spec 03 §2.3 (intra-namespace batch contract), spec 04 (the `DUPLICATE_ENTITY_LOGICAL` grouping and the
      corrected `total` semantics), AGENTS.md §7.2 (the batch gate).
      **Evidence**: `evidence-bundles/t10-lote1-plan-20261005.json`, `audit-after-t10-lote1-20261005.json`,
      `audit-after-t10-complete-20261006.json`. Backups `…T154049Z`, `…T195449Z`, `…T000524Z`.

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
| T8c/T8d | `f9737b7`…`b35ff12` | `ledger rollback` with direction inference + census + fingerprint; Lote 1: 10/302 reverted, 0 mirrors in 8, 8 symmetric edges accepted in 2; ledger 968 / 0 inconsistencies; consolidated audit 292 applied · 185 suspicious · global `passed` 0 blocking / 465 warnings; `repoint` closed the 8 restored blocking edges |
| T8e | (in progress) | 2A: 375/489/503/480 rolled back, 413/475 kept. 2B-A: 9 rolled back (492/455/423/583/532/491/347/537/451), 6 kept, 1 accepted mirror; shared neighbours 0; global audit `passed` after repoint. 2B-B: branch deployed to the host (`host-dirty-backup-20261004`), **first partial rollback** (`508` candidate `agentic-patterns:retrieval-concept`, fingerprint `d84a8a48…`) with the non-selected sibling still merged and compensating entry `[subset]`; repoint closed 2 blockings; ledger 982 / 0 inconsistencies; candidate-aware audit `full 23 · partial 1 · suspicious 170` |
| T8f | `f7fb0ef` | `--candidate` partial rollback: plan-time overlap refusal + alias-value collision refusal + candidate-aware idempotence backstop; RED `DID NOT RAISE MergeNotReversible` → GREEN; 52 focused unit · 747 unit suite · 6 testcontainers integration; ruff/mypy/architecture green; independent verification confirmed partial-then-full can no longer mutate |
| T8f.2 | (this commit) | candidate-aware retro-audit: `rolled_back` per candidate, guard on the ORIGINAL population + compensation reference check (unknown target / canonical mismatch / unknown candidate / duplicate all exit 2 pre-graph); RED 6 failed → GREEN 17 passed; 753 unit suite; ruff/mypy/architecture green; production read-only run unchanged 288 applied / 322 pairs / 307 stratified / 180 suspicious |
| T9a | `0bdf694` | decision registry + `decisions record\|list` + R5a/enqueue exclusion through one shared clause + consolidator keep-exclusion + seeder; RED 14 failed → GREEN 32 + 7 unit and 8 integration; ruff/mypy (419 files)/architecture green; production: 33 decisions seeded (25 separate / 8 keep), re-run no-op, **R5a 477 → 456**, **queue 170 → 162** |
| T8e 2C | (this commit) | 590/319 rolled back (fingerprint `6405aa70…`, 0 mirrors, no repoint) + 403/512 kept; decisions recorded via the new CLI; ledger 984 / chain OK; shared neighbours 0; scoped audits `passed`; R5a held at **456** by the separate decisions; queue **162 → 158** |
| T9b | `82746d1` | intra-namespace duplicates grouped case-insensitively: `DUPLICATE_ENTITY_LOGICAL` **0 → 64** groups, R5a unchanged 456, warning total **520**, `expose-mcp` still `passed` (uniqueness counts only blocking); RED `assert 0 == 1` → GREEN; 808 unit + 26 integration |
| T8e 2C-B | (this commit) | 371/415 rolled back (fingerprint `4e386d98…`, 0 mirrors) + 355/332/551/526 kept; six decisions recorded (registry 43); ledger 986 / chain OK; shared neighbours 0; audit `passed` (R5a held at 456, R5b 64); queue **158 → 152** |
| T8e 2C-C1 | (this commit) | 502 rolled back (fingerprint `d1c5e113…`, 0 mirrors) + 323/350/382/543/544/431 kept; seven decisions recorded (registry **50**); ledger 987 / chain OK; shared neighbours 0; audit `passed` (R5a 456, R5b 64); queue **152 → 145**; tier `[0.35,0.40)` is 24 pairs, split at 0.37 |
| T10 | `3d807e8` + `5154506` | intra-namespace batch: shared grouping constant, richness canonical, `--expect-fingerprint` refusal, order-simulating census; **64 groups merged in 3 per-namespace batches, 0 failures**, ledger **1051** / chain OK, **R5b 64 → 0**, R5a 456, self-loops 0, warning total 520 → 456, batch 3 `census drift: none`; 830 unit + 37 focused green |

## Close-out

The feature is closed. Consolidated report: `odd/reports/cross-namespace-semantic-resolution-closeout.md`
(before/after metrics, what was built, how it was applied, known limits, evidence index).

The 145 cross-namespace suspicious pairs that remain are a **deliberate stop**, not an open task: the tier sweep
showed diminishing returns (median cosine 0.506; the last three tiers produced 11 keeps against 6 reverts) and the
governance tooling is in place to intervene on demand. Remaining optional action: restart the MCP service so it
serves this branch's code (the CLI already uses it through the editable install).
