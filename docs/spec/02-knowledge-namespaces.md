# 02 — Knowledge Namespaces

> **Status: Target (unimplemented).** Entity and book IDs are currently global slug-based;
> there is no namespace concept. This spec defines the namespace model and migration.

## 1. Current state `[VERIFIED]`

- `Entity.id` is computed as `slugify(name)-type` or `slugify(canonical_name)-type`,
  **globally**, with no source/corpus/domain component.
  `[VERIFIED]` `infrastructure/llm_adapter.py::_resolve_entity_id`.
- `Book.id` is a slug of title or a hash of `(title, author)`. `[VERIFIED]`
  `domain/models.py::Book` and `application/index_book_use_case.py` (book upsert).
- `:Chunk` nodes carry `book_id` as a property; `MENTIONS` links chunks to entities.
- A single book is the only current source (`data/libro_Agentic_Architectural_Patterns.pdf`).
- There is **no** server-side filtering by source/corpus/domain; queries are global.

## 2. Target model

### 2.1 Namespace hierarchy `[TARGET]`

Logical, ordered namespaces:

| Level | Name | Meaning | Example |
|-------|------|---------|---------|
| 0 | `corpus` | A collection of related sources (a shelf/project) | `agentic-patterns` |
| 1 | `domain` | A subject area within a corpus | `multi-agent`, `mcp` |
| 2 | `source` (book/doc) | A single indexed document | `agentic-architectural-patterns` |

`domain` is optional (a corpus may have no domain split). Every entity belongs to at
least a `corpus` and, when extracted from a document, a `source`. `[TARGET]`

### 2.2 Entity identity and provenance `[TARGET]`

- An entity's durable id MUST be **namespaced**: `corpus:source:slug-type` (the `source`
  level is MANDATORY in the id — separation by default). Separator `:`. The id MUST be
  reversible (parseable back to its parts) and MUST be stable under source re-indexing.
- Two entities with the same slug in **different** sources MUST NOT collide (guaranteed
  by construction because `source` is part of the id).
- `domain`, when present, is an optional grouping level and is NOT part of the id; it is
  a catalog/label concern, not an identity component.
- Each entity MUST record provenance: the `source_id`(s) it was extracted from, its
  `source_page`, and (after 01) its version dimensions. Today only `source_page` and
  (indirectly) `MENTIONS` carry provenance. `[VERIFIED]`
- Identity changes go through a migration (see §5), never a silent in-place rewrite.

### 2.3 Shared entities and cross-domain relationships `[TARGET]`

- A **shared entity** (same real-world concept referenced by multiple sources/domains) is
  represented by a single node with one identity, plus per-source **mention** edges and
  per-source provenance, NOT by duplicated nodes per source.
- Cross-domain relationships connect entities that legitimately span domains (e.g. a
  `mcp` tool in one domain `enables` a `pattern` in another).
- The decision of whether two entities are "the same" is governed by 03 (semantic
  resolution), not by namespace alone. Namespace scoping bounds the candidate set;
  resolution decides identity.

### 2.4 Server-side filtering `[TARGET]`

- Read queries MUST accept and enforce an optional namespace filter (corpus/domain/
  source) applied at the Cypher layer, not by post-filtering results in Python.
- When no filter is given, the query **fails closed**: it is rejected with a typed error
  requiring an explicit namespace/scope. "Return everything" is not a supported default
  (no-noise, no-chaos rule). This default is documented and consistent across all tools.

### 2.5 Namespace validation / catalog `[TARGET]`

- A namespace catalog records, for each known corpus/domain/source: its id, human label,
  source version, and status (`active`, `deprecated`).
- The catalog lives in a **versioned config file** (source of truth), not in Neo4j
  initially. A Neo4j mirror may be added later if server-side validation requires it.
- Ingestion MUST validate that the source's namespace exists in the catalog before
  writing (fail-fast) — or auto-register it only in a dry-run that is approved first.
- Unknown namespace in a query MUST be rejected with a clear error, not silently return
  empty.

## 3. Migration of the existing global graph `[TARGET]`

The existing graph has global IDs. Migration MUST:

1. Be a **separate, reviewable script** with a mandatory `--dry-run` that reports the
   full re-identification plan (old id → new namespaced id, plus edge re-pointing) with
   counts and samples.
2. Preserve data: re-point `MENTIONS`, `RELATED`, `HAS_*` edges to the new ids; fold old
   ids into `aliases` where they were canonical; keep `source_page` and descriptions.
3. Run idempotently (re-running is a no-op) and inside atomic transactions per entity
   cluster (same pattern as `scripts/resolve_entities.py::apply_merges`).
4. Produce a migration report (before/after counts, unresolved/ambiguous cases routed to
   a review list, never silently dropped).
5. Require explicit human approval before writing to a production graph.

## 4. Acceptance criteria

- [ ] Two corpora containing an identically-slugged entity produce two distinct,
  non-colliding ids.
- [ ] A shared entity referenced by two sources exists once, with per-source provenance
  and mention edges.
- [ ] A namespace-filtered query returns only matching-namespace results, enforced in
  Cypher (assert via query plan or by attempting cross-namespace leakage).
- [ ] Ingestion into an unregistered namespace fails fast (or auto-registers only after
  approved dry-run).
- [ ] Migration dry-run is complete and the apply step is idempotent with zero silent
  drops.

## 5. Tests

- **Unit:** id construction/reversibility; namespace validation; catalog lookup.
- **Integration (Neo4j):** namespace-filtered queries; shared-entity single-node
  assertion; migration idempotency + edge re-pointing.
- **Migration fixture:** a small synthetic global graph that exercises same-slug
  cross-corpus and shared-entity cases.

## 6. Decisions (closed 2026-09-02)

- **Id format:** `corpus:source:slug-type` — `source` is a MANDATORY identity component
  (separation by default; no cross-book collision by construction). Separator `:`.
  `domain` is optional and is NOT part of the id.
- **Unfiltered queries fail closed:** a read query without an explicit namespace/scope is
  rejected with a typed error, never "return everything". Agents must scope to a corpus
  and/or source.
- **Namespace catalog** lives in a versioned config file (source of truth), not in Neo4j
  initially.
- **Ordering vs. 01:** 02 precedes 01 (checkpoint `source_id` depends on 02's source
  identity). Confirmed.
- **Curation policy** (see 00 §8): index only in-scope sources you will actually query;
  each `source` is reversible (add/remove without destabilizing the graph).
