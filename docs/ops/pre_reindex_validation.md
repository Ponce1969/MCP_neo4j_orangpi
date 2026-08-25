# Pre-Reindex Graph Validation — Operator Runbook

The `book-graph-rag validate` command runs a read-only validation protocol over
the current single-book Neo4j graph and emits exactly one recommendation:

- `REINDEX` — graph may be reindexed, but only after a separate maintainer approval.
- `INSTRUMENT_BEFORE_REINDEX` — evidence is missing/unobservable; add read-only observability first.
- `FIX_GRAPH_CODE_BEFORE_REINDEX` — a blocking structural/provenance/schema defect was observed.

Validation is **always graph-read-only**. It never calls `clear_index`,
`ensure_indexes`, backfill, `--fresh`, restore, or any reindex operation.

## Usage

```bash
uv run book-graph-rag validate \
  --book-id <current-book-id> \
  --manifest <path-to-smoke-manifest.json> \
  --sample-limit 50 \
  --output evidence-bundle.json
```

- `--book-id` — the intended current book identity (required).
- `--manifest` — a versioned, deterministic smoke manifest (optional; without it,
  smoke evidence is empty and the policy fails closed to `INSTRUMENT`).
- `--approval` — an optional maintainer approval record (only read, never granted here).
- The command always prints the JSON evidence bundle to stdout and exits with the
  stable status code.

## Protocol sequence

1. **Freeze context** — run id, protocol version, target/database, book identity,
   secret-free configuration fingerprint, timestamps, manifest identity.
2. **Structural audit** — bounded, allowlisted read queries for inventory, schema,
   hierarchy, endpoints, pages, provenance, relationships, and isolation.
3. **Retrieval smoke** — deterministic read-only cases (currently `entity_lookup`;
   every other kind fails closed to `unknown`).
4. **Evaluate policy** — strict pass/fail over the complete evidence bundle.
5. **Stop** — a `REINDEX` recommendation never triggers reindex.

## Status and exit codes

| Exit | Status       | Recommendation |
|-----:|--------------|----------------|
| 0    | `passed`     | `REINDEX` |
| 10   | `violations` | `FIX_GRAPH_CODE_BEFORE_REINDEX` |
| 11   | `incomplete` | `INSTRUMENT_BEFORE_REINDEX` |
| 12   | `unreachable`| `FIX_GRAPH_CODE_BEFORE_REINDEX` |
| 13   | `failed`     | `FIX_GRAPH_CODE_BEFORE_REINDEX` |

A nonzero exit never triggers a graph write or a reindex.

## Provenance gate

The strict gate requires **100%** provenance coverage for every applicable
category (`chunks`, `mentions`, `related_occurrences`). A category with zero
records must be explicitly marked inapplicable. A 99.99% result fails the gate.

## Warnings

A non-blocking warning may coexist with `REINDEX` only when the bundle records a
stable rule id, bounded total, impact statement, acceptance rationale, and an
explicit maintainer acceptance reference. A warning with structural, provenance,
isolation, or evidence-ambiguous impact is blocking.

## TOC-less exception

TOC-less ingestion is blocked by default. It is validated only with a per-run
approval record supplied **before** validation and bound to the exact book id and
source fingerprint. The exception never weakens the 100% provenance, page,
endpoint, mention, schema, or smoke requirements.

## Maintainer approval boundary

A clean `REINDEX` recommendation is **not** authorization. A separate approval
record must bind the reindex action to the exact validation run, evidence hash,
target, book scope, backup plan, and supported runner. No approval can override a
nonzero validation status.

## Non-goals

- Ingesting future books or hardcoding their titles.
- Multi-book schema redesign, scoped rollback, or book-scoped summaries.
- Any graph mutation, repair, backfill, index creation, or reindex.
- Calling Orange Pi/production as part of validation.
- Treating RAGAS/evaluator metrics as proof of graph integrity.
