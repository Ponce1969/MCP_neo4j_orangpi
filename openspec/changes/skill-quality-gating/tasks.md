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

- [ ] 4.1 `scripts/seed_skill_scores.py --dry-run` (deterministic dimension inputs,
      `provenance` field, `--approval` gate on `--apply`).
- [ ] 4.2 Seed skills for the 4 book namespaces once the Chip Huyen index lands
      (backup → dry-run → approval → apply; AGENTS.md §7.1).
- [ ] 4.3 Re-run `gate` + audit + retrieval layer; record skill-gate evidence in
      `data/evaluation/`.

## Release gate

- [ ] All of the above green + `openspec/changes/skill-quality-gating/*` archived with
      verify-report (mirror of `namespace-question-routing` flow).