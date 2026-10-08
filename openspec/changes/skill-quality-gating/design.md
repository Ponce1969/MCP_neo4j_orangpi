# Design: Quality-Gated Skill Retrieval (SkillNet)

## 1. Neo4j data model — `:Skill`

New node label `Skill`, id-namespaced following the project convention
(`<namespace>:<name>`, mirror of `SourceNamespace`). Scores are `Float` in `[0,1]`,
stored as materialized properties plus the weights that produced them (weights are
data, not code, so calibration does not require a release).

```cypher
(:Skill {
  id: String,                 // "skill:<slug>:<version>", e.g. "skill:graph-consult:v1"
  name: String,               // human-readable capability name
  version: String,
  status: String,             // "active" | "inactive" | "retired"
  tool_names: List<String>,   // MCP tools bound, e.g. ["search_chunks", "find_entity"]
  safety: Float,              // 0..1  — grounded in ToolRiskTier/ResourcePolicy defaults
  executability: Float,       // 0..1  — observed tool success rate (deterministic telemetry/probes)
  completeness: Float,        // 0..1  — declared capability coverage (vs MCP tool schema)
  maintainability: Float,     // 0..1  — provenance/age/ops signal (deterministic)
  cost_awareness: Float,      // 0..1  — budget proxy from ResourcePolicy (rate/rows/timeout)
  weight_safety: Float,       // default 2.0
  weight_executability: Float,// default 2.0
  weight_completeness: Float, // default 1.0
  weight_maintainability: Float, // default 1.0
  weight_cost_awareness: Float,  // default 1.0
  quality_score: Float,       // materialized weighted mean, 0..1
  updated_at: DateTime,
  provenance: String          // ops evidence reference (score source, run id)
})
```

### Dimension grounding (deterministic, no LLM)

| Dimension | Input source | Notes |
|---|---|---|
| `safety` | `ToolRiskTier` + `ResourcePolicy.default_for(tier)` of bound tools | LOW tier maps to a high safety score; HIGH tier (`query_cypher`) scores low; a skill binding a HIGH-tier tool MUST cap `safety` at a configurable ceiling. |
| `executability` | measured success rate of bound tools in `data/evaluation` telemetry / read-only probes | 0 failures observed -> 1.0; no evidence -> `0.0` (fail-closed, never optimistic). |
| `completeness` | diff between the tool's declared capability (MCP schema) and the skill contract | deterministic check, no runtime LLM call. |
| `maintainability` | provenance metadata (age, churn, review state) | deterministic ops input; see §4 provenance. |
| `cost_awareness` | `ResourcePolicy` budget of bound tools (rate_limit_calls, max_rows/nodes, timeout) | tighter budgets (HIGH tier) score HIGHER cost-awareness. |

## 2. Weighted formula

```
dimensions = {safety, executability, completeness, maintainability, cost_awareness}
weights    = {2.0,   2.0,           1.0,             1.0,              1.0}       (defaults)

quality_score = Σ(w_i * d_i) / Σ(w_i)
```

With defaults the denominator is `7`; the max achievable score with all dimensions at
1.0 is 1.0. Weights and the ceiling on `safety` for HIGH-tier skills are read from
config (`skill_quality_weights`, `skill_safety_high_tier_cap`) so calibration is a
config change, never a code change. `quality_score` is recomputed and re-materialized
by the ops seeding script (never by the retrieval path).

## 3. Gating Cypher (read-only, runs before the LLM tool list is assembled)

```cypher
MATCH (s:Skill)
WHERE s.status = 'active'
WITH s,
     (s.weight_safety       * s.safety
    + s.weight_executability * s.executability
    + s.weight_completeness  * s.completeness
    + s.weight_maintainability * s.maintainability
    + s.weight_cost_awareness  * s.cost_awareness)
     / (s.weight_safety + s.weight_executability + s.weight_completeness
        + s.weight_maintainability + s.weight_cost_awareness) AS quality_score
WHERE quality_score >= $min_quality
RETURN s.id AS id, s.name AS name, s.version AS version,
       quality_score, s.tool_names AS tool_names
ORDER BY quality_score DESC
LIMIT $top_k
```

Parameters: `$min_quality` (provisional default `0.60`, calibration pending),
`$top_k` (the cap must cover the catalog — five seeded capabilities, so `5`, configurable
`1..8` but never above a skills-per-prompt budget derived from model context).

