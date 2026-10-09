# 08 — Agent cache and graph recipes (design, not implemented)

**Status:** design agreed 2026-10-09. **Nothing here is implemented yet.**
Written so that any agent (or human) can pick this up in a later session without
re-reading the whole conversation that produced it.

---

## 1. Why this exists

Agents ask this graph the same questions repeatedly, and each `ask_global` call is
expensive: it currently scores **every** community summary of the chosen level with
one LLM call each (**~164 calls per question** at `detail_level=1`, measured
2026-10-09) before composing one answer. Repeating that work wastes money and adds
latency for no new information.

The goal is **not** a second retrieval engine. It is:

1. A **local answer cache** so a repeated question is answered from disk.
2. A **record of what actually helps**: which questions repeat, which recipes work,
   how the corpus drifts. That record is what turns cached answers into accumulated
   knowledge.

## 2. Decisions already made (with the reasons, so they are not re-litigated)

| Decision | Reason |
|---|---|
| **The cache lives OUTSIDE the MCP server.** | The MCP is read-only by design: scope obligation, structural Cypher allowlist, read-only authority, tier budgets and metadata-only HMAC logs are its security posture. A component that *writes* state must not live inside it. |
| **Exact-match only. No semantic / near-miss cache.** | A near-miss returns a *plausible but wrong* answer, which is the worst failure mode (a false success). Exact match on the normalized question cannot do that. |
| **The graph version is part of the cache key.** | Without it, an answer produced before a re-index survives it and silently answers with stale content. |
| **Lessons about the agent's *process* go to Engram, not here.** | Engram already is the transversal memory. Duplicating it in SQLite would create two sources of truth for the same concept. |
| **No LLM-generated Cypher.** | `query_cypher` is disabled by default, `Text2Cypher` requires a scope proof, and the allowlist is structural. Recipes may only store **approved** templates. |
| **Google Gemini context caching / NotebookLM were evaluated and are NOT the path.** | Verified 2026-10-09: NotebookLM (now "Gemini Notebook") has **no consumer API** — every MCP bridge is unofficial (browser session / reverse-engineered RPCs), with account risk and ~50 questions/day on free. Gemini context caching is **paid**: cached tokens $0.075/1M plus **$0.50 / 1M tokens per hour** of storage ⇒ a ~1M-token book kept warm ≈ **$12/day per book**. File API objects also expire after 48 h. Keep them as options, not as architecture. (Implicit caching is ON by default for Gemini 2.5+; if that route is ever revisited, start there, with no explicit cache management.) |

## 3. Stage 1 — cache plus its instrument (three tables)

```sql
-- 1) Which graph version produced each answer (the invalidation key)
CREATE TABLE graph_versions (
  scope       TEXT PRIMARY KEY,          -- 'knowledge:graphrag-agentic' (or '*' for everything)
  version     TEXT NOT NULL,             -- index hash/date (source_version + commit)
  recorded_at TEXT NOT NULL,             -- ISO-8601
  notes       TEXT
);

-- 2) The answer cache
CREATE TABLE answers (
  id            INTEGER PRIMARY KEY,
  q_norm        TEXT NOT NULL,           -- normalized question (trim, collapse spaces, lowercase)
  scope         TEXT NOT NULL,           -- corpus:source — the scope IS part of the identity
  tool          TEXT NOT NULL,           -- ask_global | search_rag | ...
  detail_level  INTEGER,                 -- parameter that changes the result
  graph_version TEXT NOT NULL,           -- from graph_versions
  answer        TEXT NOT NULL,
  citations     TEXT NOT NULL,           -- JSON: ["CommunitySummary(549c…)", …]
  model         TEXT NOT NULL,           -- which model produced it
  quality_score REAL,                    -- measured score for THIS answer, when available
  created_at    TEXT NOT NULL,
  last_hit_at   TEXT,
  hits          INTEGER NOT NULL DEFAULT 0,
  state         TEXT NOT NULL DEFAULT 'fresh',   -- fresh | stale | suspect
  UNIQUE (q_norm, scope, tool, detail_level, graph_version)
);

-- 3) The instrument: every lookup, hit or miss
CREATE TABLE cache_events (
  id         INTEGER PRIMARY KEY,
  at         TEXT NOT NULL,
  q_norm     TEXT NOT NULL,
  scope      TEXT NOT NULL,
  tool       TEXT NOT NULL,
  outcome    TEXT NOT NULL,              -- hit | miss | stale | error
  latency_ms INTEGER,
  source     TEXT                        -- 'agent:pi' | 'agent:opencode'
);

CREATE INDEX idx_events_at ON cache_events(at);

CREATE VIEW v_cache_savings AS
SELECT substr(at, 1, 10) AS day, count(*) AS lookups,
       sum(outcome = 'hit') AS hits, sum(outcome = 'miss') AS misses
FROM cache_events GROUP BY 1;
```

