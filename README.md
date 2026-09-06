# book-graph-rag

> Knowledge-graph RAG indexer for the book
> *"Agentic Architectural Patterns for Building Multi-Agent Systems"*.
>
> Reads a PDF, splits it using a **semantic chunker driven by its own TOC**
> (not fixed char windows), extracts entities and relationships via an
> OpenAI-compatible LLM (Groq by default), and writes them to a **Neo4j**
> knowledge graph using idempotent `MERGE` upserts.

---

## What this is — and what it is **not** yet

This repository today is an **indexer**. It takes a PDF book and produces a
Neo4j graph of entities and relationships. That's it. The full product vision
is a knowledge base that AI agents can query, but the later phases are not
implemented yet.

| Phase | Status |
|---|---|
| 01 — Foundation (`Settings`, Fail-Fast config) | done |
| 02 — Domain & Ports (Pydantic entities + ABCs) | done |
| 03 — Infrastructure (`PDFAdapter`, `LLMAdapter`, `Neo4jCommandAdapter`) | done |
| 04 — Application (`IndexBookUseCase`, streaming + dead-letter) | done |
| 05 — CLI (`book-graph-rag index <pdf>`) | done |
| 06 — Query layer for the loaded graph | not started |
| 07 — MCP server to expose the graph to agents | not started |

The repo name `MCP_neo4j_orangpi` reflects the **intended final deployment**
(MCP server on an Orange Pi). Today there is no MCP server here yet. When
phase 07 lands, this README will say so plainly.

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

# 7. Index a book (smoke test, ~20-30 min against Groq free plan)
uv run book-graph-rag index data/your-book.pdf

# 8. Inspect the resulting graph
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

### Chunking model — "TORO"

The chunker is driven by the PDF's own bookmark TOC (hierarchical chapters
and sections). `PDF_MAX_CHUNK_SIZE` is a **safety ceiling**, not the primary
window: if a TOC section is longer than the ceiling, it is sub-divided by
characters (with overlap) while preserving the parent chapter/section
metadata. Without a TOC, it falls back to plain char-window chunking.

---

## Deployment on Orange Pi 5 Plus (production)

Target hardware: **Orange Pi 5 Plus, 16 GB RAM, ARM**. The graph lives here;
remote agents will query it (phase 06/07).

```bash
# On the Pi, over Tailscale / SSH:
git clone https://github.com/Ponce1969/MCP_neo4j_orangpi.git
cd MCP_neo4j_orangpi
uv sync
cp .env.example .env
# Edit .env on the Pi:
#   - NEO4J_BOLT_ADVERTISED_ADDRESS=<pi-tailscale-ip>:7687
#   - NEO4J_HTTP_ADVERTISED_ADDRESS=<pi-tailscale-ip>:7474
#   - GRAPH_LLM_API_KEY=<your graph-provider key> (optional for local providers)
#   - QUERY_LLM_API_KEY=<your query-provider key> (optional for local providers)
docker compose up -d
uv run book-graph-rag index data/your-book.pdf
```

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
│   └── models.py          # Book, Chapter, Section, PageRef, Entity, Relationship, KnowledgeGraphChunk
├── ports/
│   ├── pdf_port.py        # PDFReaderPort (ABC)
│   ├── llm_port.py        # LLMProviderPort (ABC)
│   └── graph_db_port.py   # GraphDatabasePort (ABC)
├── application/
│   └── index_book_use_case.py   # streaming + mini-batches + dead-letter
├── infrastructure/
│   ├── pdf_adapter.py     # pymupdf + TOC chunking
│   ├── llm_adapter.py     # instructor + AsyncOpenAI + tenacity
│       ├── neo4j_command_adapter.py   # async driver + MERGE upserts (write side)
    └── neo4j_query_adapter.py     # async driver + MATCH queries (read side)
└── main.py                # click CLI + CompositionRoot

scripts/
├── validate_architecture.py   # hexagonal layer-import gate
├── setup_env.py
└── run_indexer.py            # placeholder

docs/spec/                    # normative target specs (00..07) + README + roadmap
    └── archive/                  # historical phase notes (former docs/specs/)
tests/                        # 532 tests, all green
docker-compose.yml            # Neo4j 5.23 with APOC, 8 vars interpolated from .env
```

---

## Roadmap

- **Phase 06** — Query layer: read-side use cases for searching entities and
  traversing relationships in the loaded graph.
- **Phase 07** — MCP server: expose the query layer as an MCP tool so remote
  agents (Claude Desktop, opencode, etc.) can answer questions from the book's
  knowledge graph.
- **Production hardening** — Docker secrets for API keys, TLS for Bolt,
  Tailscale-only firewall, Neo4j heap tuning on the Pi.