## 4. Pydantic contracts (domain — hexagonal, no infra imports)

```python
# domain/skill_models.py
from pydantic import BaseModel, ConfigDict, Field

class SkillQualityScores(BaseModel):
    """Five deterministic quality dimensions, each in [0, 1]."""
    model_config = ConfigDict(frozen=True, strict=True)
    safety: float = Field(ge=0.0, le=1.0)
    executability: float = Field(ge=0.0, le=1.0)
    completeness: float = Field(ge=0.0, le=1.0)
    maintainability: float = Field(ge=0.0, le=1.0)
    cost_awareness: float = Field(ge=0.0, le=1.0)

class SkillQualityWeights(BaseModel):
    """Per-dimension weights; defaults encode the 2x Safety/Executability policy."""
    model_config = ConfigDict(frozen=True, strict=True)
    safety: float = Field(default=2.0, gt=0.0)
    executability: float = Field(default=2.0, gt=0.0)
    completeness: float = Field(default=1.0, gt=0.0)
    maintainability: float = Field(default=1.0, gt=0.0)
    cost_awareness: float = Field(default=1.0, gt=0.0)

class Skill(BaseModel):
    """A gated unit of capability binding one or more MCP tools."""
    model_config = ConfigDict(frozen=True, strict=True)
    id: str                       # "skill:<slug>:<version>"
    name: str
    version: str
    status: str                   # "active" | "inactive" | "retired"
    tool_names: tuple[str, ...]
    scores: SkillQualityScores
    weights: SkillQualityWeights = SkillQualityWeights()

    @property
    def quality_score(self) -> float:
        """Weighted normalized mean over the five dimensions."""
        s, w = self.scores, self.weights
        total = (w.safety + w.executability + w.completeness
                 + w.maintainability + w.cost_awareness)
        weighted = (w.safety * s.safety + w.executability * s.executability
                    + w.completeness * s.completeness
                    + w.maintainability * s.maintainability
                    + w.cost_awareness * s.cost_awareness)
        return weighted / total

# application/quality_gate_use_case.py
class QualityGateResult(BaseModel):
    """Outcome of gating: selected skills plus what was gated out."""
    model_config = ConfigDict(frozen=True)
    selected: tuple[Skill, ...]          # top_k, sorted desc by quality_score
    min_quality: float
    top_k: int
    gated_out: tuple[str, ...]           # ids rejected (below threshold / inactive / unknown)

class QualityGateUseCase:
    """Select the skills to expose to the LLM (base dependency: SkillRegistryPort).

    Deterministic and stateless: same registry snapshot -> same selection.
    Never mutates the graph; never calls the LLM.
    """

    async def execute(self, *, min_quality: float, top_k: int) -> QualityGateResult:
        skills = [s for s in await self._registry.load_active() if s.quality_score >= min_quality]
        skills.sort(key=lambda s: s.quality_score, reverse=True)
        return QualityGateResult(
            selected=tuple(skills[:top_k]),
            min_quality=min_quality,
            top_k=top_k,
            gated_out=tuple(s.id for s in skills[top_k:]),
        )
```

Port: `ports/skill_registry_port.py` — `SkillRegistryPort` with
`load_active() -> Sequence[Skill]`. Infrastructure adapters:
`Neo4jSkillRegistryAdapter` (runs the §3 Cypher, read-only) and a
`JsonSkillRegistryReader` mirror for deterministic tests (same pattern as
`JsonNamespaceProfileReader`).

## 5. MCP integration point

`McpServerAdapter.create_server` registers the 8 tools today. Under this change the
server composes, per request context: `gate = QualityGateUseCase(registry)` →
`gate.execute(min_quality=settings.skill_min_quality, top_k=settings.skill_top_k)` →
the model-facing tool set is assembled ONLY from `selected[*].tool_names`. The
existing `ToolRiskTier`/`ResourcePolicy` enforcement remains untouched underneath;
the gate is additive and upstream of it.

## 6. Ops seeding (out-of-band, write path)

`scripts/seed_skill_scores.py --dry-run|--apply` computes the five dimensions from
deterministic sources (§1 table), writes `:Skill` nodes with `provenance`, and
re-materializes `quality_score`. Follows the Phase 1/2 protected protocol: backup →
dry-run → explicit approval → apply; `--apply` requires an `--approval <file>`
containing the word `approve` (same gate as Phase 2 administrative flags, AGENTS.md §7.1).