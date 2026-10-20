# 09 — Retrieval pool recall: three ways to close the unmet `precision@k >= 0.15`

**Status:** decision document (ADR). No code, no schema and no data change is proposed here
beyond the design; every number below is measured and cited.

**Context in one paragraph.** The retrieval layer is measured on 12 `current` questions with 23
hand-verified reference chunks and `k = 10`: `precision@k = 0.1250` (15 hits / 120) against a
committed `precision_at_k_min = 0.1175`. The objective of the retrieval work was `>= 0.15` and it
is **unmet and declared**. A cross-encoder reranker was built, A/B-tested with three models and
**abandoned by measurement** (all three ordered the pool worse than Lucene's lexical score). The
labels themselves are sound: a read-only verification found all 23 references exist and are
indexed. What remains is a **candidate-recall** question, and this document decides how — and
whether — to attack it.

## 1. What is measured today (the facts every option is bounded by)

| fact | value | where it comes from |
|---|---|---|
| questions scored | 12 `current` (20 records; 8 `future` carry no references by design) | `data/evaluation/retrieval_dataset.jsonl` |
| reference chunks | 23 | same |
| `precision@10` (fulltext, chunk-only denominator) | **0.1250** = 15/120 | gate run 2026-10-15 (twice) |
| committed threshold | `precision_at_k_min = 0.1175` | `data/evaluation/retrieval_baseline.json` |
| objective (unmet) | **0.15** | original retrieval work |
| recall@10 / @30 / @40 / @50 | 0.6250 / 0.7083 / 0.7500 / **0.8333** | `scripts/eval_fulltext_recall.py` on production |
| ceiling at N=40 | 18/120 = **0.150** (zero margin) | same |
| ceiling at N=50 | 20/120 = **0.1667** | same |
| references in the pool at N=40 but **outside** the top-10 | 3 | A/B, 2026-10-15 |
| references that **never surface at any N** | 3 (`ret-005 …:980`, `ret-007 …:35`, `ret-018 …:1114`) | `scripts/verify_retrieval_references.py` |
| are those 3 reachable? | **yes**: they exist (970/1500/1500 chars) and the index finds them when queried with their own words | same |
| corpus | **3 468 chunks, 4 390 122 chars** (avg 1 265), 4 books (1520/989/657/302) | Cypher census, production |
| existing chunk index | `chunk_text_index`, FULLTEXT on `Chunk.text`, analyzer `standard-no-stop-words`, ONLINE | `SHOW INDEXES` |
| existing vector indexes | **none**; `Chunk` has 6 properties and no embedding; `Entity` has 0 embeddings | `SHOW INDEXES`, property census |
| Neo4j capability | 5.23 with `db.index.vector.createNodeIndex` / `queryNodes` / `queryRelationships` | `SHOW PROCEDURES` |
| embedding model already approved | `paraphrase-multilingual-MiniLM-L12-v2`, **384 dims** (+2 others) | `config.py` `_APPROVED_EMBEDDING_MODELS` |

**The mechanism that makes this hard, stated plainly:** `precision@k` is *set-based* — it counts
which chunks are in the returned top-10, not their order. Raising the fetch limit therefore
**cannot** move it on its own: the top-10 of a limit-50 fetch is the same top-10 the limit-10
fetch returns, because both come from the same Lucene scoring (this is why the earlier A/B needed
a reranker to have any effect at all, and why the reranker's failure was informative). Only a
change in **which ten** come back — a different candidate source, or a fusion of two sources — can
move the number. That single property rules out the tempting cheap option ("just fetch more") and
is the reason the three options below are what they are.

## 2. Alternative A — native vector hybrid with rank fusion (proposed)

**Shape.** Persist one embedding per chunk, declare a vector index on `:Chunk(embedding)`, add a
vector branch beside the fulltext branch, and fuse the two ranked lists.

| piece | today | what A needs | where it lives |
|---|---|---|---|
| chunk embeddings | none (0/3468) | one embedding per chunk, written to `Chunk.embedding` | a new ingestion/backfill step |
| vector index | none | `CREATE VECTOR INDEX … FOR (c:Chunk) ON (c.embedding) OPTIONS {vector.dimensions: 384, vector.similarity_function: 'cosine'}` | copy the entity template in `infrastructure/_neo4j_vector_cypher.py` |
| question embedding | port + adapter exist | inject the port into the query adapter (it takes only `Settings` today) | `infrastructure/neo4j_query_adapter.py` |
| vector query | pattern exists for entities | the same `db.index.vector.queryNodes` call without the entity filters | same module |
| fusion (RRF) | **nothing exists** | new code, deterministic, with a documented tie-break | a retrieval service above the adapter, or inside `search_chunks` |
| evaluation | `scripts/eval_fulltext_recall.py` already does recall@N and a chunk-only A/B | add a `hybrid` candidate source | same script |

**Why the pattern is cheap to mirror and why the work is not.** The *plumbing* is already written
for entities (`_ENSURE_VECTOR_INDEX`, `_SET_ENTITY_EMBEDDING`, `_VECTOR_QUERY`, and the
`Neo4jVectorCandidateRetrieval` adapter), and it is **switched off**: `candidate_retrieval_strategy`
defaults to `brute_force`, which is why production has zero entity embeddings and no vector index.
But the *chunk* side is net-new in three places (vectors at ingestion, the chunk index, the
fusion), and the entity types (`CandidateRetrievalRequest`, `CandidateHit`) are entity-shaped, so
they are a pattern to follow, not code to reuse.

**Computational cost (measured inputs, estimated outputs).**

| item | estimate | basis |
|---|---|---|
| corpus to embed | 3 468 chunks / **4.39 M chars** ≈ 1.1 M tokens | Cypher census |
| one-off embedding pass on CPU | **~30–60 s** for the corpus, plus ~5–10 s model load | MiniLM-L12 (6 layers, 384 dims) at a few hundred texts/s on a modern laptop CPU; **the installed `torch` here is `2.13.0+cpu`, so CUDA is not available even though the machine has an RTX 4050** |
| same pass on GPU | ~2–10 s | if a CUDA torch build were installed; not worth a dependency change for a one-off job |
| query-time embedding | ~10–30 ms per question | single short text |
| storage in Neo4j | 3 468 × 384 × 4 B ≈ **5.3 MB** + index overhead | float32 |
| LLM cost | **zero** | the model is local and already a dependency |
| production latency | +1 vector query and +1 fusion over the current single fulltext query | must be measured before shipping; `search_rag` is the user-facing path |

**Risk of mutating production, and how the migration stays idempotent.** Writing 3468 vectors and
declaring an index are **writes on the production graph**, which the project treats as gated
operations. The design that keeps it safe:

1. **Dry-run first**: report `chunks`, `chunks missing an embedding`, `chunks with a stale model`,
   `chars to embed`, and a **fingerprint** of the plan. No write.
2. **Backup** of the graph before the first write (the existing `backup_only.py` path), as §7.2
   requires for a mutating operation.
3. **Explicit approval**: an approval file whose content contains `approve`, plus
   `--expect-fingerprint` so the plan reviewed is the plan applied (the `resolve_intra_ns.py`
   precedent).
4. **Idempotent, additive pass**: stamp the node with what produced the vector
   (`chunk.embedding` + `chunk.embedding_model_id` + `chunk.embedding_dim`), and re-embed only
   where the property is absent **or the model stamp differs**. Identity is the pair the evaluation
   already uses (`book_id` + `chunk_index`, the same pair `Neo4jQueryAdapter._chunk_payload`
   composes into `chunk_id`); the pass never deletes a node, never touches `text`, and re-running
   it after an interruption is a no-op for the chunks already stamped.
5. **Index declaration**: `IF NOT EXISTS`, 384 dims, cosine. A vector index is **bound to its
   dimension**, so changing the model later means dropping and recreating the index — an
   operational constraint to document, not a silent path.
6. **Post-conditions**: census before/after (`chunks == 3468 == chunks with embedding`), the index
   `ONLINE`, and a smoke that `db.index.vector.queryNodes` returns a known neighbour for a known
   chunk (the topological check, not just a count).
7. **Rollback**: the change is additive and reversible by construction — `REMOVE c.embedding,
   c.embedding_model_id, c.embedding_dim` and `DROP INDEX chunk_embedding_index`. No chunk text,
   no edge and no merge marker is touched, so a rollback cannot lose graph data.

**Fusion design (RRF), and why not a weighted sum.** Lucene's `score` and a cosine similarity are
not on the same scale and neither is calibrated, so a weighted sum would need a normalisation
choice nobody can justify from the data. **Reciprocal Rank Fusion** needs no normalisation:
`score(d) = Σ 1/(k + rank_i(d))` with the conventional `k = 60`, computed over the two ranked
lists. It is deterministic, adds two parameters at most (the constant, and how many candidates
each branch contributes), and its failure mode is graceful (a chunk found by only one branch still
scores, just lower). Tie-break must be explicit and stable so the layer-4 metric stays
reproducible.

## 3. Alternative B — lexical tuning (analyzers, boosting, query shapes)

**Shape.** Improve the existing fulltext branch: a different analyzer, field boosting, phrase or
proximity queries, or query rewriting.

| tweak | plausible gain | constraint discovered in this audit |
|---|---|---|
| analyzer change (`standard-no-stop-words` → `standard`/`simple`) | marginal, and untested | **changing the analyzer requires dropping and recreating the index** (a DDL write, though no data write); re-population for 3468 chunks is seconds |
| field boosting / phrase queries | could promote the 3 references that *are* in the pool but outside the top-10 | needs a `chunk` scoring model that does not exist yet; Lucene's own scoring is what the current order already uses |
| synonyms | **not available** | Neo4j's built-in fulltext analyzers support `standard`, `simple`, `whitespace`, `keyword` and language variants — **there is no custom synonym-graph analyzer**, so synonyms would have to be *written into an indexed property at ingestion* (a data change of its own) |
| stemming/lemmatisation per language | untested | the corpus text is English while several entity names in the graph are Spanish, so language handling is already mixed |

**The honest bound on B:** the three references that never surface miss because the question's
vocabulary genuinely does not appear in the chunk (verified: the index finds each of them with
*their own* words). That is a **semantic** gap, and no analyzer, booster or synonym list fixes a
gap that is not lexical. B's realistic ceiling is the three in-pool-but-outside-top-10 references —
half of what the objective needs.

Cost: low (no embeddings, no writes beyond one index recreation). Risk: low, and mostly confined
to the evaluation. Measurement: the same script and dataset; a variant with the alternative
analyzer behind a setting.

## 4. Alternative C — keep the baseline

Cost zero. The gate keeps passing (`0.1250 >= 0.1175`), the objective stays declared unmet, and
the loss is the *signal*: the same three questions keep being unreachable and `precision@k` stops
being able to tell a real improvement from noise. This is a legitimate choice if the retrieval
layer's weight in the product is low — and note that **retrieval is an optional, non-blocking layer
in the readiness gate** (spec 06: structure, resolution and generation block; retrieval and
extraction never do).

## 5. Recommendation: A, but first a **read-only simulation** (phase 0)

The project's best decision this year was made by measuring before building: the reranker was
designed, implemented, A/B-tested against three models and then **abandoned** because the
measurement said so, which is why the retrieval layer still ships pure fulltext with a clean
conscience. The same discipline applies here, and it is cheap:

**Phase 0 — paper hybrid, no writes (recommended next step, ~1 hour).**
Embed the corpus **on this machine** (no production writes), compute the cosine ranking for the 12
`current` questions, fuse it with the fulltext ranking by RRF, and report `precision@10`, the
per-question deltas, and whether the three unreachable references enter the fused top-10. Cut
criterion, decided in advance: **if the simulated hybrid does not beat 0.1250, do not build A** —
and report the objective as unmet with the evidence. The probe that would answer this is an
extension of `scripts/eval_fulltext_recall.py` with a `--simulate-hybrid` mode reading local
embeddings; it writes nothing to the graph.

**Phase 1 — only if phase 0 wins.** In order: the placeholder ingestion step (idempotent, stamped),
the index DDL, the fusion behind a setting (`retrieval_strategy = fulltext | hybrid`, default
`fulltext` — mirroring the `candidate_retrieval_strategy` precedent), the evaluation, and a
re-baseline of `precision_at_k_min` only after the measurement.

**Phase 2 (independent, cheap, and worth doing in the same pass as phase 0's measurement)** — the
lexical tweaks of B that need no data write: measure the alternative analyzer and a phrase-query
variant inside the same simulation harness. If one of them alone reaches 0.15, A is not needed at
all.

## 6. What would be measured, and what would still be true afterwards

- The instrument is unchanged: 12 `current` questions, `k = 10`, the chunk-only denominator,
  YAML-free raw outputs from the same script; the retrieval layer keeps measuring the **pure
  fulltext** as its control so a hybrid change is always compared against the committed baseline.
- Acceptance: `precision@10 > 0.1250` and, if the objective is to be closed, `>= 0.15`; plus the
  three specific references entering the top-10 (a per-question check, not just an average).
- Latency and cost per question are part of the acceptance, because `search_rag` is user-facing.
- Nothing about the generation layer changes: its faithfulness (0.8988) is measured over the
  production path and does not depend on chunk ordering.

## 7. Open questions for the maintainer

1. **Is `>= 0.15` still the objective**, or has the bar moved now that the retrieval layer is known
   to be non-blocking for exposure?
2. **Is a production write acceptable in principle** (3468 vectors + one index), under the §7.2
   gate, if phase 0 says the hybrid wins?
3. **What is the latency budget** for a user-facing `search_rag` call, so the fusion can be sized
   against it?
4. **Should the retrieval layer keep the pure fulltext as a permanent control** (recommended), or
   measure only the shipping configuration?

## 8. Evidence index

- Feature document: `odd/tasks/retrieval-reranker-port.md` (L9/L10 — the A/B that killed the
  reranker, and the ceiling arithmetic) and `odd/tasks/eval-question-aware-global-retrieval.md`.
- Probes: `scripts/eval_fulltext_recall.py` (recall@N + A/B + the phase-0 hybrid simulation),
  `scripts/verify_retrieval_references.py` (labels exist and are indexed).
- Dormant entity infrastructure: `infrastructure/_neo4j_vector_cypher.py`,
  `infrastructure/neo4j_vector_candidate_retrieval.py`, `config.py::candidate_retrieval_strategy`.
- Live facts: `SHOW INDEXES`, `SHOW PROCEDURES`, the `Chunk` property census, the corpus census —
  all read-only, run 2026-10-16 against production through an SSH tunnel.

## 9. Phase 0 result (2026-10-16): the mechanism is real but does not pay for itself

The simulation of section 5 ran read-only against production: 3 468 chunks embedded locally with
`paraphrase-multilingual-MiniLM-L12-v2` (**~2 minutes on CPU**, no LLM cost), exact cosine over
the whole corpus, the real fulltext query, and RRF (`k = 60`, over-fetch 40). The control and the
hybrid are measured in the same run, with the same chunk-only denominator.

| fusion weights (vector, lexical) | `precision@10` | delta vs the in-run control |
|---|---|---|
| **(0.0, 1.0)** — sanity control: no vector weight | **0.1250** | **0.0000** (reproduces the fulltext exactly, so the fusion is not inventing hits) |
| (1.0, 1.0) — the value pre-registered before any result | **0.1083** | **-0.0167** |
| (0.3, 1.0) | **0.1333** | **+0.0083** |
| (1.0, 3.0) | **0.1333** | **+0.0083** |

What the numbers say, including the part that is inconvenient:

1. **The equal-weight fusion loses** (-0.0167): it promotes one reference and degrades three
   (ret-003 `…:1417`, ret-008 `…:60`, ret-013 `…:113`).
2. **A lexical-heavy fusion gains exactly one reference** — `+1/120 = +0.0083`, the smallest
   non-zero delta the metric can even express — and **degrades none**: per question, ret-011 goes
   from 0 to 1 hits and every other question keeps its count (`ret-005` and `ret-018` stay at
   0/2 and 0/1). That one reference is ret-011's `…:1453`: the mechanism works, cleanly, for
   precisely the case A was proposed for.
3. **Even the best case stays under the objective**: `0.1333 < 0.15`. In its most favourable
   measured configuration the vector branch does not close the gap the ADR set out to close.
4. **`ret-005` and `ret-018` are not rescued by vectors either.** Exact cosine over every chunk
   in the corpus still does not bring their references into the top-10, so those two are not a
   lexical limitation that a vector branch can repair.
5. **The passing rows come from weights chosen after seeing the loss.** Only 3 of the 4 rows were
   tried, and the two that "pass" sit exactly at the metric's minimum granularity — the
   multiple-comparison hazard the pre-agreed criterion exists to prevent. The pre-registered
   value is the one that failed.

**Consequence.** Investing in A means: embedding and **writing 3468 vectors to production**, a
vector index bound to 384 dimensions, a query-time embedding, a fusion with a weight
hyperparameter to tune and defend, and one more query of latency in the user-facing `search_rag`
— to gain **one** reference, still under target. **Recommendation: do not build A.** The honest
next cheap experiment is section 3's **B**: a query-shape variant (phrase/proximity) needs no data
write and no DDL and can be measured with this same script; an analyzer change needs one index
recreation (a DDL write, no data write). If B does not reach the objective either, **C** — keeping
`0.1250` with the objective declared unmet — is the evidence-backed answer, and retrieval remains
the optional, non-blocking layer it already is.

**Owner decision (2026-10-16).** **A is not built and B is not pursued for now.** The reasoning,
verbatim in substance: adding 3468 vector writes to production, a rigid 384-dimension index, extra
latency and a weight hyperparameter tuned against a single hit (+0.0083) violates parsimony. The
retrieval objective therefore stays **declared unmet at `0.1250`**, which passes its committed
threshold (`0.1175`), keeps the readiness gate green and blocks nothing — retrieval is an optional
layer. Option **C** is the adopted state of this document. The experiment, its numbers and its
negative result stay here and in `scripts/eval_fulltext_recall.py --simulate-hybrid` so the
question can be reopened with evidence rather than intuition; a future revisit needs a *different
measured premise* (a larger corpus, a different embedding model, or an objective that justifies
the production write).

## 10. Amendment (2026-10-19): phase 2 re-opened under a fixed cut criterion

**Decision record appended per the owner's approval (L2 of the task document). This section
amends; nothing above it is rewritten.** The historical phase-0 result (§9) and the 2026-10-16
owner decision ("A is not built and B is not pursued for now") stay exactly as written.

**What changed: nothing measured — a decision to measure.** Phase 2 (alternative B) is
re-opened on 2026-10-19 by the owner. Why now: the objective `precision@k >= 0.15` remains
**unmet and declared** at `0.1250`; the query-shape experiment needs **no data write** and no
DDL (a caller-side transform of the question); and B's own bound is already known (§3: the
three in-pool-but-outside-top-10 references). The previous decision is superseded on the
*measurement question only*.

**The cut criterion, fixed before any run (owner's verbatim rules, L1).**

- The objective stays **`0.150`**.
- A variant **ships** only if it beats the committed baseline `0.1250` by **at least one net
  reference** (+1/120 = 0.0083) **without losing any current reference** — a monotonic gain.
  A change that gains one and loses one is not an improvement.
- If no variant qualifies, **option C stays** and there is no third attempt.
- **At most two variants are pre-registered**: one query shape, one analyzer.

**The control row stays in the same run.** The pure-fulltext leg must reproduce `0.1250`
exactly; if it does not, the run is **void** — the control is the canary for measurement
drift, not a result to interpret.

**Pre-registered variant 1 — query shape (caller-side).** The fulltext query string is
caller-owned and bound verbatim as a Cypher parameter, so the phrase variant is a transform of
the question before the call: `scripts/eval_fulltext_recall.py --shape-ab` escapes the question
for Lucene (no escaping existed anywhere in the repository before this; an unescaped variant
would measure a syntax error) and wraps it in a single phrase. If it wins, the shape becomes a
setting mirroring `rerank_enabled` — not `candidate_retrieval_strategy`, which is declared and
consumed nowhere.

**Pre-registered variant 2 — analyzer (one gated DDL recreation).** The current analyzer
`standard-no-stop-words` (§1) is now declared explicitly in `ensure_indexes`' DDL — same
behaviour, the repository owns the fact. A recreation with a different analyzer is a scripted,
idempotent, dry-run-first operation (`scripts-ops/recreate_chunk_text_index.py`, approval file
plus reviewed-plan fingerprint, same contract as the merge batches).

**Latency budget.** The retrieval layer reports `latency_p95_graph_floor_ms` (nearest-rank p95
over `fetch_contexts` in the same run as precision, threshold 250 ms, `<=`). It is a **floor**:
if it exceeds 250 ms the variant is discarded immediately; if it sits comfortably below, the
MCP `search_rag` envelope needs **its own** benchmark with representative traffic before the
budget is claimed as met. The layer stays non-blocking either way.

**What voids this amendment: a variant that fails the cut criterion.** Then option C is the
evidence-backed state and phase 2 closes without a third attempt. The run itself belongs to the
operator (production graph, read-only); the numbers it produces will be appended here, not
substituted for anything above.
