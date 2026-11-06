# Orchestration Layer — Operations (T7)

## Purpose

Document the operational surface of the orchestration layer: the `book-graph-rag
orchestrate` command, its options, the three coverage states, the JSON contract,
and the exit codes. The layer **sits above the MCP, it never replaces it** —
the MCP keeps its scope gate, its budgets and its logging, and this command adds
a read-only path that decides evidence vs synthesis, keeps scope explicit, and
turns graph abstention into an honest state instead of an invented answer.

Spec home: `odd/tasks/tool-intent-router.md` (S1–S13; rulings L13/L14/L15).

## Requirements — where this can run

The command composes the real graph adapters, the LLM adapter and the BTRAG
text port, so it needs **both** of the following before it can build its stack:

| Group | Settings | Why |
|-------|----------|-----|
| Graph | `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD` (plus `NEO4J_READONLY_USER`/`NEO4J_READONLY_PASSWORD` when the read credential differs) | Both Neo4j adapters are constructed with `readonly_settings(...)` — this layer never writes. |
| Text store | `TEXT_RAG_URL`, `TEXT_RAG_TOKEN` | `BtrTextPassageAdapter` fails closed at construction on a blank url or token (L15), never at query time. |

Consequently this runs **where the graph is reachable** — on the host that binds
Neo4j to loopback — not from an arbitrary machine: from the outside, the graph
is not addressable and the stack fails closed before issuing any query. LLM
provider settings (`*_LLM_BASE_URL`, `*_LLM_MODEL_NAME`) are also validated at
construction. No host names, endpoints or tokens are hard-coded anywhere in
this layer or this document.

## Command

```bash
uv run book-graph-rag orchestrate "¿cómo se conectan los patrones de orquestación?" \
    --mode evidence --intent design --scope knowledge:agentic-architectural-patterns \
    --requires-passages
```

| Option | Values | Default | Meaning |
|--------|--------|---------|---------|
| `--mode` | `evidence` \| `synthesis` | `evidence` | Evidence (`search_rag`, cheap) vs synthesis (`ask_global`, expensive; explicit opt-in, S1). |
| `--intent` | `foundation` \| `design` \| `implementation` | omitted | Declared intent; applies the S13 tuning row (k / traversal / citation style). Omitted means no tuning — S13 defines no row for "no intent". |
| `--scope` | `CORPUS:SOURCE`, repeatable | omitted = all books | Explicit caller scope (S2). A malformed value is a usage error and the stack is never built. |
| `--requires-passages` | flag | off | Also fetch text passages from the text store, whatever the graph says (S5). |

An explicit `--scope` wins outright: the namespace router is not consulted
(S2/L14). Without it, the router proposes (single or top-2 fan-out), and in
`synthesis` mode a proposal that is not a committed namespace returns
`insufficient_coverage` **before any port is touched** — no `ask_global` is
spent on a guess (L13).

## Coverage states

| `state` | Meaning |
|---------|---------|
| `covered` | The validated scope produced graph evidence. |
| `fanout` | Top-2 / multi-scope dispatch merged evidence (S8, or a caller's multi-scope `--scope`). |
| `insufficient_coverage` | The graph did not cover the question; the query derives to text (S6/S7), or synthesis had no committed scope (L13). Data, never an error. |

## Output

One JSON object on stdout (sorted keys, indent 2):

| Field | Meaning |
|-------|---------|
| `state` | One of the three coverage states above. |
| `reason` | Diagnostic text naming what the router/resolver did; never a substitute for `state`. |
| `evidence` | Per item: `namespace` (`corpus:source`) + `citations` (ids verbatim — `chunk_id`s in evidence mode, community-summary ids in synthesis). |
| `passages` | Per item: `chunk_id` only — **never the passage text**; ids are what a caller cites, the text stays in the text store. |
| `scopes` | Namespaces the composition dispatched to (explicit scope, committed route, or fan-out). |
| `cache` | `cacheable` + `reason` (S10–S12). Computed and reported here; this repo never writes the cache (T5 boundary). |
| `intent_tuning` | The applied S13 row (`k`, `traverses_relationships`, `citation_style`), or `null` when no intent was declared. |
| `mode` | `evidence` or `synthesis`, as requested. |

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Every non-error outcome, **including `insufficient_coverage`** — data, not failure (same contract as `route`). |
| `1` | Configuration error (settings, fail-closed text-port config, LLM settings), a runtime error from a port, or a close failure with no earlier error. The message goes to stderr; no traceback or JSON on stdout. |

The stack is closed in a `finally` on every path; a close failure never masks
an earlier answer error (it is reported on stderr alongside it).

## Safety boundary

- Read-only: every Neo4j access uses the read-only credential view; the layer
  performs no write, no SSH, no container change.
- The MCP surface, its `require_scope` enforcement, its budgets and its query
  logging are untouched; this command is an additional caller, not a
  replacement.
- Nothing connects at import time: the stack is built inside the command, per
  invocation, and closed in a `finally`.

## Verification record

| Command | Result |
|---------|--------|
| `uv run pytest tests/unit/test_orchestration_cli.py tests/unit/test_compose_orchestration.py -q` | PASS — 25 tests. |
| `uv run ruff check .` | PASS. |
| `uv run ruff format --check .` | PASS. |
| `uv run mypy .` | PASS. |
| `uv run python scripts/validate_architecture.py` | PASS. |
