# book-graph-rag

> Knowledge-graph RAG indexer for a growing corpus of technical books — today four
> namespaces: *Agentic Architectural Patterns for Building Multi-Agent Systems*,
> *Essential GraphRAG*, *GraphRAG agéntico* and *Ingeniería de IA* (Chip Huyen).
>
> Reads a PDF, splits it using a **semantic chunker driven by its own TOC**
> (not fixed char windows), extracts entities and relationships via an
> OpenAI-compatible LLM (**DeepSeek** in production), and writes them to a **Neo4j**
> knowledge graph using idempotent `MERGE` upserts.

---

## Current status — 2026-10-09

Phase 5 (evaluation and readiness) is complete, archived and **passing**: the
readiness gate returns exit `0` instead of the old `INCOMPLETE`, because the three
project-owned baselines now carry finalized numeric thresholds. Phase 6 (MCP
hardening) is deployed and serving.

| Area | Current state | Evidence / location |
|---|---|---|
| Phase 0 — Evidence baseline | complete | `docs/spec/` and roadmap |
| Phase 1 — Namespaces | complete — **four books** | `catalog.yaml`, `docs/spec/02-knowledge-namespaces.md` |
| Phase 2 — Resumable indexing | complete | `:Checkpoint` nodes and resume CLI |
| Phase 3 — Semantic resolution | complete with W1 follow-up | `docs/spec/03-semantic-entity-resolution.md` |
| Phase 4 — Scoped audit and gates | complete | `book-graph-rag audit` / `gate` |
| Phase 5 — Evaluation and readiness | complete, archived, **gate passing** | `docs/spec/06-evaluation-and-readiness.md`, `data/evaluation/README.md` |
| Phase 6 — MCP hardening | deployed and serving | allowlist, scope, read-only session, budgets, HMAC logs, private bind |
| Phase 7 — Guarded exposure | pending explicit approval | production exposure gate |
| Community layer | complete — 4/4 namespaces | `:CommunitySummary`, `ask_global` answers for every book |
| Skill quality gate | implemented, **off by default** | `SKILL_GATE_ENABLED`, `data/evaluation/skill_gate_evidence.json` |
| Namespace router | implemented, **off by default** | `ROUTER_ENABLED`, `data/router/namespace_profiles.json` |
| Orange Pi MCP service | running | `mcp-server.service`, streamable HTTP `/mcp` plus legacy `/sse` on port `8003` |
| Neo4j production graph | healthy | Docker service `bookgraph-neo4j` |

### Deployment facts

- Repository `main` is at `393d596`; the Orange Pi deployment runs `69d7931`
  (the newer commits are documentation and evaluation changes, pending the next
  `git pull` and service restart).
- The application repository lives on the Pi at
  `/home/gonzalo/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi`.
- The MCP server runs under systemd as `mcp-server.service`; it is **not** a
  Docker Compose service, and it binds the Tailscale address only.
- Docker Compose manages the Neo4j container only: `bookgraph-neo4j`, whose ports
  are published on `127.0.0.1` only.
- Neo4j remains untouched by application deploys; no containers, volumes, or
  databases are deleted as part of deployment.
- Reference readiness run (2026-10-09), `gate expose-mcp-readiness --target
  bookgraph-neo4j`: **exit 0**, with resolution f1 `0.6410`, generation
  faithfulness `0.792` and retrieval precision@k `0.0482`. The extraction layer
  stays `pending` ("deferred per R6.1") and is not blocking.

## What this is — and what it is **not** yet

This repository is an indexer, evaluation/readiness gate, and MCP server for a
Neo4j knowledge graph. MCP service operation is deployed and MCP hardening is
implemented; the final guarded exposure gate is still pending. The graph is never
mutated by evaluation or readiness checks.

---

## Architecture — hexagonal (ports & adapters)

```
                          ┌─────────────────────────────────┐
                          │                                 │
   CLI  (click) ────────▶│       IndexBookUseCase           │  (application)
   main.py               │  - asyncio.Queue + Sentinel      │
   (CompositionRoot)     │  - asyncio.Semaphore             │
                         │  - Dead-letter JSONL             │
                         └────────┬────────────────────────┘
                                  │ depends on
                ┌─────────────────┼─────────────────┐
                ▼                 ▼                 ▼
        PDFReaderPort    LLMProviderPort    GraphDatabasePort   (ports: ABCs)
                ▲                 ▲                 ▲
                │                 │                 │
        PDFAdapter         LLMAdapter      Neo4jCommandAdapter  (infrastructure)
        (pymupdf +      (instructor +     (AsyncGraphDatabase
         TOC algo)       AsyncOpenAI +      driver, MERGE,
                          tenacity)          idempotent)
```

