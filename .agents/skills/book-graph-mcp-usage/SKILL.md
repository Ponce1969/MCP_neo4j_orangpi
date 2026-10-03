---
name: book-graph-mcp-usage
description: "Trigger: calling the book-graph MCP tools (find_entity, traverse_relationships, search_chunks, list_entities, count_entities, search_rag, query_cypher, ask_global) or a call failing with missing_scope, invalid_scope or 401. Mandatory corpus:source scopes, caps, budgets, recipes, anti-patterns."
---

# Book Graph MCP Usage

Rules for calling the `book-graph-rag` MCP server (SSE, port 8003). All eight
tools are scope-bound and read-only. Follow the contracts below; they are
fail-closed, not advisory.

## 1. Tool reference

| Tool | Purpose | Key parameters | Returns |
|------|---------|----------------|---------|
| `find_entity` | look up entities by name/type | `name`, `entity_type?`, `source_id` | `{"entities": [...], "entity_not_found": bool}` |
| `traverse_relationships` | bounded outgoing traversal from one node | `source_id` (node), `scope_source_id` (scope), `rel_type?`, `depth` (default 1) | `{"entities": [...], "relationships": [...]}` |
| `search_chunks` | chunk text similarity | `query`, `limit` (default 10), `source_id` | `{"chunks": [...]}` |
| `list_entities` | paginated browse | `cursor` (default 0), `page_size` (default 50), `source_id` | `{"entities": [...], "next_cursor": int\|None}` |
| `count_entities` | count entities, optional type filter | `entity_type?`, `source_id` | `{"count": int}` |
| `search_rag` | unified chunks + entities + relations | `query`, `limit`, `include_relations`, `source_id` | `{"query", "entities", "relationships", "chunks", "entity_not_found", "total_results", "errors"}` |
| `query_cypher` | natural language → Cypher; **disabled by default** | `question`, `source_id` | `{"question", "error", "error_code", "cypher", "rows", "schema_source", "retries"}` |
| `ask_global` | community-level answer | `question`, `detail_level` 0–3 (default 1), `source_id` | list of `TextContent` |

## 2. Scope contract (mandatory)

- Every call MUST pass `source_id` in the exact form **`corpus:source`**
  (e.g. `knowledge:graphrag-agentic`). There is no global/unscoped mode.
- The four currently active sources (corpus `knowledge`), read from
  `catalog.yaml`:

  | `source_id` | Book |
  |-------------|------|
  | `knowledge:agentic-architectural-patterns` | Agentic Architectural Patterns |
  | `knowledge:graphrag-agentic` | GraphRAG agéntico (Alcaraz & Julien) |
  | `knowledge:essential-graphrag` | Essential GraphRAG |
  | `knowledge:ai-engineering-huyen` | Ingeniería de IA (Chip Huyen) |

- Fail-closed errors — handle them, do not retry blindly:

  | Condition | Error | Code |
  |-----------|-------|------|
  | no `source_id` at all | `MissingScopeError` | `missing_scope` |
  | malformed id, unknown corpus/source, or source not `active` | `InvalidScopeError` | `invalid_scope` |

- Optional narrowing lists (`book_ids`, `entity_types`,
  `relationship_types`) refine a scope; they never replace `source_id`.

## 3. The two-id trap in `traverse_relationships`

```
traverse_relationships(source_id=..., scope_source_id=...)
```

- `source_id` = the NODE to traverse: a full entity id `corpus:source:slug-type`.
  Take it from `find_entity` / `list_entities` / `traverse_relationships`
  output — never invent the slug.
- `scope_source_id` = the SCOPE, `corpus:source`.
- Passing a scope in `source_id` traverses nothing; passing a node as
  `scope_source_id` fails with `invalid_scope`. These are different arguments.

## 4. Caps and budgets

- **Depth:** keep traversal `depth` within **0–3** (default 1). The adapter
  clamps it in that range, and the graph layer can still reject an
  out-of-range depth with `TraversalDepthExceededError`: do not rely on
  clamping to fix a large depth.
- **Rows:** a read query materializes at most **1000 rows**; exceeding that
  raises `ResourceExhaustedError`. Narrow the scope; do not raise the cap.
- **Pages/limits:** `list_entities` defaults to `page_size=50` — follow
  `next_cursor`; `search_*` default to `limit=10`.
