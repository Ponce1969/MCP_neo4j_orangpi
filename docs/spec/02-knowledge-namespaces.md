# 02 — Knowledge Namespaces

> **Status: Implemented (closed 2026-09-23).** Namespaced ids (``corpus:source`` for
> books, ``corpus:source:slug-type`` for entities), the versioned catalog, and the
> idempotent in-place migration are implemented and **verified in production**:
> 7,111/7,111 ``:Entity`` ids namespaced, 1,520/1,520 ``:Chunk`` book_ids namespaced,
> ``:Book`` id = ``knowledge:agentic-architectural-patterns``. Server-side namespace
> filtering (fail-closed) was hardened in Phases 6/7 (R3/R7/R8).

## 1. Current state `[VERIFIED]`

- `Entity.id` is computed as `slugify(name)-type` or `slugify(canonical_name)-type`,
  **globally**, with no source/corpus/domain component.
  `[VERIFIED]` `infrastructure/llm_adapter.py::_resolve_entity_id`.
- `Book.id` is a slug of title or a hash of `(title, author)`. `[VERIFIED]`
  `domain/models.py::Book` and `application/index_book_use_case.py` (book upsert).
- `:Chunk` nodes carry `book_id` as a property; `MENTIONS` links chunks to entities.
- A single book is the only current source (`data/libro_Agentic_Architectural_Patterns.pdf`).
- There is **no** server-side filtering by source/corpus/domain; queries are global.

> **Historical baseline.** This section describes the pre-migration graph (verified
> 2026-09-02). The migration below has since been applied to production; see §1a.

### 1a. Post-migration state `[IMPLEMENTED]`

- Entity ids are `corpus:source:slug-type` (`knowledge:agentic-architectural-patterns:...`),
  built by `domain/namespaces.py::SourceNamespace.entity_id` and applied in
  `infrastructure/llm_adapter.py::_namespace_entity_id`. Verified in prod: 7,111/7,111.
- `Book.id` is the namespaced `corpus:source` (book id `knowledge:agentic-architectural-patterns`).
- `:Chunk` nodes carry the namespaced `book_id` (`knowledge:agentic-architectural-patterns:chunk-…`);
  1,520/1,520 in prod.
- Server-side filtering by namespace is enforced at the Cypher layer (R3 bind scope
  predicates, fail-closed without scope) — see 05/07 and `catalog_scope_resolver.py`.

## 2. Target model

### 2.1 Namespace hierarchy `[IMPLEMENTED]`

Logical, ordered namespaces:

| Level | Name | Meaning | Example |
|-------|------|---------|---------|
| 0 | `corpus` | A collection of related sources (a shelf/project) | `agentic-patterns` |
| 1 | `domain` | A subject area within a corpus | `multi-agent`, `mcp` |
| 2 | `source` (book/doc) | A single indexed document | `agentic-architectural-patterns` |

`domain` is optional (a corpus may have no domain split). Every entity belongs to at
least a `corpus` and, when extracted from a document, a `source`. `[IMPLEMENTED]`
(`catalog.yaml` declares ``corpora`` → ``sources``; ids always carry ``corpus:source``).

### 2.2 Entity identity and provenance `[IMPLEMENTED]`

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

`[IMPLEMENTED]` — `SourceNamespace` (pure domain), round-trip ``parse_entity_id`` /
``parse_book_id``, ids namespaced in the LLM adapter, no-collision unit tests
(`tests/test_namespaces.py`) and cross-namespace integration tests (Phase 6/7).

### 2.3 Shared entities and cross-domain relationships `[IMPLEMENTED]`

- A **shared entity** (same real-world concept referenced by multiple sources/domains) is
  represented by a single node with one identity, plus per-source **mention** edges and
  per-source provenance, NOT by duplicated nodes per source.
- Cross-domain relationships connect entities that legitimately span domains (e.g. a
  `mcp` tool in one domain `enables` a `pattern` in another).
- The decision of whether two entities are "the same" is governed by 03 (semantic
  resolution), not by namespace alone. Namespace scoping bounds the candidate set;
  resolution decides identity.