### Layer rules (enforced by `scripts/validate_architecture.py`)

| Layer | May import | May NOT import |
|---|---|---|
| `domain/` | stdlib, `pydantic` | anything external |
| `ports/` | `domain`, stdlib, `abc` | infrastructure, external libs |
| `application/` | `domain`, `ports`, stdlib | `infrastructure`, external libs |
| `infrastructure/` | `ports`, `domain`, external libs | `application` |
| `main.py` (CompositionRoot) | everything | — |

`Settings` lives in `src/book_graph_rag/config.py` (NOT under `domain/`)
because `pydantic-settings` is an external dependency and the domain layer
must remain pure. The application layer **never** receives `Settings`; it
receives primitives (`int`, `Path`) and port instances.

---

## Stack

- **Python 3.13**, `uv` (no pip / poetry / pipenv)
- `pydantic` v2, `pydantic-settings`
- `instructor` (typed LLM extraction via `AsyncOpenAI`)
- `neo4j` Python driver (async)
- `pymupdf` (`fitz`) for PDF reading and TOC bookmarks
- `tenacity` (exponential backoff for LLM calls)
- `click` (CLI)
- `ruff` + `mypy --strict` + `pytest` + `pytest-asyncio`

---

## Local setup (Windows / Linux / macOS dev)

```powershell
# 1. Clone
git clone https://github.com/Ponce1969/MCP_neo4j_orangpi.git
cd MCP_neo4j_orangpi

# 2. Install deps
uv sync

# 3. Configure environment (copy template and edit values)
Copy-Item .env.example .env      # Windows
# cp .env.example .env          # Linux/macOS
# Edit .env: at minimum set NEO4J_PASSWORD and the GRAPH_LLM_* / QUERY_LLM_* role settings

# 4. Start Neo4j
docker compose up -d

# 5. Sanity-check the indexer pipeline
uv run book-graph-rag --help
uv run book-graph-rag index --help

# 6. Run the gates (must all exit 0)
uv run ruff format . --check
uv run ruff check .
uv run mypy src
uv run python scripts/validate_architecture.py
uv run pytest -v

# 7. Index a book (end-to-end smoke test; LLM extraction dominates the runtime)
uv run book-graph-rag index data/your-book.pdf

# 8. Audit the graph (read-only structural health check)
uv run book-graph-rag audit --target bookgraph-neo4j --output audit-report.json

# 9. Evaluate a layer or run the readiness gate over the whole graph / a namespace scope
uv run book-graph-rag evaluate --layer resolution
uv run book-graph-rag gate expose-mcp-readiness --target bookgraph-neo4j
uv run book-graph-rag gate expose-mcp-readiness --target bookgraph-neo4j --scope knowledge:agentic-architectural-patterns

# Dataset schemas, baseline format, and threshold mechanism are documented in:
#   data/evaluation/README.md

# 10. Inspect the resulting graph
# Open http://localhost:7474 in a browser (Neo4j Browser), run:
#   MATCH (n) RETURN n LIMIT 25;
```

### Resumable indexing (Phase 2)

The `index` command now persists per-chunk `:Checkpoint` nodes in Neo4j so an
interrupted run can resume without re-spending LLM tokens on already-`PROCESSED`
chunks:

- `--resume` / `--no-resume` — skip `PROCESSED` chunks (default) or run a
  one-shot legacy flush.
- `--force-reprocess` — re-process `PROCESSED` chunks whose version dimensions
  changed.
- `--replay-dead-letter` — re-process failed chunks from `data/dead_letter.log`;
  combine with `--limit N` and optional `--source-id corpus:source`.
- `--backfill-checkpoints` — create `PROCESSED` checkpoints for a legacy graph
  without re-indexing; use `--dry-run` to preview or `--apply --approval <file>`
  to write.

```powershell
# Resume an interrupted run
uv run book-graph-rag index data/your-book.pdf --corpus <corpus> --source <source> --resume

# Replay up to 10 dead-letter records
uv run book-graph-rag index data/your-book.pdf --replay-dead-letter --limit 10 --source-id corpus:source

# Backfill checkpoints on a legacy graph (dry-run first)
uv run book-graph-rag index data/your-book.pdf --backfill-checkpoints --source-id corpus:source --dry-run
```

### Semantic entity resolution (Phase 3)

