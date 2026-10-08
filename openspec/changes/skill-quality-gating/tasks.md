# Tasks: Quality-Gated Skill Retrieval (SkillNet)

**Decision:** DRAFT — pending maintainer approval. **Status:** UNFROZEN (2026-10-07).
**Current phase:** Unit 0 — intent, design and normative contract.
**Delivery strategy:** Chained work-unit commits; keep each review candidate below the
400-line budget; alternate-review waiver documented (native RDD blocked upstream, see
`namespace-question-routing/tasks.md` review gate note).

**Why it was frozen, and why it is not any more (2026-10-07):** the original gate "close Unit 5"
(namespace-question-routing) was already satisfied — Units 0-5 were ARCHIVED on 2026-09-29
(`3605e33`..`0673c52`). It stayed frozen on three conditions, re-checked one by one:

1. **The Phase 5 retrieval-quality follow-up (RAGAS drop / precision@k 0.0478).** Half done: the
   `precision@k` warning was closed on 2026-10-07 as a calibration artifact (an unreachable absolute
   threshold, now removed) whose apparent drop was the expected effect of the merged-entity fix
   `80f68f1` — see B2 in `odd/backlog.md`. The **RAGAS `context_precision` drop is still open** and is
   now tracked as B5 there; it is a secondary, non-blocking metric (R6.1), so it does not gate this
   change.
2. **The 4th book (Chip Huyen) index decision.** Satisfied: the source is indexed and active —
   `knowledge:ai-engineering-huyen` has 989 chunks and 989 `PROCESSED` checkpoints in production, and
   `catalog.yaml` lists it as `status: active`.
3. **Explicit maintainer approval to unfreeze.** Given on 2026-10-07, together with the decision to
   track this draft (`6c1da49`) instead of keeping it as an untracked draft.

Production policy: unfreezing changes nothing about the host — the normal §7 gate still applies (no
graph mutation without a fresh backup and explicit approval).

## Unit 0 — Intent, design and normative contract (gated on approval)

- [x] 0A.1 Draft `proposal.md`, `design.md`, `spec.md`, `tasks.md`
      (`openspec/changes/skill-quality-gating/`). Tracked since 2026-10-07 (`6c1da49`), which
      supersedes the earlier "untracked draft only" arrangement.
- [ ] 0A.2 Maintainer review of the draft (approve / amend / reject).
- [x] 0A.3 Calibrate the provisional inputs (`skill_min_quality` = `0.60`,
      `skill_safety_high_tier_cap` = `0.40`) and record the decision — approved 2026-10-07 and
      written up in `spec.md` §3.1, together with the finding that the ceiling cannot gate out a
      HIGH-tier skill (decided to accept that and keep `query_cypher` disabled by config) and the
      post-seeding procedure that fixes the final `min_quality` from measured data.

## Unit 1 — Domain model and gate logic

- [x] 1.1 `skill_models.py`: `SkillQualityScores`, `SkillQualityWeights`,
      `Skill` (+ `quality_score` property) — stdlib + Pydantic only. It also carries the two
      fail-closed helpers the rest of the change reuses: `validate_tool_names` (REQ-SK-05) and
      `effective_quality_score` / `binds_high_tier_tool` (REQ-SK-06).
- [x] 1.2 `QualityGateUseCase` + `QualityGateResult` (top_k, min_quality, gated_out,
      rationale); unknown-tool fail-closed (reuses `UnknownToolError`).
- [x] 1.3 Safety ceiling rule for HIGH-tier tools (`query_cypher`) — applied to the score and
      reported in `rationale` per REQ-SK-06; `safety_cap` is injected at construction so
      `execute` keeps the signature the design fixes.
- [x] 1.4 Unit tests: weighting, ties, ceiling, fail-closed empty set, determinism
      (`tests/unit/test_skill_models.py` and `tests/unit/test_quality_gate_use_case.py`, 15 tests).

## Unit 2 — Registry

- [x] 2.1 `SkillRegistryPort` (`load_active`) — landed with Unit 1, because it is the gate's
      dependency and the application layer must not reach for the adapter.
- [x] 2.2 `Neo4jSkillRegistryAdapter`: one read-only `MATCH` over `:Skill`, node→`Skill`
      mapping, and rejection of a node missing a score or a weight. The threshold/`top_k`
      decision is deliberately NOT in this Cypher: §3 of the design embeds the gate in the query
      while §4-§5 put it in the gate, and the query form would duplicate the rule in two
      languages. The gate stays the single source of truth and this adapter is a reader, which
      also means a stale materialized `quality_score` can never decide what the model sees.