`[IMPLEMENTED]` — single-node identity is decided by resolution (Phase 3, `resolve_entities`
+ merge ledger); namespaced ids never collide by construction, so cross-source merges are
opt-in and auditable. Coverage: `test_neo4j_graph_merge_adapter.py`,
`test_resolution_lifecycle.py`.

### 2.4 Server-side filtering `[IMPLEMENTED]`

- Read queries MUST accept and enforce an optional namespace filter (corpus/domain/
  source) applied at the Cypher layer, not by post-filtering results in Python.
- When no filter is given, the query **fails closed**: it is rejected with a typed error
  requiring an explicit namespace/scope. "Return everything" is not a supported default
  (no-noise, no-chaos rule). This default is documented and consistent across all tools.

`[IMPLEMENTED]` — Phases 6/7: `ScopeResolver`/`ScopeContext` bind namespace predicates as
Cypher parameters (R3/R7/R8); unscoped reads fail closed with a typed error. Coverage:
13 integration tests incl. `test_cross_namespace_scope.py`, `test_query_cypher_scope.py`,
`test_scope_followups.py`.

### 2.5 Namespace validation / catalog `[IMPLEMENTED]`

- A namespace catalog records, for each known corpus/domain/source: its id, human label,
  source version, and status (`active`, `deprecated`).
- The catalog lives in a **versioned config file** (source of truth), not in Neo4j
  initially. A Neo4j mirror may be added later if server-side validation requires it.
- Ingestion MUST validate that the source's namespace exists in the catalog before
  writing (fail-fast) — or auto-register it only in a dry-run that is approved first.
- Unknown namespace in a query MUST be rejected with a clear error, not silently return
  empty.

`[IMPLEMENTED]` — `catalog.yaml` (versioned) + `CatalogLoader` + `Catalog.resolve_source`
raising `UnknownNamespaceError`; CLI `index --corpus/--source` fails fast with exit 2
(`Namespace error:`). Coverage: `tests/test_catalog_loader.py`, CLI test
`test_cli_index_unknown_namespace_fails_fast`.

## 3. Migration of the existing global graph `[IMPLEMENTED]`

Migration MUST:

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

`[IMPLEMENTED]` — `scripts/migrate_namespaces.py` (dry-run default, `--apply --yes`),
idempotent in-place rewrites (ids are node properties, so no edge re-pointing needed),
aliases folded, unresolved cases routed to a review list; `tests/test_migrate_namespaces.py`
(23 tests). **Applied to production** (verified 2026-09-23): 7,111/7,111 Entity ids,
1,520/1,520 Chunk book_ids, Book id namespaced.

## 4. Acceptance criteria

- [x] Two corpora containing an identically-slugged entity produce two distinct,
  non-colliding ids. (`test_same_slug_in_different_*_produce_distinct_ids`)
- [x] A shared entity referenced by two sources exists once, with per-source provenance
  and mention edges. (resolution layer, Phase 3 + merge ledger)
- [x] A namespace-filtered query returns only matching-namespace results, enforced in
  Cypher (assert via query plan or by attempting cross-namespace leakage).
  (Phase 6/7 integration tests)
- [x] Ingestion into an unregistered namespace fails fast (or auto-registers only after
  approved dry-run). (`test_catalog_loader.py` + CLI exit 2 test)
- [x] Migration dry-run is complete and the apply step is idempotent with zero silent
  drops. (`test_migrate_namespaces.py`, applied in prod)

## 5. Tests

- **Unit:** id construction/reversibility; namespace validation; catalog lookup.
  (`tests/test_namespaces.py`, `tests/test_catalog_loader.py`)
- **Integration (Neo4j):** namespace-filtered queries; shared-entity single-node
  assertion; migration idempotency + edge re-pointing. (Phase 6/7 scope suites +
  `test_neo4j_graph_merge_adapter.py`)
- **Migration fixture:** a small synthetic global graph that exercises same-slug
  cross-corpus and shared-entity cases. (`tests/test_migrate_namespaces.py`)
- **CLI fail-fast:** `test_cli_index_unknown_namespace_fails_fast` (exit 2, no adapters
  constructed).

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