A staged, conservative pipeline (S0 normalization -> S1 embedding retrieval -> S2 type
gate -> S3 context validation -> S4 confidence bands) consolidates duplicate entities:

- **Bands:** `exact` auto-merges; `high` goes to a human-confirm queue; `medium` goes to
  quarantine; `low` is never merged. Cross-type and cross-namespace pairs are hard
  boundaries.
- **CLI:** `uv run book-graph-rag resolve-entities --dry-run` (legacy path is unchanged;
  set `RESOLUTION_STRATEGY=hybrid` to run the new pipeline), or use the evaluation
  harness: `uv run python scripts/run_evaluation_harness.py --model
  paraphrase-multilingual-MiniLM-L12-v2 --variant A`.
- **Safety:** duplicates are soft-deleted via `merged_into` (never deleted); every
  applied merge appends to the append-only, chained-SHA-256 ledger
  `data/resolution/merge_ledger.jsonl`; `medium`/`high` candidates wait in
  `data/resolution/quarantine.jsonl` until human review; every merge is reversible
  (`resolve_entities.py rollback --ledger <path> --entry <seq>`).
- **Production:** applying merges against the production graph requires the same
  backup -> dry-run -> explicit human approval gate as any destructive op
  (`AGENTS.md` §7).

### Chunking model — "TORO"

The chunker is driven by the PDF's own bookmark TOC (hierarchical chapters
and sections). `PDF_MAX_CHUNK_SIZE` is a **safety ceiling**, not the primary
window: if a TOC section is longer than the ceiling, it is sub-divided by
characters (with overlap) while preserving the parent chapter/section
metadata. Without a TOC, it falls back to plain char-window chunking.

---

## Deployment on Orange Pi 5 Plus (production)

Target hardware: **Orange Pi 5 Plus, 16 GB RAM, ARM**, connected through
Tailscale. The production graph and MCP service are already running there.

### Components and ownership

| Component | Runtime | Current state |
|---|---|---|
| Neo4j graph | Docker Compose | `bookgraph-neo4j`, healthy |
| MCP SSE server | systemd | `mcp-server.service`, port `8003`, active |
| Application environment | `uv` | `/home/gonzalo/.local/bin/uv` |
| Repository | Git checkout | `/home/gonzalo/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi` |

Docker Compose manages **only Neo4j** in this deployment. The MCP server is a
separate systemd service, so updating the application does not require
rebuilding or restarting Docker.

### Safe application update

Run these commands on the Pi over Tailscale/SSH. They update only this
repository and its own systemd service:

```bash
cd /home/gonzalo/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi
git pull --ff-only
/home/gonzalo/.local/bin/uv sync --extra community
sudo systemctl restart mcp-server
systemctl status mcp-server --no-pager
```

### Read-only post-deploy checks

```bash
# MCP SSE endpoint
curl -H 'Accept: text/event-stream' http://127.0.0.1:8003/sse

# Neo4j container health; do not run compose down, prune, or volume commands
docker compose ps
```

**Production safety:** never run `docker compose down`, `docker system prune`,
`docker volume rm`, `docker rm`, `DROP DATABASE`, or delete project files as
part of an application deploy. Other projects and PostgreSQL services share
this Orange Pi. Graph mutations require the explicit backup → dry-run → human
approval protocol in `AGENTS.md` §7.

Recommended Neo4j heap on a 16 GB Pi: `NEO4J_server_memory_heap_max__size=2G`
(leave RAM for the indexer and OS).

---

## Environment variables (`.env`)

