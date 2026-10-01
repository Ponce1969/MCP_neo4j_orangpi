# Namespace Routing Baseline — Unit 0

## Purpose

This document records the evidence boundary for the namespace-routing change. It is a
routing baseline, not a claim that the router already exists or that routing quality has
been measured.

## Production snapshot

The current Engram project anchor records the following read-only production state from
the Orange Pi:

- Three book namespaces are indexed and resolved:
  - `knowledge:agentic-architectural-patterns`
  - `knowledge:graphrag-agentic`
  - `knowledge:essential-graphrag`
- Completed on 2026-10-01, a fourth namespace is indexed and resolved:
  - `knowledge:ai-engineering-huyen` (989 chunks, 10 chapters, 125 sections,
    354 intra-namespace merges folded, scoped and global audits `passed 0/0/0`)
- Global and namespace-scoped audits report `0/0/0` findings.
- Community summaries and cross-namespace resolution are complete.
- The MCP service is deployed on the Tailscale address with bearer authentication.
- The MCP contract remains read-only and requires an explicit validated scope.

The evidence comes from the project anchors maintained in Engram after the production
close-out. Unit 0 does not re-run production queries and does not mutate the graph.

## Repository baseline

The repository already contains the relevant boundaries:

| Concern | Current boundary |
|---------|------------------|
| Namespace identity | `domain/namespaces.py` |
| Catalog validation | `infrastructure/catalog_scope_resolver.py` |
| Scope enforcement | `infrastructure/mcp/mcp_server_adapter.py` and `neo4j_query_adapter.py` |
| Local embeddings | `ports/embedding_provider_port.py` and `infrastructure/sentence_transformer_adapter.py` |
| Community summaries | `ports/community_read_port.py` and `infrastructure/community_adapter.py` |
| Metadata-only MCP logging | `domain/models.py::QueryLogEntry` and `infrastructure/logging/` |

No namespace-question router, centroid artifact, SQLite routing adapter, or routing
dataset existed before this change.

## Unit 0 dataset contract

`data/evaluation/namespace_routing_dataset.jsonl` is a hand-curated classification
dataset. It records expected routing behavior only:

- `single`: one expected namespace;
- `multi`: an explicitly cross-book question;
- `abstain`: an ambiguous question with no safe forced namespace;
- `out_of_domain`: no expected book namespace.

It is not an answer-quality golden dataset. A future answer or telemetry event requires
an explicit validation/correction label before promotion to a golden example.

## Safety boundary

Unit 0 is repository-only:

- no SSH or production command is executed;
- no Neo4j write is executed;
- no container is restarted, stopped, rebuilt, or removed;
- no MCP service configuration is changed;
- no raw user query or answer is added to the existing MCP log;
- profile generation is deferred to a later read-only unit.

If a later unit proposes storing derived profiles in Neo4j, it must use the project's
backup → dry-run → human approval → apply → audit protocol.

## Verification record

The Unit 0 candidate must record:

```text
- JSONL parses successfully;
- every expected namespace exists in catalog.yaml;
- manifest SHA-256 and record count match;
- no production/deployment files changed;
- uv run ruff check . passes;
- uv run mypy . passes;
- uv run python scripts/validate_architecture.py passes.
```

Executed in the repository-only Unit 0 slice:

| Command | Result |
|---------|--------|
| `uv run python scripts/validate_namespace_routing_dataset.py` | PASS — 31 records; manifest SHA-256 matched. |
| `uv run pytest tests/unit/test_validate_namespace_routing_dataset.py -q` | PASS — 4 tests. |
| `uv run ruff check .` | PASS. |
| `uv run mypy .` | PASS — no issues in 338 source files. |
| `uv run python scripts/validate_architecture.py` | PASS. |
| `uv run pytest tests/unit tests/test*.py -q` | PASS — 1409 tests after the authorized test-only endpoint waiver at `tests/test_mcp_server_main.py`. |
| `uv run pytest -q` | Could not complete: integration run hangs at `test_community_read_path_is_read_only` in this local environment; excluded for Unit 0 verification. |

The full integration run remains environment-limited and outside Unit 0 scope. No
production command was executed to work around it.

## RDD waiver

