# Spec: Quality-Gated Skill Retrieval (SkillNet)

**Change:** `skill-quality-gating`
**Status:** DRAFT (unfrozen 2026-10-07; not yet approved)
**Keywords:** RFC 2119 (MUST/SHOULD/MAY). Scenarios use GIVEN/WHEN/THEN.

## 1. Scope

This change introduces a deterministic quality gate on the MCP tool surface:
capabilities are modeled as `:Skill` nodes with five quality dimensions, a weighted
`quality_score`, and a selection rule (`min_quality` threshold, `top_k` cap) applied
before the tool list is exposed to the LLM. It MUST NOT mutate the production graph
from the retrieval path and MUST NOT weaken the existing risk-tier/scope security
boundary.

## 2. Requirements

### REQ-SK-01 — Skill node contract
The gate MUST read skills from `:Skill` nodes whose `status = 'active'`. Each node
MUST carry `id`, `name`, `version`, `tool_names`, the five scores
(`safety`, `executability`, `completeness`, `maintainability`, `cost_awareness`) each
in `[0,1]`, and the five weights with defaults `2.0 / 2.0 / 1.0 / 1.0 / 1.0`.
GIVEN a skill node missing any score or weight, WHEN the gate loads it, THEN the node
is treated as ineligible (fail-closed) and never returned.

### REQ-SK-02 — Weighted formula
The gate MUST compute `quality_score = Σ(w_i·d_i) / Σ(w_i)`. The 2x weighting on
`safety` and `executability` MUST be the shipped default.
GIVEN two skills with equal raw averages, WHEN one has higher `safety`, THEN the
safer one MUST rank higher.

### REQ-SK-03 — `min_quality` gate
The gate MUST return only skills with `quality_score >= min_quality`. The threshold
is configurable (`skill_min_quality`); the provisional default is `0.60` and its final
value is a calibration decision, not a code constant.
GIVEN `min_quality=0.60`, WHEN a skill scores `0.59`, THEN it MUST be gated out.

### REQ-SK-04 — `top_k` cap
The gate MUST return at most `top_k` skills (`skill_top_k`), default `3`, bounded to
`1..8`. Order MUST be deterministic: `quality_score` descending, ties broken by `id`.
GIVEN 5 passing skills and `top_k=3`, WHEN the gate runs, THEN exactly 3 skills are
returned and the other 2 appear in `gated_out`.

### REQ-SK-05 — Fail-closed unknown tools
A skill that binds a tool name not registered by `McpServerAdapter` (undefined in
`TOOL_TIERS`) MUST be rejected at registry load with `UnknownToolError` semantics
(reuse `domain/mcp_security.py`). The gate MUST NOT expose tools outside the 8
registered tools.
GIVEN a skill binding `"query_cypher"`, WHEN the gate runs, THEN the skill is scored
with the HIGH-tier safety ceiling applied (REQ-SK-06).

### REQ-SK-06 — Safety ceiling for HIGH-tier tools
A skill that binds any HIGH-tier tool (`query_cypher`) MUST cap its effective
`safety` to `skill_safety_high_tier_cap` (provisional `0.40`) regardless of the raw
score, and report this ceiling in the selection rationale.
GIVEN a skill with raw `safety=1.0` binding `query_cypher`, WHEN the gate applies the
ceiling, THEN its effective safety is `0.40`.

### REQ-SK-07 — Determinism and read-only
The gate MUST be deterministic (same registry snapshot -> same selection) and MUST NOT
write to Neo4j, call the LLM, or change MCP signatures. Score materialization is
out-of-band (`scripts/seed_skill_scores.py`, backup → dry-run → approval → apply,
AGENTS.md §7.1).
GIVEN the same active skill set, WHEN the gate runs twice, THEN the selection is
identical.

### REQ-SK-08 — Additive to existing boundaries
The gate MUST NOT disable or relax `ToolRiskTier`/`ResourcePolicy` budgets or the
router's `require_scope` behavior. When the gate selects zero skills, the server MUST
fail closed (no un-gated tool is exposed).
GIVEN an empty active skill set, WHEN a request arrives, THEN the LLM sees no tools
(not the full 8).

## 3. Calibration inputs (provisional, not final)

| Input | Provision | Owner |
|---|---|---|
| `skill_min_quality` | `0.60` | calibration after first seeding on Orange Pi |
| `skill_top_k` | `3` | fixed by REQ-SK-04 |
| weights | `2/2/1/1/1` | proposal policy; re-evaluate after 4th book index |
| `skill_safety_high_tier_cap` | `0.40` | safety review |

## 4. Constraints and compatibility

- Hexagonal architecture: domain (`skill_models.py`) MUST stay stdlib + Pydantic.
- Gates: `uv run ruff check .`, `uv run mypy .`,
  `uv run python scripts/validate_architecture.py`, pytest — mandatory before any
  milestone is marked complete.
- No Orange Pi/Neo4j mutation during implementation units; only the seeding script
  writes skills, under the approval gate.
- Slices MUST stay under the 400-line review budget.