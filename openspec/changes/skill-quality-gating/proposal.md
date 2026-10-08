# Proposal: Quality-Gated Skill Retrieval (SkillNet)

**Change:** `skill-quality-gating`
**Status:** DRAFT (not approved; frozen pending maintainer go-ahead)
**Store:** OpenSpec (`openspec/changes/skill-quality-gating/`)
**Technical register:** English; RFC 2119 keywords; GIVEN/WHEN/THEN scenarios.

## Decision

Add a deterministic **skill quality gate** between the MCP tool surface and the LLM:
every capability exposed to the agent is represented as a `:Skill` node carrying five
quality dimensions, a materialized weighted score, and a gate that MUST only expose
`top_k` skills with `quality_score >= min_quality` before the model sees the tool list.
The pattern follows the "Quality-Gated Skill Retrieval" (SkillNet 2026 / Agentic
GraphRAG) reference: capabilities are measured, ranked and gated instead of being
blindly enumerated.

This change is additive, deterministic and read-only with respect to graph indexing:
it MUST NOT mutate the production Neo4j graph during retrieval, MUST NOT weaken the
existing `ToolRiskTier`/`ResourcePolicy` security boundary, and MUST NOT change the
namespace router or the MCP `require_scope` contract.

## Problem

Today the MCP server exposes all 8 tools unconditionally (modulo risk-tier budgets).
CodeGraph audit (2026-09-30) confirms:

- There is **no `Skill` concept** in the codebase (`grep -ri skill src/` = 0 matches;
  CodeGraph symbol query "Skill skill quality gating" = no results).
- The only quality-adjacent mechanism is `ToolRiskTier` (low/medium/high) +
  `ResourcePolicy` (timeouts, max rows/nodes, traversal depth, retries, concurrency,
  rate limits) in `domain/tool_tier_registry.py` and `domain/mcp_security.py` — a
  **safety/cost tiering**, not a quality gating.
- The router (`RouteQuestionUseCase`, `RoutingCaller.route_for_caller`) filters
  **namespaces**; it never filters tools by quality before the LLM.
- There is no prior Change/Spec on "Skill Quality" or "SkillNet" under `openspec/`
  (only `graphrag-ragas-resilience` and `namespace-question-routing` exist).

With the 4th book (Chip Huyen) entering the graph, the MCP surface and the number of
skills will grow. Every skill added to every prompt costs tokens, latency and,
if unmaintained or unsafe, retrieval quality. Without a quality gate, a broken or
irrelevant skill competes for the model's attention and there is no measurable bar
for "is this skill safe and effective enough to expose?".

## Goals

- Model agent capabilities as `:Skill` nodes with five deterministic dimensions:
  `safety`, `executability`, `completeness`, `maintainability`, `cost_awareness`.
- Materialize a weighted `quality_score` per skill using a **2x weight on Safety and
  2x weight on Executability** (skills that are unsafe or fail at runtime must never
  rank above safer, working ones).
- Gate the LLM-facing tool list: only the top `top_k` skills with
  `quality_score >= min_quality` are exposed.
- Ground the `safety` and `cost_awareness` inputs in the existing
  `ToolRiskTier`/`ResourcePolicy` machinery instead of inventing a parallel model.
- Keep the gate deterministic, unit-testable, and read-only against the graph.

## Non-goals

- No LLM-generated skills, no skill fine-tuning.
- No changes to `RouteQuestionUseCase`, `RequireScope`, or the namespace router.
- No removal or weakening of `ToolRiskTier`/`ResourcePolicy` enforcement.
- No mutation of the graph from the gate (scores are written out-of-band by ops with
  provenance).
- No new MCP transport or protocol changes.
- No commitment to final thresholds in this draft: `min_quality` and the magnitude
  of the 2x weights are calibration inputs, defined here with provisional defaults.

## Skills vs existing concepts (glossary)

- **Tool** — an MCP tool today (8 registered by `McpServerAdapter.create_server`),
  governed by `ToolRiskTier` + `ResourcePolicy`.
- **Skill** — NEW: a named capability binding one or more tools plus five quality
  scores. A skill is the unit of gating; a tool not bound to any active skill is
  hidden from the LLM under this change.
- **Gate** — NEW: deterministic selection over skills (top_k + min_quality) executed
  before the LLM-facing tool list is assembled.