- **Tier budgets** (concurrency / calls per 60 s — budgets, not exposure
  gates):

  | Tier | Tools | Concurrent | Calls per 60 s |
  |------|-------|-----------|----------------|
  | LOW | `find_entity`, `traverse_relationships`, `search_chunks`, `list_entities`, `count_entities` | 8 | 60 |
  | MEDIUM | `search_rag`, `ask_global` | 4 | 30 |
  | HIGH | `query_cypher` | 1 | 5 |

  Hitting a budget is a rate-limit error: slow down, do not fan out more calls.

## 5. Empty result vs error

- Empty is success: `entity_not_found: true`, `count: 0`, `entities: []`, or
  `total_results: 0` are normal answers — report "not found", do not retry the
  same call or switch scopes hoping for rows.
- An exception with an `error_code` (`missing_scope`, `invalid_scope`,
  `policy_violation`, `resource_exhausted`, ...) or HTTP `401` is a failure:
  fix the request, do not reinterpret it as "no data".
- `search_rag` may return a non-empty `errors` array alongside partial
  results: treat it as partial failure and say so.

## 6. `query_cypher` — disabled by default

- Default (`mcp_enable_query_cypher=False`): the tool returns
  `error_code: "policy_violation"` immediately — no graph call, no LLM call.
  Do not "enable it" yourself; enabling is a maintainer decision.
- When enabled, guards still apply: read-only keyword prefilter
  (CREATE/DELETE/SET/MERGE/DETACH/REMOVE/DROP rejected), structural allowlist
  requiring a scope proof, an `EXPLAIN` gate, 2 retries, 10 s budget.
- Never reach for it first: the structured tools in §1 cover normal queries.

## 7. `ask_global` bounds

- `detail_level` must be an integer **0–3** (default 1); out of range raises
  `ValueError`. Higher is not better — pick the level that matches the
  question.

## 8. Cite provenance from `search_rag`

- Results carry provenance: chunks expose book/page/chunk positions; entities
  expose `source_page` and a `source` string like
  `book_id=...,chunk_index=...`.
- Always cite the source (book + page/chunk) in the answer. Never present a
  `search_rag` claim without its provenance, and never invent a page number.

## 9. Bearer token

- Every HTTP path requires `Authorization: Bearer <MCP_ACCESS_TOKEN>` when the
  token is configured. Get it from the environment (`set -a; . ./.env; set +a`);
  never hardcode it.
- HTTP `401` with `{"error":"unauthorized",...}` means auth is working and the
  token was not exported — export it and retry; it does not mean the service
  is broken.
- **Never echo, print, log, or write the token value** — not in output, files,
  tests, or error messages.

## 10. Recipes

```bash
# R1 — count entities per source
count_entities(source_id="knowledge:agentic-architectural-patterns")
count_entities(source_id="knowledge:graphrag-agentic")
count_entities(source_id="knowledge:essential-graphrag")
count_entities(source_id="knowledge:ai-engineering-huyen")

# R2 — find an entity (scoped)
find_entity(name="GraphRAG", source_id="knowledge:graphrag-agentic")
# -> if entity_not_found is true: report "not found", do not retry

# R3 — traverse from an entity id you already read (two-id contract: node vs scope)
traverse_relationships(
    source_id="<entity id from find_entity or list_entities output>",
    scope_source_id="knowledge:graphrag-agentic",
    depth=1,
)

# R4 — scoped semantic search with citation
search_rag(query="community detection", source_id="knowledge:graphrag-agentic", limit=5)
# -> cite source_page / chunk provenance from every entity and chunk returned

# R5 — cross-source isolation check (same query, different scope)
search_rag(query="evaluation", source_id="knowledge:ai-engineering-huyen", limit=3)
search_rag(query="evaluation", source_id="knowledge:essential-graphrag", limit=3)
# -> results must not leak across scopes; differing rows prove isolation

# R6 — global summary of one book
ask_global(question="What are the main ideas?", detail_level=1,
           source_id="knowledge:agentic-architectural-patterns")
```

## 11. Anti-patterns

- **Bare corpus scope** (`knowledge`) — invalid; scope is always
  `corpus:source`.
- **Guessed ids** — never invent slugs; take ids from tool output or
  `catalog.yaml`.
- **Reaching for `query_cypher` first** — it is disabled and guarded; use the
  structured tools.
- **Confusing the traversal node with the scope** — `source_id` is the node,
  `scope_source_id` is the scope (§3).
- **Treating an empty result as an error** — `entity_not_found: true` is a
  valid answer (§5).
- **Retrying a `401` or a rate-limit error unchanged** — fix the token
  export or slow down (§4, §9).
