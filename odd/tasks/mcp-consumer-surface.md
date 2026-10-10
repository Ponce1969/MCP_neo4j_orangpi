# bookgraph — MCP consumer surface: contexts and a graph version

Two gaps found while building `agentic-memory` (2026-10-10). Both are additive: no
existing consumer breaks, and nothing here changes what the tools compute.

## Specs

S1. `ask_global` must return **the contexts it used**, not only citation ids. The
    summaries are already fetched and scored inside the server, so returning their text
    costs no extra LLM call. (L1: "debria devolver citas y contexto , verdad ?" — the
    answer was yes; L2: "implementa C1 mas C2 bookgraph ahora")
S2. The `answer` string stays short: the new data travels in **sibling fields**, never
    inside the answer. Every added field is additive. (L2 + the 2026-10-09 conciseness
    work)
S3. The graph must expose a **data version identifier** a caller can name and pass back.
    (L1: "el paso dos tambien pasemos version identificador")
S4. The version is derived from a **reproducible census** (per-source chunk/entity counts
    plus the catalog version), not from a counter that needs its own state and can drift.
S5. The version is available both as an **MCP resource** (`bookgraph://version`) and
    stamped into the `ask_global` answer, so a consumer that caches an answer gets the
    version in the same round trip.
S6. No existing consumer breaks: the added fields are new keys, and `bookgraph://catalog`
    keeps its current shape (it gains `graph_version`).
S7. Gates: `ruff` + `mypy` + `scripts/validate_architecture.py` + `pytest` green before
    done.

## Tasks

- T1 — Domain: `graph_version_from(catalog_version, stats)` — a pure, order-independent
  digest over the census. S4. Inline, test-first.
  `feat(domain): reproducible graph version digest`
- T2 — `ask_global` returns `contexts` (`[{id, level, score, text}]`) and `usage`
  (`{llm_calls, detail_level, summaries_considered, summaries_used}`), including on the
  empty-summary paths so the shape never varies. S1, S2. Inline, test-first.
  `feat(ask-global): return the contexts and the usage`
- T3 — The MCP adapter stamps `graph_version` into the `ask_global` answer, registers
  `bookgraph://version`, and adds `graph_version` to `bookgraph://catalog`. S3, S5, S6.
  Inline, test-first. `feat(mcp): expose the graph version`
- T4 — Docs and evidence: README response shape, the backlog items marked implemented,
  and the production payload recorded in the Log after the deploy. S2, S5, S6. Inline.
  `docs: ask_global response shape and graph version`

## Log

L1 — 2026-10-10, user (verbatim): "le creamos uno en github y pusheamos si, el paso dos
tambien pasemos version identificador , evidencia , confianza no guardar low exacto ,
terma 3) debriamos resolverlo de que manera se podria hacer que funcione ? debria devolver
citas y contexto , verdad ?"

L2 — 2026-10-10, user (authorization, verbatim): "si a todo mergea a main implementa C1
mas C2 bookgraph ahora"

L3 — 2026-10-10, findings that motivate the change (evidence):
(a) the real `ask_global` payload read on 2026-10-10 is `{"answer": "- Logs for flat event
coverage ... [Data: CommunitySummary(007873583f96e774)] ...", "citations": [...]}` — ids
but no summary text, so a consumer cannot audit whether the summary supports the sentence;
(b) the census already exists and is cheap: `_CATALOG_STATS_CYPHER` in `mcp_server_main.py`
is one round trip used by `bookgraph://catalog`;
(c) the use case already returns a dict with `citations`, so the change is additive to an
existing shape rather than a new contract.
