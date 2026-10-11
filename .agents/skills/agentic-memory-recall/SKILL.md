---
name: agentic-memory-recall
description: "Trigger: a corpus question about to be answered with ask_global, a repeated question that would cost LLM calls again, or a question whose answer may already be cached. Recipe for the local agentic-memory query cache around the book graph: read the graph version, ask the cache first, call ask_global only on a miss, store the answer with its version and citations, and measure the hit rate."
---

# Agentic Memory Recall

The `agentic-memory` SQLite cache answers an **exact** ask without touching the graph and
without any LLM call. Use it around `ask_global`: ask the memory first, pay the graph only
on a miss, and store what the graph answered so the next identical ask is free.

The memory queries nothing. It is queried. Everything it knows, including the graph
version, arrives as caller input, and the graph never learns the memory exists.

## 1. CLI contract

Every command prints one JSON object to stdout, and the exit code carries the decision.

| Exit | Meaning |
| --- | --- |
| `0` | `ask` hit: the JSON carries `answer`, `sources`, `confidence`, `stored_at`. `put` succeeded: `{"stored": true, "digest": ...}` |
| `1` | `ask` miss: `{"hit": false}`. Not a failure, it means produce the answer and store it |
| `2` | refused, in the vocabulary of the operation: `{"hit": false, "reason": ...}` from `ask`, `{"stored": false, "reason": ...}` from `put`. Fix the request; the refusal was recorded |

Branch on the exit code, never on prose.

`MEMORY_CLI` is the agentic-memory entry point. From this checkout:
`C:/Users/cerra/codigo/agentic-memory/.venv/Scripts/memory.exe`, or
`uv run --project C:/Users/cerra/codigo/agentic-memory memory`. Keep it in one variable so
a moved checkout is a one-line change. The database is `$AGENTIC_MEMORY_DB_PATH`, else
`~/.agentic-memory/memory.db`; WAL is on, so this never blocks a concurrent writer.

## 2. What makes two asks the same ask

All six fields must match: the normalized question (Unicode NFKC, case folded, whitespace
collapsed, trailing sentence punctuation dropped), `scope`, `tool`, `detail_level`,
`graph_version`, `model`.

A surface variant of the question hits. Anything else is a different ask, and in
particular a **new graph version is a miss, never a stale hit**.

## 3. The recipe

```bash
MEMORY_CLI="C:/Users/cerra/codigo/agentic-memory/.venv/Scripts/memory.exe"

# 1. The current graph version: a cheap resource read, zero LLM calls.
#    bookgraph://version -> "gv-<hex>"; the same value is stamped in every answer.
V="<version from bookgraph://version>"
SCOPE="<corpus:source, the same scope you will pass to ask_global>"
LEVEL=1
MODEL="deepseek-v4-flash"   # the running service's query model; see §4

# 2. Ask the memory first.
"$MEMORY_CLI" ask --question "$Q" --scope "$SCOPE" --tool ask_global \
  --detail-level "$LEVEL" --graph-version "$V" --model "$MODEL"
# exit 0 -> hit: answer from the cache with 0 LLM calls. Cite it and stop here.
# exit 1 -> miss: continue to step 3.
# exit 2 -> refused: the request is malformed. Fix it, do not retry it unchanged.

# 3. Pay the graph once.
#    ask_global(question=Q, detail_level=LEVEL, source_id=SCOPE)
#    -> {answer, citations, contexts, graph_version, usage}

# 4. Store it under the version the ANSWER came with, never under the one you guessed.
"$MEMORY_CLI" put --question "$Q" --answer "<answer>" \
  --source "<citation id>" --confidence high \
  --scope "$SCOPE" --tool ask_global --detail-level "$LEVEL" \
  --graph-version "<graph_version from the answer>" --model "$MODEL"

# 5. Measure instead of believing.
"$MEMORY_CLI" stats
```

## 4. Field rules worth getting wrong once

- **`SCOPE`** is the same `corpus:source` you pass to `ask_global`. A bare corpus is not a
  scope, and a different book is correctly a different ask.
- **`LEVEL`** must equal the `detail_level` you actually asked the graph with. Level 0 and
  level 1 are different asks with different answers, so mixing them silently misses.
- **`MODEL`** is the model that composed the answer. Read it from the **running service's**
  environment (`QUERY_LLM_MODEL_NAME`), never from this repository's `.env`, which is stale
  in both directions: it names `meta/llama-3.1-70b-instruct` for queries and
  `deepseek-v4-pro` for the graph builder, while the deployed service sets
  **`QUERY_LLM_MODEL_NAME=deepseek-v4-flash`** and `GRAPH_LLM_MODEL_NAME=deepseek-v4-flash`,
  both against the DeepSeek endpoint (the `NVIDIA` comment in that section is stale too).
  The graph does **not** report the composing model in `usage` either, so this value is a
  hand-maintained fact, and the first thing to check when a hit that should happen stops
  happening. Never pass a placeholder: the model is part of the key.
- **`--source`** repeats once per citation. The answer's `citations` are the natural values,
  and a stored answer without a source is refused (exit 2) by design.
- **`--confidence`**: `high` when the stored answer is the graph's own composition over the
  contexts it returned; `medium` when it is partial or a summary; `low` is refused, recorded
  as a refusal, and never stored. Do not dress a weak answer as `high` to get it stored.
- **Step 4 uses the answer's version, not step 1's.** If a re-index landed in between, the
  entry is keyed under the current version, so the next ask with the fresh version hits.
  The mismatch is self-correcting; the reverse (storing under the old version) would bury
  the answer where nothing can reach it.

## 5. Re-index, model change, invalidation

- A **re-index** changes `graph_version`. Previously stored answers are then misses on their
  own, because the version is part of the key. Stale answers cannot surface.
- **Dropping a version** is explicit, for example after deciding an answer was wrong:
  `"$MEMORY_CLI" invalidate --graph-version "$V"`.
- A **model change** changes the key, so entries from the old model are never hit again and
  are never invalidated either: they stay as harmless weight. Clear the version when the
  model changes so the counter keeps meaning something.

## 6. Anti-patterns

- **Asking the memory with an invented version.** The version is read from
  `bookgraph://version`, or taken from the answer that carried it. Never a date, never a
  guess: a guessed version silently misses forever.
- **Treating a miss as a failure.** A miss is the designed path: it is where the answer is
  produced and then stored.
- **Storing without a source, or with `low`.** Both exit 2 and record a refusal. Check the
  reason instead of retrying.
- **Storing an answer you did not get from the graph**, for example your own paraphrase.
  The cache stores validated answers, not notes.
- **Using the memory as a retrieval engine.** It matches exactly; it does not search, rank
  or summarize. Retrieval stays in the graph tools.
- **Letting the memory query the graph.** It has no client, no URL and no token, and it must
  stay that way: the version travels in from the caller.
- **Reporting a hit as fresh evidence when you did not check the version.** A hit is the same
  six-field ask, which is what makes it safe to reuse.
