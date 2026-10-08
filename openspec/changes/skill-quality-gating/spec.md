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
GIVEN two skills subject to the **same** ceiling (neither binds a HIGH-tier tool) with equal raw
averages, WHEN one has higher `safety`, THEN the safer one MUST rank higher. The ceiling of
REQ-SK-06 deliberately outranks this rule: a skill binding `query_cypher` is demoted on purpose
even when its raw average is higher.

### REQ-SK-03 — `min_quality` gate
The gate MUST return only skills with `quality_score >= min_quality`. The threshold
is configurable (`skill_min_quality`); the provisional default is `0.60` and its final
value is a calibration decision, not a code constant.
GIVEN `min_quality=0.60`, WHEN a skill scores `0.59`, THEN it MUST be gated out.

The comparison is inclusive (`>=`) on a float, so a mathematically exact tie can evaluate a hair
below the threshold (`4.2 / 7` evaluates to `0.5999999999999999`). The boundary is therefore
"at or above as computed", not "at or above in exact arithmetic" — accepted on purpose rather
than papered over with an epsilon.

### REQ-SK-04 — `top_k` cap
The gate MUST return at most `top_k` skills (`skill_top_k`), default `5` (the catalog size; see
§3.2), bounded to `1..8`. Order MUST be deterministic: `quality_score` descending, ties broken
by `id`. A cap smaller than the catalog is a surface decision, not a budget decision: the
skills it drops take their bound tools out of the model-facing list.
GIVEN 5 passing skills and `top_k=3`, WHEN the gate runs, THEN exactly 3 skills are
returned and the other 2 appear in `gated_out`.

### REQ-SK-05 — Fail-closed unknown tools
A skill that binds a tool name not registered by `McpServerAdapter` (undefined in `TOOL_TIERS`)
MUST NOT reach the model-facing tool list. The registry readers drop such a skill
(`infrastructure/skill_record_mapping.py`) and the gate re-validates every name through `tier_for`
before scoring, so the boundary holds twice and an unregistered name can never inherit a default
policy by travelling inside a skill. `UnknownToolError` is the error surfaced when a caller passes
such a name directly, as `create_server(tool_names=...)` does.
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

## 3. Calibration inputs

| Input | Value | Status |
|---|---|---|
| `skill_min_quality` | `0.60` | provisional, approved 2026-10-07; final value after first seeding (§3.1) |
| `skill_top_k` | `5` | raised from `3` on 2026-10-08, see §3.2 |
| weights | `2/2/1/1/1` | approved 2026-10-07; re-evaluate after the 4th book index |
| `skill_safety_high_tier_cap` | `0.40` | provisional, approved 2026-10-07 |

### 3.1 Calibration decision (2026-10-07)

The arithmetic behind both numbers, with the weights summing to `7`:

| Case | Score | Passes `0.60`? |
|---|---|---|
| Every dimension `1.0`, `safety` capped at `0.40` (a HIGH-tier binding) | `0.8286` | **yes** |
| No evidence (`executability = 0`), the rest perfect | `0.7143` | yes |
| No evidence **and** a HIGH-tier binding | `0.5429` | **no** |
| Every dimension exactly at the threshold | `0.6000` | yes (`>=`) |

Two consequences are normative, not accidental:

- **`0.60` is fail-closed without starving evidence-backed skills.** Because
  `executability` scores `0.0` when nothing has been observed, passing with no evidence
  requires `safety >= 0.60` (from `(2s + 3) / 7 >= 0.60`), and a HIGH-tier binding capped
  at `0.40` fails at `0.5429`.
- **The safety ceiling is a scoring signal, not a gate.** The other four dimensions
  contribute `5/7 = 0.714` on their own, so no value of the ceiling can push a HIGH-tier
  skill below `0.60`: excluding it would require `skill_min_quality > 0.8286`. Decided on
  2026-10-07 to keep it that way and let such a skill be exposed when its evidence is
  good, because `query_cypher` stays disabled by config
  (`mcp_enable_query_cypher=False`) and REQ-SK-05/06 forbid this gate from weakening the
  tier/scope boundary; REQ-SK-06 asks the ceiling to be capped and reported in the
  rationale. Revisiting this means either raising `min_quality` above `0.8286` or making
  the ceiling a hard eligibility rule — a spec change, not a config change.

**Final value (after Unit 4 seeds the skills on the Orange Pi, read-only):** measure the
`quality_score` distribution of the seeded skills, then set `min_quality` to the value
that (a) keeps every skill whose bound tools have zero observed failures and
`safety >= 0.60`, and (b) gates out every skill with no evidence and a HIGH-tier binding,
recording both counts before and after plus the chosen percentile. Per
`docs/spec/06-evaluation-and-readiness.md` these thresholds are project-owned and set
from measured baselines, never imported.

### 3.2 Why `skill_top_k` is `5` and not `3` (raised 2026-10-08)

The `3` came from the draft's REQ-SK-04, written when no catalog existed. Once Unit 4.1
defined five capabilities, that cap stopped being a prompt-budget guard and became a silent
surface cut: `rag-answer` passes the threshold at `0.8429` but lands **fourth** by score, so it
fell outside the cap and took `search_rag` and `ask_global` out of the model-facing tool list
with it.

Only running the real chain (Neo4j adapter → `QualityGateUseCase`) against the seeded graph
revealed it: the seeding dry-run reports scores, not the selection, and `top_k`/`gated_out`
nothing else. The cap is therefore the catalog size, and it MUST be revisited whenever the
catalog grows — a cap below it decides the surface by score instead of by policy.

## 4. Constraints and compatibility

- Hexagonal architecture: domain (`skill_models.py`) MUST stay stdlib + Pydantic.
- Gates: `uv run ruff check .`, `uv run mypy .`,
  `uv run python scripts/validate_architecture.py`, pytest — mandatory before any
  milestone is marked complete.
- No Orange Pi/Neo4j mutation during implementation units; only the seeding script
  writes skills, under the approval gate.
- Slices MUST stay under the 400-line review budget.