Native RDD review was attempted for slice 0A and blocked by a provider-side stall:
`state=reviewing`, `action=collect`, `reason=reviewer_results_required`, `candidates=[]`,
with no reviewer artifact, timeout, or terminal verdict. Reproduced after a fresh
audited restart and after upgrading the CLI from 3.6.0 to 3.7.0; two exact capture
submissions were rejected as outside the host session route. Reported as
[issue #14](https://github.com/Ponce1969/MCP_neo4j_orangpi/issues/14).

The maintainer explicitly authorized abandoning the affected lineages (operator
disposition, audited) and reviewing each Unit 0 slice by the alternate path: full diff
review plus Ruff/mypy/architecture gates and filtered tests. This waiver is recorded
here and in `openspec/changes/namespace-question-routing/tasks.md`. Re-enable native RDD
for Units 1-5 once issue #14 is resolved.

## Calibration (Orange Pi, 2026-09-29)

Built the first production profile artifact from the live graph:

```text
scripts/build_namespace_profiles.py --graph-snapshot pi-prod-2026-09-29
→ 3 profiles, dim=384, model=paraphrase-multilingual-MiniLM-L12-v2
artifact: data/router/namespace_profiles.json (mode 600)
```

Threshold sweep over the committed dataset with real profiles and embeddings:

```text
scripts/calibrate_namespace_routing.py --max-wrong-rate 0.0
```

| Threshold (top / margin) | accuracy | wrong | abstention | multi |
|--------------------------|----------|-------|------------|-------|
| 0.10 / 0.00 | 0.909 | 0.091 | 0.194 | 0.000 |
| **0.10 / 0.05 (selected)** | **0.727** | **0.000** | **0.452** | **0.667** |
| 0.45 / 0.10 (initial defaults) | 0.227 | 0.000 | 0.806 | 0.667 |

Selected defaults: `RouteThresholds(min_top_score=0.10, min_margin=0.05)`.

The full-book-average centroid is intentionally conservative; finer per-chapter
profiles are a future improvement and would raise coverage without forcing
wrong routes.

## Recalibration (Orange Pi, 2026-10-01, four namespaces)

Rebuilt the artifact once the fourth namespace existed:

```text
scripts/build_namespace_profiles.py --graph-snapshot pi-prod-2026-10-01
→ 4 profiles (agentic-architectural-patterns, ai-engineering-huyen,
  essential-graphrag, graphrag-agentic), dim=384,
  model=paraphrase-multilingual-MiniLM-L12-v2
artifact: data/router/namespace_profiles.json (Pi-local, untracked)
```

Sweep over the same committed dataset with the four-source artifact:

```text
scripts/calibrate_namespace_routing.py --json-only
→ swept 84 threshold combinations over 31 labels
```

| Threshold (top / margin) | accuracy | wrong | abstention | multi |
|--------------------------|----------|-------|------------|-------|
| **0.10 / 0.05 (selected again)** | **0.682** | **0.000** | **0.484** | **0.667** |
| 0.10 / 0.00 | 0.909 | 0.091 | 0.194 | 0.000 |
| 0.45 / 0.10 | 0.227 | 0.000 | 0.806 | 0.667 |

`0.10 / 0.05` stays the recommended pair (`wrong_namespace_rate = 0.0`), so the
code defaults in `domain/routing_models.py` are unchanged. Accuracy drops
0.727 → 0.682 and abstention rises 0.452 → 0.484 relative to the three-namespace
run: the fourth centroid competes for the same questions and lowers top scores.

**Coverage limitation (explicit, not hidden):** the committed dataset has 31
labels and **none for `knowledge:ai-engineering-huyen`** (10 +
10 + 9 across the other three, plus out-of-domain). The recalibration therefore
measures how the fourth candidate perturbs existing decisions; it does not
measure quality *on* the new namespace. Extending the dataset is a follow-up.

## Next step

Unit 0 is complete on 
`main`: slice 0A (`b8d6d22`), slice 0B (spec/tasks/baseline), and slice 0C
(dataset/validator/tests). Implement the domain-only classifier contracts next. Do not
wire runtime routing or SQLite until the profile and abstention contracts have tests.
