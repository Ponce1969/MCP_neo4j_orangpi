# Archive Report: skill-quality-gating

- Change: skill-quality-gating
- Status: COMPLETED — the gate mechanism ships; the flag is intentionally off
- Date: 2026-10-08
- Artifact store: openspec
- Delivery strategy: chained work-unit commits on `main` (direct-to-main). One slice, Unit 4.1,
  came in at 511 lines, over the 400-line review budget, because a script and its tests are one
  reviewable unit; recorded here rather than hidden.

## Summary

`skill-quality-gating` adds a deterministic quality gate to the MCP tool surface: every capability
is a `:Skill` node carrying five quality dimensions, a weighted `quality_score`, and a selection
rule (`min_quality` plus `top_k`) applied before the tool list reaches the model. The gate is
**fail-closed** — no evidence means no credit, and an empty registry exposes nothing — it never
mutates the graph from the retrieval path, and it sits upstream of the existing
`ToolRiskTier`/`ResourcePolicy` boundary, which it does not weaken.

## Outcome

- **Units 0-4.3 complete.** The two remaining checkboxes close with this report: `0A.2` (maintainer
  review, given across the change as each unit was approved and explicitly here) and the release
  gate.
- **Seeded in production**: five `:Skill` nodes, written with a fresh backup and a human approval
  file (§7.1). Verified surgically — nodes `29332 → 29337`, relationships `89247 → 89247`.
- **Audit passes with them**: `state: passed`, `blocking_total: 0`, exit 0.
- **The gate's real decision**: at the committed `skill_top_k = 5`, the four skills that pass are
  selected and their union is the **seven enabled tools**; only `skill:raw-cypher:v1` stays out,
  because `query_cypher` is disabled and a disabled capability should not be advertised.
- **The flag is OFF.** With `SKILL_GATE_ENABLED` unset, production behaviour is byte-for-byte what
  it was before: that is deliberate, since the gate is fail-closed and enabling it is a separate
  decision which needs the full readiness gate run first.
- **Independent verification returned `ISSUES FOUND`** — 8 findings. The two material ones
  (`search_rag` logging a false success, and the ceiling not being reported when it did not change
  the score) were fixed with tests; the documentation ones were reconciled in `spec.md` and
  `design.md`; one float boundary was accepted and documented. Details in `verify-report.md`.
- **Quality gates green**: `ruff check`, `ruff format --check` (439 files), `mypy .` (439 files),
  `validate_architecture`, and 1967 tests passing (3 host-only skips). No CRLF in the tracked tree.

## Artifacts

- `spec.md` — REQ-SK-01..06 and the calibration record (§3, §3.1, §3.2)
- `design.md` — §1-§6, including why the gate ships in the application layer
- `tasks.md` — the unit plan, 19 checkboxes
- `verify-report.md` — the independent verdict and its resolution
- `data/evaluation/skill_gate_evidence.json` — the seeding ledger and the gate's real selection
- Code: `domain/skill_models.py`, `ports/skill_registry_port.py`,
  `application/quality_gate_use_case.py`,
  `infrastructure/{neo4j_skill_registry_adapter,json_skill_registry_reader,skill_record_mapping}.py`,
  `scripts/seed_skill_scores.py`, `scripts-ops/exercise_mcp_tools.py`, `mcp_server_main.py`,
  `config.py`, `infrastructure/mcp/mcp_server_adapter.py`

## Carried forward

1. **Full readiness gate run**, then the decision to enable `SKILL_GATE_ENABLED` (pull + restart).
2. `ask_global` answers `"Run scripts/run_communities.py first"` — the community layer is empty.
3. Decide whether `:Skill` should join `_INDEX_NODE_LABELS` of
   `scripts/run_full_pipeline.py::_backup`; as derived metadata recreated by an idempotent seeding
   run, it probably should not, but that is an explicit decision rather than an omission.
4. `B5` — the RAGAS `context_precision` drop warning, still unanalysed.
5. The four `SKILL_*` keys in `.env.example`, to be added by hand (the safety policy blocks that
   path; the defaults live in `config.py`).