### Rules these tables encode

1. `graph_version` in the key ⇒ a re-index invalidates automatically.
2. `scope` in the key ⇒ books are never crossed (same discipline the MCP enforces).
3. `detail_level` and `model` in the key ⇒ a different configuration is a different answer.
4. `quality_score` + `state` ⇒ only answers that passed a bar are cached; doubtful ones are marked, not deleted.
5. Hits plus `cache_events` ⇒ the cache is an **instrument**: it shows which questions repeat, and that data decides the future routing policy.
6. `citations` ⇒ a cached answer keeps its provenance. Without citations it is not auditable.

## 4. Stage 2 — the lessons of *this* graph (only once stage 1 has data)

```sql
CREATE TABLE query_recipes (
  id          INTEGER PRIMARY KEY,
  intent      TEXT NOT NULL,     -- the intent in words
  scope       TEXT NOT NULL,
  tool        TEXT NOT NULL,
  template    TEXT,              -- APPROVED query/arguments (never LLM-generated Cypher)
  verified_at TEXT,              -- last time it was proven against the real graph
  evidence    TEXT,              -- commit/path/observation backing it
  UNIQUE (intent, scope)
);

CREATE TABLE corpus_snapshots (
  at          TEXT NOT NULL,
  scope       TEXT NOT NULL,
  entities    INTEGER,
  chunks      INTEGER,
  communities INTEGER,
  gate_state  TEXT,              -- pass | fail | incomplete
  PRIMARY KEY (at, scope)
);
```

`query_recipes` records knowledge that lives in neither the graph (which holds data,
not how to query it) nor Engram (which is transversal). `corpus_snapshots` gives a
time series of corpus health, so drift is visible without re-measuring everything.

## 5. Prerequisites, order, and how we will know it worked

**Order matters:** the cache's payoff is proportional to how expensive the graph path
is, so make the graph cheaper **first**.

1. **Deploy the pending commits** (`git pull --ff-only` + `sudo systemctl restart
   mcp-server`). They include the answer-length limit in the compose prompt, the
   per-question RAGAS scores in the gate report, and the RAGAS inputs kept at
   `data/evaluation/ragas_input_*.jsonl`. Then **measure**: same three questions,
   expecting ≤ ~800 chars and zero unicode escapes.
2. **Fix a project-owned RAGAS baseline.** Today's baseline is 67 questions over
   `evaluation_dataset_dedup.jsonl` while the gate evaluates 35 generated answers:
   the two are **not comparable**, so no delta means anything yet.
3. **Stage 1 cache** (three tables, one SQLite file, per agent/machine: simplest and
   needs no cross-user coordination).
4. **Stage 2 recipes/snapshots**, seeded from what stage 1 reveals.

**Acceptance criteria for stage 1** (all measurable):

- A repeated question is served from disk with `outcome='hit'` and a measurable
  latency reduction.
- After a re-index, previously cached answers are **not** served (they become
  `stale`), verified by a test.
- `v_cache_savings` shows a real hit rate after one week of normal use.
- Cached answers keep their citations and are traceable to `graph_version`.

## 6. Open decisions (for the maintainer, when we resume)

- **Where does the DB live:** per agent/machine (recommended) or shared with another
  user of the same graph (needs an access story).
- **Quality bar to admit an answer into the cache:** which `quality_score` and from
  which measurement (the gate's per-question RAGAS rows are the natural source).
- **TTL vs pure versioning:** expiring by age as well as by graph version.

## 7. Related work in flight (do not duplicate it)

- The graph-side cost fix (batched community scoring, so ~164 LLM calls become a few)
  is designed but **not started**.
- `ask_global_top_n` is already a Setting (default `8`).
- The readiness gate passes (2026-10-09 reference run) and its report now carries
  per-question RAGAS evidence.