All configuration is Fail-Fast: if a required variable is missing, the
process refuses to start. `SecretStr` fields are never logged in plain text.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `NEO4J_URI` | yes | — | Bolt URI (`bolt://host:7687`) |
| `NEO4J_USER` | yes | — | Neo4j username |
| `NEO4J_PASSWORD` | yes | — | Neo4j password (`SecretStr`) |
| `NEO4J_BROWSER_PORT` | yes | — | Browser host port (docker-compose) |
| `NEO4J_BOLT_PORT` | yes | — | Bolt host port (docker-compose) |
| `NEO4J_BOLT_ADVERTISED_ADDRESS` | yes | — | `host:port` reported to Bolt clients |
| `NEO4J_HTTP_ADVERTISED_ADDRESS` | yes | — | `host:port` reported to Browser |
| `NEO4J_PLUGINS` | yes | — | JSON array, e.g. `["apoc"]` |
| `GRAPH_LLM_API_KEY` | no | `None` | Graph construction and community-summary key (`SecretStr`); optional for local providers |
| `GRAPH_LLM_BASE_URL` | runtime | — | OpenAI-compatible endpoint for graph construction and community summaries |
| `GRAPH_LLM_MODEL_NAME` | runtime | — | Model used for graph extraction and community summaries |
| `QUERY_LLM_API_KEY` | no | `None` | Query, scoring, and answer-composition key (`SecretStr`); optional for local providers |
| `QUERY_LLM_BASE_URL` | runtime | — | OpenAI-compatible endpoint for Text-to-Cypher, scoring, and answer composition |
| `QUERY_LLM_MODEL_NAME` | runtime | — | Model used for Text-to-Cypher, scoring, and answer composition |
| `PDF_MAX_CHUNK_SIZE` | no | `1500` | Safety ceiling for chunk size (chars) |
| `PDF_CHUNK_OVERLAP` | no | `150` | Overlap when sub-dividing oversized chunks |
| `LLM_MAX_CONCURRENCY` | no | `3` | Max simultaneous LLM calls (`Semaphore`) |
| `PROCESSING_BATCH_SIZE` | no | `5` | Mini-batch size for graph upserts |
| `LLM_MAX_RETRIES` | no | `3` | Tenacity attempts per LLM call |
| `LLM_RETRY_WAIT_MULTIPLIER` | no | `1.0` | Tenacity exponential multiplier |
| `LLM_RETRY_WAIT_MAX` | no | `30.0` | Tenacity max wait between attempts |

Failure chunks (all retries exhausted) are appended as JSONL to
`data/dead_letter.log` with `chunk_index`, `page_ref`, `error_type`,
`error_message`, and a UTC timestamp. The pipeline does NOT abort on chunk
failures.

---

## Quality gates (must all pass before merging)

```powershell
uv run ruff format . --check
uv run ruff check .
uv run mypy src
uv run python scripts/validate_architecture.py
uv run pytest -v
```

`validate_architecture.py` uses AST analysis to enforce layer-import rules;
it is a hard architectural gate, not a stylistic linter.

---

## Repository layout

```
src/book_graph_rag/
├── config.py              # Settings (Fail-Fast, SecretStr, cross-field validator)
├── domain/
│   ├── models.py          # Book, Chapter, Section, PageRef, Entity, Relationship, KnowledgeGraphChunk
│   ├── audit_models.py    # AuditReport, AuditScope, RULE_CATEGORY, severity mapping, exit codes
│   └── gate_models.py     # GatePolicy, ReadinessGate, GateResult
├── ports/
│   ├── pdf_port.py        # PDFReaderPort (ABC)
│   ├── llm_port.py        # LLMProviderPort (ABC)
│   ├── graph_db_port.py   # GraphDatabasePort (ABC)
│   └── graph_audit_port.py # GraphIntegrityAuditPort (ABC)
├── application/
│   ├── index_book_use_case.py   # streaming + mini-batches + dead-letter
│   ├── audit_graph_use_case.py  # read-only graph audit orchestration
│   ├── evaluate_gate_use_case.py # readiness gate evaluation
│   └── resolve_audit_scope.py   # corpus[:source] validation
├── infrastructure/
│   ├── pdf_adapter.py     # pymupdf + TOC chunking
│   ├── llm_adapter.py     # instructor + AsyncOpenAI + tenacity
│   ├── neo4j_command_adapter.py   # async driver + MERGE upserts (write side)
│   ├── neo4j_query_adapter.py     # async driver + MATCH queries (read side)
│   ├── neo4j_audit_adapter.py     # static Cypher audit collector
│   ├── gate_policy_loader.py      # gates.yaml loader
│   └── catalog_loader.py          # catalog.yaml loader
└── main.py                # click CLI + CompositionRoot

scripts/
├── validate_architecture.py   # hexagonal layer-import gate
├── setup_env.py
└── run_indexer.py            # placeholder

docs/spec/                    # normative target specs (00..07) + README + roadmap
    └── archive/                  # historical phase notes (former docs/specs/)
tests/                        # unit, property, and testcontainers suites
deploy/                       # systemd unit and Orange Pi deployment notes
    docker-compose.yml            # Neo4j 5.23 with APOC, vars interpolated from .env
```

---

## MCP hardening (Phase 6) — implemented

Enforced at the MCP boundary (verified against `src/`):

- **Server-side scope:** `source_id` is validated against the namespace catalog and
  propagated as a `ScopeContext` through all 8 structured tools; scope is required
  and cross-namespace traversal is blocked.
- **Read-only authority:** the query path runs through `READ_ACCESS` + managed
  `execute_read`; write statements are rejected with a typed error (proven by a
  write-rejection integration test).
