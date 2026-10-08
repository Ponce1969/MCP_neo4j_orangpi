# Tasks: Quality-Gated Skill Retrieval (SkillNet)

**Decision:** DRAFT — pending maintainer approval. **Status:** FROZEN.
**Current phase:** None (frozen before Unit 0 completes).
**Delivery strategy:** Chained work-unit commits; keep each review candidate below the
400-line budget; alternate-review waiver documented (native RDD blocked upstream, see
`namespace-question-routing/tasks.md` review gate note).

**Why frozen:** the original gate "close Unit 5" (namespace-question-routing) is already
satisfied — Units 0-5 were ARCHIVED on 2026-09-29 (`3605e33`..`0673c52`). This change is
frozen pending (1) the Phase 5 retrieval-quality follow-up (RAGAS drop / precision@k
0.0478 — obs `roadmap/phase5-retrieval-baseline-next`), (2) the 4th book (Chip Huyen)
index decision, and (3) explicit maintainer approval to unfreeze. Production policy:
no Orange Pi/Neo4j mutation while frozen.

## Unit 0 — Intent, design and normative contract (gated on approval)

- [x] 0A.1 Draft `proposal.md`, `design.md`, `spec.md`, `tasks.md`
      (`openspec/changes/skill-quality-gating/`, committed as untracked draft only).
- [ ] 0A.2 Maintainer review of the draft (approve / amend / reject).
- [ ] 0A.3 If approved: calibrate provisional inputs (`skill_min_quality`,
      `skill_safety_high_tier_cap`) and record the decision.

## Unit 1 — Domain model and gate logic (frozen)

- [ ] 1.1 `skill_models.py`: `SkillQualityScores`, `SkillQualityWeights`,
      `Skill` (+ `quality_score` property) — stdlib + Pydantic only.
- [ ] 1.2 `QualityGateUseCase` + `QualityGateResult` (top_k, min_quality, gated_out);
      unknown-tool fail-closed (reuse `UnknownToolError`).
- [ ] 1.3 `Safety` ceiling rule for HIGH-tier tools (`query_cypher`).
- [ ] 1.4 Unit tests: weighting, ties, ceiling, fail-closed empty set, determinism.

## Unit 2 — Registry (frozen)

- [ ] 2.1 `SkillRegistryPort` (load_active).
- [ ] 2.2 `Neo4jSkillRegistryAdapter`: read-only §3 Cypher, node→`Skill` mapping,
      missing-score rejection.
- [ ] 2.3 `JsonSkillRegistryReader` mirror for deterministic tests.
- [ ] 2.4 Tests with a fake/JSON registry; gates green
      (ruff/mypy/validate_architecture/pytest).

## Unit 3 — MCP integration (frozen)

- [ ] 3.1 Wire `QualityGateUseCase` into `McpServerAdapter.create_server`; assemble
      the LLM-facing tool set from `selected[*].tool_names` only.
- [ ] 3.2 Settings: `skill_min_quality` (default 0.60), `skill_top_k` (default 3, bound
      1..8), `skill_safety_high_tier_cap` (default 0.40), fail-closed with no skills.
- [ ] 3.3 Config wiring via `pydantic-settings`; no hardcoded values.
- [ ] 3.4 Regression tests: `ToolRiskTier`/`ResourcePolicy` budgets still enforced;
      smoke test that 8 tools are exposed exactly when 3 skills select them.

## Unit 4 — Evidence and seeding script (frozen; write path gated)

- [ ] 4.1 `scripts/seed_skill_scores.py --dry-run` (deterministic dimension inputs,
      `provenance` field, `--approval` gate on `--apply`).
- [ ] 4.2 Seed skills for the 4 book namespaces once the Chip Huyen index lands
      (backup → dry-run → approval → apply; AGENTS.md §7.1).
- [ ] 4.3 Re-run `gate` + audit + retrieval layer; record skill-gate evidence in
      `data/evaluation/`.

## Release gate

- [ ] All of the above green + `openspec/changes/skill-quality-gating/*` archived with
      verify-report (mirror of `namespace-question-routing` flow).