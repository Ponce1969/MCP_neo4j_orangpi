# Verify Report: skill-quality-gating

- Change: skill-quality-gating
- Scope: Units 0-4.3 — domain models, quality gate, registry adapters, MCP wiring, seeding script, evidence
- Mode: openspec
- Verifier: independent `gentle-ai-verify` subagent (read-only), plus orchestration-level verification of the approved production write
- Date: 2026-10-08

## Executive Summary

All units are implemented and pass the project quality gates. **The independent verifier returned
`ISSUES FOUND` (8 findings)**: the two material ones were fixed with tests, the documentation ones
were reconciled in `spec.md` and `design.md`, and one is recorded as a deliberate acceptance.
Nothing was left as an unqualified claim.

Production state is out of reach for a read-only verifier without SSH, and it is reported as such
rather than assumed: the seeded graph, the audit run and the `generated_at` values in the evidence
file were verified by the orchestrator, not independently.

## Quality gates (final run)

| Gate | Command | Result |
|---|---|---|
| Lint | `uv run ruff check .` | PASSED — All checks passed |
| Format | `uv run ruff format --check .` | PASSED — 439 files already formatted |
| Type check | `uv run mypy .` | PASSED — 439 source files, no issues |
| Architecture | `uv run python scripts/validate_architecture.py` | PASSED |
| Tests (fast set) | `uv run pytest tests/unit tests/property tests/test_*.py -q` | PASSED — 1967 passed, 3 skipped |
| Line endings | `git ls-files --eol` | PASSED — no CRLF in the tracked tree |

The 3 skips are the pre-existing host-only gatekeeper compose branch.

## Independent verification: findings and resolution

| # | Finding | Resolution |
|---|---|---|
| **F5** | `search_rag` logged a **clean success** when both sub-queries failed: it gathers with `return_exceptions=True`, the payload carried `errors`, and the success `_log` omitted `error_code` — so `executability`, the very metric the gate scores, could read 1.0 for a fully failed call | **Fixed.** The log now carries `subquery_failed` whenever the payload collected errors. RED first: `test_search_rag_logs_an_error_when_its_subqueries_fail` failed with `assert None is not None`. |
| **F3** | REQ-SK-06 requires the ceiling to be "reported in the selection rationale", but the gate only reported it when it changed the score — and the seeded `skill:raw-cypher:v1` stores its raw safety already at the cap, so its rationale was empty | **Fixed.** The gate reports the *binding*. Test: `test_ceiling_is_reported_when_the_raw_safety_is_already_at_the_cap`. |
| F1 | REQ-SK-05 demanded `UnknownToolError` at registry load while the readers drop such a skill (and REQ-SK-01 asks for a drop) | **Reconciled.** REQ-SK-05 now states the drop plus the gate's re-validation through `tier_for`, and says where `UnknownToolError` is actually surfaced (`create_server`). |
| F2 | REQ-SK-02 ("equal raw averages → the safer skill ranks higher") is deliberately inverted by the ceiling | **Reconciled.** The rule is scoped to skills under the same ceiling, and the inversion is stated as intentional. |
| F4 | An exact-threshold case can float-evaluate to `0.5999999999999999` and be gated out | **Accepted and documented** in REQ-SK-03: the boundary is "at or above as computed", with no epsilon. |
| F6 | `design.md` §3 still showed the gate embedded in Cypher and a `QualityGateResult` without `rationale` | **Reconciled.** §3 records that the gate ships in the application layer, why, and that `rationale` was added for REQ-SK-06. |
| F8 | Scope failures and `ask_global`'s `detail_level` `ValueError` raise before the logging block | **Accepted.** A missing record means "no evidence", which is the fail-closed direction. |

### Reproduced independently (not taken on trust)

- The weighted formula, the `2/2/1/1/1` defaults, the inclusive `>=`, `top_k` with `id`
  tie-breaks, fail-closed on an empty registry, `UnknownToolError` on an unregistered name, and the
  ceiling applied without mutating raw scores.
- **Every number in `spec.md` §3 / §3.1 / §3.2**: 0.8286, 0.7143, 0.5429, `(2s+3)/7`, `5/7 = 0.714`,
  "requires `min_quality > 0.8286`", `rag-answer = 0.8429`, and the top_k A/B (`3` → 5 tools,
  `5` → 7 tools, names matched exactly).
- The internal consistency of `data/evaluation/skill_gate_evidence.json`: all five scores
  recomputed from the script's documented rules, plus catalog, tie-break order, `gated_out` and
  `tools_never_exposed_with_top_k_5`.
- The logging survey: after the `ask_global` fix, all eight tools record their successes, and no
  other tool returned inside its `try`.
- The RED of the two assertion-level fixes (`git show 64bbe51^:…` replayed against the HEAD test).

## Production verification (orchestrator; read-only except the approved write)

- **Seeding**: fresh backup `bookgraph_backup_20261008T120849Z.json` → human approval file
  (`/tmp/skill_seed_approval.txt`, AGENTS.md §7.1) → `--apply` wrote five `:Skill` nodes with the
  five dimensions, the weights, the materialized `quality_score` and `provenance`.
- **Surgical** (measured, not asserted): nodes `29332 → 29337` (+5 exactly),
  relationships `89247 → 89247` (untouched).
- **Audit after seeding**: `state: passed`, `blocking_total: 0`, exit 0.
- **Gate against the seeded graph** at the committed `skill_top_k = 5`: the four skills that pass
  are selected and their union is the **seven enabled tools**, leaving only `skill:raw-cypher:v1`
  out — correct, because `query_cypher` is disabled and a disabled capability should not be
  advertised.
- **`SKILL_GATE_ENABLED` remains `false`**: production behaves exactly as before this change.

## Unverified (reported, not hidden)

- The **full readiness gate run**: its LLM/RAGAS layers take ~2h30m and spend provider budget, and
  they check layers the skill gate does not touch. Recorded as a follow-up that belongs **before
  the flag is enabled**, not before this archive.
- **Enabling the flag**: a separate, later decision requiring a host pull and a service restart.
- The production numbers above were verified by the orchestrator over SSH; the independent
  verifier had no host access and said so explicitly rather than assuming them.

## Carried forward

1. Full readiness gate run, then the flag decision.
2. `ask_global` answers `"Run scripts/run_communities.py first"` — the community layer is empty.
3. Decide whether `:Skill` should join `_INDEX_NODE_LABELS` of `scripts/run_full_pipeline.py::_backup`.
4. `B5`: the RAGAS `context_precision` drop warning.
5. The four `SKILL_*` keys in `.env.example` (the safety policy blocks that path; defaults live in
   `config.py`).