- **Structural Cypher policy:** `query_cypher` is disabled by default; when enabled,
  only a read-only allowlisted subset is accepted (no keyword denylist) and `EXPLAIN`
  is mandatory.
- **Tiered budgets:** per-tool LOW/MEDIUM/HIGH concurrency/rate limits plus hard
  row/node/traversal caps, failing with typed errors.
- **Private bind:** `mcp_bind_host` defaults to `127.0.0.1`; production rejects
  wildcard/public binds; the systemd unit binds the Tailscale IP.
- **HMAC logs:** the query log is metadata-only — raw query/prompt/error text never
  enters the JSONL; requests are correlated via keyed HMAC-SHA256 fingerprints.

Known open risks (recorded in `docs/spec/07`): Text2Cypher requires a scope proof but
does not yet bind `$param` values end-to-end. Production exposure still requires the
readiness/security gate and an explicit human decision. (`ask_global` used to expose
cross-namespace summaries; the scoped community read now filters by namespace, and
production isolation was verified: the same query returns 46 entities in book 4 and 2
in book 1.)

---

## MCP consumer surface — contexts and graph version

`ask_global` returns what it used, not only citation ids:

```json
{
  "answer": "...",
  "citations": ["CommunitySummary(6ab8a4a1)"],
  "contexts": [{"id": "CommunitySummary(6ab8a4a1)", "level": 1, "score": 87, "text": "..."}],
  "usage": {"llm_calls": 7, "detail_level": 1, "summaries_considered": 141, "summaries_used": 8},
  "graph_version": "gv-1a2b3c4d5e6f"
}
```

`contexts` is what makes an answer auditable: a citation id proves a summary exists, while
its text shows whether that summary supports the sentence. Returning it costs no extra
model call, because the summaries were already fetched and scored for ranking.

`graph_version` names the graph state the answer came from, so a consumer that caches an
answer can tell when it stopped being current. It is a structural digest of the same cheap
census `bookgraph://catalog` reads, it is readable on its own at `bookgraph://version`, and
it costs no model call. A failed census read reports `null` rather than a guess.

## Roadmap

- **Phase 05 — Evaluation and readiness:** complete and archived. The thresholds are
  finalized from project-owned baselines, so the readiness gate passes or fails for
  real (see the reference run above).
- **Phase 06 — MCP hardening:** implemented through T-I.1 (24/26 tasks). Server-side
  namespace scoping, read-only authority, structural Cypher allowlist, tiered
  concurrency/rate budgets, private fail-closed bind, and metadata-only HMAC query
  logs are in place. Text2Cypher scope-parameter binding remains open (see the note
  above).
- **Phase 07 — Guarded exposure:** pending explicit approval and all readiness /
  security preconditions. The MCP service is deployed, but deployment is not
  equivalent to approval for unrestricted exposure.

### Queued next steps

- **Deploy the pending commits** (`git pull --ff-only` + `systemctl restart
  mcp-server`) and measure them: the answer composer now enforces an explicit
  length limit, and the readiness report keeps per-question RAGAS scores plus the
  answers and contexts it sent (`data/evaluation/ragas_input_*.jsonl`).
- **Answer quality.** The last measured RAGAS numbers are answer_relevancy
  `0.5953` and context_precision `0.474`, on a baseline of 67 questions over a
  different dataset than the gate's 35 generated answers: the two are not directly
  comparable, so a project-owned baseline comes first. The known cost driver is
  `ask_global` scoring **every** summary of the chosen level with one LLM call each
  (164 at level 1), which also makes the top-8 selection nearly arbitrary among
  ties; batching that scoring is the highest-value change.
- **Tunable flags, still off:** `SKILL_GATE_ENABLED` (filters the MCP tool surface
  from the seeded `:Skill` nodes) and `ROUTER_ENABLED` (namespace routing; no caller
  uses the adapter yet, so enabling it changes nothing today). `ask_global_top_n` is
  now a Setting (`ask_global_top_n`, default `8`) for the context-precision/recall
  trade-off.
- **Product direction:** *to be written with the maintainer* — what "enterprise
  ready" means for this graph and which of the queued items it gates.
- **Phase 07 — Guarded exposure:** pending explicit approval and all readiness /
  security preconditions. The MCP service is deployed, but deployment is not
  equivalent to approval for unrestricted exposure.
- **Operational hardening:** Docker secrets for API keys, TLS for Bolt,
  Tailscale-only firewall, and Neo4j heap tuning on the Pi.