- [x] 2.3 `JsonSkillRegistryReader` mirror for deterministic tests (no artifact yet = empty
      snapshot, following the `JsonNamespaceProfileReader` convention).
- [x] 2.4 Tests with a fake driver and a JSON artifact; gates green
      (ruff/mypy/validate_architecture/pytest). Both readers share
      `infrastructure/skill_record_mapping.py`, so the fail-closed policy (REQ-SK-01,
      REQ-SK-05) lives in exactly one place instead of drifting between them.

## Unit 3 — MCP integration

- [x] 3.1 Wire `QualityGateUseCase` into the composition root; the LLM-facing tool set is
      assembled from `selected[*].tool_names` only. The set reaches the adapter through a
      late-bound `tool_names` attribute (the same wiring point `catalog` already uses), so the
      adapter's constructor signature stays stable for test doubles.
- [x] 3.2 Settings: `skill_gate_enabled` (**off by default** — an addition of this unit, see
      below), `skill_min_quality` (0.60), `skill_top_k` (3, bound 1..8) and
      `skill_safety_high_tier_cap` (0.40), all validated fail-fast at startup.
- [x] 3.3 Config wiring via `pydantic-settings`; no hardcoded values.
- [x] 3.4 Regression tests: the eight-tool default, a selected subset, the empty selection
      exposing nothing, an unregistered name rejected, and the composition root resolving the
      set from settings (`tests/unit/test_skill_gate_wiring.py`). The existing
      `ToolRiskTier`/`ResourcePolicy` tests keep passing untouched, which is the "still
      enforced" half.

**Why `skill_gate_enabled` exists (added here, not in the draft):** the gate is fail-closed,
so wiring it unconditionally would expose an **empty** tool set until Unit 4 seeds the
`:Skill` nodes — a server that answers nothing instead of a server that answers everything.
The switch keeps the default behaviour identical to today's and turns the deploy order
(seed, then enable) into a deployment concern rather than a landmine.

## Unit 4 — Evidence and seeding script (write path gated)

- [x] 4.1 `scripts/seed_skill_scores.py --dry-run` (deterministic dimension inputs,
      `provenance` field, `--approval` gate on `--apply`). The catalog and the four scoring
      rules were approved 2026-10-07 and are documented in the script's docstring: five
      capabilities covering the eight tools (a per-namespace split would expose the same tool
      set, since every tool already takes `source_id`), safety as the minimum over the bound
      tiers, executability from the MCP query log with no-evidence scoring 0.0, completeness
      fixed at 1.0 until `Skill` carries an expected-output contract, maintainability 1.0 at
      seeding, and cost-awareness normalized from the real `ResourcePolicy` row budgets.
- [x] 4.2 Seed the approved capability catalog — **one run, not one per namespace**: every tool
      already takes `source_id`, so a per-namespace split would expose exactly the same tool set
      and only add rows. The 4th source this line used to wait on is already indexed and active
      (`knowledge:ai-engineering-huyen`: 989 chunks, 989 `PROCESSED` checkpoints). Protocol:
      fresh backup → dry-run → approval → apply (AGENTS.md §7.1).
      **Done 2026-10-08**: backup `bookgraph_backup_20261008T120849Z.json` → human approval file
      → `--apply` wrote **five `:Skill` nodes** (`active`, five dimensions, weights, materialized
      `quality_score`, `provenance`). Verified surgically: nodes `29332 → 29337` (+5 exactly) and
      relationships `89247 → 89247` (untouched). The same verification, run through the real gate
      instead of the seeding dry-run, also exposed that `skill_top_k = 3` cut `rag-answer` — and
      with it `search_rag` and `ask_global` — out of the surface; corrected to `5` in §3.2.
- [x] 4.3 Re-run `gate` + audit + retrieval layer; record skill-gate evidence in
      `data/evaluation/`. **Done 2026-10-08, in part**: the audit re-ran with the five `:Skill`
      nodes present and **passed** (`state: passed`, `blocking_total: 0`, exit 0), and the
      evidence is committed as `data/evaluation/skill_gate_evidence.json` — catalog, the five
      dimensions and weights per skill with their materialized scores, the gate's real selection
      at both caps, the seeding ledger with its node/relationship deltas, and the provenance
      note. **Deferred on purpose**: the full readiness gate run, because its LLM/RAGAS layers
      take ~2h30m and spend provider budget and it checks layers the skill gate does not touch —
      it belongs before the flag is enabled, not now.

## Release gate

- [ ] All of the above green + `openspec/changes/skill-quality-gating/*` archived with
      verify-report (mirror of `namespace-question-routing` flow).